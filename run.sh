#!/usr/bin/env bash
# Travel Blog Automation Platform — launcher.
# Sets up the venv + deps + .env, then starts backend (FastAPI) and UI (Streamlit).
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

# 1. Virtual environment.
if [ ! -d ".venv" ]; then
    echo "[run] Creating virtual environment..."
    "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 2. Dependencies (idempotent — pip skips already-installed).
echo "[run] Installing dependencies..."
pip install --upgrade pip -q >/dev/null 2>&1 || true
pip install -r requirements.txt -q

# 3. Secrets config.
if [ ! -f ".env" ]; then
    echo "[run] Creating .env from .env.example (fill in credentials, then re-run)."
    cp .env.example .env
fi

# 4. Launch backend + UI.
if [[ "${1:-}" == "--cli" ]]; then
    shift
    exec python cli.py "$@"
fi

echo "[run] Starting FastAPI backend on :8000 and Streamlit UI on :8501"
python app.py &
APP_PID=$!
streamlit run ui/dashboard.py --server.port "${UI_PORT:-8501}" "${STREAMLIT_ARGS:-}" &
UI_PID=$!

trap 'echo; echo "[run] Shutting down..."; kill "$APP_PID" "$UI_PID" 2>/dev/null || true' INT TERM
wait "$APP_PID" "$UI_PID"
