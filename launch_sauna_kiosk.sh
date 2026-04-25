#!/bin/bash
# Launch Smart Sauna UI in Chromium kiosk mode.
# If the backend is not running, start it in the background first.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="http://localhost:5000"

find_browser() {
  if command -v chromium-browser >/dev/null 2>&1; then
    echo "chromium-browser"
    return 0
  fi

  if command -v chromium >/dev/null 2>&1; then
    echo "chromium"
    return 0
  fi

  return 1
}

backend_running() {
  pgrep -f "python(3)? .*main.py" >/dev/null 2>&1
}

start_backend_if_needed() {
  if backend_running; then
    return 0
  fi

  mkdir -p "${HOME}/.cache/smart-sauna"
  nohup "${SCRIPT_DIR}/run_sauna.sh" > "${HOME}/.cache/smart-sauna/server.log" 2>&1 &
  sleep 2
}

main() {
  BROWSER="$(find_browser)" || {
    echo "[launch_sauna_kiosk.sh] Chromium not found. Install with: sudo apt install -y chromium-browser"
    exit 1
  }

  start_backend_if_needed

  exec "${BROWSER}" \
    --kiosk \
    --incognito \
    --no-first-run \
    --password-store=basic \
    --noerrdialogs \
    --disable-infobars \
    --check-for-update-interval=31536000 \
    "${URL}"
}

main "$@"
