import time
import os
import datetime
from typing import Optional

from PyQt5.QtWidgets import (
    QWidget, QGridLayout, QGraphicsView, QGraphicsScene, QLabel, QFrame,
    QVBoxLayout, QPushButton, QHBoxLayout, QMessageBox, QCheckBox,
    QSizePolicy, QGroupBox, QLineEdit, QFileDialog, QRadioButton, QButtonGroup
)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QFont
from utils.stream_thread import StreamThread, cleanup_staging_dir, USE_GO2RTC
from utils.save_location import nas_reachable, ask_local_dir_when_nas_missing
from utils.onvif_sync import KeyframeSyncer, SYNC_FIRE_BEFORE_START

# health_timer：偵測 worker 死掉並自動重連。間隔拉長到 15s（原本 5s 會讓真的
# 離線的相機每 5 秒洗一次「無法連線」log）；同一支連續重連失敗這麼多次就放棄，
# 不再重試，直到使用者重新勾選「連線相機」才歸零。
HEALTH_CHECK_INTERVAL_MS = 15000
MAX_RECONNECT_ATTEMPTS   = 3


class ClickableGraphicsView(QGraphicsView):
    clicked = pyqtSignal()
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class MultiCameraTab(QWidget):
    cameraClicked = pyqtSignal(str, str)

    def __init__(self, camera_ips=None, camera_names=None):
        super().__init__()
        self.layout = QVBoxLayout(self)

        # ── 上方控制列 ─────────────────────────────────────────────────
        # 左：錄影控制
        self.record_groupBox = QGroupBox("錄影")
        self.record_groupBox.setFont(QFont("Arial", 12))
        self.record_groupBox.setFixedWidth(280)
        self.record_groupBox.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self.record_layout = QGridLayout(self.record_groupBox)
        self.record_layout.setSpacing(5)

        self.record_time_label = QLabel("00:00")
        self.record_time_label.setFont(QFont("Arial", 12))
        self.record_time_label.setStyleSheet("color: black; margin-left: 2px;")
        sp = self.record_time_label.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self.record_time_label.setSizePolicy(sp)

        self.btn_start_all = QPushButton("開始選取錄影")
        self.btn_start_all.setMinimumHeight(30)
        self.btn_stop_all = QPushButton("停止所有錄影")
        self.btn_stop_all.setMinimumHeight(30)
        self.btn_stop_all.setEnabled(False)

        # 錄影時是否保留預覽。go2rtc 模式（相機只連一次再分流）下保留預覽不影響 120fps；
        # 取消勾選則錄影期間暫停預覽、省下 iGPU 解碼，停錄後自動恢復。
        # 直連模式（USE_GO2RTC=False）保留預覽會讓相機被連兩次而掉幀 → 此選項停用並強制暫停。
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

        self.record_layout.addWidget(self.record_time_label, 0, 0, 1, 2)
        self.record_layout.addWidget(self.btn_start_all,     1, 0)
        self.record_layout.addWidget(self.btn_stop_all,      1, 1)
        self.record_layout.addWidget(self.chk_keep_preview,  2, 0, 1, 2)

        # 右：存檔位置選擇
        self.path_groupBox = QGroupBox("存檔位置")
        self.path_groupBox.setFont(QFont("Arial", 12))
        self.path_groupBox.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        path_layout = QVBoxLayout(self.path_groupBox)
        path_layout.setSpacing(6)
        path_layout.setContentsMargins(8, 10, 8, 8)

        _rb_w = 46  # radio button 固定寬度，讓三行路徑欄對齊

        # NAS row
        _nas_row = QHBoxLayout()
        _nas_row.setSpacing(6)
        self.radio_nas = QRadioButton("NAS")
        self.radio_nas.setFont(QFont("Arial", 10))
        self.radio_nas.setFixedWidth(_rb_w)
        self.nas_path_edit = QLineEdit(r"Z:\PTZ recordings")
        self.nas_path_edit.setFont(QFont("Arial", 10))
        self.nas_path_edit.setMinimumWidth(220)
        _nas_row.addWidget(self.radio_nas)
        _nas_row.addWidget(self.nas_path_edit)

        # 自訂 row
        _custom_row = QHBoxLayout()
        _custom_row.setSpacing(6)
        self.radio_custom = QRadioButton("自訂")
        self.radio_custom.setFont(QFont("Arial", 10))
        self.radio_custom.setFixedWidth(_rb_w)
        self.path_lineedit = QLineEdit()
        self.path_lineedit.setPlaceholderText("選擇資料夾...")
        self.path_lineedit.setMinimumWidth(160)
        self.path_lineedit.setReadOnly(True)
        self.path_lineedit.setStyleSheet("background: #f5f5f5;")
        btn_browse = QPushButton("瀏覽...")
        btn_browse.setFixedWidth(62)
        btn_browse.clicked.connect(self._browse_save_path)
        self.custom_path_widget = QWidget()
        _cpl = QHBoxLayout(self.custom_path_widget)
        _cpl.setContentsMargins(0, 0, 0, 0)
        _cpl.setSpacing(4)
        _cpl.addWidget(self.path_lineedit)
        _cpl.addWidget(btn_browse)
        # 永遠顯示，讓三行長度對齊；沒選自訂時 disable 互動，視覺上 grey out。
        _custom_row.addWidget(self.radio_custom)
        _custom_row.addWidget(self.custom_path_widget)

        self.radio_nas.setChecked(True)

        self.save_location_group = QButtonGroup(self)
        self.save_location_group.addButton(self.radio_nas,    0)
        self.save_location_group.addButton(self.radio_custom, 2)

        path_layout.addLayout(_nas_row)
        path_layout.addLayout(_custom_row)

        # 切換 radio 時，連帶把對應的路徑欄位 enable/disable，並 grey 掉沒選中的
        def _sync_path_enables():
            self.nas_path_edit.setEnabled(self.radio_nas.isChecked())
            self.custom_path_widget.setEnabled(self.radio_custom.isChecked())
        self.radio_nas.toggled.connect(_sync_path_enables)
        self.radio_custom.toggled.connect(_sync_path_enables)
        _sync_path_enables()  # 初始狀態
        self.custom_save_dir: Optional[str] = None

        # 中：選擇要連線的相機（提供使用者壓力測試這台筆電的承載上限）
        self.connect_groupBox = QGroupBox("連線相機")
        self.connect_groupBox.setFont(QFont("Arial", 12))
        self.connect_groupBox.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.connect_grid = QGridLayout(self.connect_groupBox)
        self.connect_grid.setSpacing(4)
        self.connect_grid.setContentsMargins(8, 10, 8, 8)
        # checkbox 等 _build_grid 時跟 camera_names 一起建立

        # 組合上方列
        self.top_hbox = QHBoxLayout()
        self.top_hbox.addWidget(self.record_groupBox)
        self.top_hbox.addStretch(1)
        self.top_hbox.addWidget(self.connect_groupBox)
        self.top_hbox.addWidget(self.path_groupBox)

        self.layout.addLayout(self.top_hbox)
        self.layout.addStretch(1)

        # ── 攝影機格線 ─────────────────────────────────────────────────
        self.grid_container = QHBoxLayout()
        self.grid_container.addStretch(1)

        self.grid = QGridLayout()
        self.grid.setContentsMargins(10, 20, 10, 20)
        self.grid.setHorizontalSpacing(20)  # 4×460 + 3×20 + 20 (margins) = 1920，剛好貼齊 FHD
        self.grid.setVerticalSpacing(24)
        self.grid.setAlignment(Qt.AlignCenter)

        self.grid_container.addLayout(self.grid)
        self.grid_container.addStretch(1)

        self.layout.addLayout(self.grid_container)
        self.layout.addStretch(1)

        # ── 視窗最左下：全選錄影 ───────────────────────────────────────
        self.bottom_hbox = QHBoxLayout()
        self.bottom_hbox.setContentsMargins(10, 8, 10, 10)
        self.btn_select_all = QPushButton("全選錄影")
        self.btn_select_all.setFont(QFont("Arial", 11))
        self.btn_select_all.setMinimumHeight(32)
        self.btn_select_all.setFixedWidth(140)
        self.bottom_hbox.addWidget(self.btn_select_all, alignment=Qt.AlignLeft)
        self.bottom_hbox.addStretch(1)
        self.layout.addLayout(self.bottom_hbox)

        # ── 成員初始化 ─────────────────────────────────────────────────
        self.scenes, self.views, self.labels, self.threads = [], [], [], []
        self.checkboxes        = []   # 錄影選取
        self.connect_checkboxes = []  # 是否連線預覽（用來測試承載上限）
        self.pending_cameras = []
        # 每支相機被 health_timer 重連的失敗次數；達 MAX_RECONNECT_ATTEMPTS 就停止重試
        self._reconnect_fails = []

        self.record_timer = QTimer(self)
        self.record_timer.setInterval(1000)
        self.record_timer.timeout.connect(self._update_record_time)
        self.record_seconds = 0

        if camera_ips is None:
            camera_ips = ["10.30.3.27", "10.30.3.28", "10.30.3.29", "10.30.3.30",
                          "10.30.3.31", "10.30.3.32", "10.30.3.33", "10.30.3.34"]
        self.camera_sources = camera_ips[:]
        n = len(self.camera_sources)
        if camera_names is None:
            camera_names = [f"Ptz{i:02d}" for i in range(1, n + 1)]
        self.camera_names = camera_names[:]
        self._reconnect_fails = [0] * n

        # ── 建 2×4 畫面 ────────────────────────────────────────────────
        rows, cols = 2, 4
        for i in range(rows):
            for j in range(cols):
                idx = i * cols + j

                scene = QGraphicsScene(self)
                view = ClickableGraphicsView(self)
                view.setScene(scene)
                # 460×258：在 1920 FHD 上 4 cols + spacing 24 + margin 10 共 1928，
                # 受 QHBoxLayout stretch 包覆會自動居中、最多左右各裁 14px，可接受。
                # QHD/4K 螢幕上有更多餘裕。
                view.setFixedSize(460, 258)
                view.setStyleSheet("background-color: black; border: 1px solid #666;")
                view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
                view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
                view.setFrameShape(QFrame.NoFrame)
                view.setInteractive(False)
                scene.setSceneRect(0, 0, view.width(), view.height())

                control_container = QWidget()
                hbox = QHBoxLayout(control_container)
                hbox.setContentsMargins(0, 5, 0, 0)

                label = QLabel(self)
                label.setFont(QFont("Segoe UI", 12, QFont.Bold))
                if idx < n:
                    _txt = self.camera_names[idx]
                    if idx == 0:   # Ptz01：名字後接 IP，同字級同色 → "Ptz01 (IP:10.30.3.27)"
                        _txt += f" (IP:{self.camera_sources[idx]})"
                    label.setText(_txt)
                else:
                    label.setText("No Camera")
                # Ptz05 (idx 4)、Ptz06 (idx 5) 名字加刪除線，標示停用
                if idx in (4, 5):
                    _f = label.font()
                    _f.setStrikeOut(True)
                    label.setFont(_f)

                chk = QCheckBox("錄影")
                chk.setFont(QFont("Segoe UI", 11))
                chk.setChecked(False)
                if idx >= n: chk.setEnabled(False)

                hbox.addWidget(label)
                hbox.addStretch(1)
                hbox.addWidget(chk)

                cell = QWidget(self)
                vbox = QVBoxLayout(cell)
                vbox.setContentsMargins(0, 0, 0, 0)
                vbox.setSpacing(0)
                vbox.addWidget(view)
                vbox.addWidget(control_container)

                self.grid.addWidget(cell, i, j, alignment=Qt.AlignCenter)

                self.scenes.append(scene)
                self.views.append(view)
                self.labels.append(label)
                self.checkboxes.append(chk)
                self.threads.append(None)

                if idx < n:
                    view.clicked.connect(
                        lambda _=None, k=idx:
                        self.cameraClicked.emit(self.camera_sources[k], self.camera_names[k])
                    )
                    self.pending_cameras.append(idx)

        # 連線 checkbox：2×4 grid，跟相機格線同形狀好對位
        for idx, _name in enumerate(self.camera_names):
            ck = QCheckBox(_name)
            ck.setFont(QFont("Arial", 10))
            ck.setChecked(True)
            ck.toggled.connect(lambda checked, k=idx: self._on_connect_toggle(k, checked))
            self.connect_grid.addWidget(ck, idx // cols, idx % cols)
            self.connect_checkboxes.append(ck)

        self.btn_start_all.clicked.connect(self._start_selected_recording)
        self.btn_stop_all.clicked.connect(self._stop_all_recording)
        self.btn_select_all.clicked.connect(self._toggle_select_all)
        for chk in self.checkboxes:
            chk.stateChanged.connect(self._refresh_select_all_text)

        self.startup_timer = QTimer(self)
        self.startup_timer.timeout.connect(self._start_next_camera)
        self.startup_timer.start(300)

        self.health_timer = QTimer(self)
        self.health_timer.setInterval(HEALTH_CHECK_INTERVAL_MS)
        self.health_timer.timeout.connect(self._restart_dead_threads)
        self.health_timer.start()

        # NAS staging 複製進度追蹤（用於停錄後在 record_time_label 顯示「複製 X/N」）
        # 流程：_stop_all_recording → 1500ms 後 _after_stop snapshot expected count →
        # _on_nas_copy_status 累積進度 → 全部完成 → _on_all_nas_copies_done emit paths。
        self.nas_copy_started = 0    # worker 送 nas_copy_started 累計
        self.nas_copy_done    = 0
        self.nas_copy_errors  = 0
        self._stop_confirmed  = False  # _after_stop 跑完才開始檢查完成度
        self._nas_expected    = 0      # _after_stop snapshot 當下的 started 數
        self._nas_error_msgs  = []     # 各相機 NAS 複製失敗訊息，全部完成後彙整成一個視窗

        # 啟動時清掃 staging 孤兒檔（>24h），免得歷史 crash 殘檔吃硬碟
        try:
            cleanup_staging_dir()
        except Exception as _e:
            print(f"[multi_camera_tab] staging 清理失敗（不致命）: {_e}")

        # go2rtc 模式：建 ONVIF 同步器並預熱連線。錄影起錄時用 SetSynchronizationPoint
        # 強制 8 台同時送 I-frame，對齊起錄時間（解 go2rtc 起錄分散 ~1s 的問題）。
        self.syncer = None
        if USE_GO2RTC:
            try:
                self.syncer = KeyframeSyncer(self.camera_sources, self.camera_names)
                self.syncer.prewarm()
            except Exception as _e:
                print(f"[multi_camera_tab] KeyframeSyncer 初始化失敗（不致命）: {_e}")

    # ── 全選錄影 ───────────────────────────────────────────────────────
    def _toggle_select_all(self):
        enabled = [c for c in self.checkboxes if c.isEnabled()]
        if not enabled:
            return
        check = not all(c.isChecked() for c in enabled)
        for c in enabled:
            c.blockSignals(True)
            c.setChecked(check)
            c.blockSignals(False)
        self._refresh_select_all_text()

    def _refresh_select_all_text(self, *_):
        enabled = [c for c in self.checkboxes if c.isEnabled()]
        if enabled and all(c.isChecked() for c in enabled):
            self.btn_select_all.setText("全部取消")
        else:
            self.btn_select_all.setText("全選錄影")

    # ── 路徑選擇 ───────────────────────────────────────────────────────
    def _browse_save_path(self):
        folder = QFileDialog.getExistingDirectory(self, "選擇存檔資料夾")
        if folder:
            self.custom_save_dir = folder
            self.path_lineedit.setText(folder)

    # ── 連線選擇 ───────────────────────────────────────────────────────
    def _on_connect_toggle(self, idx: int, checked: bool):
        if self.record_timer.isActive():
            # 錄影中不允許動，把 UI 還原；不要中斷錄影
            ck = self.connect_checkboxes[idx]
            ck.blockSignals(True)
            ck.setChecked(not checked)
            ck.blockSignals(False)
            QMessageBox.information(self, "提示", "錄影中無法變更連線狀態，請先停止錄影。")
            return
        if checked:
            self._reconnect_fails[idx] = 0   # 使用者重新勾選 → 重置重試計數，允許 health_timer 再接手
            self._connect_camera(idx)
            self.checkboxes[idx].setEnabled(True)
        else:
            self._disconnect_camera(idx)
            self.checkboxes[idx].setChecked(False)
            self.checkboxes[idx].setEnabled(False)

    def _connect_camera(self, idx: int):
        if self.threads[idx] is not None and self.threads[idx].isRunning():
            return
        self.scenes[idx].clear()  # 清掉「未連線」字樣
        if idx not in self.pending_cameras:
            self.pending_cameras.append(idx)
        # 走 startup_timer 統一節流，避免一次重連多支撞 RTSP handshake
        if not self.startup_timer.isActive():
            self.startup_timer.start(300)

    def _disconnect_camera(self, idx: int):
        # 還沒啟動的從 pending 移除
        if idx in self.pending_cameras:
            self.pending_cameras.remove(idx)
        th = self.threads[idx]
        if th is not None:
            # 必須等 run() 真正退出再放掉最後一個 Python ref。
            # stop() 只送 quit / join worker process；run() 的主迴圈可能還卡在
            # frame_queue.get(timeout=0.5)。若這時 GC 掉 QThread →
            # "QThread: Destroyed while thread is still running" → 整支程式 crash。
            self.threads[idx] = None
            try:
                th.stop()         # ≤2s（grace 0.5s + terminate 1s + kill 0.5s）
                th.wait(1500)     # 等 run() 確實 return
            except Exception:
                pass
        # view 顯示「未連線」，跟「連線失敗黑畫面」做出視覺區別
        self.scenes[idx].clear()
        txt = self.scenes[idx].addText("未連線")
        txt.setDefaultTextColor(Qt.gray)
        txt.setFont(QFont("Arial", 16, QFont.Bold))
        # 置中：scene 400×225，文字寬度估約 80
        txt.setPos(160, 95)

    # ── 攝影機串流 ─────────────────────────────────────────────────────
    def _start_next_camera(self):
        if not self.pending_cameras:
            self.startup_timer.stop()
            return
        idx = self.pending_cameras.pop(0)
        # 使用者在 startup queue 處理中途取消勾選 → 跳過
        if idx < len(self.connect_checkboxes) and not self.connect_checkboxes[idx].isChecked():
            return
        src  = self.camera_sources[idx]
        name = self.camera_names[idx]
        th = StreamThread(
            self.scenes[idx], self.views[idx],
            camera_ip=src, camera_name=name,
            ui_fps=30, use_substream=True,
        )
        th.recording_error.connect(lambda msg, n=name: QMessageBox.warning(self, f"錄影錯誤 [{n}]", msg))
        th.nas_copy_status.connect(lambda status, msg, n=name: self._on_nas_copy_status(n, status, msg))
        self.threads[idx] = th
        th.start()

    def _restart_dead_threads(self):
        if self.record_timer.isActive():
            return  # 錄影中不重啟，重啟後也不會繼續錄影
        for i, t in enumerate(self.threads):
            if t is None or i >= len(self.camera_sources):
                continue
            if t.isRunning():
                self._reconnect_fails[i] = 0   # 活著就當作連上了，歸零重試計數
                continue
            # 使用者主動取消連線的相機 → 不要被 health_timer 復活
            if i < len(self.connect_checkboxes) and not self.connect_checkboxes[i].isChecked():
                continue

            _name = self.camera_names[i]
            if self._reconnect_fails[i] >= MAX_RECONNECT_ATTEMPTS:
                if self._reconnect_fails[i] == MAX_RECONNECT_ATTEMPTS:
                    # 只在剛放棄的那一拍印一次 log + 標示畫面，之後安靜
                    self._reconnect_fails[i] += 1   # 讓上面的相等條件不再成立
                    print(f"[{_name}] 連續重連失敗 {MAX_RECONNECT_ATTEMPTS} 次，停止重試。"
                          f"請確認相機狀態後重新勾選「連線相機」。")
                    self._mark_reconnect_gaveup(i)
                continue

            self._reconnect_fails[i] += 1

            print(f"[{_name}] health_timer 重連中… "
                  f"(第 {self._reconnect_fails[i]}/{MAX_RECONNECT_ATTEMPTS} 次)")
            new_th = StreamThread(
                self.scenes[i], self.views[i],
                camera_ip=self.camera_sources[i],
                camera_name=_name,
                ui_fps=30, use_substream=True,
            )
            new_th.recording_error.connect(lambda msg, n=_name: QMessageBox.warning(self, f"錄影錯誤 [{n}]", msg))
            new_th.nas_copy_status.connect(lambda status, msg, n=_name: self._on_nas_copy_status(n, status, msg))
            self.threads[i] = new_th
            new_th.start()

    def _mark_reconnect_gaveup(self, idx: int):
        # 在該相機格線畫面標示「連線失敗」，跟「未連線」（使用者主動斷）做區別
        scene = self.scenes[idx]
        scene.clear()
        txt = scene.addText("連線失敗，停止重試")
        txt.setDefaultTextColor(Qt.red)
        txt.setFont(QFont("Arial", 14, QFont.Bold))
        txt.setPos(90, 95)

    # ── 錄影控制 ───────────────────────────────────────────────────────
    def _start_selected_recording(self):
        errors = []
        started_count = 0
        _sync_idx = []   # go2rtc 同步：實際起錄的相機 index，稍後並發觸發 IDR 對齊

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
        start_at     = time.time() + 3.2      # 3 秒後正式啟動：2s 給 ffmpeg 預熱（RTSP handshake + keyframe），1s 給 worker 就緒
        session_name = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # 錄影時是否保留預覽：只有 go2rtc 模式 + 使用者勾選才保留；否則錄影期間暫停預覽。
        keep_preview = USE_GO2RTC and self.chk_keep_preview.isChecked()

        for i, (t, chk) in enumerate(zip(self.threads, self.checkboxes)):
            if t and t.isRunning() and chk.isChecked():
                try:
                    # 暫停 preview ffmpeg：直連模式下 8 路 1080p120 QSV decode 會榨乾 iGPU、
                    # 且相機被連兩次，影響錄影 RTSP 收包時序，fps 從 120 掉到 117。
                    # go2rtc 模式相機只連一次，使用者可勾選「錄影時保留預覽」(keep_preview)。
                    # 暫停時預覽變黑是有意設計，停錄後 resume_preview 自動回來。
                    if not keep_preview:
                        t.pause_preview()
                    t.start_recording(
                        target_fps=120, frame_size=(1920, 1080), fourcc="avc1",
                        save_to_nas=save_to_nas,
                        custom_dir=custom_dir,
                        nas_dir=nas_dir,
                        start_at=start_at,
                        session_name=session_name,
                        record_stream_path="stream0",
                    )
                    started_count += 1
                    _sync_idx.append(i)
                except Exception as e:
                    errors.append(f"{self.camera_names[i]}: {e}")

        # go2rtc 模式：在預熱期間並發命令這幾台同時送 I-frame，對齊起錄時間
        if USE_GO2RTC and self.syncer and _sync_idx:
            self.syncer.trigger_at(_sync_idx, start_at - SYNC_FIRE_BEFORE_START)

        if started_count > 0:
            self.btn_start_all.setEnabled(False)
            self.btn_stop_all.setEnabled(True)
            # 錄影中鎖住「保留預覽」選項（只在起錄當下讀取，中途切換不生效，停用免誤會）
            self.chk_keep_preview.setEnabled(False)
            self.record_seconds = 0
            self.record_time_label.setText("00:00")
            self.record_time_label.setVisible(True)
            self.record_timer.start()
        else:
            any_checked = any(chk.isChecked() for chk in self.checkboxes)
            if any_checked:
                QMessageBox.warning(self, "警告",
                    "勾選的攝影機串流尚未就緒，請稍候再試。\n"
                    "（若開啟過放大視窗，請先關閉後再錄影）")
            else:
                QMessageBox.information(self, "提示", "請至少勾選一支相機進行錄影！")

        if errors:
            QMessageBox.warning(self, "部分錄影失敗", "\n".join(errors))

    def _stop_all_recording(self):
        for t in self.threads:
            if t:
                try: t.stop_recording()
                except Exception: pass
                # 錄影結束 → 恢復 preview。resume_preview 對沒被 pause 的 thread
                # 是 no-op（worker 端有 if preview_paused 檢查），所以可以無腦呼叫。
                try: t.resume_preview()
                except Exception: pass
        self.btn_start_all.setEnabled(True)
        self.btn_stop_all.setEnabled(False)
        # 停錄後恢復「保留預覽」選項（直連模式下仍維持停用）
        self.chk_keep_preview.setEnabled(USE_GO2RTC)
        self.record_timer.stop()
        # 不馬上清 "00:00"，因為下面可能要顯示 NAS 複製進度
        # 1500ms 給所有 worker 把 nas_copy_started 訊息送進 result_queue
        self._stop_confirmed = False
        QTimer.singleShot(1500, self._after_stop)

    def _after_stop(self):
        """停錄後 1.5s：所有 worker 的 nas_copy_started 應該都到了，可以 snapshot 預期數量。"""
        self._stop_confirmed = True
        self._nas_expected = self.nas_copy_started
        if self._nas_expected > 0:
            self._update_nas_progress_label()
            # 也許複製超快、訊號已經全到（小檔案 / 高速 NAS）
            if (self.nas_copy_done + self.nas_copy_errors) >= self._nas_expected:
                self._on_all_nas_copies_done()
        else:
            self.record_time_label.setText("00:00")
            self._reset_recording_paths()

    def _on_nas_copy_status(self, camera_name: str, status: str, msg: str):
        """worker → StreamThread → 此 slot。status ∈ {"started", "done", "error"}"""
        if status == "started":
            self.nas_copy_started += 1
        elif status == "done":
            self.nas_copy_done += 1
        elif status == "error":
            self.nas_copy_errors += 1
            self._nas_error_msgs.append(f"{camera_name}: {msg}")
        # 還沒 _after_stop snapshot 之前不檢查完成度（避免快檔案的 started/done 同 tick 抵達誤判）
        if not self._stop_confirmed:
            return
        self._update_nas_progress_label()
        if self._nas_expected > 0 and (self.nas_copy_done + self.nas_copy_errors) >= self._nas_expected:
            self._on_all_nas_copies_done()

    def _update_nas_progress_label(self):
        completed = self.nas_copy_done + self.nas_copy_errors
        total = self._nas_expected if self._nas_expected > 0 else self.nas_copy_started
        self.record_time_label.setText(f"複製到 NAS: {completed}/{total}")
        self.record_time_label.setVisible(True)

    def _on_all_nas_copies_done(self):
        if self.nas_copy_errors:
            self.record_time_label.setText(f"NAS 複製完成（{self.nas_copy_errors} 個失敗，已存桌面）")
            # 所有相機的失敗訊息彙整成一個視窗（原本每台各跳一次）
            QMessageBox.warning(self, "NAS 複製失敗",
                                f"{self.nas_copy_errors} 支影片無法複製到 NAS，已改存本機：\n\n"
                                + "\n".join(self._nas_error_msgs))
        else:
            self.record_time_label.setText(f"NAS 複製完成（{self.nas_copy_done}）")
        # _reset_recording_paths 用更新後的 last_recording_path（已被 nas_copy_done 改成 NAS 路徑）
        self._reset_recording_paths()
        # 3 秒後若沒在錄影就清回 00:00
        QTimer.singleShot(3000, self._clear_status_label_if_idle)
        self.nas_copy_started = 0
        self.nas_copy_done    = 0
        self.nas_copy_errors  = 0
        self._stop_confirmed  = False
        self._nas_expected    = 0
        self._nas_error_msgs  = []

    def _clear_status_label_if_idle(self):
        if not self.record_timer.isActive():
            self.record_time_label.setText("00:00")

    def _reset_recording_paths(self):
        # 舊版會把剛錄好的檔路徑 emit 給「同步播放」分頁自動載入；現已改成使用者
        # 手動「選擇影片資料夾…」，signal 移除。這裡只把 last_recording_path 歸零，
        # 避免下一輪錄影誤用上一輪的路徑。
        for t in self.threads:
            if t and t.last_recording_path:
                t.last_recording_path = None

    def _update_record_time(self):
        self.record_seconds += 1
        m, s = divmod(self.record_seconds, 60)
        h, m = divmod(m, 60)
        time_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
        self.record_time_label.setText(time_str)

    # ── 視窗變大變小時，cell 等比縮放（保持 16:9）─────────────────────
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._recompute_cell_sizes()

    def _recompute_cell_sizes(self):
        if not self.views:
            return
        # 4 cols × cell_w + 3×spacing(20) + 2×grid_margin(10) = self.width()
        # → cell_w = (self.width() - 80) // 4，最小 280×157（再小看不清楚）
        avail = self.width() - 80
        cell_w = max(280, avail // 4)
        cell_h = cell_w * 9 // 16
        for view, scene in zip(self.views, self.scenes):
            if view.width() != cell_w or view.height() != cell_h:
                view.setFixedSize(cell_w, cell_h)
                scene.setSceneRect(0, 0, cell_w, cell_h)

    # ── 全機預覽暫停 / 恢復（切到 playback tab 時用） ──────────────────
    def pause_all_previews(self):
        """暫停所有 live preview（送 pause_preview cmd 到 worker，輕量、不殺 worker process）。
        錄影中不執行，避免影響錄影的 RTSP→mp4 -c copy child（雖然兩者獨立但保守起見）。"""
        if self.record_timer.isActive():
            return
        for t in self.threads:
            if t is not None and t.isRunning():
                try: t.pause_preview()
                except Exception: pass

    def resume_all_previews(self):
        """恢復所有 live preview（送 resume_preview cmd，worker 主迴圈自動重啟 ffmpeg）。"""
        for t in self.threads:
            if t is not None and t.isRunning():
                try: t.resume_preview()
                except Exception: pass

    # ── 單台攝影機暫停 / 恢復 ──────────────────────────────────────────
    def pause_camera_by_ip(self, ip_addr):
        if self.record_timer.isActive():
            return  # 錄影中不暫停串流，避免中斷錄影
        for t in self.threads:
            if t is not None and t.src == ip_addr and t.isRunning():
                t.stop()

    def resume_camera_by_ip(self, ip_addr):
        for i, src in enumerate(self.camera_sources):
            if src == ip_addr:
                if self.threads[i] is None or not self.threads[i].isRunning():
                    _name = self.camera_names[i]
                    new_th = StreamThread(
                        self.scenes[i], self.views[i],
                        camera_ip=ip_addr, camera_name=_name,
                        ui_fps=30, use_substream=True,
                    )
                    _n = self.camera_names[i]
                    new_th.recording_error.connect(lambda msg, n=_n: QMessageBox.warning(self, f"錄影錯誤 [{n}]", msg))
                    new_th.nas_copy_status.connect(lambda status, msg, n=_n: self._on_nas_copy_status(n, status, msg))
                    self.threads[i] = new_th
                    new_th.start()
                return

    # ── 清理 ───────────────────────────────────────────────────────────
    def closeEvent(self, event):
        self.startup_timer.stop()
        self.health_timer.stop()
        for t in self.threads:
            if t:
                try: t.stop_recording()
                except Exception: pass
        self.record_timer.stop()
        self._reset_recording_paths()
        # 先全部 stop()（內部會 join worker process），再 wait() 確認 run() 退出。
        # 不 wait 直接讓父 widget 解構 → QThread 被 GC 時還 isRunning() →
        # "QThread: Destroyed while thread is still running" → crash。
        for t in self.threads:
            if t:
                try: t.stop()
                except Exception: pass
        for t in self.threads:
            if t:
                try: t.wait(1500)
                except Exception: pass
        if event: event.accept()

    def close(self):
        self.closeEvent(None)
