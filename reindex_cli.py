#!/usr/bin/env python3
"""CLI wrapper for code-index reindex with real-time progress bar.

Usage:
    python reindex_cli.py [--full] [project_root]

If project_root is not given, uses CWD.
"""

import json
import os
import subprocess
import sys
import shutil
import time

# Force UTF-8 on stdout/stderr so non-ASCII filenames (or any unicode in
# progress detail) don't crash the indexer on Windows consoles that default
# to cp1252. errors='replace' degrades gracefully if a glyph still can't
# be rendered, rather than aborting a long-running reindex.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

# Cap OpenBLAS's per-thread working buffers before numpy is imported. numpy
# reserves ~32 MB per thread at import time, so on a 12-core box `import numpy`
# alone costs ~394 MB vs ~40 MB at one thread. A background reindex can run
# alongside several Claude Code sessions, so that reservation is pure waste:
# embedding is done by ONNX Runtime, not BLAS. Measured throughput is if
# anything better at one thread (30 vs 26 chunks/s). Matches the cap in
# code_index_server.py; export OPENBLAS_NUM_THREADS yourself to override.
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')

# Ensure the code_index package is importable
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SERVER_DIR)

from code_index.indexer import CodeIndexer


# --------------- GUI progress window helpers ---------------

def launch_progress_gui():
    """Spawn the tkinter progress GUI as a subprocess."""
    gui_script = os.path.join(SERVER_DIR, 'progress_gui.py')
    if not os.path.exists(gui_script):
        return None
    try:
        python_exe = sys.executable
        pythonw = python_exe.replace('python.exe', 'pythonw.exe')
        if os.path.exists(pythonw):
            python_exe = pythonw
        proc = subprocess.Popen(
            [python_exe, gui_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        return proc
    except Exception:
        return None


def send_gui(proc, msg: dict):
    """Send a JSON progress message to the GUI subprocess."""
    if proc and proc.stdin and proc.poll() is None:
        try:
            proc.stdin.write((json.dumps(msg) + '\n').encode('utf-8'))
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass


def close_gui(proc, summary: str):
    """Send done signal and let the GUI auto-close."""
    if proc and proc.poll() is None:
        send_gui(proc, {"done": True, "summary": summary})
        try:
            proc.stdin.close()
        except Exception:
            pass


def get_terminal_width():
    return shutil.get_terminal_size((80, 24)).columns


def render_progress_bar(current, total, width=30):
    if total <= 0:
        return f"[{'?' * width}]"
    ratio = min(current / total, 1.0)
    filled = int(width * ratio)
    empty = width - filled
    bar = '#' * filled + '-' * empty
    pct = int(ratio * 100)
    return f"[{bar}] {pct:3d}%"


def print_progress(phase, current, total, detail, state):
    """Print a live-updating progress line."""
    term_width = get_terminal_width()

    if phase == 'init':
        print(f"  Cleared old index", flush=True)
        return

    if phase == 'discover':
        if total == 0:
            print(f"\n  Discovering files...", end='', flush=True)
        else:
            print(f"\r  Found {total} files to process", flush=True)
        return

    if phase == 'parse':
        bar = render_progress_bar(current, total)
        # Extract just the filename from the detail
        short = detail.split('/')[-1] if '/' in detail else detail
        if len(short) > 40:
            short = short[:37] + '...'
        line = f"\r  {bar} | {current}/{total} | {short}"
        # Pad to terminal width to clear previous longer lines
        line = line.ljust(term_width - 1)
        print(line, end='', flush=True)

        # Print header on first call
        if current == 1 and state.get('phase') != 'parse':
            pass  # header already implicit from bar
        state['phase'] = 'parse'

        # Newline when done
        if current == total:
            print(flush=True)
        return

    if phase == 'embed':
        if current == 0:
            print(f"\n  Embedding {total} chunks...", end='', flush=True)
        elif current >= total:
            bar = render_progress_bar(current, total)
            line = f"\r  Embed {bar} | {current}/{total} | done"
            print(line.ljust(term_width - 1), flush=True)
        else:
            bar = render_progress_bar(current, total)
            line = f"\r  Embed {bar} | {current}/{total}"
            print(line.ljust(term_width - 1), end='', flush=True)
        return

    if phase == 'store':
        if current == 0:
            print(f"  Storing in database...", end='', flush=True)
        else:
            print(f"\r  Storing in database... done", flush=True)
        return

    if phase == 'cleanup':
        bar = render_progress_bar(current, total)
        line = f"\r  Cleanup {bar} | {current}/{total}"
        print(line.ljust(term_width - 1), end='', flush=True)
        if current == total:
            print(flush=True)
        return


def main():
    full = '--full' in sys.argv
    quiet = '--quiet' in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    project_root = args[0] if args else os.getcwd()

    indexer = CodeIndexer(project_root)

    # Quiet mode: no GUI, no terminal output — used by background hooks
    if quiet:
        try:
            indexer.force_reindex(full=full)
        finally:
            # close() checkpoints+truncates the WAL — must run even on error
            indexer.close()
        return

    mode = 'Full' if full else 'Incremental'
    print(f"\n  Code Index - {mode} Reindex")
    print(f"  Project: {project_root}")
    print(f"  {'=' * 50}")

    state = {}
    start = time.time()

    # Launch the GUI progress window
    gui_proc = launch_progress_gui()

    def callback(phase, current, total, detail):
        print_progress(phase, current, total, detail, state)
        send_gui(gui_proc, {
            "phase": phase, "current": current,
            "total": total, "detail": detail,
        })

    indexer.force_reindex(full=full, progress_callback=callback)

    elapsed = time.time() - start
    status = indexer.get_status()

    print(f"\n  {'=' * 50}")
    print(f"  Complete in {elapsed:.1f}s")
    print(f"  Files indexed: {status.get('total_files', '?')}")
    print(f"  Files skipped: {status.get('skipped_files', '?')} (non-indexable)")
    print(f"  Total chunks:  {status.get('total_chunks', '?')}")
    print(f"  Symbol types:")
    for stype, count in status.get('by_type', {}).items():
        print(f"    {stype}: {count}")
    print()

    # Close the GUI with a summary
    file_count = status.get('total_files', '?')
    chunk_count = status.get('total_chunks', '?')
    close_gui(gui_proc, f"Indexed {file_count} files, {chunk_count} chunks in {elapsed:.1f}s")

    indexer.close()


if __name__ == '__main__':
    main()
