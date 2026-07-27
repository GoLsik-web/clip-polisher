"""twitch_clips.py — Этап 3, сигнал №1: КЛИПЫ, КОТОРЫЕ НАРЕЗАЛИ САМИ ЗРИТЕЛИ.

Главная находка Этапа 3: официальный Helix `GET /helix/clips` отдаёт клипы канала, а у
свежих клипов заполнено поле `vod_offset` — ТОЧНАЯ секунда внутри записи стрима. То есть
Twitch бесплатно отдаёт нам «народные метки»: где зрители жали кнопку «клип», там людям
реально зашло. Плюс `view_count` — грубая оценка, насколько зашло.

Что важно помнить:
  * `vod_offset` есть только пока жива ЗАПИСЬ (VOD). У старых клипов там None — такие
    для разбора бесполезны, пропускаем.
  * Один момент часто нарезают несколько человек с разницей в секунды (проверено:
    два клипа на 5:21:10 и 5:21:22 — это ОДИН момент). Схлопывание — в `clipscan`.
  * Работает ПОЛЬЗОВАТЕЛЬСКИМ токеном (тем же, что бот), секрет приложения не нужен,
    и работает для ЛЮБОГО канала — не только своего.

Только стандартная библиотека, никакого Qt. Сеть подменяется параметром `http=` —
поэтому модуль целиком тестируется без интернета.
"""
from __future__ import annotations

import datetime
import re
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Optional

from core.twitch_auth import CLIENT_ID, HELIX, _get

# Тип «сделать GET»: (url, headers) → (http-код, json). По умолчанию — настоящая сеть.
HttpGet = Callable[[str, dict], tuple[int, dict]]


class TwitchError(RuntimeError):
    """Ошибка разбора/сети, которую МОЖНО показать пользователю как есть (по-русски)."""


# --------------------------------------------------------------------------
# Ссылки и время
# --------------------------------------------------------------------------

@dataclass
class Source:
    """Что пользователь вставил в поле «ссылка»."""
    kind: str            # 'vod' — конкретная запись | 'channel' — канал целиком
    channel: str = ""    # ник канала (для kind='channel')
    vod_id: str = ""     # id записи  (для kind='vod')
    raw: str = ""

    def __str__(self) -> str:
        return f"запись {self.vod_id}" if self.kind == "vod" else f"канал {self.channel}"


_VOD_RE = re.compile(r"twitch\.tv/(?:[\w%]+/)?videos?/(\d+)", re.I)
_VIDEO_ID_RE = re.compile(r"^v?(\d{6,})$")
_CHANNEL_RE = re.compile(r"twitch\.tv/([A-Za-z0-9_]{3,25})", re.I)
_BARE_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{3,25}$")

# Разделы twitch.tv, которые не являются каналом.
_NOT_CHANNELS = {"videos", "directory", "settings", "clips", "downloads", "u", "popout"}


def parse_source(text: str) -> Source:
    """Ссылка/ник → что именно разбирать. Бросает TwitchError с понятным текстом."""
    s = (text or "").strip().strip("<>\"' ")
    if not s:
        raise TwitchError("Пустая ссылка. Вставь адрес записи стрима или ник канала.")

    m = _VOD_RE.search(s)
    if m:
        return Source(kind="vod", vod_id=m.group(1), raw=s)

    m = _VIDEO_ID_RE.match(s)
    if m:
        return Source(kind="vod", vod_id=m.group(1), raw=s)

    if "clips.twitch.tv/" in s.lower() or "/clip/" in s.lower():
        raise TwitchError("Это ссылка на ОДИН клип. Нужна ссылка на запись целого "
                          "стрима (twitch.tv/videos/…) или ник канала.")

    m = _CHANNEL_RE.search(s)
    if m and m.group(1).lower() not in _NOT_CHANNELS:
        return Source(kind="channel", channel=m.group(1).lower(), raw=s)

    if _BARE_CHANNEL_RE.match(s):
        return Source(kind="channel", channel=s.lower(), raw=s)

    raise TwitchError(f"Не понял ссылку «{s}». Нужен адрес вида "
                      f"twitch.tv/videos/123456789 или ник канала.")


_DUR_RE = re.compile(r"(\d+)([hms])")


