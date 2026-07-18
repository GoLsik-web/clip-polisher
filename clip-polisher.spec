# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спека «Полировщик клипов» (onedir).

Собирает КОМПАКТНЫЙ билд: приложение + PySide6(Widgets) + faster-whisper/ctranslate2
+ ffmpeg. НЕ включает CUDA-библиотеки и модель Whisper — они докачиваются при первом
запуске (core/provision.py). Так установщик влезает в лимит GitHub 2 ГБ.
"""
import os
import shutil
from PyInstaller.utils.hooks import (collect_data_files, collect_dynamic_libs,
                                     collect_submodules)


# --- ffmpeg/ffprobe (полный билд Gyan.FFmpeg из winget) --------------------
def _ffmpeg_dir():
    cand = shutil.which("ffmpeg")
    if cand:
        try:
            item = os.path.realpath(cand)
            if os.path.isfile(item):
                return os.path.dirname(item)
        except OSError:
            pass
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    for root, _dirs, files in os.walk(base):
        if "ffmpeg.exe" in files and os.path.basename(root) == "bin":
            return root
    raise SystemExit("Не найден ffmpeg — установи Gyan.FFmpeg (winget install Gyan.FFmpeg)")


FFDIR = _ffmpeg_dir()

datas = [("assets", "assets")]
datas += collect_data_files("faster_whisper")     # silero VAD onnx + токенайзеры
datas += [(os.path.join(FFDIR, "ffmpeg.exe"), "ffmpeg"),
          (os.path.join(FFDIR, "ffprobe.exe"), "ffmpeg")]

binaries = []
binaries += collect_dynamic_libs("ctranslate2")
binaries += collect_dynamic_libs("av")
binaries += collect_dynamic_libs("onnxruntime")

hiddenimports = ["ctranslate2", "av", "onnxruntime", "tokenizers", "huggingface_hub"]
hiddenimports += collect_submodules("faster_whisper")

# Не тащим CUDA (докачивается) и заведомо ненужные тяжёлые модули.
excludes = [
    "nvidia",           # CUDA-библиотеки — качаются при первом запуске
    "torch", "tensorflow", "matplotlib", "scipy", "pandas", "tkinter",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngine",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtQuick",
    "PySide6.QtQml", "PySide6.QtQuick3D", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtQuickWidgets", "PySide6.QtDesigner", "PySide6.QtSql",
    "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtNetworkAuth", "PySide6.QtRemoteObjects",
    "PySide6.QtTextToSpeech", "PySide6.QtWebSockets",
]

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ClipPolisher",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=None,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="ClipPolisher",
)
