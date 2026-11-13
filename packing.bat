@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM Build the standalone exe via PyInstaller using main.spec.
REM Output: single file dist\PtzRecorder.exe (onefile build)
REM 需要 Python 3.14 環境（venv 或 conda ptz）；ffmpeg/ffprobe/go2rtc.exe 要在專案根目錄。
REM 順序：.venv → 目前已 activate 的 conda 環境 → 系統 Python 3.14 (py launcher)
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
    python -m PyInstaller --clean --noconfirm main.spec
) else if defined CONDA_PREFIX (
    python -m PyInstaller --clean --noconfirm main.spec
) else (
    py -3.14 -m PyInstaller --clean --noconfirm main.spec
)

if %ERRORLEVEL% neq 0 (
    echo.
    echo Build failed. See messages above.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo Build OK. Single exe at: dist\PtzRecorder.exe
echo Copy that one file to the target machine.
pause
