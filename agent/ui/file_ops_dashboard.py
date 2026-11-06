from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from ..file_ops_applier import CommandApplier, FileOperationApplier
from ..file_ops_model import CommandOperation, FileOperation, OpType, OperationSet


class _CmdRunner(QThread):
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, applier: CommandApplier, cmd: CommandOperation):
        super().__init__()
        self.applier = applier
        self.cmd = cmd

    def run(self):
        try:
            self.done.emit(self.applier.apply(self.cmd))
        except Exception as e:
            self.failed.emit(str(e))


class FileOpsDashboard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.ops: OperationSet | None = None
        self.file_applier: FileOperationApplier | None = None
        self.cmd_applier: CommandApplier | None = None
        self._selected_file: FileOperation | None = None
        self._selected_cmd: CommandOperation | None = None
        self._cmd_worker: _CmdRunner | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        header.addWidget(QLabel("Detected operations"))
        header.addStretch()
        self.summary = QLabel("0 files · 0 commands")
        self.summary.setObjectName("muted")
        header.addWidget(self.summary)
        lay.addLayout(header)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)

        # ----- Files tab -------------------------------------------------
        files_tab = QWidget()
        fl = QVBoxLayout(files_tab)
        fl.setContentsMargins(0, 6, 0, 0)

        self.file_list = QListWidget()
        self.file_list.currentItemChanged.connect(self._on_file_selected)
        fl.addWidget(self.file_list, 1)

        self.file_preview = QTextEdit()
        self.file_preview.setReadOnly(True)
        fl.addWidget(self.file_preview, 2)

        file_buttons = QHBoxLayout()
        self.overwrite_cb = QCheckBox("Confirm overwrite")
        self.overwrite_cb.setChecked(True)
        file_buttons.addWidget(self.overwrite_cb)
        file_buttons.addStretch()
        copy_btn = QPushButton("Copy content")
        copy_btn.clicked.connect(self._copy_file)
        file_buttons.addWidget(copy_btn)
        skip_btn = QPushButton("Skip")
        skip_btn.clicked.connect(self._skip_file)
        file_buttons.addWidget(skip_btn)
        apply_btn = QPushButton("Apply selected")
        apply_btn.clicked.connect(self._apply_file_selected)
        file_buttons.addWidget(apply_btn)
        apply_all = QPushButton("Apply all")
        apply_all.clicked.connect(self._apply_files_all)
        file_buttons.addWidget(apply_all)
        fl.addLayout(file_buttons)
        self.tabs.addTab(files_tab, "Files (0)")

        # ----- Commands tab ----------------------------------------------
        cmd_tab = QWidget()
        cl = QVBoxLayout(cmd_tab)
        cl.setContentsMargins(0, 6, 0, 0)

        self.cmd_list = QListWidget()
        self.cmd_list.currentItemChanged.connect(self._on_cmd_selected)
        cl.addWidget(self.cmd_list, 1)

        self.cmd_preview = QPlainTextEdit()
        self.cmd_preview.setReadOnly(True)
        cl.addWidget(self.cmd_preview, 2)

        cmd_buttons = QHBoxLayout()
        cmd_buttons.addStretch()
        copy_cmd = QPushButton("Copy command")
        copy_cmd.clicked.connect(self._copy_command)
        cmd_buttons.addWidget(copy_cmd)
        skip_cmd = QPushButton("Skip")
        skip_cmd.clicked.connect(self._skip_command)
        cmd_buttons.addWidget(skip_cmd)
        run_btn = QPushButton("Run command")
        run_btn.clicked.connect(self._run_command)
        cmd_buttons.addWidget(run_btn)
        cl.addLayout(cmd_buttons)
        self.tabs.addTab(cmd_tab, "Commands (0)")

    # ── Wiring ────────────────────────────────────────────────────────
    def set_applier(self, file_applier=None, shell_tools=None) -> None:
        if file_applier is not None:
            self.file_applier = file_applier
        if shell_tools is not None:
            self.cmd_applier = CommandApplier(shell_tools)

    def load_operations(self, ops: OperationSet) -> None:
        self.ops = ops
        self.file_list.clear()
        self.cmd_list.clear()
        for op in ops.operations:
            item = QListWidgetItem(f"{op.op.value:8s} {op.path}")
            item.setData(Qt.UserRole, op)
            self.file_list.addItem(item)
        for cmd in ops.commands:
            preview = cmd.command.splitlines()[0] if cmd.command else ""
            item = QListWidgetItem(f"CMD  {preview[:80]}")
            item.setData(Qt.UserRole, cmd)
            self.cmd_list.addItem(item)

        nf = len(ops.operations)
        nc = len(ops.commands)
        self.summary.setText(f"{nf} files · {nc} commands")
        self.tabs.setTabText(0, f"Files ({nf})")
        self.tabs.setTabText(1, f"Commands ({nc})")

        self.file_preview.clear()
        self.cmd_preview.clear()
        if ops.errors:
            self.file_preview.setPlainText(
                "Extraction notes:\n\n" + "\n".join(f"• {e}" for e in ops.errors)
            )

    # ── Files ─────────────────────────────────────────────────────────
    def _on_file_selected(self, cur, _prev) -> None:
        if cur is None:
            return
        op = cur.data(Qt.UserRole)
        if not isinstance(op, FileOperation):
            return
        self._selected_file = op
        if self.file_applier is None:
            self.file_preview.setPlainText("No project selected.")
            return
        pv = self.file_applier.preview(op)
        text = f"{op.op.value}  {op.path}\n"
        if op.destination:
            text += f"→ {op.destination}\n"
        text += f"Exists: {'yes' if pv.exists else 'no'}\n"
        if not pv.allowed:
            text += f"\n⛔ {pv.reason}\n"
        elif pv.reason:
            text += f"\nℹ {pv.reason}\n"
        if pv.diff:
            text += f"\n--- Diff ---\n{pv.diff[:12000]}\n"
        elif op.content:
            text += f"\n--- Content (first 2000) ---\n{op.content[:2000]}\n"
        self.file_preview.setPlainText(text)

    def _copy_file(self) -> None:
        if self._selected_file and self._selected_file.content:
            QApplication.clipboard().setText(self._selected_file.content)

    def _skip_file(self) -> None:
        row = self.file_list.currentRow()
        if row >= 0:
            self.file_list.takeItem(row)
            self._selected_file = None

    def _apply_file_selected(self) -> None:
        if not self._selected_file or not self.file_applier:
            return
        self._apply_one_file(self._selected_file)

    def _apply_files_all(self) -> None:
        if not self.ops or not self.file_applier:
            return
        ok = err = 0
        for op in list(self.ops.operations):
            try:
                self._apply_one_file(op, silent=True)
                ok += 1
            except Exception:
                err += 1
        QMessageBox.information(self, "Apply all",
                                f"Applied {ok} file(s). {err} skipped/failed.")

    def _apply_one_file(self, op: FileOperation, silent: bool = False) -> None:
        pv = self.file_applier.preview(op)
        if not pv.allowed:
            if not silent:
                QMessageBox.warning(self, "Blocked", pv.reason)
            return
        if pv.exists and self.overwrite_cb.isChecked():
            a = QMessageBox.question(
                self, "Overwrite?",
                f"{op.path} already exists. Overwrite?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if a != QMessageBox.Yes:
                return
        try:
            msg = self.file_applier.apply(op, confirm_overwrite=True)
            if not silent:
                QMessageBox.information(self, "Applied", msg)
        except Exception as e:
            if not silent:
                QMessageBox.critical(self, "Error", str(e))
            raise

    # ── Commands ──────────────────────────────────────────────────────
    def _on_cmd_selected(self, cur, _prev) -> None:
        if cur is None:
            return
        cmd = cur.data(Qt.UserRole)
        if not isinstance(cmd, CommandOperation):
            return
        self._selected_cmd = cmd
        if self.cmd_applier is None:
            self.cmd_preview.setPlainText(
                "Command runner requires a project.\n\n"
                + cmd.command
            )
            return
        pv = self.cmd_applier.preview(cmd)
        lines = [f"$ {cmd.command}", ""]
        if pv["purpose"]:
            lines.append(f"Purpose: {pv['purpose']}")
        if pv["blocked"]:
            lines.append(f"⛔ BLOCKED: {pv['reason']}")
        elif pv["safe"]:
            lines.append("✓ Read-only — safe to run")
        else:
            lines.append("⚠ Will require confirmation before running")
        self.cmd_preview.setPlainText("\n".join(lines))

    def _copy_command(self) -> None:
        if self._selected_cmd:
            QApplication.clipboard().setText(self._selected_cmd.command)

    def _skip_command(self) -> None:
        row = self.cmd_list.currentRow()
        if row >= 0:
            self.cmd_list.takeItem(row)
            self._selected_cmd = None

    def _run_command(self) -> None:
        if not self._selected_cmd or not self.cmd_applier:
            QMessageBox.warning(self, "No command",
                                "Select a command first.")
            return
        pv = self.cmd_applier.preview(self._selected_cmd)
        if pv["blocked"]:
            QMessageBox.warning(self, "Blocked", pv["reason"])
            return
        if not pv["safe"]:
            answer = QMessageBox.question(
                self, "Run command?",
                f"This will run:\n\n{self._selected_cmd.command}\n\n"
                "in your project root. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self._cmd_worker = _CmdRunner(self.cmd_applier, self._selected_cmd)
        self._cmd_worker.done.connect(self._on_cmd_done)
        self._cmd_worker.failed.connect(
            lambda msg: QMessageBox.critical(self, "Command failed", msg)
        )
        self._cmd_worker.start()

    def _on_cmd_done(self, result: dict) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Command finished")
        dlg.resize(720, 480)
        v = QVBoxLayout(dlg)
        status = QLabel(
            f"Return code: {result['returncode']} — "
            f"{'OK' if result['ok'] else 'FAILED'}"
        )
        v.addWidget(status)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        out = result.get("stdout", "") or ""
        err = result.get("stderr", "") or ""
        view.setPlainText(out + ("\n--- stderr ---\n" + err if err else ""))
        v.addWidget(view, 1)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)
        dlg.exec()