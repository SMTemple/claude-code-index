"""SQLite + sqlite-vec database layer for code index -- lock-free reads, batched writes.

Concurrency model:
  - WRITE operations serialise via _write_lock (RLock with timeout) on a single
    write connection (self.conn).
  - READ operations use per-thread read-only connections (via threading.local())
    with NO Python-level lock.  SQLite WAL mode allows concurrent readers that
    never block on (or get blocked by) writers.
  - insert_chunks_batch releases the write lock between each batch commit so
    concurrent reads are never starved during large indexing operations.
"""

import sqlite3
import struct
import json
import os
import hashlib
import re
import threading
from contextlib import contextmanager

try:
    import sqlite_vec
    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False

VECTOR_DIM = 384

# SQLite busy timeout in ms -- how long to wait if another process holds the lock
SQLITE_BUSY_TIMEOUT = int(os.environ.get('CODE_INDEX_DB_TIMEOUT', 30000))

# Max number of SQL parameters per query (SQLite default limit is 999)
_SQL_VAR_BATCH = 500

# How many chunks to insert before issuing a COMMIT (keeps lock hold time short)
_WRITE_BATCH_SIZE = 50

# Max seconds to wait for the Python-level write lock before raising an error
_WRITE_LOCK_TIMEOUT = int(os.environ.get('CODE_INDEX_WRITE_TIMEOUT', 30))


class CodeIndexDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._write_lock = threading.RLock()
        self._local = threading.local()  # per-thread read connections
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = self._open_connection()
        self._has_fts = False
        self._has_vec = False  # cached after table creation
        if HAS_SQLITE_VEC:
            try:
                self.conn.enable_load_extension(True)
                sqlite_vec.load(self.conn)
                self.conn.enable_load_extension(False)
            except Exception:
                pass
        self._create_tables()

    # -- Connection management -------------------------------------------------

    def _open_connection(self):
        """Open a new SQLite connection with standard pragmas."""
        conn = sqlite3.connect(
            self.db_path,
            timeout=SQLITE_BUSY_TIMEOUT / 1000,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT}")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.OperationalError:
            pass
        return conn

    def _get_read_conn(self):
        """Return a per-thread read connection.  No Python lock needed -- WAL
        allows unlimited concurrent readers without blocking writers."""
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            return conn
        conn = self._open_connection()
        if HAS_SQLITE_VEC:
            try:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
            except Exception:
                pass
        self._local.conn = conn
        return conn

    @contextmanager
    def _write_ctx(self):
        """Context manager: acquire write lock with timeout, yield write connection."""
        acquired = self._write_lock.acquire(timeout=_WRITE_LOCK_TIMEOUT)
        if not acquired:
            raise sqlite3.OperationalError(
                f"Database write lock timeout after {_WRITE_LOCK_TIMEOUT}s -- "
                "another write operation is in progress"
            )
        try:
            yield self.conn
        finally:
            self._write_lock.release()

    # -- Schema creation -------------------------------------------------------
    # Uses individual execute() calls instead of executescript() because
    # executescript() does NOT respect busy_timeout and can hang when
    # another process holds the database lock.

    def _create_tables(self):
        with self._write_ctx() as c:
            c.execute("""
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
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_chunks_file ON code_chunks(file_path)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_chunks_name ON code_chunks(symbol_name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_chunks_type ON code_chunks(symbol_type)")

            c.execute("""
                CREATE TABLE IF NOT EXISTS file_tracking (
                    file_path TEXT PRIMARY KEY,
                    file_mtime REAL NOT NULL,
                    content_hash TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    skip_reason TEXT
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS index_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    text_hash TEXT PRIMARY KEY,
                    embedding BLOB NOT NULL
                )
            """)

            # vec0 virtual table (requires sqlite-vec extension)
            if HAS_SQLITE_VEC:
                try:
                    c.execute(f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS code_chunks_vec USING vec0(
                            embedding float[{VECTOR_DIM}]
                        )
                    """)
                except sqlite3.OperationalError:
                    pass

            # FTS5 index -- called while lock is held
            self._setup_fts()
            c.commit()

            # Cache extension availability (won't change during runtime)
            self._has_vec = self._check_vec_table()

    def _check_vec_table(self):
        """Check if the vec0 virtual table exists and is usable.
        NOTE: Called during init with write lock held."""
        try:
            self.conn.execute("SELECT COUNT(*) FROM code_chunks_vec").fetchone()
            return True
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return False

    def _setup_fts(self):
        """Set up FTS5 full-text search index for hybrid search.
        NOTE: Called from _create_tables while write lock is held."""
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

    # -- Serialization helpers -------------------------------------------------

    def _serialize_vector(self, vec):
        return struct.pack(f'{len(vec)}f', *vec)

    # -- Insert operations (write lock) ----------------------------------------

    def _insert_chunk_unlocked(self, chunk: dict, embedding: list):
        """Insert a single chunk. Caller MUST hold write lock."""
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
        if self._has_vec:
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

    def insert_chunk(self, chunk: dict, embedding: list):
        with self._write_ctx():
            cid = self._insert_chunk_unlocked(chunk, embedding)
        return cid

    def insert_chunks_batch(self, chunks: list, embeddings: list):
        """Insert chunks in batches, releasing the write lock between each
        batch commit so concurrent reads are never starved."""
        total = len(chunks)
        for batch_start in range(0, total, _WRITE_BATCH_SIZE):
            batch_end = min(batch_start + _WRITE_BATCH_SIZE, total)
            with self._write_ctx():
                for i in range(batch_start, batch_end):
                    self._insert_chunk_unlocked(chunks[i], embeddings[i])
                try:
                    self.conn.commit()
                except (sqlite3.OperationalError, sqlite3.DatabaseError):
                    pass

    # -- Search operations (lock-free reads) -----------------------------------

    def search_similar(self, query_embedding: list, limit: int = 10):
        if not self._has_vec:
            return []
        try:
            conn = self._get_read_conn()
            rows = conn.execute(
                """SELECT v.rowid, v.distance, c.*
                   FROM code_chunks_vec v
                   INNER JOIN code_chunks c ON c.id = v.rowid
                   WHERE v.embedding MATCH ?
                     AND k = ?
                   ORDER BY v.distance""",
                (self._serialize_vector(query_embedding), limit)
            ).fetchall()
            return [dict(r) for r in rows]
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            # Vector search failed -- return empty so hybrid search falls back to FTS
            return []

    def search_by_name(self, name: str, symbol_type: str = None):
        try:
            conn = self._get_read_conn()
            if symbol_type:
                rows = conn.execute(
                    """SELECT * FROM code_chunks
                       WHERE symbol_name LIKE ? AND symbol_type = ?
                       ORDER BY file_path, line_start""",
                    (f'%{name}%', symbol_type)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM code_chunks
                       WHERE symbol_name LIKE ?
                       ORDER BY file_path, line_start""",
                    (f'%{name}%',)
                ).fetchall()
            return [dict(r) for r in rows]
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return []

    # -- Hybrid search (FTS5 + vector + type ranking) --------------------------

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
            conn = self._get_read_conn()
            rows = conn.execute(
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
        """Combine vector similarity and full-text search using Reciprocal Rank Fusion (RRF)."""
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
            conn = self._get_read_conn()
            rows = conn.execute(
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
            conn = self._get_read_conn()
            rows = conn.execute(
                """SELECT * FROM code_chunks
                   WHERE file_path = ?
                   ORDER BY line_start""",
                (file_path,)
            ).fetchall()
            return [dict(r) for r in rows]
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return []

    # -- Delete operations (write lock) ----------------------------------------

    def delete_file_chunks(self, file_path: str):
        try:
            with self._write_ctx():
                ids = self.conn.execute(
                    "SELECT id FROM code_chunks WHERE file_path = ?",
                    (file_path,)
                ).fetchall()
                for row in ids:
                    if self._has_vec:
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

    # -- File tracking ---------------------------------------------------------

    def set_file_tracking(self, file_path: str, mtime: float, content_hash: str,
                          file_size: int, skipped: bool = False, skip_reason: str = None):
        with self._write_ctx():
            self.conn.execute(
                """INSERT OR REPLACE INTO file_tracking
                   (file_path, file_mtime, content_hash, file_size, skipped, skip_reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (file_path, mtime, content_hash, file_size, int(skipped), skip_reason)
            )

    def set_file_tracking_batch(self, entries: list):
        with self._write_ctx():
            self.conn.executemany(
                """INSERT OR REPLACE INTO file_tracking
                   (file_path, file_mtime, content_hash, file_size, skipped, skip_reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                entries
            )
            self.conn.commit()

    def get_file_tracking(self, file_path: str):
        try:
            conn = self._get_read_conn()
            row = conn.execute(
                "SELECT * FROM file_tracking WHERE file_path = ?",
                (file_path,)
            ).fetchone()
            return dict(row) if row else None
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return None

    def get_all_file_tracking(self):
        try:
            conn = self._get_read_conn()
            rows = conn.execute("SELECT * FROM file_tracking").fetchall()
            return {r['file_path']: dict(r) for r in rows}
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return {}

    def delete_file_tracking(self, file_path: str):
        with self._write_ctx():
            self.conn.execute(
                "DELETE FROM file_tracking WHERE file_path = ?",
                (file_path,)
            )

    # -- Stats and meta --------------------------------------------------------

    def get_stats(self):
        try:
            conn = self._get_read_conn()
            total = conn.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0]
            files = conn.execute("SELECT COUNT(DISTINCT file_path) FROM code_chunks").fetchone()[0]
            types = conn.execute(
                "SELECT symbol_type, COUNT(*) FROM code_chunks GROUP BY symbol_type"
            ).fetchall()
            tracked = conn.execute("SELECT COUNT(*) FROM file_tracking").fetchone()[0]
            skipped = conn.execute(
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
            conn = self._get_read_conn()
            row = conn.execute(
                "SELECT value FROM index_meta WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return None

    def set_meta(self, key: str, value: str):
        try:
            with self._write_ctx():
                self.conn.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                    (key, value)
                )
                self.conn.commit()
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            pass

    def get_indexed_files(self):
        try:
            conn = self._get_read_conn()
            rows = conn.execute(
                "SELECT DISTINCT file_path, MAX(file_mtime) as mtime FROM code_chunks GROUP BY file_path"
            ).fetchall()
            return {r[0]: r[1] for r in rows}
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return {}

    # -- Embedding cache -------------------------------------------------------

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def get_cached_embedding(self, text_hash: str):
        """Return deserialized embedding list or None if not cached."""
        try:
            conn = self._get_read_conn()
            row = conn.execute(
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
        """Return {text_hash: embedding_list} for all cached hashes.

        Batches the IN clause to avoid hitting SQLite's variable limit (default 999).
        """
        if not text_hashes:
            return {}
        result = {}
        try:
            conn = self._get_read_conn()
            for start in range(0, len(text_hashes), _SQL_VAR_BATCH):
                batch = text_hashes[start:start + _SQL_VAR_BATCH]
                placeholders = ','.join('?' * len(batch))
                rows = conn.execute(
                    f"SELECT text_hash, embedding FROM embedding_cache "
                    f"WHERE text_hash IN ({placeholders})",
                    batch
                ).fetchall()
                for row in rows:
                    blob = row[1]
                    count = len(blob) // 4
                    result[row[0]] = list(struct.unpack(f'{count}f', blob))
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            pass
        return result

    def set_cached_embeddings_batch(self, pairs: list):
        """Store [(text_hash, embedding_list), ...] into the cache."""
        try:
            with self._write_ctx():
                self.conn.executemany(
                    "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding) VALUES (?, ?)",
                    [(h, self._serialize_vector(e)) for h, e in pairs]
                )
                self.conn.commit()
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            pass

    def clear_all(self):
        try:
            with self._write_ctx():
                if self._has_vec:
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
            # Flush the write-ahead log back into the main DB before closing.
            # Short-lived reindex processes otherwise append to the WAL and exit
            # without checkpointing, so it grows unbounded. A bloated WAL forces
            # every reader (incl. the long-lived MCP server connections) to scan
            # it on each query, which surfaces as DB "locking"/slow searches.
            #
            # PASSIVE (not TRUNCATE/FULL): never blocks on an active reader. A
            # TRUNCATE checkpoint waits up to busy_timeout (30s) for the MCP
            # server's read connections to release the WAL, which could stall
            # process exit; it also silently no-ops if a reader is mid-query.
            # PASSIVE always flushes the committed frames it can and lets the
            # WAL reset/reuse in place, keeping it bounded without the hang risk.
            # Serialized under _write_lock so it can't race an in-flight write
            # on the shared self.conn.
            try:
                with self._write_lock:
                    self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
            self.conn.close()
        except Exception:
            pass
