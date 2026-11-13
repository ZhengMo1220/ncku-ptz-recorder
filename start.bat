@echo off
REM Keep this file ASCII-only: cmd can mis-parse UTF-8 lines and run fragments.
REM Always run from the project root: icons/, wsdl/, ffmpeg.exe are found relative to it.
REM Interpreter order: .venv -> currently activated conda env -> py -3.14
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
    set "PY=.venv\Scripts\python.exe"
) else if defined CONDA_PREFIX (
    set "PY=python"
) else (
    set "PY=py -3.14"
)
%PY% main.py
