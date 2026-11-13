import sys
import os

def resource_path(relative_path):
    """ Get the absolute path to the resource, works for development, onefile, and internal paths """
    # 如果程式是打包後運行，使用 _MEIPASS 或其他內部路徑
    if hasattr(sys, '_MEIPASS'):
        base_path = sys._MEIPASS
    else:
        # 如果是開發模式或多文件模式，使用當前目錄
        base_path = os.path.abspath(".")

    # 嘗試從內部資料夾或臨時資料夾中尋找資源
    internal_path = os.path.join(base_path, '_internal', relative_path)
    if os.path.exists(internal_path):
        return internal_path
    else:
        return os.path.join(base_path, relative_path)