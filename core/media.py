"""media.py — Этап 3, блок 2: откуда брать ЗВУК для разбора.

Два источника, оба нужны (решение пользователя):
  * **по ссылке** — качаем у Twitch ТОЛЬКО звуковую дорожку (`Audio_Only`): ~180 МБ на
    2 часа вместо 6,8 ГБ видео, 5-6 минут загрузки. Файл кладём в кэш, чтобы повторный
    разбор того же стрима был мгновенным;
  * **из локального файла** — если запись уже лежит на диске, ничего не качаем вообще.

Звук в память НЕ грузим целиком: 3-часовой стрим в 16 кГц — это ~350 МБ, а на слабой
машине это больно. Вместо этого читаем PCM ПОТОКОМ из ffmpeg кусками по 5 минут
(`iter_pcm`) — и громкость, и модель звуков прекрасно считаются по кускам.

Зависимости: ffmpeg (лежит в сборке) и yt-dlp (уже в requirements — им же качаются клипы).
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

import numpy as np

from core.ffmpeg_utils import ffmpeg_bin, ffprobe_bin

SR = 16000                 # 16 кГц моно — то, что нужно и модели звуков, и Whisper
CHUNK_SEC = 300.0          # читаем звук кусками по 5 минут
Progress = Callable[[str], None]


class MediaError(RuntimeError):
    """Ошибка, которую можно показать пользователю как есть (по-русски)."""


def cache_dir() -> str:
    """Кэш скачанных звуковых дорожек (рядом с настройками пользователя)."""
    from core.provision import app_data_dir
    d = os.path.join(app_data_dir(), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def cache_size_mb() -> float:
    total = 0
    for root, _dirs, files in os.walk(cache_dir()):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / (1024 * 1024)


def clear_cache() -> int:
    """Почистить кэш звука. Возвращает, сколько файлов удалено."""
    n = 0
    for root, _dirs, files in os.walk(cache_dir()):
        for f in files:
            try:
                os.remove(os.path.join(root, f))
                n += 1
            except OSError:
                pass
    return n


CACHE_LIMIT_MB = 1500.0        # ~8 стримов по 2 часа


def trim_cache(limit_mb: float = CACHE_LIMIT_MB) -> int:
    """Не дать кэшу расти бесконечно: сносим самые старые дорожки сверх лимита.

    Каждый разобранный стрим — это ~180 МБ на диске пользователя. Без потолка через
    месяц там были бы десятки гигабайт, и никто бы не понял, куда делось место.
    """
    files = []
    for name in os.listdir(cache_dir()):
        p = os.path.join(cache_dir(), name)
        try:
            files.append((os.path.getmtime(p), os.path.getsize(p), p))
        except OSError:
            continue
    total = sum(size for _t, size, _p in files) / (1024 * 1024)
    removed = 0
    for _mtime, size, path in sorted(files):          # самые старые — первыми
        if total <= limit_mb:
            break
        try:
            os.remove(path)
            total -= size / (1024 * 1024)
            removed += 1
        except OSError:
            pass
    return removed


@dataclass
class AudioSource:
    """Откуда читать звук: путь к файлу (видео или скачанная дорожка) + длительность."""
    path: str
    duration: float = 0.0
    kind: str = "file"          # 'file' — локальная запись | 'vod' — скачанная дорожка
    from_cache: bool = False
    size_mb: float = 0.0

    @property
    def human(self) -> str:
        if self.kind == "vod":
            return "скачанная звуковая дорожка" + (" (из кэша)" if self.from_cache else "")
        return "локальный файл записи"


# --------------------------------------------------------------------------
# Длительность
# --------------------------------------------------------------------------

def probe_duration(path: str) -> float:
    """Длительность файла в секундах (0.0 — не удалось узнать)."""
    try:
        out = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60,
            creationflags=_no_window())
        return float((out.stdout or "0").strip() or 0.0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _no_window() -> int:
    """Не мигать чёрным окном консоли на Windows."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------
# Источник 1: локальный файл
# --------------------------------------------------------------------------

