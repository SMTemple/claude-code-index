---
name: index-project
description: Reindex the current project's code index with a visible progress bar. Use when the user wants to rebuild or refresh the semantic code search index.
user-invocable: true
disable-model-invocation: true
argument-hint: [--full]
allowed-tools: Bash, Read, Edit, TaskOutput
---

# Index Project

Reindex the current project using the code-index CLI with a real-time progress bar.

## Steps

1. **Ensure `.code_index` is in `.gitignore`** before indexing:
   - Read the project's `.gitignore` file (if it exists)
   - If `.code_index` or `/.code_index` is NOT already listed, append `/.code_index` to the `.gitignore`
   - If no `.gitignore` exists, create one with `/.code_index`

2. **ALWAYS run the reindex as a background task** using `run_in_background: true` on the Bash tool call:

```
python "C:/Users/stemp/.claude/tools/code-indexer/reindex_cli.py" $ARGUMENTS
```

- If the user passes `--full` or `full` as an argument, include `--full` in the command
- Otherwise, run without `--full` for an incremental reindex (only changed files)
- **This command MUST use `run_in_background: true`** — never run it in the foreground

3. After launching the background task, immediately inform the user that reindexing is running in the background.

4. Use `TaskOutput` with `block: true` to wait for the result, then report the final summary (file counts, chunk counts, symbol types).

## Usage

- `/index-project` — Incremental reindex (fast, only changed files)
- `/index-project --full` — Full reindex from scratch
