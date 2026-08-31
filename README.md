# Code Index for Claude Code

A local MCP (Model Context Protocol) server that gives Claude Code semantic search across an entire codebase. Claude finds code by **meaning** ("where does auth happen"), not just string match.

Written for EWS teammates setting this up on their own machines. ~15 min install, one time per laptop. Per-project it's automatic after that.

---

## What you're installing

- A local Python MCP server that Claude Code calls as a tool
- A per-project vector database (auto-created on first use, stored in `.code_index/` at your project root)
- A few hooks in `~/.claude/settings.json` so Claude uses it automatically and reindexes in the background after edits

Nothing leaves your machine. The embedding model runs locally. The database is local SQLite.

**Supported languages:**
- **Python** (`.py`) — full AST parsing: functions, classes, methods, decorators, routes, constants, docstrings
- **JavaScript / TypeScript** (`.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs`) — regex-based: functions, arrow fns, classes, exports
- **PHP** (`.php`, `.phtml`, etc.) — regex-based: functions, methods, classes, interfaces, traits, enums, namespaces
- **Everything else** — generic chunking: HTML, CSS, JSON, YAML, Markdown, SQL, Ruby, Go, Rust, Java, C/C++, shell, Dockerfiles, config files

Skipped automatically: `node_modules`, `venv`, `.git`, `dist`, `build`, `__pycache__`, lockfiles, `.min.js`, `.map`, `.d.ts`, binaries, `old/` directories, files under 10 bytes or over 500 KB, minified content.

---

## Prerequisites

- Claude Code CLI installed (`claude` in your terminal)
- Python 3.10+ available on PATH
- Git
- ~100 MB disk space (mostly the embedding model, one-time download)

Works on Windows (Git Bash or PowerShell), macOS, and Linux. Paths below assume `~` is your home directory — on Windows that's usually `C:\Users\<you>\`.

---

## Step 1 — Install Python dependencies

**Recommended: dedicated venv** so it doesn't interfere with other Python projects.

```bash
python -m venv ~/.claude-code-index-venv

# Windows (Git Bash):
source ~/.claude-code-index-venv/Scripts/activate
# macOS / Linux:
source ~/.claude-code-index-venv/bin/activate

pip install "mcp[cli]" sentence-transformers sqlite-vec
deactivate
```

Or install globally if you prefer — replace the venv commands with just:

```bash
pip install "mcp[cli]" sentence-transformers sqlite-vec
```

Verify:

```bash
python -c "from mcp.server.fastmcp import FastMCP; print('mcp OK')"
python -c "from sentence_transformers import SentenceTransformer; print('sentence-transformers OK')"
python -c "import sqlite_vec; print('sqlite-vec OK')"
```

> First run of `sentence-transformers` downloads the `all-MiniLM-L6-v2` model (~80 MB). One-time.

---

## Step 2 — Get the server files

```bash
git clone https://github.com/epicwebstudios/claude-code-index.git
mkdir -p ~/.claude/tools/code-indexer/code_index
cp claude-code-index/code_index_server.py ~/.claude/tools/code-indexer/
cp claude-code-index/reindex_cli.py ~/.claude/tools/code-indexer/
cp claude-code-index/progress_gui.py ~/.claude/tools/code-indexer/
cp claude-code-index/code_index/*.py ~/.claude/tools/code-indexer/code_index/
```

---

## Step 3 — Register the MCP server with Claude Code

```bash
# If Python is directly on PATH:
claude mcp add --scope user code-index -- python ~/.claude/tools/code-indexer/code_index_server.py

# If you used the dedicated venv:
# Windows (Git Bash):
claude mcp add --scope user code-index -- ~/.claude-code-index-venv/Scripts/python ~/.claude/tools/code-indexer/code_index_server.py
# macOS / Linux:
claude mcp add --scope user code-index -- ~/.claude-code-index-venv/bin/python ~/.claude/tools/code-indexer/code_index_server.py
```

Verify:

```bash
claude mcp get code-index
```

You should see the server listed with its command.

---

## Step 4 — Wire up `~/.claude/settings.json`

Merge these into your existing `settings.json`. If you don't have one, create it at `~/.claude/settings.json`.

