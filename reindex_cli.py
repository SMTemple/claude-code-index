#!/usr/bin/env python3
"""CLI wrapper for code-index reindex with real-time progress bar.

Usage:
    python reindex_cli.py [--full] [project_root]

If project_root is not given, uses CWD.
"""

import os
import sys
import shutil
import time

# Ensure the code_index package is importable
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SERVER_DIR)

from code_index.indexer import CodeIndexer


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
        else:
            print(f"\r  Embedding {total} chunks... done", flush=True)
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
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    project_root = args[0] if args else os.getcwd()

    mode = 'Full' if full else 'Incremental'
    print(f"\n  Code Index - {mode} Reindex")
    print(f"  Project: {project_root}")
    print(f"  {'=' * 50}")

    indexer = CodeIndexer(project_root)
    state = {}
    start = time.time()

    def callback(phase, current, total, detail):
        print_progress(phase, current, total, detail, state)

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

    indexer.close()


if __name__ == '__main__':
    main()
