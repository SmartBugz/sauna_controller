#!/bin/bash
# Close Chromium kiosk windows for Smart Sauna.

set -euo pipefail

pkill -f "chromium.*--kiosk" >/dev/null 2>&1 || pkill -f chromium >/dev/null 2>&1 || true
