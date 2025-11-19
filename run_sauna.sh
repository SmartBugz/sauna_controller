#!/bin/bash
# Helper launcher for Smart Sauna Controller on Raspberry Pi
# - Updates code from the main branch (for public repo)
# - Activates virtualenv
# - Runs the Flask app

set -e

cd /home/pi/sauna_controller

# Try to pull latest code; if it fails (no network, etc.) continue with existing
if command -v git >/dev/null 2>&1; then
  git pull --ff-only origin main || echo "[run_sauna.sh] git pull failed; running existing code"
fi

# Activate virtual environment
if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
else
  echo "[run_sauna.sh] Virtualenv .venv not found. Create it with:\n  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

# Start the app
exec python main.py
