# DeepSeek Local Agent

A Windows desktop coding agent that drives your existing DeepSeek web account through a Playwright-controlled Chrome instance. It uses your browser session instead of an API key and is available as both a graphical application and a headless CLI.

The GUI provides two independent working modes:

| Mode | Description |
|---|---|
| **Agent Mode** | A structured tool-call loop. DeepSeek plans and calls local tools (read, write, patch, shell, and Git) until the task is complete. |
| **Normal Chat + File Assistant** | A natural chat experience. The app scans DeepSeek replies, detects code blocks, and turns them into reviewable file and command operations that you can apply to your project. |

Switch modes using the dropdown at the top of the window. The **? Help** button explains the capabilities of the selected mode.

## Highlights

- **Two independent modes** in one window, sharing the browser and selected project without mixing session state.
- **Sandboxed filesystem tools** with path-escape protection, ignored directories (VCS folders, virtual environments, `node_modules`, build outputs, and agent backups), and protection for sensitive files such as `.env` files, keys, and credentials.
- **Atomic writes** with automatic timestamped backups in `.deepseek_agent_backups/`.
- **Patch-based editing** through `patch_file`, with unique-match enforcement to reduce risky whole-file rewrites.
- **Approval gating** with a diff preview for mutating operations. Safe commands such as `dir`, `git status`, and `python --version` can run without prompting.
- **Shell command policy** that blocks destructive commands (for example, `format`, `diskpart`, `rm -rf /`, and `curl | sh`) even when auto-approve is enabled.
- **Git tools** for status, diff, log, staging, and commits. The GUI also includes a Git dialog showing the branch, status, and unstaged diff, with controls for staging and committing.
- **Loop detection** that stops a run if the model repeats the same tool calls, rather than wasting additional rounds.
- **Protocol parsing** for fenced JSON, bare JSON, XML-encoded tool calls, and DeepSeek's stripped Copy/Download UI output.
- **Reply format selection:** JSON (default), XML, or text. Text mode is chat-only and does not use tools.
- **Local chat sessions** saved as JSON and grouped by project. Sessions can be resumed, renamed, or deleted; the first user message is used as the title.
- **DeepSeek chat history** integration to list chats from the web UI and open one directly, associating it with a saved session.
- **Attachments** for files and images. Attachments are uploaded to the DeepSeek chat input with the first prompt.
- **DeepThink toggle** to enable or disable R1/DeepThink from the GUI or with `--deepthink`.
- **Speed ↔ Safety control (0–100)** that adjusts chunked typing delays, pre-send pauses, sliding-window rate limits, and occasional longer breaks. `0` is fastest and least cautious; `100` is slowest and most cautious. Available in the GUI and through `--safety`.
- **Structured logging** through `DEEPSEEK_DEBUG=1` or `--debug`.

## Normal Chat + File Assistant

Normal Chat + File Assistant does not require a tool protocol. DeepSeek responds in ordinary Markdown; no tool schema is injected into the conversation.

### Importing conversations

You can import a conversation in three ways:

1. **Share URL:** Paste a `chat.deepseek.com/share/...` link. The app tries the public JSON endpoint first and falls back to Playwright DOM-to-Markdown extraction if the endpoint is blocked.
2. **Paste transcript:** Paste a transcript containing `User:` and `Assistant:` labels.
3. **Scan current chat:** Read assistant messages from the DeepSeek page already open in the attached Chrome browser. No share link is needed.

During import, DeepSeek reasoning preambles (for example, “The user wants…” or “Let me plan…”) are removed at the DOM, API, and text-processing levels so the extractor receives the answer rather than the reasoning preamble.

A custom DOM-to-Markdown pass preserves code fences, headings, bold text, inline code, lists, and tables. This avoids the loss of code-fence boundaries that can happen when using plain `innerText()`.

### File-path detection

The extractor uses seven path-detection patterns with priority scoring:

| Priority | Pattern |
|---:|---|
| Highest | A file-path comment inside a code fence, such as `# file: x.py`, `// path: x.js`, or a quoted filename |
| 50 | Inline markers such as `file:`, `path:`, `فایل:`, or `مسیر:` |
| 40 | Markdown headings, such as `## 1. path/to/file.py` |
| 30 | Backticked paths, such as `` `path/to/file.py` `` |
| 20 | Bold paths, such as `**path/to/file.py**` |
| 10 | Plain-text path mentions |

