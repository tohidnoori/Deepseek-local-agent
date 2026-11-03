from __future__ import annotations

import fnmatch
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import (
    AgentConfig,
    MAX_FILE_BYTES,
    MAX_LIST_RESULTS,
    MAX_SEARCH_FILE_BYTES,
    MAX_SEARCH_HITS,
    MAX_WRITE_BYTES,
    SENSITIVE_NAME_PARTS,
)
from .errors import PathViolation, ToolError


@dataclass
class Edit:
    old: str
    new: str


def _looks_sensitive(path: Path) -> bool:
    name = path.name.lower()
    if name in SENSITIVE_NAME_PARTS:
        return True
    for part in SENSITIVE_NAME_PARTS:
        if part in name and not name.endswith(part + ".example"):
            return True
    return False


class ProjectTools:
    """Filesystem sandbox scoped to a single project root.

    All path access is resolved and verified against the root. Ignored
    directories (VCS, virtualenvs, node_modules, build outputs, agent
    backups) and sensitive filenames are blocked for reads and writes.
    """

    def __init__(self, root: Path | str, config: AgentConfig | None = None):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError("Project root is not a directory.")
        self.config = config or AgentConfig()
        self.backup_dir = self.root / ".deepseek_agent_backups"
        self.ignored = set(self.config.ignored_dirs)

    # ------------------------------------------------------------------ paths
    def _path(self, relative: str | Path) -> Path:
        rel = str(relative).strip().replace("\\", "/")
        if rel in ("", "."):
            return self.root
        p = (self.root / rel).resolve()
        try:
            rel_parts = p.relative_to(self.root).parts
        except ValueError:
            raise PathViolation(f"Path escapes the project root: {relative!r}")
        if any(part in self.ignored for part in rel_parts):
            raise PathViolation(f"Access to ignored directory blocked: {relative!r}")
        if p.is_file() and _looks_sensitive(p):
            raise PathViolation(f"Access to sensitive file blocked: {relative!r}")
        return p

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.root)).replace("\\", "/")

    def _is_hidden(self, p: Path) -> bool:
        parts = p.relative_to(self.root).parts
        return any(part in self.ignored for part in parts) or _looks_sensitive(p)

    # ------------------------------------------------------------------ listing
    def list_files(self, path: str = ".", pattern: str | None = None) -> list[str]:
        base = self._path(path)
        if not base.exists():
            raise ToolError(f"Path does not exist: {path}")
        if base.is_file():
            return [self.rel(base)]
        results: list[str] = []
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if self._is_hidden(p):
                continue
            if pattern and not fnmatch.fnmatch(self.rel(p), pattern):
                continue
            results.append(self.rel(p))
            if len(results) >= MAX_LIST_RESULTS:
                break
        results.sort()
        return results

    def tree(self, path: str = ".", max_depth: int = 3) -> str:
        """Render a compact directory tree."""
        base = self._path(path)
        base_depth = len(base.relative_to(self.root).parts)
        lines: list[str] = []
        for p in sorted(base.rglob("*")):
            rel_parts = p.relative_to(self.root).parts
            depth = len(rel_parts) - base_depth
            if depth > max_depth:
                continue
            if any(part in self.ignored for part in rel_parts):
                continue
            if p.is_file() and _looks_sensitive(p):
                continue
            indent = "  " * (depth - 1)
            marker = "/" if p.is_dir() else ""
            lines.append(f"{indent}{p.name}{marker}")
            if len(lines) >= MAX_LIST_RESULTS:
                lines.append("... (truncated)")
                break
        return "\n".join(lines)

    # ------------------------------------------------------------------ read
    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        p = self._path(path)
        if not p.is_file():
            raise ToolError(f"File does not exist: {path}")
        size = p.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ToolError(f"File too large ({size} bytes > {MAX_FILE_BYTES}).")
        text = p.read_text(encoding="utf-8", errors="replace")
        if start <= 1 and end is None:
            return text
        lines = text.splitlines(keepends=True)
        s = max(1, int(start)) - 1
        e = len(lines) if end is None else max(s, int(end))
        header = f"# {self.rel(p)} lines {s + 1}-{e} of {len(lines)}\n"
        return header + "".join(lines[s:e])

    # ------------------------------------------------------------------ search
    def search_files(
        self,
        query: str,
        path: str = ".",
        regex: bool = False,
        ignore_case: bool = True,
        glob: str | None = None,
    ) -> list[dict[str, Any]]:
        if not query:
            raise ToolError("Search query is empty.")
        base = self._path(path)
        pat = None
        if regex:
            flags = re.IGNORECASE if ignore_case else 0
            try:
                pat = re.compile(query, flags)
            except re.error as e:
                raise ToolError(f"Invalid regex: {e}") from e
        needle = query.lower() if ignore_case else query

        hits: list[dict[str, Any]] = []
        files = [base] if base.is_file() else [
            self.root / rel for rel in self.list_files(self.rel(base))
        ]
        for p in files:
            try:
                if p.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
            except OSError:
                continue
            rel = self.rel(p)
            if glob and not fnmatch.fnmatch(rel, glob):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if pat is not None:
                    matched = bool(pat.search(line))
                else:
                    hay = line.lower() if ignore_case else line
                    matched = needle in hay
                if matched:
                    hits.append({"path": rel, "line": n, "text": line[:300]})
                    if len(hits) >= MAX_SEARCH_HITS:
                        return hits
        return hits

    # ------------------------------------------------------------------ write
    def _backup(self, p: Path) -> None:
        if not p.exists() or not p.is_file():
            return
        rel = p.relative_to(self.root)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dest = self.backup_dir / stamp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)

    def _atomic_write(self, p: Path, content: str) -> None:
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolError(f"Refusing to write {len(data)} bytes (> {MAX_WRITE_BYTES}).")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".agent-tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def write_file(self, path: str, content: str) -> str:
        p = self._path(path)
        self._backup(p)
        self._atomic_write(p, content)
        return f"Wrote {path} ({len(content)} characters)."

    def create_file(self, path: str, content: str) -> str:
        p = self._path(path)
        if p.exists():
            raise ToolError(f"{path} already exists. Use write_file or patch_file.")
        return self.write_file(path, content)

    def append_file(self, path: str, content: str) -> str:
        p = self._path(path)
        existing = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        return self.write_file(path, existing + content)

    def delete_file(self, path: str) -> str:
        p = self._path(path)
        if not p.is_file():
            raise ToolError(f"File does not exist: {path}")
        self._backup(p)
        p.unlink()
        return f"Deleted {path}; backup saved."

    def move_file(self, source: str, destination: str) -> str:
        src = self._path(source)
        dst = self._path(destination)
        if not src.is_file():
            raise ToolError(f"Source does not exist: {source}")
        if dst.exists():
            raise ToolError(f"Destination exists: {destination}")
        self._backup(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"Moved {source} -> {destination}"

    # ------------------------------------------------------------------ patch
    def patch_file(self, path: str, edits: list[dict[str, str]], replace_all: bool = False) -> str:
        """Apply a list of {old, new} search/replace edits to a file.

        Each `old` block must match exactly once unless `replace_all` is True.
        Edits are applied in order; overlapping blocks are rejected.
        """
        p = self._path(path)
        if not p.is_file():
            raise ToolError(f"File does not exist: {path}")
        if not edits:
            raise ToolError("No edits provided.")
        text = p.read_text(encoding="utf-8")
        self._backup(p)
        working = text
        for i, e in enumerate(edits):
            old = e.get("old", "")
            new = e.get("new", "")
            if not old:
                raise ToolError(f"Edit {i}: 'old' is empty.")
            count = working.count(old)
            if count == 0:
                raise ToolError(f"Edit {i}: search block not found in {path}.")
            if count > 1 and not replace_all:
                raise ToolError(
                    f"Edit {i}: search block appears {count} times in {path}; "
                    "make it more specific or set replace_all=true."
                )
            working = working.replace(old, new) if replace_all else working.replace(old, new, 1)
        self._atomic_write(p, working)
        return f"Patched {path} ({len(edits)} edit(s))."

    # ------------------------------------------------------------------ spec
    def tool_spec(self) -> list[dict[str, Any]]:
        return [
            {"name": "list_files", "description": "List files under a path, optionally filtered by glob.",
             "args": {"path": "string (default '.')", "pattern": "string (optional glob)"}},
            {"name": "tree", "description": "Render a compact directory tree.",
             "args": {"path": "string (default '.')", "max_depth": "int (default 3)"}},
            {"name": "read_file", "description": "Read a UTF-8 text file, optionally by line range.",
             "args": {"path": "string", "start": "int (1-based, optional)", "end": "int (optional)"}},
            {"name": "search_files", "description": "Search text across project files.",
             "args": {"query": "string", "path": "string (default '.')",
                      "regex": "bool (optional)", "ignore_case": "bool (default true)",
                      "glob": "string (optional)"}},
            {"name": "write_file", "description": "Write a complete file (backs up the old copy).",
             "args": {"path": "string", "content": "string"}},
            {"name": "create_file", "description": "Create a new file; fails if it exists.",
             "args": {"path": "string", "content": "string"}},
            {"name": "append_file", "description": "Append content to a file, creating it if needed.",
             "args": {"path": "string", "content": "string"}},
            {"name": "patch_file", "description": "Apply exact search/replace edits to a file.",
             "args": {"path": "string", "edits": "list[{old, new}]",
                      "replace_all": "bool (default false)"}},
            {"name": "delete_file", "description": "Delete a file (backup is saved).",
             "args": {"path": "string"}},
            {"name": "move_file", "description": "Move or rename a file.",
             "args": {"source": "string", "destination": "string"}},
        ]
