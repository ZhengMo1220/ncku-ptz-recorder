@echo off
REM Keep this file ASCII-only: cmd can mis-parse UTF-8 lines and run fragments.
REM Regenerates main_window_ui.py from main_window.ui (safe to regenerate).
REM
REM WARNING: do NOT regenerate ptz_control/ptz_control_ui.py with pyuic5.
REM   It is hand-maintained now (recording dot, save-location panel, right_layout,
REM   record_layout ...). The old ptz_control.ui was removed. See README section 8.
cd /d "%~dp0"
if exist .venv\Scripts\pyuic5.exe (
    .venv\Scripts\pyuic5.exe -x main_window.ui -o main_window_ui.py --import-from=PyQt5
) else (
    pyuic5 -x main_window.ui -o main_window_ui.py --import-from=PyQt5
)