```json
{
  "permissions": {
    "allow": [
      "mcp__code-index__*"
    ]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "mcp__code-index__reindex",
        "hooks": [
          {
            "type": "command",
            "command": "echo 'BLOCKED: Do not call mcp__code-index__reindex directly. Run reindex via Bash with run_in_background: true instead.' && exit 1",
            "timeout": 5
          }
        ]
      },
      {
        "matcher": "Read|Grep|Glob",
        "hooks": [
          {
            "type": "command",
            "command": "if [ -f \"$PWD/.code_index/code_index.db\" ] && [ ! -f /tmp/.claude-codeindex-used ]; then echo '[code-index] Reminder: prefer mcp__code-index search tools (search_code, search_symbol, get_file_overview) for broad code exploration. Use Read/Grep/Glob for targeted lookups on known files.'; fi",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "touch \"/tmp/.claude-reindex-needed.$(echo \"$PWD\" | md5sum | cut -d' ' -f1)\"",
            "timeout": 5,
            "async": true
          }
        ]
      },
      {
        "matcher": "mcp__code-index__search_code|mcp__code-index__search_symbol|mcp__code-index__get_file_overview",
        "hooks": [
          {
            "type": "command",
            "command": "touch /tmp/.claude-codeindex-used",
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
            "command": "rm -f /tmp/.claude-codeindex-used",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "FLAG=\"/tmp/.claude-reindex-needed.$(echo \"$PWD\" | md5sum | cut -d' ' -f1)\"; if [ -f \"$PWD/.code_index/code_index.db\" ] && [ -f \"$FLAG\" ]; then rm \"$FLAG\" && python \"$HOME/.claude/tools/code-indexer/reindex_cli.py\" --quiet > /dev/null 2>&1 & disown; fi",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

### What each hook does

| Hook | Purpose |
|---|---|
| PreToolUse on `mcp__code-index__reindex` | Blocks Claude from calling reindex directly — forces it to run via Bash with `run_in_background: true` so the conversation doesn't stall |
| PreToolUse on `Read\|Grep\|Glob` | Gentle nudge toward code-index tools for broad searches. Only fires if you haven't used the index yet this session |
| PostToolUse on `Edit\|Write` | Drops a per-project flag (`/tmp/.claude-reindex-needed.<md5 of $PWD>`) so end-of-turn knows to reindex |
| PostToolUse on `mcp__code-index__search_*` | Marks the index as used, silences the reminder hook |
| UserPromptSubmit | Clears the used-flag. Never blocks |
| Stop (end of turn) | Kicks off a background incremental reindex if the per-project flag is set and the cwd has `.code_index/code_index.db`. Never blocks |

> **Important:** if you used the venv route in Step 1, replace `python` in the Stop hook command with the full path to your venv Python (e.g. `$HOME/.claude-code-index-venv/bin/python`).

---

## Step 5 — Tell Claude to use it (add to `~/.claude/CLAUDE.md`)

Append this to your global CLAUDE.md so Claude reaches for the index first:

```markdown
## Code Index (MANDATORY)

Prefer `mcp__code-index` tools (`search_code`, `search_symbol`, `get_file_overview`) over Grep/Glob for broad code searches. Use Grep/Glob for targeted lookups on known files or exact literal string matches.

**Reindexing**: ALWAYS via Bash with `run_in_background: true`. Never block the conversation on reindex.
**Background agents**: Run Explore and research agents with `run_in_background: true`.
```

---

## Step 6 — Install the `/index-project` slash command

This is the command you'll use day-to-day to rebuild or refresh the index. Run it from inside any Claude Code session.

```bash
mkdir -p ~/.claude/skills/index-project
cp skills/index-project/SKILL.md ~/.claude/skills/index-project/
```

Usage, from inside a Claude Code session opened in a project:
- `/index-project --full` — **full reindex from scratch** (first-time init, or a clean rebuild)
- `/index-project` — incremental reindex (fast, only changed files)

> The skill shells out to `~/.claude/tools/code-indexer/reindex_cli.py` under the hood. If you used a dedicated venv in Step 1, edit `~/.claude/skills/index-project/SKILL.md` and swap `python` for your venv Python path (e.g. `~/.claude-code-index-venv/Scripts/python` on Windows, `~/.claude-code-index-venv/bin/python` on Mac/Linux).

---

## Step 7 — Gitignore the index

Each project gets its own `.code_index/` directory. Exclude globally:

```bash
echo ".code_index/" >> ~/.gitignore_global
git config --global core.excludesfile ~/.gitignore_global
```

---

## Step 8 — Build the initial index for a project (`/index-project --full`)

The very first time you use the index in a project, you need to build it from scratch.

1. Open Claude Code inside the project root:
   ```bash
   cd ~/path/to/your-project
   claude
   ```
2. At the Claude Code prompt, type:
   ```
   /index-project --full
   ```

What happens:

1. A small Tk progress window pops up (Windows/macOS/Linux with Tk installed)
2. Your terminal shows a live progress bar — file discovery → parse → embed → store
3. A `.code_index/` folder is created in the project root with the SQLite vector DB
4. Takes 10–60 seconds on small projects, 2–3 minutes on large ones (1000+ files)

When it finishes you'll see something like:

```
  ==================================================
  Complete in 42.3s
  Files indexed: 287
  Files skipped: 41 (non-indexable)
  Total chunks:  1,942
  Symbol types:
    function: 612
    class: 84
    method: 443
    ...
