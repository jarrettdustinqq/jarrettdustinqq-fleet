#!/usr/bin/env bash
set -euo pipefail

FLEET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECTS_DIR="${PROJECTS_DIR:-$HOME/projects}"
SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/id_ed25519}"

ok() { printf '[ok] %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*"; }
fail() { printf '[fail] %s\n' "$*"; }

check_cmd() {
  local cmd="$1"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd found"
  else
    fail "$cmd missing"
    return 1
  fi
}

load_nix_env_if_present() {
  if ! command -v nix >/dev/null 2>&1 && [ -e "$HOME/.nix-profile/etc/profile.d/nix.sh" ]; then
    # shellcheck source=/dev/null
    . "$HOME/.nix-profile/etc/profile.d/nix.sh"
  fi
}

load_repo_urls() {
  local -n _out_arr=$1

  if [ -f "$FLEET_DIR/repos.txt" ]; then
    mapfile -t _out_arr < <(grep -vE '^[[:space:]]*(#|$)' "$FLEET_DIR/repos.txt")
  else
    _out_arr=()
  fi
}

github_https_auth_available() {
  local protocol

  command -v gh >/dev/null 2>&1 || return 1
  protocol="$(
    GH_PROMPT_DISABLED=1 GIT_TERMINAL_PROMPT=0 \
      gh config get git_protocol --host github.com 2>/dev/null
  )" || return 1
  [ "$protocol" = "https" ] || return 1

  GH_PROMPT_DISABLED=1 GIT_TERMINAL_PROMPT=0 \
    gh auth status --hostname github.com >/dev/null 2>&1
}

report_auth_configuration() {
  if github_https_auth_available; then
    ok "GitHub CLI authenticated over HTTPS"
    return 0
  fi

  if [ -f "$SSH_KEY_PATH" ] && [ -f "${SSH_KEY_PATH}.pub" ]; then
    warn "SSH keypair present at $SSH_KEY_PATH; key presence alone is not treated as proof of GitHub access"
    return 0
  fi

  warn "No authenticated GitHub CLI HTTPS session or local SSH keypair detected; required-repository access preflight will decide health"
}

check_required_repo_access() {
  local repos=()
  local url
  local rc=0

  load_repo_urls repos
  if [ "${#repos[@]}" -eq 0 ]; then
    fail "No active repositories configured in $FLEET_DIR/repos.txt"
    return 1
  fi

  for url in "${repos[@]}"; do
    if GH_PROMPT_DISABLED=1 GIT_TERMINAL_PROMPT=0 \
      git ls-remote "$url" >/dev/null 2>&1; then
      ok "repo access: $url"
    else
      fail "repo access unavailable noninteractively: $url"
      rc=1
    fi
  done

  return "$rc"
}

main() {
  local rc=0

  check_cmd git || rc=1
  check_cmd curl || rc=1
  load_nix_env_if_present

  if command -v nix >/dev/null 2>&1; then
    ok "nix found: $(nix --version)"
  else
    warn "nix not found in PATH"
    rc=1
  fi

  report_auth_configuration
  check_required_repo_access || rc=1

  if [ -d "$PROJECTS_DIR" ]; then
    ok "projects dir exists: $PROJECTS_DIR"
    find "$PROJECTS_DIR" -mindepth 1 -maxdepth 1 -type d -printf ' - %f\n' 2>/dev/null || true
  else
    warn "projects dir missing: $PROJECTS_DIR"
    rc=1
  fi

  exit "$rc"
}

main "$@"
