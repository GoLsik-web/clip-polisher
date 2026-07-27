"""clipscan.py — Этап 3: файл разбора стрима `.clipscan` и СКОРИНГ моментов.

Идея простая: все улики (клипы зрителей, взрывы чата, метки из бота, дальше — громкость,
смех, речь, лицо) складываются в один список СИГНАЛОВ с временем. Сигналы, стоящие рядом,
сбиваются в кандидата; кандидаты получают очки и человеческое объяснение; из кандидатов
отбираются 2–8 моментов по ползунку строгости.

Почему именно так:
  * ОДИН сигнал легко обмануть (кто-то нарезал клип «для себя», чат взорвался от рекламы).
    Когда за момент голосуют РАЗНЫЕ по природе улики — ошибиться намного труднее. За это
    даётся бонус, а момент помечается «золотым».
  * Файл хранит и СЫРЬЁ, и результат. Поэтому ползунок строгости пересчитывается мгновенно
    (`rescore`) — без повторного похода в сеть и без нового прохода по видео.
  * Объяснение собирается из тех же чисел, что и очки. Не «жар 87», а «3 зрителя нарезали
    клип · чат ×6 · сплошной смех» — видно, где машина ошиблась.

Всё время — в секундах от начала записи/эфира (как и метки Этапа 2).

Чистое ядро: ни Qt, ни сети. Сеть живёт в `twitch_clips`, чат — в `chatpulse`.
"""
from __future__ import annotations

import datetime
import json
import math
import re
from dataclasses import dataclass, field
from typing import Optional

from core.config import Segment
from core.marks import AuthorType, MarksFile

SCAN_VERSION = 1
SCORING_VERSION = 1          # меняется при правке формулы — блок 7 сравнит «до/после»

# Окно вокруг центра момента — как в Этапе 2 (потом пользователь двигает руками).
DEFAULT_WINDOW = (-30.0, 10.0)
CLUSTER_GAP = 25.0           # сигналы ближе 25 с — про один и тот же момент
MIN_SPACING = 120.0          # не брать два клипа из одной минуты (разнос по времени)
MIN_MOMENTS, MAX_MOMENTS = 2, 8

# Порог очков: абсолютный (чтобы мусор не проходил даже на «мягко»)…
SCORE_AT_LOOSE, SCORE_AT_STRICT = 1.5, 6.5
# …и относительный — доля от лучшего момента этого же стрима.
# Зачем: у крупного канала момент набирает 40-60 очков, у камерного — 3-5. С одним
# только абсолютным порогом ползунок строгости у крупных не двигал бы НИЧЕГО
# (проверено на живом стриме: 8 моментов и на 0, и на 100). Относительный порог
# делает ползунок осмысленным на любом канале.
REL_AT_STRICT = 0.75


# --------------------------------------------------------------------------
# Сигналы
# --------------------------------------------------------------------------

class Kind:
    """Виды улик. Первые три работают уже в блоке 1, остальные добавят блоки 2–4."""
    VIEWER_CLIP = "viewer_clip"
    CHAT_SPIKE = "chat_spike"
    CHAT_MARK = "chat_mark"
    LOUD = "loud"
    LAUGH = "laugh"
    SPEECH = "speech"
    FACE = "face"


# «Семья» сигнала: бонус за независимость даётся за разные СЕМЬИ, а не просто виды —
# взрыв чата и метка из чата приходят от одних и тех же людей, это не два свидетеля.
FAMILY = {
    Kind.VIEWER_CLIP: "clips",
    Kind.CHAT_SPIKE: "chat",
    Kind.CHAT_MARK: "chat",
    Kind.LOUD: "audio",
    Kind.LAUGH: "audio",
    Kind.SPEECH: "speech",
    Kind.FACE: "face",
}

_MARK_WEIGHT = {AuthorType.STREAMER: 3.0, AuthorType.MODERATOR: 2.0,
                AuthorType.VIP: 2.0, AuthorType.VIEWER: 1.0}


