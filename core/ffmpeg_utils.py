"""Утилиты вокруг FFmpeg: поиск бинарей, ffprobe, реальная проверка NVENC,
запуск с парсингом прогресса. Используется render/transcribe/audio/preview.

Никакого Qt. Логи через стандартный logging.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger("clip_polisher.ffmpeg")


# --------------------------------------------------------------------------
# Поиск бинарей (можно переопределить через переменные окружения)
# --------------------------------------------------------------------------

def _bundled(name: str) -> Optional[str]:
    """Путь к ffmpeg/ffprobe, вложенному в собранный .exe (PyInstaller), если есть."""
    import sys
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        for cand in (os.path.join(base, "ffmpeg", name),
                     os.path.join(os.path.dirname(sys.executable), "ffmpeg", name)):
            if os.path.isfile(cand):
                return cand
    return None


def ffmpeg_bin() -> str:
    return (os.environ.get("CLIP_FFMPEG") or _bundled("ffmpeg.exe")
            or shutil.which("ffmpeg") or "ffmpeg")


def ffprobe_bin() -> str:
    return (os.environ.get("CLIP_FFPROBE") or _bundled("ffprobe.exe")
            or shutil.which("ffprobe") or "ffprobe")


# Скрыть окно консоли на Windows при вызове subprocess.
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# --------------------------------------------------------------------------
# ffprobe
# --------------------------------------------------------------------------

@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float          # секунды
    fps: float
    has_audio: bool


def probe_video(path: str) -> VideoInfo:
    """Получить размеры/длительность/fps/наличие звука через ffprobe."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Файл не найден: {path}")
    cmd = [
        ffprobe_bin(), "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace",
                         creationflags=_CREATE_NO_WINDOW)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe упал: {out.stderr.strip()}")
    data = json.loads(out.stdout)

    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if v is None:
        raise RuntimeError("В файле нет видео-потока")
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))

    width = int(v["width"])
    height = int(v["height"])

    # fps из avg_frame_rate ("30000/1001").
    fps = 30.0
    afr = v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1"
    try:
        num, den = afr.split("/")
        if float(den) != 0:
            fps = float(num) / float(den)
    except Exception:
        pass

    # Длительность: у потока или у контейнера.
    duration = 0.0
    for src in (v.get("duration"), data.get("format", {}).get("duration")):
        try:
            if src is not None:
                duration = float(src)
                break
        except (TypeError, ValueError):
            continue

    return VideoInfo(width=width, height=height, duration=duration, fps=fps, has_audio=has_audio)


# --------------------------------------------------------------------------
# Реальная проверка NVENC (не просто наличие в -encoders!)
# --------------------------------------------------------------------------

_nvenc_cache: Optional[bool] = None


def nvenc_available(force: bool = False) -> bool:
    """Проверить h264_nvenc РЕАЛЬНЫМ тест-энкодом (2 кадра).

    Наличие энкодера в `-encoders` НЕ гарантирует работу: он может быть
    заблокирован версией драйвера. Поэтому кодируем настоящий кадр.
    Результат кэшируется в рамках процесса.
    """
    global _nvenc_cache
    if _nvenc_cache is not None and not force:
        return _nvenc_cache

    tmp = os.path.join(tempfile.gettempdir(), f"clip_polisher_nvenc_test_{os.getpid()}.mp4")
    cmd = [
        ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=256x256:r=30:d=0.1",
        "-c:v", "h264_nvenc", "-frames:v", "2", tmp,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           creationflags=_CREATE_NO_WINDOW, timeout=30)
        ok = r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0
        if not ok:
            log.warning("h264_nvenc недоступен (тест-энкод не прошёл): %s",
                        (r.stderr or "").strip().splitlines()[-1:] or "")
    except Exception as e:
        ok = False
        log.warning("h264_nvenc проверка упала: %r", e)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass

    _nvenc_cache = ok
    log.info("NVENC (h264_nvenc) доступен: %s", ok)
    return ok


# --------------------------------------------------------------------------
# Запуск ffmpeg с парсингом прогресса (-progress pipe:1)
# --------------------------------------------------------------------------

ProgressCb = Callable[[float], None]  # получает долю выполнения 0.0..1.0


def make_filmstrip(input_path: str, out_png: str, n: int = 12,
                   cell_w: int = 120, duration: float = 0.0) -> str:
    """Собрать «киноленту» — N равномерных стоп-кадров клипа в одну картинку (tile).

    Используется интерактивным таймлайном обрезки как фон дорожки. Один вызов
    ffmpeg (fps→scale→tile). Возвращает путь к PNG.
    """
    if not os.path.isfile(input_path):
        raise FileNotFoundError(input_path)
    if duration <= 0:
        duration = probe_video(input_path).duration
    duration = max(0.1, duration)
    fps = n / duration                       # ~N кадров на всю длину
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    vf = f"fps={fps:.6f},scale={cell_w}:-2,tile={n}x1"
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
           "-i", input_path, "-vf", vf, "-frames:v", "1", "-update", "1", out_png]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=_CREATE_NO_WINDOW, timeout=120)
    if r.returncode != 0 or not os.path.isfile(out_png):
        raise RuntimeError(f"Не удалось собрать киноленту: {(r.stderr or '').strip()}")
    return out_png


def run_ffmpeg(args: list[str], total_duration: float = 0.0,
               on_progress: Optional[ProgressCb] = None) -> None:
    """Запустить ffmpeg. Если задан on_progress и total_duration>0 — парсить
    `-progress pipe:1` и звать колбэк с долей 0..1.

    args — аргументы БЕЗ имени бинаря и без `-progress` (добавим сами).
    Бросает RuntimeError при ненулевом коде возврата.
    """
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostats", "-loglevel", "error"]
    if on_progress is not None:
        cmd += ["-progress", "pipe:1"]
    cmd += args

    log.debug("ffmpeg cmd: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=_CREATE_NO_WINDOW,
    )

    if on_progress is not None and proc.stdout is not None:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_ms=") and total_duration > 0:
                try:
                    us = int(line.split("=", 1)[1])
                    frac = min(1.0, max(0.0, (us / 1_000_000.0) / total_duration))
                    on_progress(frac)
                except (ValueError, ZeroDivisionError):
                    pass
            elif line == "progress=end":
                on_progress(1.0)

    stderr = proc.stderr.read() if proc.stderr else ""
    ret = proc.wait()
    if ret != 0:
        raise RuntimeError(f"ffmpeg завершился с кодом {ret}:\n{stderr.strip()}")
