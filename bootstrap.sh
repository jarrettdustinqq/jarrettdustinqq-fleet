#!/usr/bin/env bash
set -euo pipefail

FLEET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECTS_DIR="${PROJECTS_DIR:-$HOME/projects}"
SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/id_ed25519}"
SSH_KEY_COMMENT="${SSH_KEY_COMMENT:-$USER@$(hostname)-fleet}"

log() {
  printf '[fleet-bootstrap] %s\n' "$*"
}

ensure_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log "Missing required command: $1"
    return 1
  fi
}

install_base_tools_if_possible() {
  local missing_pkgs=()
  command -v git >/dev/null 2>&1 || missing_pkgs+=("git")
  command -v curl >/dev/null 2>&1 || missing_pkgs+=("curl")

  if [ "${#missing_pkgs[@]}" -eq 0 ]; then
    return 0
  fi

  if command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    log "Installing missing packages: ${missing_pkgs[*]}"
    sudo apt-get update
    sudo apt-get install -y "${missing_pkgs[@]}"
  else
    log "Cannot auto-install packages without sudo+apt-get."
    log "Install these packages manually: ${missing_pkgs[*]}"
    exit 1
  fi
}

ensure_ssh_key() {
  ensure_cmd ssh
  ensure_cmd ssh-keygen

  mkdir -p "$HOME/.ssh"
  chmod 700 "$HOME/.ssh"

  if [ ! -f "$SSH_KEY_PATH" ]; then
    log "Creating SSH key at $SSH_KEY_PATH because an SSH repository URL is configured"
    ssh-keygen -t ed25519 -C "$SSH_KEY_COMMENT" -N "" -f "$SSH_KEY_PATH" >/dev/null
  fi

  if [ -z "${SSH_AUTH_SOCK:-}" ] && command -v ssh-agent >/dev/null 2>&1; then
    if ! eval "$(ssh-agent -s)" >/dev/null 2>&1; then
      log "ssh-agent unavailable in this environment; continuing to access preflight."
    fi
  fi

  if [ -n "${SSH_AUTH_SOCK:-}" ] && command -v ssh-add >/dev/null 2>&1; then
    ssh-add "$SSH_KEY_PATH" >/dev/null 2>&1 || true
  fi
}

load_repo_urls() {
  local -n _out_arr=$1
  shift || true

  if [ "$#" -gt 0 ]; then
    _out_arr=("$@")
    return
  fi

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

configure_repo_auth() {
  local repos=("$@")
  local url
  local has_https=0
  local has_ssh=0

  for url in "${repos[@]}"; do
    case "$url" in
      https://github.com/*) has_https=1 ;;
      git@github.com:*|ssh://*) has_ssh=1 ;;
    esac
  done

  if [ "$has_https" -eq 1 ]; then
    if github_https_auth_available; then
      log "Configuring Git to use authenticated GitHub CLI credentials for HTTPS"
      GH_PROMPT_DISABLED=1 GIT_TERMINAL_PROMPT=0 \
        gh auth setup-git --hostname github.com >/dev/null
    else
      log "No authenticated GitHub CLI HTTPS helper detected; existing Git credentials or anonymous access will be tested noninteractively."
    fi
  fi

  if [ "$has_ssh" -eq 1 ]; then
    ensure_ssh_key
  fi
}

preflight_repo_access() {
  local repos=("$@")
  local url
  local failed=0

  if [ "${#repos[@]}" -eq 0 ]; then
    return 0
  fi

  log "Preflighting noninteractive access to ${#repos[@]} configured repositories"
  for url in "${repos[@]}"; do
    if GH_PROMPT_DISABLED=1 GIT_TERMINAL_PROMPT=0 \
      git ls-remote "$url" >/dev/null 2>&1; then
      log "Access OK: $url"
    else
      log "Access FAILED: $url"
      failed=1
    fi
  done

  if [ "$failed" -ne 0 ]; then
    log "Repository access preflight failed. No clone or pull operations were started."
    return 1
  fi
}

clone_or_update_repos() {
  local repos=("$@")
  mkdir -p "$PROJECTS_DIR"

  if [ "${#repos[@]}" -eq 0 ]; then
    log "No repositories configured. Add URLs to $FLEET_DIR/repos.txt or pass them as args."
    return 0
  fi

  local url name target
  for url in "${repos[@]}"; do
    name="$(basename "$url" .git)"
    target="$PROJECTS_DIR/$name"

    if [ -d "$target/.git" ]; then
      log "Updating $name"
      GH_PROMPT_DISABLED=1 GIT_TERMINAL_PROMPT=0 \
        git -C "$target" pull --ff-only
    else
      log "Cloning $url -> $target"
      GH_PROMPT_DISABLED=1 GIT_TERMINAL_PROMPT=0 \
        git clone "$url" "$target"
    fi
  done
}

main() {
  install_base_tools_if_possible
  ensure_cmd git
  ensure_cmd curl

  local repo_urls=()
  load_repo_urls repo_urls "$@"
  configure_repo_auth "${repo_urls[@]}"
  preflight_repo_access "${repo_urls[@]}"
  clone_or_update_repos "${repo_urls[@]}"

  log "Bootstrap complete."
  log "Run health check: $FLEET_DIR/healthcheck.sh"
}

main "$@"
