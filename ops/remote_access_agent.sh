#!/usr/bin/env bash
set -euo pipefail

SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/id_ed25519}"
GITHUB_TARGET="${GITHUB_TARGET:-git@github.com}"
CONFIG_FILE="${CONFIG_FILE:-$HOME/.config/fleet/remote-agent.env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VPS_TARGET="${VPS_TARGET:-}"
TRY_GH_ADD=0
TRY_COPY_KEY=0
AUTO_MODE=0
SAVE_VPS_TARGET=""
DISCOVER_VPS=0
DISCOVERY_DEFAULT_USER=""

log() { printf '[remote-agent] %s\n' "$*"; }
ok() { printf '[ok] %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*"; }
fail() { printf '[fail] %s\n' "$*"; }

usage() {
  cat <<'EOF'
Usage:
  remote_access_agent.sh [options]

Options:
  --vps <user@host>   Verify SSH access to a VPS target.
  --save-vps <user@host>
                      Save VPS target to config for future runs.
  --discover          Auto-discover a likely VPS target from local evidence.
  --default-user <u>  Fallback SSH user for host-only discovered targets.
  --gh-add            If gh CLI is authenticated, add SSH key to GitHub.
  --copy-key          Run ssh-copy-id to VPS target before verification.
  --auto              Run full flow (add GitHub key + copy key if VPS target exists).
  -h, --help          Show help.
EOF
}

load_config() {
  if [ -f "$CONFIG_FILE" ]; then
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
  fi
}

save_vps_config() {
  [ -n "$SAVE_VPS_TARGET" ] || return 0

  mkdir -p "$(dirname "$CONFIG_FILE")"
  cat >"$CONFIG_FILE" <<EOF
VPS_TARGET="$SAVE_VPS_TARGET"
EOF
  chmod 600 "$CONFIG_FILE"
  ok "Saved VPS target to $CONFIG_FILE"
}

load_config

while [ "$#" -gt 0 ]; do
  case "$1" in
    --vps)
      VPS_TARGET="${2:-}"
      shift 2
      ;;
    --save-vps)
      SAVE_VPS_TARGET="${2:-}"
      VPS_TARGET="${2:-}"
      shift 2
      ;;
    --gh-add)
      TRY_GH_ADD=1
      shift
      ;;
    --discover)
      DISCOVER_VPS=1
      shift
      ;;
    --default-user)
      DISCOVERY_DEFAULT_USER="${2:-}"
      shift 2
      ;;
    --copy-key)
      TRY_COPY_KEY=1
      shift
      ;;
    --auto)
      AUTO_MODE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

if [ "$AUTO_MODE" -eq 1 ]; then
  TRY_GH_ADD=1
  if [ -n "$VPS_TARGET" ]; then
    TRY_COPY_KEY=1
  else
    DISCOVER_VPS=1
  fi
fi

ensure_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "Missing required command: $1"
    exit 1
  fi
}

ensure_ssh_key() {
  mkdir -p "$HOME/.ssh"
  chmod 700 "$HOME/.ssh"

  if [ ! -f "$SSH_KEY_PATH" ]; then
    log "SSH key missing. Creating one at $SSH_KEY_PATH"
    ssh-keygen -t ed25519 -C "$USER@$(hostname)-fleet" -N "" -f "$SSH_KEY_PATH" >/dev/null
    ok "SSH key created"
  else
    ok "SSH key exists at $SSH_KEY_PATH"
  fi

  log "Public key (copy this if needed):"
  cat "${SSH_KEY_PATH}.pub"
}

maybe_add_github_key() {
  [ "$TRY_GH_ADD" -eq 1 ] || return 0

  if ! command -v gh >/dev/null 2>&1; then
    warn "gh CLI not installed; skipping GitHub key add"
    return 0
  fi

  if ! gh auth status >/dev/null 2>&1; then
    warn "gh is not authenticated."
    if [ -t 0 ] && [ "$AUTO_MODE" -eq 1 ]; then
      printf 'Run "gh auth login" now? [y/N]: '
      read -r reply
      if [[ "$reply" =~ ^[Yy]$ ]]; then
        gh auth login
      else
        warn "Skipping GitHub key add"
        return 0
      fi
    else
      warn "Run: gh auth login"
      return 0
    fi
  fi

  local title
  title="${USER}@$(hostname)-$(date +%Y%m%d)"
  gh ssh-key add "${SSH_KEY_PATH}.pub" --title "$title" >/dev/null
  ok "Added SSH key to GitHub via gh"
}

check_github_auth() {
  log "Testing GitHub SSH auth (${GITHUB_TARGET})"

  set +e
  local out rc
  out="$(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -T "$GITHUB_TARGET" 2>&1)"
  rc=$?
  set -e

  # GitHub returns exit code 1 on successful auth because shell access is disabled.
  if [ $rc -eq 1 ] && printf '%s' "$out" | grep -qi "successfully authenticated"; then
    ok "GitHub SSH auth works"
    return 0
  fi

  warn "GitHub SSH check output:"
  printf '%s\n' "$out"
  fail "GitHub SSH auth not confirmed"
  return 1
}

maybe_copy_key_to_vps() {
  [ "$TRY_COPY_KEY" -eq 1 ] || return 0

  if [ -z "$VPS_TARGET" ]; then
    fail "--copy-key requires --vps <user@host>"
    exit 1
  fi

  if ! command -v ssh-copy-id >/dev/null 2>&1; then
    warn "ssh-copy-id not installed; skipping copy"
    return 0
  fi

  log "Copying SSH key to VPS: $VPS_TARGET"
  ssh-copy-id -i "${SSH_KEY_PATH}.pub" "$VPS_TARGET"
  ok "Key copied to VPS"
}

