import os

# 必須在 import cv2 之前設好。這條 env var 只影響「開機探測階段」的 cv2.VideoCapture
# （拿 width/height/source fps）。實際預覽 frame 流走 ffmpeg subprocess + h264_qsv
# native HW decoder，不經過 cv2，所以這裡用 TCP 保守設定即可（探測階段對效能不敏感）。
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay",
)

import cv2
import datetime
import shutil
import tempfile
import time
import queue
import threading
import multiprocessing as mp
import subprocess
import numpy as np
from typing import Optional, List

from PyQt5.QtCore import pyqtSignal, QThread, Qt
from PyQt5.QtGui import QImage, QPixmap
from .ui_utils import _ensure_pixitem
from .icons_utils import resource_path
from .app_config import get_camera_credentials, ENV_USER, ENV_PASS

# ffmpeg 執行檔位置：
#   PyInstaller 打包 → 從 _MEIPASS 找 ffmpeg.exe
#   開發環境 → 專案根目錄的 ffmpeg.exe；若不存在，fallback 用 PATH 上的 "ffmpeg"
def _resolve_ffmpeg_exe() -> str:
    p = resource_path("ffmpeg.exe")
    return p if os.path.isfile(p) else "ffmpeg"

_FFMPEG_EXE = _resolve_ffmpeg_exe()

# Windows 上 spawn ffmpeg 子進程預設會開 console 視窗，8 台相機 = 8 個黑窗。
# 用 CREATE_NO_WINDOW 把它隱藏；非 Windows 平台這個 flag 不存在，給 0。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ── go2rtc 本機 restream（可選，預設關閉）─────────────────────────
# True：所有 RTSP 連線改連本機 go2rtc（rtsp://127.0.0.1:8554/<camera_name>）。
#       go2rtc 對每台相機只開「一條」連線，再分流給預覽+錄影 → 解掉「相機被連兩次
#       推雙流」的壓力，讓錄影時也能開預覽而較不掉幀。需有 go2rtc.exe + go2rtc.yaml。
# False：維持原本直連相機（行為完全不變）。
# 測試步驟：① 下載 go2rtc.exe 放專案根目錄 ② 把這行改 True ③ 跑 main.py。
USE_GO2RTC  = True
GO2RTC_HOST = "127.0.0.1"
GO2RTC_PORT = 8554


def _build_rtsp_url(camera_ip, camera_name, user, pwd, port, stream_path="stream0"):
    """回傳要餵給 cv2/ffmpeg 的 RTSP URL：
    USE_GO2RTC=True → 連本機 go2rtc 的同名 stream；否則直連相機。"""
    if USE_GO2RTC:
        return f"rtsp://{GO2RTC_HOST}:{GO2RTC_PORT}/{camera_name}"
    return f"rtsp://{user}:{pwd}@{camera_ip}:{port}/{stream_path}"


def maybe_start_go2rtc():
    """USE_GO2RTC=True 時啟動本機 go2rtc（讀 go2rtc.yaml）。回傳 Popen 或 None。
    在主程序啟動時呼叫一次即可（不要每台相機都叫）。"""
    if not USE_GO2RTC:
        return None
    exe = resource_path("go2rtc.exe")
    cfg = resource_path("go2rtc.yaml")
    if not os.path.isfile(exe):
        print(f"[go2rtc] 找不到 {exe}！請下載 go2rtc.exe 放到專案根目錄，"
              f"或把 USE_GO2RTC 設回 False（否則連不到相機）。")
        return None
    try:
        # go2rtc.yaml 用 ${PTZ_CAMERA_USER} / ${PTZ_CAMERA_PASS} 取帳密，這裡從 config.ini 帶入環境變數
        user, pwd = get_camera_credentials()
        env = dict(os.environ, **{ENV_USER: user, ENV_PASS: pwd})
        proc = subprocess.Popen([exe, "-config", cfg], creationflags=_NO_WINDOW, env=env)
        print(f"[go2rtc] 已啟動，RTSP restream 在 {GO2RTC_HOST}:{GO2RTC_PORT}")
        return proc
    except Exception as e:
        print(f"[go2rtc] 啟動失敗: {e}")
        return None