def parse_duration(text: str) -> float:
    """Длительность Twitch («3h8m33s») → секунды."""
    total = 0.0
    for num, unit in _DUR_RE.findall(text or ""):
        total += int(num) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def _iso_utc(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Запросы к Helix
# --------------------------------------------------------------------------

def _helix(path: str, token: str, params: dict, http: Optional[HttpGet] = None) -> dict:
    """GET к Helix пользовательским токеном. Ошибки → TwitchError по-человечески."""
    get = http or _get
    url = f"{HELIX}/{path}?" + urllib.parse.urlencode(params, doseq=True)
    status, body = get(url, {"Client-Id": CLIENT_ID, "Authorization": "Bearer " + token})
    if status == 401:
        raise TwitchError("Twitch не принял вход — нужно войти заново на вкладке «Бот».")
    if status == 429:
        raise TwitchError("Twitch просит подождать (слишком много запросов). "
                          "Попробуй через минуту.")
    if status != 200:
        msg = body.get("message") or f"код {status}"
        raise TwitchError(f"Twitch ответил ошибкой: {msg}")
    return body


@dataclass
class VodInfo:
    """Запись стрима (VOD) — к ней привязаны и клипы, и наш будущий разбор."""
    id: str = ""
    channel: str = ""
    user_id: str = ""
    title: str = ""
    game: str = ""
    created_at: str = ""            # начало записи, ISO (это и есть «ноль» времени)
    duration: float = 0.0           # секунды
    url: str = ""
    broadcast_id: str = ""          # stream_id — сшивает VOD с файлом меток бота

    @property
    def started(self) -> Optional[datetime.datetime]:
        return parse_iso(self.created_at)

    @classmethod
    def from_helix(cls, d: dict) -> "VodInfo":
        return cls(id=str(d.get("id", "")),
                   channel=(d.get("user_login") or d.get("user_name") or "").lower(),
                   user_id=str(d.get("user_id", "")),
                   title=d.get("title", ""),
                   created_at=d.get("created_at", ""),
                   duration=parse_duration(d.get("duration", "")),
                   url=d.get("url", ""),
                   broadcast_id=str(d.get("stream_id") or ""))


def fetch_vod(token: str, vod_id: str, http: Optional[HttpGet] = None) -> VodInfo:
    """Данные записи по её id."""
    body = _helix("videos", token, {"id": vod_id}, http)
    data = body.get("data") or []
    if not data:
        raise TwitchError(
            "Такой записи на Twitch нет. Обычно это значит, что запись уже удалена: "
            "Twitch хранит их 7–60 дней. Возьми свежий стрим или укажи локальный файл записи.")
    return VodInfo.from_helix(data[0])


def fetch_last_vods(token: str, channel: str, limit: int = 5,
                    http: Optional[HttpGet] = None) -> list[VodInfo]:
    """Последние записи канала (для kind='channel': «какой стрим разбираем?»)."""
    user = _helix("users", token, {"login": channel}, http).get("data") or []
    if not user:
        raise TwitchError(f"Канала «{channel}» на Twitch нет — проверь ник.")
    body = _helix("videos", token, {"user_id": user[0]["id"], "type": "archive",
                                    "first": max(1, min(limit, 100))}, http)
    vods = [VodInfo.from_helix(d) for d in (body.get("data") or [])]
    if not vods:
        raise TwitchError(
            f"У канала «{channel}» нет доступных записей стримов. Возможно, запись прошлых "
            f"трансляций отключена в настройках Twitch или записи уже истекли.")
    return vods


@dataclass
class ViewerClip:
    """Клип, который нарезал зритель."""
    id: str = ""
    title: str = ""
    creator: str = ""
    views: int = 0
    duration: float = 0.0
    vod_offset: Optional[float] = None     # секунда внутри записи (None — VOD истёк)
    video_id: str = ""
    created_at: str = ""
    url: str = ""

    @property
    def center(self) -> float:
        """Где внутри клипа «сам момент».

        Зритель жмёт кнопку ПОСЛЕ смешного, поэтому событие сидит ближе к концу
        вырезанного куска — берём 60% длины, а не середину.
        """
        return (self.vod_offset or 0.0) + self.duration * 0.6

    @classmethod
    def from_helix(cls, d: dict) -> "ViewerClip":
        off = d.get("vod_offset")
        return cls(id=str(d.get("id", "")), title=(d.get("title") or "").strip(),
                   creator=d.get("creator_name", ""),
                   views=int(d.get("view_count") or 0),
                   duration=float(d.get("duration") or 0.0),
                   vod_offset=(float(off) if off is not None else None),
                   video_id=str(d.get("video_id") or ""),
                   created_at=d.get("created_at", ""), url=d.get("url", ""))


def fetch_clips(token: str, broadcaster_id: str, started_at: str = "", ended_at: str = "",
                max_pages: int = 20, http: Optional[HttpGet] = None) -> list[ViewerClip]:
    """Все клипы канала за период (с пагинацией). Период — по времени СОЗДАНИЯ клипа."""
    params: dict = {"broadcaster_id": broadcaster_id, "first": 100}
    if started_at:
        params["started_at"] = started_at
    if ended_at:
        params["ended_at"] = ended_at

    out: list[ViewerClip] = []
    cursor = ""
    for _ in range(max(1, max_pages)):
        q = dict(params)
        if cursor:
            q["after"] = cursor
        body = _helix("clips", token, q, http)
        page = body.get("data") or []
        out.extend(ViewerClip.from_helix(d) for d in page)
        cursor = ((body.get("pagination") or {}).get("cursor") or "")
        if not cursor or not page:
            break
    return out


def clips_for_vod(token: str, vod: VodInfo, tail_days: float = 21.0,
                  http: Optional[HttpGet] = None) -> list[ViewerClip]:
    """Клипы, нарезанные ИЗ ЭТОЙ записи, отсортированные по позиции в ней.

    Период запроса = сам стрим + хвост (клипы часто режут уже из записи, спустя дни
    после эфира — Twitch фильтрует по дате СОЗДАНИЯ клипа, а не по месту в стриме).
    ⚠️ Хвост был 3 дня — клип, нарезанный из старой записи на неделе, в выборку не
    попадал. Лишнее всё равно отсекается точно по `video_id`, так что широкое окно
    ничего не портит, только просит на пару запросов больше.
    """
    start = vod.started
    if start is None:
        raise TwitchError("Twitch не сказал, когда началась запись — разбор невозможен.")
    end = start + datetime.timedelta(seconds=vod.duration + tail_days * 86400)
    raw = fetch_clips(token, vod.user_id, _iso_utc(start), _iso_utc(end), http=http)

    mine = [c for c in raw
            if c.vod_offset is not None and (not c.video_id or c.video_id == vod.id)]
    # Клип не может начинаться за пределами записи — страховка от мусорных данных.
    if vod.duration:
        mine = [c for c in mine if -1.0 <= (c.vod_offset or 0.0) <= vod.duration + 60.0]
    mine.sort(key=lambda c: c.vod_offset or 0.0)
    return mine


def expired_clip_count(clips: list[ViewerClip]) -> int:
    """Сколько клипов пришлось выбросить из-за истёкшей записи (для честного отчёта)."""
    return sum(1 for c in clips if c.vod_offset is None)
