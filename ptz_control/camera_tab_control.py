import time
import datetime
from typing import Optional

from PyQt5.QtWidgets import (
    QWidget, QGraphicsScene, QGraphicsView, QMessageBox,
    QGroupBox, QVBoxLayout, QHBoxLayout, QRadioButton, QLineEdit, QPushButton,
    QButtonGroup, QFileDialog, QSizePolicy, QCheckBox,
)
from PyQt5.QtGui import QIcon, QFont
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer

from .ptz_control_ui import Ui_ptz_control
from utils.stream_thread import StreamThread, USE_GO2RTC
from utils.save_location import nas_reachable, ask_local_dir_when_nas_missing
from .ptz_control import ptzControl
from utils.icons_utils import resource_path

# health_timer：串流意外斷線時自動重連。間隔拉長到 15s（原本 5s 會讓連不上的
# 相機每 5 秒洗一次 log）；連續重連失敗這麼多次就放棄，直到使用者重新按「讀取串流」
# 或切換相機才歸零。
HEALTH_CHECK_INTERVAL_MS = 15000
MAX_RECONNECT_ATTEMPTS   = 3

class CameraConnectWorker(QThread):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, ip):
        super().__init__()
        self.ip = ip

    def run(self):
        try:
            ctrl = ptzControl(IP=self.ip)
            self.finished.emit(ctrl)
        except Exception as e:
            self.failed.emit(str(e))

