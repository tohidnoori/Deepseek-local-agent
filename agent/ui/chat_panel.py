# agent/ui/chat_panel.py
from __future__ import annotations

import html
import re
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..browser_client import DeepSeekBrowser
from ..chat_session import ChatSessionStore
from ..file_ops_extractor import find_file_name_near

_FENCE = re.compile(r"```(?P<lang>\w*)\n(?P<body>.*?)\n```", re.DOTALL)


# ── Off-thread send ────────────────────────────────────────────────────────
class _SendWorker(QThread):
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, browser, prompt: str):
        super().__init__()
        self.browser = browser
        self.prompt = prompt

    def run(self) -> None:
        try:
            self.browser.attach()
            reply = self.browser.send_and_wait(self.prompt)
            self.done.emit(reply)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")
        finally:
            try:
                self.browser.close()
            except Exception:
                pass


# ── Panel ──────────────────────────────────────────────────────────────────
class ChatPanel(QWidget):
    response_received = Signal(str, str)
    import_requested = Signal()
    scan_requested = Signal()

    def __init__(self, store: ChatSessionStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.current_session_id: str | None = None
        self.browser: DeepSeekBrowser | None = None
        self._worker: _SendWorker | None = None
        self._pending_session_id: str | None = None

        # id -> (path, code) for every rendered code block
        self._code_blocks: list[tuple[str | None, str]] = []

        lay = QVBoxLayout(self)

        # ---- header row --------------------------------------------------
        top = QHBoxLayout()
        top.addWidget(QLabel("Chat:"))
        self.session_combo = QComboBox()
        self.session_combo.currentIndexChanged.connect(self._on_session_selected)
        top.addWidget(self.session_combo, 1)

        new_btn = QPushButton("New")
        new_btn.clicked.connect(self._new_session)
        top.addWidget(new_btn)

        scan_btn = QPushButton("Scan current chat")
        scan_btn.clicked.connect(self.scan_requested.emit)
        top.addWidget(scan_btn)

        imp_btn = QPushButton("Import…")
        imp_btn.clicked.connect(self.import_requested.emit)
        top.addWidget(imp_btn)
        lay.addLayout(top)

        # ---- conversation view ------------------------------------------
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)                # we handle clicks ourselves
        self.view.setOpenExternalLinks(False)
        self.view.anchorClicked.connect(self._on_anchor_clicked)
        lay.addWidget(self.view, 1)

        # ---- input -------------------------------------------------------
        self.input = QTextEdit()
        self.input.setFixedHeight(90)
        self.input.setPlaceholderText(
            "Ask DeepSeek anything — file instructions are detected automatically…"
        )
        lay.addWidget(self.input)

        row = QHBoxLayout()
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._send)
        row.addWidget(self.send_btn)
        row.addStretch()
        lay.addLayout(row)

        self.refresh_sessions()

    # ==================================================================
    # Public API (used by MainWindow)
    # ==================================================================
    def set_browser(self, browser: DeepSeekBrowser | None) -> None:
        self.browser = browser

    def refresh_sessions(self) -> None:
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        for s in self.store.all():
            self.session_combo.addItem(s.title, s.id)

        if self.current_session_id:
            idx = self.session_combo.findData(self.current_session_id)
            if idx >= 0:
                self.session_combo.setCurrentIndex(idx)
        elif self.session_combo.count() == 0:
            s = self.store.create("New chat")
            self.session_combo.addItem(s.title, s.id)
            self.current_session_id = s.id
        else:
            self.current_session_id = self.session_combo.itemData(0)

        self.session_combo.blockSignals(False)

    def load_session(self, sid: str) -> None:
        self.current_session_id = sid
        self._code_blocks.clear()
        s = self.store.get(sid)
        self.view.clear()
        if s:
            for m in s.messages:
                self._append_message(m["role"], m["content"])
        self.refresh_sessions()

    # ==================================================================
    # Slots
    # ==================================================================
    def _on_session_selected(self, index: int) -> None:
        if index < 0:
            return
        sid = self.session_combo.itemData(index)
        if sid and sid != self.current_session_id:
            self.load_session(sid)

    def _new_session(self) -> None:
        s = self.store.create("New chat")
        self.load_session(s.id)

    # ==================================================================
    # Send
    # ==================================================================
    def _send(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        if not self.current_session_id:
            self._new_session()
        s = self.store.get(self.current_session_id)
        if not s:
            QMessageBox.warning(self, "Chat", "No active chat session.")
            return

        s.add("user", text)
        self._append_message("user", text)
        self.input.clear()

        if self.browser and self.browser.ready:
            self._start_browser_send(s.id, text)
        else:
            self._append_message(
                "system",
                "(Browser not connected — reply not generated. "
                "Connect the browser in Agent Mode, or use Import… to paste "
                "an existing conversation.)",
            )

    def _start_browser_send(self, session_id: str, prompt: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "Busy", "A reply is already in progress.")
            return
        self._pending_session_id = session_id
        self.send_btn.setEnabled(False)
        self._worker = _SendWorker(self.browser, prompt)
        self._worker.done.connect(self._on_send_done)
        self._worker.failed.connect(self._on_send_failed)
        self._worker.start()

    def _on_send_done(self, reply: str) -> None:
        self.send_btn.setEnabled(True)
        sid = self._pending_session_id
        self._pending_session_id = None
        if not sid:
            return
        s = self.store.get(sid)
        if not s:
            return
        s.add("assistant", reply)
        self._append_message("assistant", reply)
        self.response_received.emit(sid, reply)

    def _on_send_failed(self, err: str) -> None:
        self.send_btn.setEnabled(True)
        self._pending_session_id = None
        self._append_message("system", f"[Browser error] {err}")

    # ==================================================================
    # Rendering
    # ==================================================================
    def _append_message(self, role: str, text: str) -> None:
        colour = {
            "user": "#60a5fa",
            "assistant": "#34d399",
            "system": "#94a3b8",
        }.get(role, "#e5e7eb")

        self.view.append(f'<b style="color:{colour}">{role.capitalize()}</b>')

        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._render_markdown(cursor, text)
        self.view.append("")

    def _render_markdown(self, cursor: QTextCursor, text: str) -> None:
        pos = 0
        for m in _FENCE.finditer(text):
            if m.start() > pos:
                cursor.insertText(text[pos:m.start()])
            body = m.group("body")
            filename = find_file_name_near(text, m.start(), body)
            self._insert_code_block(cursor, filename, body, m.group("lang"))
            pos = m.end()
        if pos < len(text):
            cursor.insertText(text[pos:])

    def _insert_code_block(self, cursor: QTextCursor,
                           filename: str | None, code: str, lang: str) -> None:
        idx = len(self._code_blocks)
        self._code_blocks.append((filename, code))

        # ── header with file name + copy/download links ──
        display_name = html.escape(filename) if filename else f"code ({lang or 'text'})"
        header_html = (
            f'<div style="background:#1a2332; padding:5px 8px; margin-top:6px;">'
            f'<span style="color:#94a3b8; font-family:Consolas; font-size:11px;">'
            f'📄 {display_name}'
            f'</span>'
            f'&nbsp;&nbsp;'
            f'<a href="copy:{idx}" style="color:#60a5fa; text-decoration:none;">'
            f'📋 Copy</a>'
            f'&nbsp;'
            f'<a href="download:{idx}" style="color:#34d399; text-decoration:none;">'
            f'⬇ Download</a>'
            f'</div>'
        )
        cursor.insertHtml(header_html)

        # ── code body ──
        fmt = QTextCharFormat()
        fmt.setFontFamily("Consolas")
        fmt.setFontPointSize(10)
        fmt.setBackground(QColor("#0b1220"))
        fmt.setForeground(QColor("#e5e7eb"))
        cursor.insertText("\n" + code + "\n", fmt)
        cursor.insertBlock()

    # ==================================================================
    # Copy / Download handling
    # ==================================================================
    def _on_anchor_clicked(self, url: QUrl) -> None:
        s = url.toString()
        if ":" not in s:
            return
        action, idx_str = s.split(":", 1)
        try:
            idx = int(idx_str)
        except ValueError:
            return
        if idx < 0 or idx >= len(self._code_blocks):
            return
        filename, code = self._code_blocks[idx]

        if action == "copy":
            QApplication.clipboard().setText(code)

        elif action == "download":
            suggested = filename or "snippet.txt"
            # Give it a sane default directory
            path, _ = QFileDialog.getSaveFileName(
                self, "Save code", suggested,
                "All files (*.*)",
            )
            if not path:
                return
            try:
                Path(path).write_text(code, encoding="utf-8")
            except Exception as e:
                QMessageBox.warning(self, "Could not save", str(e))