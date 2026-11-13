@echo off
chcp 65001 >nul
REM 一律在 bat 所在資料夾（專案根目錄）執行，icons/wsdl/ffmpeg.exe 都靠相對路徑找。
cd /d "%~dp0"
REM 順序：.venv → 目前已 activate 的 conda 環境 → 系統 Python 3.14 (py launcher)
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
    python main.py
) else if defined CONDA_PREFIX (
    python main.py
) else (
    py -3.14 main.py
)
