@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM 建立 venv 並安裝依賴。需要系統 Python 3.14（C:\Python314）。
REM 若沒裝過 py launcher，把下面的 "py -3.14" 換成 "C:\Python314\python.exe"。

py -3.14 -m venv .venv
if %ERRORLEVEL% neq 0 (
    echo 找不到 Python 3.14，請先安裝 https://www.python.org/downloads/
    exit /b %ERRORLEVEL%
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if not exist config.ini (
    copy config.example.ini config.ini >nul
    echo 已建立 config.ini，請打開它填入相機帳密。
)

echo.
echo 完成。之後用 start.bat 啟動（或： .venv\Scripts\activate ^&^& python main.py）。
echo 記得把 ffmpeg.exe / ffprobe.exe / go2rtc.exe 放到專案根目錄（見 requirements.txt 說明）。
pause
