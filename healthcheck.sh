#!/usr/bin/env bash
set -euo pipefail

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
  # Non-interactive shells may not source ~/.profile.
  if ! command -v nix >/dev/null 2>&1 && [ -e "$HOME/.nix-profile/etc/profile.d/nix.sh" ]; then
    # shellcheck source=/dev/null
    . "$HOME/.nix-profile/etc/profile.d/nix.sh"
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

check_github_auth() {
  if [ -f "$SSH_KEY_PATH" ] && [ -f "${SSH_KEY_PATH}.pub" ]; then
    ok "GitHub auth available: SSH keypair at $SSH_KEY_PATH"
    return 0
  fi

  if github_https_auth_available; then
    ok "GitHub auth available: authenticated gh CLI over HTTPS"
    return 0
  fi

  warn "GitHub auth unavailable: no complete SSH keypair at $SSH_KEY_PATH and no authenticated gh CLI over HTTPS"
  return 1
}

main() {
  local rc=0

  check_cmd git || rc=1
  check_cmd curl || rc=1
  check_cmd ssh || rc=1
  load_nix_env_if_present

  if command -v nix >/dev/null 2>&1; then
    ok "nix found: $(nix --version)"
  else
    warn "nix not found in PATH"
    rc=1
  fi

  check_github_auth || rc=1

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
