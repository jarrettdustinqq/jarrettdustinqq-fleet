#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


EXCLUDED_HOSTS = {
    "github.com",
    "gitlab.com",
    "bitbucket.org",
}


@dataclass
class Candidate:
    user: str | None
    host: str
    score: int = 0
    sources: set[str] = field(default_factory=set)

    @property
    def target(self) -> str:
        if self.user:
            return f"{self.user}@{self.host}"
        return self.host


def add_candidate(
    store: dict[tuple[str | None, str], Candidate],
    user: str | None,
    host: str,
    score: int,
    source: str,
) -> None:
    host = host.strip()
    if not host or host in {"*", "localhost"}:
        return
    if any(ch in host for ch in "*?/"):
        return
    if host.startswith("[") and "]" in host:
        host = host[1 : host.index("]")]
    host = host.rstrip(":")
    if not host:
        return

    key = (user, host)
    if key not in store:
        store[key] = Candidate(user=user, host=host)
    store[key].score += score
    store[key].sources.add(source)


def parse_ssh_target(token: str) -> tuple[str | None, str] | None:
    token = token.strip()
    if not token or "/" in token:
        return None
    if token.startswith("-"):
        return None
    if "@" in token:
        user, host = token.rsplit("@", 1)
        if user and host:
            return user, host
        return None
    return None, token


def scan_ssh_config(path: Path, store: dict[tuple[str | None, str], Candidate]) -> None:
    if not path.exists():
        return
    current_hosts: list[str] = []
    current_user: str | None = None
    current_hostname: str | None = None

    def flush() -> None:
        nonlocal current_hosts, current_user, current_hostname
        if not current_hosts:
            return
        for alias in current_hosts:
            host = current_hostname or alias
            add_candidate(store, current_user, host, 6, f"ssh-config:{alias}")
        current_hosts = []
        current_user = None
        current_hostname = None

    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        key = parts[0].lower()
        vals = parts[1:]
        if key == "host":
            flush()
            current_hosts = [v for v in vals if "*" not in v and "?" not in v]
        elif key == "hostname" and vals:
            current_hostname = vals[0]
        elif key == "user" and vals:
            current_user = vals[0]
    flush()


def scan_bash_history(path: Path, store: dict[tuple[str | None, str], Candidate]) -> None:
    if not path.exists():
        return
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or "ssh" not in line:
            continue
        try:
            toks = shlex.split(line)
        except ValueError:
            continue
        if not toks:
            continue
        cmd = os.path.basename(toks[0])
        if cmd not in {"ssh", "ssh-copy-id"}:
            continue

        remote = None
        i = 1
        while i < len(toks):
            t = toks[i]
            if t.startswith("-"):
                if t in {"-i", "-p", "-o", "-F", "-J", "-b", "-c", "-D", "-E", "-I", "-L", "-l", "-m", "-Q", "-R", "-S", "-W", "-w"}:
                    i += 2
                else:
                    i += 1
                continue
            remote = t
            break
        if not remote:
            continue
        parsed = parse_ssh_target(remote)
        if not parsed:
            continue
        user, host = parsed
        add_candidate(store, user, host, 5, "bash-history")


def scan_known_hosts(path: Path, store: dict[tuple[str | None, str], Candidate]) -> None:
    if not path.exists():
        return
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("|"):
            continue
        first = line.split(" ", 1)[0]
        for part in first.split(","):
            if not part:
                continue
            host = part
            if host.startswith("[") and "]:" in host:
                host = host[1 : host.index("]")]
            add_candidate(store, None, host, 2, "known-hosts")


def extract_host_user_from_remote(url: str) -> tuple[str | None, str] | None:
    m = re.match(r"^(?P<user>[^@:/]+)@(?P<host>[^:]+):", url)
    if m:
        return m.group("user"), m.group("host")
    m = re.match(r"^ssh://(?:(?P<user>[^@/]+)@)?(?P<host>[^/:]+)", url)
    if m:
        return m.group("user"), m.group("host")
    m = re.match(r"^https?://(?P<host>[^/]+)", url)
    if m:
        return None, m.group("host")
    return None


def scan_git_remotes(projects_dir: Path, store: dict[tuple[str | None, str], Candidate]) -> None:
    if not projects_dir.exists():
        return
    for repo in projects_dir.iterdir():
        if not (repo / ".git").exists():
            continue
        proc = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            continue
        url = proc.stdout.strip()
        parsed = extract_host_user_from_remote(url)
        if not parsed:
            continue
        user, host = parsed
        if host in EXCLUDED_HOSTS:
            continue
        add_candidate(store, user, host, 2, f"git-remote:{repo.name}")


def sort_candidates(store: dict[tuple[str | None, str], Candidate]) -> list[Candidate]:
    out = list(store.values())
    # Exclude obvious public git hosts; these are not VPS targets.
    out = [c for c in out if c.host not in EXCLUDED_HOSTS]
    return sorted(out, key=lambda c: (-c.score, c.host, c.user or ""))


def pick_best(cands: list[Candidate], default_user: str | None) -> str | None:
    for c in cands:
        if c.user and c.host not in EXCLUDED_HOSTS and c.score >= 5:
            return c.target
    if default_user:
        for c in cands:
            if c.host not in EXCLUDED_HOSTS and c.score >= 6:
                return f"{default_user}@{c.host}"
    return None


def print_table(cands: list[Candidate]) -> None:
    if not cands:
        print("No VPS candidates found.")
        return
    print("Rank | Candidate               | Score | Sources")
    print("-----+-------------------------+-------+-------------------------")
    for i, c in enumerate(cands, 1):
        src = ",".join(sorted(c.sources))
        print(f"{i:>4} | {c.target:<23} | {c.score:>5} | {src}")


def print_tsv(
    cands: list[Candidate], limit: int | None = None, default_user: str | None = None
) -> None:
    rows = cands[:limit] if limit and limit > 0 else cands
    for c in rows:
        target = c.target if c.user or not default_user else f"{default_user}@{c.host}"
        src = ",".join(sorted(c.sources))
        print(f"{target}\t{c.score}\t{src}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover likely VPS SSH targets")
    parser.add_argument("--best", action="store_true", help="Print best candidate only")
    parser.add_argument("--default-user", help="Fallback user for host-only candidates")
    parser.add_argument("--tsv", action="store_true", help="Print candidates as TSV (target,score,sources)")
    parser.add_argument("--limit", type=int, default=0, help="Limit rows for --tsv output")
    args = parser.parse_args()

    home = Path.home()
    store: dict[tuple[str | None, str], Candidate] = {}
    scan_ssh_config(home / ".ssh" / "config", store)
    scan_bash_history(home / ".bash_history", store)
    scan_known_hosts(home / ".ssh" / "known_hosts", store)
    scan_git_remotes(home / "projects", store)

    cands = sort_candidates(store)
    if args.best:
        best = pick_best(cands, args.default_user)
        if not best:
            return 1
        print(best)
        return 0

    if args.tsv:
        print_tsv(
            cands,
            args.limit if args.limit > 0 else None,
            args.default_user,
        )
        return 0

    print_table(cands)
    best = pick_best(cands, args.default_user)
    if best:
        print(f"\nSuggested target: {best}")
    else:
        print("\nNo high-confidence target yet. Provide --default-user or pass --vps manually.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
