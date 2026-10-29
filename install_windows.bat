@echo off
setlocal
py -3.12 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo.
echo Installation complete. Run start_windows.bat
pause
exit /b 0
:fail
echo Installation failed. Check that Python 3.12 is installed and available as py -3.12.
pause
exit /b 1
