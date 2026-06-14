#!/usr/bin/env bash
# Dev runner: loads .env, ensures the virtualenv & deps, and starts the server with auto-reload.
set -euo pipefail

cd "$(dirname "$0")"

# --- Load .env ---
if [[ ! -f .env ]]; then
  echo "No .env found. Copy the template first:  cp .env.example .env  (then fill in your tokens)" >&2
  exit 1
fi
set -a            # export everything we source
# shellcheck disable=SC1091
source .env
set +a

# --- Ensure virtualenv + deps ---
if [[ ! -d .venv ]]; then
  echo "Creating virtualenv (.venv) ..."
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

# --- Run with auto-reload ---
HOST="${SBF_HOST:-127.0.0.1}"
PORT="${SBF_PORT:-8000}"
echo "Starting SentryBugFixer on http://${HOST}:${PORT}  (Ctrl+C to stop)"
# IMPORTANT: only watch the source package. Cloned repos live under ./data and would
# otherwise trip the reloader and kill running fix jobs mid-clone.
exec ./.venv/bin/uvicorn sentrybugfixer.app:app --host "$HOST" --port "$PORT" \
  --reload --reload-dir sentrybugfixer
