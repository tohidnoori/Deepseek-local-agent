from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from .file_ops_model import (
    CommandOperation,
    FileOperation,
    OpType,
    OperationSet,
)
from .logging_setup import get_logger

log = get_logger("file_ops_extractor")

# ── Extensions ─────────────────────────────────────────────────────────────
_EXTS = (
    "py|pyi|js|jsx|ts|tsx|mjs|cjs|vue|svelte|astro|"
    "html|htm|css|scss|sass|less|styl|"
    "json|jsonc|json5|yaml|yml|toml|ini|cfg|conf|properties|"
    "env|md|mdx|markdown|rst|txt|"
    "sh|bash|zsh|fish|bat|cmd|ps1|psm1|"
    "java|kt|kts|scala|groovy|gradle|"
    "c|h|cc|cpp|cxx|hpp|hxx|cs|go|rs|rb|php|swift|m|mm|lua|pl|pm|r|jl|"
    "sql|prisma|graphql|gql|proto|xml|svg|"
    "tf|tfvars|hcl|lock|dockerfile|makefile|cmake|mk"
)
_EXT_ALT = f"(?:{_EXTS})"
_PATH_BODY = rf"[A-Za-z0-9_.\-]+(?:[/\\][A-Za-z0-9_.\-]+)*\.{_EXT_ALT}"
_PATH = re.compile(rf"(?<![\w./\\-])({_PATH_BODY})(?![\w./\\-])", re.IGNORECASE)


# ── Fences ─────────────────────────────────────────────────────────────────
_FENCE = re.compile(
    r"(?P<bt>`{3,})(?P<lang>[^\n`]*)\n(?P<body>.*?)\n(?P=bt)",
    re.DOTALL,
)


# ── Path patterns (higher priority = more explicit) ────────────────────────
# 1. Inside the fence body (first 6 lines)
_HDR_TAGGED = re.compile(
    rf"^[ \t]*(?:#|//|--|;|%|/\*|\*|<!--)[ \t]*"
    rf"(?:file|filename|filepath|path|فایل|مسیر)[ \t]*[:\-=]?[ \t]*"
    rf"`?({_PATH_BODY})`?",
    re.MULTILINE | re.IGNORECASE,
)
_HDR_BANNER = re.compile(
    rf"^[ \t]*(?:#{{2,}}|={{2,}}|-{{2,}})[ \t]*`?({_PATH_BODY})`?[ \t]*(?:#{{2,}}|={{2,}}|-{{2,}})?[ \t]*$",
    re.MULTILINE,
)
_HDR_BARE = re.compile(
    rf"^[ \t]*(?:#|//|--|;|%|/\*|\*)[ \t]*`?({_PATH_BODY})`?[ \t]*$",
    re.MULTILINE,
)
_HDR_DOCSTRING = re.compile(
    rf"^[ \t]*(?:\"\"\"|''')[\s]*({_PATH_BODY})[\s]*(?:\"\"\"|''')",
    re.MULTILINE,
)

# 2. Just before the fence (context look-back)
_PRE_INLINE = re.compile(
    rf"\b(?:file|filename|filepath|path|فایل|مسیر)\s*[:\-]\s*[`\*]*({_PATH_BODY})",
    re.IGNORECASE,
)
_PRE_BACKTICK = re.compile(rf"`({_PATH_BODY})`")
_PRE_BOLD     = re.compile(rf"\*\*({_PATH_BODY})\*\*")
_PRE_HEADING  = re.compile(
    rf"^#{{1,6}}\s+(?:\d+\.\s+)?`?({_PATH_BODY})`?[ \t]*$",
    re.MULTILINE,
)
_PRE_PLAIN    = _PATH


# ── Command detection ──────────────────────────────────────────────────────
_CMD_LANGS = {
    "bash", "sh", "shell", "zsh", "fish",
    "powershell", "pwsh", "ps1", "cmd", "bat", "bat/cmd", "dos",
}
_DOLLAR_LINE = re.compile(r"^\s*\$[ \t]+(.+)$", re.MULTILINE)
_CMD_STARTERS = re.compile(
    r"^\s*(?:sudo\s+)?(?:pip|pipx|python|python3|py|node|npm|yarn|pnpm|"
    r"npx|deno|bun|git|gh|curl|wget|docker|docker-compose|kubectl|helm|"
    r"make|cargo|rustc|go|mvn|gradle|gradlew|dotnet|flutter|"
    r"nvm|poetry|uv|conda|virtualenv|source|\.\.?/|"
    r"apt|apt-get|yum|dnf|pacman|brew|choco|winget|scoop|"
    r"rm|mv|cp|mkdir|rmdir|touch|chmod|chown|ln|"
    r"cat|echo|ls|dir|cd|pwd|"
    r"ssh|scp|rsync|systemctl|service|"
    r"export|set|unset|if|for|while)\b",
    re.IGNORECASE,
)
_CMD_CONTEXT = re.compile(
    r"\b(?:run|execute|install|setup|set up|invoke|fire|try|use)\b"
    r"|اجرا|نصب|بزنید|بزن",
    re.IGNORECASE,
)


