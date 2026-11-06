# agent/chat_import.py
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

from .logging_setup import get_logger

log = get_logger("chat_import")

_SHARE_RE = re.compile(r"chat\.deepseek\.com/share/([a-zA-Z0-9_-]+)")
_API = "https://chat.deepseek.com/api/v0/share/content"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# ── DOM → markdown, with thinking-section skip ─────────────────────────────
_DOM_TO_MARKDOWN_JS = r"""
() => {
    function isInThinking(node) {
        let el = node;
        while (el && el !== document.body) {
            if (el.classList) {
                for (const c of el.classList) {
                    if (/think|reason|analy[sz]|reflect|internal|\bcot\b/i.test(c)) return true;
                }
            }
            if (el.getAttribute) {
                const attr = (el.getAttribute('data-message-type') || '') + ' ' +
                             (el.getAttribute('data-role') || '') + ' ' +
                             (el.getAttribute('aria-label') || '');
                if (/think|reason/i.test(attr)) return true;
            }
            el = el.parentElement;
        }
        return false;
    }

    function mdFromNode(node) {
        if (node.nodeType === Node.TEXT_NODE) return node.textContent;
        if (node.nodeType !== Node.ELEMENT_NODE) return '';
        const tag = node.tagName.toLowerCase();

        if (tag === 'pre') {
            const code = node.querySelector('code');
            const cls = code ? (code.className || '') : '';
            const m = cls.match(/language-([\w+-]+)/);
            const lang = m ? m[1] : '';
            let text = code ? code.textContent : node.textContent;
            text = text.replace(/\n+$/, '');
            return '\n```' + lang + '\n' + text + '\n```\n';
        }
        if (tag === 'code') {
            const parent = node.parentElement;
            if (parent && parent.tagName.toLowerCase() === 'pre') return node.textContent;
            return '`' + node.textContent + '`';
        }
        if (tag === 'strong' || tag === 'b') return '**' + node.textContent + '**';
        if (tag === 'em' || tag === 'i') return '*' + node.textContent + '*';
        if (/^h[1-6]$/.test(tag)) {
            const level = tag[1];
            return '\n' + '#'.repeat(+level) + ' ' + node.textContent + '\n';
        }
        if (tag === 'br') return '\n';
        if (tag === 'hr') return '\n---\n';
        if (tag === 'p') {
            let out = '';
            for (const c of node.childNodes) out += mdFromNode(c);
            return out + '\n\n';
        }
        if (tag === 'ul' || tag === 'ol') {
            let out = '\n';
            let idx = 1;
            for (const c of node.childNodes) {
                if (c.nodeType !== Node.ELEMENT_NODE) continue;
                if (c.tagName.toLowerCase() !== 'li') continue;
                let line = '';
                for (const gc of c.childNodes) line += mdFromNode(gc);
                line = line.replace(/\n+$/, '');
                out += (tag === 'ol' ? (idx++) + '. ' : '- ') + line + '\n';
            }
            return out + '\n';
        }
        if (tag === 'a') {
            const href = node.getAttribute('href') || '';
            return '[' + node.textContent + '](' + href + ')';
        }
        let out = '';
        for (const c of node.childNodes) out += mdFromNode(c);
        return out;
    }

    function stripThinkingPrefix(text) {
        const starts = [
            /^\s*The user wants\b/i,
            /^\s*The user is\b/i,
            /^\s*Let me (?:plan|think|now|first)\b/i,
            /^\s*I need to\b/i,
            /^\s*I should\b/i,
            /^\s*I(?:'|’)ll now\b/i,
            /^\s*Looking at\b/i,
            /^\s*OK,?\s*let/i,
            /^\s*Alright,?\s*let/i,
            /^\s*AssistantThe user\b/i,
        ];
        for (const pat of starts) {
            if (pat.test(text)) {
                // Find the first markdown header or code fence after a
                // short preamble — that's where the real answer starts.
                const m = text.match(/\n(?=#{1,4}\s|```)/);
                if (m && m.index > 40) {
                    return text.slice(m.index + 1);
                }
                // No boundary found — drop the whole thing (it was only reasoning)
                return '';
            }
        }
        return text;
    }

    const all = document.querySelectorAll('.ds-markdown, [class*="ds-markdown"]');
    const results = [];
    for (const b of all) {
        // Skip nested blocks
        let isNested = false;
        let p = b.parentElement;
        while (p) {
            if (p.classList && (p.classList.contains('ds-markdown') ||
                (p.className || '').indexOf('ds-markdown') !== -1)) {
                isNested = true; break;
            }
            p = p.parentElement;
        }
        if (isNested) continue;

        // Skip thinking containers
        if (isInThinking(b)) continue;

        let md = mdFromNode(b).trim();
        if (!md) continue;

        md = stripThinkingPrefix(md);
        if (md.trim()) results.push(md.trim());
    }
    return results;
}
"""


def extract_share_id(url: str) -> str | None:
    m = _SHARE_RE.search(url)
    return m.group(1) if m else None


def _strip_thinking_prefix_text(text: str) -> str:
    """Text-level fallback when the API returns raw combined content."""
    if not text:
        return text
    starts = [
        r"^\s*The user wants\b",
        r"^\s*The user is\b",
        r"^\s*Let me (?:plan|think|now|first)\b",
        r"^\s*I need to\b",
        r"^\s*I should\b",
        r"^\s*I['’]ll now\b",
        r"^\s*Looking at\b",
        r"^\s*OK,?\s*let",
        r"^\s*Alright,?\s*let",
        r"^\s*AssistantThe user\b",
    ]
    for pat in starts:
        if re.match(pat, text, re.I):
            m = re.search(r"\n(?=#{1,4}\s|```)", text)
            if m and m.start() > 40:
                return text[m.start() + 1:]
            return ""
    return text


