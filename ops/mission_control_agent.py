#!/usr/bin/env python3
"""Orchestrate unified work inventory and launch Fleet Control Hub."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path


OPS_DIR = Path(__file__).resolve().parent


def run_step(name: str, cmd: list[str]) -> int:
    printable = " ".join(shlex.quote(part) for part in cmd)
    print(f"[mission-control] step={name} cmd={printable}", flush=True)
    rc = subprocess.run(cmd, check=False).returncode
    if rc == 0:
        print(f"[mission-control] step={name} status=ok", flush=True)
    else:
        print(f"[mission-control] step={name} status=error exit={rc}", file=sys.stderr, flush=True)
    return rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run chat + venture inventory agents, then launch the guarded "
            "Control Hub entry point. Unknown args are forwarded after the "
            "selected Control Hub subcommand."
        )
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Run inventory only (no HTTP dashboard serve).",
    )
    parser.add_argument(
        "--skip-chat",
        action="store_true",
        help="Skip chat workstream synthesis step.",
    )
    parser.add_argument(
        "--skip-venture",
        action="store_true",
        help="Skip venture autonomy scan step.",
    )
    parser.add_argument(
        "--venture-run-checks",
        action="store_true",
        help="Enable safe check execution in venture-agent.",
    )
    parser.add_argument(
        "--chat-top",
        type=int,
        default=12,
        help="Top threads/workstreams to emit from chat-agent.",
    )
    parser.add_argument(
        "--venture-top",
        type=int,
        default=20,
        help="Top actions to emit from venture-agent.",
    )
    return parser


def build_hub_command(
    python: str,
    hub_agent: Path,
    *,
    scan_only: bool,
    raw_hub_args: list[str],
) -> tuple[str, list[str]]:
    hub_subcommand = "scan" if scan_only else "scan-serve"
    cleaned_args = [arg for arg in raw_hub_args if arg != "--"]
    return hub_subcommand, [
        python,
        str(hub_agent),
        hub_subcommand,
        *cleaned_args,
    ]


def main() -> int:
    parser = build_parser()
    args, raw_hub_args = parser.parse_known_args()

    python = sys.executable
    chat_agent = OPS_DIR / "chat_work_agent.py"
    venture_agent = OPS_DIR / "venture_autonomy_agent.py"
    hub_agent = OPS_DIR / "control_hub_safe_entry.py"

    failures = 0
    if not args.skip_chat:
        rc = run_step(
            "chat-agent",
            [python, str(chat_agent), "--top", str(args.chat_top)],
        )
        if rc != 0:
            failures += 1

    if not args.skip_venture:
        venture_cmd = [python, str(venture_agent), "--top", str(args.venture_top)]
        if args.venture_run_checks:
            venture_cmd.append("--run-checks")
        rc = run_step("venture-agent", venture_cmd)
        if rc != 0:
            failures += 1

    hub_subcommand, hub_cmd = build_hub_command(
        python,
        hub_agent,
        scan_only=args.scan_only,
        raw_hub_args=raw_hub_args,
    )
    print(
        "[mission-control] collector_failures="
        f"{failures} (continuing to Control Hub with latest available data)",
        flush=True,
    )
    return run_step(f"control-hub:{hub_subcommand}", hub_cmd)


if __name__ == "__main__":
    raise SystemExit(main())
