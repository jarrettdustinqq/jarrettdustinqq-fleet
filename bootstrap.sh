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
  command -v ssh >/dev/null 2>&1 || missing_pkgs+=("openssh-client")

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
  mkdir -p "$HOME/.ssh"
  chmod 700 "$HOME/.ssh"

  if [ ! -f "$SSH_KEY_PATH" ]; then
    log "Creating SSH key at $SSH_KEY_PATH"
    ssh-keygen -t ed25519 -C "$SSH_KEY_COMMENT" -N "" -f "$SSH_KEY_PATH" >/dev/null
  fi

  # Start an agent only if one is not already present.
  if [ -z "${SSH_AUTH_SOCK:-}" ]; then
    if ! eval "$(ssh-agent -s)" >/dev/null 2>&1; then
      log "ssh-agent unavailable in this environment; continuing without agent."
    fi
  fi

  if [ -n "${SSH_AUTH_SOCK:-}" ]; then
    ssh-add "$SSH_KEY_PATH" >/dev/null 2>&1 || true
  fi
  log "Public key:"
  cat "${SSH_KEY_PATH}.pub"
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
      git -C "$target" pull --ff-only
    else
      log "Cloning $url -> $target"
      git clone "$url" "$target"
    fi
  done
}

main() {
  install_base_tools_if_possible
  ensure_cmd git
  ensure_cmd curl
  ensure_cmd ssh
  ensure_cmd ssh-keygen

  ensure_ssh_key

  local repo_urls=()
  load_repo_urls repo_urls "$@"
  clone_or_update_repos "${repo_urls[@]}"

  log "Bootstrap complete."
  log "Run health check: $FLEET_DIR/healthcheck.sh"
}

main "$@"
