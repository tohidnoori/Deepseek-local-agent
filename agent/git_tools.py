from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ToolError
from .logging_setup import get_logger

log = get_logger("git")


AGENT_TOOLS = {
    "git_status", "git_diff", "git_log", "git_stage", "git_commit",
}


@dataclass
class GitResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


class GitTools:
    def __init__(self, repo_root: Path, timeout: int = 60):
        self.repo_root = Path(repo_root).resolve()
        self.timeout = timeout

    def _run(self, *args: str, check: bool = False) -> GitResult:
        env = os.environ.copy()
        env["GIT_PAGER"] = "cat"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            p = subprocess.run(
                ["git", "-c", "core.quotepath=false", *args],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=env,
                stdin=subprocess.DEVNULL,
                **kwargs,
            )
        except FileNotFoundError as e:
            raise ToolError("git executable not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"git {args[0]} timed out after {self.timeout}s") from e
        result = GitResult(p.returncode == 0, p.stdout, p.stderr, p.returncode)
        if check and not result.ok:
            raise ToolError(result.stderr.strip() or f"git {args[0]} failed")
        return result

    # ------------------------------------------------------------------ queries
    def is_repo(self) -> bool:
        try:
            return self._run("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
        except ToolError:
            return False

    def branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD", check=True).stdout.strip()

    def status(self) -> str:
        return self._run("status", "--porcelain=v1", "-b", check=True).stdout

    def diff(self, staged: bool = False, path: str | None = None) -> str:
        args = ["diff"]
        if staged:
            args.append("--staged")
        if path:
            args += ["--", path]
        return self._run(*args, check=True).stdout

    def log(self, n: int = 20) -> str:
        n = max(1, min(int(n), 500))
        return self._run("log", f"-n{n}", "--oneline", check=True).stdout

    # ------------------------------------------------------------------ mutations
    def stage(self, *paths: str) -> None:
        if not paths:
            self._run("add", "-A", check=True)
        else:
            self._run("add", "--", *paths, check=True)

    def unstage(self, *paths: str) -> None:
        paths = paths or (".",)
        self._run("restore", "--staged", "--", *paths, check=True)

    def commit(self, message: str) -> str:
        if not message.strip():
            raise ToolError("empty commit message")
        return self._run("commit", "-m", message, check=True).stdout

    def pull_ff_only(self) -> str:
        return self._run("pull", "--ff-only", check=True).stdout

    def push(self) -> str:
        return self._run("push", check=True).stdout

    # ------------------------------------------------------------------ spec
    def tool_spec(self) -> list[dict[str, Any]]:
        return [
            {"name": "git_status", "description": "Show working tree status (porcelain).", "args": {}},
            {"name": "git_diff", "description": "Show diff of changes.",
             "args": {"staged": "bool (optional)", "path": "string (optional)"}},
            {"name": "git_log", "description": "Show recent commits oneline.",
             "args": {"n": "int (optional, default 20)"}},
            {"name": "git_stage", "description": "Stage files, or everything when paths is empty.",
             "args": {"paths": "list[string] (optional)"}},
            {"name": "git_commit", "description": "Commit staged changes.", "args": {"message": "string"}},
        ]

    def dispatch(self, name: str, **kwargs) -> str:
        if name not in AGENT_TOOLS:
            raise ToolError(f"git tool not permitted: {name}")
        if name == "git_status":
            return self.status()
        if name == "git_diff":
            return self.diff(bool(kwargs.get("staged", False)), kwargs.get("path"))
        if name == "git_log":
            return self.log(int(kwargs.get("n", 20)))
        if name == "git_stage":
            paths = kwargs.get("paths") or []
            if not isinstance(paths, list):
                raise ToolError("paths must be a list of strings")
            self.stage(*paths)
            return "staged"
        if name == "git_commit":
            return self.commit(str(kwargs["message"]))
        raise AssertionError(name)
