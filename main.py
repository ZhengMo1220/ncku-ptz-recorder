import sys
import os
import multiprocessing as mp


def _run():
    from PyQt5.QtWidgets import QApplication, QMainWindow, QTabBar
    from PyQt5.QtCore import QTimer, Qt
    from PyQt5.QtGui import QIcon

    from main_window_ui import Ui_MainWindow
    from ptz_control.camera_tab_control import PtzTabControl
    from ptz_control.multi_camera_tab import MultiCameraTab
    from playback_tab import PlaybackTab
    from enlarge_tab import EnlargeTab
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
            self.init_tabs()
            self.set_window_icon()

        def init_tabs(self):
            ptz_ips   = ["10.30.3.27", "10.30.3.28", "10.30.3.29", "10.30.3.30",
                         "10.30.3.31", "10.30.3.32", "10.30.3.33", "10.30.3.34"]
            ptz_names = [f"Ptz{i:02d}" for i in range(1, 9)]

            self.ptz_tab = PtzTabControl()
            ptz_idx = self.ui.Two_d_Tab.addTab(self.ptz_tab, "單機攝影機")

            self.main_tab = MultiCameraTab(camera_ips=ptz_ips, camera_names=ptz_names)
            multi_idx = self.ui.Two_d_Tab.addTab(self.main_tab, "多機攝影機")

            self.playback_tab = PlaybackTab(camera_names=ptz_names)
            playback_idx = self.ui.Two_d_Tab.addTab(self.playback_tab, "同步播放")

            # 錄完不自動載入到同步播放分頁；使用者要看回放自己進 playback 按「選擇影片資料夾…」
            self.main_tab.cameraClicked.connect(self.open_camera_tab)

            self.ui.Two_d_Tab.setTabsClosable(True)
            self.ui.Two_d_Tab.tabCloseRequested.connect(self.handle_close_tab)
            # 3 個主分頁的 X 拿掉（保留 setTabsClosable=True 是為了之後 enlarge 分頁能關）
            for idx in (ptz_idx, multi_idx, playback_idx):
                self.ui.Two_d_Tab.tabBar().setTabButton(idx, QTabBar.RightSide, None)
                self.ui.Two_d_Tab.tabBar().setTabButton(idx, QTabBar.LeftSide, None)
            # 啟動預設停在「多隻攝影機」（使用者關心的是多攝預覽，不是單機 PTZ 操作）
            self.ui.Two_d_Tab.setCurrentIndex(multi_idx)

            # 只在 playback 真的在「載入 + 等第一幀」期間暫停 live preview，避免那段
            # 8 路 ffmpeg 同時 QSV decoder init 撞爆 iGPU → CPU 軟解 fallback → 100%。
            # 載入完成、seek 完成、clear 之後立即恢復 live preview。
            self.playback_tab.previewsShouldPause.connect(self.main_tab.pause_all_previews)
            self.playback_tab.previewsShouldResume.connect(self.main_tab.resume_all_previews)

        def open_camera_tab(self, src, name):
            if hasattr(self.main_tab, "pause_camera_by_ip"):
                self.main_tab.pause_camera_by_ip(src)
            tab = EnlargeTab(src, title=name, parent=self)
            tab.src_ip = src
            idx = self.ui.Two_d_Tab.addTab(tab, name)
            self.ui.Two_d_Tab.setCurrentIndex(idx)

        def handle_close_tab(self, index):
            w = self.ui.Two_d_Tab.widget(index)
            if w in (self.ptz_tab, self.main_tab, self.playback_tab):
                return
            target_ip = getattr(w, "src_ip", getattr(w, "src", None))
            if hasattr(w, "stop_stream"):
                try: w.stop_stream()
                except Exception: pass
            self.ui.Two_d_Tab.removeTab(index)
            if target_ip and hasattr(self.main_tab, "resume_camera_by_ip"):
                self.main_tab.resume_camera_by_ip(target_ip)

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
    window = Main()
    # cell 已改為跟視窗等比縮放（multi_camera_tab / playback_tab 的 resizeEvent），
    # 所以視窗最小設小一點，讓使用者真的能拖小視窗。1200×700 是 cell 280×157 的下限。
    window.setMinimumSize(1200, 700)
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


if __name__ == "__main__":
    os.environ.setdefault("OPENCV_VIDEOIO_TIMEOUT_MSEC", "5000")
    mp.freeze_support()
    _run()
