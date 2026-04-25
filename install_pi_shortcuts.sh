#!/bin/bash
# Install desktop shortcuts for Smart Sauna on Raspberry Pi.
# Creates:
# - Smart Sauna Server.desktop (starts backend in a terminal)
# - Smart Sauna Kiosk.desktop (starts backend if needed and opens Chromium kiosk)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HOME}"
DESKTOP_DIR="${HOME_DIR}/Desktop"
APPLICATIONS_DIR="${HOME_DIR}/.local/share/applications"
AUTOSTART_DIR="${HOME_DIR}/.config/autostart"

SERVER_SHORTCUT="${DESKTOP_DIR}/Smart Sauna Server.desktop"
KIOSK_SHORTCUT="${DESKTOP_DIR}/Smart Sauna Kiosk.desktop"
SERVICE_KIOSK_SHORTCUT="${DESKTOP_DIR}/Smart Sauna Start Kiosk.desktop"
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

chmod +x "${SERVER_SHORTCUT}" "${KIOSK_SHORTCUT}" "${SERVICE_KIOSK_SHORTCUT}"

cp "${SERVER_SHORTCUT}" "${APPLICATIONS_DIR}/smart-sauna-server.desktop"
cp "${KIOSK_SHORTCUT}" "${APPLICATIONS_DIR}/smart-sauna-kiosk.desktop"
cp "${SERVICE_KIOSK_SHORTCUT}" "${APPLICATIONS_DIR}/smart-sauna-start-kiosk.desktop"

if ${enable_autostart}; then
  cp "${KIOSK_SHORTCUT}" "${AUTOSTART_SHORTCUT}"
  chmod +x "${AUTOSTART_SHORTCUT}"
fi

echo "Installed desktop launchers:"
echo "- ${SERVER_SHORTCUT}"
echo "- ${KIOSK_SHORTCUT}"
echo "- ${SERVICE_KIOSK_SHORTCUT}"

if ${enable_autostart}; then
  echo "Autostart enabled: ${AUTOSTART_SHORTCUT}"
else
  echo "Autostart not enabled. Re-run with --autostart to open kiosk automatically after login."
fi
