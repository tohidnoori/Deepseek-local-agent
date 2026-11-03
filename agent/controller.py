from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import AgentConfig
from .errors import AgentError, ToolDenied, ToolError
from .git_tools import AGENT_TOOLS as GIT_AGENT_TOOLS, GitTools
from .logging_setup import get_logger
from .prompts import continuation_prompt, system_prompt
from .registry import ToolRegistry
from .shell_tools import ShellTools
from .tools import ProjectTools

log = get_logger("controller")

ApprovalCallback = Callable[[str], bool]


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    name: str
    ok: bool
    payload: Any
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"tool": self.name}
        if self.ok:
            out["output"] = {"result": self.payload}
        else:
            out["output"] = {"error": self.error}
        return out


@dataclass
class TaskOutcome:
    summary: str
    rounds: int
    completed: bool
    transcript: list[dict[str, Any]] = field(default_factory=list)


class AgentController:
    """Drives the plan -> act -> observe loop against a browser-backed model."""

    def __init__(
        self,
        browser,
        tools: ProjectTools,
        git: GitTools | None = None,
        shell: ShellTools | None = None,
        config: AgentConfig | None = None,
    ) -> None:
        self.browser = browser
        self.tools = tools
        self.git = git if (git and git.is_repo()) else None
        self.shell = shell
        self.config = config or AgentConfig()
        self.registry = self._build_registry()

    # ---------------------------------------------------------------- registry
    def _build_registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        t = self.tools

        reg.register("list_files", lambda a: t.list_files(a.get("path", "."), a.get("pattern")),
                     description="List files under a path.")
        reg.register("tree", lambda a: t.tree(a.get("path", "."), int(a.get("max_depth", 3))),
                     description="Render a directory tree.")
        reg.register("read_file", lambda a: t.read_file(a["path"], int(a.get("start", 1)), a.get("end")),
                     description="Read a file (optionally by line range).")
        reg.register("search_files",
                     lambda a: t.search_files(
                         a["query"], a.get("path", "."),
                         bool(a.get("regex", False)), bool(a.get("ignore_case", True)), a.get("glob"),
                     ),
                     description="Search text across project files.")

        reg.register("write_file", lambda a: t.write_file(a["path"], a["content"]),
                     mutating=True, dangerous=True, description="Write a whole file.")
        reg.register("create_file", lambda a: t.create_file(a["path"], a["content"]),
                     mutating=True, dangerous=True, description="Create a new file.")
        reg.register("append_file", lambda a: t.append_file(a["path"], a["content"]),
                     mutating=True, dangerous=True, description="Append to a file.")
        reg.register("patch_file",
                     lambda a: t.patch_file(a["path"], a["edits"], bool(a.get("replace_all", False))),
                     mutating=True, dangerous=True, description="Apply search/replace edits.")
        reg.register("delete_file", lambda a: t.delete_file(a["path"]),
                     mutating=True, dangerous=True, description="Delete a file.")
        reg.register("move_file", lambda a: t.move_file(a["source"], a["destination"]),
                     mutating=True, dangerous=True, description="Move/rename a file.")

        if self.git:
            for name in GIT_AGENT_TOOLS:
                mutating = name in {"git_stage", "git_commit"}
                reg.register(
                    name,
                    (lambda n: (lambda a: self.git.dispatch(n, **a)))(name),
                    mutating=mutating,
                    dangerous=mutating,
                    description=f"{name.replace('_', ' ')}",
                )

        if self.shell:
            reg.register(
                "run_command",
                lambda a: self._format_shell_result(a["command"]),
                mutating=True,
                dangerous=True,
                description="Run a cmd.exe command in the project root.",
            )

        return reg

    @staticmethod
    def _short_value(value: Any, limit: int = 160) -> str:
        try:
            if isinstance(value, (dict, list)):
                text = json.dumps(value, ensure_ascii=False, default=str)
            else:
                text = str(value)
        except Exception:
            text = repr(value)
        text = text.replace("\n", " ⏎ ")
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @classmethod
    def _short_args(cls, args: dict[str, Any]) -> str:
        parts = []
        for k, v in args.items():
            parts.append(f"{k}={cls._short_value(v, 60)}")
        return ", ".join(parts)

    def _format_shell_result(self, command: str) -> dict[str, Any]:
        res = self.shell.run(command)
        return {"ok": res.ok, "returncode": res.returncode, "stdout": res.stdout, "stderr": res.stderr}

    # ---------------------------------------------------------------- parsing
    def _parse(self, text: str) -> dict[str, Any] | None:
        if not text:
            return None
        for block in re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.I | re.S):
            data = self._try_json(block)
            if data is not None:
                return data
        for candidate in self._iter_balanced_json(text):
            data = self._try_json(candidate)
            if data is not None:
                return data
        xml_data = self._parse_xml(text)
        if xml_data is not None:
            return xml_data
        return None

    @staticmethod
    def _try_json(s: str) -> dict[str, Any] | None:
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict) and isinstance(data.get("tool_calls"), list):
            return data
        return None

    @staticmethod
    def _parse_xml(text: str) -> dict[str, Any] | None:
        if not text:
            return None
        m = re.search(r"<tool_calls>(.*?)</tool_calls>", text, re.S | re.I)
        if m is None:
            fm = re.search(
                r"```(?:xml)?\s*(<tool_calls>.*?</tool_calls>)\s*```",
                text, re.S | re.I,
            )
            if fm is None:
                return None
            body = fm.group(1)
        else:
            body = m.group(0)

        calls: list[dict[str, Any]] = []
        for cm in re.finditer(r"<tool_call>(.*?)</tool_call>", body, re.S | re.I):
            cb = cm.group(1)
            nm = re.search(r"<name>(.*?)</name>", cb, re.S | re.I)
            if nm is None:
                continue
            name = nm.group(1).strip()
            args: dict[str, Any] = {}
            am = re.search(r"<arguments>(.*?)</arguments>", cb, re.S | re.I)
            if am is not None:
                raw = am.group(1).strip()
                try:
                    parsed = json.loads(raw) if raw else {}
                    if isinstance(parsed, dict):
                        args = parsed
                except Exception:
                    for argm in re.finditer(
                        r'<arg\s+name=["\']([^"\']+)["\']\s*>(.*?)</arg>',
                        raw, re.S | re.I,
                    ):
                        args[argm.group(1)] = argm.group(2).strip()
            calls.append({"name": name, "arguments": args})

        if not calls:
            return None
        msg_m = re.search(r"<message>(.*?)</message>", text, re.S | re.I)
        message = msg_m.group(1).strip() if msg_m else ""
        return {"tool_calls": calls, "message": message}

    @staticmethod
    def _iter_balanced_json(text: str):
        n = len(text)
        i = 0
        while i < n:
            if text[i] != "{":
                i += 1
                continue
            depth = 0
            in_str = False
            escape = False
            end = -1
            for j in range(i, n):
                ch = text[j]
                if escape:
                    escape = False
                    continue
                if in_str:
                    if ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            if end > 0:
                yield text[i:end + 1]
                i = end + 1
            else:
                i += 1

    @staticmethod
    def _normalize_calls(parsed: dict[str, Any]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for raw in parsed.get("tool_calls", []):
            if not isinstance(raw, dict):
                continue
            name = raw.get("name")
            args = raw.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(args, dict):
                continue
            calls.append(ToolCall(name, args))
        return calls

    # ---------------------------------------------------------------- approvals
    def _needs_approval(self, call: ToolCall) -> bool:
        if call.name not in self.registry.names():
            return False
        spec = self.registry.get(call.name)
        if not spec.mutating:
            return False
        if call.name == "run_command" and self.shell and self.shell.is_safe(call.arguments.get("command", "")):
            return False
        return spec.dangerous

    def _approval_description(self, call: ToolCall) -> str:
        if call.name in {"write_file", "create_file", "append_file", "patch_file", "delete_file", "move_file"}:
            try:
                rel = call.arguments.get("path") or call.arguments.get("source") or ""
                target = self.tools._path(rel)
                if call.name == "patch_file":
                    return f"Agent wants to apply {len(call.arguments.get('edits', []))} edit(s) to {rel}"
                if call.name == "delete_file":
                    return f"Agent wants to DELETE {rel}"
                if call.name == "move_file":
                    return f"Agent wants to move {rel} -> {call.arguments.get('destination')}"
                old = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
                new = str(call.arguments.get("content", ""))
                diff = "".join(difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile=f"before/{rel}" if old else "/dev/null",
                    tofile=f"after/{rel}",
                ))
                return f"Agent wants to {call.name} {rel}\n\nProposed diff:\n{diff[:12000] or '(binary or empty change)'}"
            except Exception as e:
                return f"Agent wants to {call.name}: {json.dumps(call.arguments)[:2000]}\n(error previewing: {e})"
        return f"Agent wants to run {call.name} with args:\n{json.dumps(call.arguments, indent=2)[:4000]}"

    # ---------------------------------------------------------------- execution
    def _execute_one(self, call: ToolCall, auto_approve: bool, approve: ApprovalCallback) -> ToolResult:
        try:
            if call.name not in self.registry.names():
                return ToolResult(call.name, False, None, f"Unknown tool: {call.name}")
            if not auto_approve and self._needs_approval(call):
                if not approve(self._approval_description(call)):
                    return ToolResult(call.name, False, None, "User denied this operation.")
            payload = self.registry.call(call.name, call.arguments)
            return ToolResult(call.name, True, payload)
        except ToolDenied as e:
            return ToolResult(call.name, False, None, str(e))
        except (ToolError, AgentError) as e:
            return ToolResult(call.name, False, None, f"{type(e).__name__}: {e}")
        except TypeError as e:
            return ToolResult(call.name, False, None, f"Bad arguments for {call.name}: {e}")
        except KeyError as e:
            return ToolResult(call.name, False, None, f"Missing required argument {e} for {call.name}")
        except Exception as e:
            log.exception("tool %s crashed", call.name)
            return ToolResult(call.name, False, None, f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- loop
    def run_task(
        self,
        user_prompt: str,
        auto_approve: bool = False,
        approval_callback: ApprovalCallback | None = None,
        progress_callback: Callable[[str], None] | None = None,
        attachments: list[str] | None = None,
        format_mode: str | None = None,
    ) -> str:
        approve: ApprovalCallback = approval_callback or (lambda _: auto_approve)
        format_mode = (format_mode or getattr(self.config, "format_mode", "json") or "json").lower()
        if format_mode not in {"json", "xml", "text"}:
            format_mode = "json"

        def emit(msg: str) -> None:
            if progress_callback is not None:
                try:
                    progress_callback(msg)
                except Exception:
                    log.debug("progress callback failed", exc_info=True)

        transcript = (
            system_prompt(self.registry.specs(), str(self.tools.root), format_mode)
            + "\n\nUser task:\n"
            + user_prompt
        )
        last_response = ""
        seen_signatures: dict[str, int] = {}

        for round_no in range(1, self.config.max_rounds + 1):
            log.info("round %d (transcript %d chars, format=%s)", round_no, len(transcript), format_mode)
            note = ""
            if attachments:
                note = f" with {len(attachments)} attachment(s)"
            emit(f"▶ Round {round_no}/{self.config.max_rounds} — sending prompt ({len(transcript)} chars, {format_mode}){note}…")
            round_attachments = attachments if round_no == 1 else None
            last_response = self.browser.send_and_wait(transcript, attachments=round_attachments)
            emit(f"◀ Reply received ({len(last_response)} chars). Parsing…")

            if format_mode == "text":
                emit("✔ Text mode — returning reply as final summary.")
                return last_response.strip()

            parsed = self._parse(last_response)
            if not parsed:
                log.info("no tool block — treating as final summary")
                emit("✔ No tool calls — treating reply as final summary.")
                return last_response.strip()

            calls = self._normalize_calls(parsed)[: self.config.max_tool_calls_per_round]
            if not calls:
                emit("✔ Empty tool list — finishing.")
                return str(parsed.get("message") or last_response).strip()
            msg = str(parsed.get("message") or "").strip()
            if msg:
                emit(f"🧠 Model: {msg}")

            sig = json.dumps([[c.name, c.arguments] for c in calls], sort_keys=True, default=str)
            seen_signatures[sig] = seen_signatures.get(sig, 0) + 1
            if seen_signatures[sig] >= 2:
                return (
                    "Stopped: the model repeated the same tool calls twice in a row. "
                    "Last message:\n" + str(parsed.get("message") or "").strip()
                )

            results: list[ToolResult] = []
            for c in calls:
                emit(f"🔧 {c.name}({self._short_args(c.arguments)})")
                r = self._execute_one(c, auto_approve, approve)
                if r.ok:
                    emit(f"   └ ok: {self._short_value(r.payload)}")
                else:
                    emit(f"   └ error: {r.error}")
                results.append(r)

            names = [c.name for c in calls]
            if names and all(n in {"git_stage", "git_commit"} for n in names):
                lines = []
                for r in results:
                    lines.append(str(r.payload) if r.ok else f"ERROR: {r.error}")
                return "Committed:\n" + "\n".join(x.strip() for x in lines if x)

            transcript = continuation_prompt(parsed, [r.to_dict() for r in results], format_mode)

        return (
            f"Stopped after {self.config.max_rounds} rounds. "
            "Last model message:\n" + last_response.strip()
        )