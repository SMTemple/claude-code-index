"""Build/update orchestrator for the code index with smart change detection and timeout protection."""

import json
import os
import re
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
        self._generate_project_summary(has_meaningful_changes=True)

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
        self._generate_project_summary(has_meaningful_changes=bool(needs_index or needs_delete))

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

    def _detect_domain(self) -> str:
        """Extract production domain/URL from .env, wp-config.php, or similar config files."""
        # Candidate paths to search (project root + common web root locations)
        search_roots = [self.project_root]
        for subdir in ('public_html', 'restore/public_html', 'web', 'html', 'www'):
            p = self.project_root / subdir
            if p.is_dir():
                search_roots.append(p)

        # 1. .env files — look for URL keys first, then derive from email domain
        for root in search_roots:
            env_path = root / '.env'
            if not env_path.exists():
                continue
            try:
                env = env_path.read_text(encoding='utf-8', errors='replace')
                # Explicit URL keys
                m = re.search(
                    r'(?:APP_URL|SITE_URL|WP_HOME|WP_SITEURL|BASE_URL|APP_DOMAIN)\s*=\s*["\']?(https?://[^\s"\']+)',
                    env, re.IGNORECASE
                )
                if m:
                    return m.group(1).rstrip('/')
                # Derive from email address (e.g. MAIL_FROM_ADDRESS=no-reply@example.com)
                m = re.search(
                    r'MAIL_(?:FROM_ADDRESS|USERNAME)\s*=\s*["\']?[^@\s"\']+@([a-z0-9.-]+\.[a-z]{2,})',
                    env, re.IGNORECASE
                )
                if m:
                    return m.group(1)
            except Exception:
                pass

        # 2. wp-config.php — WP_HOME / WP_SITEURL
        for root in search_roots:
            wpcfg = root / 'wp-config.php'
            if not wpcfg.exists():
                continue
            try:
                raw = wpcfg.read_text(encoding='utf-8', errors='replace')[:4000]
                # Strip commented lines so a stale `// define('WP_HOME', ...)` is ignored
                content = '\n'.join(
                    l for l in raw.splitlines()
                    if not l.lstrip().startswith(('//', '#'))
                )
                m = re.search(
                    r"define\s*\(\s*['\"]WP_(?:HOME|SITEURL)['\"]\s*,\s*['\"]([^'\"]+)['\"]",
                    content
                )
                if m:
                    return m.group(1).rstrip('/')
            except Exception:
                pass

        return ''

    def _detect_platform(self, tracked_paths: list) -> str:
        """Identify CMS or framework from tracked file paths and known marker files."""
        paths_str = ' '.join(tracked_paths)

        # Optional in-house platform, configured out-of-band so an organisation's
        # internal CMS name and its directory fingerprint stay out of this repo
        # (which has a public mirror). Set BOTH to enable:
        #   CODE_INDEX_CUSTOM_PLATFORM  label to report, e.g. 'Acme CMS'
        #   CODE_INDEX_CUSTOM_MARKER    identifying path fragment, forward-slash
        #                               normalised, e.g. 'sources/init/'
        # Checked first so an in-house platform wins over the generic markers
        # below. If either is unset the check is skipped entirely.
        custom_label = os.environ.get('CODE_INDEX_CUSTOM_PLATFORM', '').strip()
        custom_marker = os.environ.get('CODE_INDEX_CUSTOM_MARKER', '').strip()
        if custom_label and custom_marker and custom_marker in paths_str:
            return custom_label

        # WordPress
        if any('wp-content/' in p for p in tracked_paths):
            return 'WordPress'
        for subdir in ('', 'public_html/', 'restore/public_html/'):
            if (self.project_root / subdir / 'wp-config.php').exists():
                return 'WordPress'

        # Common frameworks via marker files
        markers = {
            'artisan': 'Laravel',
            'manage.py': 'Django',
            'next.config.js': 'Next.js',
            'next.config.ts': 'Next.js',
            'nuxt.config.js': 'Nuxt.js',
            'nuxt.config.ts': 'Nuxt.js',
            'svelte.config.js': 'SvelteKit',
            'astro.config.mjs': 'Astro',
        }
        for marker, label in markers.items():
            if (self.project_root / marker).exists():
                return label

        return ''

    def _detect_web_root(self) -> str:
        """Find the subdirectory containing the actual site files."""
        for subdir in ('public_html', 'restore/public_html', 'web', 'html', 'www', 'htdocs', 'webroot'):
            p = self.project_root / subdir
            if p.is_dir() and any((p / f).exists() for f in ('index.php', 'wp-config.php', 'index.html')):
                return subdir
        return ''

    def _detect_local_dev_url(self) -> str:
        """Find local dev URL from docker-compose port mapping."""
        for dc_path in (
            self.project_root / 'docker' / 'docker-compose.yml',
            self.project_root / 'docker-compose.yml',
            self.project_root / 'docker' / 'docker-compose.yaml',
            self.project_root / 'docker-compose.yaml',
        ):
            if not dc_path.exists():
                continue
            try:
                content = dc_path.read_text(encoding='utf-8', errors='replace')
                m = re.search(r'^\s*-?\s*["\']?(\d{3,5}):(?:80|443)["\']?', content, re.MULTILINE)
                if m:
                    scheme = 'https' if ':443' in m.group(0) else 'http'
                    return f'{scheme}://localhost:{m.group(1)}'
            except Exception:
                pass
        return ''

    def _detect_php_version(self) -> str:
        """Detect PHP version from .php-version, Dockerfile, or composer.json."""

        pv = self.project_root / '.php-version'
        if pv.exists():
            try:
                return pv.read_text(encoding='utf-8').strip()
            except Exception:
                pass

        for df in (self.project_root / 'docker' / 'Dockerfile', self.project_root / 'Dockerfile'):
            if df.exists():
                try:
                    content = df.read_text(encoding='utf-8', errors='replace')[:500]
                    m = re.search(r'FROM\s+php:(\d+\.\d+)', content, re.IGNORECASE)
                    if m:
                        return m.group(1)
                except Exception:
                    pass

        search_roots = [self.project_root]
        for sub in ('public_html', 'restore/public_html'):
            p = self.project_root / sub
            if p.is_dir():
                search_roots.append(p)
        for root in search_roots:
            composer = root / 'composer.json'
            if composer.exists():
                try:
                    data = json.loads(composer.read_text(encoding='utf-8'))
                    php_req = data.get('require', {}).get('php', '')
                    if php_req:
                        m = re.search(r'(\d+\.\d+)', php_req)
                        if m:
                            return m.group(1) + '+'
                except Exception:
                    pass

        return ''

    def _detect_db_name(self) -> str:
        """Extract database name from .env."""
        search_roots = [self.project_root]
        for sub in ('public_html', 'restore/public_html'):
            p = self.project_root / sub
            if p.is_dir():
                search_roots.append(p)
        for root in search_roots:
            env_path = root / '.env'
            if env_path.exists():
                try:
                    env = env_path.read_text(encoding='utf-8', errors='replace')
                    m = re.search(
                        r'(?:DB_NAME|DATABASE_NAME|MYSQL_DATABASE|MARIADB_DATABASE)\s*=\s*["\']?([^\s"\']+)',
                        env, re.IGNORECASE
                    )
                    if m:
                        return m.group(1)
                except Exception:
                    pass
        return ''

    def _detect_cache_layers(self, tracked_paths: list) -> list:
        """Identify WordPress cache/CDN plugins from tracked file paths."""
        CACHE_PLUGINS = {
            'wp-rocket': 'WP Rocket',
            'w3-total-cache': 'W3 Total Cache',
            'sucuri-scanner': 'Sucuri',
            'litespeed-cache': 'LiteSpeed Cache',
            'wp-super-cache': 'WP Super Cache',
            'wp-fastest-cache': 'WP Fastest Cache',
            'breeze': 'Breeze (Cloudways)',
            'wp-cloudflare-page-cache': 'Cloudflare Page Cache',
            'autoptimize': 'Autoptimize',
            'sg-cachepress': 'SG Optimizer',
            'endurance-page-cache': 'Endurance Page Cache',
        }
        paths_str = ' '.join(tracked_paths)
        return [name for slug, name in CACHE_PLUGINS.items()
                if f'wp-content/plugins/{slug}/' in paths_str]

    def _detect_wp_theme(self, tracked_paths: list) -> str:
        """Identify non-default WordPress theme(s) from tracked paths and filesystem."""
        DEFAULT_THEMES = {
            'twentytwenty', 'twentytwentyone', 'twentytwentytwo', 'twentytwentythree',
            'twentytwentyfour', 'twentytwentyfive', 'twentynineteen', 'twentyeighteen',
            'twentyseventeen', 'twentysixteen', 'twentyfifteen',
        }
        themes = set()

        for path in tracked_paths:
            parts = path.split('/')
            for i, part in enumerate(parts):
                if (part == 'themes' and i > 0 and parts[i - 1] == 'wp-content'
                        and i + 1 < len(parts) and parts[i + 1] not in DEFAULT_THEMES):
                    themes.add(parts[i + 1])

        theme_dirs = [self.project_root / 'wp-content' / 'themes']
        for sub in ('public_html', 'restore/public_html'):
            theme_dirs.append(self.project_root / sub / 'wp-content' / 'themes')
        for themes_dir in theme_dirs:
            if themes_dir.is_dir():
                try:
                    for td in themes_dir.iterdir():
                        if td.is_dir() and td.name not in DEFAULT_THEMES and not td.name.startswith('.'):
                            themes.add(td.name)
                except Exception:
                    pass

        return ', '.join(sorted(themes)) if themes else ''

    def _generate_project_summary(self, has_meaningful_changes: bool = True):
        """Write .code_index/PROJECT_SUMMARY.md with static analysis + Haiku description."""
        import datetime
        from collections import Counter

        if not has_meaningful_changes:
            return

        project_name = self.project_root.name
        summary_path = self.index_dir / 'PROJECT_SUMMARY.md'

        EXT_TO_LANG = {
            '.py': 'Python', '.js': 'JavaScript', '.ts': 'TypeScript',
            '.jsx': 'JSX', '.tsx': 'TSX', '.php': 'PHP', '.rb': 'Ruby',
            '.go': 'Go', '.rs': 'Rust', '.java': 'Java', '.cs': 'C#',
            '.cpp': 'C++', '.c': 'C', '.swift': 'Swift', '.kt': 'Kotlin',
            '.html': 'HTML', '.css': 'CSS', '.scss': 'SCSS', '.sass': 'Sass',
            '.sql': 'SQL', '.sh': 'Shell', '.ps1': 'PowerShell',
            '.lua': 'Lua', '.vue': 'Vue', '.svelte': 'Svelte', '.astro': 'Astro',
        }

        tracked = {}
        ext_counts = Counter()
        try:
            tracked = self.db.get_all_file_tracking()
            for fpath, info in tracked.items():
                if not info.get('skipped'):
                    ext = Path(fpath).suffix.lower()
                    if ext:
                        ext_counts[ext] += 1
        except Exception:
            pass

        languages = []
        for ext, count in ext_counts.most_common(8):
            lang = EXT_TO_LANG.get(ext, ext.lstrip('.').upper())
            languages.append(f"{lang} ({count})")

        skip_names = {'node_modules', '__pycache__', 'vendor', '.git', '.code_index',
                      'dist', 'build', 'out', '.next', '.nuxt', 'uploads',
                      'wp-admin', 'wp-includes'}
        top_items = []
        try:
            for item in sorted(self.project_root.iterdir()):
                if item.name in skip_names or item.name.startswith('.'):
                    continue
                top_items.append(item.name + ('/' if item.is_dir() else ''))
        except Exception:
            pass

        # --- Static facts (always accurate — not LLM-generated) ---
        tracked_paths = list(tracked.keys())
        detected_domain = self._detect_domain()
        platform = self._detect_platform(tracked_paths)
        web_root = self._detect_web_root()
        local_dev_url = self._detect_local_dev_url()
        php_version = self._detect_php_version()
        db_name = self._detect_db_name()
        cache_layers = self._detect_cache_layers(tracked_paths)
        wp_theme = self._detect_wp_theme(tracked_paths)

        # --- Context files for LLM ---
        config_snippets = []
        for fname in ('package.json', 'composer.json', 'pyproject.toml',
                      'go.mod', 'requirements.txt', 'Cargo.toml', 'Gemfile'):
            fpath = self.project_root / fname
            if fpath.exists():
                try:
                    content = fpath.read_text(encoding='utf-8', errors='replace')[:600]
                    config_snippets.append(f"=== {fname} ===\n{content}")
                except Exception:
                    pass

        readme_content = ''
        for name in ('README.md', 'readme.md', 'README.txt', 'README'):
            rpath = self.project_root / name
            if rpath.exists():
                try:
                    readme_content = rpath.read_text(encoding='utf-8', errors='replace')[:2000]
                except Exception:
                    pass
                break

        # Project-local CLAUDE.md (not the global one) carries useful context
        project_claude_md = ''
        for claude_path in (self.project_root / 'CLAUDE.md',
                             self.project_root / '.claude' / 'CLAUDE.md'):
            if claude_path.exists():
                try:
                    project_claude_md = claude_path.read_text(encoding='utf-8', errors='replace')[:1500]
                except Exception:
                    pass
                break

        # --- LLM-generated sections ---
        llm_output = ''
        try:
            import anthropic
            api_key = os.environ.get('ANTHROPIC_API_KEY')
            if api_key:
                context = f"Project folder name: {project_name}\n"
                if detected_domain:
                    context += f"Production domain/URL: {detected_domain}\n"
                if platform:
                    context += f"CMS/Platform: {platform}\n"
                if web_root:
                    context += f"Web root: {web_root}/\n"
                if php_version:
                    context += f"PHP version: {php_version}\n"
                if db_name:
                    context += f"Database: {db_name}\n"
                if cache_layers:
                    context += f"Cache layers: {', '.join(cache_layers)}\n"
                if wp_theme:
                    context += f"WordPress theme: {wp_theme}\n"
                if languages:
                    context += f"Languages: {', '.join(languages)}\n"
                if top_items:
                    context += "Top-level structure:\n" + '\n'.join(f"  {i}" for i in top_items[:20]) + '\n'
                for snippet in config_snippets:
                    context += f"\n{snippet}\n"
                if readme_content:
                    context += f"\nREADME:\n{readme_content}\n"
                if project_claude_md:
                    context += f"\nProject CLAUDE.md:\n{project_claude_md}\n"

                prompt = (
                    "Analyze this project and write three markdown sections.\n"
                    "Use the domain, platform, and CLAUDE.md to be specific — avoid generic descriptions.\n\n"
                    "## Purpose\n"
                    "1-2 sentences describing the site/app and its production domain if known. "
                    "Name the site by its actual name, not the folder name.\n\n"
                    "## Frameworks & Tools\n"
                    "Bullet list of key frameworks, libraries, and build tools. "
                    "If the CMS/platform is known, name it explicitly.\n\n"
                    "## Special Considerations\n"
                    "Bullet list of anything a developer must know before touching this code: "
                    "deployment quirks, known fragile areas, auth patterns, external service dependencies, "
                    "things that have broken before (from CLAUDE.md if present). "
                    "Omit this section entirely if there is nothing notable.\n\n"
                    "Be terse. No intro or closing text. Only what's genuinely useful.\n\n"
                    f"---\n{context}"
                )

                client = anthropic.Anthropic(api_key=api_key)
                response = client.messages.create(
                    model='claude-haiku-4-5-20251001',
                    max_tokens=600,
                    messages=[{'role': 'user', 'content': prompt}],
                )
                llm_output = response.content[0].text.strip()
        except Exception:
            pass

        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        lines = [
            f"# {project_name}",
            "",
            f"> Auto-generated by code-index · {timestamp}",
            "",
        ]

        if detected_domain:
            lines += [f"**Domain:** {detected_domain}", ""]
        if platform:
            lines += [f"**Platform:** {platform}", ""]

        env_items = []
        if web_root:
            env_items.append(f"- **Web root:** `{web_root}/`")
        if local_dev_url:
            env_items.append(f"- **Local dev:** {local_dev_url}")
        if php_version:
            env_items.append(f"- **PHP:** {php_version}")
        if db_name:
            env_items.append(f"- **Database:** `{db_name}`")
        if cache_layers:
            env_items.append(f"- **Cache layers:** {', '.join(cache_layers)}")
        if wp_theme:
            env_items.append(f"- **Theme:** `{wp_theme}`")
        if env_items:
            lines += ["## Environment"] + env_items + [""]

        lines += [
            "## Languages",
            ', '.join(languages) if languages else '_not detected_',
            "",
            "## Structure",
            "```",
            *top_items[:20],
            "```",
            "",
        ]

        if llm_output:
            lines.append(llm_output)
        else:
            lines.append("_LLM description unavailable — set ANTHROPIC_API_KEY to enable._")

        try:
            summary_path.write_text('\n'.join(lines), encoding='utf-8')
        except Exception:
            pass

    def close(self):
        if self._db:
            self._db.close()