check_vps_auth() {
  if [ -z "$VPS_TARGET" ]; then
    warn "No VPS target provided; skipping VPS check"
    return 0
  fi

  log "Testing VPS SSH auth ($VPS_TARGET)"
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
    "$VPS_TARGET" 'hostname && uptime'
  ok "VPS SSH auth works"
}

probe_vps_target_hint() {
  local target="$1"
  set +e
  local out rc
  out="$(ssh -o BatchMode=yes -o ConnectTimeout=7 -o StrictHostKeyChecking=accept-new \
    "$target" 'exit 0' 2>&1)"
  rc=$?
  set -e

  if [ $rc -eq 0 ]; then
    ok "Probe reached $target and authenticated"
    return 0
  fi

  if printf '%s' "$out" | grep -qiE 'permission denied|authentication failed|publickey'; then
    ok "Probe reached $target (authentication still needed, expected before key copy)"
    return 0
  fi

  if printf '%s' "$out" | grep -qiE 'timed out|no route to host|name or service not known|could not resolve hostname|connection refused'; then
    warn "Probe suggests $target may be unreachable"
    warn "ssh probe: $(printf '%s' "$out" | head -n 1)"
    return 1
  fi

  warn "Probe result for $target was inconclusive"
  warn "ssh probe: $(printf '%s' "$out" | head -n 1)"
  return 0
}

maybe_choose_vps_target_interactive() {
  [ -t 0 ] || return 0

  local -a candidates=()
  local -a scores=()
  local -a sources=()
  local target score source selection continue_anyway
  local -a cmd=(
    python3
    "$SCRIPT_DIR/vps_discovery_agent.py"
    --tsv
    --limit
    5
  )
  if [ -n "$DISCOVERY_DEFAULT_USER" ]; then
    cmd+=(--default-user "$DISCOVERY_DEFAULT_USER")
  fi

  while IFS=$'\t' read -r target score source; do
    [ -n "${target:-}" ] || continue
    candidates+=("$target")
    scores+=("${score:-0}")
    sources+=("${source:-unknown}")
  done < <("${cmd[@]}" 2>/dev/null)

  selection=""
  if [ "${#candidates[@]}" -gt 0 ]; then
    warn "No high-confidence target yet. Top candidates:"
    local i
    for i in "${!candidates[@]}"; do
      printf '  [%d] %s (score=%s; sources=%s)\n' \
        "$((i + 1))" "${candidates[$i]}" "${scores[$i]}" "${sources[$i]}"
    done
    printf 'Choose candidate number, enter user@host, "skip", or Enter for [%s]: ' "${candidates[0]}"
    read -r selection

    if [ -z "$selection" ]; then
      selection="${candidates[0]}"
    elif [[ "$selection" =~ ^[0-9]+$ ]]; then
      local idx=$((selection - 1))
      if [ "$idx" -lt 0 ] || [ "$idx" -ge "${#candidates[@]}" ]; then
        warn "Invalid candidate number: $selection"
        return 0
      fi
      selection="${candidates[$idx]}"
    fi
  else
    printf 'Enter VPS target (user@host) or press Enter to skip: '
    read -r selection
  fi

  case "$selection" in
    ""|"skip"|"SKIP")
      return 0
      ;;
  esac

  if [[ "$selection" != *"@"* ]] && [ -n "$DISCOVERY_DEFAULT_USER" ]; then
    selection="${DISCOVERY_DEFAULT_USER}@${selection}"
  fi

  if ! probe_vps_target_hint "$selection"; then
    printf 'Use this target anyway? [y/N]: '
    read -r continue_anyway
    if [[ ! "$continue_anyway" =~ ^[Yy]$ ]]; then
      warn "Skipping VPS target selection"
      return 0
    fi
  fi

  VPS_TARGET="$selection"
  SAVE_VPS_TARGET="$selection"
  TRY_COPY_KEY=1
  ok "Using selected VPS target: $VPS_TARGET"
}

maybe_discover_vps_target() {
  [ "$DISCOVER_VPS" -eq 1 ] || return 0

  if ! command -v python3 >/dev/null 2>&1; then
    warn "python3 not found; cannot auto-discover VPS target"
    return 0
  fi

  local cmd=(
    python3
    "$SCRIPT_DIR/vps_discovery_agent.py"
    --best
  )
  if [ -n "$DISCOVERY_DEFAULT_USER" ]; then
    cmd+=(--default-user "$DISCOVERY_DEFAULT_USER")
  fi

  set +e
  local out rc
  out="$("${cmd[@]}" 2>/dev/null)"
  rc=$?
  set -e

  if [ $rc -ne 0 ] || [ -z "$out" ]; then
    warn "Could not auto-discover a high-confidence VPS target"
    if [ -t 0 ] && [ "$AUTO_MODE" -eq 1 ]; then
      maybe_choose_vps_target_interactive
    fi
    return 0
  fi

  VPS_TARGET="$out"
  ok "Auto-discovered VPS target: $VPS_TARGET"

  if [ -z "$SAVE_VPS_TARGET" ]; then
    SAVE_VPS_TARGET="$VPS_TARGET"
  fi

  if [ "$AUTO_MODE" -eq 1 ]; then
    TRY_COPY_KEY=1
  fi
}

main() {
  ensure_cmd ssh
  ensure_cmd ssh-keygen

  maybe_discover_vps_target
  save_vps_config
  ensure_ssh_key
  maybe_add_github_key

  local rc=0
  check_github_auth || rc=1
  maybe_copy_key_to_vps
  check_vps_auth || rc=1

  if [ $rc -eq 0 ]; then
    ok "Remote access checks complete"
  else
    fail "One or more checks failed"
  fi
  return $rc
}

main "$@"
