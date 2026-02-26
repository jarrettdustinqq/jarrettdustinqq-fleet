#!/usr/bin/env python3
"""Analyze local venture codebases and emit autonomy optimization actions."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_ROOTS = [
    Path.home(),
]
DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "fleet-control-hub"
DEFAULT_MARKDOWN_OUT = DEFAULT_STATE_DIR / "venture_autonomy_brief.md"
DEFAULT_JSON_OUT = DEFAULT_STATE_DIR / "venture_autonomy_report.json"
DEFAULT_CODEX_PROMPT_OUT = DEFAULT_STATE_DIR / "venture_autonomy_codex_prompt.txt"

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".nox",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "target",
    ".direnv",
}

LANGUAGE_BY_EXT = {
    ".py": "python",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".go": "go",
    ".rs": "rust",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".md": "markdown",
    ".nix": "nix",
}

INLINE_CODE_RE = re.compile(r"`([^`\n]{3,220})`")
FENCED_BLOCK_RE = re.compile(r"```(?:bash|sh|zsh)?\n(.*?)```", re.DOTALL | re.IGNORECASE)
MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:")

SAFE_EXACT = {
    "pytest",
    "go test ./...",
    "cargo test",
    "npm test",
    "pnpm test",
    "yarn test",
    "nix flake check",
}
SAFE_PREFIXES = (
    "make ",
    "pytest ",
    "python3 -m py_compile",
    "bash -n",
    "shellcheck ",
    "go test ",
    "cargo test ",
    "npm run test",
    "npm test ",
    "pnpm test ",
    "yarn test ",
    "./fleetctl health",
    "./fleetctl hub-scan",
)

PREFERRED_MAKE_TARGETS = ("verify", "health", "check", "test", "lint", "ci")

MANIFEST_NAMES = (
    "pyproject.toml",
    "requirements.txt",
    "Pipfile",
    "poetry.lock",
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "go.mod",
    "Cargo.toml",
    "flake.nix",
    "Makefile",
)


@dataclasses.dataclass
class CheckResult:
    command: str
    status: str
    exit_code: int | None
    duration_ms: int
    preview: str


@dataclasses.dataclass
class RepoReport:
    root: str
    name: str
    branch: str
    dirty: bool
    last_commit_age_hours: float | None
    language_counts: dict[str, int]
    signals: dict[str, bool]
    command_candidates: list[str]
    safe_checks: list[str]
    check_results: list[CheckResult]
    score: int
    gaps: list[str]


@dataclasses.dataclass
class ActionItem:
    repo: str
    title: str
    why: str
    impact: int
    command: str | None


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def compact(text: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", text.strip())
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def repo_last_commit_age_hours(repo: Path) -> float | None:
    raw = run_git(repo, "log", "-1", "--format=%ct")
    if not raw:
        return None
    try:
        ts = int(raw)
    except ValueError:
        return None
    delta = time.time() - ts
    return round(delta / 3600.0, 2)


def discover_git_repos(roots: list[Path], max_depth: int) -> list[Path]:
    repos: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        if (root / ".git").exists():
            repos.add(root.resolve())
            continue
        for dirpath, dirnames, _ in os.walk(root):
            current = Path(dirpath)
            rel_depth = len(current.relative_to(root).parts)
            if rel_depth > max_depth:
                dirnames[:] = []
                continue
            if ".git" in dirnames or (current / ".git").exists():
                repos.add(current.resolve())
                dirnames[:] = []
                continue
            dirnames[:] = [
                d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
            ]
    return sorted(repos)


def count_languages(repo: Path, file_limit: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            scanned += 1
            if scanned > file_limit:
                return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
            ext = Path(name).suffix.lower()
            lang = LANGUAGE_BY_EXT.get(ext)
            if not lang:
                if name == "Makefile":
                    lang = "make"
                else:
                    continue
            counts[lang] = counts.get(lang, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def looks_like_command(value: str) -> bool:
    cmd = value.strip()
    if not cmd or "\n" in cmd:
        return False
    if cmd.startswith(("./", "../")):
        return True
    if cmd in SAFE_EXACT:
        return True
    return cmd.startswith(SAFE_PREFIXES)


def is_safe_check_command(value: str) -> bool:
    cmd = value.strip()
    if cmd in SAFE_EXACT:
        return True
    return cmd.startswith(SAFE_PREFIXES)


def unique_keep_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = re.sub(r"\s+", " ", value.strip())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def read_text(path: Path, max_bytes: int = 220_000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        data = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    return data[:max_bytes]


def extract_commands_from_text(text: str) -> list[str]:
    out: list[str] = []
    for match in INLINE_CODE_RE.findall(text):
        cmd = match.strip()
        if looks_like_command(cmd):
            out.append(cmd)
    for block in FENCED_BLOCK_RE.findall(text):
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if looks_like_command(line):
                out.append(line)
    return unique_keep_order(out)


def parse_make_targets(repo: Path) -> list[str]:
    makefile = repo / "Makefile"
    if not makefile.exists():
        return []
    targets: set[str] = set()
    for raw in read_text(makefile).splitlines():
        m = MAKE_TARGET_RE.match(raw)
        if not m:
            continue
        target = m.group(1)
        if "." in target:
            continue
        targets.add(target)
    chosen: list[str] = []
    for target in PREFERRED_MAKE_TARGETS:
        if target in targets:
            chosen.append(f"make {target}")
    return chosen


def discover_command_candidates(repo: Path) -> list[str]:
    candidates: list[str] = []
    docs_to_scan = [
        repo / "AGENTS.md",
        repo / "README.md",
    ]
    docs_dir = repo / "docs"
    if docs_dir.exists():
        docs_to_scan.extend(sorted(docs_dir.glob("*.md"))[:8])

    for path in docs_to_scan:
        text = read_text(path)
        if not text:
            continue
        candidates.extend(extract_commands_from_text(text))

    candidates.extend(parse_make_targets(repo))

    if (repo / "pyproject.toml").exists() or (repo / "requirements.txt").exists():
        candidates.append("python3 -m py_compile $(rg --files -g '*.py' | tr '\\n' ' ')")
    if (repo / "package.json").exists():
        candidates.append("npm test")
    if (repo / "go.mod").exists():
        candidates.append("go test ./...")
    if (repo / "Cargo.toml").exists():
        candidates.append("cargo test")
    if (repo / "flake.nix").exists():
        candidates.append("nix flake check")

    return unique_keep_order(candidates)


def run_safe_checks(
    repo: Path,
    checks: list[str],
    max_checks: int,
    timeout_sec: int,
) -> list[CheckResult]:
    out: list[CheckResult] = []
    for command in checks[:max_checks]:
        started = time.time()
        try:
            proc = subprocess.run(
                ["bash", "-lc", command],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_sec,
            )
            preview = compact(proc.stdout + "\n" + proc.stderr, 260)
            status = "pass" if proc.returncode == 0 else "fail"
            out.append(
                CheckResult(
                    command=command,
                    status=status,
                    exit_code=proc.returncode,
                    duration_ms=int((time.time() - started) * 1000),
                    preview=preview,
                )
            )
        except subprocess.TimeoutExpired:
            out.append(
                CheckResult(
                    command=command,
                    status="timeout",
                    exit_code=None,
                    duration_ms=int((time.time() - started) * 1000),
                    preview=f"timed out after {timeout_sec}s",
                )
            )
    return out


def build_signals(repo: Path, commands: list[str], checks: list[str]) -> dict[str, bool]:
    has_tests = any((repo / d).exists() for d in ("tests", "test", "spec"))
    if not has_tests:
        has_tests = any("test" in cmd for cmd in commands)
    has_ci = (repo / ".github" / "workflows").exists()
    has_docs_dir = (repo / "docs").exists()
    has_agents_md = (repo / "AGENTS.md").exists()
    has_readme = (repo / "README.md").exists()
    has_manifest = any((repo / name).exists() for name in MANIFEST_NAMES)
    has_safe_checks = bool(checks)
    return {
        "has_tests": has_tests,
        "has_ci": has_ci,
        "has_docs_dir": has_docs_dir,
        "has_agents_md": has_agents_md,
        "has_readme": has_readme,
        "has_manifest": has_manifest,
        "has_safe_checks": has_safe_checks,
    }


def compute_score(
    signals: dict[str, bool],
    dirty: bool,
    last_commit_age_hours: float | None,
    safe_check_count: int,
) -> tuple[int, list[str]]:
    score = 0
    gaps: list[str] = []

    if signals["has_ci"]:
        score += 25
    else:
        gaps.append("Missing CI workflow")

    if signals["has_tests"]:
        score += 20
    else:
        gaps.append("No obvious test coverage")

    if signals["has_agents_md"]:
        score += 10
    else:
        gaps.append("No AGENTS.md execution guidance")

    if signals["has_readme"]:
        score += 8
    else:
        gaps.append("No README.md usage contract")

    if signals["has_docs_dir"]:
        score += 7
    else:
        gaps.append("No docs directory for operations")

    if signals["has_manifest"]:
        score += 10
    else:
        gaps.append("No language/runtime manifest found")

    if safe_check_count >= 2:
        score += 15
    elif safe_check_count == 1:
        score += 8
    else:
        gaps.append("No safe validation commands discovered")

    if not dirty:
        score += 3
    else:
        gaps.append("Working tree is dirty")

    if last_commit_age_hours is not None:
        if last_commit_age_hours < 24 * 7:
            score += 5
        elif last_commit_age_hours < 24 * 30:
            score += 2
        else:
            gaps.append("Repository appears stale (>30 days since last commit)")

    return min(score, 100), gaps


def build_actions(report: RepoReport) -> list[ActionItem]:
    repo = report.root
    actions: list[ActionItem] = []

    if not report.signals["has_ci"]:
        actions.append(
            ActionItem(
                repo=repo,
                title="Add CI quality gate",
                why="Automated checks are required for reliable autonomous execution.",
                impact=96,
                command=f"cd {shlex.quote(repo)} && ls -la .github/workflows",
            )
        )
    if not report.signals["has_tests"]:
        actions.append(
            ActionItem(
                repo=repo,
                title="Add/expand smoke tests",
                why="Agentic changes need deterministic regression detection.",
                impact=92,
                command=f"cd {shlex.quote(repo)} && ls -la",
            )
        )
    if not report.signals["has_safe_checks"]:
        actions.append(
            ActionItem(
                repo=repo,
                title="Define one-command verification",
                why="Autonomous workflows need a clear machine-runnable pass/fail command.",
                impact=88,
                command=f"cd {shlex.quote(repo)} && printf 'Add make verify or equivalent\\n'",
            )
        )
    for check in report.check_results:
        if check.status in {"fail", "timeout"}:
            actions.append(
                ActionItem(
                    repo=repo,
                    title=f"Fix failing check: {check.command}",
                    why=f"Current status is {check.status}; unblock automation before autonomous edits.",
                    impact=85,
                    command=f"cd {shlex.quote(repo)} && {check.command}",
                )
            )
    if not report.signals["has_agents_md"]:
        actions.append(
            ActionItem(
                repo=repo,
                title="Create AGENTS.md runbook",
                why="Consistent local instructions reduce agent ambiguity and execution errors.",
                impact=72,
                command=f"cd {shlex.quote(repo)} && ls -la",
            )
        )
    if report.dirty:
        actions.append(
            ActionItem(
                repo=repo,
                title="Stabilize working tree",
                why="Pending local diffs reduce confidence in autonomous decision-making.",
                impact=55,
                command=f"cd {shlex.quote(repo)} && git status --short",
            )
        )
    if report.last_commit_age_hours is not None and report.last_commit_age_hours > 24 * 30:
        actions.append(
            ActionItem(
                repo=repo,
                title="Re-baseline stale repository",
                why="Long inactivity often means outdated automation and broken assumptions.",
                impact=44,
                command=f"cd {shlex.quote(repo)} && git log -1 --date=iso --format='%cd %h %s'",
            )
        )
    return actions


def build_repo_report(
    repo: Path,
    run_checks: bool,
    max_checks_per_repo: int,
    check_timeout_sec: int,
) -> RepoReport:
    branch = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    dirty = bool(run_git(repo, "status", "--porcelain"))
    age_hours = repo_last_commit_age_hours(repo)
    language_counts = count_languages(repo, file_limit=8_000)
    command_candidates = discover_command_candidates(repo)
    safe_checks = [cmd for cmd in command_candidates if is_safe_check_command(cmd)]
    check_results = (
        run_safe_checks(repo, safe_checks, max_checks_per_repo, check_timeout_sec)
        if run_checks and safe_checks
        else []
    )
    signals = build_signals(repo, command_candidates, safe_checks)
    score, gaps = compute_score(signals, dirty, age_hours, len(safe_checks))
    if run_checks and not check_results:
        gaps.append("No checks executed")

    return RepoReport(
        root=str(repo),
        name=repo.name,
        branch=branch,
        dirty=dirty,
        last_commit_age_hours=age_hours,
        language_counts=language_counts,
        signals=signals,
        command_candidates=command_candidates[:12],
        safe_checks=safe_checks[:8],
        check_results=check_results,
        score=score,
        gaps=gaps,
    )


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def render_markdown(
    generated_at: str,
    roots: list[str],
    repos: list[RepoReport],
    actions: list[ActionItem],
    top: int,
    run_checks: bool,
) -> str:
    total = len(repos)
    with_ci = sum(1 for r in repos if r.signals["has_ci"])
    with_tests = sum(1 for r in repos if r.signals["has_tests"])
    with_safe_checks = sum(1 for r in repos if r.signals["has_safe_checks"])
    avg_score = round(sum(r.score for r in repos) / total, 1) if repos else 0.0

    lines = [
        "# Venture Autonomy Agent Brief",
        "",
        f"- Generated: `{generated_at}`",
        f"- Roots scanned: `{', '.join(roots)}`",
        f"- Repositories discovered: `{total}`",
        f"- Average autonomy score: `{avg_score}`",
        f"- Repos with CI: `{with_ci}/{total}`",
        f"- Repos with tests: `{with_tests}/{total}`",
        f"- Repos with safe checks: `{with_safe_checks}/{total}`",
        f"- Safe checks executed: `{'yes' if run_checks else 'no'}`",
        "",
        "## Top Optimization Actions",
    ]

    if not actions:
        lines.extend(["", "No blocking autonomy gaps detected.", ""])
    else:
        for idx, action in enumerate(actions[:top], 1):
            lines.append(
                f"{idx}. `{action.title}` ({action.impact}/100) - `{action.repo}`"
            )
            lines.append(f"   Why: {action.why}")
            if action.command:
                lines.append(f"   Suggested command: `{action.command}`")

    lines.extend(["", "## Repository Snapshots", ""])
    for repo in sorted(repos, key=lambda r: (r.score, r.name)):
        lines.append(f"### {repo.name}")
        lines.append(f"- Path: `{repo.root}`")
        lines.append(f"- Score: `{repo.score}`")
        lines.append(f"- Branch: `{repo.branch}`")
        lines.append(f"- Dirty: `{'yes' if repo.dirty else 'no'}`")
        if repo.last_commit_age_hours is None:
            lines.append("- Last commit age: `unknown`")
        else:
            lines.append(f"- Last commit age: `{repo.last_commit_age_hours}h`")
        top_langs = ", ".join(
            f"{lang}:{count}" for lang, count in list(repo.language_counts.items())[:4]
        )
        lines.append(f"- Languages: `{top_langs or 'unknown'}`")
        if repo.safe_checks:
            lines.append(f"- Safe checks: `{'; '.join(repo.safe_checks[:4])}`")
        if repo.gaps:
            lines.append(f"- Gaps: `{' | '.join(repo.gaps[:5])}`")
        if repo.check_results:
            summaries = "; ".join(
                f"{res.status}:{res.command}" for res in repo.check_results
            )
            lines.append(f"- Check results: `{summaries}`")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_codex_prompt(generated_at: str, actions: list[ActionItem], top: int) -> str:
    lines = [
        "You are the Venture Autonomy execution agent.",
        f"Generated at: {generated_at}",
        "",
        "Execute the following tasks in order, keeping each task scoped to one repository.",
        "For every task: implement fix, run repo checks, summarize results, then move to next task.",
        "",
        "Priority Task Queue:",
    ]
    if not actions:
        lines.append("1. No critical gaps detected. Run maintenance checks on active repos.")
    else:
        for idx, action in enumerate(actions[:top], 1):
            lines.append(f"{idx}. Repo: {action.repo}")
            lines.append(f"   Goal: {action.title}")
            lines.append(f"   Reason: {action.why}")
            if action.command:
                lines.append(f"   Start command: {action.command}")
    return "\n".join(lines).strip() + "\n"


def to_payload(
    generated_at: str,
    roots: list[str],
    repos: list[RepoReport],
    actions: list[ActionItem],
    run_checks: bool,
    top: int,
) -> dict[str, Any]:
    return {
        "generated_at": generated_at,
        "roots": roots,
        "run_checks": run_checks,
        "top": top,
        "summary": {
            "repos": len(repos),
            "repos_with_ci": sum(1 for r in repos if r.signals["has_ci"]),
            "repos_with_tests": sum(1 for r in repos if r.signals["has_tests"]),
            "repos_with_safe_checks": sum(
                1 for r in repos if r.signals["has_safe_checks"]
            ),
            "avg_score": round(
                sum(r.score for r in repos) / len(repos), 2
            )
            if repos
            else 0,
        },
        "repos": [
            {
                **dataclasses.asdict(repo),
                "check_results": [dataclasses.asdict(res) for res in repo.check_results],
            }
            for repo in repos
        ],
        "actions": [dataclasses.asdict(action) for action in actions],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze code repos and emit autonomy optimization actions"
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Scan root (repeat for multiple). Default: ~ (your Linux home)",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=4,
        help="Maximum directory depth while searching for git repositories",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=12,
        help="Top action count for markdown + codex outputs",
    )
    parser.add_argument(
        "--run-checks",
        action="store_true",
        help="Execute safe verification commands per repository",
    )
    parser.add_argument(
        "--max-checks-per-repo",
        type=int,
        default=2,
        help="Maximum safe checks to execute per repo when --run-checks is enabled",
    )
    parser.add_argument(
        "--check-timeout-sec",
        type=int,
        default=180,
        help="Timeout per safe check command",
    )
    parser.add_argument(
        "--md-out",
        default=str(DEFAULT_MARKDOWN_OUT),
        help="Markdown report output path",
    )
    parser.add_argument(
        "--json-out",
        default=str(DEFAULT_JSON_OUT),
        help="JSON report output path",
    )
    parser.add_argument(
        "--codex-prompt-out",
        default=str(DEFAULT_CODEX_PROMPT_OUT),
        help="Codex prompt output path",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    roots = (
        [Path(path).expanduser() for path in args.root]
        if args.root
        else list(DEFAULT_ROOTS)
    )
    roots = [path for path in roots if path.exists()]
    if not roots:
        print("[venture-agent] no valid roots to scan")
        return 1

    repos = discover_git_repos(roots, max_depth=max(args.max_depth, 1))
    generated_at = utc_now_iso()
    reports = [
        build_repo_report(
            repo=repo,
            run_checks=args.run_checks,
            max_checks_per_repo=max(args.max_checks_per_repo, 1),
            check_timeout_sec=max(args.check_timeout_sec, 30),
        )
        for repo in repos
    ]
    actions = sorted(
        [action for report in reports for action in build_actions(report)],
        key=lambda item: (-item.impact, item.repo, item.title),
    )

    md_out = Path(args.md_out).expanduser()
    json_out = Path(args.json_out).expanduser()
    codex_prompt_out = Path(args.codex_prompt_out).expanduser()
    ensure_parent(md_out)
    ensure_parent(json_out)
    ensure_parent(codex_prompt_out)

    markdown = render_markdown(
        generated_at=generated_at,
        roots=[str(path) for path in roots],
        repos=reports,
        actions=actions,
        top=max(args.top, 1),
        run_checks=bool(args.run_checks),
    )
    payload = to_payload(
        generated_at=generated_at,
        roots=[str(path) for path in roots],
        repos=reports,
        actions=actions,
        run_checks=bool(args.run_checks),
        top=max(args.top, 1),
    )
    codex_prompt = render_codex_prompt(generated_at, actions, max(args.top, 1))

    md_out.write_text(markdown, encoding="utf-8")
    json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    codex_prompt_out.write_text(codex_prompt, encoding="utf-8")

    print(f"[venture-agent] scanned_roots={len(roots)}")
    print(f"[venture-agent] repos={len(reports)}")
    print(f"[venture-agent] actions={len(actions)}")
    print(f"[venture-agent] markdown={md_out}")
    print(f"[venture-agent] json={json_out}")
    print(f"[venture-agent] codex_prompt={codex_prompt_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
