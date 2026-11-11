from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn",
    ".venv", "venv", "env", ".env.d",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".deepseek_agent_backups",
    "dist", "build", ".next", ".nuxt", "target",
})

SENSITIVE_NAME_PARTS = frozenset({
    ".env", ".pem", ".key", ".pfx", ".p12", "id_rsa", "id_ed25519",
    "credentials", "secrets",
})

MAX_FILE_BYTES = 1_000_000
# Files larger than this are attached as uploads instead of pasted into the prompt.
MAX_INLINE_READ_BYTES = 40_000
MAX_SEARCH_FILE_BYTES = 500_000
MAX_LIST_RESULTS = 400
MAX_SEARCH_HITS = 200
MAX_WRITE_BYTES = 1_000_000
MAX_SHELL_OUTPUT = 16_000
MAX_TOOL_CALLS_PER_ROUND = 12
MAX_ROUNDS = 20
SHELL_TIMEOUT = 120
BROWSER_TIMEOUT = 300
SESSIONS_FILE = None  # optional Path override for SessionStore


def pacing_from_safety(level: int) -> dict:
    """Translate a 0–100 safety slider into concrete pacing parameters.

    0   = fast (minimum delay, no rate limiting, may get flagged)
    50  = balanced default
    100 = safest (slow chunked typing, long random pauses, strict rate limits)
    """
    level = max(0, min(100, int(level)))
    t = level / 100.0

    # Per-chunk delay (milliseconds) between paste chunks.
    td_min = int(t * 30)
    td_max = int(t * 80) + 5
    if td_max <= td_min:
        td_max = td_min + 1

    # Pause before typing and before pressing Enter (seconds).
    pt_min = round(t * 1.0, 2)
    pt_max = round(t * 2.5 + 0.05, 2)
    ps_min = round(t * 1.0, 2)
    ps_max = round(t * 3.0 + 0.1, 2)

    # Sliding-window rate limit: at most N prompts per W seconds.
    if level < 30:
        rl_prompts, rl_window = 0, 0
    elif level < 60:
        rl_prompts, rl_window = 3, int(45 + (level - 30) * 1.0)   # 45..75
    elif level < 85:
        rl_prompts, rl_window = 2, int(75 + (level - 60) * 1.5)   # 75..112
    else:
        rl_prompts, rl_window = 2, int(112 + (level - 85) * 3.0)  # 112..157

    # Occasional long break after N prompts.
    if level < 40:
        lb_every, lb_min, lb_max = 0, 0.0, 0.0
    else:
        lb_every = max(4, int(20 - (level - 40) * 0.13))
        lb_min = round(5 + (level - 40) * 0.15, 2)
        lb_max = round(15 + (level - 40) * 0.65, 2)

    return {
        "human_typing": level > 0,
        "type_delay_min": td_min,
        "type_delay_max": td_max,
        "pre_type_pause_min": pt_min,
        "pre_type_pause_max": pt_max,
        "pre_send_pause_min": ps_min,
        "pre_send_pause_max": ps_max,
        "rate_limit_prompts": rl_prompts,
        "rate_limit_window_s": rl_window,
        "long_break_every": lb_every,
        "long_break_min": lb_min,
        "long_break_max": lb_max,
    }


@dataclass
class AgentConfig:
    max_rounds: int = MAX_ROUNDS
    max_tool_calls_per_round: int = MAX_TOOL_CALLS_PER_ROUND
    shell_timeout: int = SHELL_TIMEOUT
    browser_timeout: int = BROWSER_TIMEOUT
    auto_approve: bool = False
    debug: bool = field(default_factory=lambda: os.environ.get("DEEPSEEK_DEBUG") == "1")
    ignored_dirs: frozenset = DEFAULT_IGNORED_DIRS

    # --- pacing / anti-detection -----------------------------------------
    safety_level: int = 50
    human_typing: bool = True
    type_delay_min: int = 15
    type_delay_max: int = 45
    pre_type_pause_min: float = 0.5
    pre_type_pause_max: float = 1.3
    pre_send_pause_min: float = 0.5
    pre_send_pause_max: float = 1.6
    rate_limit_prompts: int = 3
    rate_limit_window_s: int = 65
    long_break_every: int = 18
    long_break_min: float = 6.5
    long_break_max: float = 21.5

    # --- reply protocol ---------------------------------------------------
    format_mode: str = "json"  # "json" | "xml" | "text"

    def apply_safety_level(self) -> None:
        for k, v in pacing_from_safety(self.safety_level).items():
            setattr(self, k, v)