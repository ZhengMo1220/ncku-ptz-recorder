import sys
import os
import multiprocessing as mp


def _run():
    from PyQt5.QtWidgets import QApplication, QMainWindow
    from PyQt5.QtCore import QTimer, Qt
    from PyQt5.QtGui import QIcon

    from main_window_ui import Ui_MainWindow
    from ptz_control.camera_tab_control import PtzTabControl
    from ptz_control.multi_camera_tab import MultiCameraTab
    from playback_tab import PlaybackTab
    from utils.icons_utils import resource_path
    from utils.stream_thread import maybe_start_go2rtc, stop_go2rtc

    class Main(QMainWindow):
        def __init__(self):
            super().__init__()
            # 若 USE_GO2RTC=True：先啟動本機 go2rtc restream，再連相機（相機只被連一次）。
            # USE_GO2RTC=False 時 maybe_start_go2rtc() 直接回 None，等於沒這段。
            self._go2rtc_proc = maybe_start_go2rtc()
            if self._go2rtc_proc is not None:
                import time as _t; _t.sleep(1.0)   # 等 go2rtc 綁好 RTSP port 再連
            self.ui = Ui_MainWindow()
            self.ui.setupUi(self)
            # 選單列 / 狀態列都沒在用，藏起來把高度（約 40px）讓給畫面，小筆電比較塞得下
            self.ui.menubar.hide()
            self.ui.statusbar.hide()
            self.init_tabs()
            self.set_window_icon()

        def init_tabs(self):
            ptz_ips   = ["10.30.3.27", "10.30.3.28", "10.30.3.29", "10.30.3.30",
                         "10.30.3.31", "10.30.3.32", "10.30.3.33", "10.30.3.34"]
            ptz_names = [f"Ptz{i:02d}" for i in range(1, 9)]

            self.ptz_tab = PtzTabControl()
            self.ui.Two_d_Tab.addTab(self.ptz_tab, "單機攝影機")

            self.main_tab = MultiCameraTab(camera_ips=ptz_ips, camera_names=ptz_names)
            multi_idx = self.ui.Two_d_Tab.addTab(self.main_tab, "多機攝影機")

            self.playback_tab = PlaybackTab(camera_names=ptz_names)
            self.ui.Two_d_Tab.addTab(self.playback_tab, "同步播放")

            # 錄完不自動載入到同步播放分頁；使用者要看回放自己進 playback 按「選擇影片資料夾…」
            # 多機分頁點格子 → 跳到單機分頁並連線那台（可直接操作 PTZ）
            self.main_tab.cameraClicked.connect(self.open_camera_tab)
            # 啟動預設停在「多隻攝影機」（使用者關心的是多攝預覽，不是單機 PTZ 操作）
            self.ui.Two_d_Tab.setCurrentIndex(multi_idx)

            # 只在 playback 真的在「載入 + 等第一幀」期間暫停 live preview，避免那段
            # 8 路 ffmpeg 同時 QSV decoder init 撞爆 iGPU → CPU 軟解 fallback → 100%。
            # 載入完成、seek 完成、clear 之後立即恢復 live preview。
            self.playback_tab.previewsShouldPause.connect(self.main_tab.pause_all_previews)
            self.playback_tab.previewsShouldResume.connect(self.main_tab.resume_all_previews)

        def open_camera_tab(self, src, name):
            self.ui.Two_d_Tab.setCurrentWidget(self.ptz_tab)
            self.ptz_tab.show_camera(name)

        def set_window_icon(self):
            self.setWindowIcon(QIcon(resource_path('./icons/video-camera.png')))

        def closeEvent(self, event):
            if hasattr(self.ptz_tab, "stop_stream"):
                self.ptz_tab.stop_stream()
            if hasattr(self.main_tab, "close"):
                self.main_tab.close()
            if hasattr(self.playback_tab, "closeEvent"):
                self.playback_tab.closeEvent(None)
            stop_go2rtc(getattr(self, "_go2rtc_proc", None))
            super().closeEvent(event)

    app = QApplication(sys.argv)
    _warn_if_credentials_unset()
    window = Main()
    # cell 跟視窗寬高等比縮放（utils.ui_utils.fit_grid_cell_size），視窗最小可以很小。
    # 原本 1200×700 比小筆電的可用區域還大（1366×768 開 125% 縮放 ≈ 1093×614），
    # 視窗會超出螢幕、內容被擠壓 → 改成 960×540。
    window.setMinimumSize(960, 540)
    window.showMaximized()

    # 降低 Python GC 週期性 stop-the-world 造成的預覽抖動：8 路每秒配置數百個
    # QImage/QPixmap → cyclic GC 每隔幾秒做一次 full collection、暫停所有執行緒
    # ~60-90ms（= 順幾秒卡一下）。對策（業界常見、已驗證）：凍結啟動物件 +
    # 大幅拉高觸發門檻，讓 full GC 變很罕見。refcount 仍即時回收，不會漏記憶體。
    import gc
    gc.collect()
    gc.freeze()
    gc.set_threshold(50000, 500, 500)   # 預設 (700, 10, 10) → full GC 罕見很多

    sys.exit(app.exec_())


