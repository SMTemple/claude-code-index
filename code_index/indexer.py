"""Build/update orchestrator for the code index with smart change detection."""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .database import CodeIndexDB
from .parser import CodeParser
from .embeddings import embed_text, embed_batch


class CodeIndexer:
    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        self.index_dir = self.project_root / '.code_index'
        self.db_path = str(self.index_dir / 'code_index.db')
        self._db = None
        self._parser = CodeParser(str(self.project_root))

    @property
    def db(self):
        if self._db is None:
            self._db = CodeIndexDB(self.db_path)
        return self._db

    def ensure_index(self, progress_callback=None):
        """Auto-build if missing, auto-update only truly changed files."""
        if not os.path.exists(self.db_path):
            self.build_full_index(progress_callback=progress_callback)
            return

        tracked = self.db.get_all_file_tracking()
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

            # File not tracked at all → new file
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

            # mtime unchanged → definitely skip (fast path)
            if mtime == prev['file_mtime'] and size == prev['file_size']:
                continue

            # mtime changed → check content hash to see if content actually changed
            content_hash = CodeParser.compute_file_hash(full_path)
            if content_hash == prev['content_hash']:
                # Content identical, just update mtime in tracking
                tracking_updates.append(
                    (fpath, mtime, content_hash, size, prev['skipped'], prev['skip_reason'])
                )
                continue

            # Content truly changed → re-check if it should be skipped
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
            self._incremental_update(needs_index, needs_delete, tracking_updates,
                                     progress_callback=progress_callback)

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
            stat = f.stat()
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

        # Phase 2: Parse files in parallel
        max_workers = min(8, (os.cpu_count() or 4))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_info = {
                executor.submit(self._parser.parse_file, f): (f, rel_path, stat, content_hash)
                for f, rel_path, stat, content_hash in to_parse
            }
            for future in as_completed(future_to_info):
                f, rel_path, stat, content_hash = future_to_info[future]
                try:
                    chunks = future.result()
                except Exception:
                    chunks = []
                all_chunks.extend(chunks)
                tracking_entries.append(
                    (rel_path, stat.st_mtime, content_hash, stat.st_size, 0, None)
                )
                indexed_count += 1

                if progress_callback:
                    progress_callback('parse', skipped_count + indexed_count, total_files,
                                      f'Parsed: {rel_path} ({len(chunks)} chunks)')

        if all_chunks:
            if progress_callback:
                progress_callback('embed', 0, len(all_chunks),
                                  f'Embedding {len(all_chunks)} chunks...')

            # Check embedding cache — only embed chunks whose text has changed
            text_hashes = [self.db.hash_text(c['search_text']) for c in all_chunks]
            cached = self.db.get_cached_embeddings_batch(text_hashes)

            uncached_indices = [i for i, h in enumerate(text_hashes) if h not in cached]
            if uncached_indices:
                uncached_texts = [all_chunks[i]['search_text'] for i in uncached_indices]
                new_embeddings = embed_batch(uncached_texts)
                new_pairs = [(text_hashes[i], new_embeddings[j])
                             for j, i in enumerate(uncached_indices)]
                self.db.set_cached_embeddings_batch(new_pairs)
                for j, i in enumerate(uncached_indices):
                    cached[text_hashes[i]] = new_embeddings[j]

            embeddings = [cached[h] for h in text_hashes]

            cache_hits = len(all_chunks) - len(uncached_indices)
            if progress_callback:
                progress_callback('embed', len(all_chunks), len(all_chunks),
                                  f'Embedding complete ({cache_hits} cached, {len(uncached_indices)} new)')

            if progress_callback:
                progress_callback('store', 0, 1, 'Storing in database...')

            self.db.insert_chunks_batch(all_chunks, embeddings)

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
        """Force rebuild. If full=True, clears all data and starts fresh."""
        if full and os.path.exists(self.db_path):
            try:
                # Try to delete the DB file for a clean slate
                if self._db:
                    self._db.close()
                    self._db = None
                os.remove(self.db_path)
            except (PermissionError, OSError):
                # DB is locked by another process (e.g. MCP server) — clear tables instead
                self.db.clear_all()
            if progress_callback:
                progress_callback('init', 0, 1, 'Cleared old index')
        self.build_full_index(progress_callback=progress_callback)

    def _incremental_update(self, needs_index, needs_delete, tracking_updates,
                             progress_callback=None):
        start = time.time()
        total_steps = len(needs_delete) + len(needs_index)
        step = 0

        # Remove deleted/stale files
        for fpath in needs_delete:
            self.db.delete_file_chunks(fpath)
            self.db.delete_file_tracking(fpath)
            step += 1
            if progress_callback:
                progress_callback('cleanup', step, total_steps,
                                  f'Removed: {fpath}')

        # Re-index changed files (delete old chunks first)
        for fpath in needs_index:
            self.db.delete_file_chunks(fpath)

        # Parse and embed new/changed files
        all_chunks = []
        for i, fpath in enumerate(needs_index):
            full_path = self.project_root / fpath
            if full_path.exists():
                chunks = self._parser.parse_file(full_path)
                all_chunks.extend(chunks)
                step += 1
                if progress_callback:
                    progress_callback('parse', step, total_steps,
                                      f'Parsed: {fpath} ({len(chunks)} chunks)')

        if all_chunks:
            if progress_callback:
                progress_callback('embed', 0, len(all_chunks),
                                  f'Embedding {len(all_chunks)} chunks...')

            # Check embedding cache
            text_hashes = [self.db.hash_text(c['search_text']) for c in all_chunks]
            cached = self.db.get_cached_embeddings_batch(text_hashes)

            uncached_indices = [i for i, h in enumerate(text_hashes) if h not in cached]
            if uncached_indices:
                uncached_texts = [all_chunks[i]['search_text'] for i in uncached_indices]
                new_embeddings = embed_batch(uncached_texts)
                new_pairs = [(text_hashes[i], new_embeddings[j])
                             for j, i in enumerate(uncached_indices)]
                self.db.set_cached_embeddings_batch(new_pairs)
                for j, i in enumerate(uncached_indices):
                    cached[text_hashes[i]] = new_embeddings[j]

            embeddings = [cached[h] for h in text_hashes]
            self.db.insert_chunks_batch(all_chunks, embeddings)

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

    def search_code(self, query: str, limit: int = 10):
        self.ensure_index()
        query_vec = embed_text(query)
        return self.db.search_hybrid(query_vec, query, limit)

    def search_symbol(self, name: str, symbol_type: str = None):
        self.ensure_index()
        return self.db.search_by_name(name, symbol_type)

    def get_file_overview(self, file_path: str):
        self.ensure_index()
        file_path = file_path.replace('\\', '/')
        return self.db.get_file_symbols(file_path)

    def get_status(self):
        if not os.path.exists(self.db_path):
            return {
                'indexed': False,
                'message': 'No index exists. Will be built on first search.'
            }
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

    def close(self):
        if self._db:
            self._db.close()
