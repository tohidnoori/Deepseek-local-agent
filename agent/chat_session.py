# agent/chat_session.py
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CHAT_STORE = Path.home() / "AppData" / "Local" / "DeepSeekLocalAgent" / "chat_sessions.json"


@dataclass
class ChatMessage:
    role: str          # "user" | "assistant" | "system"
    content: str       # raw markdown
    ts: float = field(default_factory=time.time)


@dataclass
class ChatSession:
    id: str
    title: str
    project_root: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def add(self, role: str, content: str) -> None:
        self.messages.append(asdict(ChatMessage(role=role, content=content)))
        self.updated = time.time()


class ChatSessionStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else CHAT_STORE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, ChatSession] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        for raw in data.get("sessions", []):
            try:
                self._sessions[raw["id"]] = ChatSession(**raw)
            except Exception:
                continue

    def _save(self) -> None:
        payload = {"version": 1, "sessions": [asdict(s) for s in self._sessions.values()]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def create(self, title: str = "New chat", project_root: str | None = None) -> ChatSession:
        s = ChatSession(id=uuid.uuid4().hex, title=title, project_root=project_root)
        self._sessions[s.id] = s
        self._save()
        return s

    def get(self, sid: str) -> ChatSession | None:
        return self._sessions.get(sid)

    def delete(self, sid: str) -> None:
        if self._sessions.pop(sid, None):
            self._save()

    def all(self) -> list[ChatSession]:
        return sorted(self._sessions.values(), key=lambda s: s.updated, reverse=True)