@echo off
cd /d %~dp0
if not exist .venv (
  py -3.12 -m venv .venv || python -m venv .venv
  call .venv\Scripts\activate
  python -m pip install --upgrade pip
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate
)
if not exist .env (
  copy .env.example .env
  notepad .env
  exit /b
)
python main.py
pause
