@echo off
setlocal
if not exist .venv\Scripts\python.exe (
  echo First run install_windows.bat
  pause
  exit /b 1
)
start "ELAC SUB3030 GUI" http://127.0.0.1:8765
.venv\Scripts\python.exe run.py
pause
