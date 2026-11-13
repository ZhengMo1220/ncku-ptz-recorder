# -*- coding: utf-8 -*-
"""ONVIF 多相機起錄同步器。

問題：go2rtc 對相機只維持一條連線，錄影 ffmpeg 當「新消費者」連上時，go2rtc 不會
觸發相機重送 keyframe，只能等串流裡下一個排程到的 keyframe → 8 台落在 GOP 不同
相位 → 起錄散到 ~1 個 GOP（實測 ~1s）。

解法：在錄影預熱期間（ffmpeg 已連上 go2rtc、正在等 keyframe 時），用 ONVIF
`SetSynchronizationPoint` 同時命令 8 台相機「立刻送一個 I-frame」。8 個 IDR 幾乎
同時經 go2rtc 傳到各自的錄影 ffmpeg → 起錄對齊。

僅在 USE_GO2RTC 時使用。media service 連線會被快取重用（避免每次錄影重連的 jitter）。
"""
import os
import time
import threading

from .app_config import get_camera_credentials

_WSDL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wsdl")

# 觸發時機：在 start_at 之前這麼多秒送 IDR。preamble 是 2.0s，預設 1.3s 代表
# 「進入預熱約 0.7s 後」觸發（此時 ffmpeg 應已連上 go2rtc 在等 keyframe）。可調。
SYNC_FIRE_BEFORE_START = 1.3


class KeyframeSyncer:
    def __init__(self, ips, names, user=None, pwd=None, port=80):
        self.ips, self.names = list(ips), list(names)
        if user is None or pwd is None:
            _u, _p = get_camera_credentials()
            user = _u if user is None else user
            pwd = _p if pwd is None else pwd
        self.user, self.pwd, self.port = user, pwd, port
        self._media = {}            # idx -> (media_service, profile_token)
        self._lock = threading.Lock()

    def _ensure(self, idx):
        """確保第 idx 台的 ONVIF media service 已連好（快取）。回傳 (media, token) 或 None。"""
        with self._lock:
            if idx in self._media:
                return self._media[idx]
        try:
            from onvif import ONVIFCamera
            cam = ONVIFCamera(self.ips[idx], self.port, self.user, self.pwd, wsdl_dir=_WSDL_DIR)
            media = cam.create_media_service()
            token = media.GetProfiles()[0].token
            with self._lock:
                self._media[idx] = (media, token)
            return self._media[idx]
        except Exception as e:
            print(f"[sync] {self.names[idx]} ONVIF media 連線失敗: {e}")
            return None

    def prewarm(self, indices=None):
        """背景把要用的相機 ONVIF 連線先建好，避免 fire 當下才連造成 jitter。"""
        idxs = list(range(len(self.ips))) if indices is None else list(indices)

        def _run():
            ts = [threading.Thread(target=self._ensure, args=(i,), daemon=True) for i in idxs]
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=10)
        threading.Thread(target=_run, daemon=True, name="kf-prewarm").start()

    def _fire_one(self, idx):
        m = self._ensure(idx)
        if not m:
            return
        media, token = m
        try:
            media.SetSynchronizationPoint({'ProfileToken': token})
        except Exception as e:
            print(f"[sync] {self.names[idx]} SetSynchronizationPoint 失敗: {e}")

    def trigger_at(self, indices, fire_at_wallclock):
        """背景：spin-wait 到 fire_at_wallclock，然後並發對 indices 觸發 IDR。"""
        idxs = list(indices)

        def _run():
            while time.time() < fire_at_wallclock - 0.001:
                time.sleep(0.0005)
            ts = [threading.Thread(target=self._fire_one, args=(i,), daemon=True) for i in idxs]
            t0 = time.time()
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=2.0)
            print(f"[sync] 已對 {len(idxs)} 台並發觸發同步 IDR（fire 耗時 {1000*(time.time()-t0):.0f}ms）")
        threading.Thread(target=_run, daemon=True, name="kf-sync").start()
