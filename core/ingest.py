"""ingest.py — приём входа: локальный файл ИЛИ ссылка на Twitch-клип (yt-dlp).

Возвращает путь к локальному mp4, готовому к дальнейшей обработке.
Никакого Qt. Прогресс скачивания — через опциональный колбэк.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Callable, Optional

log = logging.getLogger("clip_polisher.ingest")

# URL (http/https) vs локальный путь.
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def is_url(source: str) -> bool:
    return bool(_URL_RE.match(source.strip()))


def is_twitch_clip_url(source: str) -> bool:
    """Ссылка именно на Twitch-КЛИП (не VOD/канал).

    Форматы клипов:
      https://www.twitch.tv/<channel>/clip/<SlugId>
      https://clips.twitch.tv/<SlugId>
    """
    s = source.strip().lower()
    return ("clips.twitch.tv/" in s) or ("twitch.tv/" in s and "/clip/" in s)


ProgressCb = Callable[[float], None]  # доля 0..1 (может быть неточной у Twitch)


def ingest(source: str, dest_dir: str = "tests/sample_clips",
           on_progress: Optional[ProgressCb] = None) -> str:
    """Получить локальный mp4 из источника.

    - Локальный существующий файл → возвращается как есть.
    - URL → скачивается через yt-dlp в dest_dir, возвращается путь к файлу.
    """
    source = source.strip()

    if not is_url(source):
        if os.path.isfile(source):
            log.info("Локальный файл: %s", source)
            return source
        raise FileNotFoundError(f"Файл не найден и это не URL: {source}")

    if is_twitch_clip_url(source):
        log.info("Twitch-клип по ссылке: %s", source)
    else:
        log.warning("URL не распознан как Twitch-клип, пробую скачать всё равно: %s", source)

    return _download_with_ytdlp(source, dest_dir, on_progress)


def _download_with_ytdlp(url: str, dest_dir: str,
                         on_progress: Optional[ProgressCb]) -> str:
    """Скачать URL через yt-dlp (Python API). Вернуть путь к mp4."""
    import yt_dlp

    os.makedirs(dest_dir, exist_ok=True)

    result_holder: dict[str, str] = {}

    def hook(d: dict) -> None:
        if d.get("status") == "downloading" and on_progress is not None:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                on_progress(min(1.0, done / total))
        elif d.get("status") == "finished":
            if on_progress is not None:
                on_progress(1.0)

    ydl_opts = {
        # Twitch-клипы обычно уже mp4 (h264+aac). Берём лучшее качество.
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(dest_dir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,  # свой прогресс-бар yt-dlp не печатаем (есть hook)
        "progress_hooks": [hook],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # Итоговый путь после возможного merge.
        path = ydl.prepare_filename(info)
        base, _ = os.path.splitext(path)
        # merge_output_format мог сменить расширение на .mp4.
        if not os.path.isfile(path) and os.path.isfile(base + ".mp4"):
            path = base + ".mp4"

    if not os.path.isfile(path):
        raise RuntimeError(f"yt-dlp не оставил файл по ожидаемому пути: {path}")

    log.info("Скачано: %s (%.1f МБ)", path, os.path.getsize(path) / 1e6)
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Скачать/принять входной клип")
    ap.add_argument("source", help="локальный файл или URL Twitch-клипа")
    ap.add_argument("--dest", default="tests/sample_clips", help="папка для скачивания")
    args = ap.parse_args()

    def prog(frac: float) -> None:
        print(f"\rСкачивание: {frac*100:5.1f}%", end="", flush=True)

    path = ingest(args.source, args.dest, on_progress=prog)
    print(f"\nГотово: {path}")


if __name__ == "__main__":
    _main()
