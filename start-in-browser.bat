@echo off
rem Runs the CRM without the Tauri window. Handy for a quick try or if the desktop build gives you trouble.
cd /d "%~dp0"
py -3 -m pip install -q -r backend\requirements.txt
if errorlevel 1 goto fail
echo.
echo DericBI CRM is starting. Your browser will open in a few seconds.
echo Keep this window open while you use it. Close it to stop the CRM.
echo.
set DERICBI_CONSOLE=1
start "" /b cmd /c "ping -n 5 127.0.0.1 >nul & start http://127.0.0.1:8765"
py -3 backend\main.py
goto end
:fail
echo.
echo Could not install the Python packages. Is Python 3.10+ installed and on PATH?
pause
:end
