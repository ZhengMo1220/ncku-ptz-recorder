@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM main_window_ui.py 由 main_window.ui 產生，可以安全重生。
pyuic5 -x main_window.ui -o main_window_ui.py --import-from=PyQt5

REM ---------------------------------------------------------------------------
REM 注意：ptz_control/ptz_control_ui.py 已改為「手動維護」，不要用 pyuic5 重生！
REM   目前的檔案是手寫的，含錄影紅點(recording_status_label)、存檔位置面板掛載點、
REM   right_layout / record_layout 等元件；用 pyuic5 從舊 .ui 重生會全部蓋掉。
REM   對應的 ptz_control.ui 已移除。
REM ---------------------------------------------------------------------------