def face_available(comp) -> bool:
    """Можно ли вообще читать лицо: у стримера ЕСТЬ вебка и её зона задана.

    ⚠️ Вебка есть не у всех — это решение пользователя, зафиксированное в ТЗ Этапа 3.
    Сигнал «лицо» (блок 4) обязан спрашивать это, а не требовать камеру: без неё
    разбор просто идёт по клипам, чату, звуку и речи. `comp=None` — тоже «нет вебки».
    """
    if comp is None:
        return False
    wc = getattr(comp, "webcam", None)
    src = getattr(comp, "webcam_source", None)
    if wc is None or not getattr(wc, "visible", False):
        return False
    # Нулевая/схлопнутая зона источника = вебку никто не размечал.
    w = float(getattr(src, "w", 0.0) or 0.0)
    h = float(getattr(src, "h", 0.0) or 0.0)
    return w > 0.02 and h > 0.02


def face_note(comp) -> str:
    """Честная строчка для пользователя, почему сигнал «лицо» не участвует."""
    if face_available(comp):
        return ""
    return ("Зона вебки не задана или выключена — эмоции лица не смотрим. "
            "Это нормально: у многих стримеров камеры нет, разбор идёт по клипам, "
            "чату, звуку и речи.")


@dataclass
class Signal:
    """Одна улика: что, когда, насколько весомо и как это объяснить человеку."""
    kind: str
    t: float
    weight: float = 1.0
    detail: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def family(self) -> str:
        return FAMILY.get(self.kind, self.kind)

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "t": round(self.t, 2), "weight": round(self.weight, 3)}
        if self.detail:
            d["detail"] = self.detail
        if self.meta:
            d["meta"] = self.meta
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Signal":
        return cls(kind=d.get("kind", ""), t=float(d.get("t", 0.0)),
                   weight=float(d.get("weight", 1.0)), detail=d.get("detail", ""),
                   meta=d.get("meta") or {})


def signals_from_clips(clips) -> list[Signal]:
    """Клипы зрителей → сигналы. Просмотры добавляют веса, но не решают всё."""
    out: list[Signal] = []
    for c in clips:
        if c.vod_offset is None:
            continue
        views = max(0, int(c.views))
        weight = 3.0 + min(2.0, math.log10(views + 1))     # 0 просм. → 3.0, 1000+ → 5.0
        detail = f"клип «{c.title}»" if c.title else "клип зрителя"
        if views:
            detail += f" ({views} просм.)"
        out.append(Signal(kind=Kind.VIEWER_CLIP, t=c.center, weight=weight, detail=detail,
                          meta={"id": c.id, "title": c.title, "views": views,
                                "creator": c.creator, "url": c.url,
                                "start": c.vod_offset, "duration": c.duration}))
    return out


def signals_from_pulse(log, **kw) -> list[Signal]:
    """Журнал пульса чата → сигналы «здесь чат взорвался»."""
    from core.chatpulse import find_spikes
    out: list[Signal] = []
    for sp in find_spikes(log, **kw):
        weight = 1.0 + min(2.0, (sp.ratio - 2.0) / 2.0)    # ×2 → 1.0, ×6 → 3.0
        out.append(Signal(kind=Kind.CHAT_SPIKE, t=sp.center, weight=max(0.5, weight),
                          detail=sp.describe(),
                          meta={"ratio": sp.ratio, "messages": sp.messages,
                                "chatters": sp.chatters, "laugh": sp.laugh,
                                "hype": sp.hype, "top": sp.top,
                                "start": sp.start, "end": sp.end}))
    return out


def signals_from_marks(mf: MarksFile) -> list[Signal]:
    """Метки из чата (Этап 2) — тоже улика: человек прямо сказал «тут момент»."""
    names = {AuthorType.STREAMER: "стример", AuthorType.MODERATOR: "модератор",
             AuthorType.VIP: "вип", AuthorType.VIEWER: "зритель"}
    out: list[Signal] = []
    for m in mf.marks:
        detail = f"{names.get(m.type, 'зритель')} пометил момент"
        if m.note:
            detail += f" «{m.note}»"
        out.append(Signal(kind=Kind.CHAT_MARK, t=m.t, weight=_MARK_WEIGHT.get(m.type, 1.0),
                          detail=detail,
                          meta={"author": m.author, "role": m.type.value, "note": m.note}))
    return out


# --------------------------------------------------------------------------
# Момент
# --------------------------------------------------------------------------

