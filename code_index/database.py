"""SQLite + sqlite-vec database layer for code index with timeout and error protection."""

import sqlite3
import struct
import json
import os
import hashlib
import re

try:
    import sqlite_vec
    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False

VECTOR_DIM = 384

# SQLite busy timeout in ms — how long to wait if another process holds the lock
SQLITE_BUSY_TIMEOUT = int(os.environ.get('CODE_INDEX_DB_TIMEOUT', 30000))


class CodeIndexDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT / 1000)
        self.conn.row_factory = sqlite3.Row
        # Set busy timeout so concurrent access doesn't immediately fail
        self.conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT}")
        # WAL mode for better concurrent read/write performance
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass  # WAL not supported on this platform/build, use default
        self._has_fts = False
        if HAS_SQLITE_VEC:
            try:
                self.conn.enable_load_extension(True)
                sqlite_vec.load(self.conn)
                self.conn.enable_load_extension(False)
            except Exception:
                # If loading fails, disable vector search but continue
                pass
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS code_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                symbol_name TEXT NOT NULL,
                symbol_type TEXT NOT NULL,
                line_start INTEGER,
                line_end INTEGER,
                source_code TEXT NOT NULL,
                docstring TEXT,
                decorators TEXT,
                parent_class TEXT,
                route_path TEXT,
                search_text TEXT NOT NULL,
                file_mtime REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_file ON code_chunks(file_path);
            CREATE INDEX IF NOT EXISTS idx_chunks_name ON code_chunks(symbol_name);
            CREATE INDEX IF NOT EXISTS idx_chunks_type ON code_chunks(symbol_type);

            -- Tracks per-file metadata for smart change detection
            CREATE TABLE IF NOT EXISTS file_tracking (
                file_path TEXT PRIMARY KEY,
                file_mtime REAL NOT NULL,
                content_hash TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                skipped INTEGER NOT NULL DEFAULT 0,
                skip_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- Cache embeddings by content hash to avoid re-embedding unchanged text
            CREATE TABLE IF NOT EXISTS embedding_cache (
                text_hash TEXT PRIMARY KEY,
                embedding BLOB NOT NULL
            );
        """)
        # vec0 virtual table created separately (requires sqlite-vec extension)
        if HAS_SQLITE_VEC:
            try:
                self.conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS code_chunks_vec USING vec0(
                        embedding float[{VECTOR_DIM}]
                    )
                """)
            except sqlite3.OperationalError:
                # vec0 module not available even though import succeeded
                pass
        self._setup_fts()
        self.conn.commit()

    def _has_vec_table(self):
        """Check if the vec0 virtual table actually exists and is usable."""
        try:
            self.conn.execute("SELECT COUNT(*) FROM code_chunks_vec").fetchone()
            return True
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return False

    def _setup_fts(self):
        """Set up FTS5 full-text search index for hybrid search."""
        try:
            self.conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS code_chunks_fts USING fts5(
                    symbol_name,
                    search_text,
                    tokenize='porter unicode61'
                )
            """)
            # Populate FTS from existing data (migration for pre-FTS indexes)
            fts_count = self.conn.execute(
                "SELECT COUNT(*) FROM code_chunks_fts"
            ).fetchone()[0]
            chunks_count = self.conn.execute(
                "SELECT COUNT(*) FROM code_chunks"
            ).fetchone()[0]
            if fts_count == 0 and chunks_count > 0:
                self.conn.execute("""
                    INSERT INTO code_chunks_fts(rowid, symbol_name, search_text)
                    SELECT id, symbol_name, search_text FROM code_chunks
                """)
            self._has_fts = True
        except Exception:
            self._has_fts = False

    def _serialize_vector(self, vec):
        return struct.pack(f'{len(vec)}f', *vec)

    def insert_chunk(self, chunk: dict, embedding: list):
        cursor = self.conn.execute(
            """INSERT INTO code_chunks
               (file_path, symbol_name, symbol_type, line_start, line_end,
                source_code, docstring, decorators, parent_class, route_path,
                search_text, file_mtime)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chunk['file_path'], chunk['symbol_name'], chunk['symbol_type'],
             chunk.get('line_start'), chunk.get('line_end'),
             chunk['source_code'], chunk.get('docstring'),
             json.dumps(chunk.get('decorators', [])),
             chunk.get('parent_class'), chunk.get('route_path'),
             chunk['search_text'], chunk['file_mtime'])
        )
        chunk_id = cursor.lastrowid
        if self._has_vec_table():
            try:
                self.conn.execute(
                    "INSERT INTO code_chunks_vec(rowid, embedding) VALUES (?, ?)",
                    (chunk_id, self._serialize_vector(embedding))
                )
            except (sqlite3.OperationalError, sqlite3.DatabaseError):
                pass  # Vector storage failed; text search still works
        if self._has_fts:
            try:
                self.conn.execute(
                    "INSERT INTO code_chunks_fts(rowid, symbol_name, search_text) VALUES (?, ?, ?)",
                    (chunk_id, chunk['symbol_name'], chunk['search_text'])
                )
            except (sqlite3.OperationalError, sqlite3.DatabaseError):
                pass  # FTS insert failed; basic search still works
        return chunk_id

    def insert_chunks_batch(self, chunks: list, embeddings: list):
        for chunk, embedding in zip(chunks, embeddings):
            self.insert_chunk(chunk, embedding)
        self.conn.commit()

    def search_similar(self, query_embedding: list, limit: int = 10):
        if not self._has_vec_table():
            return []
        try:
            rows = self.conn.execute(
                """SELECT v.rowid, v.distance, c.*
                   FROM code_chunks_vec v
                   INNER JOIN code_chunks c ON c.id = v.rowid
                   WHERE v.embedding MATCH ?
                     AND k = ?
                   ORDER BY v.distance""",
                (self._serialize_vector(query_embedding), limit)
            ).fetchall()
            return [dict(r) for r in rows]
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            # Vector search failed — return empty so hybrid search falls back to FTS
            return []

    def search_by_name(self, name: str, symbol_type: str = None):
        try:
            if symbol_type:
                rows = self.conn.execute(
                    """SELECT * FROM code_chunks
                       WHERE symbol_name LIKE ? AND symbol_type = ?
                       ORDER BY file_path, line_start""",
                    (f'%{name}%', symbol_type)
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """SELECT * FROM code_chunks
                       WHERE symbol_name LIKE ?
                       ORDER BY file_path, line_start""",
                    (f'%{name}%',)
                ).fetchall()
            return [dict(r) for r in rows]
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return []

    # ── Hybrid search (FTS5 + vector + type ranking) ─────────────

    @staticmethod
    def _sanitize_fts_query(query_text):
        """Convert natural language query to safe FTS5 query."""
        words = re.findall(r'\w+', query_text)
        if not words:
            return None
        return ' OR '.join(f'"{w}"' for w in words)

    def search_fts(self, query_text, limit=10):
        """Full-text search using FTS5 with BM25 ranking."""
        if not self._has_fts:
            return []
        safe_query = self._sanitize_fts_query(query_text)
        if not safe_query:
            return []
        try:
            rows = self.conn.execute(
                """SELECT c.*, fts.rank
                   FROM code_chunks_fts fts
                   INNER JOIN code_chunks c ON c.id = fts.rowid
                   WHERE code_chunks_fts MATCH ?
                   ORDER BY fts.rank
                   LIMIT ?""",
                (safe_query, limit)
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def search_hybrid(self, query_embedding, query_text, limit=10):
        """Combine vector similarity and full-text search using Reciprocal Rank Fusion."""
        k = 60  # RRF constant (standard value from literature)

        type_boost = {
            'function': 1.15, 'method': 1.15, 'class': 1.1,
            'route': 1.1, 'constant': 1.0,
            'file_summary': 0.7, 'section': 0.6,
        }

        fetch_limit = limit * 3
        vec_results = self.search_similar(query_embedding, limit=fetch_limit)
        fts_results = self.search_fts(query_text, limit=fetch_limit) if self._has_fts else []

        # If both searches returned nothing, try a simple LIKE fallback
        if not vec_results and not fts_results:
            return self._search_like_fallback(query_text, limit)

        scores = {}

        for rank, r in enumerate(vec_results):
            cid = r['id']
            scores[cid] = {'data': r, 'score': 1.0 / (k + rank + 1)}

        for rank, r in enumerate(fts_results):
            cid = r['id']
            rrf = 1.0 / (k + rank + 1)
            if cid in scores:
                scores[cid]['score'] += rrf
            else:
                scores[cid] = {'data': r, 'score': rrf}

        for entry in scores.values():
            boost = type_boost.get(entry['data'].get('symbol_type', ''), 0.8)
            entry['score'] *= boost

        ranked = sorted(scores.values(), key=lambda x: x['score'], reverse=True)
        return [{**e['data'], 'score': round(e['score'], 4)} for e in ranked[:limit]]

    def _search_like_fallback(self, query_text, limit=10):
        """Last-resort search using LIKE when vector and FTS both fail."""
        try:
            words = re.findall(r'\w+', query_text)
            if not words:
                return []
            # Search for the first meaningful word
            word = max(words, key=len)
            rows = self.conn.execute(
                """SELECT * FROM code_chunks
                   WHERE search_text LIKE ? OR symbol_name LIKE ?
                   ORDER BY file_path, line_start
                   LIMIT ?""",
                (f'%{word}%', f'%{word}%', limit)
            ).fetchall()
            return [dict(r) for r in rows]
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return []

    def get_file_symbols(self, file_path: str):
        try:
            rows = self.conn.execute(
                """SELECT * FROM code_chunks
                   WHERE file_path = ?
                   ORDER BY line_start""",
                (file_path,)
            ).fetchall()
            return [dict(r) for r in rows]
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return []

    def delete_file_chunks(self, file_path: str):
        try:
            ids = self.conn.execute(
                "SELECT id FROM code_chunks WHERE file_path = ?",
                (file_path,)
            ).fetchall()
            for row in ids:
                if self._has_vec_table():
                    try:
                        self.conn.execute(
                            "DELETE FROM code_chunks_vec WHERE rowid = ?",
                            (row[0],)
                        )
                    except (sqlite3.OperationalError, sqlite3.DatabaseError):
                        pass
                if self._has_fts:
                    try:
                        self.conn.execute(
                            "DELETE FROM code_chunks_fts WHERE rowid = ?",
                            (row[0],)
                        )
                    except (sqlite3.OperationalError, sqlite3.DatabaseError):
                        pass
            self.conn.execute(
                "DELETE FROM code_chunks WHERE file_path = ?",
                (file_path,)
            )
            self.conn.commit()
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            pass

    # ── File tracking for smart change detection ─────────────────

    def set_file_tracking(self, file_path: str, mtime: float, content_hash: str,
                          file_size: int, skipped: bool = False, skip_reason: str = None):
        self.conn.execute(
            """INSERT OR REPLACE INTO file_tracking
               (file_path, file_mtime, content_hash, file_size, skipped, skip_reason)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (file_path, mtime, content_hash, file_size, int(skipped), skip_reason)
        )

    def set_file_tracking_batch(self, entries: list):
        self.conn.executemany(
            """INSERT OR REPLACE INTO file_tracking
               (file_path, file_mtime, content_hash, file_size, skipped, skip_reason)
               VALUES (?, ?, ?, ?, ?, ?)""",
            entries
        )
        self.conn.commit()

    def get_file_tracking(self, file_path: str):
        row = self.conn.execute(
            "SELECT * FROM file_tracking WHERE file_path = ?",
            (file_path,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_file_tracking(self):
        rows = self.conn.execute("SELECT * FROM file_tracking").fetchall()
        return {r['file_path']: dict(r) for r in rows}

    def delete_file_tracking(self, file_path: str):
        self.conn.execute(
            "DELETE FROM file_tracking WHERE file_path = ?",
            (file_path,)
        )

    # ── Stats and meta ───────────────────────────────────────────

    def get_stats(self):
        try:
            total = self.conn.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0]
            files = self.conn.execute("SELECT COUNT(DISTINCT file_path) FROM code_chunks").fetchone()[0]
            types = self.conn.execute(
                "SELECT symbol_type, COUNT(*) FROM code_chunks GROUP BY symbol_type"
            ).fetchall()
            tracked = self.conn.execute("SELECT COUNT(*) FROM file_tracking").fetchone()[0]
            skipped = self.conn.execute(
                "SELECT COUNT(*) FROM file_tracking WHERE skipped = 1"
            ).fetchone()[0]
            return {
                'total_chunks': total,
                'total_files': files,
                'by_type': {r[0]: r[1] for r in types},
                'tracked_files': tracked,
                'skipped_files': skipped,
            }
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            return {
                'total_chunks': 0,
                'total_files': 0,
                'by_type': {},
                'tracked_files': 0,
                'skipped_files': 0,
                'error': str(e),
            }

    def get_meta(self, key: str):
        try:
            row = self.conn.execute(
                "SELECT value FROM index_meta WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return None

    def set_meta(self, key: str, value: str):
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                (key, value)
            )
            self.conn.commit()
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            pass

    def get_indexed_files(self):
        try:
            rows = self.conn.execute(
                "SELECT DISTINCT file_path, MAX(file_mtime) as mtime FROM code_chunks GROUP BY file_path"
            ).fetchall()
            return {r[0]: r[1] for r in rows}
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return {}

    # ── Embedding cache ───────────────────────────────────────────

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def get_cached_embedding(self, text_hash: str):
        """Return deserialized embedding list or None if not cached."""
        try:
            row = self.conn.execute(
                "SELECT embedding FROM embedding_cache WHERE text_hash = ?",
                (text_hash,)
            ).fetchone()
            if row is None:
                return None
            blob = row[0]
            count = len(blob) // 4
            return list(struct.unpack(f'{count}f', blob))
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return None

    def get_cached_embeddings_batch(self, text_hashes: list) -> dict:
        """Return {text_hash: embedding_list} for all cached hashes."""
        if not text_hashes:
            return {}
        try:
            placeholders = ','.join('?' * len(text_hashes))
            rows = self.conn.execute(
                f"SELECT text_hash, embedding FROM embedding_cache WHERE text_hash IN ({placeholders})",
                text_hashes
            ).fetchall()
            result = {}
            for row in rows:
                blob = row[1]
                count = len(blob) // 4
                result[row[0]] = list(struct.unpack(f'{count}f', blob))
            return result
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return {}

    def set_cached_embeddings_batch(self, pairs: list):
        """Store [(text_hash, embedding_list), ...] into the cache."""
        try:
            self.conn.executemany(
                "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding) VALUES (?, ?)",
                [(h, self._serialize_vector(e)) for h, e in pairs]
            )
            self.conn.commit()
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            pass

    def clear_all(self):
        try:
            if self._has_vec_table():
                self.conn.execute("DELETE FROM code_chunks_vec")
            self.conn.execute("DELETE FROM code_chunks")
            if self._has_fts:
                self.conn.execute("DELETE FROM code_chunks_fts")
            self.conn.execute("DELETE FROM file_tracking")
            self.conn.execute("DELETE FROM index_meta")
            self.conn.commit()
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            pass

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