def audio_from_file(path: str, progress: Optional[Progress] = None) -> AudioSource:
    """Локальная запись — ничего не качаем и не конвертируем заранее."""
    if not os.path.isfile(path):
        raise MediaError(f"Файл записи не найден: {path}")
    say = progress or (lambda _s: None)
    dur = probe_duration(path)
    if dur <= 0:
        raise MediaError("Не удалось прочитать файл записи — возможно, он повреждён "
                         "или это не видео/аудио.")
    say(f"Беру звук из файла ({_hms(dur)})")
    return AudioSource(path=path, duration=dur, kind="file",
                       size_mb=os.path.getsize(path) / (1024 * 1024))


# --------------------------------------------------------------------------
# Источник 2: только звук записи с Twitch
# --------------------------------------------------------------------------

_AUDIO_FORMATS = "Audio_Only/bestaudio/worst"


# Расширения, которые вообще могут быть у скачанной дорожки Twitch.
_MEDIA_EXT = (".mp4", ".m4a", ".ts", ".aac", ".mkv", ".webm", ".mp3", ".ogg", ".wav")


def cached_audio(vod_id: str) -> Optional[str]:
    """Уже скачанная дорожка этой записи.

    ⚠️ Мимо кассы легко: рядом yt-dlp кладёт свои служебные файлы (`.part` — недокачка,
    `.ytdl` — состояние загрузки). Если прерваться на середине, программа приняла бы
    служебный файл за готовый звук. Поэтому берём только медиа-расширения и самый
    большой файл (недокачанные обломки мельче).
    """
    best, best_size = None, 0
    for name in os.listdir(cache_dir()):
        if not name.startswith(f"vod_{vod_id}."):
            continue
        if not name.lower().endswith(_MEDIA_EXT):
            continue
        path = os.path.join(cache_dir(), name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > best_size:
            best, best_size = path, size
    return best if best_size > 64 * 1024 else None      # огрызок — не дорожка


def audio_from_vod(vod_id: str, progress: Optional[Progress] = None,
                   url: str = "") -> AudioSource:
    """Скачать ТОЛЬКО звуковую дорожку записи (или взять из кэша)."""
    say = progress or (lambda _s: None)

    hit = cached_audio(vod_id)
    if hit:
        dur = probe_duration(hit)
        say(f"Звук этой записи уже скачан ({_hms(dur)}) — качать заново не надо")
        return AudioSource(path=hit, duration=dur, kind="vod", from_cache=True,
                           size_mb=os.path.getsize(hit) / (1024 * 1024))

    try:
        import yt_dlp
    except ImportError:
        raise MediaError("Не найден компонент загрузки (yt-dlp). Переустанови "
                         "программу — он входит в комплект.") from None

    target = url or f"https://www.twitch.tv/videos/{vod_id}"
    out_tmpl = os.path.join(cache_dir(), f"vod_{vod_id}.%(ext)s")
    state = {"pct": -10.0}

    def hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        got = d.get("downloaded_bytes") or 0
        if not total:
            return
        pct = got / total * 100.0
        if pct - state["pct"] >= 10.0:             # не сыпать строчками каждую секунду
            state["pct"] = pct
            say(f"Качаю звук записи: {pct:.0f}%  ({got / 1048576:.0f} из "
                f"{total / 1048576:.0f} МБ)")

    opts = {
        "format": _AUDIO_FORMATS,
        "outtmpl": out_tmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "concurrent_fragment_downloads": 8,        # заметно быстрее на длинных VOD
        "retries": 10,
        "fragment_retries": 20,
        "continuedl": True,                        # продолжать недокачанный файл
    }
    say("Спрашиваю у Twitch звуковую дорожку записи…")

    # ⚠️ На длинной записи (5 часов — это больше 1700 кусочков) одного сетевого чиха
    # хватает, чтобы всё сорвалось на 80%. Пробуем несколько раз: yt-dlp продолжает
    # с того места, где остановился, а не качает заново.
    last_err = ""
    for attempt in range(1, 4):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([target])
            last_err = ""
            break
        except Exception as e:                      # noqa: BLE001 — сообщения yt-dlp разные
            last_err = str(e)
            if attempt < 3:
                state["pct"] = -10.0
                say(f"Загрузка сорвалась ({attempt} из 3) — продолжаю с того же места…")
                time.sleep(3.0)
    if last_err:
        raise MediaError(_explain_ytdlp(last_err))

    path = cached_audio(vod_id)
    if not path:
        raise MediaError("Звук скачался, но файла нет — возможно, кончилось место на диске.")
    dur = probe_duration(path)
    size = os.path.getsize(path) / (1024 * 1024)
    say(f"Звук скачан: {size:.0f} МБ, {_hms(dur)}")
    dropped = trim_cache()               # не даём кэшу расти бесконечно
    if dropped:
        say(f"Из кэша убраны {dropped} старых дорожек — чтобы не занимать диск")
    return AudioSource(path=path, duration=dur, kind="vod", size_mb=size)


def _explain_ytdlp(msg: str, what: str = "звук записи") -> str:
    """Ошибку yt-dlp — на человеческий язык.

    `what` — что именно качали: тем же кодом качаются и звуковая дорожка (разбор),
    и куски видео (нарезка, `vodcut`), а «не получилось скачать звук» вместо «кусок
    записи» сбивает с толку.
    """
    low = msg.lower()
    if "404" in low or "not found" in low or "does not exist" in low:
        return ("Запись недоступна. Обычно это значит, что она удалена: Twitch хранит "
                "записи 7-60 дней. Возьми стрим посвежее или укажи локальный файл.")
    if "subscriber" in low or "403" in low:
        return f"Запись доступна только подписчикам канала — скачать {what} не выйдет."
    if "unable to download" in low or "urlopen" in low or "timed out" in low:
        return (f"Не получилось скачать {what} — похоже, оборвалась связь. Запусти ещё "
                f"раз: скачанная часть сохранилась, докачается только остаток.")
    return f"Не получилось скачать {what}: {msg.strip()[:200]}"


# --------------------------------------------------------------------------
# Чтение звука кусками
# --------------------------------------------------------------------------

def iter_pcm(path: str, chunk_sec: float = CHUNK_SEC, sr: int = SR,
             start: float = 0.0, duration: Optional[float] = None
             ) -> Iterator[tuple[float, np.ndarray]]:
    """Читать звук файла кусками: отдаёт (время начала куска, сэмплы float32 -1..1).

    Поток, а не файл: ffmpeg отдаёт сырой PCM в stdout, мы режем его по кускам.
    Так 3-часовой стрим не превращается в 350 МБ в памяти.
    """
    args = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error"]
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", path]
    if duration:
        args += ["-t", f"{duration:.3f}"]
    args += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]

    frames = int(chunk_sec * sr)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_no_window())
    t = start
    try:
        while True:
            raw = proc.stdout.read(frames * 2)      # int16 = 2 байта на сэмпл
            if not raw:
                break
            block = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if block.size:
                yield t, block
                t += block.size / sr
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
        err = b""
        try:
            err = proc.stderr.read() or b""
            proc.stderr.close()
        except OSError:
            pass
        code = proc.wait()
        if code not in (0, None) and t == start:
            raise MediaError("ffmpeg не смог прочитать звук: "
                             + err.decode("utf-8", "replace")[:200])


def extract_window(path: str, start: float, duration: float, out_wav: str,
                   sr: int = SR) -> str:
    """Вырезать кусок звука в wav (для распознавания речи по кандидатам)."""
    os.makedirs(os.path.dirname(os.path.abspath(out_wav)), exist_ok=True)
    args = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{max(0.0, start):.3f}", "-i", path, "-t", f"{max(0.1, duration):.3f}",
            "-vn", "-ac", "1", "-ar", str(sr), out_wav]
    r = subprocess.run(args, capture_output=True, text=True, creationflags=_no_window())
    if r.returncode != 0 or not os.path.isfile(out_wav):
        raise MediaError("Не удалось вырезать кусок звука: "
                         + (r.stderr or "").strip()[:200])
    return out_wav


def _hms(sec: float) -> str:
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_VOD_URL_RE = re.compile(r"videos?/(\d+)")


def vod_id_from_url(url: str) -> str:
    m = _VOD_URL_RE.search(url or "")
    return m.group(1) if m else ""
