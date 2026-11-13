import os
import json
import re
import time
import collections
import subprocess
import threading
import cv2
import numpy as np
from typing import Optional, Dict, List, Callable

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QSlider, QFileDialog, QMessageBox,
    QStyle,
)
from PyQt5.QtCore import Qt, QSize, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QImage, QPixmap

from utils.icons_utils import resource_path


DISPLAY_FPS       = 60                                  # 顯示幀率（同 ffmpeg -vf fps）
# 為何 60：source 通常是 120 fps，60 Hz 螢幕一秒最多顯示 60 張 → 1× 真實時間播放（取
# source 一半的 frame）。要慢動作就降低 timer 觸發頻率即可，未來變速控制用得到。
DISPLAY_W         = 460
DISPLAY_H         = 258
BUFFER_SECONDS    = 3                                   # 每路 ring buffer 緩衝秒數，~50 MB/cam
TIMER_INTERVAL_MS = max(1, int(1000 / DISPLAY_FPS))     # 60 Hz tick (~17 ms)
UI_REFRESH_MS     = 200                                 # 進度條 / 時間文字 5 Hz 更新

# Method 2 同步對齊：假設 8 台同步停錄，則「各檔時長差 = 起錄差」。
# 載入時把每支往前 trim 掉 (自己時長 − 最短時長)，讓 8 路對齊到共同起點。
# 主要用於 go2rtc 模式（起錄散到 ~1s）；直連錄影差很小、套了也無害。設 False 可關閉。
ALIGN_BY_DURATION = True

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _resolve_ffmpeg_exe() -> str:
    p = resource_path("ffmpeg.exe")
    return p if os.path.isfile(p) else "ffmpeg"


def _resolve_ffprobe_exe() -> str:
    p = resource_path("ffprobe.exe")
    return p if os.path.isfile(p) else "ffprobe"


_FFMPEG_EXE  = _resolve_ffmpeg_exe()
_FFPROBE_EXE = _resolve_ffprobe_exe()


