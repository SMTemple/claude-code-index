# Code Index for Claude Code

A local MCP (Model Context Protocol) server that gives Claude Code semantic search across your entire codebase. It builds a per-project vector database so Claude can search your code by **meaning**, not just text matching.

**What it does:** When you open a project in Claude Code, the code-index server automatically parses all source files, generates embeddings, and stores them in a local SQLite vector database (`.code_index/` in your project root). Claude then uses this to find relevant code faster and more accurately.

**Supported languages:** Python (full AST parsing), JavaScript/TypeScript (regex-based), plus generic support for HTML, CSS, JSON, YAML, Markdown, SQL, PHP, Ruby, Go, Rust, Java, C/C++, and more.

## Quick Start

### 1. Install Python Dependencies

```bash
pip install "mcp[cli]" sentence-transformers sqlite-vec
```

Or with a dedicated venv:

```bash
python -m venv ~/.claude-code-index-venv

# Windows (Git Bash):
source ~/.claude-code-index-venv/Scripts/activate
# Mac/Linux:
source ~/.claude-code-index-venv/bin/activate

pip install "mcp[cli]" sentence-transformers sqlite-vec
deactivate
```

Verify:

```bash
python -c "from mcp.server.fastmcp import FastMCP; print('mcp OK')"
python -c "from sentence_transformers import SentenceTransformer; print('sentence-transformers OK')"
python -c "import sqlite_vec; print('sqlite-vec OK')"
```

> **Note:** The first time `sentence-transformers` loads, it downloads the `all-MiniLM-L6-v2` model (~80MB). This is a one-time download.

### 2. Install the Server Files

Clone this repo and copy the files into your Claude Code tools directory:

```bash
git clone https://github.com/SMTemple/claude-code-index.git
mkdir -p ~/.claude/tools/code-indexer/code_index
cp claude-code-index/code_index_server.py ~/.claude/tools/code-indexer/
cp claude-code-index/reindex_cli.py ~/.claude/tools/code-indexer/
cp claude-code-index/code_index/*.py ~/.claude/tools/code-indexer/code_index/
```

### 3. Register the MCP Server

```bash
# If Python is directly in your PATH:
claude mcp add --scope user code-index -- python ~/.claude/tools/code-indexer/code_index_server.py

# If you used a dedicated venv:
# Windows (Git Bash):
claude mcp add --scope user code-index -- ~/.claude-code-index-venv/Scripts/python ~/.claude/tools/code-indexer/code_index_server.py
# Mac/Linux:
claude mcp add --scope user code-index -- ~/.claude-code-index-venv/bin/python ~/.claude/tools/code-indexer/code_index_server.py
```

Verify: `claude mcp get code-index`

### 4. Update `~/.claude/settings.json`

Merge these entries into your existing settings:

```json
{
  "permissions": {
    "allow": [
      "mcp__code-index__*"
    ]
  },
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "touch /tmp/.claude-reindex-needed",
            "timeout": 5,
            "async": true
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "if [ -f /tmp/.claude-reindex-needed ]; then rm /tmp/.claude-reindex-needed && echo '[code-index] Files were modified since last reindex. Run an incremental reindex using mcp__code-index__reindex (full: false) before doing any code searches.'; fi",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

### 5. Add Instructions to `~/.claude/CLAUDE.md`

Add this to your global CLAUDE.md so Claude knows to use the code index:

```markdown
## Code Index Usage (MANDATORY — ALL PROJECTS)

**HARD REQUIREMENT: You MUST call `mcp__code-index` tools BEFORE using Grep or Glob for ANY code search.** This is non-negotiable. If you catch yourself reaching for Grep/Glob first, STOP and use the code index instead.

**ENFORCED WORKFLOW — follow this order every time:**
1. **FIRST** — Check `index_status` if unsure whether the index is current
2. **THEN** — Use `search_code` or `search_symbol` to find what you need
3. **ONLY AFTER** — If the code index results are insufficient or too broad, fall back to Grep/Glob to narrow down

**Never skip step 2.** Even if you think Grep would be faster. Even if you "just need one quick search." The code index exists to be used FIRST.

**When to use which code index tool:**
- `search_code` — finding functions, patterns, implementations, how something works
- `search_symbol` — finding class/function/variable definitions
- `get_file_overview` — understanding what a file contains before reading it
- `index_status` — checking if the index is up to date

**The ONLY exceptions where Grep/Glob can be used directly (without code index first):**
- Editing a specific file you already have open and understand
- Searching for an exact literal string you need to replace across files (e.g., `replace_all` prep)
- Reading a file path the user gave you directly
- The code index is confirmed down/broken/empty after checking `index_status`