def _setup_utf8_console():
    """讓終端機的中文 log 不變亂碼。

    Windows 終端機預設字碼頁是 cp950（Big5），但 VS Code / 重導向 / 子行程
    各自用不同編碼輸出 → 中文變亂碼。統一成 UTF-8：
      1. 主控台字碼頁設 65001（UTF-8），程式結束時還原原本的字碼頁。
      2. 本行程 stdout/stderr 改用 UTF-8。
      3. PYTHONIOENCODING 寫進環境變數，multiprocessing spawn 出來的相機
         worker 行程會繼承，print 也是 UTF-8。
    """
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if sys.platform == "win32":
        try:
            import ctypes, atexit
            k32 = ctypes.windll.kernel32
            old_cp = k32.GetConsoleOutputCP()
            if old_cp and old_cp != 65001 and k32.SetConsoleOutputCP(65001):
                atexit.register(k32.SetConsoleOutputCP, old_cp)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass   # 打包成 --noconsole 時 stream 可能是 None


def _warn_if_credentials_unset():
    """config.ini 不進版控，新 clone 的電腦常忘了填帳密。這時只有不需帳密的
    Ptz02 / Ptz06 有畫面、其他全部「連線失敗」，很容易誤以為是網路問題。
    啟動時先檢查，帳密是空的或還是範本值就直接跳視窗講清楚。"""
    from PyQt5.QtWidgets import QMessageBox
    from utils.app_config import get_camera_credentials
    from utils.camera_check import credentials_look_unset
    user, pwd = get_camera_credentials()
    if credentials_look_unset(user, pwd):
        print("[config] ⚠ 相機帳密未設定（config.ini 不存在或仍是範本值）")
        QMessageBox.warning(
            None, "相機帳密未設定",
            "config.ini 不存在，或帳密仍是範本值（your_camera_user / your_camera_password）。\n\n"
            "沒有正確帳密時，只有 Ptz02、Ptz06 會有畫面，其他相機都會連線失敗。\n\n"
            "請在程式資料夾把 config.example.ini 複製成 config.ini，填入相機帳密後重新啟動。")


def _chdir_to_app_dir():
    """icons/、wsdl/、ffmpeg.exe、go2rtc.exe 都是相對工作目錄找的。
    從別的資料夾啟動（例如 python C:\\...\\main.py）會全部找不到 → 相機全連不上。
    開發模式下一律切到 main.py 所在資料夾；打包版資源在 _MEIPASS，不受影響。"""
    if not getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    _chdir_to_app_dir()
    _setup_utf8_console()
    os.environ.setdefault("OPENCV_VIDEOIO_TIMEOUT_MSEC", "5000")
    mp.freeze_support()
    _run()