@dataclass
class ScanMoment:
    """Найденный момент: границы, очки и ЧЕСТНОЕ объяснение, почему он выбран."""
    start: float
    end: float
    center: float
    score: float
    label: str = ""
    reasons: list[str] = field(default_factory=list)
    families: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)

    @property
    def gold(self) -> bool:
        """За момент проголосовали разные по природе улики — почти не ошибка."""
        return len(self.families) >= 2

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_segment(self) -> Segment:
        return Segment(start=self.start, end=self.end)

    def why(self) -> str:
        """Одна строка «почему выбрано» — для карточки и для консольной проверялки."""
        return " · ".join(self.reasons) if self.reasons else "нет объяснения"

    def to_dict(self) -> dict:
        return {"start": round(self.start, 2), "end": round(self.end, 2),
                "center": round(self.center, 2), "score": round(self.score, 2),
                "label": self.label, "reasons": self.reasons, "gold": self.gold,
                "families": self.families,
                "signals": [s.to_dict() for s in self.signals]}

    @classmethod
    def from_dict(cls, d: dict) -> "ScanMoment":
        return cls(start=float(d["start"]), end=float(d["end"]),
                   center=float(d.get("center", d["start"])),
                   score=float(d.get("score", 0.0)), label=d.get("label", ""),
                   reasons=list(d.get("reasons") or []),
                   families=list(d.get("families") or []),
                   signals=[Signal.from_dict(s) for s in (d.get("signals") or [])])


def fmt_time(sec: float) -> str:
    """Секунды → Ч:ММ:СС (как в плеере Twitch)."""
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------
# Скоринг: сигналы → кандидаты → отбор
# --------------------------------------------------------------------------

def _cluster(signals: list[Signal], gap: float) -> list[list[Signal]]:
    """Сигналы, стоящие рядом по времени, — про один момент.

    Именно здесь схлопываются дубли: два зрителя нарезали один момент с разницей
    в 12 секунд (реальный случай) — это ОДИН кандидат, а не два клипа.
    """
    ordered = sorted(signals, key=lambda s: s.t)
    groups: list[list[Signal]] = []
    cur: list[Signal] = []
    for s in ordered:
        if cur and s.t - cur[-1].t > gap:
            groups.append(cur)
            cur = []
        cur.append(s)
    if cur:
        groups.append(cur)
    return groups


def _plural_viewers(n: int) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return "зритель нарезал клип"
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return f"{n} зрителя нарезали клип"
    return f"{n} зрителей нарезали клип"


def good_label(text: str) -> bool:
    """Годится ли строка в НАЗВАНИЕ момента.

    Зрители называют клипы как попало: «12», «67», «f[[f[f» (случайное нажатие). Такое
    имя человеку ничего не говорит — лучше взять цитату из речи. Требуем настоящее
    слово из трёх букв и чтобы букв было хотя бы половина строки.
    """
    text = (text or "").strip()
    if not text:
        return False
    letters = sum(1 for ch in text if ch.isalpha())
    longest = max((len(w) for w in re.findall(r"[^\W\d_]+", text, re.UNICODE)), default=0)
    return longest >= 3 and letters >= len(text) * 0.5


