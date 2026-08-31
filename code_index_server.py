"""MCP server for local code indexing — semantic search across any codebase.

This is a global tool. To determine which project to index it checks, in order:

  1. Environment variable ``CODE_INDEX_PROJECT_ROOT`` (recommended for Cursor —
     set per-workspace via ``<workspace>/.cursor/mcp.json`` env block with
     ``${workspaceFolder}``).
  2. The process CWD (Claude Code sets this to the project directory; Cursor
     does NOT — it launches MCP processes from the user home dir, so relying
     on CWD under Cursor produces a useless home-dir-rooted index).

Each project gets its own .code_index/ directory at PROJECT_ROOT.

If PROJECT_ROOT resolves to the user's home directory (a strong signal that
neither mechanism worked), the server refuses to index and returns a clear
error from each tool call instead of silently building a multi-GB index of
everything under HOME.
"""

import os
import sys
import threading
import traceback

# ── OpenBLAS thread-buffer cap ───────────────────────────────────────────────
# numpy's bundled OpenBLAS reserves per-thread working buffers at import time,
# roughly 32 MB per thread. Measured on this 12-core box 2026-08-28:
#     import numpy, default            -> +394 MB private
#     import numpy, OPENBLAS_NUM_THREADS=1 -> + 40 MB private
# Every Claude Code session spawns its OWN code-index server, so that 394 MB was
# being paid N times over (5 concurrent sessions = ~2 GB of a 32 GB machine).
# This server does no heavy linear algebra: embeddings are computed by ONNX
# Runtime (fastembed), and numpy here only handles 384-dim vectors, so one BLAS
# thread costs nothing measurable while saving ~354 MB per server.
# MUST be set before numpy is first imported (pulled in by code_index.indexer).
# Export OPENBLAS_NUM_THREADS yourself to override.
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')

from mcp.server.fastmcp import FastMCP

# The server's own package directory (where this file lives)
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SERVER_DIR)


def _resolve_project_root() -> tuple[str, str | None]:
    """Return (project_root, error_message). error_message is non-None when
    the resolved root is unsafe to index (e.g. the user home dir)."""
    env_root = os.environ.get("CODE_INDEX_PROJECT_ROOT")
    if env_root:
        root = os.path.abspath(env_root)
    else:
        root = os.path.abspath(os.getcwd())

    home = os.path.abspath(os.path.expanduser("~"))
    if os.path.normcase(root) == os.path.normcase(home):
        msg = (
            f"Refusing to index user home directory ({home}). "
            "Set CODE_INDEX_PROJECT_ROOT to the workspace path, or launch the "
            "MCP server with cwd set to the workspace. "
            "In Cursor, add a per-workspace <workspace>/.cursor/mcp.json with "
            'env: {"CODE_INDEX_PROJECT_ROOT": "${workspaceFolder}"}.'
        )
        return root, msg
    return root, None


PROJECT_ROOT, _ROOT_ERROR = _resolve_project_root()

from code_index.indexer import CodeIndexer
from code_index.embeddings import prewarm_model, EmbeddingError

mcp = FastMCP("code-index")

if _ROOT_ERROR is None:
    indexer = CodeIndexer(PROJECT_ROOT)
    # Model load is DEFERRED to first use. Measured 2026-08-28 on this machine:
    # prewarming costs +520 MB private per server (47 MB -> 567 MB) to save ~1.4s
    # on the first semantic search. Every Claude Code session spawns its own
    # code-index server, so N concurrent sessions paid N x 520 MB for a model most
    # of them never queried (5 sessions = 2.6 GB of a 32 GB box). The "~30-60s"
    # figure in the old comment was the PyTorch/sentence-transformers path; the
    # default fastembed/ONNX backend loads in ~1.4s, cheap enough to pay lazily.
    # _get_model() loads under a lock on first embed call, so the first search
    # simply blocks ~1.4s and succeeds; subsequent calls are ~0.02s.
    # Set CODE_INDEX_PREWARM=1 to restore eager loading (e.g. ahead of a big reindex).
    if os.environ.get('CODE_INDEX_PREWARM', '').strip().lower() in ('1', 'true', 'yes'):
        threading.Thread(target=prewarm_model, daemon=True, name='model-prewarm').start()
