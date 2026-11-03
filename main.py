import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QLineEdit, QTextEdit, QFileDialog, QMessageBox, QSplitter, QListWidget,
    QListWidgetItem, QCheckBox, QDialog, QDialogButtonBox, QPlainTextEdit, QTabWidget,
    QSlider, QComboBox,
)

from agent.browser_client import DeepSeekBrowser
from agent.config import AgentConfig
from agent.controller import AgentController
from agent.git_tools import GitTools
from agent.logging_setup import configure_logging
from agent.sessions import SessionStore
from agent.shell_tools import ShellTools
from agent.tools import ProjectTools

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


def _make_controller(browser, tools, git, shell, config):
    return AgentController(browser, tools, git=git, shell=shell, config=config)


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
    """Runs a single browser action off the GUI thread."""
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
                url = self.browser.current_url()
                self.done.emit(ok, "Chat opened" if ok else "Could not open chat", url)
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

        layout.addWidget(QLabel("Status (porcelain)"))
        self.status_view = QTextEdit()
        self.status_view.setReadOnly(True)
        layout.addWidget(self.status_view, 1)

        layout.addWidget(QLabel("Diff (unstaged)"))
        self.diff_view = QTextEdit()
        self.diff_view.setReadOnly(True)
        layout.addWidget(self.diff_view, 2)

        msg_row = QHBoxLayout()
        self.msg = QLineEdit()
        self.msg.setPlaceholderText("Commit message")
        msg_row.addWidget(self.msg, 1)
        stage_btn = QPushButton("Stage all")
        stage_btn.clicked.connect(self.stage_all)
        msg_row.addWidget(stage_btn)
        commit_btn = QPushButton("Commit")
        commit_btn.clicked.connect(self.do_commit)
        msg_row.addWidget(commit_btn)
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
            self.git.stage()
            self.refresh()
        except Exception as e:
            QMessageBox.warning(self, "Git error", str(e))

    def do_commit(self):
        msg = self.msg.text().strip()
        if not msg:
            QMessageBox.warning(self, "Commit", "Enter a commit message first.")
            return
        try:
            self.git.commit(msg)
            self.msg.clear()
            self.refresh()
        except Exception as e:
            QMessageBox.warning(self, "Git error", str(e))


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
        self.git_tools: GitTools | None = None
        self.shell_tools: ShellTools | None = None
        self.project_tools: ProjectTools | None = None
        self.controller: AgentController | None = None
        self.worker: Worker | None = None
        self._browser_action: BrowserAction | None = None
        self.current_session_id: str | None = None
        self.attachments: list[str] = []

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        # ---------------- top bar ----------------
        top = QHBoxLayout()
        title = QLabel("DeepSeek Local Agent")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        top.addWidget(title)
        top.addStretch()
        self.status = QLabel("Browser not connected")
        self.status.setObjectName("muted")
        top.addWidget(self.status)
        self.connect_btn = QPushButton("Connect browser")
        self.connect_btn.clicked.connect(self.connect_browser)
        top.addWidget(self.connect_btn)
        self.signed_btn = QPushButton("I've signed in")
        self.signed_btn.setEnabled(False)
        self.signed_btn.clicked.connect(self.finish_login)
        top.addWidget(self.signed_btn)
        outer.addLayout(top)

        # ---------------- options ----------------
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Browser options:"))
        self.deepthink_cb = QCheckBox("DeepThink (R1)")
        self.deepthink_cb.toggled.connect(self.on_deepthink_toggled)
        opts.addWidget(self.deepthink_cb)
        self.newchat_btn = QPushButton("New chat")
        self.newchat_btn.clicked.connect(self.on_new_chat)
        self.newchat_btn.setEnabled(False)
        opts.addWidget(self.newchat_btn)
        opts.addStretch()
        outer.addLayout(opts)

        # ---------------- pacing + reply format ----------------
        pacing_row = QHBoxLayout()
        pacing_row.addWidget(QLabel("Speed ↔ Safety:"))
        self.safety_slider = QSlider(Qt.Horizontal)
        self.safety_slider.setRange(0, 100)
        self.safety_slider.setValue(self.config.safety_level)
        self.safety_slider.setFixedWidth(240)
        self.safety_slider.setToolTip("0 = fast (risky)   ·   100 = slow (safest)")
        self.safety_slider.valueChanged.connect(self.on_safety_changed)
        self.safety_slider.sliderReleased.connect(self._log_pacing)
        pacing_row.addWidget(self.safety_slider)

        self.safety_lbl = QLabel("50 — Balanced")
        self.safety_lbl.setObjectName("muted")
        pacing_row.addWidget(self.safety_lbl)

        pacing_row.addSpacing(20)
        pacing_row.addWidget(QLabel("Reply format:"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(["JSON", "XML", "Text"])
        self.format_combo.setCurrentText(self.config.format_mode.upper())
        self.format_combo.setToolTip(
            "JSON = tool-call block (default)\n"
            "XML  = same tool protocol, XML encoded\n"
            "Text = chat-only, no tools"
        )
        self.format_combo.currentTextChanged.connect(self.on_format_changed)
        pacing_row.addWidget(self.format_combo)
        pacing_row.addStretch()
        outer.addLayout(pacing_row)

        # ---------------- splitter ----------------
        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter, 1)

        # ===== LEFT: sessions & project =====
        left = QWidget()
        ll = QVBoxLayout(left)

        self.tabs = QTabWidget()
        ll.addWidget(self.tabs, 3)

        # --- Tab 1: my chats ---
        mychat_tab = QWidget()
        ml = QVBoxLayout(mychat_tab)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.addWidget(QLabel("My chats (saved in this app)"))
        self.saved_list = QListWidget()
        self.saved_list.itemDoubleClicked.connect(self.resume_saved_session)
        ml.addWidget(self.saved_list, 1)
        saved_btns = QHBoxLayout()
        b = QPushButton("New chat")
        b.clicked.connect(self.start_new_session)
        saved_btns.addWidget(b)
        b = QPushButton("Open")
        b.clicked.connect(lambda: self.resume_saved_session(self.saved_list.currentItem()))
        saved_btns.addWidget(b)
        b = QPushButton("Delete")
        b.clicked.connect(self.delete_saved_session)
        saved_btns.addWidget(b)
        ml.addLayout(saved_btns)
        self.tabs.addTab(mychat_tab, "My chats")

        # --- Tab 2: DeepSeek chats ---
        ds_tab = QWidget()
        dl = QVBoxLayout(ds_tab)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(QLabel("DeepSeek sidebar chats"))
        self.ds_list = QListWidget()
        self.ds_list.itemDoubleClicked.connect(self.open_deepseek_chat)
        dl.addWidget(self.ds_list, 1)
        ds_btns = QHBoxLayout()
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_deepseek_chats)
        ds_btns.addWidget(b)
        b = QPushButton("Open")
        b.clicked.connect(lambda: self.open_deepseek_chat(self.ds_list.currentItem()))
        ds_btns.addWidget(b)
        dl.addLayout(ds_btns)
        self.tabs.addTab(ds_tab, "DeepSeek chats")

        # --- Tab 3: project ---
        proj_tab = QWidget()
        pl = QVBoxLayout(proj_tab)
        pl.setContentsMargins(0, 0, 0, 0)
        self.project_label = QLabel("No project selected")
        self.project_label.setObjectName("muted")
        self.project_label.setWordWrap(True)
        pl.addWidget(self.project_label)
        choose = QPushButton("Choose project folder")
        choose.clicked.connect(self.choose_project)
        pl.addWidget(choose)

        self.git_summary = QLabel("Git: not a repository")
        self.git_summary.setObjectName("muted")
        self.git_summary.setWordWrap(True)
        pl.addWidget(self.git_summary)
        self.git_btn = QPushButton("Git…")
        self.git_btn.setEnabled(False)
        self.git_btn.clicked.connect(self.open_git_dialog)
        pl.addWidget(self.git_btn)

        pl.addWidget(QLabel("Project files"))
        self.file_list = QListWidget()
        self.file_list.itemDoubleClicked.connect(self.show_file)
        pl.addWidget(self.file_list, 1)

        self.auto_approve = QCheckBox("Auto-approve all operations (dangerous)")
        pl.addWidget(self.auto_approve)
        self.tabs.addTab(proj_tab, "Project")

        splitter.addWidget(left)

        # ===== RIGHT: conversation =====
        right = QWidget()
        rl = QVBoxLayout(right)
        self.session_label = QLabel("No chat selected")
        self.session_label.setObjectName("muted")
        rl.addWidget(self.session_label)
        self.chat = QTextEdit()
        self.chat.setReadOnly(True)
        self.chat.setPlaceholderText("Connect to DeepSeek, choose a project, then describe your task.")
        rl.addWidget(self.chat, 1)

        # ----- attachment row -----
        attach_row = QHBoxLayout()
        self.attach_btn = QPushButton("Attach files…")
        self.attach_btn.clicked.connect(self.choose_attachments)
        attach_row.addWidget(self.attach_btn)
        self.attach_lbl = QLabel("No files attached")
        self.attach_lbl.setObjectName("muted")
        attach_row.addWidget(self.attach_lbl, 1)
        clear_attach = QPushButton("Clear")
        clear_attach.clicked.connect(self.clear_attachments)
        attach_row.addWidget(clear_attach)
        rl.addLayout(attach_row)

        self.prompt = QTextEdit()
        self.prompt.setFixedHeight(105)
        self.prompt.setPlaceholderText("Describe what you want the agent to do...")
        rl.addWidget(self.prompt)
        buttons = QHBoxLayout()
        self.send_btn = QPushButton("Send task")
        self.send_btn.clicked.connect(self.send_task)
        buttons.addWidget(self.send_btn)
        clear = QPushButton("Clear view")
        clear.clicked.connect(self.chat.clear)
        buttons.addWidget(clear)
        rl.addLayout(buttons)
        splitter.addWidget(right)
        splitter.setSizes([340, 980])

        self.setStyleSheet(APP_STYLE)
        self.refresh_saved_sessions()
        self.on_safety_changed(self.safety_slider.value())

    # =================================================================== helpers
    def log(self, who, text):
        escaped = (
            text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        )
        self.chat.append(f"<b>{who}</b><br>{escaped}<br>")

    def _session(self):
        if not self.current_session_id:
            return None
        return self.sessions.get(self.current_session_id)

    def _store(self, role, text):
        if self.current_session_id:
            self.sessions.add_message(self.current_session_id, role, text)
            self.refresh_saved_sessions()

    # =================================================================== pacing handlers
    def on_safety_changed(self, value: int):
        self.config.safety_level = int(value)
        self.config.apply_safety_level()
        self.browser.set_safety_level(int(value))
        if value < 25:
            label = "Fast (risky)"
        elif value < 50:
            label = "Moderate"
        elif value < 75:
            label = "Balanced"
        else:
            label = "Safe (slow)"
        self.safety_lbl.setText(f"{value} — {label}")

    def _log_pacing(self):
        try:
            self.log(
                "system",
                f"Pacing: safety={self.config.safety_level}, "
                f"type_delay={self.browser.type_delay_min}-{self.browser.type_delay_max}ms, "
                f"pre_send={self.browser.pre_send_pause_min}-{self.browser.pre_send_pause_max}s, "
                f"rate_limit={self.browser.rate_limit_prompts}/{self.browser.rate_limit_window_s}s, "
                f"long_break_every={self.browser.long_break_every}",
            )
        except Exception:
            pass

    def on_format_changed(self, text: str):
        self.config.format_mode = text.lower()
        try:
            self.log("system", f"Reply format set to {text}.")
        except Exception:
            pass

    # =================================================================== sessions
    def refresh_saved_sessions(self):
        current = self.saved_list.currentItem() if self.saved_list.count() else None
        current_id = current.data(Qt.UserRole) if current else None
        self.saved_list.clear()
        rows = self.sessions.all()
        for s in rows:
            item = QListWidgetItem(f"{s.title}  ·  {s.project_name}")
            item.setData(Qt.UserRole, s.id)
            item.setToolTip(f"{s.project_root}\n{s.deepseek_url or '(new)'}")
            self.saved_list.addItem(item)
            if s.id == current_id:
                self.saved_list.setCurrentItem(item)

    def start_new_session(self):
        if not self.project_root:
            QMessageBox.information(self, "No project", "Choose a project folder first (Project tab).")
            return
        s = self.sessions.create(str(self.project_root), "New chat")
        self.current_session_id = s.id
        self.chat.clear()
        self.session_label.setText(f"Chat: {s.title}  ·  {s.project_name}")
        self.refresh_saved_sessions()
        self.log("system", "New chat started.")
        if self.browser.ready:
            self._run_browser_action("new_chat", on_done=self._after_new_chat)

    def _after_new_chat(self, ok, message, payload):
        if ok:
            self._run_browser_action("current_url", on_done=self._capture_url)

    def _capture_url(self, ok, message, url):
        if ok and url and self.current_session_id:
            self.sessions.set_deepseek_url(self.current_session_id, url)
            s = self._session()
            if s:
                self.session_label.setText(f"Chat: {s.title}  ·  {s.project_name}")

    def resume_saved_session(self, item):
        if item is None:
            return
        sid = item.data(Qt.UserRole)
        s = self.sessions.get(sid)
        if not s:
            return
        self.current_session_id = s.id
        try:
            self._set_project(Path(s.project_root))
        except Exception as e:
            self.log("Application error", f"Could not restore project: {e}")
        self.chat.clear()
        for m in s.messages:
            self.log(m.get("role", "?"), m.get("text", ""))
        self.session_label.setText(f"Chat: {s.title}  ·  {s.project_name}")
        if s.deepseek_url and self.browser.ready:
            self._run_browser_action("open_chat", s.deepseek_url, on_done=self._after_open_chat)

    def _after_open_chat(self, ok, message, url):
        if ok:
            self.log("system", f"Resumed DeepSeek chat at {url}")
        else:
            self.log("system", "Could not open the DeepSeek chat; sending will start a fresh one.")

    def delete_saved_session(self):
        item = self.saved_list.currentItem()
        if not item:
            return
        sid = item.data(Qt.UserRole)
        if QMessageBox.question(self, "Delete chat", "Delete this saved chat?", QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.sessions.delete(sid)
        if self.current_session_id == sid:
            self.current_session_id = None
            self.chat.clear()
            self.session_label.setText("No chat selected")
        self.refresh_saved_sessions()

    # =================================================================== deepseek chats
    def refresh_deepseek_chats(self):
        if not self.browser.ready:
            QMessageBox.warning(self, "Not connected", "Connect to DeepSeek and sign in first.")
            return
        self.ds_list.clear()
        self._run_browser_action("list_chats", on_done=self._fill_ds_list)

    def _fill_ds_list(self, ok, message, rows):
        if not ok or not rows:
            if not ok:
                QMessageBox.warning(self, "DeepSeek", message)
            return
        for row in rows:
            item = QListWidgetItem(row.get("title") or row.get("href", ""))
            item.setData(Qt.UserRole, row.get("href") or row.get("title"))
            item.setToolTip(row.get("href", ""))
            self.ds_list.addItem(item)

    def open_deepseek_chat(self, item):
        if item is None:
            return
        if not self.project_root:
            QMessageBox.information(self, "No project", "Choose a project folder first (Project tab).")
            return
        href = item.data(Qt.UserRole) or item.text()
        title = item.text() or "DeepSeek chat"
        s = self.sessions.create(str(self.project_root), title)
        self.sessions.set_deepseek_url(s.id, href)
        self.current_session_id = s.id
        self.chat.clear()
        self.session_label.setText(f"Chat: {s.title}  ·  {s.project_name}")
        self.refresh_saved_sessions()
        self._run_browser_action("open_chat", href, on_done=self._after_open_chat)

    # =================================================================== browser
    def connect_browser(self):
        try:
            self.status.setText("Opening Chrome...")
            self.browser.open_chrome()
            self.status.setText("Chrome opened — sign in, then click 'I've signed in'")
            self.signed_btn.setEnabled(True)
            self.connect_btn.setEnabled(False)
        except Exception as e:
            QMessageBox.critical(self, "Browser error", str(e))
            self.status.setText("Browser connection failed")

    def finish_login(self):
        self.browser.mark_ready()
        self.status.setText("Browser ready")
        self.signed_btn.setEnabled(False)
        self.newchat_btn.setEnabled(True)
        self._ensure_controller()
        self.refresh_deepseek_chats()
        QMessageBox.information(self, "Connected", "Browser session marked ready. You can start a task.")

    def _ensure_controller(self):
        if not (self.browser.ready and self.project_root):
            return
        self.project_tools = ProjectTools(self.project_root, self.config)
        self.shell_tools = ShellTools(self.project_root, timeout=self.config.shell_timeout)
        self.controller = _make_controller(
            self.browser, self.project_tools, self.git_tools, self.shell_tools, self.config
        )

    def _run_browser_action(self, action, payload=None, on_done=None):
        if self._browser_action and self._browser_action.isRunning():
            QMessageBox.information(self, "Busy", "A browser action is already running.")
            return
        self._browser_action = BrowserAction(self.browser, action, payload)
        if on_done is not None:
            self._browser_action.done.connect(on_done)
        self._browser_action.start()

    def on_deepthink_toggled(self, checked):
        self.browser._deepthink_on = bool(checked)
        if not self.browser.ready:
            return
        self._run_browser_action("set_deepthink", bool(checked))

    def on_new_chat(self):
        if not self.browser.ready:
            QMessageBox.warning(self, "Not connected", "Connect to DeepSeek and sign in first.")
            return
        if self.project_root:
            self.start_new_session()
        else:
            self._run_browser_action("new_chat")

    # =================================================================== project
    def choose_project(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose project folder")
        if not folder:
            return
        self._set_project(Path(folder))

    def _set_project(self, folder: Path):
        self.project_root = folder.resolve()
        self.project_label.setText(str(self.project_root))
        self.git_tools = GitTools(self.project_root)
        self.refresh_git_summary()
        self.refresh_files()
        self._ensure_controller()

    def refresh_git_summary(self):
        if not self.git_tools or not self.git_tools.is_repo():
            self.git_summary.setText("Git: not a repository")
            self.git_btn.setEnabled(False)
            return
        try:
            branch = self.git_tools.branch()
            status = self.git_tools.status()
        except Exception as e:
            self.git_summary.setText(f"Git error: {e}")
            self.git_btn.setEnabled(False)
            return
        dirty = sum(1 for line in status.splitlines() if line and not line.startswith("##"))
        self.git_summary.setText(f"Git: {branch} — {dirty} change(s)")
        self.git_btn.setEnabled(True)

    def open_git_dialog(self):
        if not self.git_tools or not self.git_tools.is_repo():
            return
        GitDialog(self.git_tools, self).exec()
        self.refresh_git_summary()

    def refresh_files(self):
        self.file_list.clear()
        if not self.project_root:
            return
        try:
            tools = ProjectTools(self.project_root, self.config)
            for rel in tools.list_files("."):
                self.file_list.addItem(rel)
        except Exception as e:
            self.log("Application error", f"Could not list files: {e}")

    def show_file(self, item):
        if not self.project_root:
            return
        try:
            tools = ProjectTools(self.project_root, self.config)
            content = tools.read_file(item.text())
        except Exception as e:
            QMessageBox.warning(self, "Cannot open file", str(e))
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(item.text())
        dlg.resize(850, 600)
        layout = QVBoxLayout(dlg)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(content)
        layout.addWidget(view)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)
        dlg.exec()

    # =================================================================== attachments
    def choose_attachments(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach files to send to DeepSeek", "",
            "All files (*.*);;Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;Documents (*.pdf *.docx *.doc *.txt *.md *.csv *.xlsx *.pptx)",
        )
        if not paths:
            return
        for p in paths:
            if p not in self.attachments:
                self.attachments.append(p)
        self._update_attach_label()

    def clear_attachments(self):
        self.attachments = []
        self._update_attach_label()

    def _update_attach_label(self):
        if not self.attachments:
            self.attach_lbl.setText("No files attached")
            return
        names = [Path(p).name for p in self.attachments]
        shown = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
        self.attach_lbl.setText(f"{len(names)} file(s): {shown}")

    # =================================================================== task
    def send_task(self):
        if not self.browser.ready:
            QMessageBox.warning(self, "Not connected", "Connect to DeepSeek and sign in first.")
            return
        if not self.project_root:
            QMessageBox.warning(self, "No project", "Choose a project folder first.")
            return
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Busy", "A task is already running.")
            return
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            return
        if not self.current_session_id:
            self.start_new_session()
            if not self.current_session_id:
                return
        self._ensure_controller()
        if not self.controller:
            QMessageBox.warning(self, "Not ready", "Controller could not be created.")
            return

        self.browser._deepthink_on = self.deepthink_cb.isChecked()
        self.prompt.clear()
        attach = list(self.attachments)
        attach_note = ""
        if attach:
            attach_note = "\n\n[Attachments: " + ", ".join(Path(p).name for p in attach) + "]"
        self.log("You", prompt + attach_note)
        self._store("user", prompt + attach_note)
        self.attachments = []
        self._update_attach_label()
        self.send_btn.setEnabled(False)
        self.worker = Worker(
            self.controller, prompt,
            self.auto_approve.isChecked(),
            attachments=attach,
            format_mode=self.config.format_mode,
        )
        self.worker.finished_text.connect(self.task_finished)
        self.worker.failed.connect(self.task_failed)
        self.worker.approval_needed.connect(self.ask_approval)
        self.worker.progress.connect(self.task_progress)
        self.worker.start()

    def ask_approval(self, description, worker):
        box = QMessageBox(self)
        box.setWindowTitle("Approve operation?")
        box.setIcon(QMessageBox.Warning)
        box.setText("The agent wants to perform an operation.")
        box.setDetailedText(description)
        box.setInformativeText("Only approve if you understand and trust this operation.")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        worker.approve(box.exec() == QMessageBox.Yes)

    def task_progress(self, message):
        self.log("progress", message)
        self._store("progress", message)

    def task_finished(self, result):
        self.log("DeepSeek agent", result)
        self._store("assistant", result)
        self.send_btn.setEnabled(True)
        self.refresh_files()
        self.refresh_git_summary()
        if self.current_session_id:
            self._run_browser_action("current_url", on_done=self._capture_url)

    def task_failed(self, error):
        self.log("Application error", error)
        self._store("error", error)
        self.send_btn.setEnabled(True)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.wait()
        if self._browser_action and self._browser_action.isRunning():
            self._browser_action.wait()
        try:
            self.browser.close()
        except Exception:
            pass
        event.accept()


if __name__ == "__main__":
    configure_logging(debug=AgentConfig().debug)
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())