class PtzTabControl(QWidget):
    def __init__(self):
        super().__init__()
        self.ui = Ui_ptz_control()
        self.ui.setupUi(self)

        self.view = self._find_first_view()
        self.scene = QGraphicsScene(self)
        if self.view:
            self.view.setScene(self.scene)
            self.scene.setSceneRect(0, 0, self.view.width(), self.view.height())

        self.streaming = False
        self.stream_thread = None
        self.ptz_control = None
        self.connect_worker = None
        self._reconnect_fails = 0   # health_timer 連續重連失敗次數；達上限就停止重試

        self.record_timer = QTimer(self) 
        self.record_timer.setInterval(1000) 
        self.record_timer.timeout.connect(self._update_record_time)
        self.record_seconds = 0

        self._setup_icons()
        self._init_camera_map()
        self._build_save_location_panel()
        self._build_keep_preview_checkbox()
        self._bind_ui()
        self._disable_control_buttons()
        if hasattr(self.ui, "recording_status_label"):
            self.ui.recording_status_label.setVisible(False)

        self.health_timer = QTimer(self)
        self.health_timer.setInterval(HEALTH_CHECK_INTERVAL_MS)
        self.health_timer.timeout.connect(self._check_stream_health)
        self.health_timer.start()

    def _find_first_view(self) -> QGraphicsView:
        views = self.findChildren(QGraphicsView)
        return views[0] if views else None

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.view:
            self.scene.setSceneRect(0, 0, self.view.width(), self.view.height())

    def _setup_icons(self):
        def set_icon(btn_attr, filename):
            if hasattr(self.ui, btn_attr):
                path = resource_path(f'./icons/{filename}')
                getattr(self.ui, btn_attr).setIcon(QIcon(path))
        set_icon("move_up_button", "up-arrow.png")
        set_icon("move_down_button", "down-arrow.png")
        set_icon("move_left_button", "left-arrow.png")
        set_icon("move_right_button", "right-arrow.png")
        set_icon("zoom_in_button", "zoom-in.png")
        set_icon("zoom_out_button", "zoom-out.png")

    def _init_camera_map(self):
        self.camera_map = {
            **{f"Rot{i:02d}": f"10.30.3.{i}" for i in range(1, 21)},
            **{f"Sti{i:02d}": f"10.30.3.{i+20}" for i in range(1, 7)},
            **{f"Ptz{i:02d}": f"10.30.3.{i+26}" for i in range(1, 9)},
        }
        if hasattr(self.ui, "camera_select_combobox"):
            self.ui.camera_select_combobox.clear()
            self.ui.camera_select_combobox.addItems(self.camera_map.keys())

    def _build_save_location_panel(self):
        """在 right_layout 的 record_groupBox 下方插入「存檔位置」面板。
        跟多相機分頁同樣三選一：NAS / 本機 / 自訂。"""
        if not hasattr(self.ui, "right_layout"):
            return

        self.path_groupBox = QGroupBox("存檔位置")
        self.path_groupBox.setFont(QFont("Arial", 11))
        self.path_groupBox.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        # 限制 groupbox 自然寬度，避免 lineedit 預設 sizeHint 把右 panel 撐出視窗
        self.path_groupBox.setMaximumWidth(320)

        path_layout = QVBoxLayout(self.path_groupBox)
        path_layout.setSpacing(6)
        path_layout.setContentsMargins(8, 10, 8, 8)

        _rb_w = 46  # 三行 radio 寬度對齊用
        _le_max = 220  # lineedit 最大寬度（避免 QLineEdit 預設 17 ex sizeHint 把 panel 撐爆）

        # NAS row
        nas_row = QHBoxLayout(); nas_row.setSpacing(6)
        self.radio_nas = QRadioButton("NAS")
        self.radio_nas.setFont(QFont("Arial", 10)); self.radio_nas.setFixedWidth(_rb_w)
        self.nas_path_edit = QLineEdit(r"Z:\PTZ recordings")
        self.nas_path_edit.setFont(QFont("Arial", 10))
        self.nas_path_edit.setMaximumWidth(_le_max)
        nas_row.addWidget(self.radio_nas); nas_row.addWidget(self.nas_path_edit)

        # 自訂 row
        custom_row = QHBoxLayout(); custom_row.setSpacing(6)
        self.radio_custom = QRadioButton("自訂")
        self.radio_custom.setFont(QFont("Arial", 10)); self.radio_custom.setFixedWidth(_rb_w)
        self.path_lineedit = QLineEdit()
        self.path_lineedit.setPlaceholderText("選擇資料夾...")
        self.path_lineedit.setReadOnly(True)
        self.path_lineedit.setStyleSheet("background: #f5f5f5;")
        self.path_lineedit.setMaximumWidth(_le_max - 66)  # 同 row 還要塞「瀏覽」62px
        btn_browse = QPushButton("瀏覽...")
        btn_browse.setFixedWidth(62)
        btn_browse.clicked.connect(self._browse_save_path)
        self.custom_path_widget = QWidget()
        _cpl = QHBoxLayout(self.custom_path_widget)
        _cpl.setContentsMargins(0, 0, 0, 0); _cpl.setSpacing(4)
        _cpl.addWidget(self.path_lineedit); _cpl.addWidget(btn_browse)
        # 永遠顯示，讓三行長度對齊；沒選自訂時 disable 互動，視覺上 grey out。
        custom_row.addWidget(self.radio_custom); custom_row.addWidget(self.custom_path_widget)

        self.radio_nas.setChecked(True)

        self.save_location_group = QButtonGroup(self)
        self.save_location_group.addButton(self.radio_nas,    0)
        self.save_location_group.addButton(self.radio_custom, 2)

        path_layout.addLayout(nas_row)
        path_layout.addLayout(custom_row)

        # 切換 radio 時，連帶把對應的路徑欄位 enable/disable，並 grey 掉沒選中的
        def _sync_path_enables():
            self.nas_path_edit.setEnabled(self.radio_nas.isChecked())
            self.custom_path_widget.setEnabled(self.radio_custom.isChecked())
        self.radio_nas.toggled.connect(_sync_path_enables)
        self.radio_custom.toggled.connect(_sync_path_enables)
        _sync_path_enables()  # 初始狀態
        self.custom_save_dir: Optional[str] = None

        # 插到 record_groupBox 之後（如果找得到），否則放在 right_layout 尾端
        inserted = False
        if hasattr(self.ui, "record_groupBox"):
            idx = self.ui.right_layout.indexOf(self.ui.record_groupBox)
            if idx >= 0:
                self.ui.right_layout.insertWidget(idx + 1, self.path_groupBox)
                inserted = True
        if not inserted:
            self.ui.right_layout.addWidget(self.path_groupBox)

    def _build_keep_preview_checkbox(self):
        """在錄影 groupBox 內補一個「錄影時保留預覽」選項（同多相機分頁策略）。
        go2rtc 模式（相機只連一次再分流）下保留預覽不影響 120fps；取消勾選則錄影
        期間暫停預覽、省下 iGPU 解碼，停錄後自動恢復。直連模式保留預覽會讓相機被連
        兩次而掉幀 → 此選項停用並強制暫停。"""
        self.chk_keep_preview = QCheckBox("錄影時保留預覽")
        self.chk_keep_preview.setFont(QFont("Arial", 10))
        self.chk_keep_preview.setChecked(USE_GO2RTC)
        if USE_GO2RTC:
            self.chk_keep_preview.setToolTip(
                "勾選：錄影時預覽持續顯示（透過 go2rtc，不影響 120fps）。\n"
                "取消：錄影時暫停預覽以省下 iGPU 解碼，停錄後自動恢復。")
        else:
            self.chk_keep_preview.setEnabled(False)
            self.chk_keep_preview.setToolTip(
                "需啟用 go2rtc 才能在錄影時保留預覽；\n"
                "直連模式下保留預覽會讓相機被連兩次而掉幀，故強制暫停。")
        # 放進錄影 groupBox 的 record_layout 第 2 列（時間/按鈕之下），跨 3 欄
        if hasattr(self.ui, "record_layout"):
            self.ui.record_layout.addWidget(self.chk_keep_preview, 2, 0, 1, 3)

    def _browse_save_path(self):
        folder = QFileDialog.getExistingDirectory(self, "選擇存檔資料夾")
        if folder:
            self.custom_save_dir = folder
            self.path_lineedit.setText(folder)

    def _bind_ui(self):
        if hasattr(self.ui, "connect_camera_button"):
            self.ui.connect_camera_button.clicked.connect(self.handle_toggle_stream)
        
        for n, fn in [
            ("move_left_button", self.handle_move_left),
            ("move_right_button", self.handle_move_right),
            ("move_up_button", self.handle_move_up),
            ("move_down_button", self.handle_move_down),
            ("zoom_in_button", self.handle_zoom_in),
            ("zoom_out_button", self.handle_zoom_out),
        ]:
            if hasattr(self.ui, n):
                getattr(self.ui, n).clicked.connect(fn)
        if hasattr(self.ui, "camera_select_combobox"):
            self.ui.camera_select_combobox.currentIndexChanged.connect(self.handle_camera_change)
        if hasattr(self.ui, "start_record_button"):
            self.ui.start_record_button.clicked.connect(self.start_recording)
        if hasattr(self.ui, "stop_record_button"):
            self.ui.stop_record_button.clicked.connect(self.stop_recording)

    def _enable_control_buttons(self):
        for name in ("move_left_button","move_right_button","move_up_button","move_down_button",
                     "zoom_in_button","zoom_out_button","start_record_button","stop_record_button"):
            if hasattr(self.ui, name):
                getattr(self.ui, name).setEnabled(True)

    def _disable_control_buttons(self):
        for name in ("move_left_button","move_right_button","move_up_button","move_down_button",
                     "zoom_in_button","zoom_out_button","start_record_button","stop_record_button"):
            if hasattr(self.ui, name):
                getattr(self.ui, name).setEnabled(False)

    def _close_stream_thread(self):
        self.stop_recording()
        t = self.stream_thread
        if t is not None:
            try: t.stop()
            finally: self.stream_thread = None
        self.ptz_control = None

    def handle_camera_change(self):
        if self.streaming:
            self._reconnect_fails = 0   # 換相機 → 重置重試計數
            self._close_stream_thread()
            self._start_connect_camera()

    def handle_toggle_stream(self):
        if not self.streaming:
            if hasattr(self.ui, "connect_camera_button"):
                self.ui.connect_camera_button.setText("連線中...")
                self.ui.connect_camera_button.setEnabled(False)
            self.streaming = True
            self._reconnect_fails = 0   # 使用者主動連線 → 重置重試計數
            self._start_connect_camera()
        else:
            self.streaming = False
            if hasattr(self.ui, "connect_camera_button"):
                self.ui.connect_camera_button.setText("讀取串流")
            self._disable_control_buttons()
            self._close_stream_thread()
            if hasattr(self.ui, "recording_status_label"):
                self.ui.recording_status_label.setVisible(False)

    def _start_connect_camera(self):
        if not self.view: return

        # 切換相機前先斷開舊 worker 的 signal，避免舊相機的 callback 在新連線建立後才回來
        # 覆蓋錯誤的串流（race condition）。舊 worker thread 繼續跑到自然結束即可。
        if self.connect_worker is not None:
            try:
                self.connect_worker.finished.disconnect()
                self.connect_worker.failed.disconnect()
            except TypeError:
                pass
            self.connect_worker = None

        try:
            name = self.ui.camera_select_combobox.currentText()
            ip = self.camera_map[name]
        except (KeyError, AttributeError):
            name, ip = "Ptz01", "10.30.3.27"

        # Rot* / Sti* 是固定鏡頭，沒有 ONVIF PTZ profile，跳過 ptzControl 直接開串流。
        # Ptz* 才走 ONVIF PTZ 連線，成功後同時提供方向 / zoom 按鈕。
        if not name.startswith("Ptz"):
            self._on_connect_success(None, ip, name)
            return

        self.connect_worker = CameraConnectWorker(ip)
        self.connect_worker.finished.connect(lambda ctrl: self._on_connect_success(ctrl, ip, name))
        self.connect_worker.failed.connect(lambda msg: self._on_connect_fail(msg, ip, name))
        self.connect_worker.start()

    def _on_connect_success(self, ptz_ctrl, ip, name):
        self._close_stream_thread()
        self.ptz_control = ptz_ctrl
        # 預覽 30fps（同多相機分頁）。單機只有一個大畫面，不像多相機需要 use_substream
        # 降到 460×258 省 iGPU；保持全解析度預覽較清晰。
        # 單機大畫面：ffmpeg 縮到 1280×720 再送 pipe（vs 原本 1920×1080 raw 186 MB/s），
        # pipe 流量降到 83 MB/s（2.2 倍改善），JPEG q70，UI 再放大到 view 尺寸。
        self.stream_thread = StreamThread(
            scene=self.scene, view=self.view, camera_ip=ip, camera_name=name,
            ui_fps=30, preview_size=(1280, 720),
        )
        self.stream_thread.recording_error.connect(
            lambda msg: QMessageBox.warning(self, "錄影錯誤", msg)
        )
        # NAS staging 複製狀態：在 record_time_label 顯示「複製到 NAS / 完成」（同多相機分頁策略）。
        # 複製失敗時由 _on_nas_copy_status 跳 QMessageBox 警告。
        self.stream_thread.nas_copy_status.connect(self._on_nas_copy_status)
        self.stream_thread.start()
        # PTZ 按鈕只有 ONVIF PTZ 連線成功才啟用；錄影按鈕不論機型都可用。
        if ptz_ctrl is not None:
            self._enable_control_buttons()
        else:
            self._enable_record_buttons_only()
        if hasattr(self.ui, "connect_camera_button"):
            self.ui.connect_camera_button.setText("停止串流")
            self.ui.connect_camera_button.setEnabled(True)

    def _enable_record_buttons_only(self):
        for name in ("move_left_button","move_right_button","move_up_button","move_down_button",
                     "zoom_in_button","zoom_out_button"):
            if hasattr(self.ui, name):
                getattr(self.ui, name).setEnabled(False)
        for name in ("start_record_button","stop_record_button"):
            if hasattr(self.ui, name):
                getattr(self.ui, name).setEnabled(True)

    def _on_connect_fail(self, error_msg, ip=None, name=None):
        print(f"PTZ Connection Failed: {error_msg}")
        # PTZ ONVIF 連線失敗時，仍以純視頻模式開啟串流（錄影按鈕可用、方向鍵停用）。
        # 與 Rot*/Sti* 固定鏡頭的處理方式相同，讓使用者至少能看到畫面並錄影。
        if ip is None or name is None:
            try:
                name = self.ui.camera_select_combobox.currentText()
                ip = self.camera_map[name]
            except (KeyError, AttributeError):
                ip, name = "10.30.3.27", "Ptz01"
        QMessageBox.warning(self, "PTZ 連線失敗",
            f"PTZ 控制連線失敗，改以純視頻模式開啟：\n{error_msg}")
        self._on_connect_success(None, ip, name)

    def _check_stream_health(self):
        """定期檢查：若使用者仍在串流中但 StreamThread 已死（耗盡重連次數），
        自動重新連線，等同多機分頁的 health_timer 機制。連續失敗
        MAX_RECONNECT_ATTEMPTS 次就停止重試，避免離線相機一直洗 log。"""
        if not self.streaming:
            return
        if self.connect_worker is not None and self.connect_worker.isRunning():
            return  # PTZ 連線正在進行中，不要干擾
        if self.stream_thread is not None and self.stream_thread.isRunning():
            self._reconnect_fails = 0   # 串流正常 → 歸零重試計數
            return

        if self._reconnect_fails >= MAX_RECONNECT_ATTEMPTS:
            if self._reconnect_fails == MAX_RECONNECT_ATTEMPTS:
                self._reconnect_fails += 1   # 只印一次，之後安靜
                print(f"[單機] 連續重連失敗 {MAX_RECONNECT_ATTEMPTS} 次，停止重試。"
                      f"請確認相機狀態後重新按「讀取串流」。")
            return

        self._reconnect_fails += 1
        print(f"[單機] 串流已斷線，自動重新連線… "
              f"(第 {self._reconnect_fails}/{MAX_RECONNECT_ATTEMPTS} 次)")
        self._start_connect_camera()

    def handle_move_up(self):
        if self.ptz_control: self.ptz_control.move_up()
    def handle_move_down(self):
        if self.ptz_control: self.ptz_control.move_down()
    def handle_move_left(self):
        if self.ptz_control: self.ptz_control.move_left()
    def handle_move_right(self):
        if self.ptz_control: self.ptz_control.move_right()
    def handle_zoom_in(self):
        if self.ptz_control: self.ptz_control.zoom_in()
    def handle_zoom_out(self):
        if self.ptz_control: self.ptz_control.zoom_out()

    # --- 錄影與計時邏輯 ---
    def start_recording(self):
        if not self.stream_thread:
            return

        # 從存檔位置面板讀設定（同多相機分頁邏輯）
        nas_dir   = self.nas_path_edit.text().strip()   or r"Z:\PTZ recordings"
        if self.radio_custom.isChecked():
            if not self.custom_save_dir:
                QMessageBox.warning(self, "提示", "請先選擇自訂存檔資料夾！")
                return
            save_to_nas = False
            custom_dir  = self.custom_save_dir
        else:   # 只剩 NAS / 自訂兩種模式，預設 NAS
            save_to_nas = True
            custom_dir  = None
            # NAS 根目錄（如 Z:\）不存在 → 先問改存哪裡，避免錄完才每台各跳一次「NAS 複製失敗」
            if not nas_reachable(nas_dir):
                local_dir = ask_local_dir_when_nas_missing(self, nas_dir)
                if not local_dir:
                    return
                self.custom_save_dir = local_dir
                self.path_lineedit.setText(local_dir)
                self.radio_custom.setChecked(True)
                save_to_nas = False
                custom_dir  = local_dir

        # 3 秒後正式啟動：2s 給 ffmpeg 預熱（RTSP handshake + keyframe），1s 給 worker 就緒。
        # worker 在 start_at - PREAMBLE_SECS 起 ffmpeg 並錄一段 preamble，避免冷啟動的開頭缺幀。
        start_at     = time.time() + 3.2
        session_name = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # 錄影時是否保留預覽：只有 go2rtc 模式 + 使用者勾選才保留；否則錄影期間暫停預覽。
        # 直連模式下保留預覽會讓相機被連兩次而掉幀（1080p120 QSV decode 跟錄影 RTSP 收包
        # 競爭 iGPU），故暫停，停錄後 resume_preview 自動回來。
        keep_preview = USE_GO2RTC and self.chk_keep_preview.isChecked()
        if not keep_preview:
            self.stream_thread.pause_preview()
        self.stream_thread.start_recording(
            target_fps=120, frame_size=(1920, 1080), fourcc="avc1",
            save_to_nas=save_to_nas,
            custom_dir=custom_dir,
            nas_dir=nas_dir,
            start_at=start_at,
            session_name=session_name,
            record_stream_path="stream0",
        )
        if hasattr(self.ui, "recording_status_label"):
            self.ui.recording_status_label.setVisible(True)
        # 錄影中鎖住「保留預覽」選項（只在起錄當下讀取，中途切換不生效）
        self.chk_keep_preview.setEnabled(False)

        self.record_seconds = 0
        if hasattr(self.ui, "record_time_label"):
            self.ui.record_time_label.setText("00:00")
        self.record_timer.start()

    def stop_recording(self):
        if self.stream_thread:
            self.stream_thread.stop_recording()
            # 錄影結束 → preview 自動回來（resume_preview 對沒被 pause 的 thread 是 no-op）
            self.stream_thread.resume_preview()
            if hasattr(self.ui, "recording_status_label"):
                self.ui.recording_status_label.setVisible(False)

        # 停錄後恢復「保留預覽」選項（直連模式下仍維持停用）
        if hasattr(self, "chk_keep_preview"):
            self.chk_keep_preview.setEnabled(USE_GO2RTC)
        self.record_timer.stop()

    def _on_nas_copy_status(self, status: str, msg: str):
        """NAS staging 背景複製狀態，顯示在 record_time_label（同多相機分頁策略）。
        status ∈ {"started", "done", "error"}；單機只有一支檔，不需累計進度。"""
        print(f"[單機] NAS 複製狀態 [{status}] {msg}")
        if not hasattr(self.ui, "record_time_label"):
            return
        if status == "started":
            self.ui.record_time_label.setText("複製到 NAS...")
        elif status == "done":
            self.ui.record_time_label.setText("NAS 複製完成")
            QTimer.singleShot(3000, self._clear_status_if_idle)
        elif status == "error":
            self.ui.record_time_label.setText("NAS 複製失敗（已存本機）")
            QTimer.singleShot(4000, self._clear_status_if_idle)
            QMessageBox.warning(self, "NAS 複製失敗", msg)

    def _clear_status_if_idle(self):
        if not self.record_timer.isActive() and hasattr(self.ui, "record_time_label"):
            self.ui.record_time_label.setText("00:00")

    def _update_record_time(self):
        self.record_seconds += 1
        m, s = divmod(self.record_seconds, 60)
        h, m = divmod(m, 60)
        
        if h > 0:
            time_str = f"{h:02d}:{m:02d}:{s:02d}" 
        else:
            time_str = f"{m:02d}:{s:02d}" 
            
        if hasattr(self.ui, "record_time_label"):
            self.ui.record_time_label.setText(time_str)

    def stop_stream(self):
        self._close_stream_thread()

    def closeEvent(self, event):
        self.health_timer.stop()
        self.stop_recording()
        self.stop_stream()
        super().closeEvent(event)