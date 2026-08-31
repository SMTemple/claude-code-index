---
name: index-project
description: Reindex the current project's code index with a visible progress bar. Use when the user wants to rebuild or refresh the semantic code search index.
user-invocable: true
disable-model-invocation: true
argument-hint: [--full]
allowed-tools: Bash, Read, Edit
---

# Index Project

Reindex the current project using the code-index CLI with a real-time progress bar.

## Steps

1. **Ensure `.code_index` is in `.gitignore`** before indexing:
   - Read the project's `.gitignore` file (if it exists)
   - If `.code_index` or `/.code_index` is NOT already listed, append `/.code_index` to the `.gitignore`
   - If no `.gitignore` exists, create one with `/.code_index`

2. Run the reindex CLI script via Bash **with `run_in_background: true`** so the user can keep working while the index rebuilds:

```
python "$HOME/.claude/tools/code-indexer/reindex_cli.py" $ARGUMENTS
```

- If the user passes `--full` or `full` as an argument, include `--full` in the command
- Otherwise, run without `--full` for an incremental reindex (only changed files)
- The script launches a tkinter progress window that shows live progress in its own GUI window — the user gets visual feedback even though the Bash call returns immediately
- After kicking it off, report the BashShell ID and tell the user the GUI window will show progress; offer to poll with BashOutput at a natural pause if they want a status check

3. The script shows a live progress bar (in the GUI window AND streamed to the background shell's stdout) with:
   - File discovery count
   - Per-file parsing progress with a visual bar
   - Embedding progress
   - Final summary with file counts, chunk counts, and symbol types

## Why background

Full reindexes on large WordPress / monorepo projects can take 1–10 minutes. Running foreground blocks the conversation; background lets work continue. The MCP server's existing index keeps serving searches against the previous data until the rebuild completes — there is no "down time" for code-index queries.

## Usage

- `/index-project` — Incremental reindex (fast, only changed files)
- `/index-project --full` — Full reindex from scratch
