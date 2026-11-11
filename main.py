import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QLineEdit, QTextEdit, QFileDialog, QMessageBox, QSplitter, QListWidget,
    QListWidgetItem, QCheckBox, QDialog, QDialogButtonBox, QPlainTextEdit, QTabWidget,
    QSlider, QComboBox, QStackedWidget,
)

from agent.browser_client import DeepSeekBrowser
from agent.config import AgentConfig
from agent.controller import AgentController
from agent.git_tools import GitTools
from agent.logging_setup import configure_logging
from agent.sessions import SessionStore
from agent.shell_tools import ShellTools
from agent.tools import ProjectTools

from agent.chat_session import ChatSessionStore
from agent.chat_import import (
    import_from_share_url, import_from_pasted_text, import_via_browser,
)
from agent.file_ops_extractor import extract_operations
from agent.file_ops_applier import FileOperationApplier
from agent.ui.chat_panel import ChatPanel
from agent.ui.file_ops_dashboard import FileOpsDashboard


APP_STYLE = """
QMainWindow, QWidget { background: #111827; color: #e5e7eb; font-size: 13px; }
QTextEdit, QLineEdit, QListWidget, QPlainTextEdit { background: #0b1220; color: #e5e7eb; border: 1px solid #334155; border-radius: 8px; padding: 8px; }
QPushButton { background: #2563eb; color: white; border: 0; border-radius: 8px; padding: 9px 14px; }
QPushButton:hover { background: #1d4ed8; }
QPushButton:disabled { background: #475569; color: #cbd5e1; }
QLabel#muted { color: #94a3b8; }
QCheckBox { color: #e5e7eb; }
QTabWidget::pane { border: 1px solid #334155; border-radius: 8px; }
QTabBar::tab { background: #1f2937; padding: 6px 12px; border-radius: 6px; margin-right: 4px; }
QTabBar::tab:selected { background: #2563eb; }
QSlider::groove:horizontal { height: 6px; background: #1f2937; border-radius: 3px; }
QSlider::handle:horizontal { background: #2563eb; width: 16px; margin: -6px 0; border-radius: 8px; }
QComboBox { background: #0b1220; color: #e5e7eb; border: 1px solid #334155; border-radius: 6px; padding: 4px 8px; }
"""

AGENT_HELP = """<h2>Agent Mode</h2>
<p>Structured, tool-call protocol. You describe a task once; the model plans and
calls local tools until the task is done.</p>
<ul>
  <li><b>Connect browser</b> → Chrome launches with DeepSeek. Sign in and click "I've signed in".</li>
  <li><b>Choose a project folder</b> in the Project tab. All file operations are sandboxed there.</li>
  <li><b>Describe the task</b> in the prompt box and press <i>Send task</i>. The controller loops
      (JSON/XML tool blocks) until the model returns a summary.</li>
  <li><b>Approval</b>: any dangerous tool call (write, delete, run_command) triggers a confirm dialog
      unless <i>Auto-approve</i> is checked.</li>
  <li><b>Pacing</b> slider controls typing speed and rate limits (higher = safer, slower).</li>
  <li><b>Git…</b> opens a status/diff/commit dialog.</li>
  <li><b>Reply format</b>: JSON (default), XML, or Text (chat-only, no tools).</li>
</ul>
<p><b>Best for:</b> end-to-end coding tasks ("refactor X", "add feature Y") where you want the
model to actually read, edit and run things.</p>
"""

CHAT_HELP = """<h2>Normal Chat + File Assistant</h2>
<p>Chat naturally — no tool-call protocol. The app reads every code block in the reply and
turns it into a file- or command-operation you can review and apply.</p>
<ul>
  <li><b>Send</b>: your message goes to DeepSeek exactly as typed. No system prompt is injected.</li>
  <li><b>Import…</b>: paste a <code>chat.deepseek.com/share/…</code> link, paste a raw transcript,
      or read a share page via the attached Chrome.</li>
  <li><b>Scan current chat</b>: reads all assistant messages from the currently open browser page
      (no share link needed).</li>
  <li><b>Detection</b>: every ```fence``` is examined. The extractor picks a file name from:
      header comments inside the block (<code># file: x.py</code>), backticked paths just above
      the block, bold paths, markdown headings, or plain prose. It then classifies the intent
      (CREATE / REPLACE / UPDATE / APPEND / DELETE / MOVE) from the surrounding words.</li>
  <li><b>Commands</b>: fences tagged <code>bash</code>/<code>sh</code>/<code>powershell</code>/
      <code>cmd</code>, blocks with <code>$ </code> prompts, or bare command-like lines with a
      "run this" lead-in become command operations. Each one requires confirmation before it runs,
      inside the project root, and uses the same forbidden-pattern list as the agent.</li>
  <li><b>Dashboard tabs</b>: <i>Files</i> (diff preview + apply / skip / copy) and
      <i>Commands</i> (text + safety check + run / skip / copy).</li>
  <li><b>Code blocks in the chat</b> show <code>📄 file.py  📋 Copy  ⬇ Download</code> in the
      header.</li>
</ul>
<p><b>Best for:</b> iterating on real conversations, reviewing AI-generated code before
committing, importing past sessions, or running one-off shell commands from a chat.</p>
"""