def stop_go2rtc(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try: proc.kill()
        except Exception: pass

# NAS 模式下：錄影先寫到 TEMP\PTZ_staging\<session>，停錄後 daemon thread
# 複製到 NAS。理由：SMB per-write RTT 在 8 路 1080p120 並寫下 ≈ 2-3% 掉幀。
# 參考 Frigate NVR + OBS Studio PR #5717，業界 NVR 都採「本機暫存 + 後歸檔」模式。
# _STAGING_SUBDIR 現僅供 cleanup_staging_dir 清掉舊版遺留在 Documents 的孤兒檔。
_STAGING_SUBDIR = ".staging"

# 舊版「本機」模式的預設路徑，現僅供 cleanup_staging_dir 清理遺留孤兒檔之用。
DEFAULT_LOCAL_DIR = os.path.join(os.path.expanduser("~"), "Documents", "PTZ recordings")


def cleanup_staging_dir(max_age_secs: int = 86400) -> None:
    """掃 staging 目錄刪 max_age_secs (預設 24h) 以前的孤兒檔。
    應在 UI 啟動時呼叫一次（單執行緒），用來清掉程式 crash 留下的 staging 檔。
    清兩個位置：(1) TEMP/PTZ_staging（目前唯一的 staging 位置）、
    (2) Documents/PTZ recordings/.staging（舊版遺留位置，順手清掉）。"""
    staging_roots = [
        os.path.join(tempfile.gettempdir(), "PTZ_staging"),
        os.path.join(DEFAULT_LOCAL_DIR, _STAGING_SUBDIR),
    ]
    now = time.time()
    for staging_root in staging_roots:
        if not os.path.isdir(staging_root):
            continue
        removed_files = 0
        for dirpath, _dirnames, filenames in os.walk(staging_root, topdown=False):
            for f in filenames:
                p = os.path.join(dirpath, f)
                try:
                    if now - os.path.getmtime(p) > max_age_secs:
                        os.remove(p)
                        removed_files += 1
                except OSError:
                    pass
            if dirpath != staging_root:
                try:
                    os.rmdir(dirpath)  # 空目錄就順手收掉
                except OSError:
                    pass
        if removed_files:
            print(f"[staging cleanup] {staging_root} 清掉 {removed_files} 個 >{max_age_secs/3600:.0f}h 孤兒檔")


# =====================================================================
# 獨立的工作進程 (Worker Process)
# =====================================================================
def camera_worker_process(
    camera_ip: str, camera_name: str, rtsp_user: str, rtsp_pass: str, rtsp_port: int,
    ui_fps: int, preview_hw: tuple,
    frame_queue: mp.Queue, cmd_queue: mp.Queue, result_queue: mp.Queue,
):
    # 把 stderr 重導向 devnull，消除 OpenCV/FFmpeg 解碼器的 av_log 警告訊息。
    # print() 走 stdout（fd 1），不受影響；FFmpeg 子進程有獨立 pipe 也不受影響。
    try:
        _dn = os.open(os.devnull, os.O_WRONLY)
        os.dup2(_dn, 2)
        os.close(_dn)
    except OSError:
        pass

    # 測試/診斷用：set DISABLE_PREVIEW=1 跳過 preview ffmpeg 啟動，只跑錄影路徑。
    # 用來驗證 8 路 fps drop 是不是 preview QSV decode 飽和造成的：preview 在 iGPU 上
    # 做 1080p120 H.264 decode（filter fps=15 只是輸出端 throttle，decode 仍跑全速），
    # 8 路並發可能榨乾 iGPU；record 走 -c copy 不過 GPU。
    # 把 preview 斷掉再錄 → 如果 fps 飆到 120 = preview 確認是瓶頸。
    _disable_preview = os.environ.get("DISABLE_PREVIEW") == "1"

    ffmpeg_process       = None   # 獨立 RTSP→mp4 -c copy ffmpeg child
    ffmpeg_stderr_thread = None   # daemon thread drain ffmpeg stderr，避免 pipe 滿了 backpressure
    ffmpeg_stderr_buf: List[bytes] = []  # 累積 stderr 給異常退出時印 log 用
    rec_active           = False
    save_path: Optional[str]            = None   # 當前錄影檔（NAS 模式下指 staging 暫存路徑）
    nas_destination_path: Optional[str] = None   # 非 None 時，stop_rec 會把 save_path 複製到此

    # 錄影管線（2026-05-12 重構）：ffmpeg child 自己連 RTSP、自己抄 H.264 packet
    # 進 mp4 (-c copy)。worker 主迴圈只負責 cv2 預覽。陳子權 2024 V1B4 已驗證此路徑可行；
    # rawvideo→nvenc 在 8 路 1080p120 場景會被單 NVENC engine throughput 卡死。
    rec_preamble_secs    = 0.0    # 預熱長度：ffmpeg 在 start_at 前多錄這麼多秒當開頭緩衝
    PREAMBLE_SECS        = 2.0    # 預設預熱長度（解決 RTSP handshake + keyframe wait 造成的 startup gap）

    # 多相機格線 view 是 460×258，預覽 frame 直接吃到剛好，UI 不再 scale。
    _PREVIEW_W, _PREVIEW_H = 460, 258

    frame_w        = 1920
    frame_h        = 1080

    _OPEN_TIMEOUT = getattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC", None)
    _HW_ACCEL     = getattr(cv2, "CAP_PROP_HW_ACCELERATION",       None)
    _HW_D3D11     = getattr(cv2, "VIDEO_ACCELERATION_D3D11",       None)

    # HW accel 回報值 → 人類可讀（cv2.CAP_PROP_HW_ACCELERATION 讀出來的）
    _HW_NAMES = {0: "NONE(SW)", 1: "ANY", 2: "D3D11VA", 3: "MFX", 4: "VAAPI"}

    def _open_cap(url: str) -> cv2.VideoCapture:
        # Windows D3D11 硬解：cv2 + CUDA 路徑在 Win 上有 empty frame bug
        # (opencv/opencv#25185)，學長 2024 pptx 也是用 D3D11，這條最穩。
        # D3D11VA 實際走 iGPU 還是 dGPU 取決於 Windows → Graphics Settings；
        # 把 python.exe / 打包後的 exe 設成「省電」可強制走 Iris Xe（UMA，免 PCIe）。
        # 失敗就 fallback 軟解，再失敗就純預設構造。
        params = []
        if _OPEN_TIMEOUT is not None:
            params += [_OPEN_TIMEOUT, 5000]
        if _HW_ACCEL is not None and _HW_D3D11 is not None:
            try:
                cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG,
                                       params + [_HW_ACCEL, _HW_D3D11])
                if cap.isOpened():
                    return cap
                cap.release()
            except Exception:
                pass
        if params:
            try:
                return cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
            except TypeError:
                pass
        return cv2.VideoCapture(url, cv2.CAP_FFMPEG)

    def _connect():
        # 直連攝影機 RTSP stream0 主流；USE_GO2RTC=True 時改連本機 go2rtc restream
        url = _build_rtsp_url(camera_ip, camera_name, rtsp_user, rtsp_pass, rtsp_port)
        cap = _open_cap(url)
        if cap.isOpened():
            if _HW_ACCEL is not None:
                actual = int(cap.get(_HW_ACCEL))
                hw_name = _HW_NAMES.get(actual, f"unknown({actual})")
                print(f"[{camera_name}] HW accel = {hw_name} "
                      f"({'iGPU/dGPU 由 Windows 圖形設定決定' if actual == 2 else '已 fallback 軟解'})")
            return cap
        cap.release()
        return None

    # ── FFmpeg RTSP→mp4 -c copy 模式：送 'q' 給 stdin，ffmpeg 寫 trailer 退出 ───
    def _stop_ffmpeg(proc):
        try:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
            except (OSError, BrokenPipeError):
                pass
            try:
                proc.stdin.close()
            except OSError:
                pass
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        finally:
            try: proc.stderr.close()
            except OSError: pass

    # ── 用 cv2 探測一次取得解析度／確認攝影機可連線，之後完全走 ffmpeg subprocess ─
    cap = _connect()
    if cap is None:
        print(f"[{camera_name}] Worker 無法連線攝影機。")
        return
    _fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    _fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    _src_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
    if _fw > 0 and _fh > 0:
        frame_w, frame_h = _fw, _fh
    cap.release()  # 不再用 cv2 讀，釋放掉

    # preview_hw=(w,h) → ffmpeg 縮到指定尺寸；(0,0) → 保持原解析度
    _pw, _ph = preview_hw
    out_w, out_h = (_pw, _ph) if _pw > 0 else (frame_w, frame_h)
    _do_scale = _pw > 0
    print(f"[{camera_name}] Worker 探測完成 (stream0) {frame_w}x{frame_h} @ {_src_fps:.1f}fps "
          f"→ ffmpeg h264_qsv → {out_w}x{out_h} @ {ui_fps}fps")
    FRAME_BYTES = out_w * out_h * 3
    MAX_RETRIES = 10

    rtsp_url = _build_rtsp_url(camera_ip, camera_name, rtsp_user, rtsp_pass, rtsp_port)

    # ── ffmpeg preview subprocess（依 ffmpegcv production pattern） ─────
    preview_proc           = None
    preview_reader_thread  = None
    preview_stderr_thread  = None
    preview_stop_event     = threading.Event()
    preview_paused         = False

    def _spawn_preview():
        # 關鍵 flags（驗證過的）：
        #   -hwaccel qsv -hwaccel_output_format qsv -c:v h264_qsv
        #     → Intel iGPU 的 native HW decoder，frame 留在 iGPU surface 上（qsv format）
        #   -vf "fps=N,hwdownload,format=nv12,scale=W:H"
        #     → fps 在 qsv surface 上丟幀；hwdownload 把 frame 從 iGPU 拿回 CPU NV12
        #       （UMA 架構下 RAM→RAM，~0.1ms/frame）；libswscale 用 CPU SIMD 做 scale +
        #       後續 -pix_fmt bgr24 NV12→BGR，i9 上 1ms/frame 級別。
        #     不用 scale_qsv：在 ffmpeg 8.x + Iris Xe 上開 8 路會踩 D3D11 texture pool bug
        #     (E_INVALIDARG)，且 NV12 要求偶數尺寸（225 是奇數會直接拒絕）。
        #   stderr=PIPE + drain thread：上次失敗就是 DEVNULL 把 QSV 錯誤吃光。
        if _do_scale:
            vf = f"fps={ui_fps},hwdownload,format=nv12,scale={out_w}:{out_h}"
        else:
            vf = f"fps={ui_fps},hwdownload,format=nv12"
        cmd = [
            _FFMPEG_EXE, "-hide_banner", "-loglevel", "error",
            "-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-c:v", "h264_qsv",
            "-rtsp_transport", "tcp",
            # 拿掉 nobuffer + low_delay：原本為了低延遲，但讓 H.264 IDR keyframe burst
            # (1~2 秒一次) 直接打到 UI → 週期性卡頓。改用 thread_queue_size 給 demuxer
            # 一點 buffer 吸收 I-frame burst，預覽延遲 +100~300ms（監看用途可接受）。
            "-thread_queue_size", "512",
            "-strict", "experimental",
            "-i", rtsp_url,
            "-vf", vf,
            "-pix_fmt", "bgr24", "-f", "rawvideo",
            "-an", "pipe:1",
        ]
        return subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW, bufsize=0,
        )

    def _reader_loop(proc):
        stdout = proc.stdout
        try:
            while not preview_stop_event.is_set():
                # Windows pipe 偶爾切短，必須拼完整一幀才能 reshape
                data = bytearray()
                while len(data) < FRAME_BYTES:
                    if preview_stop_event.is_set():
                        return
                    chunk = stdout.read(FRAME_BYTES - len(data))
                    if not chunk:
                        return  # ffmpeg 結束
                    data.extend(chunk)
                frame = np.frombuffer(bytes(data), dtype=np.uint8).reshape((out_h, out_w, 3))

                # 大尺寸預覽（單機 1280×720 以上）用較高 JPEG 品質；多攝小格 460×258 用 50 即可
                ui_q = 70 if out_w >= 960 else 50
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, ui_q])
                if ok:
                    # 不再 drain-then-put（那會丟掉 burst 中間的幀 → 預覽一頓一頓）。
                    # 改成有界 FIFO 緩衝：滿了才丟「最舊」一張、bound 住延遲。
                    # StreamThread 端用固定節拍器把這些幀平均放出來，抹平 ffmpeg 的 burst。
                    try:
                        frame_queue.put_nowait(jpg.tobytes())
                    except queue.Full:
                        try: frame_queue.get_nowait()       # 丟最舊一張
                        except queue.Empty: pass
                        try: frame_queue.put_nowait(jpg.tobytes())
                        except queue.Full: pass
        except Exception as e:
            print(f"[{camera_name}] preview reader 異常: {e}")

    def _stderr_drain_loop(proc):
        # ffmpeg 的 QSV 啟動成功 / 失敗訊息都從 stderr 出，必須印出來讓使用者能診斷。
        try:
            for line in iter(proc.stderr.readline, b""):
                if not line or preview_stop_event.is_set():
                    break
                text = line.decode(errors="replace").rstrip()
                if text:
                    print(f"[{camera_name}] ffmpeg: {text}")
        except Exception:
            pass

    def _start_preview():
        nonlocal preview_proc, preview_reader_thread, preview_stderr_thread
        if preview_proc is not None and preview_proc.poll() is None:
            return True
        preview_stop_event.clear()
        try:
            preview_proc = _spawn_preview()
        except Exception as e:
            print(f"[{camera_name}] 啟動預覽 ffmpeg 失敗: {e}")
            preview_proc = None
            return False
        preview_reader_thread = threading.Thread(
            target=_reader_loop, args=(preview_proc,),
            daemon=True, name=f"ppreview-{camera_name}",
        )
        preview_stderr_thread = threading.Thread(
            target=_stderr_drain_loop, args=(preview_proc,),
            daemon=True, name=f"pstderr-{camera_name}",
        )
        preview_reader_thread.start()
        preview_stderr_thread.start()
        return True

    def _stop_preview():
        nonlocal preview_proc, preview_reader_thread, preview_stderr_thread
        preview_stop_event.set()
        if preview_proc is not None:
            try: preview_proc.terminate()
            except Exception: pass
            try: preview_proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try: preview_proc.kill()
                except Exception: pass
            preview_proc = None
        if preview_reader_thread is not None:
            preview_reader_thread.join(timeout=1.0)
            preview_reader_thread = None
        if preview_stderr_thread is not None:
            preview_stderr_thread.join(timeout=0.5)
            preview_stderr_thread = None

    if _disable_preview:
        print(f"[{camera_name}] [DEV] DISABLE_PREVIEW=1：preview 不啟動，只跑錄影路徑。")
    elif not _start_preview():
        print(f"[{camera_name}] Worker 預覽 ffmpeg 啟動失敗，退出。")
        return

    reconnect_count = 0

    while True:
        # ── 處理指令 ──────────────────────────────────────────────
        # get(timeout=0.1) 取代 get_nowait + sleep，主迴圈自然節流不 busy loop
        try:
            cmd = cmd_queue.get(timeout=0.1)

            if cmd["action"] == "quit":
                if ffmpeg_process:
                    _stop_ffmpeg(ffmpeg_process)
                _stop_preview()
                break

            elif cmd["action"] == "pause_preview":
                if not preview_paused:
                    _stop_preview()
                    preview_paused = True
                    print(f"[{camera_name}] 預覽已暫停（放大視窗）。")

            elif cmd["action"] == "resume_preview":
                if preview_paused:
                    preview_paused  = False
                    reconnect_count = 0
                    print(f"[{camera_name}] 預覽恢復，主迴圈會自動重連 ffmpeg。")

            elif cmd["action"] == "start_rec":
                if rec_active and ffmpeg_process and ffmpeg_process.poll() is None:
                    print(f"[{camera_name}] 錄影中，忽略重複的 start_rec。")
                    continue

                rec_active = True

                # 決定存檔目錄
                # NAS 模式：先寫到本機 staging (TEMP\PTZ_staging)，停錄後背景複製到 NAS。
                # 理由：8 路 1080p120 直寫 SMB 會掉 2-3% 幀（FFmpeg file output buffer 只有 256KB、
                # 每秒 ~380 次 SMB write call 撞 per-RTT 開銷）。Frigate / OBS PR #5717 都採此模式。
                # 若 staging 不可用，fallback 鏈順著走，最壞直寫 NAS（退回舊行為）。
                _desktop_fallback = os.path.join(os.path.expanduser("~"), "Desktop", "PTZ recordings")
                custom_dir  = cmd.get("custom_dir")
                _nas_dir    = cmd.get("nas_dir")   or r"Z:\PTZ recordings"
                # staging 暫存固定用系統 TEMP（保證存在、本機磁碟，通常是
                # C:\Users\…\AppData\Local\Temp）。已移除可由使用者設定的本機路徑。
                _temp_staging = os.path.join(tempfile.gettempdir(), "PTZ_staging")

                nas_target_root = None   # 非 None → stop_rec 要把 save_path 複製到此 NAS 根目錄
                if custom_dir:
                    _candidates = [(custom_dir, "custom"),
                                   (_desktop_fallback, "desktop")]
                elif cmd.get("save_to_nas", False):
                    nas_target_root = _nas_dir
                    # staging 只用 TEMP\PTZ_staging（保證存在的本機磁碟）；
                    # 不可用就直寫 NAS（會掉幀），再不行退桌面。
                    _candidates = [(_temp_staging, "staging"),
                                   (_nas_dir, "nas"),
                                   (_desktop_fallback, "desktop")]
                else:
                    # 移除「本機」模式後 UI 只剩 NAS / 自訂，理論上不會走到這；
                    # 保險起見退到桌面，避免 worker 因無候選路徑而中止。
                    _candidates = [(_desktop_fallback, "desktop")]

                target_dir, selected_label = None, None
                for _d, _label in _candidates:
                    try:
                        os.makedirs(_d, exist_ok=True)
                        target_dir, selected_label = _d, _label
                        if _label == "staging":
                            # _d 才是真正用到的路徑（可能是 _staging_dir 或 _temp_staging）
                            print(f"[{camera_name}] NAS 模式：暫存到 {_d}，停錄後背景複製到 {_nas_dir}")
                        elif _label == "nas":
                            print(f"[{camera_name}] [WARN] staging 不可用，直接寫 NAS: {_nas_dir}（可能掉幀）")
                        elif _label == "desktop":
                            print(f"[{camera_name}] [WARN] 備援至桌面: {_desktop_fallback}")
                        break
                    except Exception as _e:
                        print(f"[{camera_name}] 路徑 {_d} 不可用 ({_e})，嘗試下一個...")

                if target_dir is None:
                    _err = "所有存檔路徑均無法建立，請手動指定存檔資料夾"
                    print(f"[{camera_name}] [WARN] {_err}")
                    result_queue.put({"type": "rec_error", "msg": _err})
                    rec_active = False
                    continue

                session_name = cmd.get("session_name")
                if session_name:
                    target_dir = os.path.join(target_dir, session_name)
                    os.makedirs(target_dir, exist_ok=True)

                dt        = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_name = "".join(x for x in camera_name if x.isalnum())
                save_path = os.path.join(target_dir, f"{safe_name}_{dt}.mp4")

                # 只有走 staging 才設 nas_destination_path；fallback 到直寫 NAS 時不設，
                # 避免 stop_rec 又把 NAS 上的檔再複製一次到自己。
                if selected_label == "staging" and nas_target_root:
                    _nas_dst_dir = os.path.join(nas_target_root, session_name) if session_name else nas_target_root
                    nas_destination_path = os.path.join(_nas_dst_dir, f"{safe_name}_{dt}.mp4")
                else:
                    nas_destination_path = None

                # 精確同步等待 + 預熱：在 start_at - PREAMBLE_SECS 啟動 ffmpeg，
                # 讓 RTSP handshake / keyframe wait 在 start_at 之前完成。
                start_at = cmd.get("start_at")
                rec_preamble_secs = 0.0
                if start_at:
                    preamble_at = start_at - PREAMBLE_SECS
                    wait_secs   = preamble_at - time.time()
                    if wait_secs > 5.0:
                        print(f"[{camera_name}] preamble_at 過遠 ({wait_secs:.1f}s)，直接啟動")
                        rec_preamble_secs = PREAMBLE_SECS
                    elif wait_secs > 0.001:
                        while time.time() < preamble_at - 0.001:
                            time.sleep(0.0001)
                        while time.time() < preamble_at:
                            pass
                        rec_preamble_secs = PREAMBLE_SECS
                    elif wait_secs > -PREAMBLE_SECS:
                        rec_preamble_secs = max(0.0, start_at - time.time())
                    else:
                        print(f"[{camera_name}] start_at 已過期 {-(start_at - time.time()):.1f}s，立即啟動")

                # ── RTSP → mp4 -c copy 錄影 ─────────────────────────────
                # ffmpeg child 自己連 RTSP、零解碼零編碼，直接抄 H.264 packet 進 mp4。
                _path = cmd.get("record_stream_path") or "stream0"
                rec_rtsp_src = _build_rtsp_url(camera_ip, camera_name, rtsp_user, rtsp_pass, rtsp_port, _path)

                # 錄影 flag 配置（2026-05-19 修正：1080p120 寫入 NAS 掉到 108fps、本機 118fps）：
                # -loglevel warning             暫時開 warning 級別，觀察 non-monotonous DTS / past duration 等訊息。
                # -rtsp_transport tcp           LAN 上 packet loss <0.01%，避免 UDP jitter 造成花屏。
                # -thread_queue_size 1024       demuxer→muxer queue 加深，吸收下游短暫 stall。
                # -rtbufsize 500M               RTP 收包 buffer，吸收網路 hiccup。
                # -avoid_negative_ts make_zero  mp4 PTS 從 0 開始，避開部分播放器 seek bug。
                # -fflags +genpts               缺 PTS 自動生成。【拿掉 discardcorrupt】先前對 TCP 抖動太敏感，
                #                               延遲到達的 packet 被誤判 corrupt → 累積丟幀。
                #
                # 【2026-05-19 移除】-max_interleave_delta 0  → -an 場景下無作用。
                # 【2026-05-19 移除】-flush_packets 1         → FFmpeg 官方 ffmpeg-formats 文件：
                #   "1 enables it, and has the effect of reducing the latency, 0 disables it
                #    and may increase IO throughput in some cases."
                #   120fps × per-packet flush 在 NAS (SMB ~2ms RTT) 上 ≈ 240ms/s 純等待 → 掉 10% 幀。
                #   錄影不需要低延遲，吞吐量優先。Frigate / MediaMTX 等生產級 NVR 都沒用這條。
                #
                # 不要 -use_wallclock_as_timestamps（TCP burst 會壓縮 PTS 造成快放卡頓）。
                # 不要 -movflags +faststart（2-pass 對 NAS 28MB 檔要 56MB 跨網 I/O，常 timeout）。
                ffmpeg_cmd = [
                    _FFMPEG_EXE, "-hide_banner", "-loglevel", "warning",
                    "-rtsp_transport", "tcp",
                    "-thread_queue_size", "1024",
                    "-rtbufsize", "500M",
                    "-avoid_negative_ts", "make_zero",
                    "-fflags", "+genpts",
                    "-i", rec_rtsp_src,
                    "-map", "0:v:0",
                    "-c:v", "copy",
                    "-an",
                    save_path,
                ]
                try:
                    ffmpeg_process = subprocess.Popen(
                        ffmpeg_cmd,
                        stdin=subprocess.PIPE,    # 送 'q' 讓 ffmpeg 寫 trailer 退出
                        stderr=subprocess.PIPE,
                        creationflags=_NO_WINDOW, # 不要每台相機都跳一個 console 黑窗
                    )
                    # stderr drain thread：避免 pipe 滿了讓 ffmpeg 寫 stderr 時 block 拖慢 RTSP read。
                    ffmpeg_stderr_buf = []
                    def _drain_stderr(proc=ffmpeg_process, buf=ffmpeg_stderr_buf):
                        try:
                            for line in iter(proc.stderr.readline, b""):
                                if not line:
                                    break
                                buf.append(line)
                                if len(buf) > 200:
                                    del buf[:100]
                        except Exception:
                            pass
                    ffmpeg_stderr_thread = threading.Thread(
                        target=_drain_stderr, daemon=True,
                        name=f"ffstderr-{camera_name}",
                    )
                    ffmpeg_stderr_thread.start()
                    print(f"[{camera_name}] FFmpeg 錄影開始 (-c copy 1080p 120fps, preamble={rec_preamble_secs:.2f}s): {save_path}")
                    result_queue.put({"type": "rec_path", "path": save_path})
                    if start_at and rec_preamble_secs > 0:
                        while time.time() < start_at - 0.001:
                            time.sleep(0.0001)
                        while time.time() < start_at:
                            pass
                except Exception as e:
                    print(f"[{camera_name}] 啟動錄影失敗！請確認 ffmpeg.exe 存在且能連到相機 RTSP。錯誤: {e}")
                    result_queue.put({"type": "rec_error", "msg": f"啟動錄影失敗：{e}"})
                    rec_active    = False
                    ffmpeg_process = None
                    continue

            elif cmd["action"] == "stop_rec":
                if rec_active:
                    if ffmpeg_process:
                        _stop_ffmpeg(ffmpeg_process)
                if ffmpeg_stderr_thread is not None:
                    ffmpeg_stderr_thread.join(timeout=1.0)

                # NAS staging → 背景複製到 NAS（不阻塞 worker 主迴圈，使用者可立即開始下一段錄影）。
                # 失敗時把 staging 檔搬到桌面（不在開機清理路徑內），UI 跳警告，資料絕對不丟。
                # —— 這是「搬到沒 NAS 的電腦」的保命路徑：TEMP\PTZ_staging 會被 >24h 清掉，桌面不會。
                if nas_destination_path and save_path and os.path.isfile(save_path):
                    def _copy_to_nas(src=save_path, dst=nas_destination_path,
                                     q=result_queue, name=camera_name):
                        t0 = time.time()
                        try:
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copy2(src, dst)
                            try:
                                os.remove(src)
                            except OSError as _re:
                                print(f"[{name}] NAS 複製成功但 staging 檔刪除失敗: {_re}")
                            elapsed = time.time() - t0
                            print(f"[{name}] NAS 複製完成 ({elapsed:.1f}s): {dst}")
                            q.put({"type": "nas_copy_done", "src": src, "dst": dst,
                                   "elapsed_secs": elapsed})
                        except Exception as e:
                            # NAS 連不到時 src 還在 TEMP\PTZ_staging，會被開機清理 >24h 刪掉。
                            # 為了「沒 NAS 的電腦也絕不掉資料」，把檔案搬到桌面（不在清理路徑內）。
                            _rescued = src
                            try:
                                _session = os.path.basename(os.path.dirname(src))
                                _rescue_root = os.path.join(os.path.expanduser("~"),
                                                            "Desktop", "PTZ recordings")
                                _rescue_dir = _rescue_root if _session in ("", "PTZ_staging") \
                                    else os.path.join(_rescue_root, _session)
                                os.makedirs(_rescue_dir, exist_ok=True)
                                _rescued = os.path.join(_rescue_dir, os.path.basename(src))
                                shutil.move(src, _rescued)
                                print(f"[{name}] NAS 複製失敗: {e}，已改存桌面: {_rescued}")
                            except Exception as _me:
                                # 連搬桌面都失敗（極罕見）才退回保留在 TEMP
                                print(f"[{name}] NAS 複製失敗且搬桌面失敗 ({_me})，檔案保留: {src}")
                            q.put({"type": "nas_copy_error",
                                   "msg": f"NAS 複製失敗 ({e})。檔案已存本機: {_rescued}",
                                   "local_path": _rescued})
                    # 先送 started 訊息（UI 馬上看到「複製中」），再起 daemon thread
                    result_queue.put({"type": "nas_copy_started",
                                      "src": save_path, "dst": nas_destination_path})
                    threading.Thread(target=_copy_to_nas, daemon=True,
                                     name=f"nascp-{camera_name}").start()

                rec_active           = False
                ffmpeg_process       = None
                ffmpeg_stderr_thread = None
                ffmpeg_stderr_buf    = []
                save_path            = None
                nas_destination_path = None
                print(f"[{camera_name}] 錄影結束。")

        except queue.Empty:
            pass

        # ── 錄影 ffmpeg 健康檢查（RTSP → -c copy mp4 child）────────
        if rec_active and ffmpeg_process:
            ret_code = ffmpeg_process.poll()
            if ret_code is not None:
                if ret_code != 0:
                    if ffmpeg_stderr_thread is not None:
                        ffmpeg_stderr_thread.join(timeout=1.0)
                    err = b"".join(ffmpeg_stderr_buf).decode(errors="replace")
                    print(f"[{camera_name}] [WARN] FFmpeg 錄影異常退出 (code {ret_code}): {err[-400:]}")
                    result_queue.put({
                        "type": "rec_error",
                        "msg":  f"錄影 ffmpeg 異常退出 (code {ret_code})",
                    })
                try: ffmpeg_process.stderr.close()
                except OSError: pass
                rec_active           = False
                ffmpeg_process       = None
                ffmpeg_stderr_thread = None
                ffmpeg_stderr_buf    = []

        # ── 預覽 ffmpeg 健康檢查 ──────────────────────────────────
        # 真正的 frame 流轉在 reader thread 裡；主迴圈只偵測 subprocess 死掉 → 重啟。
        if not _disable_preview and not preview_paused and (preview_proc is None or preview_proc.poll() is not None):
            rc = preview_proc.returncode if preview_proc is not None else "n/a"
            print(f"[{camera_name}] 預覽 ffmpeg 結束 (code {rc})，"
                  f"嘗試重連 ({reconnect_count + 1}/{MAX_RETRIES})...")
            _stop_preview()
            reconnect_count += 1
            if reconnect_count >= MAX_RETRIES:
                print(f"[{camera_name}] 達到重連上限，Worker 退出。")
                break
            time.sleep(2.0)
            if _start_preview():
                reconnect_count = 0

    _stop_preview()
    print(f"[{camera_name}] Worker 進程已關閉。")


