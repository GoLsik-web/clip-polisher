"""provision.py — первый запуск: докачка тяжёлых частей в папку пользователя.

Установщик КОМПАКТНЫЙ (без модели и без CUDA — иначе не влезает в лимит GitHub 2 ГБ).
При первом запуске сюда докачиваются:
  * модель Whisper large-v3 (~3 ГБ) — с HuggingFace;
  * GPU-библиотеки CUDA (cuBLAS/cuDNN/nvrtc, ~2 ГБ) — колёса с PyPI (только если есть NVIDIA).

Всё кладётся в %LOCALAPPDATA%\\ClipPolisher\\ и живёт там между запусками. Никакого Qt.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from typing import Callable, Optional

log = logging.getLogger("clip_polisher.provision")

APP_NAME = "ClipPolisher"
MODEL_REPO = "Systran/faster-whisper-large-v3"
# Колёса CUDA для ctranslate2 (CUDA 12) — ТОЧНЫЕ версии, проверенные с
# ctranslate2 4.8.1 (те, что стоят на dev-машине и реально работают на GPU).
# (пакет, версия). cuda-runtime не нужен — cudart идёт вместе с cublas.
CUDA_PACKAGES = [
    ("nvidia-cublas-cu12", "12.9.2.10"),
    ("nvidia-cudnn-cu12", "9.24.0.43"),
    ("nvidia-cuda-nvrtc-cu12", "12.9.86"),
]

ProgressCb = Callable[[float, str], None]   # (доля 0..1, текст стадии)
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# --------------------------------------------------------------------------
# Пути
# --------------------------------------------------------------------------

def app_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def models_dir() -> str:
    d = os.path.join(app_data_dir(), "models")
    os.makedirs(d, exist_ok=True)
    return d


def model_path() -> str:
    """Локальная папка модели large-v3 (куда качаем снапшот с HF)."""
    return os.path.join(models_dir(), "faster-whisper-large-v3")


def cuda_dir() -> str:
    d = os.path.join(app_data_dir(), "cuda")
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# Проверки готовности
# --------------------------------------------------------------------------

def model_ready() -> bool:
    mp = model_path()
    return os.path.isfile(os.path.join(mp, "model.bin"))


def cuda_ready() -> bool:
    """Есть ли распакованные CUDA-DLL (ищем cublas и cudnn)."""
    have_cublas = have_cudnn = False
    for root, _dirs, files in os.walk(cuda_dir()):
        for f in files:
            fl = f.lower()
            if fl.startswith("cublas64"):
                have_cublas = True
            if fl.startswith("cudnn") and fl.endswith(".dll"):
                have_cudnn = True
    return have_cublas and have_cudnn


def has_nvidia() -> bool:
    """Груба: есть ли nvidia-smi (значит есть драйвер/видеокарта NVIDIA)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        # стандартный путь драйвера
        cand = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "nvidia-smi.exe")
        exe = cand if os.path.isfile(cand) else None
    if not exe:
        return False
    try:
        r = subprocess.run([exe], capture_output=True, timeout=10,
                           creationflags=_CREATE_NO_WINDOW)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def needs_provision() -> bool:
    """Нужно ли что-то докачивать при старте."""
    if not model_ready():
        return True
    if has_nvidia() and not cuda_ready():
        return True
    return False


# --------------------------------------------------------------------------
# Загрузка
# --------------------------------------------------------------------------

def _download(url: str, dst: str, on_bytes: Optional[Callable[[int, int], None]] = None) -> None:
    """Скачать url в dst атомарно (через .part) с колбэком (получено, всего)."""
    req = urllib.request.Request(url, headers={"User-Agent": "ClipPolisher"})
    part = dst + ".part"
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        got = 0
        with open(part, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)   # 1 МБ
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if on_bytes:
                    on_bytes(got, total)
    os.replace(part, dst)   # готово целиком → только теперь занимаем итоговое имя