# ── API path ───────────────────────────────────────────────────────────────
def _api_extract(share_id: str, timeout: int = 20) -> list[dict[str, str]]:
    endpoint = f"{_API}?{urllib.parse.urlencode({'share_id': share_id})}"
    req = urllib.request.Request(
        endpoint,
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.info("share API request failed: %s", e)
        return []

    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []

    data = payload.get("data") or payload
    fragments = data.get("chat_messages") or data.get("messages") or data
    if isinstance(fragments, dict):
        fragments = fragments.get("messages") or fragments.get("fragments") or []
    if not isinstance(fragments, list):
        return []

    def _content_to_md(content) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for frag in content:
                if not isinstance(frag, dict):
                    parts.append(str(frag))
                    continue
                ftype = (frag.get("type") or "").lower()
                # Skip thinking fragments explicitly
                if ftype in ("thinking", "reasoning", "thought", "analysis"):
                    continue
                if ftype == "code":
                    lang = frag.get("language") or frag.get("lang") or ""
                    code = frag.get("content") or frag.get("text") or ""
                    parts.append(f"```{lang}\n{str(code).rstrip()}\n```")
                else:
                    parts.append(str(frag.get("text") or frag.get("content") or ""))
            return "\n\n".join(p for p in parts if p)
        return str(content)

    out: list[dict[str, str]] = []
    for frag in fragments:
        if not isinstance(frag, dict):
            continue
        role = (frag.get("role") or frag.get("author") or "").lower()
        if role not in {"user", "assistant", "human", "ai"}:
            ftype = (frag.get("type") or "").lower()
            if ftype in {"request", "user"}:
                role = "user"
            elif ftype in {"response", "assistant"}:
                role = "assistant"
            elif ftype in {"thinking", "reasoning", "thought"}:
                continue
            else:
                continue
        content = _content_to_md(frag.get("content") or frag.get("text") or "")
        if not content.strip():
            continue
        if role in {"assistant", "ai"}:
            content = _strip_thinking_prefix_text(content)
            if not content.strip():
                continue
        out.append({
            "role": "user" if role in {"user", "human"} else "assistant",
            "content": content,
        })
    return out


# ── Playwright path ────────────────────────────────────────────────────────
def _playwright_extract(url: str, headed: bool = False) -> list[dict[str, str]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "Playwright is not installed. Run: "
            "pip install playwright && python -m playwright install chromium"
        ) from e

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=_USER_AGENT,
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for sel in (".ds-markdown", "[class*='markdown']", "pre"):
                try:
                    page.wait_for_selector(sel, timeout=12000)
                    break
                except Exception:
                    continue
            page.wait_for_timeout(2500)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)

            markdowns = page.evaluate(_DOM_TO_MARKDOWN_JS)
            if markdowns:
                return [
                    {"role": "assistant", "content": md}
                    for md in markdowns if md and md.strip()
                ]

            body = page.inner_text("body")
            if body.strip():
                body = _strip_thinking_prefix_text(body)
                return [{"role": "assistant", "content": body.strip()}] if body.strip() else []
            return []
        finally:
            browser.close()


# ── Public API ─────────────────────────────────────────────────────────────
def import_from_share_url(url: str, timeout: int = 20) -> list[dict[str, str]]:
    sid = extract_share_id(url)
    if not sid:
        raise RuntimeError("Not a DeepSeek share URL.")

    messages = _api_extract(sid, timeout=timeout)
    if messages:
        log.info("share API returned %d message(s)", len(messages))
        return messages

    log.info("share API empty; rendering with Playwright")
    messages = _playwright_extract(url)
    if not messages:
        raise RuntimeError(
            "No messages could be extracted. The share link may be private, "
            "expired, or the page layout has changed."
        )
    return messages


def import_from_pasted_text(text: str) -> list[dict[str, str]]:
    pattern = re.compile(
        r"^\s*(?:\*\*)?(User|You|Human|Assistant|AI|DeepSeek)(?:\*\*)?\s*[:\-]\s*",
        re.I | re.M,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return [{"role": "assistant", "content": _strip_thinking_prefix_text(text).strip()}]
    out: list[dict[str, str]] = []
    for i, m in enumerate(matches):
        role_raw = m.group(1).lower()
        role = "user" if role_raw in {"user", "you", "human"} else "assistant"
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if role == "assistant":
            content = _strip_thinking_prefix_text(content)
        if content:
            out.append({"role": role, "content": content})
    return out


def import_via_browser(browser, url: str) -> list[dict[str, str]]:
    if browser is None:
        raise RuntimeError("No browser session available.")
    browser.attach()
    try:
        if not browser.open_chat(url):
            raise RuntimeError("Could not open the share page in the browser.")
        time.sleep(3)
        try:
            markdowns = browser.page.evaluate(_DOM_TO_MARKDOWN_JS)
        except Exception:
            markdowns = []
        if markdowns:
            return [
                {"role": "assistant", "content": md}
                for md in markdowns if md and md.strip()
            ]
        texts = browser._assistant_texts()
        if texts:
            cleaned = []
            for t in texts:
                s = _strip_thinking_prefix_text(t)
                if s.strip():
                    cleaned.append({"role": "assistant", "content": s})
            if cleaned:
                return cleaned
        raise RuntimeError("No messages found on the page.")
    finally:
        try:
            browser.close()
        except Exception:
            pass