# =====================================================================
# QThread 包裝器
# =====================================================================
class StreamThread(QThread):
    image_signal    = pyqtSignal(QImage)
    recording_error = pyqtSignal(str)
    # NAS staging 模式下的複製狀態。第一個 str = "started" / "done" / "error"，第二個 = 訊息或路徑
    nas_copy_status = pyqtSignal(str, str)

    def __init__(
        self, scene, view, camera_ip: str = "10.30.3.34", camera_name: str = "Camera",
        ui_fps: int = 15, use_substream: bool = False,
        preview_size: tuple = None,
        rtsp_username: Optional[str] = None, rtsp_password: Optional[str] = None, rtsp_port: int = 554,
    ):
        super().__init__()
        if rtsp_username is None or rtsp_password is None:
            _u, _p = get_camera_credentials()
            rtsp_username = _u if rtsp_username is None else rtsp_username
            rtsp_password = _p if rtsp_password is None else rtsp_password
        self.scene       = scene
        self.view        = view
        self.src         = camera_ip
        self.camera_name = camera_name

        # preview_size=(w,h) → 自訂縮放尺寸（單機大畫面用 960×540 省 pipe bandwidth）
        # use_substream=True → 多攝格線固定 460×258
        # 兩者都沒設 → (0,0) 表示輸出原始解析度（已不建議用）
        if preview_size is not None:
            _preview_hw = (int(preview_size[0]), int(preview_size[1]))
        elif use_substream:
            _preview_hw = (460, 258)
        else:
            _preview_hw = (0, 0)

        self.frame_queue  = mp.Queue(maxsize=12)   # jitter buffer：吸收 ffmpeg 的 burst（~0.4s 上限）
        self._display_fps = max(1, int(ui_fps))    # 預覽放出節拍 (fps)
        self.cmd_queue    = mp.Queue()
        self.result_queue = mp.Queue()
        self.running      = True
        self.worker_process = None
        self.last_recording_path: Optional[str] = None
        # UI 端丟舊幀用：True = UI 主執行緒還在畫上一張，run() 就跳過新幀，
        # 避免 image_signal 的 queued connection 無上限累積 → 預覽慢動作/延遲。
        self._ui_busy = False

        if hasattr(view, "_pixitem"):
            delattr(view, "_pixitem")

        self.worker_args = (
            self.src, self.camera_name, rtsp_username, rtsp_password, rtsp_port,
            ui_fps, _preview_hw,
            self.frame_queue, self.cmd_queue, self.result_queue,
        )
        self.image_signal.connect(self.update_ui_image)

    def update_ui_image(self, qimg: QImage):
        try:
            pixitem = _ensure_pixitem(self.scene, self.view)
            pix     = QPixmap.fromImage(qimg)
            vw, vh  = self.view.width(), self.view.height()
            if pix.width() > 0 and vw > 0 and vh > 0:
                scaled = pix.scaled(vw, vh, Qt.KeepAspectRatio, Qt.FastTransformation)
                pixitem.setPixmap(scaled)
                # 上下左右置中：view 非 16:9 時（單機大畫面）空白集中在中央兩側
                pixitem.setPos((vw - scaled.width()) / 2, (vh - scaled.height()) / 2)
        except Exception:
            pass
        finally:
            self._ui_busy = False   # 這張畫完了，run() 才能送下一張

    def run(self):
        print(f"[{self.camera_name}] UI 代理執行緒啟動。")
        self.worker_process = mp.Process(
            target=camera_worker_process, args=self.worker_args, daemon=True
        )
        self.worker_process.start()

        # 定時節拍器：以固定間隔從 jitter buffer 取「最舊一張」放出來，
        # 把 ffmpeg 一陣一陣吐幀(burst)抹平成順順的畫面（同 playback 的做法）。
        _interval = 1.0 / self._display_fps          # e.g. 30fps → 33ms
        _next_t   = time.perf_counter()

        while self.running:
            # 等到下一個節拍（小睡，保持 result_queue 仍每 ~interval 輪詢一次）
            _wait = _next_t - time.perf_counter()
            if _wait > 0:
                time.sleep(min(_wait, 0.1))
            _next_t += _interval
            if _next_t < time.perf_counter() - 0.5:  # 落後太多就重新對齊，避免追幀爆衝
                _next_t = time.perf_counter()

            # 這一拍：UI 有空就從緩衝取「最舊一張」(FIFO，維持時間順序) 畫出來
            if not self._ui_busy:
                try:
                    jpeg_bytes = self.frame_queue.get_nowait()
                except queue.Empty:
                    jpeg_bytes = None
                if jpeg_bytes is not None:
                    qimg = QImage()
                    qimg.loadFromData(jpeg_bytes, "JPG")
                    self._ui_busy = True
                    self.image_signal.emit(qimg)

            try:
                result = self.result_queue.get_nowait()
                rtype = result.get("type")
                if rtype == "rec_path":
                    self.last_recording_path = result["path"]
                elif rtype == "rec_error":
                    self.recording_error.emit(result["msg"])
                elif rtype == "nas_copy_started":
                    self.nas_copy_status.emit("started", result.get("dst", ""))
                elif rtype == "nas_copy_done":
                    # 把 last_recording_path 從 staging 更新成 NAS 最終位置
                    # （playback / 後續處理拿到的就是 NAS 上那支檔）
                    self.last_recording_path = result["dst"]
                    self.nas_copy_status.emit("done", result["dst"])
                elif rtype == "nas_copy_error":
                    # 複製失敗：保留 local 路徑當 fallback，讓使用者至少有本機檔可用
                    self.last_recording_path = result.get("local_path") or self.last_recording_path
                    self.nas_copy_status.emit("error", result["msg"])
                    self.recording_error.emit(result["msg"])
            except queue.Empty:
                pass

            if self.worker_process and not self.worker_process.is_alive():
                break

    def start_recording(self, path=None, target_seconds=None, target_fps=120.0,
                        frame_size=(1920, 1080), fourcc="avc1",
                        save_to_nas=False, custom_dir=None, start_at=None,
                        session_name=None, record_stream_path=None,
                        nas_dir=None):
        cmd = {
            "action":             "start_rec",
            "fps":                float(target_fps),
            "size":               (int(frame_size[0]), int(frame_size[1])),
            "fourcc":             str(fourcc),
            "save_to_nas":        save_to_nas,
            "custom_dir":         custom_dir,
            "nas_dir":            nas_dir,
            "start_at":           start_at,
            "session_name":       session_name,
            "record_stream_path": record_stream_path,
        }
        self.cmd_queue.put(cmd)

    def pause_preview(self):
        self.cmd_queue.put({"action": "pause_preview"})

    def resume_preview(self):
        self.cmd_queue.put({"action": "resume_preview"})

    def stop_recording(self):
        self.cmd_queue.put({"action": "stop_rec"})

    def stop(self):
        self.running = False
        try:
            self.cmd_queue.put({"action": "quit"})
        except Exception:
            pass
        proc = self.worker_process
        if proc is not None and proc.is_alive():
            # 短 grace 期：iGPU decoder 過載時 cap.read() 會 block 數百 ms 甚至更久，
            # 等不到 worker 自己 break 出主迴圈。一過 grace 立刻 terminate，
            # terminate 失敗（少見但會發生在 D3D11 handle 未釋放時）再上 kill。
            proc.join(timeout=0.5)
            if proc.is_alive():
                try: proc.terminate()   # Win32 TerminateProcess
                except Exception: pass
                proc.join(timeout=1.0)
            if proc.is_alive():
                try: proc.kill()        # 最後一道保險
                except Exception: pass
                proc.join(timeout=0.5)
            status = "已終止" if not proc.is_alive() else f"[WARN] 仍存活 PID={proc.pid}"
            print(f"[{self.camera_name}] worker {status}")
        self.quit()