def _pypi_win_wheel_url(pkg: str, version: Optional[str] = None) -> Optional[str]:
    """URL win_amd64-колеса пакета на PyPI. version=None → последняя версия."""
    api = (f"https://pypi.org/pypi/{pkg}/{version}/json" if version
           else f"https://pypi.org/pypi/{pkg}/json")
    req = urllib.request.Request(api, headers={"User-Agent": "ClipPolisher"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    for f in data.get("urls", []):
        if f.get("filename", "").endswith("win_amd64.whl"):
            return f.get("url")
    return None


def ensure_cuda(on_progress: Optional[ProgressCb] = None) -> None:
    """Скачать колёса CUDA с PyPI и распаковать DLL в cuda_dir (структура nvidia/*/bin)."""
    if cuda_ready():
        return
    n = len(CUDA_PACKAGES)
    tmp = tempfile.mkdtemp(prefix="clip_cuda_")
    try:
        for i, (pkg, ver) in enumerate(CUDA_PACKAGES):
            base = i / n
            if on_progress:
                on_progress(base, f"GPU-библиотеки: {pkg}")
            url = _pypi_win_wheel_url(pkg, ver)
            if not url:
                log.warning("Нет win-колеса для %s — пропускаю", pkg)
                continue
            whl = os.path.join(tmp, os.path.basename(url))
            def cb(got, total, _b=base, _p=pkg):
                if on_progress and total:
                    frac = _b + (got / total) / n
                    on_progress(min(0.999, frac), f"GPU-библиотеки: {_p} "
                                f"{got//(1<<20)}/{total//(1<<20)} МБ")
            _download(url, whl, cb)
            # колесо = zip; берём только nvidia/**/bin/*.dll (+ lib)
            with zipfile.ZipFile(whl) as z:
                for name in z.namelist():
                    low = name.lower()
                    if low.endswith(".dll") and "/bin/" in low.replace("\\", "/"):
                        target = os.path.join(cuda_dir(), name.replace("\\", "/"))
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with z.open(name) as src, open(target, "wb") as out:
                            shutil.copyfileobj(src, out)
            os.remove(whl)
        if on_progress:
            on_progress(1.0, "GPU-библиотеки готовы")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


HF_BASE = "https://huggingface.co"


def _hf_file_list(repo: str) -> list[str]:
    """Список файлов модели в HF-репозитории (без сторонних либ и без tqdm)."""
    url = f"{HF_BASE}/api/models/{repo}"
    req = urllib.request.Request(url, headers={"User-Agent": "ClipPolisher"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return [s.get("rfilename", "") for s in data.get("siblings", []) if s.get("rfilename")]


def ensure_model(on_progress: Optional[ProgressCb] = None) -> str:
    """Скачать модель large-v3 напрямую с HuggingFace в model_path().

    Качаем сами (urllib) — БЕЗ huggingface_hub/tqdm: в оконном .exe tqdm пишет в
    stderr=None и роняет приложение. Даём реальный прогресс в МБ.
    """
    if model_ready():
        return model_path()
    if on_progress:
        on_progress(0.0, "Скачиваю модель распознавания (~3 ГБ), это один раз…")

    files = [f for f in _hf_file_list(MODEL_REPO)
             if f.endswith((".bin", ".json", ".txt", ".model")) or "tokenizer" in f.lower()]
    if not any(f.endswith("model.bin") for f in files):
        raise RuntimeError("В репозитории модели не найден model.bin")
    os.makedirs(model_path(), exist_ok=True)

    # Мелкие файлы (конфиги/токенайзер) — быстро, отдаём им 0..8% прогресса;
    # model.bin (~3 ГБ) — основное, 8..100%.
    small = [f for f in files if not f.endswith("model.bin")]
    for i, fn in enumerate(small):
        if on_progress:
            on_progress(0.08 * i / max(1, len(small)), f"Модель: {fn}")
        _download(f"{HF_BASE}/{MODEL_REPO}/resolve/main/{fn}",
                  _model_dst(fn), None)

    def big_cb(got, total):
        if on_progress and total:
            on_progress(0.08 + 0.92 * (got / total),
                        f"Модель: {got // (1 << 20)}/{total // (1 << 20)} МБ")
    _download(f"{HF_BASE}/{MODEL_REPO}/resolve/main/model.bin",
              _model_dst("model.bin"), big_cb)

    if on_progress:
        on_progress(1.0, "Модель готова")
    return model_path()


def _model_dst(rel: str) -> str:
    dst = os.path.join(model_path(), rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    return dst


def ensure_runtime(on_progress: Optional[ProgressCb] = None,
                   want_gpu: bool = True) -> None:
    """Докачать всё нужное для первого запуска: модель (+CUDA, если есть NVIDIA)."""
    do_cuda = want_gpu and has_nvidia() and not cuda_ready()

    def stage(frac_lo, frac_hi):
        def cb(f, s):
            if on_progress:
                on_progress(frac_lo + (frac_hi - frac_lo) * f, s)
        return cb

    if do_cuda:
        ensure_cuda(stage(0.0, 0.4))
        ensure_model(stage(0.4, 1.0))
    else:
        ensure_model(stage(0.0, 1.0))
    if on_progress:
        on_progress(1.0, "Готово к работе")


# --------------------------------------------------------------------------
# Интеграция с transcribe: пути к DLL и модели
# --------------------------------------------------------------------------

def add_cuda_to_path() -> None:
    """Добавить скачанные CUDA-DLL в путь поиска (для ctranslate2)."""
    if os.name != "nt":
        return
    for root, dirs, _files in os.walk(cuda_dir()):
        if os.path.basename(root).lower() == "bin":
            try:
                os.add_dll_directory(root)
                os.environ["PATH"] = root + os.pathsep + os.environ.get("PATH", "")
            except OSError:
                pass