def _make_controller(browser, tools, git, shell, config):
    return AgentController(browser, tools, git=git, shell=shell, config=config)


# ── Worker/BrowserAction/GitDialog/ImportDialog (unchanged) ────────────────
class Worker(QThread):
    finished_text = Signal(str)
    failed = Signal(str)
    approval_needed = Signal(str, object)
    progress = Signal(str)

    def __init__(self, controller, prompt, auto_approve=False, attachments=None, format_mode="json"):
        super().__init__()
        self.controller = controller
        self.prompt = prompt
        self.auto_approve = auto_approve
        self.attachments = list(attachments or [])
        self.format_mode = format_mode
        self._approved = None

    def approve(self, approved):
        self._approved = approved

    def run(self):
        browser = self.controller.browser
        try:
            self.progress.emit("Attaching to browser…")
            browser.attach()
            result = self.controller.run_task(
                self.prompt,
                auto_approve=self.auto_approve,
                approval_callback=self._request_approval,
                progress_callback=self.progress.emit,
                attachments=self.attachments,
                format_mode=self.format_mode,
            )
            self.finished_text.emit(str(result))
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")
        finally:
            browser.close()

    def _request_approval(self, description):
        self._approved = None
        self.approval_needed.emit(description, self)
        while self._approved is None:
            self.msleep(50)
        return self._approved


class BrowserAction(QThread):
    done = Signal(bool, str, object)

    def __init__(self, browser, action, payload=None):
        super().__init__()
        self.browser = browser
        self.action = action
        self.payload = payload

    def run(self):
        try:
            self.browser.attach()
            if self.action == "new_chat":
                ok = self.browser.new_chat()
                self.done.emit(ok, "New chat" if ok else "Could not find 'New chat'", None)
            elif self.action == "set_deepthink":
                self.browser.set_deepthink(bool(self.payload))
                state = self.browser.is_deepthink_on()
                self.done.emit(state if state is not None else True, f"DeepThink = {state}", None)
            elif self.action == "list_chats":
                rows = self.browser.list_sidebar_chats()
                self.done.emit(True, f"{len(rows)} chat(s)", rows)
            elif self.action == "open_chat":
                ok = self.browser.open_chat(str(self.payload))
                self.done.emit(ok, "Chat opened" if ok else "Could not open chat",
                               self.browser.current_url())
            elif self.action == "current_url":
                self.done.emit(True, "", self.browser.current_url())
            else:
                self.done.emit(False, f"Unknown action: {self.action}", None)
        except Exception as e:
            self.done.emit(False, f"{type(e).__name__}: {e}", None)
        finally:
            try:
                self.browser.close()
            except Exception:
                pass


class GitDialog(QDialog):
    def __init__(self, git_tools: GitTools, parent=None):
        super().__init__(parent)
        self.git = git_tools
        self.setWindowTitle(f"Git — {self.git.repo_root.name}")
        self.resize(940, 680)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel("Branch:"))
        self.branch_lbl = QLabel("…")
        header.addWidget(self.branch_lbl)
        header.addStretch()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        layout.addLayout(header)
        layout.addWidget(QLabel("Status"))
        self.status_view = QTextEdit(); self.status_view.setReadOnly(True)
        layout.addWidget(self.status_view, 1)
        layout.addWidget(QLabel("Diff (unstaged)"))
        self.diff_view = QTextEdit(); self.diff_view.setReadOnly(True)
        layout.addWidget(self.diff_view, 2)
        msg_row = QHBoxLayout()
        self.msg = QLineEdit(); self.msg.setPlaceholderText("Commit message")
        msg_row.addWidget(self.msg, 1)
        stage = QPushButton("Stage all"); stage.clicked.connect(self.stage_all)
        msg_row.addWidget(stage)
        commit = QPushButton("Commit"); commit.clicked.connect(self.do_commit)
        msg_row.addWidget(commit)
        layout.addLayout(msg_row)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        self.refresh()

    def refresh(self):
        try:
            self.branch_lbl.setText(self.git.branch())
            self.status_view.setPlainText(self.git.status() or "(clean)")
            self.diff_view.setPlainText(self.git.diff() or "(no unstaged changes)")
        except Exception as e:
            QMessageBox.warning(self, "Git error", str(e))

    def stage_all(self):
        try:
            self.git.stage(); self.refresh()
        except Exception as e:
            QMessageBox.warning(self, "Git error", str(e))

    def do_commit(self):
        msg = self.msg.text().strip()
        if not msg:
            QMessageBox.warning(self, "Commit", "Enter a commit message.")
            return
        try:
            self.git.commit(msg); self.msg.clear(); self.refresh()
        except Exception as e:
            QMessageBox.warning(self, "Git error", str(e))