# ── File-op intent lexicon ─────────────────────────────────────────────────
_CREATE = re.compile(
    r"\b(create|new file|add a file|add a new|save (?:this )?as|"
    r"write a new|write the new|write the following to)\b"
    r"|(?:\u2014|\u2013|--)\s*new\b"
    r"|\(\s*new(?:\s+file)?\s*\)"
    r"|(?:\u2014|\u2013|--)\s*جدید"
    r"|فایل جدید|ایجاد|بساز",
    re.IGNORECASE,
)
_REPLACE = re.compile(
    r"\b(replace|overwrite|rewrite|swap|use this instead|"
    r"here(?:'|\u2019)?s? the (?:new|updated|full|final) version|"
    r"full file|entire file|complete file)\b"
    r"|جایگزین|بازنویسی|کل فایل|کل محتوا",
    re.IGNORECASE,
)
_UPDATE = re.compile(
    r"\b(update|modify|change|edit|patch|fix|adjust|refactor)\b"
    r"|به.?روز|اصلاح|تغییر|ویرایش",
    re.IGNORECASE,
)
_APPEND = re.compile(
    r"\b(append|add this to the end|add to the end|insert at the end)\b"
    r"|اضافه کن|به انتها|به آخر",
    re.IGNORECASE,
)
_DELETE = re.compile(
    r"\b(delete (?:this |the )?file|remove (?:this |the )?file|drop this file)\b"
    r"|حذف|پاک کن",
    re.IGNORECASE,
)
_MOVE = re.compile(
    r"\b(move (?:this |the )?file to|rename (?:this |the )?file to|rename to|move to)\b"
    r"|انتقال|تغییر نام",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helper: find file name near a fence (also used by ChatPanel)
# ═══════════════════════════════════════════════════════════════════════════
def _path_from_fence_header(body: str) -> tuple[Optional[str], str]:
    head = "\n".join(body.splitlines()[:6])
    for label, pat in (
        ("tagged", _HDR_TAGGED),
        ("banner", _HDR_BANNER),
        ("bare", _HDR_BARE),
        ("docstring", _HDR_DOCSTRING),
    ):
        m = pat.search(head)
        if m:
            return m.group(1).strip(), label
    return None, ""


def _paths_before(text: str, fence_start: int,
                  window: int = 600) -> list[tuple[int, str, str, int]]:
    """Return (score, path, source, position) for path mentions before a fence."""
    pre_start = max(0, fence_start - window)
    pre = text[pre_start: fence_start]
    found: dict[str, tuple[int, str, str, int]] = {}

    def consider(path: str, source: str, weight: int, pos_in_pre: int) -> None:
        # Higher weight dominates; within same weight, later position wins
        score = weight * 100_000 + pos_in_pre
        prev = found.get(path)
        if prev is None or score > prev[0]:
            found[path] = (score, path, source, pre_start + pos_in_pre)

    for m in _PRE_INLINE.finditer(pre):
        consider(m.group(1), "pre_inline", 50, m.start())
    for m in _PRE_HEADING.finditer(pre):
        consider(m.group(1), "pre_heading", 40, m.start())
    for m in _PRE_BACKTICK.finditer(pre):
        consider(m.group(1), "pre_backtick", 30, m.start())
    for m in _PRE_BOLD.finditer(pre):
        consider(m.group(1), "pre_bold", 20, m.start())
    for m in _PRE_PLAIN.finditer(pre):
        consider(m.group(1), "pre_plain", 10, m.start())

    return list(found.values())


def find_file_name_near(text: str, fence_start: int, body: str) -> Optional[str]:
    """Public helper — used by ChatPanel to label each code block."""
    header_path, _ = _path_from_fence_header(body)
    if header_path:
        return header_path
    before = _paths_before(text, fence_start)
    if before:
        before.sort(key=lambda x: x[0], reverse=True)
        return before[0][1]
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Intent + command classification
# ═══════════════════════════════════════════════════════════════════════════
def _classify_file_intent(context: str) -> OpType:
    if _MOVE.search(context):
        return OpType.MOVE
    if _DELETE.search(context):
        return OpType.DELETE
    if _APPEND.search(context):
        return OpType.APPEND
    if _CREATE.search(context):
        return OpType.CREATE
    if _REPLACE.search(context):
        return OpType.REPLACE
    if _UPDATE.search(context):
        return OpType.UPDATE
    return OpType.REPLACE


def _is_command_fence(lang: str, body: str, pre_context: str) -> bool:
    if lang.strip().lower() in _CMD_LANGS:
        return True
    if _DOLLAR_LINE.search(body):
        return True
    first_line = next((l for l in body.splitlines() if l.strip()), "")
    if first_line and _CMD_STARTERS.match(first_line):
        if _CMD_CONTEXT.search(pre_context):
            return True
        if len(body.splitlines()) <= 3:
            return True
    return False


def _strip_prompt(body: str) -> str:
    out = []
    for line in body.splitlines():
        m = _DOLLAR_LINE.match(line)
        out.append(m.group(1) if m else line)
    return "\n".join(out).strip()


def _make_command(fence_start: int, text: str, body: str) -> Optional[CommandOperation]:
    cleaned = _strip_prompt(body)
    if not cleaned:
        return None
    pre = text[max(0, fence_start - 300): fence_start].strip()
    purpose = pre.splitlines()[-1].rstrip(":").strip() if pre else ""
    return CommandOperation(
        command=cleaned,
        purpose=purpose[:200],
        shell="cmd",
        source_message=text[:300],
        raw_snippet=body[:800],
    )


# ═══════════════════════════════════════════════════════════════════════════
# Extraction
# ═══════════════════════════════════════════════════════════════════════════
def _extract_from_message(text: str, result: OperationSet) -> None:
    if not text:
        return
    fences = list(_FENCE.finditer(text))
    if not fences:
        return

    used_positions: set[int] = set()

    for fence in fences:
        lang = fence.group("lang").strip()
        body = fence.group("body")
        pre = text[max(0, fence.start() - 400): fence.start()]
        body_path, body_source = _path_from_fence_header(body)

        # ── 1. Commands (may also have a body path, but we treat them separately)
        if _is_command_fence(lang, body, pre):
            cmd = _make_command(fence.start(), text, body)
            if cmd is not None:
                result.add_command(cmd)
            # If the fence ALSO has a body path, keep processing (rare, but
            # covers things like "# file: x.sh" in a bash block we also
            # want to write as a file).
            if not body_path:
                continue

        # ── 2. Files ──────────────────────────────────────────────────────
        path = body_path
        source = body_source
        chosen_pos = -1

        if not path:
            before = _paths_before(text, fence.start())
            # Prefer mentions we haven't "used" yet — handles multi-file lists
            fresh = [p for p in before if p[3] not in used_positions]
            pool = fresh if fresh else before
            if pool:
                pool.sort(key=lambda x: x[0], reverse=True)
                _, path, source, chosen_pos = pool[0]

        if not path:
            result.errors.append(
                f"Fence (lang={lang or '?'}, {len(body)} chars) has no "
                f"identifiable file name. First line: "
                f"{body.strip().splitlines()[0][:80]!r}"
            )
            continue

        if chosen_pos > 0:
            used_positions.add(chosen_pos)

        op = _classify_file_intent(pre + "\n" + (body_path or ""))
        result.add(FileOperation(
            op=op,
            path=path,
            content=body,
            source_message=text[:300],
            raw_snippet=fence.group(0)[:1500],
        ))


def extract_operations(messages: Iterable[dict]) -> OperationSet:
    result = OperationSet()
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        _extract_from_message(msg.get("content", ""), result)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Diagnostics — run this against any chat to see WHY a path was picked
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class _FenceInfo:
    start: int
    end: int
    lang: str
    body_len: int
    path_from_body: Optional[str]
    path_source: str
    candidates_before: list[tuple[int, str, str, int]]
    is_command_block: bool
    sample: str


def debug_extract(text: str) -> dict:
    fences = list(_FENCE.finditer(text))
    info = {
        "message_len": len(text),
        "fences": [],
        "all_path_mentions": [],
    }
    for m in fences:
        body = m.group("body")
        body_path, source = _path_from_fence_header(body)
        before = _paths_before(text, m.start())
        info["fences"].append(_FenceInfo(
            start=m.start(),
            end=m.end(),
            lang=m.group("lang").strip(),
            body_len=len(body),
            path_from_body=body_path,
            path_source=source,
            candidates_before=before,
            is_command_block=_is_command_fence(
                m.group("lang"), body,
                text[max(0, m.start() - 400): m.start()],
            ),
            sample=body.strip()[:100],
        ))

    for m in _PRE_INLINE.finditer(text):
        info["all_path_mentions"].append(("pre_inline", m.group(1), m.start()))
    for m in _PRE_BACKTICK.finditer(text):
        info["all_path_mentions"].append(("pre_backtick", m.group(1), m.start()))
    for m in _PRE_BOLD.finditer(text):
        info["all_path_mentions"].append(("pre_bold", m.group(1), m.start()))
    for m in _PRE_HEADING.finditer(text):
        info["all_path_mentions"].append(("pre_heading", m.group(1), m.start()))
    return info