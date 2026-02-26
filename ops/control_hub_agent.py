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
import sqlite3
import subprocess
import sys
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
        """
    )
    conn.commit()


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

    return {
        "repos": repos,
        "tasks": tasks,
        "recommendations": recs,
        "meta": meta,
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

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
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
    <div class="meta">Last scan: {esc(meta.get("last_scan_at", "never"))} | Linear scan: {esc(meta.get("last_linear_scan_status", "unknown"))}</div>
    <div class="toolbar">
      <form method="post" action="/scan"><button type="submit">Rescan Now</button></form>
      <span class="small">Tip: export <code>LINEAR_API_KEY</code> and <code>LINEAR_TEAM_ID</code> before rescanning for richer task inventory.</span>
    </div>
    <div class="grid">
      <div class="card"><div class="k">Repositories</div><div class="v">{len(repos)}</div></div>
      <div class="card"><div class="k">Dirty Repositories</div><div class="v">{len(dirty_repos)}</div></div>
      <div class="card"><div class="k">Open Tasks</div><div class="v">{len(open_tasks)}</div></div>
      <div class="card"><div class="k">Open Recommendations</div><div class="v">{len(recs)}</div></div>
    </div>

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
    if args.scan_first:
        run_scan(args.db, args.projects_root, args.linear_team_id)

    HubHandler.db_path = args.db
    HubHandler.projects_root = args.projects_root
    HubHandler.linear_team_id = args.linear_team_id

    server = ThreadingHTTPServer((args.host, args.port), HubHandler)
    print(f"Control Hub running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
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
    sp_serve.set_defaults(func=cmd_serve)

    sp_scan_serve = sub.add_parser("scan-serve", help="Run scan, then serve dashboard.")
    sp_scan_serve.add_argument("--host", default="127.0.0.1")
    sp_scan_serve.add_argument("--port", type=int, default=8765)
    sp_scan_serve.set_defaults(func=cmd_serve, scan_first=True)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

