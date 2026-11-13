@echo off
REM Keep this file ASCII-only: cmd can mis-parse UTF-8 lines and run fragments.
REM Create .venv (Python 3.14) and install requirements.
REM Prefers uv (fast, downloads Python 3.14 itself); falls back to py launcher + pip.
REM Install uv: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
cd /d "%~dp0"

where uv >nul 2>nul
if %ERRORLEVEL% equ 0 (
    uv venv --python 3.14 .venv
    if errorlevel 1 goto :fail
    uv pip install --python .venv\Scripts\python.exe -r requirements.txt
    if errorlevel 1 goto :fail
) else (
    py -3.14 -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Neither uv nor Python 3.14 found. Install uv - recommended - or Python 3.14 from python.org.
        goto :fail
    )
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
)

if not exist config.ini (
    copy config.example.ini config.ini >nul
    echo Created config.ini - open it and fill in the camera username/password.
)

echo.
echo Done. Run start.bat, or press F5 in VS Code.
echo Remember to put ffmpeg.exe, ffprobe.exe and go2rtc.exe in the project root - see README.
pause
exit /b 0

:fail
echo.
echo Install failed. See messages above.
pause
exit /b 1
