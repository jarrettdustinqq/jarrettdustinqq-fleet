#!/usr/bin/env python3
"""Synthesize Codex terminal chats into actionable workstream recommendations."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


DEFAULT_STATE_DB = Path.home() / ".codex" / "state_5.sqlite"
DEFAULT_HISTORY = Path.home() / ".codex" / "history.jsonl"
DEFAULT_MARKDOWN_OUT = Path.home() / ".local" / "share" / "fleet-control-hub" / "chat_work_brief.md"
DEFAULT_JSON_OUT = Path.home() / ".local" / "share" / "fleet-control-hub" / "chat_work_brief.json"
DEFAULT_CODEX_PROMPT_OUT = Path.home() / ".local" / "share" / "fleet-control-hub" / "chat_work_codex_prompt.txt"
DEFAULT_DELTA_LOG = Path.home() / ".local" / "share" / "fleet-control-hub" / "chat_work_deltas.jsonl"
DEFAULT_ACK_STATE = Path.home() / ".local" / "share" / "fleet-control-hub" / "chat_work_ack.json"

BLOCKED_PATTERNS = [
    "error",
    "failed",
    "permission denied",
    "timed out",
    "not authenticated",
    "could not",
    "cannot",
    "missing",
    "blocked",
]
DONE_PATTERNS = [
    "done",
    "worked",
    "complete",
    "completed",
    "fixed",
    "success",
    "resolved",
]
CHECK_HINTS = [
    "access-gate",
    "access-audit",
    "pytest",
    "test",
    "build",
    "deploy",
    "hub-scan",
    "hub-serve",
    "remote-agent",
]
NON_TARGET_WORDS = {
    "sure",
    "this",
    "that",
    "it",
    "we",
    "the",
    "a",
    "an",
}

TOPIC_RULES = [
    (
        "account-incident-response",
        [
            "incident",
            "linear",
            "github",
            "sessions",
            "api keys",
            "containment",
            "re-baseline",
        ],
    ),
    (
        "continuity-ledger-access-automation",
        [
            "continuity",
            "ledger",
            "access_",
            "access-gate",
            "access-audit",
            "witness",
            "automation_gate",
        ],
    ),
    (
        "fleet-remote-access",
        [
            "fleetctl",
            "remote-agent",
            "vps",
            "ssh",
            "discover",
            "user@host",
        ],
    ),
    (
        "control-hub-workflow",
        [
            "control hub",
            "hub-scan",
            "hub-serve",
            "interactive progress agent",
            "dashboard",
        ],
    ),
    (
        "bootstrap-nix-setup",
        [
            "nix",
            "bootstrap",
            "crostini",
            "termina",
            "chromebook",
        ],
    ),
]

PROFILE_WEIGHTS: dict[str, dict[str, int]] = {
    "balanced": {
        "risk_incident": 40,
        "risk_continuity": 30,
        "risk_fleet": 22,
        "recent_2h": 20,
        "recent_24h": 10,
        "blocked_unit": 8,
        "blocked_cap": 24,
        "checks_bonus": 12,
        "persistent_bonus": 12,
        "done_unit": 6,
        "done_cap": 24,
    },
    "security-first": {
        "risk_incident": 70,
        "risk_continuity": 38,
        "risk_fleet": 20,
        "recent_2h": 18,
        "recent_24h": 8,
        "blocked_unit": 10,
        "blocked_cap": 30,
        "checks_bonus": 14,
        "persistent_bonus": 16,
        "done_unit": 5,
        "done_cap": 20,
    },
    "ship-fast": {
        "risk_incident": 35,
        "risk_continuity": 24,
        "risk_fleet": 20,
        "recent_2h": 28,
        "recent_24h": 14,
        "blocked_unit": 6,
        "blocked_cap": 18,
        "checks_bonus": 8,
        "persistent_bonus": 8,
        "done_unit": 8,
        "done_cap": 28,
    },
    "cleanup-first": {
        "risk_incident": 30,
        "risk_continuity": 24,
        "risk_fleet": 18,
        "recent_2h": 10,
        "recent_24h": 6,
        "blocked_unit": 6,
        "blocked_cap": 18,
        "checks_bonus": 7,
        "persistent_bonus": 10,
        "done_unit": 4,
        "done_cap": 16,
    },
}


@dataclasses.dataclass
class ThreadRecord:
    thread_id: str
    title: str
    first_user_message: str
    cwd: str
    created_at: int
    updated_at: int
    archived: bool
    source: str
    agent_role: str | None
    parent_thread_id: str | None
    last_user_text: str
    last_user_ts: int
    blocked_signals: int
    done_signals: int
    failing_checks: list[str]
    topic: str
    role_class: str
    priority: int
    priority_reason: list[str]
    repo_root: str | None


@dataclasses.dataclass
class RepoState:
    root: str
    branch: str
    dirty: bool
    ahead: int
    behind: int
    last_commit_age_hours: float | None


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def to_iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def safe_text(value: str, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", (value or "").strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def parse_source(source: str) -> tuple[str | None, str]:
    if not source or source == "cli":
        return None, "primary"
    try:
        obj = json.loads(source)
    except json.JSONDecodeError:
        return None, "other"

    if not isinstance(obj, dict):
        return None, "other"

    subagent_obj = obj.get("subagent")
    if not isinstance(subagent_obj, dict):
        return None, "other"

    thread_spawn = subagent_obj.get("thread_spawn")
    if not isinstance(thread_spawn, dict):
        return None, "other"

    parent = thread_spawn.get("parent_thread_id")
    if parent:
        return str(parent), "subagent"
    return None, "other"


def load_history(history_path: Path) -> dict[str, tuple[int, str]]:
    by_session: dict[str, tuple[int, str]] = {}
    if not history_path.exists():
        return by_session

    with history_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = str(row.get("session_id") or "").strip()
            ts = int(row.get("ts") or 0)
            text = str(row.get("text") or "")
            if not sid:
                continue
            prev = by_session.get(sid)
            if prev is None or ts >= prev[0]:
                by_session[sid] = (ts, text)
    return by_session


def classify_topic(*texts: str) -> str:
    hay = " ".join(texts).lower()
    best_topic = "general"
    best_score = 0
    for topic, needles in TOPIC_RULES:
        score = sum(1 for needle in needles if needle in hay)
        if score > best_score:
            best_score = score
            best_topic = topic
    return best_topic if best_score > 0 else "general"


def count_signals(text: str, patterns: Iterable[str]) -> int:
    lower = (text or "").lower()
    return sum(1 for pat in patterns if pat in lower)


def extract_failing_checks(*texts: str) -> list[str]:
    blob = " ".join(texts).lower()
    blocked = any(token in blob for token in BLOCKED_PATTERNS)
    found = [token for token in CHECK_HINTS if token in blob]
    if blocked:
        if "make " in blob:
            for target in sorted(set(re.findall(r"make\s+([a-zA-Z0-9_.:-]+)", blob))):
                if target in NON_TARGET_WORDS or len(target) < 3:
                    continue
                found.append(target)
        if "npm run " in blob:
            for target in sorted(set(re.findall(r"npm\s+run\s+([a-zA-Z0-9_.:-]+)", blob))):
                if target in NON_TARGET_WORDS or len(target) < 3:
                    continue
                found.append(target)
    deduped = []
    seen = set()
    for item in found:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def derive_role_class(parent_thread_id: str | None, role: str | None) -> str:
    if parent_thread_id:
        return f"subagent:{role or 'unknown'}"
    return "primary"


def load_delta_history(path: Path, lookback: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if lookback <= 0:
        return rows
    return rows[-lookback:]


def persistent_blocker_topics(delta_rows: list[dict[str, Any]]) -> set[str]:
    counts: dict[str, int] = defaultdict(int)
    for row in delta_rows:
        for topic in row.get("blocked_topics") or []:
            counts[str(topic)] += 1
    return {topic for topic, count in counts.items() if count >= 2}


def visible_primary_threads(threads: list[ThreadRecord], ack_state: dict[str, Any]) -> list[ThreadRecord]:
    ack_threads = set(ack_state.get("acked_threads") or [])
    ack_topics = set(ack_state.get("acked_topics") or [])
    primaries = [t for t in threads if t.role_class == "primary"]
    return [t for t in primaries if t.thread_id not in ack_threads and t.topic not in ack_topics]


def currently_blocked_topics(threads: list[ThreadRecord], ack_state: dict[str, Any]) -> set[str]:
    visible = visible_primary_threads(threads, ack_state)
    return {t.topic for t in visible if t.blocked_signals > 0}


def compute_priority(
    topic: str,
    archived: bool,
    updated_at: int,
    blocked_signals: int,
    done_signals: int,
    role_class: str,
    failing_checks: list[str],
    persistent_topics: set[str],
    profile: str,
) -> tuple[int, list[str]]:
    weights = PROFILE_WEIGHTS.get(profile, PROFILE_WEIGHTS["balanced"])
    score = 0
    reasons: list[str] = []
    now_ts = int(utc_now().timestamp())
    age_hours = max(0.0, (now_ts - updated_at) / 3600.0)

    if archived:
        score -= 50
        reasons.append("archived thread")
    if role_class != "primary":
        score -= 10
        reasons.append("subagent/support thread")

    if topic == "account-incident-response":
        score += weights["risk_incident"]
        reasons.append("security-sensitive stream")
    elif topic == "continuity-ledger-access-automation":
        score += weights["risk_continuity"]
        reasons.append("compliance/integrity stream")
    elif topic == "fleet-remote-access":
        score += weights["risk_fleet"]
        reasons.append("infrastructure access stream")

    if age_hours <= 2:
        score += weights["recent_2h"]
        reasons.append("recently active (<2h)")
    elif age_hours <= 24:
        score += weights["recent_24h"]
        reasons.append("active in last 24h")

    if blocked_signals > 0:
        score += min(weights["blocked_cap"], blocked_signals * weights["blocked_unit"])
        reasons.append("blocker/error signals present")
    if failing_checks:
        score += weights["checks_bonus"]
        reasons.append("check/build failures referenced")
    if topic in persistent_topics:
        score += weights["persistent_bonus"]
        reasons.append("persistent blocker trend")
    if done_signals > 0:
        score -= min(weights["done_cap"], done_signals * weights["done_unit"])
        reasons.append("completion signals present")

    return score, reasons


def find_git_root(path: str) -> str | None:
    if not path:
        return None
    candidate = Path(path).expanduser()
    if not candidate.exists():
        return None
    start = candidate if candidate.is_dir() else candidate.parent
    proc = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return root or None


def collect_repo_state(repo_root: str) -> RepoState | None:
    proc = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip() or "unknown"

    dirty_proc = subprocess.run(
        ["git", "-C", repo_root, "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    dirty = bool((dirty_proc.stdout or "").strip())

    ahead = 0
    behind = 0
    ab_proc = subprocess.run(
        ["git", "-C", repo_root, "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if ab_proc.returncode == 0:
        parts = (ab_proc.stdout or "").strip().split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            behind = int(parts[0])
            ahead = int(parts[1])

    age_hours = None
    ts_proc = subprocess.run(
        ["git", "-C", repo_root, "log", "-1", "--format=%ct"],
        capture_output=True,
        text=True,
        check=False,
    )
    if ts_proc.returncode == 0 and (ts_proc.stdout or "").strip().isdigit():
        commit_ts = int((ts_proc.stdout or "").strip())
        age_hours = max(0.0, (int(utc_now().timestamp()) - commit_ts) / 3600.0)

    return RepoState(
        root=repo_root,
        branch=branch,
        dirty=dirty,
        ahead=ahead,
        behind=behind,
        last_commit_age_hours=age_hours,
    )


def collect_repo_states(threads: list[ThreadRecord]) -> dict[str, RepoState]:
    roots = sorted({t.repo_root for t in threads if t.repo_root})
    out: dict[str, RepoState] = {}
    for root in roots:
        state = collect_repo_state(root)
        if state:
            out[root] = state
    return out


def load_threads(
    db_path: Path,
    history: dict[str, tuple[int, str]],
    include_archived: bool,
    persistent_topics: set[str],
    profile: str,
) -> list[ThreadRecord]:
    if not db_path.exists():
        raise FileNotFoundError(f"state DB not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
              id,
              title,
              first_user_message,
              cwd,
              created_at,
              updated_at,
              archived,
              source,
              agent_role
            FROM threads
            ORDER BY updated_at DESC
            """
        ).fetchall()
    finally:
        conn.close()

    out: list[ThreadRecord] = []
    for row in rows:
        archived = bool(row["archived"])
        if archived and not include_archived:
            continue
        thread_id = str(row["id"])
        last_ts, last_text = history.get(thread_id, (0, ""))
        parent, _source_class = parse_source(str(row["source"] or ""))
        role_class = derive_role_class(parent, row["agent_role"])
        topic = classify_topic(
            str(row["title"] or ""),
            str(row["first_user_message"] or ""),
            last_text,
            str(row["cwd"] or ""),
        )
        blocked = count_signals(last_text + " " + str(row["title"] or ""), BLOCKED_PATTERNS)
        done = count_signals(last_text, DONE_PATTERNS)
        failing_checks = extract_failing_checks(str(row["title"] or ""), last_text)
        priority, reasons = compute_priority(
            topic=topic,
            archived=archived,
            updated_at=int(row["updated_at"]),
            blocked_signals=blocked,
            done_signals=done,
            role_class=role_class,
            failing_checks=failing_checks,
            persistent_topics=persistent_topics,
            profile=profile,
        )
        out.append(
            ThreadRecord(
                thread_id=thread_id,
                title=str(row["title"] or ""),
                first_user_message=str(row["first_user_message"] or ""),
                cwd=str(row["cwd"] or ""),
                created_at=int(row["created_at"]),
                updated_at=int(row["updated_at"]),
                archived=archived,
                source=str(row["source"] or ""),
                agent_role=row["agent_role"],
                parent_thread_id=parent,
                last_user_text=last_text,
                last_user_ts=last_ts,
                blocked_signals=blocked,
                done_signals=done,
                failing_checks=failing_checks,
                topic=topic,
                role_class=role_class,
                priority=priority,
                priority_reason=reasons,
                repo_root=find_git_root(str(row["cwd"] or "")),
            )
        )
    return out


