#!/usr/bin/env bash
set -euo pipefail

SCRIPT_VERSION="2.0.0"
EXPECTED_USER="${FLEET_EXPECTED_USER:-jarrettdustinqq}"
USER_HOME="${FLEET_USER_HOME:-$HOME}"
OUTPUT_ROOT="$USER_HOME"
FLEET_DIR="${FLEET_DIR:-$USER_HOME/agent_workspace/jarrettdustinqq-fleet}"
OUTER_WORKSPACE="${FLEET_OUTER_WORKSPACE:-$USER_HOME/agent_workspace}"
NO_FETCH=0
QUICK=0

usage() {
  cat <<'USAGE'
Usage: fleet_host_remediation.sh [options]

Create a reversible host evidence bundle and a checksummed backup of the outer
workspace Git metadata. The script does not move Git metadata, delete data,
change ownership, or modify systemd units.

Options:
  --output-root PATH       Parent directory for timestamped output.
  --fleet-dir PATH         Fleet checkout to validate.
  --outer-workspace PATH   Workspace whose .git directory is backed up.
  --no-fetch               Do not update Git remote-tracking references.
  --quick                  Skip broad host, service, state, and ownership probes.
  -h, --help               Show this help.
USAGE
}

while (($#)); do
  case "$1" in
    --output-root)
      OUTPUT_ROOT="$2"
      shift 2
      ;;
    --fleet-dir)
      FLEET_DIR="$2"
      shift 2
      ;;
    --outer-workspace)
      OUTER_WORKSPACE="$2"
      shift 2
      ;;
    --no-fetch)
      NO_FETCH=1
      shift
      ;;
    --quick)
      QUICK=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$OUTPUT_ROOT/fleet-system-remediation-$TS"
REPORT="$OUT/report.txt"
ARCHIVE_DIR="$OUT/backups"
STATE_DIR="$USER_HOME/.local/state/fleet-system-remediation"
SUMMARY_JSON="$OUT/summary.json"
REQUIRED_FAILURES=0
DIAGNOSTIC_WARNINGS=0

mkdir -p "$OUT" "$ARCHIVE_DIR" "$STATE_DIR"
chmod 700 "$OUT" "$ARCHIVE_DIR" "$STATE_DIR"
exec > >(tee -a "$REPORT") 2>&1

section() {
  printf '\n===== %s =====\n' "$1"
}

run_required() {
  printf '\n$'
  printf ' %q' "$@"
  printf '\n'
  if "$@"; then
    return 0
  else
    local rc=$?
    REQUIRED_FAILURES=$((REQUIRED_FAILURES + 1))
    printf 'COMMAND_FAILED required=yes rc=%s\n' "$rc"
    return 0
  fi
}

run_probe() {
  printf '\n$'
  printf ' %q' "$@"
  printf '\n'
  if "$@"; then
    return 0
  else
    local rc=$?
    DIAGNOSTIC_WARNINGS=$((DIAGNOSTIC_WARNINGS + 1))
    printf 'COMMAND_WARNING diagnostic=yes rc=%s\n' "$rc"
    return 0
  fi
}

run_probe_shell() {
  local command_text="$1"
  printf '\n$ %s\n' "$command_text"
  if bash -lc "$command_text"; then
    return 0
  else
    local rc=$?
    DIAGNOSTIC_WARNINGS=$((DIAGNOSTIC_WARNINGS + 1))
    printf 'COMMAND_WARNING diagnostic=yes rc=%s\n' "$rc"
    return 0
  fi
}

user_systemctl() {
  local uid runtime_dir bus_address
  uid="$(id -u)"
  runtime_dir="/run/user/$uid"
  bus_address="unix:path=$runtime_dir/bus"

  if systemctl --user "$@"; then
    printf 'USER_SYSTEMD_ROUTE=direct\n'
    return 0
  fi

  if [ -S "$runtime_dir/bus" ] && \
     env XDG_RUNTIME_DIR="$runtime_dir" DBUS_SESSION_BUS_ADDRESS="$bus_address" \
       systemctl --user "$@"; then
    printf 'USER_SYSTEMD_ROUTE=runtime-bus\n'
    return 0
  fi

  if systemctl --user --machine="$EXPECTED_USER@.host" "$@"; then
    printf 'USER_SYSTEMD_ROUTE=machine-bus\n'
    return 0
  fi

  DIAGNOSTIC_WARNINGS=$((DIAGNOSTIC_WARNINGS + 1))
  printf 'USER_SYSTEMD_UNAVAILABLE=YES\n'
  return 0
}

