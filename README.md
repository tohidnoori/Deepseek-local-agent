# DeepSeek Local Agent

A Windows desktop coding agent that drives your existing DeepSeek web account through a
Playwright-controlled Chrome instance. No API key is used. Runs in a GUI or headless CLI.

## Highlights

- **Sandboxed filesystem tools** — path-escape protection, ignored dirs (VCS, venvs,
  `node_modules`, build outputs, agent backups), sensitive files (`.env`, keys, credentials).
- **Atomic writes** + automatic timestamped backups under `.deepseek_agent_backups/`.
- **Patch-based editing** (`patch_file`) with unique-match enforcement — much safer than
  rewriting whole files from an LLM.
- **Approval gating** with a real diff preview for every mutating tool; safe shell commands
  (`dir`, `git status`, `python --version`, …) run without prompting.
- **Shell command policy** — a blocklist rejects destructive commands (format, diskpart,
  rm -rf /, `curl | sh`, …) even with auto-approve on.
- **Git tools** for status/diff/log/stage/commit, with the loop ending cleanly on a
  stage/commit-only round. A dedicated **Git dialog** in the GUI shows branch, status and
  unstaged diff, and lets you stage/commit from the app.
- **Loop detection** — if the model repeats the same tool calls twice, the run stops with
  a clear message instead of burning rounds.
- **Robust protocol parsing** — accepts fenced JSON, bare JSON, XML-encoded tool calls, and
  DeepSeek's stripped "Copy/Download" UI output.
- **Reply format switch** — `json` (default), `xml`, or `text` (chat-only, no tools).
- **Chat sessions** — every task is saved to a local JSON store, grouped by project. Resume,
  rename, or delete saved chats; the first user message becomes the title.
- **DeepSeek chat history** — list the sidebar chats from the web UI and open one directly,
  binding it to a saved session.
- **Attachments** — attach files/images to the first prompt of a task; they are uploaded to
  the DeepSeek chat input before the prompt is sent.
- **DeepThink toggle** — turn R1/DeepThink on or off from the GUI or via `--deepthink`.
- **Human-like pacing slider (0–100)** — a single "Speed ↔ Safety" control derives chunked
  typing delays, pre-send pauses, a sliding-window rate limit, and occasional long breaks.
  `0` is fast and risky; `100` is slow and safest. Exposed in the GUI and as `--safety` in
  the CLI.
- **Structured logging** via `DEEPSEEK_DEBUG=1` or `--debug`.

## Layout

```
main.py              PySide6 GUI
cli.py               Headless CLI
agent/
  browser_client.py  Chrome/CDP + DeepSeek DOM automation, pacing, attachments, chat history
  controller.py      Plan → act → observe loop, JSON/XML/text protocol
  registry.py        ToolRegistry (handlers + metadata)
  prompts.py         System + continuation prompts
  tools.py           Sandboxed filesystem tools
  shell_tools.py     cmd.exe runner + safety policy
  git_tools.py       Git subprocess wrapper
  sessions.py        Local JSON chat-session store
  config.py          Settings, limits, and safety→pacing mapping
  errors.py          Error hierarchy
  logging_setup.py   Logging
  __init__.py        Public API
tests/               Pytest suite
```

