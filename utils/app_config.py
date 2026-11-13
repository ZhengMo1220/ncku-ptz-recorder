"""相機帳密設定（不進版控）。

讀取順序（後者覆蓋前者）：
  1. config.ini 的 [camera] username / password
     搜尋位置：打包後 exe 所在資料夾 → 專案根目錄 → PyInstaller 暫存目錄(_MEIPASS)
  2. 環境變數 PTZ_CAMERA_USER / PTZ_CAMERA_PASS

config.ini 已列在 .gitignore；範本見 config.example.ini。
"""
import configparser
import os
import sys

ENV_USER = "PTZ_CAMERA_USER"
ENV_PASS = "PTZ_CAMERA_PASS"

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cache = None


def _candidate_paths():
    paths = []
    if getattr(sys, "frozen", False):
        paths.append(os.path.join(os.path.dirname(sys.executable), "config.ini"))
    paths.append(os.path.join(_PROJECT_ROOT, "config.ini"))
    if hasattr(sys, "_MEIPASS"):
        paths.append(os.path.join(sys._MEIPASS, "config.ini"))
    return paths


def get_camera_credentials():
    """回傳 (username, password)。找不到時回傳空字串並印警告。"""
    global _cache
    if _cache is not None:
        return _cache

    user = pwd = ""
    for path in _candidate_paths():
        if os.path.isfile(path):
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(path, encoding="utf-8")
            user = cp.get("camera", "username", fallback="")
            pwd = cp.get("camera", "password", fallback="")
            break

    user = os.environ.get(ENV_USER, user)
    pwd = os.environ.get(ENV_PASS, pwd)

    if not user or not pwd:
        print("[config] 找不到相機帳密！請複製 config.example.ini 為 config.ini 並填入 "
              f"[camera] username/password，或設定環境變數 {ENV_USER} / {ENV_PASS}。")
    _cache = (user, pwd)
    return _cache
