@echo off
REM ===========================================================================
REM Dwell Time Monitor - Windows one-click rebuild script (ONNX Runtime v0.3)
REM
REM Usage: double-click this file, or run from a terminal inside the project
REM        root:  build_windows.bat
REM
REM Output: dist\windows\dwell_monitor\dwell_monitor.exe
REM         dist\windows\dwell_monitor_windows_x64.zip
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM --- 1. find Python 3.10+ ---
set PY=
for %%C in (python py -3.13 py -3.12 py -3.11 py -3.10) do (
    if not defined PY (
        %%C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set PY=%%C
    )
)
if not defined PY (
    echo ERROR: Python 3.10+ not found. Install it from https://python.org first.
    pause
    exit /b 1
)
echo [1/5] Using %PY%

REM --- 2. clean venv ---
if not exist ".venv-win-build\Scripts\python.exe" (
    %PY% -m venv .venv-win-build
)
call ".venv-win-build\Scripts\activate.bat"
python -m pip install --upgrade pip -q
echo [2/5] Installing dependencies ^(onnxruntime etc., first run takes a few minutes^)...
python -m pip install -q fastapi "uvicorn[standard]" opencv-python numpy scipy onnxruntime Pillow pyinstaller

REM --- 3. sanity checks ---
if not exist "backend\main.py"        ( echo ERROR: backend\main.py missing    & goto :fail )
if not exist "backend\yolov8n.onnx"   ( echo ERROR: backend\yolov8n.onnx missing & goto :fail )
if not exist "frontend\index.html"    ( echo ERROR: frontend\index.html missing & goto :fail )
echo [3/5] Source and model checks passed

REM --- 4. pyinstaller ---
echo [4/5] Running PyInstaller...
pyinstaller dwell_monitor.spec --clean --noconfirm --distpath dist\windows --workpath work\build_windows
if errorlevel 1 goto :fail
REM place the model next to the exe so users can swap it easily
copy /y "backend\yolov8n.onnx" "dist\windows\dwell_monitor\yolov8n.onnx" >nul

REM --- 5. zip for distribution ---
echo [5/5] Creating zip...
if exist "dist\windows\dwell_monitor_windows_x64.zip" del "dist\windows\dwell_monitor_windows_x64.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\windows\dwell_monitor' -DestinationPath 'dist\windows\dwell_monitor_windows_x64.zip' -Force"

echo.
echo ====================================================================
echo  Build complete!
echo    App:  dist\windows\dwell_monitor\dwell_monitor.exe
echo    Zip:  dist\windows\dwell_monitor_windows_x64.zip
echo ====================================================================
pause
exit /b 0

:fail
echo BUILD FAILED
pause
exit /b 1
