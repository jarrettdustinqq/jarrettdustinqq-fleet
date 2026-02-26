#!/usr/bin/env python3
"""
Fleet Control Hub Agent

Inventories local work (git repos), optional Linear tasks, and environment signals,
then serves an interactive local dashboard for management.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import request
from urllib.parse import parse_qs


APP_NAME = "fleet-control-hub"
DEFAULT_DB = Path.home() / ".local" / "share" / APP_NAME / "control_hub.db"
DEFAULT_PROJECTS_ROOT = Path.home() / "projects"
LINEAR_API_URL = "https://api.linear.app/graphql"
DEFAULT_WINDOW_POLL_SECONDS = 2.0
DEFAULT_WINDOW_EVENT_LIMIT = 250
DEFAULT_AGENDA_HISTORY_LIMIT = 40
DEFAULT_OPPORTUNITY_HISTORY_LIMIT = 120
DEFAULT_OCR_MAX_CHARS = 1200
DEFAULT_MODE_AGENT_NAME = "mode-efficiency-agent"
DEFAULT_CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
DEFAULT_MODE_STABILITY_THRESHOLD = 2


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(iso_text: str | None) -> datetime | None:
    if not iso_text:
        return None
    try:
        # Allow trailing Z.
        return datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
    except ValueError:
        return None


def days_since(iso_text: str | None) -> int | None:
    dt = parse_iso(iso_text)
    if not dt:
        return None
    delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    return max(0, int(delta.total_seconds() // 86400))


def run_cmd(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def log_startup(message: str, *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    print(f"[control-hub] {message}", file=stream, flush=True)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def db_connect(db_path: Path) -> sqlite3.Connection:
    ensure_parent(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS repos (
            path TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            branch TEXT,
            dirty INTEGER NOT NULL DEFAULT 0,
            ahead INTEGER NOT NULL DEFAULT 0,
            behind INTEGER NOT NULL DEFAULT 0,
            last_commit_at TEXT,
            last_commit_age_days INTEGER,
            remote_url TEXT,
            focus_level INTEGER NOT NULL DEFAULT 0,
            next_action TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT,
            priority INTEGER,
            assignee TEXT,
            url TEXT,
            notes TEXT NOT NULL DEFAULT '',
            done INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source, external_id)
        );

        CREATE TABLE IF NOT EXISTS recommendations (
            fingerprint TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            details TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 3,
            status TEXT NOT NULL DEFAULT 'open',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS active_window (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            title TEXT NOT NULL DEFAULT '',
            app TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            pid INTEGER,
            window_id TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS window_activity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            app TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            pid INTEGER,
            window_id TEXT,
            observed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS window_agendas (
            context_key TEXT PRIMARY KEY,
            agenda_title TEXT NOT NULL,
            app TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            content_summary TEXT NOT NULL DEFAULT '',
            last_step TEXT NOT NULL DEFAULT '',
            next_step TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT 'active-window',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS interaction_opportunities (
            fingerprint TEXT PRIMARY KEY,
            context_key TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'active-window',
            app TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            agenda_title TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL,
            signal TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            confidence INTEGER NOT NULL DEFAULT 50,
            source TEXT NOT NULL DEFAULT 'heuristic',
            status TEXT NOT NULL DEFAULT 'open',
            observed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    ensure_column(conn, "active_window", "content_summary TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "active_window", "agenda_title TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "active_window", "last_step TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "active_window", "next_step TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "active_window", "scope TEXT NOT NULL DEFAULT 'active-window'")
    ensure_column(conn, "active_window", "source_backend TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "active_window", "interaction_needs TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "active_window", "helper_recommendations TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "active_window", "ocr_excerpt TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "active_window", "analysis_source TEXT NOT NULL DEFAULT 'metadata'")
    ensure_column(conn, "active_window", "task_complexity TEXT NOT NULL DEFAULT 'unknown'")
    ensure_column(conn, "active_window", "suggested_reasoning_mode TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "active_window", "mode_rationale TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "window_activity_events", "content_summary TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "window_activity_events", "agenda_title TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "window_activity_events", "last_step TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "window_activity_events", "next_step TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "window_activity_events", "scope TEXT NOT NULL DEFAULT 'active-window'")
    ensure_column(conn, "window_activity_events", "source_backend TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "window_activity_events", "context_key TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "window_activity_events", "interaction_needs TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "window_activity_events", "helper_recommendations TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "window_activity_events", "ocr_excerpt TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "window_activity_events", "analysis_source TEXT NOT NULL DEFAULT 'metadata'")
    ensure_column(conn, "window_activity_events", "task_complexity TEXT NOT NULL DEFAULT 'unknown'")
    ensure_column(conn, "window_activity_events", "suggested_reasoning_mode TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "window_activity_events", "mode_rationale TEXT NOT NULL DEFAULT ''")
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column_def: str) -> None:
    col_name = column_def.split()[0].strip()
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    known = {r[1] for r in rows}
    if col_name not in known:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")


def set_codex_reasoning_mode(config_path: Path, mode: str) -> tuple[bool, str]:
    target = (mode or "").strip().lower()
    if target not in {"low", "medium", "high"}:
        return False, f"invalid reasoning mode '{mode}'"

    if not config_path.exists():
        return False, f"config not found: {config_path}"

    try:
        original = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"unable to read config: {exc}"

    line_re = re.compile(r'(?m)^model_reasoning_effort\s*=\s*"([^"]*)"\s*$')
    match = line_re.search(original)
    desired_line = f'model_reasoning_effort = "{target}"'

    if match:
        current = (match.group(1) or "").strip().lower()
        if current == target:
            return True, f"already set to {target}"
        updated = line_re.sub(desired_line, original, count=1)
    else:
        model_re = re.compile(r'(?m)^model\s*=\s*"[^"]*"\s*$')
        model_match = model_re.search(original)
        if model_match:
            insert_at = model_match.end()
            updated = f"{original[:insert_at]}\n{desired_line}{original[insert_at:]}"
        else:
            prefix = "" if not original or original.endswith("\n") else "\n"
            updated = f"{original}{prefix}{desired_line}\n"

    if updated == original:
        return True, f"already set to {target}"

    try:
        ensure_parent(config_path)
        fd, tmp_path_text = tempfile.mkstemp(prefix="codex-config-", suffix=".toml", dir=str(config_path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
        os.replace(tmp_path_text, str(config_path))
    except OSError as exc:
        return False, f"unable to write config: {exc}"

    return True, f"set model_reasoning_effort={target}"


def find_git_repos(projects_root: Path) -> list[Path]:
    repos: list[Path] = []
    if not projects_root.exists():
        return repos

    for root, dirs, _files in os.walk(projects_root):
        root_path = Path(root)
        if ".git" in dirs:
            repos.append(root_path)
            # Do not descend into nested folders inside a git repository.
            dirs[:] = []
            continue

        # Skip heavy directories when not in a repo yet.
        skip = {".cache", "node_modules", ".venv", "venv"}
        dirs[:] = [d for d in dirs if d not in skip]

    return sorted(repos)


@dataclass
class RepoSnapshot:
    path: str
    name: str
    branch: str
    dirty: int
    ahead: int
    behind: int
    last_commit_at: str | None
    last_commit_age_days: int | None
    remote_url: str | None
    updated_at: str


def snapshot_repo(repo_path: Path) -> RepoSnapshot:
    rc, branch, _ = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    if rc != 0:
        branch = "unknown"

    rc, porcelain, _ = run_cmd(["git", "status", "--porcelain"], cwd=repo_path)
    dirty = 1 if rc == 0 and porcelain else 0

    ahead = 0
    behind = 0
    rc, counts, _ = run_cmd(
        ["git", "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
        cwd=repo_path,
    )
    if rc == 0 and counts:
        parts = counts.split()
        if len(parts) == 2:
            behind = int(parts[0])
            ahead = int(parts[1])

    rc, last_commit_at, _ = run_cmd(["git", "log", "-1", "--format=%cI"], cwd=repo_path)
    if rc != 0 or not last_commit_at:
        last_commit_at = None
    last_age = days_since(last_commit_at)

    rc, remote_url, _ = run_cmd(["git", "remote", "get-url", "origin"], cwd=repo_path)
    if rc != 0 or not remote_url:
        remote_url = None

    return RepoSnapshot(
        path=str(repo_path),
        name=repo_path.name,
        branch=branch,
        dirty=dirty,
        ahead=ahead,
        behind=behind,
        last_commit_at=last_commit_at,
        last_commit_age_days=last_age,
        remote_url=remote_url,
        updated_at=now_utc_iso(),
    )


def upsert_repo(conn: sqlite3.Connection, snap: RepoSnapshot) -> None:
    conn.execute(
        """
        INSERT INTO repos (
            path, name, branch, dirty, ahead, behind, last_commit_at,
            last_commit_age_days, remote_url, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            name = excluded.name,
            branch = excluded.branch,
            dirty = excluded.dirty,
            ahead = excluded.ahead,
            behind = excluded.behind,
            last_commit_at = excluded.last_commit_at,
            last_commit_age_days = excluded.last_commit_age_days,
            remote_url = excluded.remote_url,
            updated_at = excluded.updated_at
        """,
        (
            snap.path,
            snap.name,
            snap.branch,
            snap.dirty,
            snap.ahead,
            snap.behind,
            snap.last_commit_at,
            snap.last_commit_age_days,
            snap.remote_url,
            snap.updated_at,
        ),
    )


def prune_missing_repos(conn: sqlite3.Connection, repo_paths: list[str]) -> None:
    if not repo_paths:
        conn.execute("DELETE FROM repos")
        return
    placeholders = ",".join("?" for _ in repo_paths)
    conn.execute(f"DELETE FROM repos WHERE path NOT IN ({placeholders})", repo_paths)


@dataclass
class WindowSnapshot:
    title: str
    app: str
    location: str
    summary: str
    content_summary: str
    agenda_title: str
    last_step: str
    next_step: str
    scope: str
    source_backend: str
    context_key: str
    interaction_needs: str
    helper_recommendations: str
    ocr_excerpt: str
    analysis_source: str
    task_complexity: str
    suggested_reasoning_mode: str
    mode_rationale: str
    observed_at: str
    pid: int | None = None
    window_id: str | None = None

    @property
    def fingerprint(self) -> str:
        text = "|".join(
            [
                self.window_id or "",
                str(self.pid or 0),
                self.title,
                self.app,
                self.location,
                self.summary,
                self.next_step,
                self.interaction_needs,
            ]
        )
        return hashlib.sha1(text.encode("utf-8")).hexdigest()


@dataclass
class InteractionOpportunity:
    category: str
    signal: str
    recommendation: str
    confidence: int
    source: str = "heuristic"

    def fingerprint(self, context_key: str) -> str:
        seed = f"{context_key}|{self.category}|{self.signal}|{self.recommendation}"
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def app_family(app: str) -> str:
    lower = (app or "").lower()
    if any(token in lower for token in ("firefox", "chrome", "chromium", "brave", "edge", "browser", "vivaldi")):
        return "browser"
    if any(token in lower for token in ("code", "vim", "nvim", "emacs", "jetbrains", "zed", "sublime")):
        return "code"
    if any(token in lower for token in ("term", "kitty", "alacritty", "wezterm", "xterm", "tmux", "konsole")):
        return "terminal"
    return "other"


def focus_scope(app: str) -> str:
    if app_family(app) == "browser":
        return "active-tab"
    return "active-window"


def normalize_focus_title(title: str, app: str) -> str:
    cleaned = " ".join((title or "").split())
    if not cleaned:
        return "(untitled)"
    family = app_family(app)
    if family == "browser":
        cleaned = re.sub(
            r"\s[-\u2014]\s(Mozilla Firefox|Firefox|Google Chrome|Chromium|Brave|Microsoft Edge|Vivaldi)$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
    cleaned = re.sub(r"\s[-\u2014]\s(Visual Studio Code|Code - OSS|Code OSS)$", "", cleaned, flags=re.IGNORECASE)
    return cleaned or "(untitled)"


def shorten_path(path_text: str) -> str:
    path = Path(path_text).expanduser()
    home = Path.home()
    try:
        rel = path.relative_to(home)
        return f"~/{rel}" if str(rel) != "." else "~"
    except ValueError:
        return str(path)


def find_repo_root(path: Path) -> Path | None:
    current = path
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def describe_window_location(pid: int | None, projects_root: Path) -> str:
    if not pid:
        return "Unknown location"

    cwd_link = Path(f"/proc/{pid}/cwd")
    if not cwd_link.exists():
        return f"PID {pid}"

    try:
        cwd = cwd_link.resolve()
    except OSError:
        return f"PID {pid}"

    repo_root = find_repo_root(cwd)
    if repo_root:
        repo_name = repo_root.name
        return f"{repo_name} @ {shorten_path(str(repo_root))}"

    try:
        rel = cwd.relative_to(projects_root)
        return f"projects/{rel}"
    except ValueError:
        return shorten_path(str(cwd))


def summarize_window_work(title: str, app: str, location: str) -> str:
    clean_title = normalize_focus_title(title, app)
    family = app_family(app)
    if family == "code":
        return f"Coding: {clean_title} | {location}"
    if family == "browser":
        return f"Active tab: {clean_title} | {location}"
    if family == "terminal":
        return f"Terminal work: {clean_title} | {location}"
    if app:
        return f"{app}: {clean_title} | {location}"
    return f"Active work: {clean_title} | {location}"


def summarize_window_content(agenda_title: str, app: str, location: str, scope: str) -> str:
    family = app_family(app)
    if family == "browser":
        return f"Reading tab '{agenda_title}' in {location}."
    if family == "code":
        return f"Editing/inspecting code around '{agenda_title}' in {location}."
    if family == "terminal":
        return f"Running terminal workflow '{agenda_title}' in {location}."
    return f"Focused on '{agenda_title}' in {location}."


def suggest_next_step(agenda_title: str, app: str, content_summary: str) -> str:
    title_l = agenda_title.lower()
    family = app_family(app)

    if any(token in title_l for token in ("pull request", "merge request", "review")):
        return "Resolve one review item end-to-end, then update status in the related thread."
    if any(token in title_l for token in ("issue", "ticket", "bug", "linear", "jira", "task")):
        return "Turn this agenda into a short checklist and execute the first unchecked item."
    if family == "code":
        return "Make the smallest next code change, then run the nearest test or lint check."
    if family == "terminal":
        if any(token in title_l for token in ("test", "pytest", "lint", "build", "error", "fail")):
            return "Fix the top failing signal in the terminal output, then rerun the same command."
        return "Run the next command that moves this task forward and capture the result."
    if family == "browser":
        if any(token in title_l for token in ("docs", "documentation", "readme", "guide", "reference", "api")):
            return "Extract one actionable instruction from this tab and apply it in your working project."
        return "Capture one decision from this tab, then switch back to implementation."
    return f"Choose the next concrete action based on: {content_summary}"


def build_context_key(app: str, location: str, agenda_title: str, scope: str) -> str:
    seed = f"{app.strip().lower()}|{location.strip().lower()}|{agenda_title.strip().lower()}|{scope}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def derive_agenda_steps(
    conn: sqlite3.Connection,
    app: str,
    location: str,
    agenda_title: str,
    scope: str,
    content_summary: str,
) -> tuple[str, str, str]:
    context_key = build_context_key(app, location, agenda_title, scope)
    row = conn.execute(
        """
        SELECT last_step, next_step
        FROM window_agendas
        WHERE context_key = ?
        """,
        (context_key,),
    ).fetchone()

    if row:
        last_step = row["next_step"] or row["last_step"] or "Continued previous work in this context."
    else:
        last_step = "Entered this context and captured the agenda."
    next_step = suggest_next_step(agenda_title, app, content_summary)
    return context_key, last_step, next_step


def detect_window_tracking_support() -> tuple[bool, str]:
    if os.environ.get("WAYLAND_DISPLAY"):
        if shutil.which("swaymsg") is not None:
            return True, "active: swaymsg (wayland)"
        if shutil.which("hyprctl") is not None:
            return True, "active: hyprctl (wayland)"

    if os.environ.get("DISPLAY"):
        has_xdotool = shutil.which("xdotool") is not None
        has_xprop = shutil.which("xprop") is not None
        if has_xdotool and has_xprop:
            return True, "active: xdotool (x11, xprop fallback)"
        if has_xdotool:
            return True, "active: xdotool (x11)"
        if has_xprop:
            return True, "active: xprop (x11)"

    if os.environ.get("WAYLAND_DISPLAY"):
        return False, "disabled: no supported wayland backend (install swaymsg or hyprctl)"
    if os.environ.get("DISPLAY"):
        return False, "disabled: install xdotool or xprop for x11 window tracking"
    return False, "disabled: no DISPLAY or WAYLAND_DISPLAY detected"


def read_app_name_from_pid(pid: int | None) -> str:
    if not pid:
        return ""
    comm_path = Path(f"/proc/{pid}/comm")
    try:
        return comm_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build_window_snapshot(
    title: str,
    app: str,
    pid: int | None,
    window_id: str,
    projects_root: Path,
    source_backend: str,
    workspace: str | None = None,
) -> WindowSnapshot:
    resolved_location = describe_window_location(pid, projects_root)
    if workspace:
        if resolved_location == "Unknown location":
            location = workspace
        else:
            location = f"{resolved_location} | {workspace}"
    else:
        location = resolved_location

    clean_title = normalize_focus_title(title, app)
    scope = focus_scope(app)
    summary = summarize_window_work(clean_title, app, location)
    content_summary = summarize_window_content(clean_title, app, location, scope)
    return WindowSnapshot(
        title=title or "(untitled)",
        app=app or "unknown",
        location=location,
        summary=summary,
        content_summary=content_summary,
        agenda_title=clean_title,
        last_step="",
        next_step="",
        scope=scope,
        source_backend=source_backend,
        context_key="",
        interaction_needs="",
        helper_recommendations="",
        ocr_excerpt="",
        analysis_source="metadata",
        task_complexity="unknown",
        suggested_reasoning_mode="",
        mode_rationale="",
        pid=pid,
        window_id=window_id,
        observed_at=now_utc_iso(),
    )


def capture_active_window_xdotool(projects_root: Path) -> WindowSnapshot | None:
    rc, window_id, _ = run_cmd(["xdotool", "getactivewindow"])
    if rc != 0 or not window_id:
        return None

    rc, title, _ = run_cmd(["xdotool", "getwindowname", window_id])
    if rc != 0:
        title = "(untitled)"
    else:
        title = title or "(untitled)"

    pid: int | None = None
    rc, pid_text, _ = run_cmd(["xdotool", "getwindowpid", window_id])
    if rc == 0 and pid_text.isdigit():
        pid = int(pid_text)

    app = read_app_name_from_pid(pid)
    return build_window_snapshot(title, app, pid, window_id, projects_root, source_backend="xdotool")


def capture_active_window_xprop(projects_root: Path) -> WindowSnapshot | None:
    rc, root_info, _ = run_cmd(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    if rc != 0 or "#" not in root_info:
        return None

    window_id = root_info.split("#", 1)[1].strip()
    if not window_id or window_id in {"0x0", "0"}:
        return None

    rc, details, _ = run_cmd(["xprop", "-id", window_id, "_NET_WM_NAME", "WM_NAME", "_NET_WM_PID", "WM_CLASS"])
    if rc != 0:
        return None

    title = "(untitled)"
    pid: int | None = None
    app = ""

    for line in details.splitlines():
        if "_NET_WM_NAME" in line or line.startswith("WM_NAME"):
            names = re.findall(r'"([^"]*)"', line)
            if names and names[0]:
                title = names[0]
        elif "_NET_WM_PID" in line:
            match = re.search(r"=\s*(\d+)", line)
            if match:
                pid = int(match.group(1))
        elif line.startswith("WM_CLASS"):
            classes = re.findall(r'"([^"]*)"', line)
            if classes:
                app = classes[-1]

    if not app:
        app = read_app_name_from_pid(pid)

    return build_window_snapshot(title, app, pid, window_id, projects_root, source_backend="xprop")


def _find_focused_sway_node(node: dict[str, Any], workspace: str | None = None) -> tuple[dict[str, Any], str | None] | None:
    current_workspace = workspace
    if node.get("type") == "workspace":
        current_workspace = node.get("name") or workspace
    if node.get("focused"):
        return node, current_workspace
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = _find_focused_sway_node(child, current_workspace)
        if found:
            return found
    return None


def capture_active_window_sway(projects_root: Path) -> WindowSnapshot | None:
    rc, output, _ = run_cmd(["swaymsg", "-t", "get_tree", "-r"])
    if rc != 0 or not output:
        return None
    try:
        tree = json.loads(output)
    except json.JSONDecodeError:
        return None
    found = _find_focused_sway_node(tree)
    if not found:
        return None
    node, workspace = found
    window_props = node.get("window_properties") or {}
    app = (
        node.get("app_id")
        or window_props.get("class")
        or window_props.get("instance")
        or "unknown"
    )
    title = node.get("name") or "(untitled)"
    pid_val = node.get("pid")
    pid = pid_val if isinstance(pid_val, int) else None
    window_id = str(node.get("id") or "")
    ws = f"workspace:{workspace}" if workspace else None
    return build_window_snapshot(title, app, pid, window_id, projects_root, source_backend="swaymsg", workspace=ws)


def capture_active_window_hypr(projects_root: Path) -> WindowSnapshot | None:
    rc, output, _ = run_cmd(["hyprctl", "activewindow", "-j"])
    if rc != 0 or not output:
        return None
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None

    title = data.get("title") or "(untitled)"
    app = data.get("class") or data.get("initialClass") or "unknown"
    pid_val = data.get("pid")
    pid = pid_val if isinstance(pid_val, int) else None
    window_id = str(data.get("address") or "")
    workspace_name = (data.get("workspace") or {}).get("name")
    ws = f"workspace:{workspace_name}" if workspace_name else None
    return build_window_snapshot(title, app, pid, window_id, projects_root, source_backend="hyprctl", workspace=ws)


def capture_active_window(projects_root: Path) -> WindowSnapshot | None:
    if os.environ.get("WAYLAND_DISPLAY"):
        if shutil.which("swaymsg") is not None:
            snap = capture_active_window_sway(projects_root)
            if snap:
                return snap
        if shutil.which("hyprctl") is not None:
            snap = capture_active_window_hypr(projects_root)
            if snap:
                return snap
    if shutil.which("xdotool") is not None:
        snap = capture_active_window_xdotool(projects_root)
        if snap:
            return snap
    if shutil.which("xprop") is not None:
        snap = capture_active_window_xprop(projects_root)
        if snap:
            return snap
    return None


def detect_ocr_support(enable_ocr: bool) -> tuple[bool, str]:
    if not enable_ocr:
        return False, "disabled: --no-window-ocr"
    if shutil.which("tesseract") is None:
        return False, "disabled: tesseract not installed"
    if os.environ.get("DISPLAY") and shutil.which("import") is not None:
        return True, "active: tesseract+import"
    return False, "disabled: no screenshot backend for OCR (need X11 import)"


def sanitize_window_id(window_id: str | None) -> str | None:
    if not window_id:
        return None
    candidate = window_id.strip()
    if re.fullmatch(r"[0-9A-Fa-fx]+", candidate):
        return candidate
    return None


def capture_window_ocr_text(window_id: str | None, max_chars: int) -> str:
    safe_window_id = sanitize_window_id(window_id)
    if not safe_window_id:
        return ""
    fd, tmp_path_text = tempfile.mkstemp(prefix="fleet-hub-", suffix=".png")
    os.close(fd)
    tmp_path = Path(tmp_path_text)
    try:
        rc, _stdout, _stderr = run_cmd(["import", "-window", safe_window_id, str(tmp_path)])
        if rc != 0:
            return ""
        rc, text, _stderr = run_cmd(
            [
                "tesseract",
                str(tmp_path),
                "stdout",
                "-l",
                "eng",
                "--psm",
                "6",
            ]
        )
        if rc != 0:
            return ""
        compact = " ".join(text.split())
        return compact[:max_chars]
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def infer_user_activity(agenda_title: str, app: str, content_text: str) -> str:
    lower = f"{agenda_title} {content_text}".lower()
    family = app_family(app)

    if family == "browser":
        if any(token in lower for token in ("checkout", "payment", "cart", "order", "billing")):
            return "completing a transactional flow"
        if any(token in lower for token in ("sign in", "login", "verify", "2fa", "captcha")):
            return "authenticating account access"
        if any(token in lower for token in ("docs", "documentation", "guide", "reference", "readme")):
            return "reading implementation guidance"
        return "working through a web page flow"
    if family == "code":
        if any(token in lower for token in ("test", "failing", "error", "traceback")):
            return "debugging or fixing failing checks"
        if any(token in lower for token in ("pull request", "review", "diff")):
            return "reviewing code changes"
        return "editing or inspecting source code"
    if family == "terminal":
        if any(token in lower for token in ("pytest", "test", "build", "lint")):
            return "running command-line checks"
        return "executing terminal workflow steps"
    return "progressing work in the focused window"


def detect_interaction_opportunities(text: str, app: str) -> list[InteractionOpportunity]:
    lower = " ".join(text.lower().split())
    opportunities: list[InteractionOpportunity] = []

    rules = [
        (
            "auth",
            ("sign in", "login", "password", "2fa", "verify", "captcha"),
            "Complete authentication fields and submit to unblock the rest of this flow.",
            85,
        ),
        (
            "form",
            ("required", "fill out", "form", "submit", "application", "registration"),
            "Fill all required fields, validate entries, then submit the form.",
            80,
        ),
        (
            "checkout",
            ("checkout", "payment", "billing", "shipping", "cart", "order"),
            "Confirm cart details, complete payment/shipping fields, then place the order.",
            80,
        ),
        (
            "pending",
            ("pending", "not started", "todo", "to do", "draft", "unsaved", "incomplete"),
            "Convert pending items into a short checklist and complete the top unchecked step.",
            75,
        ),
        (
            "error",
            ("error", "failed", "denied", "invalid", "warning"),
            "Resolve the first blocking error message, then retry the same interaction.",
            90,
        ),
        (
            "approval",
            ("approve", "review", "merge", "request changes", "assign"),
            "Decide approval state and leave a concise update so the workflow can continue.",
            72,
        ),
    ]

    seen: set[str] = set()
    for category, signals, recommendation, confidence in rules:
        for signal in signals:
            if signal in lower:
                key = f"{category}:{signal}"
                if key in seen:
                    continue
                opportunities.append(
                    InteractionOpportunity(
                        category=category,
                        signal=signal,
                        recommendation=recommendation,
                        confidence=confidence,
                    )
                )
                seen.add(key)
                break

    if not opportunities and app_family(app) == "browser":
        opportunities.append(
            InteractionOpportunity(
                category="navigation",
                signal="page-flow",
                recommendation="Identify the primary call-to-action on this tab and complete it before switching contexts.",
                confidence=55,
            )
        )

    return opportunities


def summarize_interaction_needs(opportunities: list[InteractionOpportunity]) -> str:
    if not opportunities:
        return "No obvious unfinished interactive step detected."
    signals = [f"{op.category}:{op.signal}" for op in opportunities[:4]]
    return "Detected interaction needs: " + ", ".join(signals)


def summarize_helper_recommendations(
    opportunities: list[InteractionOpportunity],
    default_next_step: str,
) -> str:
    if not opportunities:
        return default_next_step
    top = [op.recommendation for op in opportunities[:3]]
    return " | ".join(top)


def sync_interaction_opportunities(
    conn: sqlite3.Connection,
    snap: WindowSnapshot,
    opportunities: list[InteractionOpportunity],
    max_rows: int = DEFAULT_OPPORTUNITY_HISTORY_LIMIT,
) -> None:
    now = now_utc_iso()
    conn.execute(
        """
        UPDATE interaction_opportunities
        SET status = 'resolved', updated_at = ?
        WHERE context_key = ? AND status = 'open'
        """,
        (now, snap.context_key),
    )

    for op in opportunities:
        fp = op.fingerprint(snap.context_key)
        conn.execute(
            """
            INSERT INTO interaction_opportunities (
                fingerprint, context_key, scope, app, location, agenda_title,
                category, signal, recommendation, confidence, source, status,
                observed_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                scope = excluded.scope,
                app = excluded.app,
                location = excluded.location,
                agenda_title = excluded.agenda_title,
                category = excluded.category,
                signal = excluded.signal,
                recommendation = excluded.recommendation,
                confidence = excluded.confidence,
                source = excluded.source,
                status = 'open',
                observed_at = excluded.observed_at,
                updated_at = excluded.updated_at
            """,
            (
                fp,
                snap.context_key,
                snap.scope,
                snap.app,
                snap.location,
                snap.agenda_title,
                op.category,
                op.signal,
                op.recommendation,
                op.confidence,
                op.source,
                snap.observed_at,
                now,
            ),
        )

    conn.execute(
        """
        DELETE FROM interaction_opportunities
        WHERE fingerprint NOT IN (
            SELECT fingerprint
            FROM interaction_opportunities
            ORDER BY updated_at DESC, observed_at DESC
            LIMIT ?
        )
        """,
        (max_rows,),
    )


class InteractionHelperAgent:
    def __init__(
        self,
        enable_helper: bool = True,
        enable_ocr: bool = True,
        ocr_max_chars: int = DEFAULT_OCR_MAX_CHARS,
        opportunity_history_limit: int = DEFAULT_OPPORTUNITY_HISTORY_LIMIT,
    ) -> None:
        self.enable_helper = enable_helper
        self.enable_ocr = enable_ocr
        self.ocr_max_chars = max(120, ocr_max_chars)
        self.opportunity_history_limit = max(20, opportunity_history_limit)
        self.ocr_supported, self.ocr_status = detect_ocr_support(enable_ocr and enable_helper)

    def status(self) -> tuple[str, str]:
        if not self.enable_helper:
            return "disabled: --no-interaction-helper", "disabled"
        helper = "active: heuristic interaction analysis"
        return helper, self.ocr_status

    def enrich_snapshot(self, conn: sqlite3.Connection, snap: WindowSnapshot) -> list[InteractionOpportunity]:
        if not self.enable_helper:
            snap.analysis_source = "metadata"
            snap.interaction_needs = "Interaction helper disabled."
            snap.helper_recommendations = snap.next_step
            return []

        ocr_excerpt = ""
        analysis_sources = ["metadata"]
        if self.ocr_supported:
            ocr_excerpt = capture_window_ocr_text(snap.window_id, self.ocr_max_chars)
            if ocr_excerpt:
                analysis_sources.append("ocr")

        activity = infer_user_activity(snap.agenda_title, snap.app, f"{snap.content_summary} {ocr_excerpt}")
        snap.content_summary = f"{snap.content_summary} You appear to be {activity}."

        opportunity_text = " ".join(
            part for part in [snap.agenda_title, snap.title, snap.content_summary, ocr_excerpt] if part
        )
        opportunities = detect_interaction_opportunities(opportunity_text, snap.app)
        snap.interaction_needs = summarize_interaction_needs(opportunities)
        snap.helper_recommendations = summarize_helper_recommendations(opportunities, snap.next_step)
        if opportunities:
            snap.next_step = opportunities[0].recommendation

        snap.ocr_excerpt = ocr_excerpt
        snap.analysis_source = "+".join(analysis_sources)
        sync_interaction_opportunities(conn, snap, opportunities, self.opportunity_history_limit)
        return opportunities


def _keyword_score(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for token in keywords if token in text)


def recommend_reasoning_mode(
    agenda_title: str,
    app: str,
    content_summary: str,
    opportunities: list[InteractionOpportunity],
) -> tuple[str, str, str]:
    text = " ".join([agenda_title, content_summary]).lower()
    categories = {op.category for op in opportunities}

    simple_keywords = (
        "typo",
        "spell",
        "format",
        "lint",
        "readme",
        "docs",
        "documentation",
        "settings",
        "status",
        "checklist",
        "rename",
        "small",
        "quick",
        "follow-up",
        "minor",
    )
    complex_keywords = (
        "incident",
        "outage",
        "security",
        "traceback",
        "exception",
        "error",
        "failed",
        "failure",
        "refactor",
        "architecture",
        "migration",
        "database",
        "schema",
        "performance",
        "production",
        "rollback",
    )

    simple_score = _keyword_score(text, simple_keywords)
    complex_score = _keyword_score(text, complex_keywords)
    family = app_family(app)

    if family == "browser" and any(token in text for token in ("docs", "guide", "reference", "settings", "help")):
        simple_score += 1
    if family == "terminal" and any(token in text for token in ("test", "build", "deploy", "error", "traceback")):
        complex_score += 1
    if family == "code" and any(token in text for token in ("refactor", "design", "migration", "debug")):
        complex_score += 1

    if categories.intersection({"error", "auth", "checkout", "form", "approval"}):
        complex_score += 2
    if categories == {"navigation"}:
        simple_score += 1

    if complex_score >= 2:
        return (
            "complex",
            "high",
            "Complex/blocking signals detected; keep deeper reasoning enabled.",
        )
    if simple_score >= 2 and complex_score == 0:
        return (
            "simple",
            "low",
            "Obvious routine task detected; lower reasoning mode for speed and efficiency.",
        )
    return (
        "moderate",
        "medium",
        "Mixed signals detected; balanced reasoning is likely the best tradeoff.",
    )


class ModeEfficiencyAgent:
    def __init__(
        self,
        enabled: bool = True,
        auto_apply: bool = False,
        codex_config_path: Path = DEFAULT_CODEX_CONFIG,
        stability_threshold: int = DEFAULT_MODE_STABILITY_THRESHOLD,
    ) -> None:
        self.enabled = enabled
        self.auto_apply = auto_apply and enabled
        self.codex_config_path = codex_config_path
        self.stability_threshold = max(1, int(stability_threshold))
        self._last_applied_mode: str = ""
        self._candidate_mode: str = ""
        self._candidate_count: int = 0
        self._lock = threading.Lock()

    def status(self) -> str:
        if not self.enabled:
            return f"disabled: --no-{DEFAULT_MODE_AGENT_NAME}"
        if self.auto_apply:
            return (
                "active: heuristic task-complexity mode tuning "
                f"(auto-apply enabled, threshold={self.stability_threshold})"
            )
        return "active: heuristic task-complexity mode tuning (recommendation only)"

    def enrich_snapshot(
        self,
        snap: WindowSnapshot,
        opportunities: list[InteractionOpportunity],
    ) -> None:
        if not self.enabled:
            snap.task_complexity = "unknown"
            snap.suggested_reasoning_mode = ""
            snap.mode_rationale = "Mode-efficiency agent disabled."
            return

        complexity, reasoning_mode, rationale = recommend_reasoning_mode(
            snap.agenda_title,
            snap.app,
            snap.content_summary,
            opportunities,
        )
        snap.task_complexity = complexity
        snap.suggested_reasoning_mode = reasoning_mode
        snap.mode_rationale = f"{rationale} Suggested command: /reasoning {reasoning_mode}"

    def maybe_auto_apply(self, suggested_mode: str) -> tuple[bool, str]:
        if not self.enabled or not self.auto_apply:
            return False, "auto-apply disabled"

        target = (suggested_mode or "").strip().lower()
        if target not in {"low", "medium", "high"}:
            return False, "no valid suggested mode"

        with self._lock:
            if self._last_applied_mode == target:
                return False, "already applied this mode in current session"
            if self._candidate_mode == target:
                self._candidate_count += 1
            else:
                self._candidate_mode = target
                self._candidate_count = 1
            if self._candidate_count < self.stability_threshold:
                needed = self.stability_threshold - self._candidate_count
                return (
                    False,
                    f"waiting for stability: saw {target} {self._candidate_count}/{self.stability_threshold} "
                    f"(need {needed} more)",
                )
            ok, msg = set_codex_reasoning_mode(self.codex_config_path, target)
            if ok:
                self._last_applied_mode = target
            return ok, msg


def upsert_active_window(conn: sqlite3.Connection, snap: WindowSnapshot) -> None:
    conn.execute(
        """
        INSERT INTO active_window (
            singleton, title, app, location, summary, content_summary, agenda_title,
            last_step, next_step, scope, source_backend, interaction_needs,
            helper_recommendations, ocr_excerpt, analysis_source, task_complexity,
            suggested_reasoning_mode, mode_rationale, pid, window_id, updated_at
        )
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(singleton) DO UPDATE SET
            title = excluded.title,
            app = excluded.app,
            location = excluded.location,
            summary = excluded.summary,
            content_summary = excluded.content_summary,
            agenda_title = excluded.agenda_title,
            last_step = excluded.last_step,
            next_step = excluded.next_step,
            scope = excluded.scope,
            source_backend = excluded.source_backend,
            interaction_needs = excluded.interaction_needs,
            helper_recommendations = excluded.helper_recommendations,
            ocr_excerpt = excluded.ocr_excerpt,
            analysis_source = excluded.analysis_source,
            task_complexity = excluded.task_complexity,
            suggested_reasoning_mode = excluded.suggested_reasoning_mode,
            mode_rationale = excluded.mode_rationale,
            pid = excluded.pid,
            window_id = excluded.window_id,
            updated_at = excluded.updated_at
        """,
        (
            snap.title,
            snap.app,
            snap.location,
            snap.summary,
            snap.content_summary,
            snap.agenda_title,
            snap.last_step,
            snap.next_step,
            snap.scope,
            snap.source_backend,
            snap.interaction_needs,
            snap.helper_recommendations,
            snap.ocr_excerpt,
            snap.analysis_source,
            snap.task_complexity,
            snap.suggested_reasoning_mode,
            snap.mode_rationale,
            snap.pid,
            snap.window_id,
            snap.observed_at,
        ),
    )


def append_window_activity_event(conn: sqlite3.Connection, snap: WindowSnapshot, max_events: int) -> None:
    conn.execute(
        """
        INSERT INTO window_activity_events (
            title, app, location, summary, content_summary, agenda_title, last_step, next_step,
            scope, source_backend, context_key, interaction_needs, helper_recommendations,
            ocr_excerpt, analysis_source, task_complexity, suggested_reasoning_mode,
            mode_rationale, pid, window_id, observed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snap.title,
            snap.app,
            snap.location,
            snap.summary,
            snap.content_summary,
            snap.agenda_title,
            snap.last_step,
            snap.next_step,
            snap.scope,
            snap.source_backend,
            snap.context_key,
            snap.interaction_needs,
            snap.helper_recommendations,
            snap.ocr_excerpt,
            snap.analysis_source,
            snap.task_complexity,
            snap.suggested_reasoning_mode,
            snap.mode_rationale,
            snap.pid,
            snap.window_id,
            snap.observed_at,
        ),
    )
    conn.execute(
        """
        DELETE FROM window_activity_events
        WHERE id NOT IN (
            SELECT id FROM window_activity_events
            ORDER BY observed_at DESC, id DESC
            LIMIT ?
        )
        """,
        (max_events,),
    )


def upsert_window_agenda(conn: sqlite3.Connection, snap: WindowSnapshot) -> None:
    conn.execute(
        """
        INSERT INTO window_agendas (
            context_key, agenda_title, app, location, content_summary,
            last_step, next_step, scope, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(context_key) DO UPDATE SET
            agenda_title = excluded.agenda_title,
            app = excluded.app,
            location = excluded.location,
            content_summary = excluded.content_summary,
            last_step = excluded.last_step,
            next_step = excluded.next_step,
            scope = excluded.scope,
            updated_at = excluded.updated_at
        """,
        (
            snap.context_key,
            snap.agenda_title,
            snap.app,
            snap.location,
            snap.content_summary,
            snap.last_step,
            snap.next_step,
            snap.scope,
            snap.observed_at,
        ),
    )


class WindowTracker:
    def __init__(
        self,
        db_path: Path,
        projects_root: Path,
        helper_agent: InteractionHelperAgent | None = None,
        mode_agent: ModeEfficiencyAgent | None = None,
        poll_seconds: float = DEFAULT_WINDOW_POLL_SECONDS,
        max_events: int = DEFAULT_WINDOW_EVENT_LIMIT,
    ) -> None:
        self.db_path = db_path
        self.projects_root = projects_root
        self.helper_agent = helper_agent
        self.mode_agent = mode_agent
        self.poll_seconds = max(0.5, poll_seconds)
        self.max_events = max(50, max_events)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_fingerprint: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="window-tracker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.poll_seconds * 2)

    def _run(self) -> None:
        while not self._stop.is_set():
            conn: sqlite3.Connection | None = None
            try:
                snap = capture_active_window(self.projects_root)
                if snap is not None:
                    conn = db_connect(self.db_path)
                    init_db(conn)
                    context_key, last_step, next_step = derive_agenda_steps(
                        conn,
                        app=snap.app,
                        location=snap.location,
                        agenda_title=snap.agenda_title,
                        scope=snap.scope,
                        content_summary=snap.content_summary,
                    )
                    snap.context_key = context_key
                    snap.last_step = last_step
                    snap.next_step = next_step
                    opportunities: list[InteractionOpportunity] = []
                    if self.helper_agent:
                        opportunities = self.helper_agent.enrich_snapshot(conn, snap)
                    if self.mode_agent:
                        self.mode_agent.enrich_snapshot(snap, opportunities)
                        applied, apply_msg = self.mode_agent.maybe_auto_apply(snap.suggested_reasoning_mode)
                        if applied:
                            set_meta(conn, "mode_efficiency_last_applied_mode", snap.suggested_reasoning_mode)
                            set_meta(conn, "mode_efficiency_last_apply_status", f"ok: {apply_msg}")
                            set_meta(conn, "mode_efficiency_last_apply_at", now_utc_iso())
                        else:
                            set_meta(conn, "mode_efficiency_last_apply_status", apply_msg)
                    upsert_window_agenda(conn, snap)
                    upsert_active_window(conn, snap)
                    if self._last_fingerprint != snap.fingerprint:
                        append_window_activity_event(conn, snap, self.max_events)
                        self._last_fingerprint = snap.fingerprint
                    set_meta(conn, "window_tracking_last_seen", now_utc_iso())
                    set_meta(conn, "window_tracking_scope", "active-tab for browsers; active-window for other apps")
                    set_meta(conn, "window_tracking_backend", snap.source_backend)
                    set_meta(conn, "interaction_helper_last_seen", now_utc_iso())
                    set_meta(conn, "interaction_helper_analysis_source", snap.analysis_source)
                    set_meta(conn, "mode_efficiency_last_seen", now_utc_iso())
                    set_meta(conn, "mode_efficiency_recommendation", snap.suggested_reasoning_mode or "n/a")
                    conn.commit()
            except Exception as exc:  # pragma: no cover - runtime environment path
                try:
                    err_conn = db_connect(self.db_path)
                    init_db(err_conn)
                    set_meta(err_conn, "window_tracking_status", f"error: {exc}")
                    err_conn.commit()
                    err_conn.close()
                except Exception:
                    pass
            finally:
                if conn is not None:
                    conn.close()
            self._stop.wait(self.poll_seconds)


def linear_graphql(api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        LINEAR_API_URL,
        data=body,
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=30) as resp:
        data = resp.read().decode("utf-8")
    decoded = json.loads(data)
    if decoded.get("errors"):
        raise RuntimeError(f"Linear API errors: {decoded['errors']}")
    return decoded


def scan_linear_tasks(
    conn: sqlite3.Connection,
    linear_api_key: str,
    team_id: str | None = None,
) -> int:
    payload = {
        "query": """
        query {
          viewer {
            id
            name
            email
            assignedIssues(first: 100) {
              nodes {
                id
                identifier
                title
                priority
                url
                updatedAt
                state { name type }
                team { id key name }
              }
            }
          }
        }
        """
    }
    data = linear_graphql(linear_api_key, payload)
    viewer = data["data"]["viewer"]
    assignee = viewer.get("name") or viewer.get("email") or "me"
    nodes = viewer.get("assignedIssues", {}).get("nodes", [])

    kept_ids: list[str] = []
    count = 0
    for node in nodes:
        state = node.get("state", {})
        state_type = (state.get("type") or "").lower()
        if state_type in {"completed", "canceled"}:
            continue
        team = node.get("team", {})
        if team_id and team.get("id") != team_id:
            continue

        ext_id = node.get("identifier") or node["id"]
        kept_ids.append(ext_id)
        conn.execute(
            """
            INSERT INTO tasks (
              source, external_id, title, status, priority, assignee, url, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, external_id) DO UPDATE SET
              title = excluded.title,
              status = excluded.status,
              priority = excluded.priority,
              assignee = excluded.assignee,
              url = excluded.url,
              updated_at = excluded.updated_at
            """,
            (
                "linear",
                ext_id,
                node.get("title", "(untitled)"),
                state.get("name", "Unknown"),
                node.get("priority") if node.get("priority") is not None else 4,
                assignee,
                node.get("url"),
                node.get("updatedAt") or now_utc_iso(),
            ),
        )
        count += 1

    if kept_ids:
        placeholders = ",".join("?" for _ in kept_ids)
        conn.execute(
            f"DELETE FROM tasks WHERE source = 'linear' AND external_id NOT IN ({placeholders})",
            kept_ids,
        )
    else:
        conn.execute("DELETE FROM tasks WHERE source = 'linear'")

    return count


@dataclass
class Recommendation:
    category: str
    title: str
    details: str
    priority: int

    @property
    def fingerprint(self) -> str:
        text = f"{self.category}|{self.title}|{self.details}"
        return hashlib.sha1(text.encode("utf-8")).hexdigest()


def generate_recommendations(
    repos: list[RepoSnapshot],
    linear_task_count: int,
    projects_root: Path,
) -> list[Recommendation]:
    recs: list[Recommendation] = []

    if not repos:
        recs.append(
            Recommendation(
                category="inventory",
                title="No git repositories discovered",
                details=f"No repositories were found under {projects_root}. Add or clone repos, then rescan.",
                priority=1,
            )
        )

    dirty_repos = [r for r in repos if r.dirty]
    for repo in dirty_repos:
        recs.append(
            Recommendation(
                category="repo",
                title=f"Uncommitted changes in {repo.name}",
                details=f"{repo.path} has working tree changes. Commit, stash, or discard to reduce context drift.",
                priority=2,
            )
        )

    behind_repos = [r for r in repos if r.behind > 0]
    for repo in behind_repos:
        recs.append(
            Recommendation(
                category="repo",
                title=f"{repo.name} is behind upstream",
                details=f"{repo.path} is behind by {repo.behind} commit(s). Pull and re-run checks.",
                priority=2,
            )
        )

    stale_repos = [r for r in repos if (r.last_commit_age_days or 0) > 21]
    for repo in stale_repos:
        recs.append(
            Recommendation(
                category="focus",
                title=f"Stale repo: {repo.name}",
                details=f"Last commit was {repo.last_commit_age_days} days ago. Decide: archive, delegate, or schedule next action.",
                priority=3,
            )
        )

    if linear_task_count == 0:
        recs.append(
            Recommendation(
                category="execution",
                title="No open Linear tasks imported",
                details="Set LINEAR_API_KEY and optionally LINEAR_TEAM_ID before scanning so active issues appear in the dashboard.",
                priority=1,
            )
        )

    if not os.environ.get("GITHUB_TOKEN") and not os.environ.get("GH_TOKEN"):
        recs.append(
            Recommendation(
                category="integration",
                title="GitHub token not set for extended inventory",
                details="Set GITHUB_TOKEN or GH_TOKEN to expand remote inventory (PRs, issue counts, stale branches).",
                priority=3,
            )
        )

    if not os.environ.get("NOTION_API_KEY"):
        recs.append(
            Recommendation(
                category="integration",
                title="Notion token missing",
                details="Set NOTION_API_KEY if you want workspace pages/databases included in centralized inventory.",
                priority=3,
            )
        )

    return recs


def sync_recommendations(conn: sqlite3.Connection, recs: list[Recommendation]) -> None:
    now = now_utc_iso()
    current_fingerprints = {r.fingerprint for r in recs}

    for rec in recs:
        conn.execute(
            """
            INSERT INTO recommendations (
              fingerprint, category, title, details, priority, status, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'open', ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
              category = excluded.category,
              title = excluded.title,
              details = excluded.details,
              priority = excluded.priority,
              updated_at = excluded.updated_at
            """,
            (rec.fingerprint, rec.category, rec.title, rec.details, rec.priority, now),
        )

    for row in conn.execute("SELECT fingerprint FROM recommendations"):
        fp = row["fingerprint"]
        if fp not in current_fingerprints:
            conn.execute(
                "UPDATE recommendations SET status = 'resolved', updated_at = ? WHERE fingerprint = ?",
                (now, fp),
            )


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def run_scan(db_path: Path, projects_root: Path, linear_team_id: str | None) -> dict[str, int]:
    conn = db_connect(db_path)
    init_db(conn)

    repos_paths = find_git_repos(projects_root)
    snapshots = [snapshot_repo(path) for path in repos_paths]
    for snap in snapshots:
        upsert_repo(conn, snap)
    prune_missing_repos(conn, [s.path for s in snapshots])

    linear_task_count = 0
    linear_key = os.environ.get("LINEAR_API_KEY")
    if linear_key:
        try:
            linear_task_count = scan_linear_tasks(conn, linear_key, linear_team_id)
            set_meta(conn, "last_linear_scan_status", "ok")
        except Exception as exc:  # pragma: no cover - runtime networking path
            set_meta(conn, "last_linear_scan_status", f"error: {exc}")
    else:
        set_meta(conn, "last_linear_scan_status", "skipped: LINEAR_API_KEY missing")

    recs = generate_recommendations(snapshots, linear_task_count, projects_root)
    sync_recommendations(conn, recs)

    set_meta(conn, "last_scan_at", now_utc_iso())
    set_meta(conn, "projects_root", str(projects_root))
    conn.commit()
    conn.close()

    return {
        "repos": len(snapshots),
        "dirty_repos": len([s for s in snapshots if s.dirty]),
        "linear_tasks": linear_task_count,
        "recommendations": len(recs),
    }


def query_dashboard_state(conn: sqlite3.Connection) -> dict[str, Any]:
    repos = conn.execute(
        """
        SELECT *
        FROM repos
        ORDER BY focus_level DESC, dirty DESC, name ASC
        """
    ).fetchall()

    tasks = conn.execute(
        """
        SELECT *
        FROM tasks
        ORDER BY done ASC, priority ASC, updated_at DESC
        """
    ).fetchall()

    recs = conn.execute(
        """
        SELECT *
        FROM recommendations
        WHERE status != 'resolved'
        ORDER BY priority ASC, updated_at DESC
        """
    ).fetchall()

    meta_rows = conn.execute("SELECT key, value FROM meta").fetchall()
    meta = {r["key"]: r["value"] for r in meta_rows}

    active_window = conn.execute(
        """
        SELECT *
        FROM active_window
        WHERE singleton = 1
        """
    ).fetchone()

    window_events = conn.execute(
        """
        SELECT *
        FROM window_activity_events
        ORDER BY observed_at DESC, id DESC
        LIMIT 25
        """
    ).fetchall()

    agendas = conn.execute(
        """
        SELECT *
        FROM window_agendas
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (DEFAULT_AGENDA_HISTORY_LIMIT,),
    ).fetchall()

    opportunities = conn.execute(
        """
        SELECT *
        FROM interaction_opportunities
        WHERE status = 'open'
        ORDER BY confidence DESC, updated_at DESC
        LIMIT 40
        """
    ).fetchall()

    return {
        "repos": repos,
        "tasks": tasks,
        "recommendations": recs,
        "meta": meta,
        "active_window": active_window,
        "window_events": window_events,
        "agendas": agendas,
        "opportunities": opportunities,
    }


def esc(text: Any) -> str:
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def render_dashboard(state: dict[str, Any]) -> str:
    repos = state["repos"]
    tasks = state["tasks"]
    recs = state["recommendations"]
    meta = state["meta"]
    active_window = state["active_window"]
    window_events = state["window_events"]
    agendas = state["agendas"]
    opportunities = state["opportunities"]

    open_tasks = [t for t in tasks if not t["done"]]
    dirty_repos = [r for r in repos if r["dirty"]]

    repo_rows = []
    for r in repos:
        dirty_mark = "YES" if r["dirty"] else "no"
        age = r["last_commit_age_days"] if r["last_commit_age_days"] is not None else "-"
        repo_rows.append(
            f"""
            <tr>
              <td>{esc(r["name"])}</td>
              <td><code>{esc(r["branch"])}</code></td>
              <td>{dirty_mark}</td>
              <td>{esc(r["ahead"])}/{esc(r["behind"])}</td>
              <td>{esc(age)}</td>
              <td><code>{esc(r["remote_url"] or "")}</code></td>
              <td>
                <form method="post" action="/repo/update">
                  <input type="hidden" name="path" value="{esc(r["path"])}" />
                  <input type="number" min="0" max="3" name="focus_level" value="{esc(r["focus_level"])}" style="width:4rem" />
                  <input type="text" name="next_action" value="{esc(r["next_action"])}" placeholder="next action" style="width:16rem" />
                  <button type="submit">Save</button>
                </form>
              </td>
            </tr>
            """
        )

    task_rows = []
    for t in tasks:
        done_checked = "checked" if t["done"] else ""
        task_rows.append(
            f"""
            <tr>
              <td>{esc(t["source"])}</td>
              <td>{esc(t["external_id"])}</td>
              <td>{esc(t["title"])}</td>
              <td>{esc(t["status"])}</td>
              <td>{esc(t["priority"])}</td>
              <td>{f'<a href="{esc(t["url"])}" target="_blank">open</a>' if t["url"] else ""}</td>
              <td>
                <form method="post" action="/task/update">
                  <input type="hidden" name="source" value="{esc(t["source"])}" />
                  <input type="hidden" name="external_id" value="{esc(t["external_id"])}" />
                  <label><input type="checkbox" name="done" value="1" {done_checked} /> done</label>
                  <input type="text" name="notes" value="{esc(t["notes"])}" placeholder="notes" style="width:14rem" />
                  <button type="submit">Save</button>
                </form>
              </td>
            </tr>
            """
        )

    rec_rows = []
    for rec in recs:
        done_checked = "checked" if rec["status"] == "done" else ""
        rec_rows.append(
            f"""
            <tr>
              <td>{esc(rec["priority"])}</td>
              <td>{esc(rec["category"])}</td>
              <td>{esc(rec["title"])}</td>
              <td>{esc(rec["details"])}</td>
              <td>
                <form method="post" action="/recommendation/update">
                  <input type="hidden" name="fingerprint" value="{esc(rec["fingerprint"])}" />
                  <label><input type="checkbox" name="done" value="1" {done_checked} /> done</label>
                  <button type="submit">Save</button>
                </form>
              </td>
            </tr>
            """
        )

    window_rows = []
    for event in window_events:
        event_mode = event["suggested_reasoning_mode"] or "-"
        event_complexity = event["task_complexity"] or "unknown"
        window_rows.append(
            f"""
            <tr>
              <td>{esc(event["observed_at"])}</td>
              <td>{esc(event["scope"])}</td>
              <td>{esc(event["app"])}</td>
              <td>{esc(event["agenda_title"] or event["title"])}</td>
              <td>{esc(event["location"])}</td>
              <td>{esc(event_complexity)} / {esc(event_mode)}</td>
              <td>{esc(event["content_summary"] or event["summary"])}</td>
              <td>{esc(event["last_step"])}</td>
              <td>{esc(event["next_step"])}</td>
              <td>{esc(event["interaction_needs"])}</td>
            </tr>
            """
        )

    agenda_rows = []
    for agenda in agendas:
        agenda_rows.append(
            f"""
            <tr>
              <td>{esc(agenda["updated_at"])}</td>
              <td>{esc(agenda["scope"])}</td>
              <td>{esc(agenda["app"])}</td>
              <td>{esc(agenda["agenda_title"])}</td>
              <td>{esc(agenda["location"])}</td>
              <td>{esc(agenda["last_step"])}</td>
              <td>{esc(agenda["next_step"])}</td>
            </tr>
            """
        )

    opportunity_rows = []
    for op in opportunities:
        opportunity_rows.append(
            f"""
            <tr>
              <td>{esc(op["updated_at"])}</td>
              <td>{esc(op["confidence"])}</td>
              <td>{esc(op["category"])}</td>
              <td>{esc(op["signal"])}</td>
              <td>{esc(op["agenda_title"])}</td>
              <td>{esc(op["recommendation"])}</td>
            </tr>
            """
        )

    if active_window:
        current_title = esc(active_window["agenda_title"] or active_window["title"])
        current_location = esc(active_window["location"])
        current_summary = esc(active_window["content_summary"] or active_window["summary"])
        current_app = esc(active_window["app"])
        current_scope = esc(active_window["scope"])
        current_last_step = esc(active_window["last_step"])
        current_next_step = esc(active_window["next_step"])
        current_backend = esc(active_window["source_backend"])
        current_interaction_needs = esc(active_window["interaction_needs"])
        current_helper_recommendations = esc(active_window["helper_recommendations"])
        current_analysis_source = esc(active_window["analysis_source"])
        current_ocr_excerpt = esc((active_window["ocr_excerpt"] or "")[:240])
        current_complexity = esc(active_window["task_complexity"] or "unknown")
        current_suggested_mode = esc(active_window["suggested_reasoning_mode"] or "-")
        current_mode_rationale = esc(active_window["mode_rationale"] or "-")
        current_updated = esc(active_window["updated_at"])
        suggested_mode_raw = (active_window["suggested_reasoning_mode"] or "").strip().lower()
        if suggested_mode_raw in {"low", "medium", "high"}:
            apply_mode_controls = f"""
              <form method="post" action="/mode/apply" style="display:flex; gap:0.4rem; align-items:center; margin-top:0.5rem;">
                <input type="hidden" name="mode" value="{esc(suggested_mode_raw)}" />
                <button type="submit">Apply Suggested Mode</button>
                <span class="small">Writes <code>/reasoning {esc(suggested_mode_raw)}</code> to Codex config for future sessions.</span>
              </form>
            """
        else:
            apply_mode_controls = '<div class="small">No valid reasoning mode suggestion available yet.</div>'
    else:
        current_title = "No active window captured yet"
        current_location = "-"
        current_summary = "Start hub serving in a desktop session with a supported backend (swaymsg, hyprctl, xdotool, or xprop)."
        current_app = "-"
        current_scope = "-"
        current_last_step = "-"
        current_next_step = "-"
        current_backend = "-"
        current_interaction_needs = "-"
        current_helper_recommendations = "-"
        current_analysis_source = "-"
        current_ocr_excerpt = "-"
        current_complexity = "-"
        current_suggested_mode = "-"
        current_mode_rationale = "-"
        current_updated = "-"
        apply_mode_controls = '<div class="small">No active focus yet.</div>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="4" />
  <title>Fleet Control Hub</title>
  <style>
    :root {{
      --bg: #0b1020;
      --panel: #121a2f;
      --text: #e6ebff;
      --muted: #8da0d8;
      --accent: #4ade80;
      --warn: #f59e0b;
      --danger: #f87171;
      --border: #273354;
    }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: radial-gradient(1200px 600px at 5% 0%, #1d2a4f 0%, var(--bg) 55%);
      color: var(--text);
    }}
    .wrap {{ max-width: 1200px; margin: 0 auto; padding: 1rem; }}
    h1 {{ margin: 0.2rem 0 0.6rem; font-size: 1.7rem; }}
    .meta {{ color: var(--muted); margin-bottom: 1rem; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 0.8rem;
      margin-bottom: 1rem;
    }}
    .card {{
      background: linear-gradient(180deg, #19274a, var(--panel));
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 0.8rem;
    }}
    .card .k {{ color: var(--muted); font-size: 0.85rem; }}
    .card .v {{ font-size: 1.5rem; font-weight: 700; margin-top: 0.2rem; }}
    section {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 0.8rem;
      margin-bottom: 1rem;
      overflow-x: auto;
    }}
    h2 {{ margin: 0.2rem 0 0.8rem; font-size: 1.1rem; }}
    table {{
      border-collapse: collapse;
      width: 100%;
      min-width: 1000px;
    }}
    th, td {{
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
      padding: 0.45rem;
      font-size: 0.92rem;
    }}
    input, button {{
      background: #0f1730;
      color: var(--text);
      border: 1px solid #334477;
      border-radius: 8px;
      padding: 0.28rem 0.4rem;
      font-size: 0.88rem;
    }}
    button {{
      background: #1f2c55;
      cursor: pointer;
    }}
    .toolbar {{
      display: flex;
      gap: 0.6rem;
      flex-wrap: wrap;
      margin-bottom: 1rem;
    }}
    .small {{ font-size: 0.82rem; color: var(--muted); }}
    a {{ color: #9cc2ff; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Fleet Control Hub</h1>
    <div class="meta">
      Last scan: {esc(meta.get("last_scan_at", "never"))}
      | Linear scan: {esc(meta.get("last_linear_scan_status", "unknown"))}
      | Window tracking: {esc(meta.get("window_tracking_status", "unknown"))}
      | Backend: {esc(meta.get("window_tracking_backend", "n/a"))}
      | Scope: {esc(meta.get("window_tracking_scope", "active-window"))}
      | Helper: {esc(meta.get("interaction_helper_status", "unknown"))}
      | OCR: {esc(meta.get("interaction_helper_ocr_status", "unknown"))}
      | Mode Agent: {esc(meta.get("mode_efficiency_status", "unknown"))}
      | Mode Auto-Apply: {esc(meta.get("mode_efficiency_auto_apply", "off"))}
      | Stability Threshold: {esc(meta.get("mode_efficiency_stability_threshold", "n/a"))}
      | Last Apply: {esc(meta.get("mode_efficiency_last_apply_status", "n/a"))}
    </div>
    <div class="toolbar">
      <form method="post" action="/scan"><button type="submit">Rescan Now</button></form>
      <span class="small">
        Auto-refresh every 4s. Tip: export <code>LINEAR_API_KEY</code> and <code>LINEAR_TEAM_ID</code> before rescanning for richer task inventory.
      </span>
    </div>
    <div class="grid">
      <div class="card"><div class="k">Repositories</div><div class="v">{len(repos)}</div></div>
      <div class="card"><div class="k">Dirty Repositories</div><div class="v">{len(dirty_repos)}</div></div>
      <div class="card"><div class="k">Open Tasks</div><div class="v">{len(open_tasks)}</div></div>
      <div class="card"><div class="k">Open Recommendations</div><div class="v">{len(recs)}</div></div>
      <div class="card"><div class="k">Window Events</div><div class="v">{len(window_events)}</div></div>
      <div class="card"><div class="k">Tracked Agendas</div><div class="v">{len(agendas)}</div></div>
      <div class="card"><div class="k">Open Opportunities</div><div class="v">{len(opportunities)}</div></div>
    </div>

    <section>
      <h2>Live Focus</h2>
      <div><strong>Agenda Title:</strong> {current_title}</div>
      <div><strong>Scope:</strong> {current_scope}</div>
      <div><strong>App:</strong> {current_app}</div>
      <div><strong>Location:</strong> {current_location}</div>
      <div><strong>In Window:</strong> {current_summary}</div>
      <div><strong>Last Step:</strong> {current_last_step}</div>
      <div><strong>Next Step:</strong> {current_next_step}</div>
      <div><strong>Interaction Needs:</strong> {current_interaction_needs}</div>
      <div><strong>Helper Recommendations:</strong> {current_helper_recommendations}</div>
      <div><strong>Task Complexity:</strong> {current_complexity}</div>
      <div><strong>Suggested Reasoning Mode:</strong> {current_suggested_mode}</div>
      <div><strong>Mode Rationale:</strong> {current_mode_rationale}</div>
      {apply_mode_controls}
      <div class="small">Analysis source: {current_analysis_source}</div>
      <div class="small">OCR excerpt: {current_ocr_excerpt}</div>
      <div class="small">Capture backend: {current_backend}</div>
      <div class="small">Last update: {current_updated}</div>
    </section>

    <section>
      <h2>Recent Window Activity</h2>
      <table>
        <thead>
          <tr><th>Observed At</th><th>Scope</th><th>App</th><th>Agenda</th><th>Location</th><th>Complexity/Mode</th><th>In Window</th><th>Last Step</th><th>Next Step</th><th>Needs</th></tr>
        </thead>
        <tbody>
          {''.join(window_rows) if window_rows else '<tr><td colspan="10">No window changes captured yet.</td></tr>'}
        </tbody>
      </table>
    </section>

    <section>
      <h2>Interaction Opportunities</h2>
      <table>
        <thead>
          <tr><th>Updated</th><th>Confidence</th><th>Category</th><th>Signal</th><th>Agenda</th><th>Recommended Action</th></tr>
        </thead>
        <tbody>
          {''.join(opportunity_rows) if opportunity_rows else '<tr><td colspan="6">No open interaction blockers detected.</td></tr>'}
        </tbody>
      </table>
    </section>

    <section>
      <h2>Agenda Memory</h2>
      <table>
        <thead>
          <tr><th>Updated</th><th>Scope</th><th>App</th><th>Agenda</th><th>Location</th><th>Last Step</th><th>Next Step</th></tr>
        </thead>
        <tbody>
          {''.join(agenda_rows) if agenda_rows else '<tr><td colspan="7">No agenda history yet.</td></tr>'}
        </tbody>
      </table>
    </section>

    <section>
      <h2>Repository Inventory</h2>
      <table>
        <thead>
          <tr>
            <th>Name</th><th>Branch</th><th>Dirty</th><th>Ahead/Behind</th><th>Last Commit Age (d)</th><th>Remote</th><th>Management</th>
          </tr>
        </thead>
        <tbody>
          {''.join(repo_rows) if repo_rows else '<tr><td colspan="7">No repositories found.</td></tr>'}
        </tbody>
      </table>
    </section>

    <section>
      <h2>Tasks (Linear + local)</h2>
      <table>
        <thead>
          <tr>
            <th>Source</th><th>ID</th><th>Title</th><th>Status</th><th>Priority</th><th>URL</th><th>Management</th>
          </tr>
        </thead>
        <tbody>
          {''.join(task_rows) if task_rows else '<tr><td colspan="7">No tasks imported yet.</td></tr>'}
        </tbody>
      </table>
    </section>

    <section>
      <h2>Recommendations</h2>
      <table>
        <thead>
          <tr><th>P</th><th>Category</th><th>Title</th><th>Details</th><th>Status</th></tr>
        </thead>
        <tbody>
          {''.join(rec_rows) if rec_rows else '<tr><td colspan="5">No recommendations. Nice work.</td></tr>'}
        </tbody>
      </table>
    </section>
  </div>
</body>
</html>
"""


class HubHandler(BaseHTTPRequestHandler):
    db_path: Path
    projects_root: Path
    linear_team_id: str | None
    codex_config_path: Path

    def _conn(self) -> sqlite3.Connection:
        conn = db_connect(self.db_path)
        init_db(conn)
        return conn

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(body, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def _redirect(self, location: str = "/") -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        conn = self._conn()
        state = query_dashboard_state(conn)
        conn.close()

        payload = render_dashboard(state).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/scan":
            run_scan(self.db_path, self.projects_root, self.linear_team_id)
            self._redirect("/")
            return

        form = self._read_form()
        conn = self._conn()

        try:
            if self.path == "/repo/update":
                path = form.get("path", "")
                focus = int(form.get("focus_level", "0") or 0)
                next_action = form.get("next_action", "").strip()
                conn.execute(
                    "UPDATE repos SET focus_level = ?, next_action = ? WHERE path = ?",
                    (focus, next_action, path),
                )
            elif self.path == "/task/update":
                source = form.get("source", "")
                ext_id = form.get("external_id", "")
                done = 1 if form.get("done") == "1" else 0
                notes = form.get("notes", "").strip()
                conn.execute(
                    """
                    UPDATE tasks
                    SET done = ?, notes = ?, updated_at = ?
                    WHERE source = ? AND external_id = ?
                    """,
                    (done, notes, now_utc_iso(), source, ext_id),
                )
            elif self.path == "/recommendation/update":
                fp = form.get("fingerprint", "")
                status = "done" if form.get("done") == "1" else "open"
                conn.execute(
                    "UPDATE recommendations SET status = ?, updated_at = ? WHERE fingerprint = ?",
                    (status, now_utc_iso(), fp),
                )
            elif self.path == "/mode/apply":
                target_mode = form.get("mode", "").strip().lower()
                ok, message = set_codex_reasoning_mode(self.codex_config_path, target_mode)
                status = f"ok: {message}" if ok else f"error: {message}"
                set_meta(conn, "mode_efficiency_last_apply_status", status)
                set_meta(conn, "mode_efficiency_last_apply_at", now_utc_iso())
                if ok:
                    set_meta(conn, "mode_efficiency_last_applied_mode", target_mode)
            else:
                conn.close()
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            conn.commit()
        finally:
            conn.close()

        self._redirect("/")

    def log_message(self, fmt: str, *args: Any) -> None:
        # concise logging to stderr
        sys.stderr.write(f"[control-hub] {fmt % args}\n")


def cmd_scan(args: argparse.Namespace) -> int:
    summary = run_scan(args.db, args.projects_root, args.linear_team_id)
    print(json.dumps(summary, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    tracking_enabled = not args.no_window_tracking
    sqlite3_cli_path = shutil.which("sqlite3")
    sqlite3_cli_status = (
        f"available: {sqlite3_cli_path}"
        if sqlite3_cli_path
        else "missing: install sqlite3 for CLI DB inspection"
    )
    log_startup(
        f"startup requested: host={args.host} port={args.port} db={args.db} projects_root={args.projects_root}"
    )
    log_startup(f"sqlite3 CLI: {sqlite3_cli_status}")
    if not sqlite3_cli_path:
        log_startup(
            "hint: `sqlite3 ~/.local/share/fleet-control-hub/control_hub.db '.tables'` "
            "needs sqlite3 installed in PATH"
        )

    if args.scan_first:
        log_startup("scan-first enabled: starting inventory scan")
        summary = run_scan(args.db, args.projects_root, args.linear_team_id)
        log_startup(
            "scan complete: "
            f"repos={summary.get('repos', 0)} "
            f"dirty_repos={summary.get('dirty_repos', 0)} "
            f"linear_tasks={summary.get('linear_tasks', 0)} "
            f"recommendations={summary.get('recommendations', 0)}"
        )

    tracker: WindowTracker | None = None
    helper_agent = InteractionHelperAgent(
        enable_helper=not args.no_interaction_helper,
        enable_ocr=not args.no_window_ocr,
        ocr_max_chars=args.ocr_max_chars,
    )
    mode_agent = ModeEfficiencyAgent(
        enabled=not args.no_mode_efficiency_agent,
        auto_apply=args.auto_apply_reasoning_mode,
        codex_config_path=args.codex_config,
        stability_threshold=args.mode_stability_threshold,
    )
    conn = db_connect(args.db)
    init_db(conn)
    helper_status, helper_ocr_status = helper_agent.status()
    mode_status = mode_agent.status()
    set_meta(conn, "interaction_helper_status", helper_status)
    set_meta(conn, "interaction_helper_ocr_status", helper_ocr_status)
    set_meta(conn, "mode_efficiency_status", mode_status)
    set_meta(conn, "mode_efficiency_auto_apply", "on" if args.auto_apply_reasoning_mode else "off")
    set_meta(conn, "mode_efficiency_stability_threshold", str(args.mode_stability_threshold))
    set_meta(conn, "mode_efficiency_codex_config", str(args.codex_config))
    set_meta(conn, "sqlite3_cli_status", sqlite3_cli_status)
    set_meta(conn, "serve_bind_target", f"{args.host}:{args.port}")
    if tracking_enabled:
        supported, status = detect_window_tracking_support()
        set_meta(conn, "window_tracking_status", status)
        set_meta(conn, "window_tracking_scope", "active-tab for browsers; active-window for other apps")
        backend_hint = status.split(":", 1)[1].strip() if ":" in status else status
        set_meta(conn, "window_tracking_backend", backend_hint)
        log_startup(f"window tracking status: {status}")
        if supported:
            tracker = WindowTracker(
                db_path=args.db,
                projects_root=args.projects_root,
                helper_agent=helper_agent,
                mode_agent=mode_agent,
                poll_seconds=args.window_poll_seconds,
            )
    else:
        set_meta(conn, "window_tracking_status", "disabled: flag --no-window-tracking")
        set_meta(conn, "window_tracking_backend", "disabled")
        log_startup("window tracking status: disabled by --no-window-tracking")
    set_meta(conn, "startup_status", "initializing")
    conn.commit()
    conn.close()

    HubHandler.db_path = args.db
    HubHandler.projects_root = args.projects_root
    HubHandler.linear_team_id = args.linear_team_id
    HubHandler.codex_config_path = args.codex_config

    try:
        server = ThreadingHTTPServer((args.host, args.port), HubHandler)
    except OSError as exc:
        bind_msg = f"failed to bind {args.host}:{args.port}: {exc}"
        log_startup(bind_msg, err=True)
        log_startup("hint: choose a different --port or stop the process using this port", err=True)
        if tracker:
            tracker.stop()
        conn = db_connect(args.db)
        init_db(conn)
        set_meta(conn, "startup_status", f"error: {bind_msg}")
        conn.commit()
        conn.close()
        return 1

    if tracker:
        tracker.start()
    conn = db_connect(args.db)
    init_db(conn)
    set_meta(conn, "startup_status", f"running: http://{args.host}:{args.port}")
    conn.commit()
    conn.close()
    log_startup(f"running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        conn = db_connect(args.db)
        init_db(conn)
        set_meta(conn, "startup_status", "stopped")
        if tracker:
            tracker.stop()
            set_meta(conn, "window_tracking_status", "stopped")
            set_meta(conn, "window_tracking_backend", "stopped")
            set_meta(conn, "interaction_helper_status", "stopped")
            set_meta(conn, "mode_efficiency_status", "stopped")
        conn.commit()
        conn.close()
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fleet Control Hub Agent")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite DB path (default: {DEFAULT_DB})")
    p.add_argument(
        "--projects-root",
        type=Path,
        default=DEFAULT_PROJECTS_ROOT,
        help=f"Projects root to inventory (default: {DEFAULT_PROJECTS_ROOT})",
    )
    p.add_argument(
        "--linear-team-id",
        default=os.environ.get("LINEAR_TEAM_ID"),
        help="Optional Linear team ID filter. Defaults to LINEAR_TEAM_ID env.",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    sp_scan = sub.add_parser("scan", help="Run inventory scan and update DB.")
    sp_scan.set_defaults(func=cmd_scan)

    sp_serve = sub.add_parser("serve", help="Serve dashboard from existing DB.")
    sp_serve.add_argument("--host", default="127.0.0.1")
    sp_serve.add_argument("--port", type=int, default=8765)
    sp_serve.add_argument("--scan-first", action="store_true", help="Run scan before serving.")
    sp_serve.add_argument(
        "--no-window-tracking",
        action="store_true",
        help="Disable live active-window tracking.",
    )
    sp_serve.add_argument(
        "--window-poll-seconds",
        type=float,
        default=DEFAULT_WINDOW_POLL_SECONDS,
        help=f"Window tracking poll interval in seconds (default: {DEFAULT_WINDOW_POLL_SECONDS}).",
    )
    sp_serve.add_argument(
        "--no-interaction-helper",
        action="store_true",
        help="Disable interaction helper analysis and recommendations.",
    )
    sp_serve.add_argument(
        "--no-window-ocr",
        action="store_true",
        help="Disable OCR attempts for active-window analysis.",
    )
    sp_serve.add_argument(
        "--ocr-max-chars",
        type=int,
        default=DEFAULT_OCR_MAX_CHARS,
        help=f"Maximum OCR excerpt length (default: {DEFAULT_OCR_MAX_CHARS}).",
    )
    sp_serve.add_argument(
        "--no-mode-efficiency-agent",
        action="store_true",
        help="Disable simple-task detection and reasoning-mode recommendations.",
    )
    sp_serve.add_argument(
        "--auto-apply-reasoning-mode",
        action="store_true",
        help="Auto-write suggested reasoning mode into Codex config when focus complexity changes.",
    )
    sp_serve.add_argument(
        "--codex-config",
        type=Path,
        default=DEFAULT_CODEX_CONFIG,
        help=f"Codex config path used for mode apply (default: {DEFAULT_CODEX_CONFIG}).",
    )
    sp_serve.add_argument(
        "--mode-stability-threshold",
        type=int,
        default=DEFAULT_MODE_STABILITY_THRESHOLD,
        help=(
            "Consecutive matching mode recommendations required before auto-apply writes "
            f"(default: {DEFAULT_MODE_STABILITY_THRESHOLD})."
        ),
    )
    sp_serve.set_defaults(func=cmd_serve)

    sp_scan_serve = sub.add_parser("scan-serve", help="Run scan, then serve dashboard.")
    sp_scan_serve.add_argument("--host", default="127.0.0.1")
    sp_scan_serve.add_argument("--port", type=int, default=8765)
    sp_scan_serve.add_argument(
        "--no-window-tracking",
        action="store_true",
        help="Disable live active-window tracking.",
    )
    sp_scan_serve.add_argument(
        "--window-poll-seconds",
        type=float,
        default=DEFAULT_WINDOW_POLL_SECONDS,
        help=f"Window tracking poll interval in seconds (default: {DEFAULT_WINDOW_POLL_SECONDS}).",
    )
    sp_scan_serve.add_argument(
        "--no-interaction-helper",
        action="store_true",
        help="Disable interaction helper analysis and recommendations.",
    )
    sp_scan_serve.add_argument(
        "--no-window-ocr",
        action="store_true",
        help="Disable OCR attempts for active-window analysis.",
    )
    sp_scan_serve.add_argument(
        "--ocr-max-chars",
        type=int,
        default=DEFAULT_OCR_MAX_CHARS,
        help=f"Maximum OCR excerpt length (default: {DEFAULT_OCR_MAX_CHARS}).",
    )
    sp_scan_serve.add_argument(
        "--no-mode-efficiency-agent",
        action="store_true",
        help="Disable simple-task detection and reasoning-mode recommendations.",
    )
    sp_scan_serve.add_argument(
        "--auto-apply-reasoning-mode",
        action="store_true",
        help="Auto-write suggested reasoning mode into Codex config when focus complexity changes.",
    )
    sp_scan_serve.add_argument(
        "--codex-config",
        type=Path,
        default=DEFAULT_CODEX_CONFIG,
        help=f"Codex config path used for mode apply (default: {DEFAULT_CODEX_CONFIG}).",
    )
    sp_scan_serve.add_argument(
        "--mode-stability-threshold",
        type=int,
        default=DEFAULT_MODE_STABILITY_THRESHOLD,
        help=(
            "Consecutive matching mode recommendations required before auto-apply writes "
            f"(default: {DEFAULT_MODE_STABILITY_THRESHOLD})."
        ),
    )
    sp_scan_serve.set_defaults(func=cmd_serve, scan_first=True)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
