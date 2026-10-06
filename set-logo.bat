@echo off
rem Applies backend\static\logo.png everywhere locally: startup screen and every program icon size.
cd /d "%~dp0"
copy /y backend\static\logo.png splash\logo.png >nul
if errorlevel 1 goto fail
call npm install
if errorlevel 1 goto fail
call npx tauri icon backend\static\logo.png
if errorlevel 1 goto fail
echo Done. Commit and push to publish the new icon.
goto end
:fail
echo.
echo Something went wrong. Read the message above.
:end
pause