**Reindexing**: ALL reindexing — both incremental (`full=false`) and full (`full=true`) — MUST ALWAYS run in a **background Task agent** or via Bash with `run_in_background: true`. NEVER run reindexing in the foreground. Continue working on other tasks in parallel while it runs. If it doesn't complete within a few minutes, flag it to the user and fall back to direct tools.

**Background agents**: Always run Explore agents and other research/search Task agents with `run_in_background: true`. Continue working on other tasks in parallel while waiting for results. Only use foreground agents when their output is a hard prerequisite with no other work to do.
```

### 6. Install the `/index-project` Skill (Optional)

This gives you a `/index-project` slash command inside Claude Code:

```bash
mkdir -p ~/.claude/skills/index-project
cp skills/index-project/SKILL.md ~/.claude/skills/index-project/SKILL.md
```

> **Important:** Edit the `SKILL.md` file and update the Python path in the command to match your system (the default uses `C:/Users/stemp/.claude/tools/code-indexer/reindex_cli.py`).

Usage:
- `/index-project` — Incremental reindex (fast, only changed files)
- `/index-project --full` — Full reindex from scratch

### 7. Add `.code_index` to `.gitignore`

Each project gets a `.code_index/` directory. Add it to your global gitignore:

```bash
echo ".code_index/" >> ~/.gitignore_global
git config --global core.excludesfile ~/.gitignore_global
```

## How It Works

### Available Tools

| Tool | Description |
|------|-------------|
| `search_code` | Semantic search — find code by meaning (e.g., "database connection handling") |
| `search_symbol` | Find symbols by name, optionally filter by type (function, class, method, route) |
| `get_file_overview` | List all symbols in a specific file |
| `index_status` | Check index health and statistics |
| `reindex` | Update index — incremental by default, `full=true` for clean rebuild |

### What Gets Indexed

- **Python** (.py) — Full AST parsing: functions, classes, methods, decorators, routes, constants, docstrings
- **JavaScript/TypeScript** (.js, .jsx, .ts, .tsx, .mjs, .cjs) — Regex-based: functions, arrow functions, classes, exports
- **Everything else** — Generic chunking: HTML, CSS, JSON, YAML, Markdown, SQL, PHP, Ruby, Go, Rust, Java, C/C++, shell scripts, config files, Dockerfiles, etc.

### What Gets Skipped

- Directories: `node_modules`, `venv`, `.git`, `dist`, `build`, `__pycache__`, `site-packages`, etc.
- Files: lock files, `.min.js`, `.map`, `.d.ts`, binary files, auto-generated files
- Size limits: Files under 10 bytes or over 500KB
- Minified content: Files with average line length > 500 characters

### Performance

- First index build: 10-60 seconds depending on project size
- Incremental reindex: Milliseconds to seconds (only processes changed files via mtime + content hash comparison)
- Full reindex on large projects (1000+ files): 2-3 minutes

## File Structure

```
~/.claude/tools/code-indexer/
    code_index_server.py      # Main MCP server entry point
    reindex_cli.py            # CLI tool for manual reindexing
    code_index/
        __init__.py
        indexer.py            # Build/update orchestrator
        parser.py             # Multi-language code parser
        embeddings.py         # Sentence-transformers wrapper
        database.py           # SQLite + sqlite-vec database layer
```

## Troubleshooting

### Server not connecting
1. Check registration: `claude mcp get code-index`
2. Check dependencies: Run the verify commands from Step 1
3. Check logs: `%LOCALAPPDATA%/claude-cli-nodejs/Cache/*/mcp-logs-code-index/` (Windows) or `~/.local/share/claude-cli-nodejs/Cache/*/mcp-logs-code-index/` (Mac/Linux)

### Wrong Python version
If you get import errors, re-register with the correct Python path:
```bash
claude mcp remove code-index
claude mcp add --scope user code-index -- /full/path/to/python ~/.claude/tools/code-indexer/code_index_server.py
```

## Config Locations Reference

| What | Where |
|------|-------|
| Server code | `~/.claude/tools/code-indexer/` |
| MCP registration | `claude mcp add --scope user code-index -- python ~/.claude/tools/code-indexer/code_index_server.py` |
| Permission | `~/.claude/settings.json` → `permissions.allow` → `"mcp__code-index__*"` |
| Hooks | `~/.claude/settings.json` → `hooks` section |
| `/index-project` skill | `~/.claude/skills/index-project/SKILL.md` |
| Claude instructions | `~/.claude/CLAUDE.md` → Code Index Usage section |
| Per-project index | `<project-root>/.code_index/` (auto-created, add to .gitignore) |