def _reasons(group: list[Signal]) -> tuple[list[str], str]:
    """Человеческое объяснение кандидата + имя момента (бесплатно из клипов/заметок).

    ⚠️ Улики одного вида СВОРАЧИВАЮТСЯ в одну строку: на живом стриме выходило
    «смеются · смеются · стало громче в 5.7 раза · стало громче в 5.9 раза» — читать
    такое невозможно.
    """
    clips = [s for s in group if s.kind == Kind.VIEWER_CLIP]
    marks = [s for s in group if s.kind == Kind.CHAT_MARK]
    spikes = [s for s in group if s.kind == Kind.CHAT_SPIKE]
    speech = [s for s in group if s.kind == Kind.SPEECH]
    sounds = [s for s in group if s.kind in (Kind.LAUGH, Kind.LOUD)]

    reasons: list[str] = []
    if clips:
        views = sum(int(s.meta.get("views", 0)) for s in clips)
        reasons.append(_plural_viewers(len(clips)) + (f" ({views} просм.)" if views else ""))

    if spikes:                       # взрыв чата — берём самый сильный
        reasons.append(max(spikes, key=lambda s: s.weight).detail)

    # Звук: смех / крик / овации / просто громче — по одной строке на вид.
    laughs = [s for s in sounds if s.meta.get("sound") == "laugh"]
    shouts = [s for s in sounds if s.meta.get("sound") == "shout"]
    hypes = [s for s in sounds if s.meta.get("sound") == "hype"]
    louds = [s for s in sounds if s.meta.get("sound") in ("loud", "silence_burst")]
    if laughs:
        reasons.append("смеются" if len(laughs) == 1 else f"смеются ({len(laughs)} раза)")
    if shouts:
        reasons.append("крик")
    if hypes:
        reasons.append("овации")
    if louds:
        if any(s.meta.get("sound") == "silence_burst" for s in louds):
            reasons.append("пауза, а потом резко громче")
        else:
            peak = max(float(s.meta.get("score", 0) or 0) for s in louds)
            reasons.append(f"стало громче обычного в {peak:.1f} раза")

    if speech:                       # самая эмоциональная реплика
        best = max(speech, key=lambda s: s.weight)
        quote = str(best.meta.get("quote") or "")
        reasons.append(f"сказал «{quote}»" if quote else "эмоциональная реплика")
        if any(int(s.meta.get("profanity", 0) or 0) for s in speech):
            reasons.append("на эмоциях")

    if marks:
        roles = [s.meta.get("role", "") for s in marks]
        if "streamer" in roles:
            reasons.append("стример пометил момент")
        elif len(marks) == 1:
            reasons.append(marks[0].detail)
        else:
            reasons.append(f"{len(marks)} меток из чата")

    # Имя момента: заголовок самого популярного клипа, иначе заметка из метки.
    # Пустышки вроде «12» отбрасываем — их заменит цитата из речи (блок 3).
    label = ""
    if clips:
        for s in sorted(clips, key=lambda s: -int(s.meta.get("views", 0))):
            title = str(s.meta.get("title") or "")
            if good_label(title):
                label = title
                break
    if not label:
        label = next((str(s.meta.get("note")) for s in marks
                      if good_label(str(s.meta.get("note") or ""))), "")
    return reasons, label[:80]


def candidates(signals: list[Signal], window: tuple[float, float] = DEFAULT_WINDOW,
               gap: float = CLUSTER_GAP,
               duration: Optional[float] = None) -> list[ScanMoment]:
    """Все кандидаты со счётом (без отбора) — отсортированы по времени."""
    lo, hi = window
    out: list[ScanMoment] = []
    for group in _cluster(signals, gap):
        total = sum(s.weight for s in group)
        if total <= 0:
            continue
        # Центр — средневзвешенное: тяжёлый сигнал тянет момент к себе.
        center = sum(s.t * s.weight for s in group) / total
        families = sorted({s.family for s in group})
        score = total * (1.0 + 0.3 * (len(families) - 1))
        start = max(0.0, center + lo)
        end = center + hi
        if duration:
            end = min(end, duration)
        if end <= start:
            continue
        reasons, label = _reasons(group)
        out.append(ScanMoment(start=start, end=end, center=center, score=round(score, 3),
                              label=label, reasons=reasons, families=families,
                              signals=list(group)))
    return out


def threshold_for(strictness: float, best: float = 0.0) -> float:
    """Ползунок строгости 0…100 → минимальные очки момента.

    `best` — очки лучшего кандидата этого стрима: порог тянется и за ним, иначе на
    канале с сотнями зрительских клипов ползунок не влиял бы ни на что.
    """
    k = min(100.0, max(0.0, float(strictness))) / 100.0
    absolute = SCORE_AT_LOOSE + (SCORE_AT_STRICT - SCORE_AT_LOOSE) * k
    return max(absolute, best * REL_AT_STRICT * k)


