# -*- mode: python ; coding: utf-8 -*-
# 純錄影版 PyInstaller spec：不打 YOLO，所以拔掉 torch / ultralytics
# Build: pyinstaller main.spec  → 單檔 dist/PtzRecorder.exe（onefile，自我解壓到暫存後執行）

from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

datas = [
    ('ffmpeg.exe',    '.'),
    ('ffprobe.exe',   '.'),
    ('go2rtc.exe',    '.'),   # USE_GO2RTC=True 需要：本機 restream，相機只連一次再分流給預覽+錄影
    ('go2rtc.yaml',   '.'),   # go2rtc 的串流設定（Ptz01..Ptz08 → 各相機 RTSP）
    ('wsdl',          'wsdl'),
    ('icons',         'icons'),
    ('main_window.ui', '.'),
]

# 相機帳密：有 config.ini 才打包進 exe（執行時也會優先讀 exe 旁邊的 config.ini）
import os as _os
if _os.path.isfile('config.ini'):
    datas.append(('config.ini', '.'))

# onvif 透過 zeep 做動態 WSDL/transport 載入，PyInstaller 看不到，要手動 collect
hiddenimports = collect_submodules('onvif')
hiddenimports += collect_submodules('zeep')

# cv2 的 opencv_world*.dll 不一定被掃到，明確 collect
binaries = collect_dynamic_libs('cv2')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 純錄影版完全不用，excludes 才能讓 PyInstaller 連 transitive deps 也跳過
        'torch', 'torchvision', 'torchaudio', 'ultralytics',
        'tensorflow', 'tensorboard', 'keras',
        'matplotlib', 'pandas', 'scipy', 'sklearn', 'PIL.ImageQt',
        'PySimpleGUI',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# onefile：把 binaries + datas 全部塞進單一 exe（不要 COLLECT）。
# 執行時 PyInstaller 會把內含的 ffmpeg/ffprobe/go2rtc 等解壓到暫存資料夾再跑，
# sys._MEIPASS 指向該暫存目錄，resource_path 仍能找到它們。
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PtzRecorder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icons/video-camera.ico'],   # 用 .ico：給 .png 的話 PyInstaller 需另裝 Pillow 才能轉，否則打包失敗
)
