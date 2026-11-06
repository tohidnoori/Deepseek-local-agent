# agent/patcher.py
"""
Safe surgical patcher.

Reads a plain-text instruction like the "Two small patches" snippet and
applies ONLY the specific change it asks for — never replaces a whole file.

Supported instruction shapes (English, matching the patch style used in
this project):

  1. "Find the X dataclass and add one line: <python block>"
  2. "Paste anywhere inside X (e.g. after Y): <python block>"
  3. "Find this line inside X: <a>\nReplace with: <b>"

Anything else is reported as 'unsupported' and the file is left untouched.
"""
from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Matches "```python\n...\n```" fenced blocks
_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class PatchOp:
    file: Path
    kind: str          # "add_field" | "add_method" | "replace_line" | "unsupported"
    payload: str = ""
    old: str = ""
    new: str = ""
    anchor: str = ""
    description: str = ""
    warnings: list[str] = field(default_factory=list)


# ===========================================================================
# Parse the instruction text
# ===========================================================================
def parse_patches(text: str, project_root: Path) -> list[PatchOp]:
    ops: list[PatchOp] = []
    blocks = _FENCE.findall(text)
    headers = _split_into_sections(text)

    for header, body in headers:
        op = _parse_one(header, body, blocks, project_root)
        if op:
            ops.append(op)
    return ops


