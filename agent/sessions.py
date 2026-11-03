from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

log = get_logger("sessions")


DEFAULT_STORE = Path.home() / "AppData" / "Local" / "DeepSeekLocalAgent" / "sessions.json"


@dataclass
class Message:
    role: str  # "user" | "assistant" | "progress" | "error"
    text: str
    ts: float = field(default_factory=time.time)


@dataclass
class ChatSession:
    id: str
    title: str
    project_root: str
    deepseek_url: str | None = None   # if the chat lives on the DeepSeek web UI
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    messages: list[dict[str, Any]] = field(default_factory=list)

    def add(self, role: str, text: str) -> None:
        self.messages.append(asdict(Message(role=role, text=text)))
        self.updated = time.time()

    @property
    def project_name(self) -> str:
        try:
            return Path(self.project_root).name or self.project_root
        except Exception:
            return self.project_root


class SessionStore:
    """A tiny JSON-file store of chat sessions, keyed by project + chat."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else DEFAULT_STORE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, ChatSession] = {}
        self._load()

    # ------------------------------------------------------------------ io
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("could not read sessions file: %s", e)
            return
        for raw in data.get("sessions", []):
            try:
                s = ChatSession(**raw)
                self._sessions[s.id] = s
            except Exception:
                continue

    def _save(self) -> None:
        payload = {"version": 1, "sessions": [asdict(s) for s in self._sessions.values()]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------ api
    def create(self, project_root: str, title: str = "New chat") -> ChatSession:
        s = ChatSession(id=uuid.uuid4().hex, title=title, project_root=project_root)
        self._sessions[s.id] = s
        self._save()
        return s

    def get(self, session_id: str) -> ChatSession | None:
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> None:
        if self._sessions.pop(session_id, None) is not None:
            self._save()

    def rename(self, session_id: str, title: str) -> None:
        s = self._sessions.get(session_id)
        if not s:
            return
        s.title = title.strip() or s.title
        s.updated = time.time()
        self._save()

    def set_deepseek_url(self, session_id: str, url: str | None) -> None:
        s = self._sessions.get(session_id)
        if not s:
            return
        s.deepseek_url = url
        s.updated = time.time()
        self._save()

    def add_message(self, session_id: str, role: str, text: str) -> None:
        s = self._sessions.get(session_id)
        if not s:
            return
        s.add(role, text)
        # First user message becomes the title, if still default.
        if role == "user" and s.title in ("New chat", ""):
            s.title = text.strip().splitlines()[0][:80] or "Untitled"
        self._save()

    def for_project(self, project_root: str) -> list[ChatSession]:
        proj = str(Path(project_root).resolve())
        rows = [s for s in self._sessions.values() if str(Path(s.project_root).resolve()) == proj]
        rows.sort(key=lambda s: s.updated, reverse=True)
        return rows

    def all(self) -> list[ChatSession]:
        rows = list(self._sessions.values())
        rows.sort(key=lambda s: s.updated, reverse=True)
        return rows
