#!/bin/bash
# One-touch launcher for Raspberry Pi desktop:
# 1) restart sauna.service
# 2) open Chromium in kiosk mode

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

restart_service() {
  if sudo -n systemctl restart sauna.service >/dev/null 2>&1; then
    return 0
  fi

  if command -v pkexec >/dev/null 2>&1; then
    pkexec systemctl restart sauna.service
    return 0
  fi

  echo "[launch_sauna_service_kiosk.sh] Unable to restart sauna.service without interactive privilege escalation."
  echo "[launch_sauna_service_kiosk.sh] Configure passwordless sudo for 'systemctl restart sauna.service' or install pkexec."
  return 1
}

main() {
  restart_service

  # Give systemd a moment to start the app process before opening kiosk.
  sleep 2
  exec "${SCRIPT_DIR}/launch_sauna_kiosk.sh"
}

main "$@"
