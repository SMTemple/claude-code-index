"""Multi-language code parser with smart file filtering."""

import ast
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import List, Dict, Tuple, Optional

logger = logging.getLogger(__name__)

# Directories to always skip (exact basename match at any depth)
SKIP_DIRS = {
    '__pycache__', '.git', '.svn', '.hg',
    'venv', '.venv', 'env', '.env',
    'node_modules', 'bower_components',
    '.code_index', '.tox', '.mypy_cache', '.pytest_cache',
    'dist', 'build', '_build', '.build', 'out', 'output',
    'site-packages', '_internal',
    'archives', 'backups', 'logs', 'tmp', '.tmp',
    '.next', '.nuxt', '.output',
    'coverage', '.coverage', 'htmlcov',
    '.terraform', '.serverless',
    'vendor', '.eggs',
    # IDE / editor
    '.idea', '.vs', '.vscode',
    # Language-specific build/cache
    'target', '.cargo', 'obj', 'bin',
    '.gradle', '.maven',
    # JS/TS tooling caches
    '.cache', '.parcel-cache', '.turbo', '.swc',
    '.yarn', '.pnp', '.angular', '.expo',
    # Deployment / hosting
    '.vercel', '.netlify', '.amplify',
    # Sass / CSS caches
    '.sass-cache',
    # Mac zip artifacts
    '__MACOSX',
    # WordPress core (never edit, unambiguous names)
    'wp-admin', 'wp-includes',
    # WordPress core clone directory (Docker/local-dev installs)
    'wordpress',
    # WordPress large/junk dirs (unambiguous names)
    'uploads',                  # wp-content/uploads — media library
    'ai1wm-backups',            # All-in-One WP Migration backups
    'updraft',                  # UpdraftPlus backups
    'wp-rocket-config',         # WP Rocket per-host config
    'w3tc-config',              # W3 Total Cache config
    'wflogs',                   # Wordfence logs
    'endurance-page-cache',     # Bluehost/Endurance page cache
    'upgrade-temp-backup',      # WP core auto-update temp dir
    'breeze-cache',             # Cloudways Breeze cache
    'litespeed-cache',          # LiteSpeed cache (when as a dir)
    'et-cache',                 # Elegant Themes (Divi) cache
    'smush-webp',               # Smush image optimization output
    # WordPress default bundled themes — pure boilerplate, never edited
    'twentyfifteen', 'twentysixteen', 'twentyseventeen', 'twentyeighteen',
    'twentynineteen', 'twentytwenty', 'twentytwentyone', 'twentytwentytwo',
    'twentytwentythree', 'twentytwentyfour', 'twentytwentyfive',
    # Popular premium parent themes — custom code is always in a child theme
    'Divi', 'Avada', 'Jupiter', 'Salient', 'Betheme', 'X', 'enfold',
    # Old / archived copies of code
    'old', '.old',
}

# Directories to skip ONLY when their direct parent has one of these names.
# Useful for generic names ('cache', 'upgrade') that are safe to index in
# arbitrary projects but are junk inside specific contexts (wp-content).
PARENT_SCOPED_SKIP_DIRS = {
    'wp-content': {
        'cache',          # any cache plugin's output
        'upgrade',        # WP core upgrade staging
        'wp-rocket',      # WP Rocket cache (per-host subdirs)
        'plugins',        # 3rd-party plugins — custom code lives in themes/mu-plugins
        'plugins-old',
        'themes-old',
        'mu-plugins-old',
        'languages',      # auto-generated translations
    },
    # mu-plugins DOES hold our custom code, so it is not skipped wholesale.
    # But managed hosts inject their own platform mu-plugins there, and those
    # are vendor code we never edit. Skip the known host-injected ones by name.
    'mu-plugins': {
        # WP Engine
        'wpengine-common', 'wpe-cache-plugin', 'wpe-update-source-selector',
        'wpe-wp-sign-on-plugin', 'object-cache-pro',
        # Other managed hosts
        'endurance-page-cache',   # Bluehost/Endurance
        'kinsta-mu-plugins',      # Kinsta
        'wp-stack-cache',         # Cloudways
        'pantheon-mu-plugin',     # Pantheon
    },
}

