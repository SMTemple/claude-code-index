"""MCP server for local code indexing — semantic search across any Python codebase.

This is a global tool. It uses the CWD (set by Claude Code) to determine which
project to index. Each project gets its own .code_index/ directory.
"""

import os
import sys
import threading
import traceback

from mcp.server.fastmcp import FastMCP

# The server's own package directory (where this file lives)
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SERVER_DIR)

# Project root = CWD, which Claude Code sets to the project directory
PROJECT_ROOT = os.getcwd()

from code_index.indexer import CodeIndexer
from code_index.embeddings import prewarm_model, EmbeddingError

mcp = FastMCP("code-index")
indexer = CodeIndexer(PROJECT_ROOT)

# Pre-load the sentence-transformers model in the background so the first
# reindex doesn't pay the model load cost (~30-60s) on top of embedding time.
threading.Thread(target=prewarm_model, daemon=True, name='model-prewarm').start()


@mcp.tool()
def search_code(query: str, limit: int = 10) -> str:
    """Semantic search across the codebase. Find code by meaning, not just text.
    Examples: "Chrome WebDriver cleanup", "database connection handling", "Flask route for login"
    """
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
    function, method, class, route, constant, file_summary.
    Examples: search_symbol("start_analysis"), search_symbol("cleanup", "method")
    """
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
def reindex(full: bool = False) -> str:
    """Rebuild the code index. By default does incremental update (only changed files).
    Set full=True to rebuild from scratch.
    """
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
