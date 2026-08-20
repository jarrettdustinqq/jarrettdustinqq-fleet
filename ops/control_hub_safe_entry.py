#!/usr/bin/env python3
"""Safe command-line entry point for Fleet Control Hub operations.

This wrapper keeps scan-related options on the subcommands used by ``fleetctl``
and refuses inventory when no configured projects root can be observed. Healthy
roots remain independently usable when a peer root is unavailable. The
underlying dashboard implementation remains in ``control_hub_agent.py``.
"""

from __future__ import annotations

import argparse
import os
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Sequence

import control_hub_agent as hub


_CORE_RUN_SCAN = hub.run_scan
_CORE_HUB_HANDLER = hub.HubHandler


class ScanRefusedError(RuntimeError):
    """Raised when a scan cannot safely establish its observation boundary."""


def add_common_inventory_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        default=hub.DEFAULT_DB,
        help=f"SQLite DB path (default: {hub.DEFAULT_DB})",
    )
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=hub.DEFAULT_PROJECTS_ROOT,
        help=f"Projects root to inventory (default: {hub.DEFAULT_PROJECTS_ROOT})",
    )
    parser.add_argument(
        "--additional-projects-root",
        type=Path,
        action="append",
        default=[],
        help=(
            "Additional independent repository observation root; repeat for "
            f"multiple roots (maximum {hub.MAX_PROJECTS_ROOTS} total)."
        ),
    )
    parser.add_argument(
        "--repo-registry",
        type=Path,
        default=None,
        help=(
            "Continuity repo-registry.json path (default: "
            "<projects-root>/continuity/repo-registry.json; "
            "env: CONTINUITY_REPO_REGISTRY)"
        ),
    )
    parser.add_argument(
        "--linear-team-id",
        default=os.environ.get("LINEAR_TEAM_ID"),
        help="Optional Linear team ID filter. Defaults to LINEAR_TEAM_ID env.",
    )
    parser.add_argument(
        "--chat-work-json",
        type=Path,
        default=hub.DEFAULT_CHAT_WORK_JSON,
        help=f"Chat workstream report path (default: {hub.DEFAULT_CHAT_WORK_JSON}).",
    )
    parser.add_argument(
        "--venture-report-json",
        type=Path,
        default=hub.DEFAULT_VENTURE_REPORT_JSON,
        help=f"Venture autonomy report path (default: {hub.DEFAULT_VENTURE_REPORT_JSON}).",
    )