```

### `/index-project --full` vs incremental — when to use which

| Command | What it does | When to run it |
|---|---|---|
| `/index-project --full` | Wipes `.code_index/` and rebuilds from scratch | **First time in a project.** Also any time the index looks broken, you've done a huge refactor, or you've pulled a branch that diverged massively |
| `/index-project` | Incremental — only re-processes files whose mtime or content hash changed | Everyday refreshes. Fast — milliseconds to seconds. This is what the background hooks run automatically between prompts |

You almost never need to run the incremental reindex by hand — the `UserPromptSubmit` hook does it for you between prompts whenever files changed. `/index-project --full` is the one you'll actually type.

### Fallback: running the CLI directly (skill not installed / outside Claude Code)

If you skipped Step 6 or want to run the indexer outside a Claude Code session, invoke the CLI directly from the project root:

```bash
# If Python is on PATH:
python ~/.claude/tools/code-indexer/reindex_cli.py --full

# If you used the dedicated venv (Step 1):
# Windows (Git Bash):
~/.claude-code-index-venv/Scripts/python ~/.claude/tools/code-indexer/reindex_cli.py --full
# macOS / Linux:
~/.claude-code-index-venv/bin/python ~/.claude/tools/code-indexer/reindex_cli.py --full
```

Drop `--full` for an incremental reindex. Same effect as the slash command — just more typing.

---

## Step 9 — Verify end-to-end

1. Open a Claude Code session in the project you just indexed: `cd ~/your-project && claude`
2. Ask Claude something that needs code search, e.g. *"What does the main entry point of this project do?"*
3. Claude should call `mcp__code-index__search_code` or `mcp__code-index__get_file_overview` — you'll see the tool calls in the session

If you don't see index tools being used, check:

- `claude mcp get code-index` — is the server registered?
- Your CLAUDE.md — did the "Code Index (MANDATORY)" section get added?
- `~/.claude/settings.json` — are the hooks present and valid JSON?
- Does `.code_index/code_index.db` exist in the project root? If not, Step 8 didn't complete — run it again

---

## Day-to-day usage

You shouldn't have to do anything. Claude calls the index tools automatically. After you edit files, the next prompt triggers a background incremental reindex. You just keep working.

**When to manually run `/index-project --full`:**
- You pulled a branch with a huge diff and searches feel stale
- You added/removed a bunch of top-level directories
- `mcp__code-index__index_status` reports missing files or looks wrong
- You upgraded the embedding model or the indexer itself

From inside a Claude Code session in the project:

```
/index-project --full
```

(See Step 8's "Fallback" section if you need to run it without the slash command.)

---

## What the tools do

| Tool | When Claude uses it |
|---|---|
| `search_code` | Semantic queries — patterns, implementations, "how does X work" |
| `search_symbol` | Finding a class / function / interface / trait / route by name |
| `get_file_overview` | What's in a file before reading it in full (big token saver) |
| `index_status` | Confirming the index is current and healthy |
| `reindex` | Refresh after edits. Background only, enforced by hook |

---

## Troubleshooting

**Server not connecting**
1. Re-register: `claude mcp remove code-index` then repeat Step 3
2. Check dependencies: rerun the verify commands from Step 1
3. Logs: `%LOCALAPPDATA%/claude-cli-nodejs/Cache/*/mcp-logs-code-index/` (Windows) or `~/.local/share/claude-cli-nodejs/Cache/*/mcp-logs-code-index/` (Mac/Linux)

**Wrong Python version / import errors**
Re-register with the full path to the correct Python:

```bash
claude mcp remove code-index
claude mcp add --scope user code-index -- /full/path/to/python ~/.claude/tools/code-indexer/code_index_server.py
```

**Index looks stale**
Force a full rebuild from a Claude Code session: `/index-project --full` (or run `python ~/.claude/tools/code-indexer/reindex_cli.py --full` from the project root if you don't have the slash command installed).

**Hooks not firing**
- JSON must be valid — run `python -m json.tool ~/.claude/settings.json` to check
- Restart Claude Code after editing `settings.json`
- On Windows, the hook commands use Git Bash syntax (`[ -f ... ]`, `touch`). Make sure Git Bash is installed.

---

## Reference — where things live

| What | Path |
|---|---|
| Server code | `~/.claude/tools/code-indexer/` |
| MCP registration | Managed by `claude mcp add` |
| Permission entry | `~/.claude/settings.json` → `permissions.allow` → `"mcp__code-index__*"` |
| Hooks | `~/.claude/settings.json` → `hooks` |
| Claude instructions | `~/.claude/CLAUDE.md` → "Code Index (MANDATORY)" section |
| Per-project index | `<project-root>/.code_index/` (auto-created, gitignored) |

---

## Repo structure

```
claude-code-index/
    code_index_server.py      # MCP server entry point
    reindex_cli.py            # CLI for manual reindexing
    progress_gui.py           # Optional Tk progress window
    code_index/
        __init__.py
        indexer.py            # Build/update orchestrator
        parser.py             # Multi-language code parser
        embeddings.py         # sentence-transformers wrapper
        database.py           # SQLite + sqlite-vec layer
    skills/index-project/
        SKILL.md              # Optional /index-project slash command