Path mentions are deduplicated by position so that a multi-file response does not consume the same path more than once. The intended operation is inferred from nearby wording and can be one of:

- `CREATE`
- `REPLACE`
- `UPDATE`
- `APPEND`
- `DELETE`
- `MOVE`

The classifier recognizes relevant Persian terms as well, including `ایجاد`, `جایگزین`, and `حذف`.

A path mention is associated with a code fence only when it is sufficiently close to the fence (within 600 characters). If no path can be identified, the code block appears in the dashboard's **Extraction notes** rather than being assigned a guessed filename.

When the operation is ambiguous, the default is `REPLACE`. The dashboard still displays a full diff for review before the operation is applied.

### Command detection

A code fence is classified as a command if any of the following conditions is met:

- Its language tag is a shell language, such as `bash`, `sh`, `shell`, `zsh`, `fish`, `powershell`, `pwsh`, `ps1`, `cmd`, or `bat`.
- Its body starts with a `$ ` shell prompt.
- It has three lines or fewer and starts with a recognized program, such as `pip`, `python`, `git`, `npm`, `cargo`, or `docker`.
- It is longer, starts with a recognized program, and the paragraph immediately above it contains an execution cue such as `run`, `install`, `execute`, `setup`, `try`, `اجرا`, or `نصب`.

### Files and Commands dashboard

Detected operations appear in two dashboard tabs.

**Files tab**

- Preview the diff, or inspect the full content when the target does not exist.
- Apply one selected operation or apply all operations.
- Copy content or skip an operation.
- Missing targets are created automatically when appropriate.
- `CREATE` on an existing file and `DELETE` on a missing file are blocked.
- **Confirm overwrite** is enabled by default and asks before replacing an existing file.

**Commands tab**

- Review the command's safety classification: blocked, read-only, or requires confirmation.
- Run, copy, or skip a command.
- Commands that are not classified as safe require an explicit Yes/No confirmation.
- Execution runs on a worker thread and displays stdout, stderr, and the return code.

Each code block in the chat view also displays its detected filename, when available, with **Copy** and **Download** actions.

The full session is re-extracted after each assistant reply. Previously detected operations remain in the dashboard until you apply or skip them.

Chat-mode sessions use a separate JSON store and never mix with Agent Mode sessions.

## Project layout

The following is the documented project structure:

```text
.
├── main.py                    # PySide6 GUI (both modes)
├── cli.py                     # Headless CLI (Agent Mode only)
├── diagnose_extraction.py     # Debug file/command extraction
├── agent/
│   ├── browser_client.py      # Chrome/CDP and DeepSeek DOM automation
│   ├── controller.py          # Agent planning and tool-call loop
│   ├── registry.py            # Tool handlers and metadata
│   ├── prompts.py             # System and continuation prompts
│   ├── tools.py               # Sandboxed filesystem tools
│   ├── shell_tools.py         # Shell runner and safety policy
│   ├── git_tools.py           # Git subprocess wrapper
│   ├── sessions.py            # Agent Mode session store
│   ├── config.py              # Settings, limits, and pacing configuration
│   ├── errors.py              # Error hierarchy
│   ├── logging_setup.py       # Logging configuration
│   └── __init__.py            # Public package API
├── chat_session.py            # Normal Chat session store
├── chat_import.py             # Share URL, transcript, and browser import
├── file_ops_model.py          # FileOperation, CommandOperation, OperationSet
├── file_ops_extractor.py      # Path and command detection
├── file_ops_applier.py        # FileOperationApplier and CommandApplier
├── patcher.py                 # Find/replace text patcher
├── ui/
│   ├── __init__.py
│   ├── chat_panel.py          # Chat UI and code-block actions
│   └── file_ops_dashboard.py  # Files and Commands dashboard tabs
└── tests/                     # Pytest suite
```

## Requirements and setup

- Windows 10 or Windows 11
- Python (a version compatible with the dependencies in `requirements-dev.txt`)
- Google Chrome/Chromium support through Playwright

### Install

