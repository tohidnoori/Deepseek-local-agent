from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .errors import ToolDenied, ToolError
from .logging_setup import get_logger

log = get_logger("registry")


@dataclass
class ToolSpec:
    name: str
    handler: Callable[[dict[str, Any]], Any]
    mutating: bool = False
    dangerous: bool = False
    description: str = ""

    def spec_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description}


class ToolRegistry:
    """Maps tool names to handlers and metadata."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        handler: Callable[[dict[str, Any]], Any],
        *,
        mutating: bool = False,
        dangerous: bool = False,
        description: str = "",
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = ToolSpec(name, handler, mutating, dangerous, description)

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise ToolError(f"Unknown tool: {name}")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec_dict() for t in self._tools.values()]

    def call(self, name: str, args: dict[str, Any]) -> Any:
        spec = self.get(name)
        return spec.handler(args)