# A wp-content root does not always sit in a folder literally named
# 'wp-content'. All-in-One WP Migration (.wpress) archives extract wp-content's
# *contents* to the archive root, so the tree looks like
# `<pull-dir>/plugins`, `<pull-dir>/themes`, ... and every 'wp-content'
# parent-scoped rule above would silently never fire. Detect the directory by
# its shape instead of its name so those rules still apply.
# (Observed 2026-08-04: a .wpress pull indexed 30,210 chunks of third-party
# plugin code — 91% of the whole index — purely because of this.)
WP_CONTENT_MARKER_DIRS = {'plugins', 'themes', 'mu-plugins', 'uploads'}
WP_CONTENT_MIN_MARKERS = 3


def looks_like_wp_content(dir_names, file_names=()) -> bool:
    """True when a directory's children look unmistakably like wp-content's.

    Marker dirs alone are NOT sufficient: `plugins/` + `themes/` + `uploads/`
    is a plausible shape for a non-WordPress plugin-architecture app, and a
    false positive here silently prunes real source from the index. So a match
    also requires one WordPress-specific corroborating signal:

      * `mu-plugins/` — a near-unique WordPress term, or
      * an `index.php` file — WordPress ships wp-content/index.php ("silence
        is golden"), which a Node/Python project with those dirs would not have.

    Erring toward a false NEGATIVE is deliberate: failing to detect means we
    index some vendor noise (visible, annoying), while a false positive means
    we silently drop real code (invisible, harmful).
    """
    matched = WP_CONTENT_MARKER_DIRS.intersection(dir_names)
    if len(matched) < WP_CONTENT_MIN_MARKERS:
        return False
    return 'mu-plugins' in matched or 'index.php' in file_names

# Directory name suffixes that should also be skipped (e.g. *.egg-info, *.old)
SKIP_DIR_SUFFIXES = ('.egg-info', '.old')

# Directories to skip ONLY when they appear at the project root.
# Useful for short/ambiguous names that are meaningful deep in a tree
# (e.g. a `n` dir in node_modules) but are scratch/notes folders at the root.
ROOT_ONLY_SKIP_DIRS = {
    'n',
}

