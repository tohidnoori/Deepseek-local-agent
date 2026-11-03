from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import MAX_SHELL_OUTPUT, SHELL_TIMEOUT
from .errors import ToolError
from .logging_setup import get_logger

log = get_logger("shell")


@dataclass
class ShellResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


# Read-only commands that never require approval.
SAFE_PREFIXES = (
    "dir", "ls", "type", "cat", "more", "echo", "pwd", "cd", "whoami",
    "where", "which", "findstr", "grep", "find",
    "git status", "git diff", "git log", "git show", "git branch",
    "git rev-parse", "git blame", "git ls-files",
    "python --version", "python -V", "py --version",
    "pip --version", "pip list", "pip freeze",
    "node --version", "npm --version", "npm list",
    "pytest --version", "ruff --version", "black --version",
    "mypy --version",
)

# Commands that are never allowed even with approval —
# they can wipe state outside the sandbox or hang the process.
FORBIDDEN_PATTERNS = [
    r"\bformat\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\brd\s+/s\s+/q\s+c:\\\\",
    r"\bdel\s+/[fq]\s+/s\s+/q\s+c:\\\\",
    r"\brm\s+-rf\s+/",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bdiskpart\b",
    r"\breg\s+delete\b",
    r"\bnet\s+user\b",
    r"\btaskkill\s+/im\b",
    r"\bschtasks\b",
    r"\bpowershell\s+-enc\b",
    r"curl\b.*\|\s*(ba)?sh",
    r"wget\b.*\|\s*(ba)?sh",
]


class ShellTools:
    """Runs a Windows command via cmd.exe inside the project root."""

    def __init__(self, cwd: Path, timeout: int = SHELL_TIMEOUT):
        self.cwd = Path(cwd).resolve()
        self.timeout = timeout

    def is_safe(self, command: str) -> bool:
        cmd = command.strip().lower()
        if not cmd:
            return False
        return any(cmd == p or cmd.startswith(p + " ") for p in SAFE_PREFIXES)

    def is_forbidden(self, command: str) -> tuple[bool, str]:
        for pat in FORBIDDEN_PATTERNS:
            if re.search(pat, command, re.IGNORECASE):
                return True, f"Blocked by safety policy (matches {pat!r})"
        return False, ""

    def run(self, command: str) -> ShellResult:
        if not command.strip():
            raise ToolError("empty command")
        blocked, reason = self.is_forbidden(command)
        if blocked:
            raise ToolError(reason)

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["GIT_PAGER"] = "cat"
        env["PAGER"] = "cat"

        kwargs: dict[str, Any] = {}
        if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        log.info("run: %s", command)
        try:
            p = subprocess.run(
                command,
                cwd=self.cwd,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                env=env,
                **kwargs,
            )
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"command timed out after {self.timeout}s") from e
        except OSError as e:
            raise ToolError(f"failed to spawn command: {e}") from e

        return ShellResult(
            ok=p.returncode == 0,
            stdout=(p.stdout or "")[-MAX_SHELL_OUTPUT:],
            stderr=(p.stderr or "")[-MAX_SHELL_OUTPUT:],
            returncode=p.returncode,
        )

    def tool_spec(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "run_command",
                "description": (
                    "Run a Windows shell command (cmd.exe) inside the project root. "
                    "Read-only commands run freely; anything else requires user approval. "
                    "Never run long-running servers."
                ),
                "args": {"command": "string"},
            }
        ]
