from __future__ import annotations

import difflib
from dataclasses import dataclass

from .file_ops_model import CommandOperation, FileOperation, OpType
from .shell_tools import ShellTools
from .tools import ProjectTools


@dataclass
class Preview:
    op: FileOperation
    exists: bool
    diff: str
    allowed: bool
    reason: str = ""


class FileOperationApplier:
    def __init__(self, tools: ProjectTools):
        self.tools = tools

    def preview(self, op: FileOperation) -> Preview:
        try:
            target = self.tools._path(op.path)
        except Exception as e:
            return Preview(op, False, "", False, f"Path rejected: {e}")

        exists = target.is_file()

        if op.op is OpType.DELETE:
            return Preview(op, exists, "", exists,
                           "" if exists else "File does not exist.")
        if op.op is OpType.MOVE:
            if not exists:
                return Preview(op, False, "", False, "Source does not exist.")
            if not op.destination:
                return Preview(op, True, "", False, "No destination specified.")
            return Preview(op, True, "", True, "")
        if op.op is OpType.CREATE and exists:
            return Preview(op, True, "", False,
                           "File already exists; use REPLACE to overwrite.")

        content = op.content or ""
        if exists:
            old = target.read_text(encoding="utf-8", errors="replace")
            diff = "".join(difflib.unified_diff(
                old.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"before/{op.path}", tofile=f"after/{op.path}",
            ))
            return Preview(op, True, diff, True, "")
        diff = "".join(difflib.unified_diff(
            [], content.splitlines(keepends=True),
            fromfile="/dev/null", tofile=f"after/{op.path}",
        ))
        return Preview(op, False, diff, True,
                       "File does not exist yet — will be created.")

    def apply(self, op: FileOperation, confirm_overwrite: bool = False) -> str:
        pv = self.preview(op)
        if not pv.allowed:
            raise ValueError(pv.reason)
        target = self.tools._path(op.path)
        exists = target.is_file()

        if op.op is OpType.DELETE:
            self.tools._backup(target)
            target.unlink()
            return f"Deleted {op.path}"
        if op.op is OpType.MOVE:
            dst = self.tools._path(op.destination)
            if dst.exists():
                raise FileExistsError(f"Destination exists: {op.destination}")
            self.tools._backup(target)
            dst.parent.mkdir(parents=True, exist_ok=True)
            target.rename(dst)
            return f"Moved {op.path} → {op.destination}"

        if op.op is OpType.CREATE and exists:
            raise FileExistsError("Refusing to overwrite an existing file.")
        if exists and op.op is OpType.REPLACE and not confirm_overwrite:
            raise FileExistsError("Overwrite confirmation required.")

        self.tools._backup(target)

        if op.op is OpType.APPEND and exists:
            old = target.read_text(encoding="utf-8", errors="replace")
            self.tools._atomic_write(target, old + (op.content or ""))
            return f"Appended to {op.path}"

        self.tools._atomic_write(target, op.content or "")
        return f"Created {op.path}" if not exists else f"Replaced {op.path}"


class CommandApplier:
    """Runs extracted shell commands inside the project root via ShellTools."""

    def __init__(self, shell_tools: ShellTools):
        self.shell = shell_tools

    def preview(self, cmd: CommandOperation) -> dict:
        blocked, reason = self.shell.is_forbidden(cmd.command)
        return {
            "command": cmd.command,
            "blocked": blocked,
            "reason": reason,
            "safe": self.shell.is_safe(cmd.command),
            "purpose": cmd.purpose,
        }

    def apply(self, cmd: CommandOperation) -> dict:
        blocked, reason = self.shell.is_forbidden(cmd.command)
        if blocked:
            raise RuntimeError(reason)
        result = self.shell.run(cmd.command)
        return {
            "ok": result.ok,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
