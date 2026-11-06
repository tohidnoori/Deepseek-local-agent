from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OpType(str, Enum):
    CREATE = "CREATE"
    REPLACE = "REPLACE"
    UPDATE = "UPDATE"
    APPEND = "APPEND"
    DELETE = "DELETE"
    MOVE = "MOVE"


@dataclass
class FileOperation:
    op: OpType
    path: str
    content: str | None = None
    source_message: str = ""
    destination: str | None = None
    confidence: float = 1.0
    warnings: list[str] = field(default_factory=list)
    raw_snippet: str = ""

    def key(self) -> tuple[str, str]:
        return (self.op.value, self.path)


@dataclass
class CommandOperation:
    command: str
    purpose: str = ""
    source_message: str = ""
    shell: str = "cmd"          # "cmd" | "bash" | "powershell"
    raw_snippet: str = ""

    def key(self) -> tuple[str, str]:
        return ("CMD", self.command.strip())


@dataclass
class OperationSet:
    operations: list[FileOperation] = field(default_factory=list)
    commands: list[CommandOperation] = field(default_factory=list)
    ignored: list[FileOperation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, op: FileOperation) -> None:
        if any(o.key() == op.key() for o in self.operations):
            self.ignored.append(op)
        else:
            self.operations.append(op)

    def add_command(self, cmd: CommandOperation) -> None:
        if any(c.key() == cmd.key() for c in self.commands):
            return
        self.commands.append(cmd)