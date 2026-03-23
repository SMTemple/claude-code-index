"""Build/update orchestrator for the code index with smart change detection and timeout protection."""

import os
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from pathlib import Path

from .database import CodeIndexDB
from .parser import CodeParser
from .embeddings import embed_text, embed_batch, EmbeddingError

# In-memory LRU cache for query embeddings to avoid re-computing on repeated searches
_QUERY_CACHE_MAX = 128

# Per-file parse timeout (seconds)
FILE_PARSE_TIMEOUT = int(os.environ.get('CODE_INDEX_PARSE_TIMEOUT', 30))
# Overall indexing timeout (seconds) — 0 means no limit
INDEXING_TIMEOUT = int(os.environ.get('CODE_INDEX_TIMEOUT', 600))


class IndexingError(Exception):
    """Raised when indexing encounters an unrecoverable error."""
    pass


class CodeIndexer:
    LOCK_FILE_NAME = '.reindex.lock'

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        self.index_dir = self.project_root / '.code_index'
        self.db_path = str(self.index_dir / 'code_index.db')
        self.lock_path = str(self.index_dir / self.LOCK_FILE_NAME)
        self._db = None
        self._parser = CodeParser(str(self.project_root))
        self._query_cache = OrderedDict()  # LRU cache: query_text -> embedding

    @property
    def db(self):
        if self._db is None:
            self._db = CodeIndexDB(self.db_path)
        return self._db

    def is_reindex_locked(self):
        """Check if another process is currently reindexing.

        Returns False if the lock is held by the current process (same PID),
        so that force_reindex -> ensure_index call chains work correctly.
        """
        if not os.path.exists(self.lock_path):
            return False
        # Check if the current process owns this lock
        try:
            with open(self.lock_path, 'r') as f:
                lock_pid = f.read().strip()
            if lock_pid == str(os.getpid()):
                return False  # We own this lock — not blocked
        except (OSError, ValueError):
            pass
        # Stale lock protection: if lock is older than 15 minutes, ignore it
        try:
            age = time.time() - os.path.getmtime(self.lock_path)
            if age > 900:
                try:
                    os.remove(self.lock_path)
                except OSError:
                    pass
                return False
        except OSError:
            return False
        return True

    def _acquire_lock(self):
        """Create a lock file to signal reindex in progress."""
        os.makedirs(self.index_dir, exist_ok=True)
        try:
            with open(self.lock_path, 'w') as f:
                f.write(str(os.getpid()))
        except OSError:
            pass

    def _release_lock(self):
        """Remove the lock file."""
        try:
            os.remove(self.lock_path)
        except OSError:
            pass

    def ensure_index(self, progress_callback=None):
        """Auto-build if missing, auto-update only truly changed files."""
        # Skip if another process is already reindexing
        if self.is_reindex_locked():
            return

        if not os.path.exists(self.db_path):
            # Acquire lock to prevent concurrent builds
            self._acquire_lock()
            try:
                self.build_full_index(progress_callback=progress_callback)
            finally:
                self._release_lock()
            return

        try:
            tracked = self.db.get_all_file_tracking()
        except Exception as e:
            # DB is corrupted or unreadable — rebuild from scratch
            if progress_callback:
                progress_callback('init', 0, 1, f'DB error ({e}), rebuilding...')
            self._acquire_lock()
            try:
                self.build_full_index(progress_callback=progress_callback)
            finally:
                self._release_lock()
            return

        current_files = {}
        for f in self._parser.discover_files():
            rel = str(f.relative_to(self.project_root)).replace('\\', '/')
            try:
                stat = f.stat()
                current_files[rel] = (f, stat.st_mtime, stat.st_size)
            except OSError:
                pass

        needs_index = []
        needs_delete = []
        tracking_updates = []

        for fpath, (full_path, mtime, size) in current_files.items():
            prev = tracked.get(fpath)

            # File not tracked at all -> new file
            if prev is None:
                skip_reason = CodeParser.should_skip_file(full_path)
                content_hash = CodeParser.compute_file_hash(full_path)
                if skip_reason:
                    tracking_updates.append(
                        (fpath, mtime, content_hash, size, 1, skip_reason)
                    )
                else:
                    needs_index.append(fpath)
                    tracking_updates.append(
                        (fpath, mtime, content_hash, size, 0, None)
                    )
                continue

            # mtime unchanged -> definitely skip (fast path)
            if mtime == prev['file_mtime'] and size == prev['file_size']:
                continue

            # mtime changed -> check content hash to see if content actually changed
            content_hash = CodeParser.compute_file_hash(full_path)
            if content_hash == prev['content_hash']:
                # Content identical, just update mtime in tracking
                tracking_updates.append(
                    (fpath, mtime, content_hash, size, prev['skipped'], prev['skip_reason'])
                )
                continue

            # Content truly changed -> re-check if it should be skipped
            skip_reason = CodeParser.should_skip_file(full_path)
            if skip_reason:
                # Was indexed before but now should be skipped (became auto-generated?)
                if not prev['skipped']:
                    needs_delete.append(fpath)
                tracking_updates.append(
                    (fpath, mtime, content_hash, size, 1, skip_reason)
                )
            else:
                needs_index.append(fpath)
                tracking_updates.append(
                    (fpath, mtime, content_hash, size, 0, None)
                )

        # Files that were tracked but no longer exist on disk
        deleted = [f for f in tracked if f not in current_files]
        needs_delete.extend(deleted)

        if needs_index or needs_delete or tracking_updates:
            self._acquire_lock()
            try:
                self._incremental_update(needs_index, needs_delete, tracking_updates,
                                         progress_callback=progress_callback)
            finally:
                self._release_lock()

    def build_full_index(self, progress_callback=None):
        """Build the entire index from scratch with smart filtering."""
        start = time.time()
        os.makedirs(self.index_dir, exist_ok=True)

        self.db.clear_all()

        if progress_callback:
            progress_callback('discover', 0, 0, 'Discovering files...')

        files = self._parser.discover_files()
        total_files = len(files)

        if progress_callback:
            progress_callback('discover', total_files, total_files,
                              f'Found {total_files} files to process')

        all_chunks = []
        tracking_entries = []
        indexed_count = 0
        skipped_count = 0

        # Phase 1: Filter files (fast sequential pass)
        to_parse = []
        for f in files:
            rel_path = str(f.relative_to(self.project_root)).replace('\\', '/')
            try:
                stat = f.stat()
            except OSError:
                skipped_count += 1
                continue
            content_hash = CodeParser.compute_file_hash(f)

            skip_reason = CodeParser.should_skip_file(f)
            if skip_reason:
                tracking_entries.append(
                    (rel_path, stat.st_mtime, content_hash, stat.st_size, 1, skip_reason)
                )
                skipped_count += 1
                if progress_callback:
                    progress_callback('parse', skipped_count, total_files,
                                      f'Skipped: {rel_path}')
                continue

            to_parse.append((f, rel_path, stat, content_hash))

        # Phase 2: Parse files in parallel with per-file timeout
        max_workers = min(8, (os.cpu_count() or 4))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_info = {
                executor.submit(self._parser.parse_file, f): (f, rel_path, stat, content_hash)
                for f, rel_path, stat, content_hash in to_parse
            }

            for future in as_completed(future_to_info):
                f, rel_path, stat, content_hash = future_to_info[future]
                try:
                    chunks = future.result(timeout=FILE_PARSE_TIMEOUT)
                except FuturesTimeoutError:
                    # File parse hung — skip it and continue
                    chunks = []
                    if progress_callback:
                        progress_callback('parse', skipped_count + indexed_count, total_files,
                                          f'Timeout: {rel_path} (skipped)')
                except Exception:
                    chunks = []

                all_chunks.extend(chunks)
                tracking_entries.append(
                    (rel_path, stat.st_mtime, content_hash, stat.st_size, 0, None)
                )
                indexed_count += 1

                if progress_callback:
                    progress_callback('parse', skipped_count + indexed_count, total_files,
                                      f'{rel_path} ({len(chunks)} chunks)')

                # Check overall timeout
                if INDEXING_TIMEOUT and (time.time() - start) > INDEXING_TIMEOUT:
                    if progress_callback:
                        progress_callback('parse', skipped_count + indexed_count, total_files,
                                          f'Timeout after {INDEXING_TIMEOUT}s — saving partial index')
                    # Cancel remaining futures
                    for remaining in future_to_info:
                        remaining.cancel()
                    break

        # Phase 3: Embed all chunks
        if all_chunks:
            if progress_callback:
                progress_callback('embed', 0, len(all_chunks),
                                  f'Embedding {len(all_chunks)} chunks...')

            try:
                # Check embedding cache — only embed chunks whose text has changed
                text_hashes = [self.db.hash_text(c['search_text']) for c in all_chunks]
                cached = self.db.get_cached_embeddings_batch(text_hashes)

                uncached_indices = [i for i, h in enumerate(text_hashes) if h not in cached]
                cache_hits = len(all_chunks) - len(uncached_indices)
                if uncached_indices:
                    uncached_texts = [all_chunks[i]['search_text'] for i in uncached_indices]

                    def _embed_progress(current, total):
                        if progress_callback:
                            done = cache_hits + current
                            progress_callback('embed', done, len(all_chunks),
                                              f'Embedded {done}/{len(all_chunks)} chunks ({cache_hits} cached)')

                    if progress_callback and cache_hits:
                        progress_callback('embed', cache_hits, len(all_chunks),
                                          f'{cache_hits} cached, embedding {len(uncached_indices)} new...')

                    new_embeddings = embed_batch(uncached_texts, progress_callback=_embed_progress)
                    new_pairs = [(text_hashes[i], new_embeddings[j])
                                 for j, i in enumerate(uncached_indices)]
                    self.db.set_cached_embeddings_batch(new_pairs)
                    for j, i in enumerate(uncached_indices):
                        cached[text_hashes[i]] = new_embeddings[j]

                embeddings = [cached[h] for h in text_hashes]

                if progress_callback:
                    progress_callback('embed', len(all_chunks), len(all_chunks),
                                      f'Embedding complete ({cache_hits} cached, {len(uncached_indices)} new)')
            except EmbeddingError as e:
                # Embedding failed — store chunks without embeddings
                # (text/FTS search will still work, vector search won't)
                if progress_callback:
                    progress_callback('embed', len(all_chunks), len(all_chunks),
                                      f'Embedding failed: {e} — storing without vectors')
                embeddings = [([0.0] * 384) for _ in all_chunks]

            if progress_callback:
                progress_callback('store', 0, 1, 'Storing in database...')

            try:
                self.db.insert_chunks_batch(all_chunks, embeddings)
            except Exception as e:
                if progress_callback:
                    progress_callback('store', 1, 1, f'DB store error: {e}')

            if progress_callback:
                progress_callback('store', 1, 1, 'Database updated')

        if tracking_entries:
            self.db.set_file_tracking_batch(tracking_entries)

        elapsed = time.time() - start
        self.db.set_meta('last_full_build', str(time.time()))
        self.db.set_meta('build_time_seconds', f'{elapsed:.1f}')
        self.db.set_meta('total_files', str(indexed_count))
        self.db.set_meta('skipped_files', str(skipped_count))
        self.db.set_meta('project_root', str(self.project_root))

    def force_reindex(self, full: bool = True, progress_callback=None):
        """Force rebuild. If full=True, clears all data and starts fresh.
        If full=False, runs incremental update (only changed files)."""
        self._acquire_lock()
        try:
            if full:
                if os.path.exists(self.db_path):
                    try:
                        # Try to delete the DB file for a clean slate
                        if self._db:
                            self._db.close()
                            self._db = None
                        os.remove(self.db_path)
                    except (PermissionError, OSError):
                        # DB is locked by another process (e.g. MCP server) — clear tables instead
                        try:
                            self.db.clear_all()
                        except Exception:
                            pass
                    if progress_callback:
                        progress_callback('init', 0, 1, 'Cleared old index')
                self.build_full_index(progress_callback=progress_callback)
            else:
                self.ensure_index(progress_callback=progress_callback)
        finally:
            self._release_lock()

    def _incremental_update(self, needs_index, needs_delete, tracking_updates,
                             progress_callback=None):
        start = time.time()
        total_steps = len(needs_delete) + len(needs_index)
        step = 0

        # Remove deleted/stale files
        for fpath in needs_delete:
            try:
                self.db.delete_file_chunks(fpath)
                self.db.delete_file_tracking(fpath)
            except Exception:
                pass
            step += 1
            if progress_callback:
                progress_callback('cleanup', step, total_steps,
                                  f'Removed: {fpath}')

        # Re-index changed files (delete old chunks first)
        for fpath in needs_index:
            try:
                self.db.delete_file_chunks(fpath)
            except Exception:
                pass

        # Parse new/changed files with per-file timeout protection
        all_chunks = []
        with ThreadPoolExecutor(max_workers=1) as parse_executor:
            for i, fpath in enumerate(needs_index):
                full_path = self.project_root / fpath
                if full_path.exists():
                    try:
                        future = parse_executor.submit(self._parser.parse_file, full_path)
                        chunks = future.result(timeout=FILE_PARSE_TIMEOUT)
                    except FuturesTimeoutError:
                        chunks = []
                        if progress_callback:
                            progress_callback('parse', step, total_steps,
                                              f'Timeout: {fpath} (skipped)')
                    except Exception:
                        chunks = []
                    all_chunks.extend(chunks)
                    step += 1
                    if progress_callback:
                        progress_callback('parse', step, total_steps,
                                          f'Parsed: {fpath} ({len(chunks)} chunks)')

        if all_chunks:
            if progress_callback:
                progress_callback('embed', 0, len(all_chunks),
                                  f'Embedding {len(all_chunks)} chunks...')

            try:
                # Check embedding cache
                text_hashes = [self.db.hash_text(c['search_text']) for c in all_chunks]
                cached = self.db.get_cached_embeddings_batch(text_hashes)

                uncached_indices = [i for i, h in enumerate(text_hashes) if h not in cached]
                cache_hits = len(all_chunks) - len(uncached_indices)
                if uncached_indices:
                    uncached_texts = [all_chunks[i]['search_text'] for i in uncached_indices]

                    def _embed_progress(current, total):
                        if progress_callback:
                            done = cache_hits + current
                            progress_callback('embed', done, len(all_chunks),
                                              f'Embedded {done}/{len(all_chunks)} chunks ({cache_hits} cached)')

                    if progress_callback and cache_hits:
                        progress_callback('embed', cache_hits, len(all_chunks),
                                          f'{cache_hits} cached, embedding {len(uncached_indices)} new...')

                    new_embeddings = embed_batch(uncached_texts, progress_callback=_embed_progress)
                    new_pairs = [(text_hashes[i], new_embeddings[j])
                                 for j, i in enumerate(uncached_indices)]
                    self.db.set_cached_embeddings_batch(new_pairs)
                    for j, i in enumerate(uncached_indices):
                        cached[text_hashes[i]] = new_embeddings[j]

                embeddings = [cached[h] for h in text_hashes]
            except EmbeddingError as e:
                if progress_callback:
                    progress_callback('embed', len(all_chunks), len(all_chunks),
                                      f'Embedding failed: {e} — storing without vectors')
                embeddings = [([0.0] * 384) for _ in all_chunks]

            try:
                self.db.insert_chunks_batch(all_chunks, embeddings)
            except Exception:
                pass

            if progress_callback:
                progress_callback('embed', len(all_chunks), len(all_chunks),
                                  'Embedding complete')

        # Update all tracking records
        if tracking_updates:
            self.db.set_file_tracking_batch(tracking_updates)

        elapsed = time.time() - start
        self.db.set_meta('last_incremental_update', str(time.time()))
        self.db.set_meta('incremental_time_seconds', f'{elapsed:.1f}')
        self.db.set_meta('files_reindexed', str(len(needs_index)))
        self.db.set_meta('files_deleted', str(len(needs_delete)))

    def _get_query_embedding(self, query: str):
        """Get embedding for a search query, using in-memory LRU cache."""
        if query in self._query_cache:
            self._query_cache.move_to_end(query)
            return self._query_cache[query]
        try:
            vec = embed_text(query)
            self._query_cache[query] = vec
            if len(self._query_cache) > _QUERY_CACHE_MAX:
                self._query_cache.popitem(last=False)
            return vec
        except EmbeddingError:
            return None

    def _require_index(self):
        """Check that the index DB exists. Returns True if ready, False if not."""
        return os.path.exists(self.db_path)

    def search_code(self, query: str, limit: int = 10):
        if not self._require_index():
            return []
        try:
            query_vec = self._get_query_embedding(query)
            if query_vec is not None:
                return self.db.search_hybrid(query_vec, query, limit)
            # Embedding unavailable — use text search only
            return self.db.search_fts(query, limit) or self.db._search_like_fallback(query, limit)
        except EmbeddingError:
            # Vector search unavailable — fall back to text search
            return self.db.search_fts(query, limit) or self.db._search_like_fallback(query, limit)
        except Exception:
            return []

    def search_symbol(self, name: str, symbol_type: str = None):
        if not self._require_index():
            return []
        try:
            return self.db.search_by_name(name, symbol_type)
        except Exception:
            return []

    def get_file_overview(self, file_path: str):
        if not self._require_index():
            return []
        try:
            file_path = file_path.replace('\\', '/')
            return self.db.get_file_symbols(file_path)
        except Exception:
            return []

    def get_status(self):
        if not os.path.exists(self.db_path):
            return {
                'indexed': False,
                'message': 'No index exists. Will be built on first search.'
            }
        try:
            stats = self.db.get_stats()
            return {
                'indexed': True,
                'total_chunks': stats['total_chunks'],
                'total_files': stats['total_files'],
                'by_type': stats['by_type'],
                'tracked_files': stats['tracked_files'],
                'skipped_files': stats['skipped_files'],
                'last_full_build': self.db.get_meta('last_full_build'),
                'build_time_seconds': self.db.get_meta('build_time_seconds'),
                'project_root': self.db.get_meta('project_root'),
            }
        except Exception as e:
            return {
                'indexed': False,
                'message': f'Index exists but is unreadable: {e}',
            }

    def close(self):
        if self._db:
            self._db.close()
