from __future__ import annotations


class AgentError(Exception):
    """Base class for all agent errors."""


class PathViolation(AgentError):
    """Raised when a tool tries to touch a path outside the sandbox."""


class ToolError(AgentError):
    """Raised when a tool fails to execute."""


class ToolDenied(AgentError):
    """Raised when the user denies a tool call."""


class ProtocolError(AgentError):
    """Raised when the model output cannot be parsed."""


class BrowserError(AgentError):
    """Raised for browser automation failures."""