def load_live_codex_processes() -> list[dict[str, Any]]:
    cmd = ["ps", "-eo", "pid=,tty=,etimes=,args="]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return []
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        if " codex" not in raw and "/codex" not in raw:
            continue
        if "rg -n codex" in raw:
            continue
        parts = raw.split(maxsplit=3)
        if len(parts) < 4:
            continue
        pid, tty, etimes, args = parts
        rows.append(
            {
                "pid": int(pid),
                "tty": tty,
                "elapsed_seconds": int(etimes) if etimes.isdigit() else 0,
                "args": args,
            }
        )
    return rows


def load_ack_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"acked_threads": [], "acked_topics": [], "updated_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"acked_threads": [], "acked_topics": [], "updated_at": None}
    return {
        "acked_threads": sorted(set(data.get("acked_threads") or [])),
        "acked_topics": sorted(set(data.get("acked_topics") or [])),
        "updated_at": data.get("updated_at"),
    }


def write_ack_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now().isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def apply_ack_updates(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    thread_set = set(state.get("acked_threads") or [])
    topic_set = set(state.get("acked_topics") or [])

    if args.clear_acks:
        thread_set.clear()
        topic_set.clear()

    for tid in args.ack_thread:
        if tid:
            thread_set.add(tid)
    for topic in args.ack_topic:
        if topic:
            topic_set.add(topic)
    for tid in args.unack_thread:
        thread_set.discard(tid)
    for topic in args.unack_topic:
        topic_set.discard(topic)

    return {
        "acked_threads": sorted(thread_set),
        "acked_topics": sorted(topic_set),
        "updated_at": state.get("updated_at"),
    }


def summarize_workstreams(
    threads: list[ThreadRecord],
    repo_states: dict[str, RepoState],
) -> list[dict[str, Any]]:
    streams: dict[str, list[ThreadRecord]] = defaultdict(list)
    for thread in threads:
        if thread.role_class == "primary":
            streams[thread.topic].append(thread)

    out: list[dict[str, Any]] = []
    for topic, items in streams.items():
        items_sorted = sorted(items, key=lambda t: (-t.priority, -t.updated_at))
        blocked_total = sum(t.blocked_signals for t in items_sorted)
        done_total = sum(t.done_signals for t in items_sorted)
        total_priority = sum(t.priority for t in items_sorted)
        newest = max(items_sorted, key=lambda t: t.updated_at)

        repo_summaries = []
        seen_roots = set()
        for t in items_sorted:
            if not t.repo_root or t.repo_root in seen_roots:
                continue
            seen_roots.add(t.repo_root)
            state = repo_states.get(t.repo_root)
            if state:
                repo_summaries.append(
                    {
                        "root": state.root,
                        "branch": state.branch,
                        "dirty": state.dirty,
                        "ahead": state.ahead,
                        "behind": state.behind,
                        "last_commit_age_hours": state.last_commit_age_hours,
                    }
                )

        check_hints = sorted(
            {
                check
                for t in items_sorted
                for check in t.failing_checks
            }
        )

        out.append(
            {
                "topic": topic,
                "thread_count": len(items_sorted),
                "blocked_signals": blocked_total,
                "done_signals": done_total,
                "priority_score": total_priority,
                "latest_updated_at": newest.updated_at,
                "latest_title": newest.title,
                "threads": [t.thread_id for t in items_sorted],
                "repos": repo_summaries,
                "check_hints": check_hints,
            }
        )

    out.sort(key=lambda r: (-int(r["priority_score"]), -int(r["latest_updated_at"])))
    return out


def recommendation_for_thread(thread: ThreadRecord, persistent_topics: set[str]) -> str:
    if thread.topic == "account-incident-response":
        if thread.blocked_signals > 0:
            return "Finish authentication handoff + containment checkpoint to close active security workflow."
        return "Close the incident loop by confirming containment and re-baseline evidence."
    if thread.topic == "continuity-ledger-access-automation":
        if thread.blocked_signals > 0:
            return "Resolve failing gate/audit checks before merging integrity changes."
        return "Finalize commit grouping and close open review/risk questions."
    if thread.topic == "fleet-remote-access":
        if thread.blocked_signals > 0:
            return "Unblock remote-agent flow by selecting/validating VPS target and rerunning."
        return "Complete remote access bootstrap and persist chosen VPS target."
    if thread.topic == "control-hub-workflow":
        return "Stabilize dashboard workflow and document one canonical entrypoint."
    if thread.topic in persistent_topics:
        return "Persistent trend indicates this stream is repeatedly stalling; close or archive decisively."
    if thread.done_signals > 0:
        return "Mark this stream complete or archive it to reduce active context load."
    return "Either finish the concrete next step or archive this thread if superseded."


def suggest_archives(
    primaries: list[ThreadRecord],
    ack_threads: set[str],
    ack_topics: set[str],
    max_items: int,
) -> list[dict[str, Any]]:
    now_ts = int(utc_now().timestamp())
    candidates: list[tuple[int, ThreadRecord, str]] = []
    for t in primaries:
        if t.thread_id in ack_threads or t.topic in ack_topics:
            continue
        age_hours = max(0.0, (now_ts - t.updated_at) / 3600.0)
        reason = ""
        score = 0
        if t.done_signals >= 1 and age_hours >= 12:
            score = 80 + min(20, int(age_hours // 24))
            reason = "completion signals + stale thread"
        elif t.topic == "general" and age_hours >= 24 and t.blocked_signals == 0:
            score = 60 + min(15, int(age_hours // 24))
            reason = "general stream stale with no active blockers"
        elif t.priority <= 10 and age_hours >= 48:
            score = 50 + min(20, int(age_hours // 24))
            reason = "low-priority stale thread"
        if score > 0:
            candidates.append((score, t, reason))

    candidates.sort(key=lambda x: (-x[0], x[1].updated_at))
    out = []
    for score, thread, reason in candidates[:max_items]:
        out.append(
            {
                "thread_id": thread.thread_id,
                "topic": thread.topic,
                "title": safe_text(thread.title, 140),
                "updated_at": to_iso(thread.updated_at),
                "age_hours": round((now_ts - thread.updated_at) / 3600.0, 1),
                "reason": reason,
                "suggested_command": f"./fleetctl chat-agent --ack-thread {thread.thread_id}",
                "archive_score": score,
            }
        )
    return out


def build_report(
    threads: list[ThreadRecord],
    live_processes: list[dict[str, Any]],
    top_n: int,
    ack_state: dict[str, Any],
    persistent_topics: set[str],
    repo_states: dict[str, RepoState],
    delta_rows: list[dict[str, Any]],
    archive_suggest_max: int,
    profile: str,
) -> dict[str, Any]:
    primaries = [t for t in threads if t.role_class == "primary"]
    subagents = [t for t in threads if t.role_class != "primary"]

    ack_threads = set(ack_state.get("acked_threads") or [])
    ack_topics = set(ack_state.get("acked_topics") or [])

    visible_primaries = [
        t
        for t in primaries
        if t.thread_id not in ack_threads and t.topic not in ack_topics
    ]
    suppressed_primaries = [t for t in primaries if t not in visible_primaries]

    sorted_threads = sorted(visible_primaries, key=lambda t: (-t.priority, -t.updated_at))
    top_threads = sorted_threads[:top_n]
    streams = summarize_workstreams(visible_primaries, repo_states)
    archive_suggestions = suggest_archives(
        primaries=primaries,
        ack_threads=ack_threads,
        ack_topics=ack_topics,
        max_items=max(0, archive_suggest_max),
    )

    recs = []
    for t in top_threads[: min(7, len(top_threads))]:
        recs.append(
            {
                "thread_id": t.thread_id,
                "topic": t.topic,
                "title": safe_text(t.title, 140),
                "priority": t.priority,
                "updated_at": to_iso(t.updated_at),
                "why_now": recommendation_for_thread(t, persistent_topics),
                "last_user_text": safe_text(t.last_user_text, 180),
                "failing_checks": t.failing_checks,
                "repo_root": t.repo_root,
            }
        )

    report = {
        "generated_at": utc_now().isoformat().replace("+00:00", "Z"),
        "profile": profile,
        "counts": {
            "total_threads": len(threads),
            "open_primary_threads": len([t for t in primaries if not t.archived]),
            "visible_primary_threads": len(visible_primaries),
            "suppressed_primary_threads": len(suppressed_primaries),
            "archived_threads": len([t for t in threads if t.archived]),
            "subagent_threads": len(subagents),
            "live_codex_processes": len(live_processes),
        },
        "ack_state": ack_state,
        "trend": {
            "history_points_considered": len(delta_rows),
            "persistent_blocker_topics": sorted(persistent_topics),
        },
        "workstreams": streams,
        "top_threads": [
            {
                "thread_id": t.thread_id,
                "topic": t.topic,
                "priority": t.priority,
                "updated_at": to_iso(t.updated_at),
                "title": safe_text(t.title, 160),
                "cwd": t.cwd,
                "repo_root": t.repo_root,
                "blocked_signals": t.blocked_signals,
                "done_signals": t.done_signals,
                "failing_checks": t.failing_checks,
                "priority_reason": t.priority_reason[:5],
            }
            for t in top_threads
        ],
        "recommendations": recs,
        "archive_suggestions": archive_suggestions,
        "live_processes": live_processes,
    }
    return report


def append_delta_snapshot(delta_log: Path, report: dict[str, Any]) -> None:
    delta_log.parent.mkdir(parents=True, exist_ok=True)
    blocked_topics = [
        w["topic"]
        for w in report.get("workstreams") or []
        if int(w.get("blocked_signals") or 0) > 0
    ]
    snapshot = {
        "ts": report["generated_at"],
        "open_primary_threads": report["counts"]["open_primary_threads"],
        "visible_primary_threads": report["counts"]["visible_primary_threads"],
        "suppressed_primary_threads": report["counts"]["suppressed_primary_threads"],
        "top_topics": [w["topic"] for w in (report.get("workstreams") or [])[:5]],
        "blocked_topics": blocked_topics,
        "top_recommendations": [
            {
                "thread_id": r.get("thread_id"),
                "topic": r.get("topic"),
                "priority": r.get("priority"),
            }
            for r in (report.get("recommendations") or [])[:5]
        ],
    }
    with delta_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def to_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Chat Work Agent Brief")
    lines.append("")
    lines.append(f"- Generated: `{report['generated_at']}`")
    lines.append(f"- Profile: `{report['profile']}`")
    lines.append(f"- Open primary chats: `{report['counts']['open_primary_threads']}`")
    lines.append(f"- Visible primary chats: `{report['counts']['visible_primary_threads']}`")
    lines.append(f"- Suppressed by ack: `{report['counts']['suppressed_primary_threads']}`")
    lines.append(f"- Saved/archived chats: `{report['counts']['archived_threads']}`")
    lines.append(f"- Subagent/support chats: `{report['counts']['subagent_threads']}`")
    lines.append(f"- Live Codex terminals: `{report['counts']['live_codex_processes']}`")
    lines.append("")

    if report["ack_state"]["acked_threads"] or report["ack_state"]["acked_topics"]:
        lines.append("## Active Acks")
        lines.append("")
        if report["ack_state"]["acked_topics"]:
            lines.append(f"- Acked topics: `{', '.join(report['ack_state']['acked_topics'])}`")
        if report["ack_state"]["acked_threads"]:
            lines.append(f"- Acked threads: `{len(report['ack_state']['acked_threads'])}`")
        lines.append("")

    lines.append("## Trend Signals")
    lines.append("")
    lines.append(f"- Delta points considered: `{report['trend']['history_points_considered']}`")
    if report["trend"]["persistent_blocker_topics"]:
        lines.append(
            f"- Persistent blocker topics: `{', '.join(report['trend']['persistent_blocker_topics'])}`"
        )
    else:
        lines.append("- Persistent blocker topics: `(none)`")
    lines.append("")

    lines.append("## Priority Workstreams")
    lines.append("")
    if not report["workstreams"]:
        lines.append("- No workstreams detected.")
    else:
        for stream in report["workstreams"][:8]:
            lines.append(
                f"- `{stream['topic']}` | priority `{stream['priority_score']}` | "
                f"threads `{stream['thread_count']}` | blocked `{stream['blocked_signals']}` | "
                f"latest `{to_iso(int(stream['latest_updated_at']))}`"
            )
            lines.append(f"  Latest title: {safe_text(str(stream['latest_title']), 180)}")
            if stream.get("check_hints"):
                lines.append(f"  Check hints: `{', '.join(stream['check_hints'][:6])}`")
            repos = stream.get("repos") or []
            for repo in repos[:2]:
                dirty = "dirty" if repo["dirty"] else "clean"
                lines.append(
                    f"  Repo: `{repo['root']}` [{repo['branch']}] {dirty} ahead={repo['ahead']} behind={repo['behind']}"
                )
    lines.append("")

    lines.append("## Finish Next (Why)")
    lines.append("")
    if not report["recommendations"]:
        lines.append("- No active recommendations.")
    else:
        for idx, rec in enumerate(report["recommendations"], start=1):
            lines.append(
                f"{idx}. `{rec['topic']}` | priority `{rec['priority']}` | {rec['why_now']}"
            )
            lines.append(f"   Thread: `{rec['thread_id']}`")
            lines.append(f"   Title: {rec['title']}")
            if rec.get("repo_root"):
                lines.append(f"   Repo: `{rec['repo_root']}`")
            if rec.get("failing_checks"):
                lines.append(f"   Failing checks: `{', '.join(rec['failing_checks'])}`")
            if rec["last_user_text"]:
                lines.append(f"   Last user signal: {rec['last_user_text']}")
    lines.append("")

    lines.append("## Archive Suggestions")
    lines.append("")
    suggestions = report.get("archive_suggestions") or []
    if not suggestions:
        lines.append("- No archive suggestions right now.")
    else:
        for idx, item in enumerate(suggestions, start=1):
            lines.append(
                f"{idx}. `{item['topic']}` | score `{item['archive_score']}` | {item['reason']}"
            )
            lines.append(f"   Thread: `{item['thread_id']}`")
            lines.append(f"   Age: `{item['age_hours']}h`")
            lines.append(f"   Title: {item['title']}")
            lines.append(f"   Ack cmd: `{item['suggested_command']}`")
    lines.append("")

    lines.append("## Live Terminal Chats")
    lines.append("")
    if not report["live_processes"]:
        lines.append("- No active Codex terminal processes detected.")
    else:
        for row in report["live_processes"][:12]:
            minutes = int(row["elapsed_seconds"]) // 60
            lines.append(
                f"- `pid={row['pid']}` `tty={row['tty']}` running `{minutes}m` | {safe_text(str(row['args']), 150)}"
            )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Analyze open/saved Codex terminal chats and recommend what to finish next."
    )
    ap.add_argument(
        "--state-db",
        type=Path,
        default=DEFAULT_STATE_DB,
        help=f"Path to Codex state sqlite DB (default: {DEFAULT_STATE_DB})",
    )
    ap.add_argument(
        "--history",
        type=Path,
        default=DEFAULT_HISTORY,
        help=f"Path to Codex history jsonl (default: {DEFAULT_HISTORY})",
    )
    ap.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived threads in analysis input.",
    )
    ap.add_argument(
        "--profile",
        choices=sorted(PROFILE_WEIGHTS.keys()),
        default="balanced",
        help="Priority scoring profile.",
    )
    ap.add_argument("--top", type=int, default=12, help="Number of top threads to include.")
    ap.add_argument(
        "--json-out",
        type=Path,
        default=DEFAULT_JSON_OUT,
        help=f"Write JSON report to path (default: {DEFAULT_JSON_OUT})",
    )
    ap.add_argument(
        "--md-out",
        type=Path,
        default=DEFAULT_MARKDOWN_OUT,
        help=f"Write markdown brief to path (default: {DEFAULT_MARKDOWN_OUT})",
    )
    ap.add_argument(
        "--codex-prompt-out",
        type=Path,
        default=DEFAULT_CODEX_PROMPT_OUT,
        help=f"Write a ready-to-paste Codex handoff prompt (default: {DEFAULT_CODEX_PROMPT_OUT})",
    )
    ap.add_argument(
        "--delta-log",
        type=Path,
        default=DEFAULT_DELTA_LOG,
        help=f"Append trend delta snapshots to JSONL path (default: {DEFAULT_DELTA_LOG})",
    )
    ap.add_argument(
        "--trend-lookback",
        type=int,
        default=8,
        help="Number of prior snapshots to inspect for persistent blocker trends.",
    )
    ap.add_argument(
        "--append-delta",
        dest="append_delta",
        action="store_true",
        default=True,
        help="Append current run to delta log (default: on).",
    )
    ap.add_argument(
        "--no-append-delta",
        dest="append_delta",
        action="store_false",
        help="Skip appending delta snapshot for this run.",
    )
    ap.add_argument(
        "--ack-state",
        type=Path,
        default=DEFAULT_ACK_STATE,
        help=f"Ack/suppression state file path (default: {DEFAULT_ACK_STATE})",
    )
    ap.add_argument(
        "--archive-suggest-max",
        type=int,
        default=8,
        help="Maximum number of archive suggestions.",
    )
    ap.add_argument(
        "--apply-archive-suggestions",
        action="store_true",
        help="Auto-ack suggested archive thread IDs after generating suggestions.",
    )
    ap.add_argument("--ack-thread", action="append", default=[], help="Suppress a thread ID from future recommendations.")
    ap.add_argument("--ack-topic", action="append", default=[], help="Suppress a topic from future recommendations.")
    ap.add_argument("--unack-thread", action="append", default=[], help="Remove suppression for a thread ID.")
    ap.add_argument("--unack-topic", action="append", default=[], help="Remove suppression for a topic.")
    ap.add_argument("--clear-acks", action="store_true", help="Clear all thread/topic suppressions.")
    ap.add_argument("--print-json", action="store_true", help="Also print JSON report to stdout.")
    return ap.parse_args(argv)


def build_codex_prompt(report_path_md: Path, report_path_json: Path) -> str:
    return (
        "Use this chat-work synthesis to produce an execution briefing.\n\n"
        f"Inputs:\n- Markdown brief: {report_path_md}\n- JSON report: {report_path_json}\n\n"
        "Required output:\n"
        "1) Top 5 finish-next actions, each with risk/impact rationale\n"
        "2) What to defer/archive and why\n"
        "3) A one-session execution sequence (ordered commands/prompts)\n"
        "4) Dependencies/blockers that need user intervention\n"
        "5) Confidence level per recommendation (high/medium/low)\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    prior_deltas = load_delta_history(args.delta_log, args.trend_lookback)
    persistent_topics = persistent_blocker_topics(prior_deltas)

    ack_state = load_ack_state(args.ack_state)
    updated_ack_state = apply_ack_updates(args, ack_state)
    if updated_ack_state != ack_state:
        write_ack_state(args.ack_state, updated_ack_state)
    ack_state = updated_ack_state

    history = load_history(args.history)

    # First pass: compute current blocked topics under the active ack filter.
    pre_threads = load_threads(
        args.state_db,
        history,
        include_archived=args.include_archived,
        persistent_topics=set(),
        profile=args.profile,
    )
    active_blocked_topics = currently_blocked_topics(pre_threads, ack_state)
    effective_persistent_topics = persistent_topics & active_blocked_topics

    # Second pass: apply trend bonus only for currently blocked active topics.
    threads = load_threads(
        args.state_db,
        history,
        include_archived=args.include_archived,
        persistent_topics=effective_persistent_topics,
        profile=args.profile,
    )
    repo_states = collect_repo_states(threads)
    live = load_live_codex_processes()
    report = build_report(
        threads=threads,
        live_processes=live,
        top_n=max(1, args.top),
        ack_state=ack_state,
        persistent_topics=effective_persistent_topics,
        repo_states=repo_states,
        delta_rows=prior_deltas,
        archive_suggest_max=args.archive_suggest_max,
        profile=args.profile,
    )

    if args.apply_archive_suggestions:
        suggested_ids = {
            str(item.get("thread_id"))
            for item in (report.get("archive_suggestions") or [])
            if item.get("thread_id")
        }
        if suggested_ids:
            ack_threads = set(ack_state.get("acked_threads") or [])
            if not suggested_ids.issubset(ack_threads):
                ack_threads.update(suggested_ids)
                ack_state = {
                    "acked_threads": sorted(ack_threads),
                    "acked_topics": sorted(set(ack_state.get("acked_topics") or [])),
                    "updated_at": ack_state.get("updated_at"),
                }
                write_ack_state(args.ack_state, ack_state)
                report = build_report(
                    threads=threads,
                    live_processes=live,
                    top_n=max(1, args.top),
                    ack_state=ack_state,
                    persistent_topics=effective_persistent_topics,
                    repo_states=repo_states,
                    delta_rows=prior_deltas,
                    archive_suggest_max=args.archive_suggest_max,
                    profile=args.profile,
                )

    markdown = to_markdown(report)

    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.write_text(markdown, encoding="utf-8")

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    args.codex_prompt_out.parent.mkdir(parents=True, exist_ok=True)
    args.codex_prompt_out.write_text(
        build_codex_prompt(args.md_out, args.json_out),
        encoding="utf-8",
    )

    if args.append_delta:
        append_delta_snapshot(args.delta_log, report)

    print(markdown)
    print(f"[chat-work-agent] markdown={args.md_out}")
    print(f"[chat-work-agent] json={args.json_out}")
    print(f"[chat-work-agent] codex_prompt={args.codex_prompt_out}")
    print(f"[chat-work-agent] delta_log={args.delta_log}")
    print(f"[chat-work-agent] ack_state={args.ack_state}")
    if args.print_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