# File extensions to index, grouped by parse strategy
PYTHON_EXTS = {'.py'}
JS_TS_EXTS = {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'}
PHP_EXTS = {'.php', '.phtml', '.php3', '.php4', '.php5', '.phps'}
WEB_EXTS = {'.html', '.htm', '.css', '.scss', '.less', '.vue', '.svelte'}
CONFIG_EXTS = {'.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf'}
DOC_EXTS = {'.md', '.rst', '.txt'}
TEMPLATE_EXTS = {'.jinja', '.jinja2', '.j2', '.hbs', '.ejs', '.pug'}
SHELL_EXTS = {'.sh', '.bash', '.zsh', '.ps1', '.bat', '.cmd'}
DATA_EXTS = {'.sql', '.graphql', '.gql', '.proto'}
OTHER_CODE_EXTS = {'.rb', '.go', '.rs', '.java', '.kt', '.swift', '.c', '.cpp', '.h', '.hpp', '.cs'}

ALL_INDEXABLE_EXTS = (
    PYTHON_EXTS | JS_TS_EXTS | PHP_EXTS | WEB_EXTS | CONFIG_EXTS | DOC_EXTS |
    TEMPLATE_EXTS | SHELL_EXTS | DATA_EXTS | OTHER_CODE_EXTS
)

# Also index specific filenames without extensions
INDEXABLE_FILENAMES = {
    'Dockerfile', 'docker-compose.yml', 'docker-compose.yaml',
    'Makefile', 'Rakefile', 'Gemfile', 'Procfile',
    '.gitignore', '.dockerignore', '.eslintrc', '.prettierrc',
    'requirements.txt', 'Pipfile', 'pyproject.toml', 'setup.cfg',
    'package.json', 'tsconfig.json', 'webpack.config.js',
    'CLAUDE.md',
}

# Files to always skip regardless of extension
SKIP_FILENAMES = {
    'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml',
    'Pipfile.lock', 'poetry.lock', 'composer.lock', 'Gemfile.lock',
    'cargo.lock', 'flake.lock',
    '.DS_Store', 'Thumbs.db', 'desktop.ini',
    # WordPress: secrets + boilerplate
    'wp-config.php',          # contains DB creds + 8 secret keys — never index
    'wp-config-sample.php',   # WP boilerplate
    'wp-cli.yml',             # tiny config
    'readme.html',            # WP root readme (boilerplate)
    'wp-activate.php', 'wp-blog-header.php', 'wp-comments-post.php',
    'wp-cron.php', 'wp-links-opml.php', 'wp-load.php', 'wp-login.php',
    'wp-mail.php', 'wp-settings.php', 'wp-signup.php',
    'wp-trackback.php', 'xmlrpc.php',
}

# Filename patterns to skip (compiled once)
SKIP_FILENAME_PATTERNS = re.compile(
    r'(?:'
    r'\.min\.(?:js|css)$'       # Minified files
    r'|\.bundle\.(?:js|css)$'   # Bundle files
    r'|\.chunk\.(?:js|css)$'    # Chunk files
    r'|\.map$'                  # Source maps
    r'|\.d\.ts$'                # TypeScript declaration files
    r'|\.pyc$'                  # Compiled Python
    r'|\.pyo$'                  # Optimized Python
    r'|-lock\.'                 # Any lock file pattern
    r')',
    re.IGNORECASE,
)

# Markers in first few lines that indicate auto-generated files
AUTOGEN_MARKERS = [
    'auto-generated', 'autogenerated', 'auto generated',
    'do not edit', 'do not modify', 'don\'t edit',
    'generated by', 'generated from', 'generated with',
    'this file is generated', 'machine generated',
    'code generated', 'automatically generated',
]

# Max file size to index (skip huge generated/minified files)
MAX_FILE_SIZE = 500_000  # 500KB
# Min file size to index (skip empty/near-empty files)
MIN_FILE_SIZE = 10  # bytes
# Max average line length before we suspect minified content
MAX_AVG_LINE_LENGTH = 500


class CodeParser:
    def __init__(self, project_root: str):
        self.project_root = Path(project_root)

    def discover_files(self) -> List[Path]:
        """Find all indexable files, skipping vendor/build/venv dirs."""
        found = []
        for root, dirs, files in os.walk(self.project_root):
            root_path = Path(root)
            is_root = root_path == self.project_root
            current_dir_name = root_path.name
            # Only apply parent-scoped skips when we're NOT at the project root —
            # otherwise pointing the indexer directly at a `wp-content/` dir would
            # silently prune top-level children like `cache`, `languages`, etc.
            # Parent-scoped skips resolve by directory NAME, falling back to
            # shape detection so an extracted wp-content tree (e.g. a .wpress
            # pull, where the folder is named after the pull timestamp) still
            # gets the 'wp-content' rules applied.
            scoped_key = current_dir_name
            if (not is_root
                    and scoped_key not in PARENT_SCOPED_SKIP_DIRS
                    and looks_like_wp_content(dirs, files)):
                scoped_key = 'wp-content'
                # Log it: this prunes whole subtrees, and a silent prune is
                # exactly the failure mode this detection was added to fix.
                logger.info(
                    'wp-content shape detected at %s - applying wp-content '
                    'skip rules (%s)',
                    root_path,
                    ', '.join(sorted(PARENT_SCOPED_SKIP_DIRS['wp-content']
                                     .intersection(dirs))) or 'none pruned',
                )
            scoped_skips = (
                PARENT_SCOPED_SKIP_DIRS.get(scoped_key, set())
                if not is_root else set()
            )
            dirs[:] = [d for d in dirs
                       if d not in SKIP_DIRS
                       and d not in scoped_skips
                       and not d.endswith(SKIP_DIR_SUFFIXES)
                       and not (is_root and d in ROOT_ONLY_SKIP_DIRS)]
            for f in files:
                fpath = Path(root) / f
                ext = fpath.suffix.lower()
                if ext in ALL_INDEXABLE_EXTS or f in INDEXABLE_FILENAMES:
                    try:
                        size = fpath.stat().st_size
                        if MIN_FILE_SIZE <= size <= MAX_FILE_SIZE:
                            found.append(fpath)
                    except OSError:
                        pass
        return found

    @staticmethod
    def compute_file_hash(file_path: Path) -> str:
        """Fast content hash using MD5 (not for security, just change detection)."""
        h = hashlib.md5()
        try:
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
        except OSError:
            return ''
        return h.hexdigest()

    @staticmethod
    def should_skip_file(file_path: Path) -> Optional[str]:
        """Check if a file should be skipped. Returns skip reason or None."""
        name = file_path.name

        # Known skip filenames
        if name in SKIP_FILENAMES:
            return 'lock_or_system_file'

        # Filename patterns (minified, bundles, maps, declarations)
        if SKIP_FILENAME_PATTERNS.search(name):
            return 'generated_or_minified'

        # Read first few KB to check content
        try:
            with open(file_path, 'rb') as f:
                head = f.read(4096)
        except OSError:
            return 'unreadable'

        # Binary file detection (null bytes in first chunk)
        if b'\x00' in head:
            return 'binary_file'

        # Try to decode as text
        try:
            text_head = head.decode('utf-8', errors='strict')
        except UnicodeDecodeError:
            try:
                text_head = head.decode('latin-1')
            except Exception:
                return 'encoding_error'

        # Auto-generated file detection (check first 5 lines)
        first_lines = text_head.lower()[:1000]
        for marker in AUTOGEN_MARKERS:
            if marker in first_lines:
                return 'auto_generated'

        # Minified content detection (very long lines = likely minified)
        lines = text_head.split('\n')
        non_empty = [l for l in lines if l.strip()]
        if non_empty:
            avg_len = sum(len(l) for l in non_empty) / len(non_empty)
            if avg_len > MAX_AVG_LINE_LENGTH:
                return 'likely_minified'

        return None  # File is worth indexing

    def parse_file(self, file_path: Path) -> List[Dict]:
        """Parse a file into chunks based on its type."""
        ext = file_path.suffix.lower()
        if ext in PYTHON_EXTS:
            return self._parse_python(file_path)
        elif ext in JS_TS_EXTS:
            return self._parse_js_ts(file_path)
        elif ext in PHP_EXTS:
            return self._parse_php(file_path)
        else:
            return self._parse_generic(file_path)

    # ── Python parsing (AST-based, rich extraction) ──────────────

    def _parse_python(self, file_path: Path) -> List[Dict]:
        try:
            source = file_path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            return []

        try:
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError:
            # Fall back to generic parsing if syntax is invalid
            return self._parse_generic(file_path)

        rel_path = str(file_path.relative_to(self.project_root)).replace('\\', '/')
        mtime = file_path.stat().st_mtime
        lines = source.splitlines()
        chunks = []

        # File summary
        module_doc = ast.get_docstring(tree) or ''
        imports = [
            self._get_source(node, lines)
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        summary_text = f"File: {rel_path}\n"
        if module_doc:
            summary_text += f"Docstring: {module_doc}\n"
        if imports:
            summary_text += f"Imports: {'; '.join(imports[:20])}\n"

        chunks.append({
            'file_path': rel_path,
            'symbol_name': rel_path,
            'symbol_type': 'file_summary',
            'line_start': 1,
            'line_end': min(len(lines), 30),
            'source_code': '\n'.join(lines[:30]),
            'docstring': module_doc,
            'decorators': [],
            'parent_class': None,
            'route_path': None,
            'search_text': summary_text,
            'file_mtime': mtime,
        })

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chunks.append(self._parse_function(node, lines, rel_path, mtime))
            elif isinstance(node, ast.ClassDef):
                chunks.extend(self._parse_class(node, lines, rel_path, mtime))
            elif isinstance(node, ast.Assign):
                chunk = self._parse_constant(node, lines, rel_path, mtime)
                if chunk:
                    chunks.append(chunk)

        return chunks

    def _parse_function(self, node, lines, file_path, mtime, parent_class=None):
        decorators = [self._get_decorator_text(d) for d in node.decorator_list]
        route_path = self._extract_route(node.decorator_list)
        docstring = ast.get_docstring(node) or ''
        source = self._get_source_range(lines, node.lineno, node.end_lineno)

        symbol_type = 'method' if parent_class else 'function'
        if route_path:
            symbol_type = 'route'

        name = node.name
        search_text = f"{symbol_type}: {name}"
        if parent_class:
            search_text += f" (in class {parent_class})"
        if route_path:
            search_text += f" route={route_path}"
        if docstring:
            search_text += f"\n{docstring}"
        search_text += f"\n{source[:500]}"

        return {
            'file_path': file_path,
            'symbol_name': name,
            'symbol_type': symbol_type,
            'line_start': node.lineno,
            'line_end': node.end_lineno,
            'source_code': source,
            'docstring': docstring,
            'decorators': decorators,
            'parent_class': parent_class,
            'route_path': route_path,
            'search_text': search_text,
            'file_mtime': mtime,
        }

    def _parse_class(self, node, lines, file_path, mtime):
        chunks = []
        docstring = ast.get_docstring(node) or ''
        source_preview = self._get_source_range(
            lines, node.lineno, min(node.lineno + 20, node.end_lineno)
        )

        search_text = f"class: {node.name}"
        if docstring:
            search_text += f"\n{docstring}"
        search_text += f"\n{source_preview[:500]}"

        chunks.append({
            'file_path': file_path,
            'symbol_name': node.name,
            'symbol_type': 'class',
            'line_start': node.lineno,
            'line_end': node.end_lineno,
            'source_code': source_preview,
            'docstring': docstring,
            'decorators': [self._get_decorator_text(d) for d in node.decorator_list],
            'parent_class': None,
            'route_path': None,
            'search_text': search_text,
            'file_mtime': mtime,
        })

        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chunks.append(
                    self._parse_function(child, lines, file_path, mtime, parent_class=node.name)
                )

        return chunks

    def _parse_constant(self, node, lines, file_path, mtime):
        if not node.targets:
            return None
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            return None
        name = target.id
        if not name.isupper() or name.startswith('_'):
            return None

        source = self._get_source_range(lines, node.lineno, node.end_lineno)
        return {
            'file_path': file_path,
            'symbol_name': name,
            'symbol_type': 'constant',
            'line_start': node.lineno,
            'line_end': node.end_lineno,
            'source_code': source,
            'docstring': None,
            'decorators': [],
            'parent_class': None,
            'route_path': None,
            'search_text': f"constant: {name}\n{source[:200]}",
            'file_mtime': mtime,
        }

    def _extract_route(self, decorators):
        for d in decorators:
            text = self._get_decorator_text(d)
            if 'route(' in text or '.get(' in text or '.post(' in text:
                if isinstance(d, ast.Call) and d.args:
                    if isinstance(d.args[0], ast.Constant):
                        return d.args[0].value
        return None

    def _get_decorator_text(self, node):
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return f"{self._get_decorator_text(node.value)}.{node.attr}"
        elif isinstance(node, ast.Call):
            func_text = self._get_decorator_text(node.func)
            args = []
            for a in node.args[:3]:
                if isinstance(a, ast.Constant):
                    args.append(repr(a.value))
            return f"{func_text}({', '.join(args)})"
        return '?'

    def _get_source(self, node, lines):
        if hasattr(node, 'end_lineno') and node.end_lineno:
            return self._get_source_range(lines, node.lineno, node.end_lineno)
        return lines[node.lineno - 1] if node.lineno <= len(lines) else ''

    def _get_source_range(self, lines, start, end):
        if start and end:
            return '\n'.join(lines[start - 1:end])
        return ''

    # ── JS/TS parsing (regex-based, extracts functions/classes/exports) ──

    def _parse_js_ts(self, file_path: Path) -> List[Dict]:
        try:
            source = file_path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            return []

        rel_path = str(file_path.relative_to(self.project_root)).replace('\\', '/')
        mtime = file_path.stat().st_mtime
        lines = source.splitlines()
        chunks = []

        # File summary
        chunks.append(self._make_file_summary(rel_path, lines, mtime))

        # Extract functions: function name(...), const name = (...) =>, async function name(...)
        func_patterns = [
            # Standard function declarations
            re.compile(r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)', re.MULTILINE),
            # Arrow functions assigned to const/let/var
            re.compile(r'^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[^=])\s*=>', re.MULTILINE),
            # Class declarations
            re.compile(r'^(?:export\s+)?class\s+(\w+)', re.MULTILINE),
        ]

        seen = set()
        for pattern in func_patterns:
            for match in pattern.finditer(source):
                name = match.group(1)
                if name in seen:
                    continue
                seen.add(name)
                line_num = source[:match.start()].count('\n') + 1
                # Find the end of this block (rough heuristic)
                end_line = min(line_num + 30, len(lines))
                source_chunk = '\n'.join(lines[line_num - 1:end_line])

                is_class = 'class ' in match.group(0)
                symbol_type = 'class' if is_class else 'function'

                chunks.append({
                    'file_path': rel_path,
                    'symbol_name': name,
                    'symbol_type': symbol_type,
                    'line_start': line_num,
                    'line_end': end_line,
                    'source_code': source_chunk[:1000],
                    'docstring': None,
                    'decorators': [],
                    'parent_class': None,
                    'route_path': None,
                    'search_text': f"{symbol_type}: {name}\n{source_chunk[:500]}",
                    'file_mtime': mtime,
                })

        return chunks

    # ── PHP parsing (regex-based, extracts functions/classes/namespaces) ──

    _PHP_NAMESPACE_RE = re.compile(r'^[ \t]*namespace[ \t]+([\w\\]+)[ \t]*[;{]', re.MULTILINE | re.IGNORECASE)
    _PHP_CLASS_RE = re.compile(r'^[ \t]*(?:abstract[ \t]+|final[ \t]+|readonly[ \t]+)*class[ \t]+(\w+)', re.MULTILINE | re.IGNORECASE)
    _PHP_INTERFACE_RE = re.compile(r'^[ \t]*interface[ \t]+(\w+)', re.MULTILINE | re.IGNORECASE)
    _PHP_TRAIT_RE = re.compile(r'^[ \t]*trait[ \t]+(\w+)', re.MULTILINE | re.IGNORECASE)
    _PHP_ENUM_RE = re.compile(r'^[ \t]*enum[ \t]+(\w+)', re.MULTILINE | re.IGNORECASE)
    # Captures both top-level functions and class methods — PHP regex can't
    # cheaply tell them apart without a real parser, so we just call them "function".
    _PHP_FUNCTION_RE = re.compile(
        r'^[ \t]*(?:(?:public|protected|private|static|final|abstract)[ \t]+)*'
        r'function[ \t]+&?(\w+)[ \t]*\(',
        re.MULTILINE | re.IGNORECASE,
    )
    _PHP_CONST_RE = re.compile(r'^[ \t]*(?:(?:public|protected|private|final)[ \t]+)*const[ \t]+(\w+)', re.MULTILINE | re.IGNORECASE)
    _PHP_DEFINE_RE = re.compile(r'\bdefine[ \t]*\([ \t]*[\'"](\w+)[\'"]', re.IGNORECASE)

    def _parse_php(self, file_path: Path) -> List[Dict]:
        try:
            source = file_path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            return []

        rel_path = str(file_path.relative_to(self.project_root)).replace('\\', '/')
        mtime = file_path.stat().st_mtime
        lines = source.splitlines()
        chunks = [self._make_file_summary(rel_path, lines, mtime)]

        symbol_patterns = [
            ('namespace', self._PHP_NAMESPACE_RE),
            ('class', self._PHP_CLASS_RE),
            ('interface', self._PHP_INTERFACE_RE),
            ('trait', self._PHP_TRAIT_RE),
            ('enum', self._PHP_ENUM_RE),
            ('function', self._PHP_FUNCTION_RE),
            ('constant', self._PHP_CONST_RE),
            ('constant', self._PHP_DEFINE_RE),
        ]

        seen = set()  # (symbol_type, name, line_num) to dedupe across overlapping patterns

        for sym_type, pattern in symbol_patterns:
            for match in pattern.finditer(source):
                name = match.group(1)
                line_num = source[:match.start()].count('\n') + 1
                key = (sym_type, name, line_num)
                if key in seen:
                    continue
                seen.add(key)

                end_line = min(line_num + 30, len(lines))
                source_chunk = '\n'.join(lines[line_num - 1:end_line])

                chunks.append({
                    'file_path': rel_path,
                    'symbol_name': name,
                    'symbol_type': sym_type,
                    'line_start': line_num,
                    'line_end': end_line,
                    'source_code': source_chunk[:1000],
                    'docstring': None,
                    'decorators': [],
                    'parent_class': None,
                    'route_path': None,
                    'search_text': f"{sym_type}: {name}\n{source_chunk[:500]}",
                    'file_mtime': mtime,
                })

        return chunks

    # ── Generic parsing (for config, docs, templates, etc.) ──────

    def _parse_generic(self, file_path: Path) -> List[Dict]:
        try:
            source = file_path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            return []

        rel_path = str(file_path.relative_to(self.project_root)).replace('\\', '/')
        mtime = file_path.stat().st_mtime
        lines = source.splitlines()

        chunks = [self._make_file_summary(rel_path, lines, mtime)]

        # For larger files, split into sections for better search granularity
        if len(lines) > 50:
            chunk_size = 50
            for i in range(0, len(lines), chunk_size):
                section_lines = lines[i:i + chunk_size]
                section_text = '\n'.join(section_lines)
                if section_text.strip():
                    chunks.append({
                        'file_path': rel_path,
                        'symbol_name': f"{rel_path}:{i+1}-{min(i+chunk_size, len(lines))}",
                        'symbol_type': 'section',
                        'line_start': i + 1,
                        'line_end': min(i + chunk_size, len(lines)),
                        'source_code': section_text[:1000],
                        'docstring': None,
                        'decorators': [],
                        'parent_class': None,
                        'route_path': None,
                        'search_text': f"File section: {rel_path} lines {i+1}-{min(i+chunk_size, len(lines))}\n{section_text[:500]}",
                        'file_mtime': mtime,
                    })

        return chunks

    # ── Helpers ──────────────────────────────────────────────────

    def _make_file_summary(self, rel_path: str, lines: list, mtime: float) -> Dict:
        preview = '\n'.join(lines[:30])
        return {
            'file_path': rel_path,
            'symbol_name': rel_path,
            'symbol_type': 'file_summary',
            'line_start': 1,
            'line_end': min(len(lines), 30),
            'source_code': preview,
            'docstring': None,
            'decorators': [],
            'parent_class': None,
            'route_path': None,
            'search_text': f"File: {rel_path}\n{preview[:500]}",
            'file_mtime': mtime,
        }