else:
    indexer = None  # All tool calls will short-circuit with _ROOT_ERROR.


def _root_guard() -> str | None:
    """Return the configured error message if the project root is unsafe."""
    return _ROOT_ERROR


@mcp.tool()
def search_code(query: str, limit: int = 10) -> str:
    """Semantic search across the codebase. Find code by meaning, not just text.
    Examples: "Chrome WebDriver cleanup", "database connection handling", "Flask route for login"
    """
    err = _root_guard()
    if err:
        return err
    try:
        results = indexer.search_code(query, limit)
        if not results:
            return "No results found. Try different search terms or run `reindex` first."

        output = []
        for r in results:
            score = r.get('score', 0)
            output.append(
                f"**{r['symbol_type']}** `{r['symbol_name']}` "
                f"in `{r['file_path']}` (L{r['line_start']}-{r['line_end']}) "
                f"[relevance={score}]\n"
                f"```python\n{r['source_code'][:500]}\n```\n"
            )
        return '\n---\n'.join(output)
    except EmbeddingError as e:
        return (
            f"Model not ready: {e}\n\n"
            "The embedding model is still loading. Fall back to Grep/Glob for now, "
            "or wait a moment and retry."
        )
    except Exception as e:
        return f"Search failed: {e}\nTry running `reindex` to rebuild the index."


@mcp.tool()
def search_symbol(name: str, symbol_type: str = None) -> str:
    """Search for a symbol by name. Optionally filter by type:
    function, method, class, interface, trait, enum, namespace, route, constant, file_summary.
    Examples: search_symbol("start_analysis"), search_symbol("cleanup", "method"), search_symbol("parsePage", "function")
    """
    err = _root_guard()
    if err:
        return err
    try:
        results = indexer.search_symbol(name, symbol_type)
        if not results:
            return f"No symbols matching '{name}' found."

        output = []
        for r in results:
            line = (
                f"**{r['symbol_type']}** `{r['symbol_name']}` "
                f"in `{r['file_path']}` (L{r['line_start']}-{r['line_end']})"
            )
            if r.get('route_path'):
                line += f" route={r['route_path']}"
            if r.get('parent_class'):
                line += f" class={r['parent_class']}"
            output.append(line)
        return '\n'.join(output)
    except Exception as e:
        return f"Symbol search failed: {e}\nTry running `reindex` to rebuild the index."


@mcp.tool()
def get_file_overview(file_path: str) -> str:
    """List all symbols (functions, classes, methods, routes) in a file.
    Pass the relative path, e.g. 'mcp_server.py' or 'code_index/parser.py'
    """
    err = _root_guard()
    if err:
        return err
    try:
        results = indexer.get_file_overview(file_path)
        if not results:
            return f"No symbols found for '{file_path}'. Check the file path is relative to the project root."

        output = [f"**File: {file_path}**\n"]
        for r in results:
            prefix = '  ' if r.get('parent_class') else ''
            line = f"{prefix}- **{r['symbol_type']}** `{r['symbol_name']}` (L{r['line_start']}-{r['line_end']})"
            if r.get('route_path'):
                line += f" route={r['route_path']}"
            if r.get('docstring'):
                doc_preview = r['docstring'][:80].replace('\n', ' ')
                line += f" — {doc_preview}"
            output.append(line)
        return '\n'.join(output)
    except Exception as e:
        return f"File overview failed: {e}\nTry running `reindex` to rebuild the index."


