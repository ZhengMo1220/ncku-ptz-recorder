# NCKU PTZ 錄影 / 同步播放 App

成大體育場多路 PTZ 網路攝影機的**即時預覽 + 同步錄影 + 多路同步回放**桌面程式。
PyQt5 GUI，錄影與解碼交給 ffmpeg 子行程，可選用 go2rtc 做本機 restream。

> 交接筆記（2026-09）：YOLO 偵測**尚未接上**，`playback_tab.py` 只留了 hook 接口
> （見[下方](#yolo-預留接口)）。目前打包版是「純錄影版」。

---

## 1. 需求

| 項目 | 版本 / 說明 |
|---|---|
| OS | Windows 10/11 |
| Python | **3.14**，裝在專案根目錄的 `.venv\`。建議用 [uv](https://docs.astral.sh/uv/) 建立（會自動下載 Python 3.14，不用另外安裝）。早期的 conda `ptz` 3.9 環境已棄用 |
| GPU | Intel iGPU（Iris Xe 等），預覽/回放解碼走 QSV（`h264_qsv`）。建議在 Windows 圖形設定把 `python.exe` / 打包後 exe 設「省電」強制走 iGPU |
| 網路 | 與相機同網段（預設 `10.30.3.0/24`），RTSP 走 TCP |
| 硬碟 | 錄影檔很大（1080p120，~1.5 GB/分/路）；NAS 模式先寫本機 TEMP 再背景複製 |

外部執行檔（**很大、不進版控**，下載方式見 [2-3](#2-3-外部執行檔必要)）：

| 檔案 | 版本 | 用途 |
|---|---|---|
| `ffmpeg.exe` | FFmpeg 8.1.1 essentials（gyan.dev） | 預覽解碼 + 錄影（`-c copy`） |
| `ffprobe.exe` | 同上 | 同步播放分頁探測影片長度 / fps |
| `go2rtc.exe` | go2rtc 1.9.14 | `USE_GO2RTC=True` 時的本機 restream |

---

## 2. 安裝

### 2-1. 取得程式碼

```bat
git clone https://github.com/ZhengMo1220/ncku-ptz-recorder.git
cd ncku-ptz-recorder
```

### 2-2. Python 環境（uv，建議）

沒有 uv 先裝一次（PowerShell）：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

然後在專案根目錄執行：

```bat
install.bat
```

`install.bat` 會：有 uv 就用 `uv venv --python 3.14 .venv` + `uv pip install -r requirements.txt`；沒有 uv 才退回 `py -3.14 -m venv` + pip（需先從 python.org 裝 3.14）。順便從範本建立 `config.ini`。

手動版：

```bat
uv venv --python 3.14 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

> conda 也能用（`conda create -n ptz -c conda-forge python=3.14 pip`，`conda activate ptz` 後 `pip install -r requirements.txt`），`start.bat` / `packing.bat` 在沒有 `.venv` 時會使用目前已啟用的 conda 環境。

### 2-3. 外部執行檔（必要）

這三個 exe 太大（ffmpeg/ffprobe 各約 100 MB），**不放在 GitHub**，要自己下載。以下連結都是**已測試過的固定版本**（與開發機上的檔案逐位元組相同），請不要換成最新版：程式裡有針對 ffmpeg 8.x 的 QSV 行為做處理，新版未經測試。

| 步驟 | 下載（點了直接下載 zip） | 從 zip 裡取出 |
|---|---|---|
| ① | [ffmpeg-8.1.1-essentials_build.zip](https://github.com/GyanD/codexffmpeg/releases/download/8.1.1/ffmpeg-8.1.1-essentials_build.zip)（約 105 MB，gyan.dev 官方 GitHub 鏡像） | `ffmpeg-8.1.1-essentials_build\bin\` 裡的 **`ffmpeg.exe`**、**`ffprobe.exe`**（`ffplay.exe` 用不到） |
| ② | [go2rtc_win64.zip（v1.9.14）](https://github.com/AlexxIT/go2rtc/releases/download/v1.9.14/go2rtc_win64.zip)（約 7 MB） | 裡面唯一的 **`go2rtc.exe`** |

**放到專案根目錄**，也就是和 `main.py` 同一層：

```
ncku-ptz-recorder\
├─ main.py
├─ ffmpeg.exe      ← ①
├─ ffprobe.exe     ← ①
├─ go2rtc.exe      ← ②
├─ config.ini      ← 2-4 建立
├─ go2rtc.yaml
└─ ...
```

> 這三個檔名已列在 `.gitignore`，放進來也不會被 commit。
> 少了 `ffmpeg.exe` 會退回使用 PATH 上的 `ffmpeg`（版本不一定對）；少了 `go2rtc.exe` 終端機會印 `[go2rtc] 找不到 ...`，相機會連不上。
> 原始下載頁：[gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/)、[go2rtc releases](https://github.com/AlexxIT/go2rtc/releases)。

### 2-4. 相機帳密（必要）

帳密**不進版控**。複製範本並填入實際帳密：

```bat
copy config.example.ini config.ini
```

```ini
[camera]
username = 相機帳號
password = 相機密碼
```

- `config.ini` 已在 `.gitignore`，不會被 commit。
- 也可改用環境變數 `PTZ_CAMERA_USER` / `PTZ_CAMERA_PASS`（優先於 `config.ini`）。
- 這組帳密同時用在 RTSP 直連、ONVIF（PTZ / 起錄同步）以及 `go2rtc.yaml`（App 啟動 go2rtc 時以環境變數 `${PTZ_CAMERA_USER}` / `${PTZ_CAMERA_PASS}` 帶入）。
- 密碼若含 `@ : /` 等字元會讓 RTSP URL 解析錯誤，請避免。

> ⚠️ `wsdl/` 裡的 `xmlmime`、`envelope`、`include`、`addressing` **本來就沒有副檔名**，不要改名。
> 從 Google Drive 下載時這些檔可能被自動加上 `.xml`，會導致 ONVIF 報 `No such file or directory: ...\wsdl\xmlmime`，把副檔名拿掉即可。

---

## 3. 執行

**終端機**（要在專案根目錄執行；`icons/`、`wsdl/`、`ffmpeg.exe` 等都是相對於工作目錄找的）：

| 終端機 | 指令 |
|---|---|
| PowerShell（VS Code 預設） | `.\start.bat` 或 `.\.venv\Scripts\python.exe main.py` |
| 命令提示字元 cmd | `start.bat` 或 `.venv\Scripts\python.exe main.py` |
| 任何終端機（用 uv） | `uv run python main.py`（會自動使用專案的 `.venv`） |

> PowerShell 不會執行目前資料夾的檔案，所以要加 `.\`（打 `start.bat` 會說無法辨識）。
> 想先啟用環境再用 `python`：PowerShell 用 `.\.venv\Scripts\Activate.ps1`，cmd 用 `.venv\Scripts\activate.bat`。
終端機會印出每台相機的連線、錄影、NAS 複製 log，除錯時很有用。`start.bat` 使用的直譯器順序：`.venv\` → 已啟用的 conda 環境 → `py -3.14`。啟動預設停在「多機攝影機」分頁。

**VS Code**：

1. 安裝 Python 擴充套件（開啟資料夾時會依 `.vscode/extensions.json` 提示）。
2. `.vscode/settings.json` 已把直譯器預設為 `${workspaceFolder}\.venv`；右下角若沒顯示 `.venv`，`Ctrl+Shift+P` → **Python: Select Interpreter** 手動選。
3. `F5` 選 **Run PTZ Recorder (main.py)**（`.vscode/launch.json`，已把 `cwd` 設為專案根目錄）。

> 所有 `.bat` 檔刻意**只用 ASCII（英文）**。cmd 在 `chcp 65001` 下讀含中文的 bat 會切錯行、把半行字當指令執行（實測會誤跑 `ffmpeg`），修改時請勿加入中文。

> 不在相機網段（`10.30.3.0/24`）時程式仍可開啟，但預覽會是黑畫面、ONVIF 會印連線失敗，屬正常現象。

---

## 4. 三個分頁

| 分頁 | 檔案 | 功能 |
|---|---|---|
| 單機攝影機 | `ptz_control/camera_tab_control.py` | 選一台相機看大畫面、ONVIF PTZ 方向/zoom 控制（限 `Ptz*`）、單路錄影。錄影中會顯示紅點 `recording_status_label` |
| 多機攝影機 | `ptz_control/multi_camera_tab.py` | 2×4 格線同時預覽 8 台 `Ptz01~Ptz08`、勾選多台同步錄影、可勾選要不要連線（壓力測試承載上限）。點格子會開「放大」分頁（`enlarge_tab.py`） |
| 同步播放 | `playback_tab.py` | 選一個錄影資料夾，依相機名前綴配對 mp4，8 路同步回放。用「各檔時長差 = 起錄差」把 8 路 trim 對齊（`ALIGN_BY_DURATION`） |

---

## 5. 架構概觀

### 預覽資料流（每路一份）

```
相機 RTSP ──> ffmpeg 子行程（-hwaccel qsv -c:v h264_qsv，iGPU 解碼）
           ──> -vf fps=N,hwdownload,format=nv12,scale=W:H ──> bgr24 rawvideo pipe
           ──> worker reader thread：cv2.imencode JPEG ──> mp.Queue（有界 jitter buffer）
           ──> StreamThread.run()：固定節拍器取最舊一張 ──> image_signal ──> QGraphicsView
```

- `utils/stream_thread.py`
  - `camera_worker_process`：獨立 **process**（`mp.Process`），跑 ffmpeg 預覽 + ffmpeg 錄影，靠三個 `mp.Queue`（frame / cmd / result）與主行程溝通。
  - `StreamThread(QThread)`：UI 端代理，把 worker 的 JPEG 幀丟上 GUI，並轉發 `start_recording` / `pause_preview` / `resume_preview` / `stop` 等指令。
- 錄影管線是 **`ffmpeg -i rtsp ... -c:v copy out.mp4`**（零解碼零編碼，直接抄 H.264 packet）。所以 `start_recording()` 的 `target_fps` / `frame_size` / `fourcc` 參數目前**沒有作用**，錄的是相機原生串流。

### go2rtc 模式（`USE_GO2RTC`，預設 `True`）

`utils/stream_thread.py` 頂端的 `USE_GO2RTC`：

- `True`：`main.py` 啟動時跑 `go2rtc.exe -config go2rtc.yaml`，所有 RTSP 改連 `rtsp://127.0.0.1:8554/<camera_name>`。每台相機只被 go2rtc 連**一次**再分流給預覽+錄影 → 錄影時開預覽較不掉幀。除錯頁：`http://127.0.0.1:1984`。
- `False`：直連相機（`rtsp://user:pwd@ip:554/stream0`），錄影時強制暫停預覽（相機被連兩次會掉幀）。

go2rtc 模式下起錄時，`utils/onvif_sync.py` 的 `KeyframeSyncer` 會用 ONVIF `SetSynchronizationPoint` 命令 8 台同時送 I-frame，把起錄時間對齊（解 go2rtc 起錄散 ~1s 的問題）。

### NAS staging

NAS 模式錄影先寫 `%TEMP%\PTZ_staging\<session>\`，停錄後 daemon thread `shutil.copy2` 到 NAS（預設 `Z:\PTZ recordings`）。理由：8 路 1080p120 直寫 SMB 會掉 2~3% 幀。NAS 連不到時檔案改存桌面 `PTZ recordings\`（不會被 24h 清理刪掉）。

**開始錄影前**會先檢查 NAS 路徑的根（如 `Z:\` 或 `\\server\share\`）是否存在（`utils/save_location.py`）；不存在就跳一次對話框，讓使用者選「改存桌面」/「選擇資料夾…」/「取消錄影」，並自動把存檔位置切到「自訂」。若錄影途中 NAS 才斷線，停錄後所有相機的複製失敗會彙整成**一個**視窗（不再每台各跳一次）。開機時 `cleanup_staging_dir()` 清 >24h 的 staging 孤兒檔。

---

## 6. 重要設定點

| 要改什麼 | 在哪 |
|---|---|
| go2rtc 開關 | `utils/stream_thread.py` → `USE_GO2RTC` |
| 相機 IP ↔ 名稱對應 | `main.py`（`Ptz01~08`）、`ptz_control/camera_tab_control.py` → `_init_camera_map`（另有 `Rot01~20` / `Sti01~06`）、`go2rtc.yaml`（三處要一致） |
| RTSP / ONVIF 帳密 | `config.ini`（或環境變數 `PTZ_CAMERA_USER` / `PTZ_CAMERA_PASS`），由 `utils/app_config.py` 統一讀取；見 [2-4](#2-4-相機帳密必要) |
| 預設 NAS 路徑 | UI 上「存檔位置」欄位；預設字串在 `multi_camera_tab.py` / `camera_tab_control.py` 的 `r"Z:\PTZ recordings"` |
| 預覽 fps / 尺寸 | 建 `StreamThread` 時的 `ui_fps` / `preview_size` / `use_substream` |
| 回放顯示 fps | `playback_tab.py` → `DISPLAY_FPS` |

---

## 7. 打包

**用途**：把程式包成單一 `dist\PtzRecorder.exe`（約 178 MB，內含 Python、所有套件、ffmpeg/ffprobe/go2rtc、wsdl、icons），複製到**沒有開發環境的電腦**（例如場館錄影機）雙擊即可執行。自己開發測試用 F5 / `start.bat` 即可，不需要打包；改了程式碼要重新打包，目標電腦上的 exe 才會更新。

```bat
.\packing.bat      REM = (.venv / 已啟用的 conda 環境 / py -3.14) -m PyInstaller --clean --noconfirm main.spec
```

- 約 2 分鐘。需要 Python 3.14 環境且已裝 `pyinstaller`（`requirements.txt` 已包含）。
- 若專案根目錄有 `config.ini`，會一併打包進 exe；執行時也會**優先讀 exe 旁邊的 `config.ini`**，所以換帳密不用重新打包。⚠️ 打包進帳密的 exe 不要公開散布。
- `main.spec` 是 **onefile**（單一 `dist\PtzRecorder.exe`，執行時自解壓到暫存）。
- **打包前** `ffmpeg.exe` / `ffprobe.exe` / `go2rtc.exe` 要先放在專案根目錄（`main.spec` 的 `datas` 會把它們塞進 exe）。
- `main.spec` 明確 `excludes` 掉 `torch` / `ultralytics` / `tensorflow` 等（純錄影版）。之後要打包含 YOLO 的版本要改這裡。
- exe 圖示用 `icons/video-camera.ico`（由 `video-camera.png` 轉出，含 16~256px）。不要改回 `.png`：PyInstaller 需要另裝 Pillow 才能轉 png，沒裝會直接打包失敗。
- `WARNING: Hidden import "sip" not found!` 可忽略（PyQt5 用的是 `PyQt5.sip`）。
- 已驗證（2026-11）：exe 複製到空資料夾單獨執行，8 台相機經內建 go2rtc 正常連線。

---

## 8. UI 檔維護（重要）

| 檔案 | 能否用 pyuic5 重生 |
|---|---|
| `main_window_ui.py` | ✅ 可以（`compile.bat` 會從 `main_window.ui` 重生） |
| `ptz_control/ptz_control_ui.py` | ❌ **不行，已改成手動維護** |

`ptz_control_ui.py` 原本是 pyuic5 產生的，但之後大量手改（`right_layout`、`record_layout`、錄影紅點、存檔面板掛載點…），對應的 `ptz_control.ui` 已與它脫節並移除。**要改這個版面直接編輯 `.py`**，跑 `pyuic5` 會把手加的東西全部蓋掉。

---

## 9. YOLO 預留接口

`playback_tab.py`：

```python
tab.set_annotate_hook(fn)   # fn: (frame_bgr: np.ndarray, cam_name: str) -> np.ndarray
```

每幀顯示前會呼叫 `fn`，回傳的 frame 取代原 frame 畫上 UI。目前沒有任何地方 set，所以不會跑。要接 YOLO：實作一個 hook（載模型、`model.predict`、畫框），在 `main.py` 建好 `playback_tab` 後呼叫 `set_annotate_hook`。dGPU/CUDA 與否取決於你裝的 `torch` build。

---

## 10. 已知限制 / TODO

- YOLO 尚未實作（見上）。
- 錄影參數 `target_fps` / `frame_size` / `fourcc` 在 `-c copy` 下無效，簽名待精簡。
- `enlarge_tab.py` 放大視窗走「輸出原始解析度」的預覽路徑（pipe ~90 MB/s），可考慮給 `preview_size=(1280,720)`。
- 相機清單 `Rot* / Sti* / Ptz*` 寫死在多個檔。

---

## 11. 檔案結構

```
main.py                     進入點；建三個分頁、啟動/收 go2rtc、GC 調校
main_window.ui / _ui.py     外框（QTabWidget），可 pyuic5 重生
compile.bat                 只重生 main_window_ui.py（勿碰 ptz_control_ui.py）
install.bat / start.bat     用 uv 建 .venv (Python 3.14) / 啟動
packing.bat / main.spec     PyInstaller onefile 打包
config.example.ini          相機帳密範本 → 複製成 config.ini（不進版控）
.vscode/                    VS Code 啟動設定（launch.json）與建議擴充套件

playback_tab.py             同步播放分頁（含 _CameraDecoder：每路一支解碼 ffmpeg）
enlarge_tab.py              多機分頁點格子後的放大視窗

ptz_control/
  camera_tab_control.py     單機攝影機分頁
  multi_camera_tab.py       多機攝影機分頁
  ptz_control.py            ONVIF PTZ 控制（AbsoluteMove）
  ptz_control_ui.py         單機分頁版面（手動維護！）

utils/
  stream_thread.py          StreamThread + camera_worker_process + go2rtc 啟停 + NAS staging
  onvif_sync.py             go2rtc 模式的多相機起錄同步（SetSynchronizationPoint）
  app_config.py             讀 config.ini / 環境變數取得相機帳密
  icons_utils.py            resource_path（開發 / PyInstaller 兩用）
  ui_utils.py               _ensure_pixitem

wsdl/                        ONVIF WSDL（onvif_zeep 需要，路徑顯式指過去）
icons/                       按鈕圖示 PNG
go2rtc.yaml                  go2rtc restream 設定（相機名 ↔ RTSP URL；帳密用 ${PTZ_CAMERA_USER}/${PTZ_CAMERA_PASS}）
```
