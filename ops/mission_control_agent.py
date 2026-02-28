#!/usr/bin/env python3
"""Orchestrate unified work inventory and launch Fleet Control Hub."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path


OPS_DIR = Path(__file__).resolve().parent
CONTROL_HUB_GLOBAL_OPTS_WITH_VALUE = {
    "--db",
    "--projects-root",
    "--linear-team-id",
    "--chat-work-json",
    "--venture-report-json",
}


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
    p = argparse.ArgumentParser(
        description=(
            "Run chat + venture inventory agents, then launch Control Hub scan/serve. "
            "Unknown args are forwarded to control_hub_agent.py."
        )
    )
    p.add_argument("--scan-only", action="store_true", help="Run inventory only (no HTTP dashboard serve).")
    p.add_argument("--skip-chat", action="store_true", help="Skip chat workstream synthesis step.")
    p.add_argument("--skip-venture", action="store_true", help="Skip venture autonomy scan step.")
    p.add_argument("--venture-run-checks", action="store_true", help="Enable safe check execution in venture-agent.")
    p.add_argument("--chat-top", type=int, default=12, help="Top threads/workstreams to emit from chat-agent.")
    p.add_argument("--venture-top", type=int, default=20, help="Top actions to emit from venture-agent.")
    return p


def split_control_hub_args(raw_args: list[str]) -> tuple[list[str], list[str]]:
    """Place known Control Hub globals before subcommand; keep others after."""
    global_args: list[str] = []
    passthrough_args: list[str] = []
    i = 0
    while i < len(raw_args):
        arg = raw_args[i]
        if arg == "--":
            i += 1
            continue

        opt_name, has_equals, _ = arg.partition("=")
        if opt_name in CONTROL_HUB_GLOBAL_OPTS_WITH_VALUE:
            if has_equals:
                global_args.append(arg)
                i += 1
                continue
            if i + 1 < len(raw_args):
                global_args.extend([arg, raw_args[i + 1]])
                i += 2
                continue

        passthrough_args.append(arg)
        i += 1

    return global_args, passthrough_args


def main() -> int:
    parser = build_parser()
    args, raw_hub_args = parser.parse_known_args()
    hub_global_args, hub_passthrough_args = split_control_hub_args(raw_hub_args)

    python = sys.executable
    chat_agent = OPS_DIR / "chat_work_agent.py"
    venture_agent = OPS_DIR / "venture_autonomy_agent.py"
    hub_agent = OPS_DIR / "control_hub_agent.py"

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

    hub_subcommand = "scan" if args.scan_only else "scan-serve"
    if args.scan_only and (hub_global_args or hub_passthrough_args):
        print("[mission-control] scan-only ignores Control Hub passthrough args.", flush=True)
        hub_global_args = []
        hub_passthrough_args = []
    hub_cmd = [python, str(hub_agent), *hub_global_args, hub_subcommand, *hub_passthrough_args]
    print(
        "[mission-control] collector_failures="
        f"{failures} (continuing to Control Hub with latest available data)",
        flush=True,
    )
    return run_step(f"control-hub:{hub_subcommand}", hub_cmd)


if __name__ == "__main__":
    raise SystemExit(main())