@mcp.tool()
def index_status() -> str:
    """Check if the code index exists and show statistics."""
    err = _root_guard()
    if err:
        return err
    try:
        status = indexer.get_status()
        if not status['indexed']:
            return f"Project: {PROJECT_ROOT}\n{status['message']}"

        lines = [
            f"Project: {status.get('project_root', PROJECT_ROOT)}",
            f"Index is active: {status['total_chunks']} chunks across {status['total_files']} files",
            f"Tracked: {status.get('tracked_files', '?')} files ({status.get('skipped_files', '?')} skipped as non-indexable)",
            f"Build time: {status.get('build_time_seconds', '?')}s",
            "Symbol types:"
        ]
        for stype, count in status.get('by_type', {}).items():
            lines.append(f"  - {stype}: {count}")
        return '\n'.join(lines)
    except Exception as e:
        return f"Status check failed: {e}"


@mcp.tool()
def get_project_summary() -> str:
    """Return the auto-generated project summary (languages, structure, purpose, frameworks).
    Generated/updated automatically on each reindex.
    """
    err = _root_guard()
    if err:
        return err
    summary_path = indexer.index_dir / 'PROJECT_SUMMARY.md'
    try:
        if summary_path.exists():
            return summary_path.read_text(encoding='utf-8')
        return "No project summary yet. Run `reindex` to generate one."
    except Exception as e:
        return f"Could not read project summary: {e}"


@mcp.tool()
def reindex(full: bool = False) -> str:
    """Rebuild the code index. By default does incremental update (only changed files).
    Set full=True to rebuild from scratch.
    """
    err = _root_guard()
    if err:
        return err
    try:
        progress_log = []
        phase_summaries = {}

        def progress_callback(phase: str, current: int, total: int, detail: str):
            progress_log.append(detail)
            phase_summaries[phase] = (current, total)

        indexer.force_reindex(full=full, progress_callback=progress_callback)

        status = indexer.get_status()
        mode = 'Full' if full else 'Incremental'

        # Nothing changed — index was already up to date
        if not phase_summaries and not full:
            return '\n'.join([
                "Incremental reindex complete.\n",
                "No changes detected — index is already up to date.",
                "",
                f"Files indexed: {status.get('total_files', '?')}",
                f"Files skipped: {status.get('skipped_files', '?')} (non-indexable)",
                f"Total chunks:  {status.get('total_chunks', '?')}",
            ])

        lines = [f"{mode} reindex complete.\n"]

        # Show scope (full rebuild only — incremental doesn't emit 'discover')
        discover = phase_summaries.get('discover')
        if discover:
            skipped = status.get('skipped_files', '?')
            lines.append(f"Scope: {discover[1]} files discovered ({skipped} skipped as non-indexable)")

        parse = phase_summaries.get('parse')
        if parse:
            lines.append(f"Parsed: {parse[0]}/{parse[1]} files processed")

        embed = phase_summaries.get('embed')
        if embed:
            lines.append(f"Embedded: {embed[1]} chunks")

        cleanup = phase_summaries.get('cleanup')
        if cleanup:
            lines.append(f"Cleaned up: {cleanup[0]} stale entries removed")

        lines.append("")
        lines.append(f"Files indexed: {status.get('total_files', '?')}")
        lines.append(f"Files skipped: {status.get('skipped_files', '?')} (non-indexable)")
        lines.append(f"Total chunks:  {status.get('total_chunks', '?')}")

        if full:
            lines.append(f"Build time:    {status.get('build_time_seconds', '?')}s")
        else:
            try:
                inc_time = indexer.db.get_meta('incremental_time_seconds')
                lines.append(f"Build time:    {inc_time or '?'}s")
            except Exception:
                lines.append("Build time:    ?s")

        lines.append("")
        lines.append("Symbol types:")
        for stype, count in status.get('by_type', {}).items():
            lines.append(f"  {stype}: {count}")

        return '\n'.join(lines)
    except Exception as e:
        return f"Reindex failed: {e}\n\nStack trace:\n{traceback.format_exc()}"


if __name__ == "__main__":
    mcp.run()
