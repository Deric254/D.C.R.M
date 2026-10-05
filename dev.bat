@echo off
rem Runs the desktop app straight from the source code (needs Python, Node.js and Rust).
cd /d "%~dp0"
echo Installing the Python packages...
py -3 -m pip install -q -r backend\requirements.txt
if errorlevel 1 goto fail
echo Installing the app tooling (first time only)...
call npm install
if errorlevel 1 goto fail
echo Starting DericBI CRM...
call npm run dev
goto end
:fail
echo.
echo Something went wrong. Read the message above, fix it, and run this again.
pause
:end