Open PowerShell in the project directory and run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
playwright install chromium
```

If PowerShell blocks virtual-environment activation, run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

## Usage

### GUI

Start the application:

```powershell
python main.py
```

On first launch:

1. Click **Connect browser**. Chrome opens.
2. Sign in to your DeepSeek account yourself.
3. Click **I've signed in** in the application.
4. Select a project folder. In Agent Mode, use the Project tab; in Normal Chat, use the Project row.
5. Choose a mode from the dropdown.

#### Agent Mode

Describe a task and click **Send task**. The controller sends structured JSON or XML tool-call requests, executes approved local operations, and continues until DeepSeek returns a final summary.

Additional controls include:

- **Speed ↔ Safety:** Change browser pacing at runtime. The selected level is logged and applied to subsequent prompts.
- **Reply format:** Choose JSON, XML, or Text.
- **DeepThink (R1):** Enable or disable DeepThink.
- **New chat:** Start a new session.
- **My chats:** View saved Agent Mode sessions and double-click one to resume it.
- **DeepSeek chats:** List and open chats from the DeepSeek web UI.
- **Project:** Browse files, inspect Git status, open the Git dialog, and control auto-approval.
- **Attach files:** Queue files or images for the next task.

#### Normal Chat + File Assistant

1. Type a message and click **Send**. DeepSeek replies naturally; no tool protocol is required.
2. To import a conversation, click **Import…** and choose one of the available methods:
   - **Share URL:** Paste a `https://chat.deepseek.com/share/...` link and click **Fetch conversation**. The app tries the public JSON endpoint, then falls back to headless Chromium rendering.
   - **Paste transcript:** Paste text containing `User:` and `Assistant:` labels, then click **Parse pasted**.
   - **Read via attached Chrome / Scan current chat:** Use the Chrome session in which you are already signed in to read assistant messages from the currently open chat.
3. Review the detected operations in the dashboard.
4. In **Files**, select an operation to inspect its diff or content. Use **Copy content**, **Skip**, **Apply selected**, or **Apply all**.
5. In **Commands**, inspect the safety verdict. Use **Copy command**, **Skip**, or **Run command**. Commands requiring confirmation display a Yes/No dialog.
6. Use the **Copy** or **Download** actions shown on individual code blocks when you want the code without applying it to the project.

### Help

The **? Help** button beside the mode dropdown explains the capabilities of the currently selected mode.

### CLI

The CLI supports **Agent Mode only**.

Examples:

```powershell
python cli.py -p "C:\path\to\project" "Refactor utils.py to use pathlib and add tests"

python cli.py -p . --deepthink --debug "Investigate failing tests"

python cli.py -p . --safety 80 --format xml "Bump version in pyproject.toml"

python cli.py -p . --auto-approve "Fix lint errors"
```

> **Warning:** `--auto-approve` allows mutating operations to proceed without the usual per-operation approval. Use it only when you trust the task and the project state.

Available options:

| Option | Description |
|---|---|
| `-p`, `--project` | Project directory |
| `--auto-approve` | Automatically approve operations that would otherwise require approval |
| `--deepthink` | Enable DeepThink/R1 |
| `--safety 0-100` | Configure the Speed ↔ Safety pacing level |
| `--format json\|xml\|text` | Select the response format |
| `--max-rounds` | Set the maximum number of agent rounds |
| `--debug` | Enable debug logging |

The CLI opens Chrome and waits for you to press Enter after you sign in. It then runs the same Agent Mode loop used by the GUI.

## Agent Mode tool protocol

By default, the model must return one JSON tool-call block per round. For example:

```json
{
  "tool_calls": [
    {
      "name": "read_file",
      "arguments": {
        "path": "src/app.py"
      }
    },
    {
      "name": "patch_file",
      "arguments": {
        "path": "src/app.py",
        "edits": [
          {
            "old": "import os",
            "new": "import pathlib"
          }
        ]
      }
    }
  ],
  "message": "Reading app.py and switching to pathlib."
}
```

With `--format xml`, the same payload can be represented as:

```xml
<tool_calls>
  <tool_call>
    <name>read_file</name>
    <arguments>{"path": "src/app.py"}</arguments>
  </tool_call>
</tool_calls>
<message>Reading app.py.</message>
```

