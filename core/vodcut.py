"""vodcut.py — Этап 3, блок 6: качаем ТОЛЬКО куски записи, а не весь стрим.

Смысл всего Этапа 3: «вставил ссылку → программа сама нашла моменты → ты выбрал →
качаются ровно выбранные окна». Весь VOD 1080p60 за 2 часа — это 6,8 ГБ; шесть окон
по 40 секунд — около 60 МБ. Замер проекта (2026-07-27): кусок 20 с в 1080p60 = 5,3 МБ
и качается 13 секунд.

Здесь только загрузка кусков. Разбор (звук/речь) живёт в `media.py` и берёт лишь
звуковую дорожку — эти два пути намеренно не смешиваются: анализ не должен тянуть видео.

Куски кладём в тот же кэш, что и дорожки (`%LOCALAPPDATA%\\ClipPolisher\\cache`), под
именем `cut_<vod>_<начало>-<конец>_<качество>.mp4`. Повторный экспорт того же момента
не качает ничего — просто берём готовый файл.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

from core.media import (MediaError, _explain_ytdlp, _hms, cache_dir, probe_duration,
                        trim_cache)

Progress = Callable[[str], None]
ShouldStop = Callable[[], bool]

# Лесенки форматов Twitch: сначала желаемый, потом что помельче, потом хоть что-нибудь.
# Если у записи нет 1080p60 (стример вещал в 720), yt-dlp просто возьмёт следующий.
QUALITY_LADDER = {
    "1080p60": "1080p60/1080p/720p60/720p/best",
    "720p60": "720p60/720p/480p/best",
    "480p": "480p/360p/worst",
    "160p": "160p/360p/worst",
}
# Сколько мегабайт в секунде — ЗАМЕРЕНО на реальном VOD, а не взято из заявленного
# битрейта формата (он завышен почти втрое). Нужно, чтобы честно сказать пользователю,
# сколько всего скачается, ДО начала загрузки.
MB_PER_SEC = {"1080p60": 0.27, "720p60": 0.13, "480p": 0.06, "160p": 0.02}

DEFAULT_QUALITY = "1080p60"
# Запас по краям: границы окна пользователь ещё будет двигать в редакторе, и лучше
# иметь лишнюю секунду с обеих сторон, чем обрезанную реплику.
PAD = 1.0


class Cancelled(RuntimeError):
    """Пользователь нажал «Стоп» во время загрузки."""


@dataclass
class ClipPiece:
    """Скачанный кусок записи: файл + где он был во времени стрима."""
    path: str
    start: float                 # начало куска во времени СТРИМА (с учётом запаса)
    end: float
    duration: float = 0.0        # фактическая длина файла
    size_mb: float = 0.0
    from_cache: bool = False
    quality: str = DEFAULT_QUALITY

    def local(self, t_stream: float) -> float:
        """Время стрима → позиция внутри скачанного файла."""
        return max(0.0, t_stream - self.start)


def estimate_mb(seconds: float, quality: str = DEFAULT_QUALITY) -> float:
    """Сколько примерно скачается. Честная оценка по замерам, не по битрейту формата."""
    return max(0.0, seconds) * MB_PER_SEC.get(quality, MB_PER_SEC[DEFAULT_QUALITY])


def human_size(mb: float) -> str:
    return f"{mb / 1024:.1f} ГБ" if mb >= 1024 else f"{mb:.0f} МБ"


def window_path(vod_id: str, start: float, end: float,
                quality: str = DEFAULT_QUALITY) -> str:
    """Куда лёг бы этот кусок. Имя — по секундам, чтобы кэш попадал сам собой."""
    name = f"cut_{vod_id or 'local'}_{int(start)}-{int(end)}_{quality}.mp4"
    return os.path.join(cache_dir(), name)


def cached_window(vod_id: str, start: float, end: float,
                  quality: str = DEFAULT_QUALITY) -> Optional[str]:
    """Готовый кусок с прошлого раза (огрызок недокачки за кусок не считаем)."""
    path = window_path(vod_id, start, end, quality)
    try:
        if os.path.getsize(path) > 64 * 1024:
            return path
    except OSError:
        pass
    return None


def fetch_window(vod_id: str, start: float, end: float, url: str = "",
                 quality: str = DEFAULT_QUALITY, pad: float = PAD,
                 progress: Optional[Progress] = None,
                 should_stop: Optional[ShouldStop] = None) -> ClipPiece:
    """Скачать один кусок записи [start, end] (плюс запас по краям).

    `--force-keyframes-at-cuts` — края перекодируются, зато границы точные: без него
    кусок начинался бы с ближайшего ключевого кадра, то есть «как повезёт», до пары
    секунд мимо.
    """
    say = progress or (lambda _s: None)
    stop = should_stop or (lambda: False)
    # Проверяем ДО начала, а не только в прогрессе загрузки: иначе нажатие «Стоп»
    # между кусками не мешало следующему куску начать качаться.
    if stop():
        raise Cancelled()
    a = max(0.0, start - pad)
    b = max(a + 1.0, end + pad)

    hit = cached_window(vod_id, a, b, quality)
    if hit:
        say(f"Кусок {_hms(a)}–{_hms(b)} уже скачан — беру готовый")
        return ClipPiece(path=hit, start=a, end=b, duration=probe_duration(hit),
                         size_mb=os.path.getsize(hit) / (1024 * 1024),
                         from_cache=True, quality=quality)

    try:
        import yt_dlp
        from yt_dlp.utils import download_range_func
    except ImportError:
        raise MediaError("Не найден компонент загрузки (yt-dlp). Переустанови "
                         "программу — он входит в комплект.") from None

    out = window_path(vod_id, a, b, quality)
    target = url or f"https://www.twitch.tv/videos/{vod_id}"
    state = {"pct": -20.0}

    def hook(d: dict) -> None:
        if stop():
            raise Cancelled()
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        got = d.get("downloaded_bytes") or 0
        if not total:
            return
        pct = got / total * 100.0
        if pct - state["pct"] >= 20.0:
            state["pct"] = pct
            say(f"Качаю кусок {_hms(a)}–{_hms(b)}: {pct:.0f}%")

    opts = {
        "format": QUALITY_LADDER.get(quality, QUALITY_LADDER[DEFAULT_QUALITY]),
        # Расширение фиксируем: у Twitch кусок приходит mp4, а гадать по %(ext)s
        # потом при поиске файла — лишний источник ошибок.
        "outtmpl": out,
        "download_ranges": download_range_func(None, [(a, b)]),
        "force_keyframes_at_cuts": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "concurrent_fragment_downloads": 4,
        "retries": 5,
        "fragment_retries": 10,
        "continuedl": True,
        "overwrites": True,
    }
    say(f"Качаю кусок {_hms(a)}–{_hms(b)} ({human_size(estimate_mb(b - a, quality))})…")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([target])
    except Cancelled:
        _remove(out)
        raise
    except Exception as e:                       # noqa: BLE001 — сообщения yt-dlp разные
        _remove(out)
        raise MediaError(_explain_ytdlp(str(e), what="кусок записи")) from None

    if not os.path.isfile(out) or os.path.getsize(out) < 64 * 1024:
        raise MediaError(f"Кусок {_hms(a)}–{_hms(b)} не скачался. Если запись только что "
                         f"закончилась, Twitch иногда отдаёт её не сразу — попробуй позже.")
    dur = probe_duration(out)
    size = os.path.getsize(out) / (1024 * 1024)
    say(f"Кусок готов: {size:.1f} МБ, {_hms(dur)}")
    return ClipPiece(path=out, start=a, end=b, duration=dur or (b - a),
                     size_mb=size, quality=quality)


def fetch_windows(vod_id: str, windows: list[tuple[float, float]], url: str = "",
                  quality: str = DEFAULT_QUALITY, pad: float = PAD,
                  progress: Optional[Progress] = None,
                  should_stop: Optional[ShouldStop] = None) -> list[ClipPiece]:
    """Скачать все выбранные окна подряд. Возвращает куски в том же порядке.

    Кэш чистим ОДИН раз в конце: иначе на длинном списке потолок кэша мог бы снести
    кусок, который только что скачали для этой же нарезки.
    """
    say = progress or (lambda _s: None)
    total_sec = sum(max(0.0, b - a) + 2 * pad for a, b in windows)
    say(f"Нужно скачать {len(windows)} кусков — примерно "
        f"{human_size(estimate_mb(total_sec, quality))}")
    pieces: list[ClipPiece] = []
    for i, (a, b) in enumerate(windows, 1):
        say(f"Кусок {i} из {len(windows)}")
        pieces.append(fetch_window(vod_id, a, b, url=url, quality=quality, pad=pad,
                                   progress=say, should_stop=should_stop))
    trim_cache()
    return pieces


def _remove(path: str) -> None:
    """Убрать огрызок недокачки, чтобы он не выдал себя за готовый кусок."""
    for p in (path, path + ".part", path + ".ytdl"):
        try:
            os.remove(p)
        except OSError:
            pass


__all__ = ["Cancelled", "ClipPiece", "DEFAULT_QUALITY", "MB_PER_SEC", "QUALITY_LADDER",
           "cached_window", "estimate_mb", "fetch_window", "fetch_windows",
           "human_size", "window_path"]
