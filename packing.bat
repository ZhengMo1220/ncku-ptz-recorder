@echo off
REM Keep this file ASCII-only: cmd can mis-parse UTF-8 lines and run fragments.
REM Build single-file dist\PtzRecorder.exe with PyInstaller (main.spec).
REM Needs ffmpeg.exe / ffprobe.exe / go2rtc.exe in the project root.
REM Interpreter order: .venv -> currently activated conda env -> py -3.14
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
    set "PY=.venv\Scripts\python.exe"
) else if defined CONDA_PREFIX (
    set "PY=python"
) else (
    set "PY=py -3.14"
)
%PY% -m PyInstaller --clean --noconfirm main.spec

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
