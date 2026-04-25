#!/bin/bash
# Helper launcher for Smart Sauna Controller on Raspberry Pi
# - Updates code from the main branch (for public repo)
# - Activates virtualenv
# - Runs the Flask app

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

STATE_FILE="${SCRIPT_DIR}/sauna_state.json"
STATE_BACKUP="/tmp/sauna_state.backup.$$"

# Try to pull latest code; if it fails (no network, etc.) continue with existing
if command -v git >/dev/null 2>&1; then
  # Preserve local runtime state across pulls, even if older clones still track sauna_state.json.
  if [ -f "${STATE_FILE}" ]; then
    cp "${STATE_FILE}" "${STATE_BACKUP}" || true
  fi

  # Clean generated runtime artifacts that can block fast-forward updates.
  git restore --staged --worktree sauna_state.json >/dev/null 2>&1 || true
  find "${SCRIPT_DIR}" -type f -path '*/__pycache__/*.pyc' -delete >/dev/null 2>&1 || true
  git restore --staged --worktree __pycache__ >/dev/null 2>&1 || true

  git pull --ff-only origin main || echo "[run_sauna.sh] git pull failed; running existing code"

  if [ -f "${STATE_BACKUP}" ]; then
    cp "${STATE_BACKUP}" "${STATE_FILE}" || true
    rm -f "${STATE_BACKUP}"
  fi
fi

# Activate virtual environment
if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
else
  echo "[run_sauna.sh] Virtualenv .venv not found. Create it with:\n  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

# Avoid writing __pycache__ files on the runtime device.
export PYTHONDONTWRITEBYTECODE=1

# Run a single worker so only one SaunaController loop owns GPIO/relay state.
exec gunicorn --workers 1 --worker-class gthread --threads 4 --bind 0.0.0.0:5000 --timeout 60 main:app