compile_existing_python() {
  local -a candidates=()
  local path
  for path in \
    "$FLEET_DIR/ops/control_hub_agent.py" \
    "$FLEET_DIR/ops/control_hub_safe_entry.py" \
    "$FLEET_DIR/ops/mission_control_agent.py"; do
    if [ -f "$path" ]; then
      candidates+=("$path")
    else
      printf 'PYTHON_ENTRYPOINT_ABSENT=%s\n' "$path"
    fi
  done

  if ((${#candidates[@]})); then
    run_required python3 -m py_compile "${candidates[@]}"
  else
    REQUIRED_FAILURES=$((REQUIRED_FAILURES + 1))
    printf 'COMMAND_FAILED required=yes reason=no-python-entrypoints\n'
  fi
}

section "Identity"
printf 'script_version=%s\n' "$SCRIPT_VERSION"
printf 'timestamp=%s\n' "$TS"
printf 'user=%s\n' "$(id -un)"
printf 'uid=%s\n' "$(id -u)"
printf 'home=%s\n' "$USER_HOME"
printf 'hostname=%s\n' "$(hostname)"
printf 'fleet_dir=%s\n' "$FLEET_DIR"
printf 'outer_workspace=%s\n' "$OUTER_WORKSPACE"
if [ "$(id -un)" != "$EXPECTED_USER" ] && [ "${FLEET_ALLOW_OTHER_USER:-0}" != "1" ]; then
  printf 'ERROR: run as %s or set FLEET_ALLOW_OTHER_USER=1 for an isolated test.\n' "$EXPECTED_USER" >&2
  exit 2
fi

if [ "$QUICK" -eq 0 ]; then
  section "Host baseline"
  run_probe uname -a
  run_probe_shell 'test -r /etc/os-release && cat /etc/os-release || true'
  run_probe df -hT
  run_probe free -h
  run_probe uptime
  run_probe ps -eo user,pid,ppid,%cpu,%mem,stat,comm,args --sort=-%cpu
  run_probe ss -ltnp
  run_probe systemctl --failed
  user_systemctl --failed
  user_systemctl list-timers --all
fi

section "Workspace topology"
for path in "$OUTER_WORKSPACE" "$USER_HOME/worktrees" "$USER_HOME/projects" "$USER_HOME/continuity"; do
  if [ -e "$path" ]; then
    printf 'PATH_PRESENT=%s\n' "$path"
    run_probe ls -ld "$path"
  else
    printf 'PATH_MISSING=%s\n' "$path"
  fi
done

section "Outer workspace Git backup"
OUTER_GIT="$OUTER_WORKSPACE/.git"
if [ -e "$OUTER_GIT" ]; then
  run_probe git -C "$OUTER_WORKSPACE" rev-parse --is-inside-work-tree
  run_probe git -C "$OUTER_WORKSPACE" status --short --branch
  run_probe git -C "$OUTER_WORKSPACE" remote -v
  run_probe git -C "$OUTER_WORKSPACE" show-ref
  run_probe git -C "$OUTER_WORKSPACE" stash list
  run_probe git -C "$OUTER_WORKSPACE" worktree list --porcelain
  run_probe git -C "$OUTER_WORKSPACE" submodule status

  ARCHIVE="$ARCHIVE_DIR/agent_workspace-dotgit-$TS.tar.gz"
  run_required tar -C "$OUTER_WORKSPACE" -czf "$ARCHIVE" .git
  if [ -f "$ARCHIVE" ]; then
    run_required sha256sum "$ARCHIVE"
    sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"
    run_required sha256sum -c "$ARCHIVE.sha256"
    run_required tar -tzf "$ARCHIVE"
    chmod 600 "$ARCHIVE" "$ARCHIVE.sha256"
    printf 'OUTER_GIT_BACKUP=%s\n' "$ARCHIVE"
  fi
else
  printf 'OUTER_GIT_NOT_FOUND=%s\n' "$OUTER_GIT"
fi

section "Git repository and worktree inventory"
python3 - "$OUTER_WORKSPACE" "$USER_HOME/worktrees" "$USER_HOME/projects" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

roots = [Path(arg) for arg in sys.argv[1:]]
skip = {".cache", "node_modules", ".venv", "venv", ".npm", ".cargo", ".rustup"}

def run(cmd, cwd=None):
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

seen = set()
rows = []
for base in roots:
    if not base.exists():
        continue
    for root, dirs, files in os.walk(base):
        dirs[:] = [name for name in dirs if name not in skip]
        root_path = Path(root)
        marker = None
        if ".git" in dirs:
            marker = root_path / ".git"
            dirs.remove(".git")
        elif ".git" in files:
            marker = root_path / ".git"
        if marker is None:
            continue
        rc, top, error = run(["git", "rev-parse", "--show-toplevel"], cwd=root_path)
        if rc != 0:
            print(f"INVALID_GIT_MARKER path={root_path} error={error}")
            continue
        top_path = Path(top).resolve()
        if top_path in seen:
            continue
        seen.add(top_path)
        _, git_dir, _ = run(["git", "rev-parse", "--git-dir"], cwd=top_path)
        _, common_dir, _ = run(["git", "rev-parse", "--git-common-dir"], cwd=top_path)
        git_dir_path = Path(git_dir)
        common_dir_path = Path(common_dir)
        if not git_dir_path.is_absolute():
            git_dir_path = (top_path / git_dir_path).resolve()
        if not common_dir_path.is_absolute():
            common_dir_path = (top_path / common_dir_path).resolve()
        git_dir = str(git_dir_path)
        common_dir = str(common_dir_path)
        _, branch, _ = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=top_path)
        _, remote, _ = run(["git", "remote", "get-url", "origin"], cwd=top_path)
        _, status, _ = run(["git", "status", "--porcelain"], cwd=top_path)
        rows.append((str(top_path), git_dir, common_dir, branch, remote, len(status.splitlines()) if status else 0))

print(f"VALID_WORKTREE_COUNT={len(rows)}")
groups = {}
for row in rows:
    groups.setdefault(row[2], []).append(row)
print(f"CANONICAL_REPOSITORY_COUNT={len(groups)}")
for common, members in sorted(groups.items()):
    print(f"CANONICAL common_git_dir={common} worktrees={len(members)}")
    for top, git_dir, _, branch, remote, dirty in members:
        print(f"  WORKTREE path={top} git_dir={git_dir} branch={branch} dirty={dirty} remote={remote}")
PY

if [ "$QUICK" -eq 0 ]; then
  section "Privileged operator inventory"
  for root in \
    "$OUTER_WORKSPACE" \
    "$USER_HOME/.config" \
    "$USER_HOME/.local" \
    "/etc/systemd/system" \
    "$USER_HOME/.config/systemd/user"; do
    [ -e "$root" ] || continue
    printf 'SEARCH_ROOT=%s\n' "$root"
    find "$root" -xdev \
      \( -iname '*system-operator*' -o -iname '*trusted-action*' -o -iname '*manifest*' -o -iname '*operator-agent*' \) \
      -print 2>/dev/null || true
  done

  section "Service definitions referencing operator or Fleet"
  run_probe systemctl list-unit-files
  user_systemctl list-unit-files
  run_probe_shell "systemctl cat '*fleet*' '*operator*' '*control*' 2>/dev/null || true"
  if ! user_systemctl cat '*fleet*' '*operator*' '*control*'; then
    true
  fi

  section "Root and user state split audit"
  for path in \
    "$USER_HOME/.local/share/fleet-control-hub" \
    "/root/.local/share/fleet-control-hub"; do
    if [ -e "$path" ]; then
      printf 'STATE_PATH=%s\n' "$path"
      find "$path" -maxdepth 3 -type f \
        \( -name '*.db' -o -name '*.sqlite' -o -name '*.json' \) \
        -printf '%u %g %m %s %TY-%Tm-%TdT%TH:%TM:%TS %p\n' 2>/dev/null || true
    fi
  done
fi

section "Fleet checkout validation"
if [ -d "$FLEET_DIR/.git" ] || git -C "$FLEET_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  run_probe git -C "$FLEET_DIR" status --short --branch
  run_probe git -C "$FLEET_DIR" remote -v
  if [ "$NO_FETCH" -eq 0 ]; then
    run_required git -C "$FLEET_DIR" fetch --all --prune
  else
    printf 'GIT_FETCH_SKIPPED=YES\n'
  fi
  run_probe git -C "$FLEET_DIR" branch -vv
  run_probe git -C "$FLEET_DIR" worktree list --porcelain

  shell_files=()
  for path in \
    "$FLEET_DIR/bootstrap.sh" \
    "$FLEET_DIR/healthcheck.sh" \
    "$FLEET_DIR/install_nix.sh" \
    "$FLEET_DIR/fleetctl" \
    "$FLEET_DIR/ops/fleet_host_remediation.sh"; do
    [ -f "$path" ] && shell_files+=("$path")
  done
  if ((${#shell_files[@]})); then
    run_required bash -n "${shell_files[@]}"
    if command -v shellcheck >/dev/null 2>&1; then
      run_required shellcheck "${shell_files[@]}"
    else
      printf 'SHELLCHECK_UNAVAILABLE=YES\n'
    fi
  fi

  compile_existing_python

  if [ -d "$FLEET_DIR/tests" ]; then
    run_required python3 -m unittest discover -s "$FLEET_DIR/tests" -p 'test_*.py' -v
    if [ -f "$FLEET_DIR/tests/test_healthcheck.sh" ]; then
      run_required bash "$FLEET_DIR/tests/test_healthcheck.sh"
    fi
    if [ -f "$FLEET_DIR/tests/test_fleet_host_remediation.sh" ]; then
      printf 'HOST_REMEDIATION_SELF_TEST_SKIPPED=recursive-invocation\n'
    fi
  fi
else
  REQUIRED_FAILURES=$((REQUIRED_FAILURES + 1))
  printf 'COMMAND_FAILED required=yes reason=fleet-checkout-not-found path=%s\n' "$FLEET_DIR"
fi

if [ "$QUICK" -eq 0 ]; then
  section "Ownership audit"
  for path in \
    "$OUTER_WORKSPACE" \
    "$USER_HOME/worktrees" \
    "$USER_HOME/.local/share/fleet-control-hub" \
    "$USER_HOME/continuity"; do
    [ -e "$path" ] || continue
    printf 'OWNERSHIP_ROOT=%s\n' "$path"
    find "$path" -xdev ! -user "$(id -un)" -printf '%u %g %m %p\n' 2>/dev/null | head -n 500 || true
  done
fi

section "Security-sensitive exclusions"
printf 'No contents from .ssh, .gnupg, token stores, cookies, .env files, or authentication databases were copied into this report.\n'

printf '%s\n' "$REPORT" > "$STATE_DIR/latest-report"
printf '%s\n' "$OUT" > "$STATE_DIR/latest-output-dir"
chmod 600 "$STATE_DIR/latest-report" "$STATE_DIR/latest-output-dir"

python3 - "$SUMMARY_JSON" "$TS" "$REPORT" "$ARCHIVE_DIR" "$REQUIRED_FAILURES" "$DIAGNOSTIC_WARNINGS" <<'PY'
import json
import sys
from pathlib import Path

path, timestamp, report, backup_dir, failures, warnings = sys.argv[1:]
payload = {
    "timestamp": timestamp,
    "report": report,
    "backup_dir": backup_dir,
    "required_failures": int(failures),
    "diagnostic_warnings": int(warnings),
    "audit_mode": "reversible-only",
    "git_metadata_moved": False,
    "database_deleted": False,
    "systemd_unit_changed": False,
}
Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
chmod 600 "$SUMMARY_JSON"

section "Completion"
printf 'REPORT=%s\n' "$REPORT"
printf 'SUMMARY_JSON=%s\n' "$SUMMARY_JSON"
printf 'BACKUP_DIR=%s\n' "$ARCHIVE_DIR"
printf 'LATEST_REPORT_POINTER=%s\n' "$STATE_DIR/latest-report"
printf 'REQUIRED_FAILURE_COUNT=%s\n' "$REQUIRED_FAILURES"
printf 'DIAGNOSTIC_WARNING_COUNT=%s\n' "$DIAGNOSTIC_WARNINGS"
printf 'AUDIT_MODE=REVERSIBLE_ONLY\n'
printf 'NO_GIT_METADATA_MOVED=YES\n'
printf 'NO_DATABASE_DELETED=YES\n'
printf 'NO_SYSTEMD_UNIT_CHANGED=YES\n'

if [ "$REQUIRED_FAILURES" -ne 0 ]; then
  exit 1
fi