def pick(cands: list[ScanMoment], strictness: float = 50.0,
         min_spacing: float = MIN_SPACING, want_max: int = MAX_MOMENTS,
         want_min: int = MIN_MOMENTS) -> list[ScanMoment]:
    """Отобрать 2–8 моментов: по очкам, с разносом по времени, по ползунку строгости.

    Рамки жёсткие: даже на максимальной строгости вернём хотя бы `want_min` (если они
    вообще есть), и никогда больше `want_max`. Строгость двигает только порог очков.
    """
    if not cands:
        return []
    ranked = sorted(cands, key=lambda m: m.score, reverse=True)
    thr = threshold_for(strictness, best=ranked[0].score)

    def spaced(pool: list[ScanMoment], limit: int) -> list[ScanMoment]:
        taken: list[ScanMoment] = []
        for m in pool:
            if len(taken) >= limit:
                break
            if all(abs(m.center - t.center) >= min_spacing for t in taken):
                taken.append(m)
        return taken

    chosen = spaced([m for m in ranked if m.score >= thr], want_max)
    if len(chosen) < want_min:                    # порог съел слишком много — добираем лучшие
        chosen = spaced(ranked, max(want_min, len(chosen)))
    return sorted(chosen, key=lambda m: m.start)


def possible_count(cands: list[ScanMoment], min_spacing: float = MIN_SPACING,
                   want_max: int = MAX_MOMENTS) -> int:
    """Сколько моментов вообще можно набрать — для честного «нашлось 5 из 8 возможных».

    Считается по ТЕМ ЖЕ правилам, что и обычный отбор (включая нижнюю рамку). Иначе
    получалось «нашлось 1 из 0 возможных» — на слабых уликах порог отбрасывал всё,
    а сам отбор всё равно возвращал лучшее.
    """
    return len(pick(cands, strictness=0.0, min_spacing=min_spacing, want_max=want_max))


# --------------------------------------------------------------------------
# Файл разбора
# --------------------------------------------------------------------------

@dataclass
class ScanFile:
    """`.clipscan` — всё, что мы узнали о стриме: сырьё + отобранные моменты."""
    source: dict = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    moments: list[ScanMoment] = field(default_factory=list)
    strictness: float = 50.0
    notes: list[str] = field(default_factory=list)      # честные предупреждения юзеру
    created_at: str = ""
    scan_version: int = SCAN_VERSION
    scoring_version: int = SCORING_VERSION

    # ---- источник ----
    @property
    def duration(self) -> Optional[float]:
        d = self.source.get("duration")
        return float(d) if d else None

    @property
    def channel(self) -> str:
        return self.source.get("channel", "")

    # ---- пересчёт без сети (ползунок строгости) ----
    def rescore(self, strictness: Optional[float] = None,
                window: tuple[float, float] = DEFAULT_WINDOW) -> list[ScanMoment]:
        if strictness is not None:
            self.strictness = strictness
        cands = candidates(self.signals, window=window, duration=self.duration)
        self.moments = pick(cands, self.strictness)
        return self.moments

    def possible(self, window: tuple[float, float] = DEFAULT_WINDOW) -> int:
        return possible_count(candidates(self.signals, window=window,
                                         duration=self.duration))

    def segments(self) -> list[Segment]:
        return [m.to_segment() for m in self.moments]

    # ---- сериализация ----
    def to_dict(self) -> dict:
        return {"version": self.scan_version, "scoring_version": self.scoring_version,
                "created_at": self.created_at or _now_iso(), "source": self.source,
                "strictness": self.strictness, "notes": self.notes,
                "signals": [s.to_dict() for s in self.signals],
                "moments": [m.to_dict() for m in self.moments]}

    @classmethod
    def from_dict(cls, d: dict) -> "ScanFile":
        return cls(source=d.get("source") or {},
                   signals=[Signal.from_dict(s) for s in (d.get("signals") or [])],
                   moments=[ScanMoment.from_dict(m) for m in (d.get("moments") or [])],
                   strictness=float(d.get("strictness", 50.0)),
                   notes=list(d.get("notes") or []),
                   created_at=d.get("created_at", ""),
                   scan_version=int(d.get("version", SCAN_VERSION)),
                   scoring_version=int(d.get("scoring_version", SCORING_VERSION)))

    def to_json(self, path: str) -> None:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    @classmethod
    def from_json(cls, path: str) -> "ScanFile":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def build_scan(source: dict, signals: list[Signal], strictness: float = 50.0,
               notes: Optional[list[str]] = None,
               window: tuple[float, float] = DEFAULT_WINDOW) -> ScanFile:
    """Собрать разбор из готовых сигналов (сеть/файлы — снаружи, здесь чистая логика)."""
    scan = ScanFile(source=dict(source), signals=list(signals), strictness=strictness,
                    notes=list(notes or []), created_at=_now_iso())
    scan.rescore(window=window)
    return scan
