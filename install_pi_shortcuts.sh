#!/bin/bash
# Install desktop shortcuts for Smart Sauna on Raspberry Pi.
# Creates:
# - Smart Sauna Server.desktop (starts backend in a terminal)
# - Smart Sauna Kiosk.desktop (starts backend if needed and opens Chromium kiosk)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HOME}"
DESKTOP_DIR=""
APPLICATIONS_DIR="${HOME_DIR}/.local/share/applications"
AUTOSTART_DIR="${HOME_DIR}/.config/autostart"

if command -v xdg-user-dir >/dev/null 2>&1; then
  CANDIDATE_DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
  if [[ -n "${CANDIDATE_DESKTOP_DIR}" ]]; then
    DESKTOP_DIR="${CANDIDATE_DESKTOP_DIR}"
  fi
fi

if [[ -z "${DESKTOP_DIR}" ]]; then
  if [[ -d "${HOME_DIR}/Desktop" ]]; then
    DESKTOP_DIR="${HOME_DIR}/Desktop"
  elif [[ -d "${HOME_DIR}/desktop" ]]; then
    DESKTOP_DIR="${HOME_DIR}/desktop"
  else
    DESKTOP_DIR="${HOME_DIR}/Desktop"
  fi
fi

SERVER_SHORTCUT="${DESKTOP_DIR}/Smart Sauna Server.desktop"
KIOSK_SHORTCUT="${DESKTOP_DIR}/Smart Sauna Kiosk.desktop"
SERVICE_KIOSK_SHORTCUT="${DESKTOP_DIR}/Smart Sauna Start Kiosk.desktop"
EXIT_KIOSK_SHORTCUT="${DESKTOP_DIR}/Smart Sauna Exit Kiosk.desktop"
AUTOSTART_SHORTCUT="${AUTOSTART_DIR}/smart-sauna-kiosk.desktop"

SERVER_EXEC="${PROJECT_DIR}/run_sauna.sh"

enable_autostart=false
if [[ "${1:-}" == "--autostart" ]]; then
  enable_autostart=true
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "[install_pi_shortcuts.sh] This installer is intended for Raspberry Pi Linux."
  exit 1
fi

if command -v lxterminal >/dev/null 2>&1; then
  SERVER_EXEC="lxterminal -e bash -lc '${PROJECT_DIR}/run_sauna.sh'"
elif command -v x-terminal-emulator >/dev/null 2>&1; then
  SERVER_EXEC="x-terminal-emulator -e bash -lc '${PROJECT_DIR}/run_sauna.sh'"
fi

mkdir -p "${DESKTOP_DIR}" "${APPLICATIONS_DIR}" "${AUTOSTART_DIR}"

chmod +x "${PROJECT_DIR}/run_sauna.sh"
chmod +x "${PROJECT_DIR}/launch_sauna_kiosk.sh"
chmod +x "${PROJECT_DIR}/launch_sauna_service_kiosk.sh"
chmod +x "${PROJECT_DIR}/exit_sauna_kiosk.sh"

cat > "${SERVER_SHORTCUT}" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Smart Sauna Server
Comment=Start Smart Sauna backend server
Exec=${SERVER_EXEC}
Icon=utilities-terminal
Terminal=false
Categories=Utility;
EOF

cat > "${KIOSK_SHORTCUT}" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Smart Sauna Kiosk
Comment=Launch Smart Sauna in kiosk mode
Exec=${PROJECT_DIR}/launch_sauna_kiosk.sh
Icon=web-browser
Terminal=false
Categories=Utility;
EOF

cat > "${SERVICE_KIOSK_SHORTCUT}" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Smart Sauna Start Kiosk
Comment=Restart sauna service and launch kiosk mode
Exec=${PROJECT_DIR}/launch_sauna_service_kiosk.sh
Icon=web-browser
Terminal=false
Categories=Utility;
EOF

cat > "${EXIT_KIOSK_SHORTCUT}" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Smart Sauna Exit Kiosk
Comment=Close Chromium kiosk mode
Exec=${PROJECT_DIR}/exit_sauna_kiosk.sh
Icon=application-exit
Terminal=false
Categories=Utility;
EOF

chmod +x "${SERVER_SHORTCUT}" "${KIOSK_SHORTCUT}" "${SERVICE_KIOSK_SHORTCUT}" "${EXIT_KIOSK_SHORTCUT}"

cp "${SERVER_SHORTCUT}" "${APPLICATIONS_DIR}/smart-sauna-server.desktop"
cp "${KIOSK_SHORTCUT}" "${APPLICATIONS_DIR}/smart-sauna-kiosk.desktop"
cp "${SERVICE_KIOSK_SHORTCUT}" "${APPLICATIONS_DIR}/smart-sauna-start-kiosk.desktop"
cp "${EXIT_KIOSK_SHORTCUT}" "${APPLICATIONS_DIR}/smart-sauna-exit-kiosk.desktop"

if ${enable_autostart}; then
  cp "${KIOSK_SHORTCUT}" "${AUTOSTART_SHORTCUT}"
  chmod +x "${AUTOSTART_SHORTCUT}"
fi

echo "Installed desktop launchers:"
echo "- ${SERVER_SHORTCUT}"
echo "- ${KIOSK_SHORTCUT}"
echo "- ${SERVICE_KIOSK_SHORTCUT}"
echo "- ${EXIT_KIOSK_SHORTCUT}"

if ${enable_autostart}; then
  echo "Autostart enabled: ${AUTOSTART_SHORTCUT}"
else
  echo "Autostart not enabled. Re-run with --autostart to open kiosk automatically after login."
fi
