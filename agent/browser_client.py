from __future__ import annotations

import random
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from .config import BROWSER_TIMEOUT, pacing_from_safety
from .errors import BrowserError
from .logging_setup import get_logger

log = get_logger("browser")


DEEPSEEK_URL = "https://chat.deepseek.com/"

INPUT_SELECTORS = [
    "textarea[placeholder]",
    "textarea",
    '[contenteditable="true"]',
    '[role="textbox"]',
    ".ProseMirror",
    '[data-testid*="chat-input"]',
    '[data-testid*="message-input"]',
]

ASSISTANT_SELECTORS = [
    ".ds-markdown",
    '[class*="markdown"]',
    '[data-message-author-role="assistant"]',
    '[class*="message"][class*="assistant"]',
]

STOP_BUTTON_SELECTORS = [
    'button[aria-label*="Stop"]',
    'button:has-text("Stop")',
    'div[role="button"]:has-text("Stop")',
    '[data-testid*="stop"]',
]

DEEPTHINK_SELECTORS = [
    'button:has-text("DeepThink")',
    'div[role="button"]:has-text("DeepThink")',
    'button:has-text("Deep Think")',
    'div[role="button"]:has-text("Deep Think")',
    'button:has-text("R1")',
    'div[role="button"]:has-text("R1")',
    'button[aria-pressed]:has-text("DeepThink")',
    'button[data-testid*="deepthink"]',
    'button[data-testid*="reason"]',
    'button[class*="deepthink"]',
    'button[class*="reason"]',
]

NEW_CHAT_SELECTORS = [
    'div[tabindex="0"]:has(> span:text-is("New chat"))',
    'div[tabindex="0"]:has(span:text-is("New chat"))',
    'span:text-is("New chat")',
    'span:text-is("新对话")',
    'div[tabindex="0"]:has(> span:text-is("新对话"))',
    'a[aria-label="New chat"]',
    'button[aria-label="New chat"]',
    '[aria-label="New chat"]',
    'a[title="New chat"]',
    'button[title="New chat"]',
    'button:has-text("New chat")',
    'div[role="button"]:has-text("New chat")',
    'a:has-text("New chat")',
    'button:has-text("新对话")',
    '[data-testid*="new-chat"]',
    '[data-testid*="newchat"]',
    '[data-testid*="new_chat"]',
]


