"""marks.py — Этап 2: метки из чата → моменты для склейки.

Контракт бота ↔ приложения: бот во время стрима пишет ФАЙЛ МЕТОК (JSON) — какая
трансляция, чья, онлайн и список меток (время в секундах от начала стрима + тип
автора). Приложение читает этот файл и превращает точки-метки в ОТРЕЗКИ (моменты):

    метки → схлопывание близких (gap) → отбор по режиму/порогам → окно вокруг центра
    → слияние перекрытий → список Moment (в координатах ВРЕМЕНИ СТРИМА).

Дальше моменты становятся `config.Segment` и уходят в pipeline (склейка уже готова).

Никакого Qt здесь быть не должно (чистый core).

ВАЖНО про время: метки и моменты живут во времени СТРИМА. Если локальный файл записи
неполный (сдвиг начала/конца, дыры в середине) — перевод «время стрима → позиция в
файле» делает отдельный слой маппинга (Этап 2, «неполная запись»), не этот модуль.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from statistics import median
from typing import Optional

from .config import Segment


# --------------------------------------------------------------------------
# Типы
# --------------------------------------------------------------------------

class AuthorType(str, Enum):
    """Кто поставил метку. Влияет на отбор моментов и на цвет пина в UI."""
    STREAMER = "streamer"
    MODERATOR = "moderator"
    VIP = "vip"
    VIEWER = "viewer"


# «Доверенные» авторы — их метка сама по себе создаёт момент (без порога).
TRUSTED = (AuthorType.STREAMER, AuthorType.MODERATOR, AuthorType.VIP)

# Вес автора для «жара» момента (сортировка списка моментов по важности).
_WEIGHT = {
    AuthorType.STREAMER: 4,
    AuthorType.MODERATOR: 2,
    AuthorType.VIP: 2,
    AuthorType.VIEWER: 1,
}


class AudienceMode(str, Enum):
    """Режим отбора под размер аудитории (мягкие названия — не «мелкий стример»).

    AUTO выбирает один из трёх сам по онлайну из файла меток.
    Порог = сколько РАЗНЫХ зрителей должны отметить место, чтобы это стало моментом
    (метки доверенных авторов создают момент всегда, вне зависимости от порога).
    """
    AUTO = "auto"
    SMALL = "small"      # камерный онлайн — считается каждая зрительская метка
    MEDIUM = "medium"    # средний онлайн — нужно несколько
    LARGE = "large"      # большой онлайн — нужно много


# Порог «разных зрителей» на момент для каждого режима.
VIEWER_THRESHOLD = {
    AudienceMode.SMALL: 1,
    AudienceMode.MEDIUM: 3,
    AudienceMode.LARGE: 10,
}

# Границы онлайна для AUTO (< → режим).
_AUTO_SMALL_MAX = 50      # онлайн < 50  → камерный
_AUTO_MEDIUM_MAX = 500    # онлайн < 500 → средний; иначе большой

# Дефолты схлопывания/окна (совпадают с решениями по ТЗ Этапа 2).
DEFAULT_GAP = 15.0                 # метки ближе 15 с — один момент
DEFAULT_WINDOW = (-30.0, 10.0)     # окно вокруг центра метки: −30 … +10 с


@dataclass
class Mark:
    """Одна метка: точка во времени стрима + кто поставил + опц. заметка."""
    t: float
    type: AuthorType = AuthorType.VIEWER
    author: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Mark":
        return cls(t=float(d["t"]),
                   type=AuthorType(d.get("type", "viewer")),
                   author=d.get("author", ""),
                   note=d.get("note", ""))


@dataclass
class MarksFile:
    """Разобранный файл меток (то, что выгружает бот)."""
    platform: str = "twitch"
    streamer: str = ""
    broadcast_id: str = ""
    started_at: str = ""
    duration: Optional[float] = None     # длина трансляции, с (для матчинга видео)
    online: Optional[int] = None         # онлайн на стриме (для AUTO)
    title: str = ""                      # название трансляции (чтобы отличать эфиры)
    game: str = ""                       # категория/игра
    marks: list[Mark] = field(default_factory=list)

    # ---- сериализация ----
    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "streamer": self.streamer,
            "broadcast_id": self.broadcast_id,
            "started_at": self.started_at,
            "duration": self.duration,
            "online": self.online,
            "title": self.title,
            "game": self.game,
            "marks": [m.to_dict() for m in self.marks],
        }

    def to_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "MarksFile":
        return cls(
            platform=d.get("platform", "twitch"),
            streamer=d.get("streamer", ""),
            broadcast_id=str(d.get("broadcast_id", "")),
            started_at=d.get("started_at", ""),
            duration=(float(d["duration"]) if d.get("duration") is not None else None),
            online=(int(d["online"]) if d.get("online") is not None else None),
            title=d.get("title", ""),      # в старых файлах этих полей нет — не страшно
            game=d.get("game", ""),
            marks=[Mark.from_dict(m) for m in d.get("marks", [])],
        )

    @classmethod
    def from_json(cls, path: str) -> "MarksFile":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


@dataclass
class Moment:
    """Отобранный момент (отрезок во времени СТРИМА) + метаданные для UI."""
    start: float
    end: float
    center: float
    heat: int                             # «жар» — вес по авторам (сортировка)
    marks: list[Mark] = field(default_factory=list)
    label: str = ""                       # имя момента (первая заметка из меток)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_segment(self) -> Segment:
        return Segment(start=self.start, end=self.end)


# --------------------------------------------------------------------------
# Логика отбора
# --------------------------------------------------------------------------

def resolve_mode(mode: AudienceMode, online: Optional[int]) -> AudienceMode:
    """AUTO → конкретный режим по онлайну. Не-AUTO возвращается как есть."""
    if mode != AudienceMode.AUTO:
        return mode
    if online is None:
        return AudienceMode.MEDIUM              # нет данных — берём середину
    if online < _AUTO_SMALL_MAX:
        return AudienceMode.SMALL
    if online < _AUTO_MEDIUM_MAX:
        return AudienceMode.MEDIUM
    return AudienceMode.LARGE


def _cluster(marks: list[Mark], gap: float) -> list[list[Mark]]:
    """Схлопнуть метки в группы: разрыв между соседними > gap рвёт группу."""
    ms = sorted(marks, key=lambda m: m.t)
    groups: list[list[Mark]] = []
    cur: list[Mark] = []
    for m in ms:
        if cur and m.t - cur[-1].t > gap:
            groups.append(cur)
            cur = []
        cur.append(m)
    if cur:
        groups.append(cur)
    return groups


def _distinct_viewers(group: list[Mark]) -> int:
    """Сколько РАЗНЫХ зрителей в группе (по нику; без ника — считаем по числу меток)."""
    viewers = [m for m in group if m.type == AuthorType.VIEWER]
    named = {m.author for m in viewers if m.author}
    return len(named) if named else len(viewers)


def select_moments(mf: MarksFile,
                   mode: AudienceMode = AudienceMode.AUTO,
                   window: tuple[float, float] = DEFAULT_WINDOW,
                   gap: float = DEFAULT_GAP,
                   author_filter: Optional[set[AuthorType]] = None) -> list[Moment]:
    """Превратить метки в список моментов (во времени стрима), отсортированный по времени.

    - author_filter: если задан — учитываем только метки этих типов (для UI-фильтра).
    - метки доверенных авторов (стример/модер/вип) → момент всегда;
      зрительские → момент, только если разных зрителей в группе >= порог режима.
    - каждая группа меток = ОТДЕЛЬНЫЙ момент. Если окна соседних моментов налезают
      друг на друга (частое на коротком видео/близких метках) — граница между ними
      разводится по СЕРЕДИНЕ между центрами (моменты не сливаются и не дублируют footage).
    """
    marks = mf.marks
    if author_filter is not None:
        marks = [m for m in marks if m.type in author_filter]
    if not marks:
        return []

    eff_mode = resolve_mode(mode, mf.online)
    threshold = VIEWER_THRESHOLD[eff_mode]
    lo, hi = window
    dur = mf.duration

    raw: list[Moment] = []
    for group in _cluster(marks, gap):
        has_trusted = any(m.type in TRUSTED for m in group)
        if not has_trusted and _distinct_viewers(group) < threshold:
            continue
        center = float(median([m.t for m in group]))
        start = max(0.0, center + lo)
        end = center + hi
        if dur is not None:
            end = min(end, dur)
        if end <= start:
            continue
        heat = sum(_WEIGHT.get(m.type, 1) for m in group)
        label = next((m.note for m in group if m.note), "")
        raw.append(Moment(start=start, end=end, center=center,
                          heat=heat, marks=list(group), label=label))

    # Каждая группа = отдельный момент. Перекрытие соседних окон (широкое ±30/±10 на
    # близких метках) НЕ сливаем, а разводим границу по середине между их центрами —
    # так две разнесённые метки всегда дают ДВА клипа, без общего/дублированного footage.
    raw.sort(key=lambda mo: mo.center)
    for a, b in zip(raw, raw[1:]):
        if a.end > b.start:                       # окна налезли
            mid = (a.center + b.center) / 2.0
            mid = min(max(mid, a.center), b.center)
            a.end = min(a.end, mid)
            b.start = max(b.start, mid)
    return raw


def moments_to_segments(moments: list[Moment]) -> list[Segment]:
    """Список моментов → список сегментов для pipeline/render (в порядке времени)."""
    return [mo.to_segment() for mo in moments]


def by_heat(moments: list[Moment]) -> list[Moment]:
    """Те же моменты, отсортированные по «жару» (для панели «Моменты» / топ-N)."""
    return sorted(moments, key=lambda mo: mo.heat, reverse=True)