def _split_into_sections(text: str) -> list[tuple[str, str]]:
    """Split '8.1 file.py — do X' sections apart."""
    pattern = re.compile(
        r"^#{1,4}\s*(?:\d+(?:\.\d+)*\.?\s*)?"
        r"([\w./\\-]+\.py)\s*(?:—|-|:)\s*(.*?)$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((m.group(0), text[start:end]))
    return out


def _parse_one(header: str, body: str, blocks: list[str],
               root: Path) -> Optional[PatchOp]:
    filename = re.search(r"([\w./\\-]+\.py)", header)
    if not filename:
        return None
    file = (root / filename.group(1)).resolve()

    payload = ""
    for b in blocks:
        if b in body:
            payload = b
            break

    lower = (header + " " + body).lower()

    # --- add_field (dataclass field) -----------------------------------
    if "add one line" in lower or "add the field" in lower or "add a field" in lower:
        anchor = ""
        m = re.search(r"Find the (\w+) dataclass", body, re.IGNORECASE)
        if m:
            anchor = f"class {m.group(1)}"
        return PatchOp(
            file=file, kind="add_field", payload=payload,
            anchor=anchor, description=header.strip(),
        )

    # --- add_method ----------------------------------------------------
    if "paste anywhere inside" in lower or "add two helper" in lower or \
            "add a method" in lower or "add this method" in lower or \
            "add helper" in lower:
        anchor = ""
        m = re.search(r"after (\w+)\b", body, re.IGNORECASE)
        if m:
            anchor = f"def {m.group(1)}"
        if not anchor:
            m = re.search(r"inside (?:the )?(\w+)", body, re.IGNORECASE)
            if m:
                anchor = f"class {m.group(1)}"
        return PatchOp(
            file=file, kind="add_method", payload=payload,
            anchor=anchor, description=header.strip(),
        )

    # --- replace_line --------------------------------------------------
    m = re.search(r"Find (?:this line|the line)(?: inside \w+)?:\s*\n(.*?)\n", body)
    if m:
        old = m.group(1).strip()
        m2 = re.search(r"Replace with:\s*\n(.*?)(?:\n\n|$)", body, re.DOTALL)
        new = m2.group(1).strip() if m2 else payload
        return PatchOp(
            file=file, kind="replace_line", old=old, new=new,
            description=header.strip(),
        )

    return PatchOp(
        file=file, kind="unsupported", payload=payload,
        description=header.strip(),
        warnings=["Could not determine patch kind from instruction."],
    )


# ===========================================================================
# Apply one patch
# ===========================================================================
def apply_patch(op: PatchOp, dry_run: bool = False) -> tuple[bool, str]:
    if op.kind == "unsupported":
        return False, f"{op.file.name}: unsupported patch shape"

    if not op.file.exists():
        return False, f"{op.file}: file not found"

    src = op.file.read_text(encoding="utf-8")

    if op.kind == "add_field":
        new_src = _apply_add_field(src, op)
    elif op.kind == "add_method":
        new_src = _apply_add_method(src, op)
    elif op.kind == "replace_line":
        new_src = _apply_replace_line(src, op)
    else:
        return False, f"{op.file.name}: unknown kind {op.kind}"

    if new_src is None:
        return False, f"{op.file.name}: {op.kind} failed (anchor not found)"

    if new_src == src:
        return True, f"{op.file.name}: already applied"

    if dry_run:
        return True, f"{op.file.name}: would apply ({op.kind})"

    # Backup before touching anything
    backup_dir = op.file.parent / ".patch_backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(op.file, backup_dir / f"{op.file.name}.{stamp}.bak")

    op.file.write_text(new_src, encoding="utf-8")
    return True, f"{op.file.name}: applied ({op.kind})"


# ===========================================================================
# Add field inside a dataclass
# ===========================================================================
def _apply_add_field(src: str, op: PatchOp) -> Optional[str]:
    payload = op.payload.strip()
    if not payload:
        return None

    anchor = op.anchor or "class TransactionDraft"
    idx = src.find(anchor)
    if idx == -1:
        return None
    # Find end of the last field in the dataclass (first blank line after
    # the anchor at same indent level, or the next class).
    body_start = idx + len(anchor)
    next_class = src.find("\nclass ", body_start)
    next_def   = src.find("\ndef ", body_start)
    boundaries = [x for x in (next_class, next_def) if x != -1]
    body_end = min(boundaries) if boundaries else len(src)
    block = src[body_start:body_end]

    # Already present?
    field_name = _extract_last_field_name(payload)
    if field_name and re.search(rf"\b{re.escape(field_name)}\b\s*:", block):
        return src  # no-op

    # Insert the line at the end of the field block, aligned with existing fields
    lines = block.splitlines(keepends=True)
    # Find the last line that looks like a field (indented `name: type`)
    last_field_line = -1
    for i, line in enumerate(lines):
        if re.match(r"\s+\w+\s*:\s*\S", line):
            last_field_line = i
    if last_field_line == -1:
        return None

    indent_match = re.match(r"(\s*)", lines[last_field_line])
    indent = indent_match.group(1) if indent_match else "    "
    # Normalise the payload to the same indent
    payload_lines = [indent + l.strip() for l in payload.splitlines()]

    insert_at = last_field_line + 1
    lines[insert_at:insert_at] = [p + "\n" for p in payload_lines]
    new_block = "".join(lines)
    return src[:body_start] + new_block + src[body_end:]


def _extract_last_field_name(payload: str) -> Optional[str]:
    m = re.search(r"(\w+)\s*:", payload)
    return m.group(1) if m else None


# ===========================================================================
# Add method(s) inside a class
# ===========================================================================
def _apply_add_method(src: str, op: PatchOp) -> Optional[str]:
    payload = op.payload.rstrip()
    if not payload:
        return None

    anchor = op.anchor
    if anchor:
        idx = src.find(anchor)
        if idx == -1:
            # Fallback: anchor may be a def that already exists — insert after it
            return None
        # Find the class that contains this anchor
        class_start = src.rfind("\nclass ", 0, idx)
        if class_start == -1:
            return None

        # Already present?
        method_name = _extract_first_def_name(payload)
        if method_name and re.search(rf"\ndef {re.escape(method_name)}\b", src):
            return src

        # Insert right after the anchor method's body end
        anchor_end = _find_method_end(src, idx)
        if anchor_end == -1:
            return None
        insertion = "\n\n" + payload + "\n"
        return src[:anchor_end] + insertion + src[anchor_end:]

    return None


def _extract_first_def_name(payload: str) -> Optional[str]:
    m = re.search(r"def\s+(\w+)\s*\(", payload)
    return m.group(1) if m else None


def _find_method_end(src: str, def_start: int) -> int:
    """Given the start index of a method (`def ...`), return the index of the
    start of the next `def ` at the same indent or the end of the class."""
    # Find the indentation of this def
    line_start = src.rfind("\n", 0, def_start) + 1
    m = re.match(r"(\s*)", src[line_start:])
    indent = m.group(1) if m else "    "

    pos = def_start
    while True:
        nxt = src.find("\n" + indent + "def ", pos + 1)
        if nxt == -1:
            # End of class: next class or EOF
            nxt_class = src.find("\nclass ", pos + 1)
            nxt_top = src.find("\ndef ", pos + 1)
            candidates = [x for x in (nxt_class, nxt_top) if x != -1]
            return min(candidates) if candidates else len(src)
        return nxt


# ===========================================================================
# Replace a specific line
# ===========================================================================
def _apply_replace_line(src: str, op: PatchOp) -> Optional[str]:
    old = op.old.strip()
    new = op.new.strip()
    if not old:
        return None
    if old in src:
        return src.replace(old, new, 1)
    return None