```

---

## Performance

- First index build: 10–60 seconds depending on project size
- Incremental reindex: milliseconds to seconds (mtime + content hash comparison)
- Full reindex on large projects (1000+ files): 2–3 minutes

### Memory footprint

Claude Code spawns **one MCP server per session**, so this server's resident
size is multiplied by however many sessions you run at once. Defaults are tuned
for that:

| State | Private memory |
|---|---|
| Idle (model not yet loaded) | ~88 MB |
| After the first semantic search | ~215 MB |

Two settings keep it there. Both were measured on a 12-core Windows box; the
server previously used **569 MB** per session.

**`OPENBLAS_NUM_THREADS`** (default `1`) — numpy's bundled OpenBLAS reserves
roughly 32 MB of per-thread working buffer at import time, so `import numpy`
alone costs ~394 MB at 12 threads:

| `OPENBLAS_NUM_THREADS` | `import numpy` cost |
|---|---|
| unset (= core count) | +394 MB |
| 4 | +136 MB |
| 2 | +72 MB |
| **1 (default here)** | **+40 MB** |

Embedding runs through ONNX Runtime, not BLAS — numpy only handles 384-dim
vectors — so capping it costs nothing. Measured throughput is if anything
slightly better at one thread (30 vs 26 chunks/s) from reduced contention.
Set the variable yourself before launching to override.

**`CODE_INDEX_PREWARM`** (default off) — set to `1` to load the embedding model
eagerly at startup instead of on first use. Eager loading costs ~128 MB whether
or not the session ever searches; the fastembed/ONNX backend loads in ~1.4s, so
the lazy default simply makes the first search take ~1.4s and every later one
~0.02s. Worth enabling only ahead of a large batch reindex.

### Other environment variables

| Variable | Default | Effect |
|---|---|---|
| `CODE_INDEX_PROJECT_ROOT` | cwd | Which project to index (required under Cursor) |
| `CODE_INDEX_BACKEND` | auto | Force `fastembed` or `sentence_transformers` |
| `CODE_INDEX_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Embedding model |
| `CODE_INDEX_BATCH_SIZE` | `256` | Chunks per encode pass |
| `CODE_INDEX_MODEL_TIMEOUT` | `180` | Seconds allowed for model load |
| `CODE_INDEX_EMBED_TIMEOUT` | `600` | Seconds allowed for one embed batch |
| `FASTEMBED_CACHE_PATH` | `.model_cache/` | Where the ONNX model is cached |

---

Questions — ping Sean.