class DeepSeekBrowser:
    """Launches Chrome once; each worker thread attaches via CDP and detaches when done.

    Pacing is controlled by ``safety_level`` (0–100). All the individual
    timing knobs are derived from it via :func:`agent.config.pacing_from_safety`
    and can also be overridden directly in the constructor (used by the CLI).
    """

    def __init__(
        self,
        debug_port: int = 9222,
        profile_dir: Path | None = None,
        chrome_path: str | None = None,
        *,
        human_typing: bool = True,
        type_delay_min: int = 15,
        type_delay_max: int = 45,
        pre_send_pause_min: float = 0.5,
        pre_send_pause_max: float = 1.6,
        pre_type_pause_min: float = 0.5,
        pre_type_pause_max: float = 1.3,
        rate_limit_prompts: int = 3,
        rate_limit_window_s: int = 65,
        long_break_every: int = 18,
        long_break_min: float = 6.5,
        long_break_max: float = 21.5,
        safety_level: int = 50,
    ):
        self.debug_port = debug_port
        self.profile_dir = profile_dir or (
            Path.home() / "AppData" / "Local" / "DeepSeekLocalAgent" / "chrome-profile"
        )
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.chrome_path = chrome_path or r"C:\Program Files\Google\Chrome\Application\chrome.exe"

        # pacing knobs
        self.safety_level = max(0, min(100, int(safety_level)))
        self.human_typing = bool(human_typing)
        self.type_delay_min = int(type_delay_min)
        self.type_delay_max = int(type_delay_max)
        self.pre_send_pause_min = float(pre_send_pause_min)
        self.pre_send_pause_max = float(pre_send_pause_max)
        self.pre_type_pause_min = float(pre_type_pause_min)
        self.pre_type_pause_max = float(pre_type_pause_max)
        self.rate_limit_prompts = int(rate_limit_prompts)
        self.rate_limit_window_s = int(rate_limit_window_s)
        self.long_break_every = int(long_break_every)
        self.long_break_min = float(long_break_min)
        self.long_break_max = float(long_break_max)

        # runtime state
        self._recent_prompts: list[float] = []
        self._prompts_since_break = 0

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.ready = False
        self._deepthink_on = False

    # ----------------------------------------------------------- pacing API
    def set_safety_level(self, level: int) -> None:
        """Recompute every pacing knob from a 0–100 safety level."""
        self.safety_level = max(0, min(100, int(level)))
        for k, v in pacing_from_safety(self.safety_level).items():
            setattr(self, k, v)
        log.info(
            "pacing set: safety=%d type_delay=%d-%dms pre_type=%.2f-%.2fs "
            "pre_send=%.2f-%.2fs rate=%d/%ds long_break=%d every",
            self.safety_level, self.type_delay_min, self.type_delay_max,
            self.pre_type_pause_min, self.pre_type_pause_max,
            self.pre_send_pause_min, self.pre_send_pause_max,
            self.rate_limit_prompts, self.rate_limit_window_s, self.long_break_every,
        )

    def set_pacing(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)

    # ----------------------------------------------------------- lifecycle
    def _port_ready(self, timeout_s: int = 30) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.debug_port), timeout=1):
                    return True
            except OSError:
                time.sleep(0.5)
        return False

    def open_chrome(self) -> None:
        chrome = Path(self.chrome_path)
        if not chrome.exists():
            raise BrowserError(
                f"Chrome not found at {chrome}. Edit `chrome_path` or install Chrome."
            )
        if self._port_ready(timeout_s=2):
            return
        subprocess.Popen([
            str(chrome),
            f"--remote-debugging-port={self.debug_port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            DEEPSEEK_URL,
        ])
        if not self._port_ready():
            raise BrowserError(
                "Chrome debugging port did not become available. "
                "Close other Chrome windows using the same profile and retry."
            )

    def mark_ready(self) -> None:
        self.ready = True

    def attach(self) -> None:
        if self.playwright:
            return
        try:
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self.debug_port}"
            )
            if not self.browser.contexts:
                raise BrowserError("Connected to Chrome but no contexts are open.")
            self.context = self.browser.contexts[0]
            self.page = self._find_deepseek_page()
            self.set_deepthink(self._deepthink_on)
            log.info("attached: %s", self.page.url if self.page else "(no page)")
        except Exception as e:
            self.close()
            raise BrowserError(f"Could not attach to Chrome via CDP: {e}") from e

    def _find_deepseek_page(self):
        for p in self.context.pages:
            try:
                if "deepseek.com" in (p.url or ""):
                    return p
            except Exception:
                continue
        if self.context.pages:
            return self.context.pages[0]
        return self.context.new_page()

    def close(self) -> None:
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    # ----------------------------------------------------------- helpers
    def _first_visible(self, selectors: list[str], allow_disabled: bool = False):
        if not self.page:
            return None
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                n = loc.count()
                if not n:
                    continue
                for i in range(n):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible():
                            continue
                        if not allow_disabled and not el.is_enabled():
                            continue
                        return el
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    def _click_first(
        self, selectors: list[str], wait_after: float = 0.5,
        timeout_s: float = 10.0, allow_disabled: bool = False,
    ) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            el = self._first_visible(selectors, allow_disabled=allow_disabled)
            if el is not None:
                try:
                    el.click(timeout=3000)
                    time.sleep(wait_after)
                    return True
                except Exception as e:
                    log.debug("click failed (%s), retrying", e)
            time.sleep(0.4)
        return False

    # ----------------------------------------------------------- deepthink
    def set_deepthink(self, on: bool) -> bool:
        self._deepthink_on = bool(on)
        if not self.page:
            return self._deepthink_on
        btn = self._first_visible(DEEPTHINK_SELECTORS)
        if btn is None:
            log.debug("deepthink toggle not found")
            return self._deepthink_on
        try:
            current = self.is_deepthink_on()
            if current == on:
                return current
            btn.click()
            time.sleep(0.4)
            return self.is_deepthink_on() if self.is_deepthink_on() is not None else on
        except Exception as e:
            log.debug("deepthink toggle error: %s", e)
            return self._deepthink_on

    def is_deepthink_on(self) -> bool | None:
        btn = self._first_visible(DEEPTHINK_SELECTORS)
        if btn is None:
            return None
        try:
            for attr in ("aria-pressed", "aria-checked"):
                v = btn.get_attribute(attr)
                if v in ("true", "false"):
                    return v == "true"
            cls = (btn.get_attribute("class") or "").lower()
            return any(k in cls for k in ("active", "selected", "on"))
        except Exception:
            return None

    # ----------------------------------------------------------- new chat
    def new_chat(self) -> bool:
        if not self.page:
            raise BrowserError("Not attached to a page.")
        try:
            if "deepseek.com" not in (self.page.url or ""):
                self.page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=30000)
                time.sleep(1.0)
        except Exception as e:
            log.debug("navigation to DeepSeek failed: %s", e)

        deadline = time.time() + 12.0
        while time.time() < deadline:
            for locator_spec in (
                self.page.get_by_text("New chat", exact=True),
                self.page.get_by_text("新对话", exact=True),
            ):
                try:
                    if locator_spec.count() == 0:
                        continue
                    el = locator_spec.first
                    if not el.is_visible():
                        continue
                    handle = el.evaluate_handle(
                        "node => node.closest('[tabindex=\"0\"],button,a,[role=\"button\"]') || node"
                    )
                    el_to_click = handle.as_element()
                    if el_to_click is not None:
                        el_to_click.click(timeout=3000)
                        time.sleep(0.8)
                        return True
                    el.click(timeout=3000)
                    time.sleep(0.8)
                    return True
                except Exception as e:
                    log.debug("new_chat click attempt failed: %s", e)
            time.sleep(0.4)

        ok = self._click_first(
            NEW_CHAT_SELECTORS, wait_after=0.8, timeout_s=4.0, allow_disabled=True
        )
        if not ok:
            try:
                counts = []
                for sel in NEW_CHAT_SELECTORS:
                    try:
                        counts.append(f"{sel}: {self.page.locator(sel).count()}")
                    except Exception as e:
                        counts.append(f"{sel}: err {e}")
                log.info(
                    "new_chat failed on url=%r; selector counts:\n%s",
                    self.page.url, "\n".join(counts),
                )
            except Exception:
                pass
        return ok

    # ----------------------------------------------------------- input
    def _find_input(self, timeout_s: int = 15):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            for sel in INPUT_SELECTORS:
                try:
                    loc = self.page.locator(sel)
                    for i in reversed(range(loc.count())):
                        el = loc.nth(i)
                        try:
                            if el.is_visible() and el.is_editable():
                                return el
                        except Exception:
                            continue
                except Exception:
                    continue
            time.sleep(0.4)
        diag = []
        for sel in INPUT_SELECTORS:
            try:
                diag.append(f"{sel}: {self.page.locator(sel).count()}")
            except Exception as e:
                diag.append(f"{sel}: {e}")
        raise BrowserError(
            "Could not locate the DeepSeek chat input.\n"
            f"URL: {self.page.url if self.page else '(no page)'}\n"
            + "\n".join(diag)
        )

    def _assistant_texts(self) -> list[str]:
        if not self.page:
            return []
        for sel in ASSISTANT_SELECTORS:
            try:
                loc = self.page.locator(sel)
                if loc.count():
                    texts = [t.strip() for t in loc.all_inner_texts() if t.strip()]
                    if texts:
                        return texts
            except Exception:
                continue
        return []

    def _is_streaming(self) -> bool:
        return self._first_visible(STOP_BUTTON_SELECTORS) is not None

    # ----------------------------------------------------------- chat history
    def current_url(self) -> str:
        if not self.page:
            return ""
        try:
            return self.page.url or ""
        except Exception:
            return ""

    def list_sidebar_chats(self, limit: int = 100) -> list[dict[str, str]]:
        if not self.page:
            raise BrowserError("Not attached to a page.")
        try:
            loc = self.page.locator('a[href*="/chat/s/"]')
            n = loc.count()
        except Exception:
            return []
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for i in range(min(n, limit)):
            try:
                el = loc.nth(i)
                href = el.get_attribute("href") or ""
                if not href or href in seen:
                    continue
                seen.add(href)
                try:
                    title = (el.inner_text() or "").strip().splitlines()[0][:120]
                except Exception:
                    title = ""
                rows.append({"title": title or href.rsplit("/", 1)[-1], "href": href})
            except Exception:
                continue
        return rows

    def open_chat(self, href_or_url: str) -> bool:
        if not self.page:
            raise BrowserError("Not attached to a page.")
        target = href_or_url.strip()
        if not target:
            return False
        if target.startswith("/"):
            target = "https://chat.deepseek.com" + target
        elif not target.startswith("http"):
            target = "https://chat.deepseek.com/" + target.lstrip("/")
        try:
            self.page.goto(target, wait_until="domcontentloaded", timeout=30000)
            time.sleep(1.0)
            try:
                self.set_deepthink(self._deepthink_on)
            except Exception:
                pass
            log.info("opened chat %s", target)
            return True
        except Exception as e:
            log.info("open_chat failed: %s", e)
            return False

    # ----------------------------------------------------------- attachments
    def _find_file_input(self, timeout_s: float = 8.0):
        if not self.page:
            return None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                loc = self.page.locator('input[type="file"]')
                if loc.count():
                    for i in range(loc.count()):
                        el = loc.nth(i)
                        try:
                            return el
                        except Exception:
                            continue
            except Exception:
                pass
            time.sleep(0.3)
        return None

    def upload_files(self, paths: list[str]) -> int:
        if not self.page:
            raise BrowserError("Not attached to a page.")
        existing = [p for p in paths if Path(p).is_file()]
        if not existing:
            return 0
        el = self._find_file_input()
        if el is None:
            raise BrowserError(
                "Could not find DeepSeek's file input. The upload button may have "
                "moved; open a chat and check the DOM for input[type=file]."
            )
        el.set_input_files(existing)
        time.sleep(min(1.5 + 0.3 * len(existing), 5.0))
        log.info("uploaded %d file(s)", len(existing))
        return len(existing)

    # ----------------------------------------------------------- pacing primitives
    @staticmethod
    def _human_pause(lo: float, hi: float) -> None:
        lo = max(0.0, float(lo))
        hi = max(lo, float(hi))
        if hi <= 0:
            return
        time.sleep(random.uniform(lo, hi))

    def _type_humanly(self, box, text: str) -> None:
        """Paste the prompt in chunks with random pauses between them.

        - safety 0–5 or ``human_typing=False``: single paste, no pauses.
        - otherwise: chunk size and inter-chunk delay scale with safety.
          Small prompts are effectively pasted at once; large prompts are
          split into 30–900 character pastes with random 15–80 ms pauses.
        """
        try:
            box.click()
        except Exception:
            pass
        try:
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Delete")
        except Exception:
            pass

        if not self.human_typing or self.safety_level <= 5:
            try:
                box.fill(text)
            except Exception:
                try:
                    self.page.keyboard.type(text, delay=1)
                except Exception:
                    self.page.keyboard.insert_text(text)
            return

        t = max(0, min(100, int(self.safety_level))) / 100.0
        chunk_min = max(20, int(500 - t * 470))     # 500 .. 30 chars
        chunk_max = max(chunk_min + 20, int(1200 - t * 1100))  # 1200 .. 100 chars
        delay_min = max(0.0, self.type_delay_min / 1000.0)
        delay_max = max(delay_min, self.type_delay_max / 1000.0)

        n = len(text)
        i = 0
        while i < n:
            size = random.randint(chunk_min, chunk_max)
            chunk = text[i:i + size]
            try:
                self.page.keyboard.insert_text(chunk)
            except Exception:
                try:
                    box.fill(text)
                except Exception:
                    pass
                return
            i += size
            if i < n:
                time.sleep(random.uniform(delay_min, delay_max))

    def _throttle_before_send(self) -> None:
        # Long break after N prompts.
        if self.long_break_every > 0:
            self._prompts_since_break += 1
            if self._prompts_since_break >= self.long_break_every:
                lo = max(0.0, float(self.long_break_min))
                hi = max(lo, float(self.long_break_max))
                pause = random.uniform(lo, hi) if hi > lo else lo
                log.info("long break: %.1fs after %d prompts", pause, self._prompts_since_break)
                time.sleep(pause)
                self._prompts_since_break = 0

        # Sliding-window rate limit.
        if self.rate_limit_prompts > 0 and self.rate_limit_window_s > 0:
            now = time.time()
            cutoff = now - self.rate_limit_window_s
            self._recent_prompts = [t for t in self._recent_prompts if t > cutoff]
            if len(self._recent_prompts) >= self.rate_limit_prompts:
                wait = (self._recent_prompts[0] + self.rate_limit_window_s) - now
                wait = max(0.5, wait) + random.uniform(0.5, 2.5)
                log.info("rate limit: waiting %.1fs before next prompt", wait)
                time.sleep(wait)

    # ----------------------------------------------------------- send
    def send_and_wait(self, prompt: str, timeout: int = BROWSER_TIMEOUT,
                      attachments: list[str] | None = None) -> str:
        if not self.page:
            raise BrowserError("Not attached to a page.")

        try:
            self.set_deepthink(self._deepthink_on)
        except Exception:
            pass

        before = self._assistant_texts()
        before_last = before[-1] if before else ""

        if attachments:
            try:
                self.upload_files(attachments)
            except Exception as e:
                log.info("attachment upload failed: %s", e)
                raise

        # Rate limiting + long break happen *before* we start typing so the
        # window doesn't sit idle with a huge prompt already in the box.
        self._throttle_before_send()

        box = self._find_input()

        # Pause as if the human is reading / thinking before pasting.
        self._human_pause(self.pre_type_pause_min, self.pre_type_pause_max)

        self._type_humanly(box, prompt)

        # Random pause before pressing Enter (reviewing the prompt).
        self._human_pause(self.pre_send_pause_min, self.pre_send_pause_max)

        box.press("Enter")
        self._recent_prompts.append(time.time())
        log.info("sent prompt len=%d", len(prompt))

        start = time.time()
        last = ""
        stable_since = None
        saw_streaming = False
        poll_interval = 0.6

        while time.time() - start < timeout:
            self.page.wait_for_timeout(int(poll_interval * 1000))
            texts = self._assistant_texts()
            streaming = self._is_streaming()
            if streaming:
                saw_streaming = True

            if not texts:
                continue
            current = texts[-1]
            if current == before_last:
                continue

            if current != last:
                last = current
                stable_since = None
                continue

            if not streaming:
                stable_since = stable_since or time.time()
                required = 2.0 if saw_streaming else 3.0
                if time.time() - stable_since >= required:
                    log.info("reply received len=%d", len(current))
                    return current

        raise BrowserError(f"Timed out after {timeout}s waiting for a DeepSeek reply.")