# ────────────────────────── 探測檔案資訊（不解碼） ──────────────────────────
def _probe_file(path: str) -> tuple:
    """回傳 (duration_ms, fps)。優先用 ffprobe，失敗就 parse ffmpeg stderr 兜底。"""
    try:
        out = subprocess.run(
            [_FFPROBE_EXE, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, creationflags=_NO_WINDOW, timeout=10,
        )
        data = json.loads(out.stdout)
        duration_s = float(data["format"]["duration"])
        fps = 30.0
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                rate = stream.get("r_frame_rate", "30/1")
                try:
                    num, den = rate.split("/")
                    fps = float(num) / float(den) if float(den) else 30.0
                except Exception:
                    fps = 30.0
                break
        return duration_s * 1000.0, fps
    except Exception:
        pass
    try:
        out = subprocess.run(
            [_FFMPEG_EXE, "-i", path],
            capture_output=True, creationflags=_NO_WINDOW, timeout=10,
        )
        text = out.stderr.decode(errors="replace")
        m = re.search(r"Duration:\s+(\d+):(\d+):([\d.]+)", text)
        dur_s = 0.0
        if m:
            h, mn, s = m.groups()
            dur_s = int(h) * 3600 + int(mn) * 60 + float(s)
        m = re.search(r"(\d+(?:\.\d+)?)\s+fps", text)
        fps = float(m.group(1)) if m else 30.0
        return dur_s * 1000.0, fps
    except Exception:
        return 0.0, 30.0


# ────────────────────────── 單路 lazy decoder ──────────────────────────
class _CameraDecoder:
    """一路 ffmpeg subprocess 從 mp4 解碼 + scale + bgr24 pipe；reader thread 餵 ring buffer。

    pipeline：iGPU h264_qsv decode → hwdownload NV12 → CPU swscale 到 DISPLAY_W×DISPLAY_H +
    NV12→BGR。reader thread 拼幀寫入 deque（backpressure：buffer 滿就阻塞，consumer 取走才喚醒），
    主執行緒呼叫 pop_next() 取最舊一幀顯示。seek 用 -ss 重啟 ffmpeg（~1 sec startup，
    QSV decoder context init 主要成本）。
    """

    def __init__(self, name: str, path: str,
                 display_w: int, display_h: int, display_fps: int):
        self.name        = name
        self.path        = path
        self.W           = display_w
        self.H           = display_h
        self.fps         = display_fps
        self.frame_bytes = display_w * display_h * 3

        self.proc:           Optional[subprocess.Popen] = None
        self.reader_thread:  Optional[threading.Thread]  = None
        self.stderr_thread:  Optional[threading.Thread]  = None
        self.stop_event:     threading.Event             = threading.Event()
        self.buffer:         collections.deque           = collections.deque(
            maxlen=max(1, display_fps * BUFFER_SECONDS)
        )
        self.buffer_lock:    threading.Lock              = threading.Lock()
        self.buffer_not_full: threading.Condition        = threading.Condition(self.buffer_lock)
        self.eof:            bool                        = False

    def start(self, seek_time_s: float = 0.0) -> bool:
        """重新啟動 ffmpeg subprocess 跳到 seek_time_s。回傳 True 即啟動成功。"""
        self.stop()
        cmd = [
            _FFMPEG_EXE, "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, seek_time_s):.3f}",
            "-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-c:v", "h264_qsv",
            "-i", self.path,
            "-vf", f"fps={self.fps},hwdownload,format=nv12,scale={self.W}:{self.H}",
            "-pix_fmt", "bgr24", "-f", "rawvideo", "-an", "pipe:1",
        ]
        self.eof        = False
        self.stop_event = threading.Event()
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=_NO_WINDOW, bufsize=0,
            )
        except Exception as e:
            print(f"[{self.name}] decoder spawn 失敗: {e}")
            self.proc = None
            return False

        self.buffer_not_full = threading.Condition(self.buffer_lock)
        self.reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name=f"pbdec-{self.name}",
        )
        self.stderr_thread = threading.Thread(
            target=self._stderr_drain_loop, daemon=True, name=f"pbderr-{self.name}",
        )
        self.reader_thread.start()
        self.stderr_thread.start()
        return True

    def stop(self):
        self.stop_event.set()
        with self.buffer_lock:
            self.buffer_not_full.notify_all()
        if self.proc is not None:
            try: self.proc.terminate()
            except Exception: pass
            try: self.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try: self.proc.kill()
                except Exception: pass
            self.proc = None
        if self.reader_thread is not None:
            self.reader_thread.join(timeout=0.5)
            self.reader_thread = None
        if self.stderr_thread is not None:
            self.stderr_thread.join(timeout=0.3)
            self.stderr_thread = None
        with self.buffer_lock:
            self.buffer.clear()

    def seek(self, time_s: float) -> bool:
        return self.start(seek_time_s=time_s)

    def pop_next(self) -> Optional[np.ndarray]:
        """取最舊一幀並從 buffer 移除。空 buffer 回傳 None（reader 還沒追上時）。"""
        with self.buffer_lock:
            if self.buffer:
                frame = self.buffer.popleft()
                self.buffer_not_full.notify()
                return frame
        return None

    def peek_latest(self) -> Optional[np.ndarray]:
        """看最新一幀但不取走，用來 seek 後立刻刷新顯示。"""
        with self.buffer_lock:
            if self.buffer:
                return self.buffer[-1]
        return None

    def is_eof(self) -> bool:
        return self.eof

    def buffer_count(self) -> int:
        with self.buffer_lock:
            return len(self.buffer)

    def _reader_loop(self):
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        stdout = proc.stdout
        try:
            while not self.stop_event.is_set():
                # Windows pipe 偶爾切短，要拼完整一幀才 reshape
                data = bytearray()
                while len(data) < self.frame_bytes:
                    if self.stop_event.is_set():
                        return
                    chunk = stdout.read(self.frame_bytes - len(data))
                    if not chunk:
                        self.eof = True
                        return
                    data.extend(chunk)
                frame = np.frombuffer(bytes(data), np.uint8).reshape((self.H, self.W, 3))

                # backpressure：buffer 滿就等 consumer pop 一格再寫
                with self.buffer_lock:
                    while len(self.buffer) >= self.buffer.maxlen:
                        if self.stop_event.is_set():
                            return
                        self.buffer_not_full.wait(timeout=0.1)
                    self.buffer.append(frame)
        except Exception as e:
            print(f"[{self.name}] decoder reader 異常: {e}")

    def _stderr_drain_loop(self):
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in iter(proc.stderr.readline, b""):
                if not line or self.stop_event.is_set():
                    break
                text = line.decode(errors="replace").rstrip()
                # 過濾 ffmpeg 的 deprecation noise（QSV hwaccel_output_format 警告每次都印）
                if text and "DEPRECATED" not in text and "deprecated" not in text:
                    print(f"[{self.name}] decoder ffmpeg: {text}")
        except Exception:
            pass