class ImportDialog(QDialog):
    def __init__(self, browser=None, parent=None):
        super().__init__(parent)
        self.browser = browser
        self._messages: list[dict[str, str]] = []
        self.setWindowTitle("Import conversation")
        self.resize(760, 620)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()

        url_tab = QWidget(); ul = QVBoxLayout(url_tab)
        ul.addWidget(QLabel(
            "Paste a DeepSeek share link:\nhttps://chat.deepseek.com/share/…"
        ))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://chat.deepseek.com/share/…")
        ul.addWidget(self.url_input)
        self.use_browser_cb = QCheckBox("Read via attached Chrome")
        ul.addWidget(self.use_browser_cb)
        b = QPushButton("Fetch conversation"); b.clicked.connect(self._fetch_url)
        ul.addWidget(b); ul.addStretch()
        self.tabs.addTab(url_tab, "Share URL")

        paste_tab = QWidget(); pl = QVBoxLayout(paste_tab)
        pl.addWidget(QLabel(
            "Paste a transcript with  **User:** / **Assistant:**  labels."
        ))
        self.paste_input = QPlainTextEdit()
        pl.addWidget(self.paste_input, 1)
        b = QPushButton("Parse pasted"); b.clicked.connect(self._parse_paste)
        pl.addWidget(b)
        self.tabs.addTab(paste_tab, "Paste transcript")

        layout.addWidget(self.tabs, 1)
        layout.addWidget(QLabel("Preview"))
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 2)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._accept); btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        self.ok_btn = btns.button(QDialogButtonBox.Ok)
        self.ok_btn.setEnabled(False)

    def _show(self, messages):
        self._messages = messages
        self.preview.clear()
        for m in messages:
            self.preview.appendPlainText(f"[{m['role']}]")
            self.preview.appendPlainText(m["content"])
            self.preview.appendPlainText("")
        self.ok_btn.setEnabled(bool(messages))

    def _fetch_url(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Empty", "Paste a URL.")
            return
        try:
            if self.use_browser_cb.isChecked() and self.browser:
                msgs = import_via_browser(self.browser, url)
            else:
                msgs = import_from_share_url(url)
            self._show(msgs)
        except Exception as e:
            QMessageBox.warning(self, "Import failed", str(e))

    def _parse_paste(self):
        text = self.paste_input.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Empty", "Paste some text.")
            return
        try:
            self._show(import_from_pasted_text(text))
        except Exception as e:
            QMessageBox.warning(self, "Parse failed", str(e))

    def _accept(self):
        if not self._messages:
            QMessageBox.warning(self, "Nothing to import", "Fetch or parse first.")
            return
        self.accept()

    def result_messages(self):
        return self._messages


class HelpDialog(QDialog):
    def __init__(self, mode: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Help — " + mode)
        self.resize(720, 620)
        v = QVBoxLayout(self)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setHtml(AGENT_HELP if mode.startswith("Agent") else CHAT_HELP)
        v.addWidget(view, 1)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DeepSeek Local Agent")
        self.resize(1320, 840)
        self.config = AgentConfig()
        self.config.apply_safety_level()
        self.browser = DeepSeekBrowser()
        self.browser.set_safety_level(self.config.safety_level)
        self.sessions = SessionStore()
        self.project_root: Path | None = None
        self.git_tools = None
        self.shell_tools = None
        self.project_tools = None
        self.controller = None
        self.worker = None
        self._browser_action = None
        self.current_session_id = None
        self.attachments: list[str] = []

        self._chat_store = ChatSessionStore()
        self._chat_panel: ChatPanel | None = None
        self._ops_dashboard: FileOpsDashboard | None = None
        self._current_mode = "Agent Mode"

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        # top bar
        top = QHBoxLayout()
        ttl = QLabel("DeepSeek Local Agent")
        ttl.setStyleSheet("font-size: 20px; font-weight: 700;")
        top.addWidget(ttl)
        top.addStretch()
        self.status = QLabel("Browser not connected"); self.status.setObjectName("muted")
        top.addWidget(self.status)
        self.connect_btn = QPushButton("Connect browser")
        self.connect_btn.clicked.connect(self.connect_browser)
        top.addWidget(self.connect_btn)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Reconnect if the browser gets a socket error or locks up")
        self.refresh_btn.clicked.connect(self.refresh_browser)
        top.addWidget(self.refresh_btn)
        self.signed_btn = QPushButton("I've signed in")
        self.signed_btn.setEnabled(False)
        self.signed_btn.clicked.connect(self.finish_login)
        top.addWidget(self.signed_btn)
        outer.addLayout(top)

        # mode row + help
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Agent Mode", "Normal Chat + File Assistant"])
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_combo)
        help_btn = QPushButton("?  Help")
        help_btn.clicked.connect(self._show_help)
        mode_row.addWidget(help_btn)
        mode_row.addStretch()
        outer.addLayout(mode_row)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        self.agent_widget = QWidget()
        self._build_agent_ui(self.agent_widget)
        self.stack.addWidget(self.agent_widget)

        self.chat_widget = QWidget()
        self._build_chat_ui(self.chat_widget)
        self.stack.addWidget(self.chat_widget)

        self.setStyleSheet(APP_STYLE)
        self.refresh_saved_sessions()
        self.on_safety_changed(self.safety_slider.value())
        self._refresh_chat_sessions()

    def _show_help(self):
        HelpDialog(self._current_mode, self).exec()

    # ── Agent-mode UI (unchanged) ────────────────────────────────────────
    def _build_agent_ui(self, container):
        layout = QVBoxLayout(container); layout.setContentsMargins(0, 0, 0, 0)
        opts = QHBoxLayout(); opts.addWidget(QLabel("Browser options:"))
        self.deepthink_cb = QCheckBox("DeepThink (R1)")
        self.deepthink_cb.toggled.connect(self.on_deepthink_toggled)
        opts.addWidget(self.deepthink_cb)
        self.newchat_btn = QPushButton("New chat")
        self.newchat_btn.clicked.connect(self.on_new_chat)
        self.newchat_btn.setEnabled(False)
        opts.addWidget(self.newchat_btn); opts.addStretch()
        layout.addLayout(opts)

        pacing = QHBoxLayout()
        pacing.addWidget(QLabel("Safety:"))
        self.safety_slider = QSlider(Qt.Horizontal)
        self.safety_slider.setRange(0, 100)
        self.safety_slider.setValue(self.config.safety_level)
        self.safety_slider.setFixedWidth(240)
        self.safety_slider.valueChanged.connect(self.on_safety_changed)
        self.safety_slider.sliderReleased.connect(self._log_pacing)
        pacing.addWidget(self.safety_slider)
        self.safety_lbl = QLabel("50 — Balanced"); self.safety_lbl.setObjectName("muted")
        pacing.addWidget(self.safety_lbl)
        pacing.addSpacing(20)
        pacing.addWidget(QLabel("Format:"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(["JSON", "XML", "Text"])
        self.format_combo.setCurrentText(self.config.format_mode.upper())
        self.format_combo.currentTextChanged.connect(self.on_format_changed)
        pacing.addWidget(self.format_combo); pacing.addStretch()
        layout.addLayout(pacing)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, 1)

        left = QWidget(); ll = QVBoxLayout(left)
        self.tabs = QTabWidget(); ll.addWidget(self.tabs, 3)

        mt = QWidget(); ml = QVBoxLayout(mt)
        ml.addWidget(QLabel("My chats")); self.saved_list = QListWidget()
        self.saved_list.itemDoubleClicked.connect(self.resume_saved_session)
        ml.addWidget(self.saved_list, 1)
        row = QHBoxLayout()
        b = QPushButton("New"); b.clicked.connect(self.start_new_session); row.addWidget(b)
        b = QPushButton("Open")
        b.clicked.connect(lambda: self.resume_saved_session(self.saved_list.currentItem()))
        row.addWidget(b)
        b = QPushButton("Delete"); b.clicked.connect(self.delete_saved_session); row.addWidget(b)
        ml.addLayout(row); self.tabs.addTab(mt, "My chats")

        dt = QWidget(); dl = QVBoxLayout(dt)
        dl.addWidget(QLabel("DeepSeek chats"))
        self.ds_list = QListWidget()
        self.ds_list.itemDoubleClicked.connect(self.open_deepseek_chat)
        dl.addWidget(self.ds_list, 1)
        row2 = QHBoxLayout()
        b = QPushButton("Refresh"); b.clicked.connect(self.refresh_deepseek_chats); row2.addWidget(b)
        b = QPushButton("Open")
        b.clicked.connect(lambda: self.open_deepseek_chat(self.ds_list.currentItem()))
        row2.addWidget(b)
        dl.addLayout(row2); self.tabs.addTab(dt, "DeepSeek chats")

        pt = QWidget(); pl = QVBoxLayout(pt)
        self.project_label = QLabel("No project selected")
        self.project_label.setObjectName("muted"); self.project_label.setWordWrap(True)
        pl.addWidget(self.project_label)
        b = QPushButton("Choose project folder"); b.clicked.connect(self.choose_project)
        pl.addWidget(b)
        self.git_summary = QLabel("Git: not a repository"); self.git_summary.setObjectName("muted")
        pl.addWidget(self.git_summary)
        self.git_btn = QPushButton("Git…"); self.git_btn.setEnabled(False)
        self.git_btn.clicked.connect(self.open_git_dialog); pl.addWidget(self.git_btn)
        pl.addWidget(QLabel("Project files"))
        self.file_list = QListWidget()
        self.file_list.itemDoubleClicked.connect(self.show_file)
        pl.addWidget(self.file_list, 1)
        self.auto_approve = QCheckBox("Auto-approve all operations (dangerous)")
        pl.addWidget(self.auto_approve)
        self.tabs.addTab(pt, "Project")
        splitter.addWidget(left)

        right = QWidget(); rl = QVBoxLayout(right)
        self.session_label = QLabel("No chat selected"); self.session_label.setObjectName("muted")
        rl.addWidget(self.session_label)
        self.chat = QTextEdit(); self.chat.setReadOnly(True)
        rl.addWidget(self.chat, 1)
        arow = QHBoxLayout()
        self.attach_btn = QPushButton("Attach files…")
        self.attach_btn.clicked.connect(self.choose_attachments)
        arow.addWidget(self.attach_btn)
        self.attach_lbl = QLabel("No files attached"); self.attach_lbl.setObjectName("muted")
        arow.addWidget(self.attach_lbl, 1)
        b = QPushButton("Clear"); b.clicked.connect(self.clear_attachments); arow.addWidget(b)
        rl.addLayout(arow)
        self.prompt = QTextEdit(); self.prompt.setFixedHeight(105)
        rl.addWidget(self.prompt)
        brow = QHBoxLayout()
        self.send_btn = QPushButton("Send task"); self.send_btn.clicked.connect(self.send_task)
        brow.addWidget(self.send_btn)
        b = QPushButton("Clear view"); b.clicked.connect(self.chat.clear); brow.addWidget(b)
        rl.addLayout(brow)
        splitter.addWidget(right); splitter.setSizes([340, 980])

    def _build_chat_ui(self, container):
        layout = QVBoxLayout(container); layout.setContentsMargins(0, 0, 0, 0)
        proj_row = QHBoxLayout()
        proj_row.addWidget(QLabel("Project:"))
        self.chat_project_lbl = QLabel(str(self.project_root) if self.project_root else "No project")
        self.chat_project_lbl.setObjectName("muted"); self.chat_project_lbl.setWordWrap(True)
        proj_row.addWidget(self.chat_project_lbl, 1)
        b = QPushButton("Choose project"); b.clicked.connect(self.choose_project)
        proj_row.addWidget(b)
        layout.addLayout(proj_row)
        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, 1)
        self._chat_panel = ChatPanel(self._chat_store)
        if hasattr(self._chat_panel, "import_requested"):
            self._chat_panel.import_requested.connect(self._on_import_requested)
        if hasattr(self._chat_panel, "response_received"):
            self._chat_panel.response_received.connect(self._on_chat_response)
        if hasattr(self._chat_panel, "scan_requested"):
            self._chat_panel.scan_requested.connect(self._on_scan_chat)
        splitter.addWidget(self._chat_panel)
        self._ops_dashboard = FileOpsDashboard()
        splitter.addWidget(self._ops_dashboard)
        splitter.setSizes([720, 500])

    # ── Mode switching ──────────────────────────────────────────────────
    def _on_mode_changed(self, mode: str):
        self._current_mode = mode
        if mode.startswith("Agent"):
            self.stack.setCurrentIndex(0)
        else:
            self.stack.setCurrentIndex(1)
            if self._chat_panel is not None:
                self._chat_panel.set_browser(self.browser)
            self._refresh_chat_sessions()
            self._refresh_dashboard_appliers()

    def _refresh_dashboard_appliers(self):
        if self._ops_dashboard is None:
            return
        if self.project_tools is not None:
            self._ops_dashboard.set_applier(
                FileOperationApplier(self.project_tools),
                self.shell_tools,
            )

    def _refresh_chat_sessions(self):
        if self._chat_panel is None:
            return
        fn = getattr(self._chat_panel, "refresh_sessions", None)
        if callable(fn):
            try: fn()
            except Exception: pass

    # ── Chat-mode callbacks ─────────────────────────────────────────────
    def _on_chat_response(self, session_id, assistant_text):
        session = self._chat_store.get(session_id)
        if not session: return
        ops = extract_operations(session.messages)
        if self._ops_dashboard is None: return
        self._refresh_dashboard_appliers()
        self._ops_dashboard.load_operations(ops)
        if ops.operations or ops.commands or ops.errors:
            self.log("system",
                     f"Detected {len(ops.operations)} file op(s), "
                     f"{len(ops.commands)} command(s).")

    def _on_scan_chat(self):
        if not self.browser.ready:
            QMessageBox.warning(self, "Not connected", "Connect browser first.")
            return
        try:
            self.browser.attach()
            texts = self.browser._assistant_texts()
        except Exception as e:
            QMessageBox.warning(self, "Browser error", str(e))
            return
        finally:
            try: self.browser.close()
            except Exception: pass
        if not texts:
            QMessageBox.information(self, "No messages", "No assistant text on this page.")
            return
        s = self._chat_store.create(
            f"Scan {time.strftime('%H:%M:%S')}",
            str(self.project_root) if self.project_root else None,
        )
        for t in texts:
            s.add("assistant", t)
        if self._chat_panel is not None:
            self._chat_panel.load_session(s.id)
        self._on_chat_response(s.id, texts[-1])

    def _on_import_requested(self):
        dlg = ImportDialog(self.browser if self.browser.ready else None, self)
        if dlg.exec() != QDialog.Accepted: return
        msgs = dlg.result_messages()
        if not msgs: return
        title = "Imported chat"
        for m in msgs:
            if m["role"] == "user" and m["content"].strip():
                title = m["content"].strip().splitlines()[0][:80]; break
        s = self._chat_store.create(
            title, str(self.project_root) if self.project_root else None)
        for m in msgs:
            s.add(m["role"], m["content"])
        if self._chat_panel is not None:
            self._chat_panel.load_session(s.id)
        self._on_chat_response(s.id, msgs[-1]["content"] if msgs else "")

    # ── Helpers, sessions, browser, project, task (unchanged behaviour) ──
    def log(self, who, text):
        escaped = (text.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;").replace("\n", "<br>"))
        self.chat.append(f"<b>{who}</b><br>{escaped}<br>")

    def _session(self):
        return self.sessions.get(self.current_session_id) if self.current_session_id else None

    def _store(self, role, text):
        if self.current_session_id:
            self.sessions.add_message(self.current_session_id, role, text)
            self.refresh_saved_sessions()

    def on_safety_changed(self, value):
        self.config.safety_level = int(value)
        self.config.apply_safety_level()
        self.browser.set_safety_level(int(value))
        label = ("Fast (risky)" if value < 25 else "Moderate" if value < 50
                 else "Balanced" if value < 75 else "Safe (slow)")
        self.safety_lbl.setText(f"{value} — {label}")

    def _log_pacing(self):
        try:
            self.log("system",
                     f"Pacing: safety={self.config.safety_level}, "
                     f"type_delay={self.browser.type_delay_min}-{self.browser.type_delay_max}ms, "
                     f"rate_limit={self.browser.rate_limit_prompts}/{self.browser.rate_limit_window_s}s")
        except Exception: pass

    def on_format_changed(self, text):
        self.config.format_mode = text.lower()

    def refresh_saved_sessions(self):
        cur = self.saved_list.currentItem() if self.saved_list.count() else None
        cur_id = cur.data(Qt.UserRole) if cur else None
        self.saved_list.clear()
        for s in self.sessions.all():
            item = QListWidgetItem(f"{s.title}  ·  {s.project_name}")
            item.setData(Qt.UserRole, s.id)
            item.setToolTip(f"{s.project_root}\n{s.deepseek_url or '(new)'}")
            self.saved_list.addItem(item)
            if s.id == cur_id:
                self.saved_list.setCurrentItem(item)

    def start_new_session(self):
        if not self.project_root:
            QMessageBox.information(self, "No project", "Choose a project first.")
            return
        s = self.sessions.create(str(self.project_root), "New chat")
        self.current_session_id = s.id
        self.chat.clear()
        self.session_label.setText(f"Chat: {s.title}")
        self.refresh_saved_sessions()
        if self.browser.ready:
            self._run_browser_action("new_chat", on_done=self._after_new_chat)

    def _after_new_chat(self, ok, msg, payload):
        if ok: self._run_browser_action("current_url", on_done=self._capture_url)

    def _capture_url(self, ok, msg, url):
        if ok and url and self.current_session_id:
            self.sessions.set_deepseek_url(self.current_session_id, url)

    def resume_saved_session(self, item):
        if item is None: return
        s = self.sessions.get(item.data(Qt.UserRole))
        if not s: return
        self.current_session_id = s.id
        try: self._set_project(Path(s.project_root))
        except Exception: pass
        self.chat.clear()
        for m in s.messages:
            self.log(m.get("role", "?"), m.get("text", ""))
        self.session_label.setText(f"Chat: {s.title}")
        if s.deepseek_url and self.browser.ready:
            self._run_browser_action("open_chat", s.deepseek_url,
                                     on_done=self._after_open_chat)

    def _after_open_chat(self, ok, msg, url):
        self.log("system", f"Resumed chat at {url}" if ok else
                 "Could not open the chat; sending will start a new one.")

    def delete_saved_session(self):
        item = self.saved_list.currentItem()
        if not item: return
        sid = item.data(Qt.UserRole)
        if QMessageBox.question(self, "Delete", "Delete this chat?",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.sessions.delete(sid)
        if self.current_session_id == sid:
            self.current_session_id = None
            self.chat.clear()
            self.session_label.setText("No chat selected")
        self.refresh_saved_sessions()

    def refresh_deepseek_chats(self):
        if not self.browser.ready:
            QMessageBox.warning(self, "Not connected", "Connect first.")
            return
        self.ds_list.clear()
        self._run_browser_action("list_chats", on_done=self._fill_ds_list)

    def _fill_ds_list(self, ok, msg, rows):
        if not ok:
            QMessageBox.warning(self, "DeepSeek", msg); return
        for row in rows or []:
            item = QListWidgetItem(row.get("title") or row.get("href", ""))
            item.setData(Qt.UserRole, row.get("href") or row.get("title"))
            item.setToolTip(row.get("href", ""))
            self.ds_list.addItem(item)

    def open_deepseek_chat(self, item):
        if item is None: return
        if not self.project_root:
            QMessageBox.information(self, "No project", "Choose a project first.")
            return
        href = item.data(Qt.UserRole) or item.text()
        s = self.sessions.create(str(self.project_root), item.text() or "DeepSeek chat")
        self.sessions.set_deepseek_url(s.id, href)
        self.current_session_id = s.id
        self.chat.clear(); self.refresh_saved_sessions()
        self.session_label.setText(f"Chat: {s.title}")
        self._run_browser_action("open_chat", href, on_done=self._after_open_chat)

    def connect_browser(self):
        try:
            self.status.setText("Opening Chrome...")
            self.browser.open_chrome()
            self.status.setText("Chrome opened — sign in then click 'I've signed in'")
            self.signed_btn.setEnabled(True); self.connect_btn.setEnabled(False)
        except Exception as e:
            QMessageBox.critical(self, "Browser error", str(e))

    def finish_login(self):
        self.browser.mark_ready()
        self.status.setText("Browser ready")
        self.signed_btn.setEnabled(False); self.newchat_btn.setEnabled(True)
        self._ensure_controller()
        self.refresh_deepseek_chats()
        QMessageBox.information(self, "Connected", "Browser session ready.")

    def refresh_browser(self):
        """Reconnect to Chrome if the browser hit a socket error or locked up."""
        if not self.browser.ready:
            QMessageBox.information(self, "Refresh", "Browser is not connected yet. Click 'Connect browser' first.")
            return
        try:
            self.status.setText("Refreshing browser...")
            ok = self.browser.refresh(navigate=True)
        except Exception as e:
            QMessageBox.critical(self, "Browser error", str(e))
            self.status.setText("Browser not connected")
            return
        if ok:
            self.status.setText("Browser ready (refreshed)")
        else:
            self.status.setText("Refresh failed — try Connect again")
            QMessageBox.warning(self, "Refresh", "Could not refresh the browser. Try 'Connect browser' again.")

    def _ensure_controller(self):
        if not (self.browser.ready and self.project_root): return
        if self.project_tools is None:
            self.project_tools = ProjectTools(self.project_root, self.config)
        if self.shell_tools is None:
            self.shell_tools = ShellTools(self.project_root, timeout=self.config.shell_timeout)
        self.controller = _make_controller(
            self.browser, self.project_tools, self.git_tools, self.shell_tools, self.config)

    def _run_browser_action(self, action, payload=None, on_done=None):
        if self._browser_action and self._browser_action.isRunning():
            QMessageBox.information(self, "Busy", "Browser action running.")
            return
        self._browser_action = BrowserAction(self.browser, action, payload)
        if on_done is not None: self._browser_action.done.connect(on_done)
        self._browser_action.start()

    def on_deepthink_toggled(self, checked):
        self.browser._deepthink_on = bool(checked)
        for name in ("deepthink_cb", "deepthink_cb_chat"):
            cb = getattr(self, name, None)
            if cb is not None and cb.isChecked() != checked:
                cb.blockSignals(True); cb.setChecked(checked); cb.blockSignals(False)
        if not self.browser.ready: return
        self._run_browser_action("set_deepthink", bool(checked))

    def on_new_chat(self):
        if not self.browser.ready:
            QMessageBox.warning(self, "Not connected", "Connect first."); return
        if self.project_root: self.start_new_session()
        else: self._run_browser_action("new_chat")

    def choose_project(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose project folder")
        if not folder: return
        self._set_project(Path(folder))

    def _set_project(self, folder: Path):
        self.project_root = folder.resolve()
        if hasattr(self, "project_label"): self.project_label.setText(str(self.project_root))
        if hasattr(self, "chat_project_lbl"): self.chat_project_lbl.setText(str(self.project_root))
        self.git_tools = GitTools(self.project_root)
        self.project_tools = ProjectTools(self.project_root, self.config)
        self.shell_tools = ShellTools(self.project_root, timeout=self.config.shell_timeout)
        self.refresh_git_summary(); self.refresh_files()
        self._ensure_controller()
        self._refresh_dashboard_appliers()

    def refresh_git_summary(self):
        if not self.git_tools or not self.git_tools.is_repo():
            self.git_summary.setText("Git: not a repository"); self.git_btn.setEnabled(False)
            return
        try:
            br = self.git_tools.branch()
            st = self.git_tools.status()
        except Exception as e:
            self.git_summary.setText(f"Git error: {e}"); self.git_btn.setEnabled(False); return
        dirty = sum(1 for l in st.splitlines() if l and not l.startswith("##"))
        self.git_summary.setText(f"Git: {br} — {dirty} change(s)")
        self.git_btn.setEnabled(True)

    def open_git_dialog(self):
        if not self.git_tools or not self.git_tools.is_repo(): return
        GitDialog(self.git_tools, self).exec(); self.refresh_git_summary()

    def refresh_files(self):
        self.file_list.clear()
        if not self.project_root: return
        try:
            tools = ProjectTools(self.project_root, self.config)
            for rel in tools.list_files("."):
                self.file_list.addItem(rel)
        except Exception as e:
            self.log("error", f"List failed: {e}")

    def show_file(self, item):
        if not self.project_root: return
        try:
            tools = ProjectTools(self.project_root, self.config)
            content = tools.read_file(item.text())
        except Exception as e:
            QMessageBox.warning(self, "Cannot open", str(e)); return
        dlg = QDialog(self); dlg.setWindowTitle(item.text()); dlg.resize(850, 600)
        v = QVBoxLayout(dlg)
        tv = QPlainTextEdit(); tv.setReadOnly(True); tv.setPlainText(content)
        v.addWidget(tv)
        bb = QDialogButtonBox(QDialogButtonBox.Close); bb.rejected.connect(dlg.reject)
        v.addWidget(bb); dlg.exec()

    def choose_attachments(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach files", "",
                                                 "All files (*.*)")
        for p in paths:
            if p not in self.attachments:
                self.attachments.append(p)
        self._update_attach_label()

    def clear_attachments(self):
        self.attachments = []; self._update_attach_label()

    def _update_attach_label(self):
        if not self.attachments:
            self.attach_lbl.setText("No files attached"); return
        names = [Path(p).name for p in self.attachments]
        shown = ", ".join(names[:3]) + (f" +{len(names)-3} more" if len(names) > 3 else "")
        self.attach_lbl.setText(f"{len(names)} file(s): {shown}")

    def send_task(self):
        if not self.browser.ready:
            QMessageBox.warning(self, "Not connected", "Connect first."); return
        if not self.project_root:
            QMessageBox.warning(self, "No project", "Choose a project first."); return
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Busy", "Task running."); return
        prompt = self.prompt.toPlainText().strip()
        if not prompt: return
        if not self.current_session_id:
            self.start_new_session()
            if not self.current_session_id: return
        self._ensure_controller()
        if not self.controller:
            QMessageBox.warning(self, "Not ready", "Controller unavailable."); return
        self.browser._deepthink_on = self.deepthink_cb.isChecked()
        self.prompt.clear()
        attach = list(self.attachments); note = ""
        if attach:
            note = "\n\n[Attachments: " + ", ".join(Path(p).name for p in attach) + "]"
        self.log("You", prompt + note); self._store("user", prompt + note)
        self.attachments = []; self._update_attach_label()
        self.send_btn.setEnabled(False)
        self.worker = Worker(self.controller, prompt,
                             self.auto_approve.isChecked(),
                             attachments=attach,
                             format_mode=self.config.format_mode)
        self.worker.finished_text.connect(self.task_finished)
        self.worker.failed.connect(self.task_failed)
        self.worker.approval_needed.connect(self.ask_approval)
        self.worker.progress.connect(self.task_progress)
        self.worker.start()

    def ask_approval(self, description, worker):
        box = QMessageBox(self)
        box.setWindowTitle("Approve operation?"); box.setIcon(QMessageBox.Warning)
        box.setText("The agent wants to perform an operation.")
        box.setDetailedText(description)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        worker.approve(box.exec() == QMessageBox.Yes)

    def task_progress(self, message):
        self.log("progress", message); self._store("progress", message)

    def task_finished(self, result):
        self.log("agent", result); self._store("assistant", result)
        self.send_btn.setEnabled(True)
        self.refresh_files(); self.refresh_git_summary()
        if self.current_session_id:
            self._run_browser_action("current_url", on_done=self._capture_url)

    def task_failed(self, error):
        self.log("error", error); self._store("error", error)
        self.send_btn.setEnabled(True)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning(): self.worker.wait()
        if self._browser_action and self._browser_action.isRunning(): self._browser_action.wait()
        try: self.browser.close()
        except Exception: pass
        event.accept()


if __name__ == "__main__":
    configure_logging(debug=AgentConfig().debug)
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())