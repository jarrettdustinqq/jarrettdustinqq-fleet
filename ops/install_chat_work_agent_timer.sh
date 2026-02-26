#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_NAME="fleet-chat-work-hourly.service"
TIMER_NAME="fleet-chat-work-hourly.timer"
SERVICE_PATH="${UNIT_DIR}/${SERVICE_NAME}"
TIMER_PATH="${UNIT_DIR}/${TIMER_NAME}"

usage() {
  cat <<'EOF'
Usage:
  install_chat_work_agent_timer.sh [options]

Options:
  --uninstall      Remove timer/service units and disable them.
  --status         Show timer + service status.
  -h, --help       Show this help.
EOF
}

log() { printf '[chat-agent-timer] %s\n' "$*"; }
ok() { printf '[ok] %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*"; }

show_status() {
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl not found"
    return 0
  fi
  systemctl --user status "${TIMER_NAME}" --no-pager || true
  systemctl --user status "${SERVICE_NAME}" --no-pager || true
  systemctl --user list-timers "${TIMER_NAME}" --no-pager || true
}

install_units() {
  mkdir -p "${UNIT_DIR}"

  cat >"${SERVICE_PATH}" <<EOF
[Unit]
Description=Fleet chat-agent synthesis run
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${REPO_DIR}
ExecStart=/usr/bin/env bash -lc './fleetctl chat-agent --top 12'
EOF

  cat >"${TIMER_PATH}" <<'EOF'
[Unit]
Description=Run Fleet chat-agent hourly

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=300
Unit=fleet-chat-work-hourly.service

[Install]
WantedBy=timers.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now "${TIMER_NAME}"
  ok "Installed and started ${TIMER_NAME}"
}

uninstall_units() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now "${TIMER_NAME}" >/dev/null 2>&1 || true
    systemctl --user stop "${SERVICE_NAME}" >/dev/null 2>&1 || true
  fi

  rm -f "${SERVICE_PATH}" "${TIMER_PATH}"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload || true
  fi
  ok "Removed ${TIMER_NAME} and ${SERVICE_NAME}"
}

MODE="install"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --uninstall)
      MODE="uninstall"
      shift
      ;;
    --status)
      MODE="status"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 1
      ;;
  esac
done

if ! command -v systemctl >/dev/null 2>&1; then
  warn "systemctl is required for timer management"
  exit 1
fi

case "${MODE}" in
  install)
    install_units
    show_status
    ;;
  uninstall)
    uninstall_units
    ;;
  status)
    show_status
    ;;
esac
