from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent.browser_client import DeepSeekBrowser
from agent.config import AgentConfig
from agent.controller import AgentController
from agent.git_tools import GitTools
from agent.logging_setup import configure_logging
from agent.shell_tools import ShellTools
from agent.tools import ProjectTools


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DeepSeek local coding agent (headless CLI).")
    ap.add_argument("prompt", nargs="*", help="Task to run. If omitted, read from stdin.")
    ap.add_argument("--project", "-p", required=True, help="Project root directory.")
    ap.add_argument("--auto-approve", action="store_true",
                    help="Approve all tools without prompting (dangerous).")
    ap.add_argument("--deepthink", action="store_true", help="Enable DeepThink.")
    ap.add_argument("--safety", type=int, default=50,
                    help="Pacing safety level 0 (fast, risky) to 100 (slow, safest). Default 50.")
    ap.add_argument("--format", choices=["json", "xml", "text"], default="json",
                    help="Reply protocol. Default: json.")
    ap.add_argument("--debug", action="store_true", help="Verbose logs.")
    ap.add_argument("--max-rounds", type=int, default=None)
    ap.add_argument("--no-auto-refresh", action="store_true",
                    help="Disable automatic reconnect on socket errors / locked UI.")
    args = ap.parse_args(argv)

    config = AgentConfig()
    config.safety_level = max(0, min(100, int(args.safety)))
    config.apply_safety_level()
    config.format_mode = args.format
    if args.max_rounds:
        config.max_rounds = args.max_rounds
    configure_logging(debug=args.debug or config.debug)

    prompt = " ".join(args.prompt).strip() or sys.stdin.read().strip()
    if not prompt:
        print("No prompt provided.", file=sys.stderr)
        return 2

    root = Path(args.project).resolve()
    if not root.is_dir():
        print(f"Project path is not a directory: {root}", file=sys.stderr)
        return 2

    browser = DeepSeekBrowser(
        human_typing=config.human_typing,
        type_delay_min=config.type_delay_min,
        type_delay_max=config.type_delay_max,
        pre_send_pause_min=config.pre_send_pause_min,
        pre_send_pause_max=config.pre_send_pause_max,
        pre_type_pause_min=config.pre_type_pause_min,
        pre_type_pause_max=config.pre_type_pause_max,
        rate_limit_prompts=config.rate_limit_prompts,
        rate_limit_window_s=config.rate_limit_window_s,
        long_break_every=config.long_break_every,
        long_break_min=config.long_break_min,
        long_break_max=config.long_break_max,
        safety_level=config.safety_level,
    )
    try:
        browser.open_chrome()
    except Exception as e:
        print(f"Could not start Chrome: {e}", file=sys.stderr)
        return 1
    print("Chrome is open. Sign in if needed, then press Enter here...", file=sys.stderr)
    try:
        input()
    except EOFError:
        pass
    browser.mark_ready()
    browser._deepthink_on = args.deepthink
    browser.auto_refresh = not args.no_auto_refresh

    tools = ProjectTools(root, config)
    git = GitTools(root)
    shell = ShellTools(root, timeout=config.shell_timeout)
    controller = AgentController(browser, tools, git=git, shell=shell, config=config)

    def approve(description: str) -> bool:
        if args.auto_approve:
            return True
        print("\n--- Approval required ---\n" + description + "\n-------------------------", file=sys.stderr)
        try:
            answer = input("Approve? [y/N] ").strip().lower()
        except EOFError:
            return False
        return answer in {"y", "yes"}

    try:
        browser.attach()
        result = controller.run_task(
            prompt,
            auto_approve=args.auto_approve,
            approval_callback=approve,
            format_mode=config.format_mode,
        )
        print(result)
        return 0
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        browser.close()


if __name__ == "__main__":
    raise SystemExit(main())