def add_serve_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--no-window-tracking",
        action="store_true",
        help="Disable live active-window tracking.",
    )
    parser.add_argument(
        "--window-poll-seconds",
        type=float,
        default=hub.DEFAULT_WINDOW_POLL_SECONDS,
        help=(
            "Window tracking poll interval in seconds "
            f"(default: {hub.DEFAULT_WINDOW_POLL_SECONDS})."
        ),
    )
    parser.add_argument(
        "--no-interaction-helper",
        action="store_true",
        help="Disable interaction helper analysis and recommendations.",
    )
    parser.add_argument(
        "--no-window-ocr",
        action="store_true",
        help="Disable OCR attempts for active-window analysis.",
    )
    parser.add_argument(
        "--ocr-max-chars",
        type=int,
        default=hub.DEFAULT_OCR_MAX_CHARS,
        help=f"Maximum OCR excerpt length (default: {hub.DEFAULT_OCR_MAX_CHARS}).",
    )
    parser.add_argument(
        "--no-mode-efficiency-agent",
        action="store_true",
        help="Disable simple-task detection and reasoning-mode recommendations.",
    )
    parser.add_argument(
        "--auto-apply-reasoning-mode",
        action="store_true",
        help="Auto-write suggested reasoning mode into Codex config.",
    )
    parser.add_argument(
        "--codex-config",
        type=Path,
        default=hub.DEFAULT_CODEX_CONFIG,
        help=f"Codex config path used for mode apply (default: {hub.DEFAULT_CODEX_CONFIG}).",
    )
    parser.add_argument(
        "--mode-stability-threshold",
        type=int,
        default=hub.DEFAULT_MODE_STABILITY_THRESHOLD,
        help=(
            "Consecutive matching recommendations required before auto-apply "
            f"(default: {hub.DEFAULT_MODE_STABILITY_THRESHOLD})."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe Fleet Control Hub entry point")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    scan = subparsers.add_parser("scan", help="Run an inventory scan and update the DB.")
    add_common_inventory_options(scan)
    scan.set_defaults(func=hub.cmd_scan, scan_first=True)

    serve = subparsers.add_parser("serve", help="Serve the dashboard from an existing DB.")
    add_common_inventory_options(serve)
    add_serve_options(serve)
    serve.add_argument(
        "--scan-first",
        action="store_true",
        help="Run a guarded inventory scan before serving.",
    )
    serve.set_defaults(func=hub.cmd_serve)

    scan_serve = subparsers.add_parser(
        "scan-serve",
        help="Run a guarded scan, then serve the dashboard.",
    )
    add_common_inventory_options(scan_serve)
    add_serve_options(scan_serve)
    scan_serve.set_defaults(func=hub.cmd_serve, scan_first=True)

    return parser


def projects_root_error(projects_root: Path) -> str | None:
    root = projects_root.expanduser()

    try:
        resolved = root.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        return f"projects root is unavailable: {root} ({exc})"

    if not resolved.is_dir():
        return f"projects root is not a directory: {resolved}"
    if resolved == Path(resolved.anchor):
        return f"refusing to recursively scan filesystem root: {resolved}"
    if not os.access(resolved, os.R_OK | os.X_OK):
        return f"projects root is not readable/searchable: {resolved}"

    return None


def projects_roots_refusal(
    projects_root: Path,
    additional_projects_roots: Sequence[Path] = (),
) -> str | None:
    """Refuse only invalid configuration or a set with no observable root."""

    try:
        roots = hub.normalize_projects_roots(
            projects_root,
            additional_projects_roots,
        )
    except hub.RepoDiscoveryError as exc:
        return str(exc)

    errors = [error for root in roots if (error := projects_root_error(root))]
    if len(errors) == len(roots):
        return "; ".join(errors)
    return None


def guarded_run_scan(
    db_path: Path,
    projects_root: Path,
    linear_team_id: str | None,
    *,
    additional_projects_roots: Sequence[Path] = (),
    chat_work_json: Path = hub.DEFAULT_CHAT_WORK_JSON,
    venture_report_json: Path = hub.DEFAULT_VENTURE_REPORT_JSON,
    repo_registry_path: Path | None = None,
) -> dict[str, int | str]:
    """Run the core scanner only after validating the observation boundary.

    Keeping the guard on the shared callable protects CLI scans, scan-first serving,
    and HTTP-triggered rescans that otherwise call ``control_hub_agent.run_scan``
    directly after the server has already started.
    """

    error = projects_roots_refusal(projects_root, additional_projects_roots)
    if error:
        raise ScanRefusedError(error)
    return _CORE_RUN_SCAN(
        db_path,
        projects_root,
        linear_team_id,
        additional_projects_roots=additional_projects_roots,
        chat_work_json=chat_work_json,
        venture_report_json=venture_report_json,
        repo_registry_path=repo_registry_path,
    )


class SafeHubHandler(_CORE_HUB_HANDLER):
    """Dashboard handler that fails closed when the scan root disappears."""

    def _send_scan_refusal(self, error: str) -> None:
        payload = (
            f"scan refused: {error}\n"
            "existing database state was not opened or pruned.\n"
        ).encode("utf-8")
        self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/scan":
            error = projects_roots_refusal(
                self.projects_root,
                getattr(self, "additional_projects_roots", ()),
            )
            if error:
                self._send_scan_refusal(error)
                return
        try:
            super().do_POST()
        except (ScanRefusedError, hub.RepoDiscoveryError) as exc:
            self._send_scan_refusal(str(exc))


def install_runtime_guards() -> None:
    """Route all scans reached through this entry point through fail-closed guards."""

    hub.run_scan = guarded_run_scan
    hub.HubHandler = SafeHubHandler


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    install_runtime_guards()

    if getattr(args, "scan_first", False):
        error = projects_roots_refusal(
            args.projects_root,
            args.additional_projects_root,
        )
        if error:
            print(f"[control-hub] scan refused: {error}", file=sys.stderr)
            print(
                "[control-hub] existing database state was not opened or pruned.",
                file=sys.stderr,
            )
            return 2

    try:
        return int(args.func(args))
    except (ScanRefusedError, hub.RepoDiscoveryError) as exc:
        print(f"[control-hub] scan refused: {exc}", file=sys.stderr)
        print(
            "[control-hub] existing database state was not opened or pruned.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