## Setup (Windows 10/11)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
playwright install chromium
```

If activation is blocked: `Set-ExecutionPolicy -Scope Process Bypass` first.

## Usage

### GUI

```powershell
python main.py
```

1. **Connect browser** → Chrome opens → sign in to DeepSeek yourself.
2. Click **I've signed in**.
3. Choose a project folder (Project tab).
4. Type a task and **Send task**.

GUI extras:

- **Speed ↔ Safety slider** — adjust pacing at runtime; the chosen level is logged and
  applied to the next prompt.
- **Reply format** dropdown — JSON / XML / Text.
- **DeepThink (R1)** checkbox and **New chat** button.
- **My chats** tab — saved sessions (double-click to resume).
- **DeepSeek chats** tab — list and open sidebar chats from the web UI.
- **Project** tab — file list, Git summary + **Git…** dialog, and the **auto-approve** switch.
- **Attach files…** — queue files/images for the next task.

### CLI

```powershell
python cli.py -p "C:\path\to\project" "Refactor utils.py to use pathlib and add tests"
python cli.py -p . --deepthink --debug "Investigate failing tests"
python cli.py -p . --safety 80 --format xml "Bump version in pyproject.toml"
python cli.py -p . --auto-approve "Fix lint errors"   # dangerous
```

Options: `--project/-p`, `--auto-approve`, `--deepthink`, `--safety 0-100`,
`--format json|xml|text`, `--max-rounds`, `--debug`.

CLI mode opens Chrome, waits for you to press Enter after signing in, then runs the same
loop the GUI uses.

## Tool protocol

The model must reply with exactly one JSON block per round:

```json
{
  "tool_calls": [
    {"name": "read_file", "arguments": {"path": "src/app.py"}},
    {"name": "patch_file", "arguments": {
      "path": "src/app.py",
      "edits": [{"old": "import os", "new": "import pathlib"}]
    }}
  ],
  "message": "Reading app.py and switching to pathlib."
}
```

With `--format xml`, the same payload can be encoded as:

```xml
<tool_calls>
  <tool_call>
    <name>read_file</name>
    <arguments>{"path": "src/app.py"}</arguments>
  </tool_call>
</tool_calls>
<message>Reading app.py.</message>
```

`--format text` disables tool use entirely and returns the model's reply as the final
summary. A round with no tool block also ends the task.

### Filesystem tools

`list_files`, `tree`, `read_file` (with `start`/`end` line range), `search_files`
(regex/glob), `write_file`, `create_file`, `append_file`, `patch_file`, `delete_file`,
`move_file`.

### Git tools

`git_status`, `git_diff`, `git_log`, `git_stage`, `git_commit`.

### Shell

`run_command` — read-only commands are auto-approved; everything else prompts unless
`--auto-approve` / the GUI checkbox is set. A blocklist of destructive commands is enforced
regardless.

## Sessions and attachments

Sessions are stored at `%LOCALAPPDATA%\DeepSeekLocalAgent\sessions.json`. Each session
records the project root, the DeepSeek chat URL (when known), and the message transcript.
The first user message becomes the session title.

Attachments are uploaded to the DeepSeek chat input on the first round only; subsequent
rounds reuse the same conversation.

## Pacing / anti-detection

The `Speed ↔ Safety` slider (or `--safety`) is mapped through `pacing_from_safety()` in
`agent/config.py` to concrete values: per-chunk typing delay, pre-type and pre-send pauses,
sliding-window rate limit (N prompts per W seconds), and an occasional long break every K
prompts. At `0` typing is a single fast paste with no throttling; at `100` prompts are split
into small chunks with long random pauses and strict rate limits.

## Safety model

- File access is confined to the chosen project folder; symlink/`..` escapes are rejected.
- Sensitive filenames and ignored directories are blocked from reads, writes, and listings.
- Every write creates a timestamped backup in `.deepseek_agent_backups/`.
- Writes use write-to-temp-then-rename, so a crash can't leave a half-written file.
- Mutating tool calls surface a diff (or the full command) for approval by default.
- The shell runner never pipes input and runs with a timeout.

## Development

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests exercise the filesystem sandbox (escape, sensitive files, atomic writes, patch
uniqueness) and the controller's protocol parser — no browser required.

## Limitations

- DeepSeek's web UI has no stable public automation contract. If selectors break, edit
  `agent/browser_client.py` — the selector lists are grouped at the top of the file.
- No streaming token capture; replies are detected by DOM stability.
- Very large projects should be explored with `tree` / `search_files` rather than a full
  dump.