With `--format text`, tool use is disabled and DeepSeek's reply is returned as the final summary. A round without a tool block also ends the task.

## Available tools

### Filesystem tools

- `list_files`
- `tree`
- `read_file` (supports start/end line ranges)
- `search_files` (supports regex/glob searches)
- `write_file`
- `create_file`
- `append_file`
- `patch_file`
- `delete_file`
- `move_file`

### Git tools

- `git_status`
- `git_diff`
- `git_log`
- `git_stage`
- `git_commit`

### Shell

`run_command` executes commands from the project root. Read-only commands are auto-approved; other commands require confirmation unless `--auto-approve` or the GUI auto-approve switch is enabled. The destructive-command blocklist is enforced regardless of auto-approve.

## Diagnosing file extraction

Run the diagnostic script against a share URL or a local transcript:

```powershell
python diagnose_extraction.py https://chat.deepseek.com/share/...

# Or:
python diagnose_extraction.py some_chat.txt
```

For each code fence, the script reports:

- The language tag and body length.
- Any path found inside the code block and the pattern that detected it.
- Path candidates found above the fence, including their scores and positions.
- Whether the code block was classified as a command.
- The resulting `OperationSet`, including files, commands, and extraction notes.

## Sessions and attachments

Agent Mode sessions are stored at:

```text
%LOCALAPPDATA%\DeepSeekLocalAgent\sessions.json
```

Each session records the project root, the DeepSeek chat URL (when known), and the message transcript. The first user message becomes the session title.

Normal Chat sessions are stored separately at:

```text
%LOCALAPPDATA%\DeepSeekLocalAgent\chat_sessions.json
```

The two session stores are independent.

Attachments are uploaded to the DeepSeek chat input on the first round only. Subsequent rounds continue in the same conversation.

## Pacing and browser automation

The Speed ↔ Safety slider (or CLI `--safety` option) is mapped by `pacing_from_safety()` in `agent/config.py` to concrete browser-pacing settings:

- Per-chunk typing delay.
- Pre-typing and pre-send pauses.
- Sliding-window prompt rate limit (N prompts per W seconds).
- Occasional longer breaks every K prompts.

At `0`, text is entered as a fast paste with no throttling. At `100`, prompts are split into smaller chunks with longer randomized pauses and stricter rate limits.

Pacing applies to both GUI modes because they share the same browser automation client.

## Safety model

- File access is confined to the selected project directory. Path traversal and symlink escapes are rejected.
- Sensitive filenames and ignored directories are blocked from reads, writes, and listings.
- Every write creates a timestamped backup in `.deepseek_agent_backups/`, including writes applied from the Normal Chat dashboard.
- Writes use a write-to-temporary-file-then-rename process to reduce the risk of leaving a partially written file after a crash.
- Mutating Agent Mode tool calls show a diff (or the full command) for approval by default.
- Normal Chat's **Confirm overwrite** option is enabled by default.
- Commands detected in Normal Chat use the same forbidden-pattern list as `run_command`.
- Commands run only if they are classified as safe or you explicitly confirm them. Blocked destructive commands remain blocked.
- The shell runner does not pipe input and enforces a timeout.
- Reasoning preambles are stripped from imported conversations so the file extractor processes the actual answer.

## Development and tests

Install the development dependencies:

```powershell
pip install -r requirements-dev.txt
```

Run the test suite:

```powershell
python -m pytest -q
```

The tests cover the filesystem sandbox (path escapes, sensitive files, atomic writes, and patch uniqueness) and the controller's protocol parser. These tests do not require a browser.

## Limitations

- DeepSeek's web UI does not provide a stable public automation contract. If its selectors change, update `agent/browser_client.py`; selector lists are grouped near the top of that file.
- The application does not capture streaming tokens. It detects completed replies through DOM stability.
- For very large projects, use `tree` and `search_files` rather than dumping the entire project into context.
- Normal Chat file-path detection is heuristic. If a code block has no clear filename nearby, it is reported in **Extraction notes** instead of being assigned a guessed path. Use `diagnose_extraction.py` to inspect the detection process.
- The DeepSeek share endpoint may be WAF-protected. If it returns an empty response, the app falls back to rendering the share page in headless Chromium, which can take a few seconds per import.