# ────────────────────────── ffprobe 背景執行緒（不解碼，只取 duration / fps） ─────
class _ProbeThread(QThread):
    done = pyqtSignal(dict, float)   # {name: {"duration_ms", "fps", "path"}}, max_duration_ms

    def __init__(self, files: Dict[str, str]):
        super().__init__()
        self.files = files

    def run(self):
        info: Dict[str, Dict] = {}
        max_dur = 0.0
        for name, path in self.files.items():
            dur_ms, fps = _probe_file(path)
            info[name] = {"duration_ms": dur_ms, "fps": fps, "path": path}
            if dur_ms > max_dur:
                max_dur = dur_ms
        self.done.emit(info, max_dur)


# ────────────────────────── PlaybackTab ──────────────────────────
class PlaybackTab(QWidget):
    # 給 main.py 接到 multi_camera_tab.pause_all_previews / resume_all_previews。
    # 在 load + 等第一幀、seek + 等第一幀的「8 路 ffmpeg QSV decoder init 同時噴」階段
    # 暫停 live preview，避免 16 個 QSV session 撞爆 iGPU。
    previewsShouldPause  = pyqtSignal()
    previewsShouldResume = pyqtSignal()

    def __init__(self, camera_names=None):
        super().__init__()
        if camera_names is None:
            camera_names = [f"Ptz{i:02d}" for i in range(1, 9)]
        self.camera_names: List[str] = camera_names

        # 內部狀態
        self._decoders:           Dict[str, _CameraDecoder] = {}
        self._video_info:         Dict[str, Dict]            = {}
        self._offset_ms:          Dict[str, float]            = {}   # Method 2：各台往前 trim 的毫秒數
        self._duration_ms:        float                       = 0.0
        self._current_time_ms:    float                       = 0.0
        self._is_playing:         bool                        = False
        self._play_start_t:       float                       = 0.0
        self._play_start_time_ms: float                       = 0.0
        self._probe_thread:       Optional[_ProbeThread]       = None
        # 追蹤目前是不是已 emit pause（用來避免重複 emit / 漏 emit resume）
        self._previews_paused:    bool                        = False

        # 之後可由外部設定 YOLO annotation hook：(frame_bgr, cam_name) -> frame_bgr
        self._annotate_hook: Optional[Callable[[np.ndarray, str], np.ndarray]] = None

        self._frame_labels: List[QLabel] = []

        # 播放 tick：30 Hz；UI 更新 5 Hz
        self._timer = QTimer(self)
        self._timer.setInterval(TIMER_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(UI_REFRESH_MS)
        self._ui_timer.timeout.connect(self._refresh_ui)

        # 載入第一幀的輪詢 timer（避免阻塞 GUI 等 ffmpeg startup）
        self._first_frame_timer = QTimer(self)
        self._first_frame_timer.setInterval(50)
        self._first_frame_timer.timeout.connect(self._check_first_frames)

        self._build_ui()

    # ── 公開 API：YOLO annotation hook ────────────────────────────────
    def set_annotate_hook(self, fn: Optional[Callable[[np.ndarray, str], np.ndarray]]):
        """掛 annotation 函式；每幀顯示前會被呼叫，回傳的 frame 取代原 frame 上 UI。"""
        self._annotate_hook = fn

    # ── live preview pause/resume emit（用 state flag 避免重複 emit） ─
    def _emit_preview_pause(self):
        if not self._previews_paused:
            self._previews_paused = True
            self.previewsShouldPause.emit()

    def _emit_preview_resume(self):
        if self._previews_paused:
            self._previews_paused = False
            self.previewsShouldResume.emit()

    # ── 建立 UI ───────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("同步播放")
        title.setFont(QFont("Arial", 13, QFont.Bold))

        self.lbl_status = QLabel("尚未載入影片")
        self.lbl_status.setStyleSheet("color: #888;")
        self.lbl_status.setFont(QFont("Arial", 10))

        btn_folder = QPushButton("選擇影片資料夾…")
        btn_folder.clicked.connect(self._browse_folder)

        self.btn_clear = QPushButton("清空畫面")
        self.btn_clear.clicked.connect(self.clear_all)

        top.addWidget(title)
        top.addWidget(self.lbl_status)
        top.addStretch(1)
        top.addWidget(self.btn_clear)
        top.addWidget(btn_folder)
        root.addLayout(top)

        # 2×4 格線
        n    = len(self.camera_names)
        cols = 4
        rows = (n + cols - 1) // cols

        root.addStretch(1)
        grid_hbox = QHBoxLayout()
        grid_hbox.addStretch(1)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(20)
        self.grid.setVerticalSpacing(16)

        for i in range(rows):
            for j in range(cols):
                idx  = i * cols + j
                name = self.camera_names[idx] if idx < n else ""

                lbl_frame = QLabel()
                lbl_frame.setFixedSize(DISPLAY_W, DISPLAY_H)
                lbl_frame.setStyleSheet("background-color: #ddd;")
                lbl_frame.setAlignment(Qt.AlignCenter)

                lbl_name = QLabel(name)
                lbl_name.setFont(QFont("Segoe UI", 11, QFont.Bold))
                lbl_name.setStyleSheet("color: #333;")

                cell = QWidget(self)
                vbox = QVBoxLayout(cell)
                vbox.setContentsMargins(0, 0, 0, 0)
                vbox.setSpacing(2)
                vbox.addWidget(lbl_frame)
                vbox.addWidget(lbl_name)

                self.grid.addWidget(cell, i, j, alignment=Qt.AlignCenter)
                self._frame_labels.append(lbl_frame)

        grid_hbox.addLayout(self.grid)
        grid_hbox.addStretch(1)
        root.addLayout(grid_hbox)
        root.addStretch(1)

        # 控制列
        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)

        style    = self.style()
        icon_sz  = QSize(16, 16)
        btn_qss  = (
            "QPushButton { padding: 3px 12px; border: 1px solid #b8b8b8;"
            " border-radius: 4px; background: #fafafa; }"
            "QPushButton:hover:!disabled { background: #eef4ff; border-color: #6aa1ff; }"
            "QPushButton:pressed:!disabled { background: #dde7ff; }"
            "QPushButton:disabled { color: #aaa; background: #f2f2f2; }"
        )

        self.btn_play = QPushButton(" 播放")
        self.btn_play.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self.btn_play.setIconSize(icon_sz)
        self.btn_play.setMinimumHeight(30)
        self.btn_play.setStyleSheet(btn_qss)
        self.btn_play.setEnabled(False)
        self.btn_play.clicked.connect(self.play_all)

        self.btn_pause = QPushButton(" 暫停")
        self.btn_pause.setIcon(style.standardIcon(QStyle.SP_MediaPause))
        self.btn_pause.setIconSize(icon_sz)
        self.btn_pause.setMinimumHeight(30)
        self.btn_pause.setStyleSheet(btn_qss)
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self.pause_all)

        self.btn_reset = QPushButton(" 重置")
        self.btn_reset.setIcon(style.standardIcon(QStyle.SP_MediaSkipBackward))
        self.btn_reset.setIconSize(icon_sz)
        self.btn_reset.setMinimumHeight(30)
        self.btn_reset.setStyleSheet(btn_qss)
        self.btn_reset.clicked.connect(self.reset_all)

        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.setValue(0)
        self.seek_slider.setEnabled(False)
        self.seek_slider.sliderMoved.connect(self._on_slider_moved)
        self.seek_slider.sliderReleased.connect(
            lambda: self._on_seek(self.seek_slider.value())
        )

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setFont(QFont("Consolas", 10))
        self.time_label.setMinimumWidth(120)

        ctrl.addWidget(self.btn_play)
        ctrl.addWidget(self.btn_pause)
        ctrl.addWidget(self.btn_reset)
        ctrl.addWidget(self.seek_slider, 1)
        ctrl.addWidget(self.time_label)
        root.addLayout(ctrl)

    # ── 載入影片 ──────────────────────────────────────────────────────
    def load_files(self, files: dict):
        """files: {camera_name: file_path}。
        先 stop 舊 decoders + 清狀態，跑 ffprobe 拿 duration（背景 thread），完成後
        spawn 每路 decoder 從 t=0 開始解，輪詢直到所有 decoder 都有第一幀後顯示。
        """
        self.clear_all()  # 內部會 emit_resume，等等再 emit_pause 進入載入狀態

        valid = {k: v for k, v in files.items()
                 if os.path.isfile(v) and self._name_to_idx(k) is not None}
        if not valid:
            self.lbl_status.setText("找不到符合的影片檔")
            return

        # 進入「載入中」：要 8 路 ffmpeg 同時 init QSV decoder context，CPU 會吃比較重，
        # 暫停 live preview 把 iGPU 讓出來。等 _check_first_frames 看到第一幀就 emit_resume。
        self._emit_preview_pause()

        self.lbl_status.setText(f"探測 {len(valid)} 支影片中…")
        self._probe_thread = _ProbeThread(valid)
        self._probe_thread.done.connect(self._on_probe_done)
        self._probe_thread.start()

    def _on_probe_done(self, info: Dict[str, Dict], max_dur_ms: float):
        if not info or max_dur_ms <= 0:
            self.lbl_status.setText("影片探測失敗")
            return
        self._video_info  = info

        # Method 2 同步對齊：假設同步停錄 → 時長差 = 起錄差。各台往前 trim
        # (自己時長 − 最短時長)，對齊到共同起點；對齊後可播長度 = 最短時長。
        durs = {n: m["duration_ms"] for n, m in info.items()}
        min_dur = min(durs.values()) if durs else 0.0
        self._offset_ms = {n: (d - min_dur if ALIGN_BY_DURATION else 0.0)
                           for n, d in durs.items()}
        self._duration_ms = min_dur if ALIGN_BY_DURATION else max_dur_ms
        if ALIGN_BY_DURATION and durs:
            print(f"[playback] 8 檔時長差 {(max(durs.values())-min_dur)/1000:.3f}s → 套用起錄對齊 trim")

        # spawn decoders（從各自 offset 起，使 8 路對齊）
        for name, meta in info.items():
            dec = _CameraDecoder(name, meta["path"], DISPLAY_W, DISPLAY_H, DISPLAY_FPS)
            if dec.start(self._offset_ms.get(name, 0.0) / 1000.0):
                self._decoders[name] = dec
            else:
                print(f"[{name}] decoder 啟動失敗，跳過")

        total_str = self._fmt(self._duration_ms / 1000.0)
        self.lbl_status.setText(f"載入 {len(self._decoders)} 支，等待第一幀…")
        self.time_label.setText(f"00:00 / {total_str}")
        # 開始輪詢第一幀（避免阻塞 GUI 等 ffmpeg startup ~1s）
        self._first_frame_timer.start()

    def _check_first_frames(self):
        if not self._decoders:
            self._first_frame_timer.stop()
            return
        all_ready = True
        for name, dec in self._decoders.items():
            frame = dec.peek_latest()
            if frame is not None:
                self._show_bgr(name, frame)
            else:
                all_ready = False
        if all_ready:
            self._first_frame_timer.stop()
            total_str = self._fmt(self._duration_ms / 1000.0)
            self.lbl_status.setText(f"已載入 {len(self._decoders)} 支，總長 {total_str}")
            self.btn_play.setEnabled(True)
            self.seek_slider.setEnabled(True)
            # 載入 / seek 階段結束，把 live preview 還回去
            self._emit_preview_resume()

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "選擇錄影資料夾")
        if not folder:
            return
        files: Dict[str, str] = {}
        for fname in sorted(os.listdir(folder)):
            if not fname.lower().endswith(".mp4"):
                continue
            for name in self.camera_names:
                if fname.startswith(name):
                    path = os.path.join(folder, fname)
                    if name not in files or os.path.getmtime(path) > os.path.getmtime(files[name]):
                        files[name] = path
        if files:
            self.load_files(files)
        else:
            QMessageBox.information(self, "提示", "資料夾中找不到符合攝影機名稱的 MP4 檔案。")

    # ── 播放控制 ──────────────────────────────────────────────────────
    def play_all(self):
        if not self._decoders:
            return
        self._is_playing         = True
        self._play_start_t       = time.perf_counter()
        self._play_start_time_ms = self._current_time_ms
        self.btn_play.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self._timer.start()
        self._ui_timer.start()

    def pause_all(self):
        self._is_playing = False
        self._timer.stop()
        self._ui_timer.stop()
        self.btn_play.setEnabled(bool(self._decoders))
        self.btn_pause.setEnabled(False)
        self._refresh_ui()

    def reset_all(self):
        self.pause_all()
        self._current_time_ms = 0.0
        self.seek_slider.setValue(0)
        # 所有 decoder seek 回各自對齊起點（offset）
        for name, dec in self._decoders.items():
            dec.seek(self._offset_ms.get(name, 0.0) / 1000.0)
        # 輪詢等第一幀
        self._first_frame_timer.start()
        total_str = self._fmt(self._duration_ms / 1000.0)
        self.time_label.setText(f"00:00 / {total_str}")

    def clear_all(self):
        self._timer.stop()
        self._ui_timer.stop()
        self._first_frame_timer.stop()
        self._is_playing = False
        if self._probe_thread is not None and self._probe_thread.isRunning():
            try: self._probe_thread.terminate()
            except Exception: pass
            self._probe_thread.wait(1000)
        self._probe_thread = None
        for dec in self._decoders.values():
            dec.stop()
        self._decoders.clear()
        self._video_info.clear()
        self._offset_ms.clear()
        self._duration_ms     = 0.0
        self._current_time_ms = 0.0
        self.seek_slider.setEnabled(False)
        self.seek_slider.setValue(0)
        self.btn_play.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.time_label.setText("00:00 / 00:00")
        self.lbl_status.setText("尚未載入影片")
        for lbl in self._frame_labels:
            lbl.clear()
            lbl.setStyleSheet("background-color: #ddd;")
        # 防呆：若在「載入中」狀態被清空，確保 live preview 回得來
        self._emit_preview_resume()

    # ── 播放 tick ─────────────────────────────────────────────────────
    def _on_tick(self):
        if not self._is_playing or not self._decoders:
            return
        # 用 wall-clock 算當前播放時間，避免 Windows timer 抖動造成速度偏慢
        elapsed_ms = (time.perf_counter() - self._play_start_t) * 1000
        self._current_time_ms = self._play_start_time_ms + elapsed_ms

        if self._current_time_ms >= self._duration_ms:
            self._current_time_ms = self._duration_ms
            self.pause_all()
            return

        # 從每路 decoder 取最舊一幀顯示。若 buffer 空（decoder 還沒追上），跳過此 tick。
        all_eof = True
        for name, dec in self._decoders.items():
            frame = dec.pop_next()
            if frame is not None:
                all_eof = False
                self._show_bgr(name, frame)
            elif not dec.is_eof():
                all_eof = False
        if all_eof:
            self.pause_all()

    # ── UI 刷新（進度條 + 時間文字） ──────────────────────────────────
    def _refresh_ui(self):
        if self._duration_ms <= 0:
            return
        if not self.seek_slider.isSliderDown():
            ratio = min(self._current_time_ms / self._duration_ms, 1.0)
            self.seek_slider.setValue(int(ratio * 1000))
        self.time_label.setText(
            f"{self._fmt(self._current_time_ms / 1000.0)} / "
            f"{self._fmt(self._duration_ms / 1000.0)}"
        )

    # ── Seek ──────────────────────────────────────────────────────────
    def _on_slider_moved(self, value: int):
        """拖曳中即時更新時間文字；實際 seek 等 sliderReleased 才做（避免每 tick 重啟 ffmpeg）"""
        if self._duration_ms <= 0:
            return
        pos_ms = value / 1000.0 * self._duration_ms
        self.time_label.setText(
            f"{self._fmt(pos_ms / 1000.0)} / {self._fmt(self._duration_ms / 1000.0)}"
        )

    def _on_seek(self, value: int):
        if self._duration_ms <= 0 or not self._decoders:
            return
        was_playing = self._is_playing
        if was_playing:
            self.pause_all()

        pos_ms = value / 1000.0 * self._duration_ms
        self._current_time_ms = pos_ms

        # seek 跟 load 一樣會讓 8 路 ffmpeg 同時 restart + QSV decoder context 重新 init
        # → 暫停 live preview，等第一幀回來再恢復（_check_first_frames 內 emit_resume）
        self._emit_preview_pause()

        # 每路 decoder 重啟 ffmpeg 到新位置（加上各自對齊 offset）
        for name, dec in self._decoders.items():
            dec.seek((pos_ms + self._offset_ms.get(name, 0.0)) / 1000.0)
        self.lbl_status.setText(f"Seek 到 {self._fmt(pos_ms / 1000.0)}，等待解碼…")
        self._first_frame_timer.start()

        # seek 完是否自動續播：v1 暫不自動，讓使用者明確按播放
        # 若想自動續播：解註下面這段 + 等 _check_first_frames 完成後再呼叫 play_all()
        # if was_playing:
        #     self._auto_play_after_seek = True

    # ── 幀顯示（BGR ndarray → QPixmap） ───────────────────────────────
    def _show_bgr(self, cam_name: str, frame_bgr: np.ndarray):
        label_idx = self._name_to_idx(cam_name)
        if label_idx is None:
            return
        # YOLO annotation hook（v1 預留接口；外部設了才會跑）
        if self._annotate_hook is not None:
            try:
                frame_bgr = self._annotate_hook(frame_bgr, cam_name)
            except Exception as e:
                print(f"[{cam_name}] annotate hook 異常: {e}")

        rgb  = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        qimg = QImage(rgb.data, w, h, w * 3, QImage.Format_RGB888).copy()
        pix  = QPixmap.fromImage(qimg)
        lbl  = self._frame_labels[label_idx]
        # 縮放到 label 當下大小（label 會跟 resizeEvent 動態變化）
        lw, lh = lbl.width(), lbl.height()
        if lw > 0 and lh > 0 and (pix.width() != lw or pix.height() != lh):
            pix = pix.scaled(lw, lh, Qt.KeepAspectRatio, Qt.FastTransformation)
        lbl.setPixmap(pix)

    # ── 視窗 resize 時 cell 等比縮放（保持 16:9） ─────────────────────
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._recompute_cell_sizes()

    def _recompute_cell_sizes(self):
        if not self._frame_labels:
            return
        # 4 cols × cell_w + 3×spacing(20) + 2×grid_margin(? 預設) ≈ self.width()
        avail = self.width() - 80
        cell_w = max(280, avail // 4)
        cell_h = cell_w * 9 // 16
        for lbl in self._frame_labels:
            if lbl.width() != cell_w or lbl.height() != cell_h:
                lbl.setFixedSize(cell_w, cell_h)

    # ── 工具方法 ──────────────────────────────────────────────────────
    def _name_to_idx(self, name: str) -> Optional[int]:
        try:
            return self.camera_names.index(name)
        except ValueError:
            return None

    @staticmethod
    def _fmt(sec: float) -> str:
        s = int(sec)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

    def closeEvent(self, event):
        self.clear_all()
        if event:
            event.accept()
