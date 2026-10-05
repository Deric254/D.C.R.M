@echo off
rem Builds the Windows installer for DericBI CRM.
rem Needs: Python 3.10+, Node.js, Rust (rustup.rs) and the Visual Studio C++ Build Tools.
cd /d "%~dp0"
echo.
echo === Checking tools ===
where py >nul 2>nul
if errorlevel 1 goto nopython
where node >nul 2>nul
if errorlevel 1 goto nonode
where cargo >nul 2>nul
if errorlevel 1 goto norust

echo.
echo === 1/3 Packaging the CRM engine ===
py -3 -m pip install -q -r backend\requirements.txt pyinstaller
if errorlevel 1 goto fail
if exist backend-dist rmdir /s /q backend-dist
py -3 -m PyInstaller --noconfirm --clean --onefile --name dericbi-backend ^
  --distpath "%~dp0backend-dist\out" --workpath "%~dp0backend-dist\work" --specpath "%~dp0backend-dist\work" ^
  --collect-all playwright --add-data "%~dp0backend\static;static" ^
  --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.lifespan.on "%~dp0backend\main.py"
if errorlevel 1 goto fail

rem Tauri wants the engine named with the machine type, e.g. dericbi-backend-x86_64-pc-windows-msvc.exe
for /f "tokens=2" %%i in ('rustc -vV ^| findstr /b "host:"') do set TRIPLE=%%i
copy /y "backend-dist\out\dericbi-backend.exe" "backend-dist\dericbi-backend-%TRIPLE%.exe" >nul
if errorlevel 1 goto fail

echo.
echo === 2/3 Installing app tooling ===
call npm install
if errorlevel 1 goto fail

echo.
echo === 3/3 Building the installer (the first build takes several minutes) ===
call npm run build
if errorlevel 1 goto fail

echo.
echo Done. Your installer is in:
echo   src-tauri\target\release\bundle\nsis\
start "" "src-tauri\target\release\bundle\nsis"
goto end

:nopython
echo Python was not found. Install Python 3.10 or newer from python.org (tick "Add python.exe to PATH").
goto fail
:nonode
echo Node.js was not found. Install it from nodejs.org.
goto fail
:norust
echo Rust was not found. Install it from rustup.rs, then restart this window.
goto fail
:fail
echo.
echo The build stopped. Read the message above, fix it, and run this file again.
pause
exit /b 1
:end
pause
