@echo off
REM Travel Blog Automation Platform — launcher (Windows).
REM Sets up venv + deps + .env, then starts backend (FastAPI) and UI (Streamlit).
setlocal
cd /d "%~dp0"

if not exist ".venv" (
    echo [run] Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

echo [run] Installing dependencies...
python -m pip install --upgrade pip -q
pip install -r requirements.txt -q

if not exist ".env" (
    echo [run] Creating .env from .env.example (fill in credentials).
    copy .env.example .env
)

echo [run] Starting FastAPI backend on :8000 and Streamlit UI on :8501
start "FastAPI" python app.py
start "Streamlit" streamlit run ui\dashboard.py --server.port 8501

endlocal
