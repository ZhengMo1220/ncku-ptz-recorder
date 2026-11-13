"""錄影前檢查「存檔位置」：NAS 磁碟（如 Z:\\）不存在時，先問使用者改存哪裡。

不檢查的話，沒掛 NAS 的電腦會照樣錄完，停錄後每台相機各跳一次「NAS 複製失敗」。
"""
import os
from pathlib import PureWindowsPath
from typing import Optional

from PyQt5.QtWidgets import QFileDialog, QMessageBox

DESKTOP_RECORDINGS_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "PTZ recordings")


def nas_reachable(nas_dir: str) -> bool:
    """NAS 路徑的根（磁碟機 Z:\\ 或 UNC \\\\server\\share\\）是否存在。
    只看根目錄：子資料夾不存在沒關係，錄影時會自動建立。"""
    anchor = PureWindowsPath(nas_dir).anchor
    if not anchor:          # 相對路徑之類，交給後續 fallback 處理
        return True
    try:
        return os.path.isdir(anchor)
    except OSError:
        return False


def ask_local_dir_when_nas_missing(parent, nas_dir: str) -> Optional[str]:
    """NAS 連不到時跳一次對話框。回傳要改存的本機資料夾；使用者取消則回傳 None。"""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("NAS 無法存取")
    box.setText(f"找不到 NAS 存檔位置：\n{nas_dir}\n\n"
                f"這台電腦可能沒有掛載 {PureWindowsPath(nas_dir).anchor or nas_dir}。要改存到哪裡？")
    btn_desktop = box.addButton("改存桌面", QMessageBox.AcceptRole)
    btn_browse  = box.addButton("選擇資料夾…", QMessageBox.ActionRole)
    box.addButton("取消錄影", QMessageBox.RejectRole)
    box.setDefaultButton(btn_desktop)
    box.exec_()

    clicked = box.clickedButton()
    if clicked is btn_desktop:
        os.makedirs(DESKTOP_RECORDINGS_DIR, exist_ok=True)
        return DESKTOP_RECORDINGS_DIR
    if clicked is btn_browse:
        folder = QFileDialog.getExistingDirectory(parent, "選擇存檔資料夾")
        return folder or None
    return None
