"""chatpulse.py — Этап 3, сигнал №2: ПУЛЬС ЧАТА («кардиограмма» эфира).

Что это. Пока бот сидит в чате и ловит `!clip`, он попутно ведёт НЕВИДИМЫЙ журнал:
раз в 10 секунд записывает, сколько было сообщений, сколько РАЗНЫХ людей писало,
сколько было смеха и хайпа и какие 2-3 слова/эмоута повторялись чаще всего.
В чат из этого журнала ничего не уходит — он лежит файлом рядом с метками.

Зачем. Потом, при разборе стрима, взрыв чата — это улика: «на 1:12:30 писали в 6 раз
активнее обычного, сплошной ору/KEKW» → почти наверняка там момент. А топ-эмоуты дают
ЧЕЛОВЕЧЕСКОЕ объяснение на карточке момента, а не «жар 87».

Важно: «взрыв» считается относительно СОБСТВЕННОГО обычного темпа канала, а не по
абсолютным числам. Иначе у камерных стримеров не «взрывалось» бы никогда, а у крупных
взрывалось бы постоянно.

Формат файла — `.chatpulse` рядом с `.clipmarks` (то же имя, другое расширение).
Хранятся только НЕПУСТЫЕ корзины: минуты тишины места не занимают.

Только стандартная библиотека, никакого Qt.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from typing import Optional

BUCKET_SEC = 10.0          # шаг «кардиограммы»
TOP_K = 3                  # сколько частых слов держать в корзине

# --------------------------------------------------------------------------
# Словарики смеха и хайпа
# --------------------------------------------------------------------------
# Куски слов: ловим «ахахах», «ахаахах», «ржунимагу» и т.п. одной проверкой.
_LAUGH_PARTS = ("ахах", "хаха", "хах", "ахх", "ору", "ржу", "ржа", "рофл", "кек",
                "лол", "угар", "ахаха")
# Точные слова/эмоуты (сравниваем в нижнем регистре).
_LAUGH_WORDS = {"kekw", "lulw", "lul", "omegalul", "lol", "lmao", "xd", "kek",
                "kekwait", "icant", "ahahah", "haha", "hah"}
_HYPE_WORDS = {"pog", "poggers", "pogchamp", "pogu", "gg", "ez", "w", "ws", "wow",
               "hype", "letsgo", "clap", "база", "имба", "красава", "топ", "вау",
               "ого", "сила", "зарешал", "нокаут", "жёстко", "жестко", "мощь",
               "камон", "давай", "го"}
# Слова-паразиты, которые не несут смысла в «топе» момента.
_BORING = {"и", "а", "но", "да", "нет", "это", "что", "как", "ну", "же", "бы", "не",
           "the", "a", "is", "to", "of", "in", "it", "you", "we", "he", "she"}

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]{2,20}")


def classify(text: str) -> tuple[bool, bool, list[str]]:
    """Сообщение → (есть смех, есть хайп, список «заметных» слов для топа)."""
    low = (text or "").lower()
    laugh = low.count(")") >= 3 or any(p in low for p in _LAUGH_PARTS)
    hype = False
    keys: list[str] = []

    for w in _WORD_RE.findall(text or ""):
        lw = w.lower()
        if lw in _LAUGH_WORDS:
            laugh = True
        if lw in _HYPE_WORDS:
            hype = True
        # В топ идут: эмоуты/словечки из словарей, КАПС (эмоции) и повторяемые слова.
        if lw in _LAUGH_WORDS or lw in _HYPE_WORDS:
            keys.append(w if w.isupper() else lw)
        elif w.isupper() and len(w) >= 3 and not w.isdigit():
            keys.append(w)
        elif lw not in _BORING and len(lw) >= 4:
            keys.append(lw)
    return laugh, hype, keys


# --------------------------------------------------------------------------
# Формат файла
# --------------------------------------------------------------------------

@dataclass
class Bucket:
    """Одна корзина «кардиограммы» (по умолчанию 10 секунд эфира)."""
    t: float                       # начало корзины, секунды от старта эфира
    n: int = 0                     # сообщений
    u: int = 0                     # разных авторов
    laugh: int = 0                 # сообщений со смехом
    hype: int = 0                  # сообщений с хайпом
    top: list[list] = field(default_factory=list)   # [["ору", 5], ["KEKW", 3]]

    def to_dict(self) -> dict:
        d = {"t": round(self.t, 1), "n": self.n, "u": self.u}
        if self.laugh:
            d["laugh"] = self.laugh
        if self.hype:
            d["hype"] = self.hype
        if self.top:
            d["top"] = [[w, c] for w, c in self.top]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Bucket":
        return cls(t=float(d.get("t", 0.0)), n=int(d.get("n", 0)), u=int(d.get("u", 0)),
                   laugh=int(d.get("laugh", 0)), hype=int(d.get("hype", 0)),
                   top=[list(x) for x in (d.get("top") or [])])


@dataclass
class PulseLog:
    """Журнал пульса одного эфира."""
    platform: str = "twitch"
    streamer: str = ""
    broadcast_id: str = ""
    started_at: str = ""
    bucket: float = BUCKET_SEC
    duration: Optional[float] = None
    buckets: list[Bucket] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"version": 1, "platform": self.platform, "streamer": self.streamer,
                "broadcast_id": self.broadcast_id, "started_at": self.started_at,
                "bucket": self.bucket, "duration": self.duration,
                "buckets": [b.to_dict() for b in self.buckets]}

    @classmethod
    def from_dict(cls, d: dict) -> "PulseLog":
        return cls(platform=d.get("platform", "twitch"), streamer=d.get("streamer", ""),
                   broadcast_id=str(d.get("broadcast_id", "")),
                   started_at=d.get("started_at", ""),
                   bucket=float(d.get("bucket", BUCKET_SEC)),
                   duration=(float(d["duration"]) if d.get("duration") is not None else None),
                   buckets=[Bucket.from_dict(b) for b in (d.get("buckets") or [])])

    def to_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, path: str) -> "PulseLog":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    @property
    def total_messages(self) -> int:
        return sum(b.n for b in self.buckets)

    @property
    def avg_per_min(self) -> float:
        """Средний темп чата за эфир (сообщений в минуту)."""
        span = self.duration or (self.buckets[-1].t + self.bucket if self.buckets else 0.0)
        return (self.total_messages / span * 60.0) if span else 0.0


def pulse_path_for(marks_path: str) -> str:
    """Журнал пульса лежит рядом с файлом меток — то же имя, расширение .chatpulse."""
    return os.path.splitext(marks_path)[0] + ".chatpulse"


# --------------------------------------------------------------------------
# Сбор во время эфира
# --------------------------------------------------------------------------

class PulseCollector:
    """Копит корзины по ходу чата и сбрасывает журнал на диск.

    Сообщения складываются в текущую корзину; как только пошло время следующей —
    предыдущая закрывается. Пустые корзины (тишина) не хранятся вовсе.

    ⚠️ Счётчики трогают ДВА потока: чат (`feed`) и таймер бота (`tick`). Без замка
    закрытие корзины могло совпасть с приходом сообщения — и сообщение терялось
    (ловилось тестом: в журнал попадало 4 из 5).
    """

    def __init__(self, channel: str, output_path: str, ref_epoch: Optional[float] = None,
                 bucket: float = BUCKET_SEC, broadcast_id: str = "", started_at: str = "",
                 top_k: int = TOP_K, save_every: float = 30.0):
        self.channel = channel
        self.output_path = output_path
        self.ref_epoch = ref_epoch if ref_epoch is not None else time.time()
        self.bucket = max(1.0, bucket)
        self.broadcast_id = broadcast_id
        self.started_at = started_at
        self.top_k = top_k
        self.save_every = save_every
        self.buckets: list[Bucket] = []
        self.duration: Optional[float] = None

        self._idx: Optional[int] = None            # индекс текущей корзины
        self._n = 0
        self._users: set[str] = set()
        self._laugh = 0
        self._hype = 0
        self._words: Counter = Counter()
        self._last_msg_epoch: Optional[float] = None
        self._last_save = 0.0
        self._dirty = False
        self._lock = threading.Lock()

    # ---- накопление ----
    def feed(self, login: str, text: str, now: Optional[float] = None) -> None:
        """Учесть одно сообщение чата (ЛЮБОЕ, не только команду)."""
        now = time.time() if now is None else now
        idx = int(max(0.0, now - self.ref_epoch) // self.bucket)
        laugh, hype, keys = classify(text)
        with self._lock:
            if self._idx is not None and idx != self._idx:
                self._close(idx)
            if self._idx is None:
                self._idx = idx
            self._n += 1
            self._users.add((login or "?").lower())
            self._laugh += 1 if laugh else 0
            self._hype += 1 if hype else 0
            for k in keys:
                self._words[k] += 1
            self._last_msg_epoch = now
            self._dirty = True

    def tick(self, now: Optional[float] = None) -> list[Bucket]:
        """Позвать по таймеру: закрывает отжившую корзину и изредка пишет файл.

        Возвращает корзины, закрытые прямо сейчас (боту — чтобы среагировать на взрыв).
        """
        now = time.time() if now is None else now
        idx = int(max(0.0, now - self.ref_epoch) // self.bucket)
        closed: list[Bucket] = []
        save = False
        with self._lock:
            if self._idx is not None and idx != self._idx:
                b = self._close(idx)
                if b is not None:
                    closed.append(b)
            if self._dirty and (now - self._last_save) >= self.save_every:
                save = True
                self._last_save = now
        if save:
            self.save()
        return closed

    def _close(self, new_idx: Optional[int]) -> Optional[Bucket]:
        """Закрыть текущую корзину и начать новую."""
        b: Optional[Bucket] = None
        if self._idx is not None and self._n:
            top = [[w, c] for w, c in self._words.most_common(self.top_k) if c > 1]
            if not top:                       # ничего не повторялось — берём самое частое
                top = [[w, c] for w, c in self._words.most_common(1)]
            b = Bucket(t=self._idx * self.bucket, n=self._n, u=len(self._users),
                       laugh=self._laugh, hype=self._hype, top=top)
            self.buckets.append(b)
        self._idx = new_idx
        self._n = 0
        self._users = set()
        self._laugh = 0
        self._hype = 0
        self._words = Counter()
        return b

    # ---- живые показатели (для болталки и !отчёта) ----
    @property
    def last_bucket(self) -> Optional[Bucket]:
        return self.buckets[-1] if self.buckets else None

    @property
    def has_data(self) -> bool:
        """Есть что писать? (сообщения текущей корзины ещё не закрыты — но они есть)"""
        return bool(self.buckets or self._n)

    def silence_sec(self, now: Optional[float] = None) -> Optional[float]:
        """Сколько секунд в чате тишина (None — сообщений ещё не было вовсе)."""
        if self._last_msg_epoch is None:
            return None
        return max(0.0, (time.time() if now is None else now) - self._last_msg_epoch)

    def rate_per_min(self, window_sec: float = 120.0, now: Optional[float] = None) -> float:
        """Темп чата за последние N секунд (сообщений в минуту)."""
        now = time.time() if now is None else now
        edge = max(0.0, (now - self.ref_epoch) - window_sec)
        n = sum(b.n for b in self.buckets if b.t >= edge) + self._n
        return n / window_sec * 60.0

    def live_ratio(self, now: Optional[float] = None) -> float:
        """Во сколько раз последняя корзина горячее обычного темпа эфира.

        Пока истории мало — считаем, что всё обычно: иначе бот радовался бы «взрыву»
        на первых же сообщениях эфира.
        """
        if len(self.buckets) <= MIN_HISTORY:
            return 1.0
        base = _baseline([b.n for b in self.buckets[:-1]][-30:])
        return (self.buckets[-1].n / base) if base > 0 else 1.0

    # ---- файл ----
    def as_log(self) -> PulseLog:
        return PulseLog(streamer=self.channel, broadcast_id=self.broadcast_id,
                        started_at=self.started_at, bucket=self.bucket,
                        duration=self.duration, buckets=list(self.buckets))

    def resume_existing(self) -> int:
        """Подхватить журнал этого же эфира (перезапуск программы посреди стрима)."""
        if not os.path.isfile(self.output_path):
            return 0
        try:
            old = PulseLog.from_json(self.output_path)
        except (OSError, ValueError):
            return 0
        self.buckets = list(old.buckets) + self.buckets
        return len(old.buckets)

    def save(self) -> None:
        path = self.output_path
        if not path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        self.as_log().to_json(tmp)
        os.replace(tmp, path)        # атомарно: файл всегда целый
        self._dirty = False

    def finalize(self, end_epoch: Optional[float] = None) -> None:
        with self._lock:
            self._close(None)
        end = end_epoch if end_epoch is not None else time.time()
        self.duration = max(0.0, round(end - self.ref_epoch, 1)) or self.duration
        self.save()


# --------------------------------------------------------------------------
# Разбор журнала: где чат взрывался
# --------------------------------------------------------------------------

def _baseline(values: list[int]) -> float:
    """Обычный темп: медиана (устойчива к одиночным всплескам), но не ниже пола."""
    if not values:
        return 1.0
    return max(median(values), 0.6)


@dataclass
class Spike:
    """Взрыв чата — кусок эфира, где писали заметно активнее обычного."""
    start: float
    end: float
    peak_t: float                  # где было горячее всего
    ratio: float                   # во сколько раз выше обычного
    messages: int
    chatters: int
    laugh: int
    hype: int
    top: list[list] = field(default_factory=list)

    @property
    def center(self) -> float:
        return self.peak_t

    def describe(self) -> str:
        """Человеческое объяснение для карточки момента."""
        parts = [f"чат ×{self.ratio:.1f}"]
        if self.laugh and self.laugh * 2 >= self.messages:
            parts.append("сплошной смех")
        elif self.laugh:
            parts.append(f"смех ({self.laugh})")
        if self.hype:
            parts.append("хайп")
        words = ", ".join(str(w) for w, _c in self.top[:2])
        if words:
            parts.append(f"«{words}»")
        return " · ".join(parts)


MIN_HISTORY = 6          # меньше минуты истории — сравнивать не с чем


def find_spikes(log: PulseLog, min_ratio: float = 2.2, min_messages: int = 3,
                history: int = 30, join_gap: float = 30.0) -> list[Spike]:
    """Найти взрывы чата в журнале.

    `history` — сколько корзин назад смотрим «обычный темп» (30 × 10 с = 5 минут).
    Соседние горячие корзины (разрыв ≤ join_gap) склеиваются в ОДИН взрыв.

    ⚠️ В самом начале записи истории ещё нет, и любая живая корзина выглядела бы
    «взрывом ×8». Поэтому первые корзины сравниваются со средним темпом ВСЕГО эфира.
    """
    if not log.buckets:
        return []
    step = log.bucket
    by_idx = {int(round(b.t / step)): b for b in log.buckets}
    last = max(by_idx)
    rates = [(by_idx[i].n if i in by_idx else 0) for i in range(last + 1)]
    whole = _baseline(rates)

    hot: list[tuple[int, float]] = []           # (индекс корзины, во сколько раз выше)
    for i, n in enumerate(rates):
        if n < min_messages:
            continue
        window = rates[max(0, i - history):i]
        base = _baseline(window) if len(window) >= MIN_HISTORY else whole
        ratio = n / base
        if ratio >= min_ratio:
            hot.append((i, ratio))
    if not hot:
        return []

    spikes: list[Spike] = []
    group: list[tuple[int, float]] = []
    for item in hot:
        if group and (item[0] - group[-1][0]) * step > join_gap:
            spikes.append(_spike_from(group, by_idx, step))
            group = []
        group.append(item)
    if group:
        spikes.append(_spike_from(group, by_idx, step))
    return spikes


def _spike_from(group: list[tuple[int, float]], by_idx: dict[int, Bucket],
                step: float) -> Spike:
    idxs = [i for i, _r in group]
    peak_i, peak_ratio = max(group, key=lambda x: x[1])
    words: Counter = Counter()
    msgs = chatters = laugh = hype = 0
    for i in idxs:
        b = by_idx.get(i)
        if not b:
            continue
        msgs += b.n
        chatters = max(chatters, b.u)
        laugh += b.laugh
        hype += b.hype
        for w, c in b.top:
            words[w] += int(c)
    return Spike(start=min(idxs) * step, end=(max(idxs) + 1) * step,
                 peak_t=peak_i * step + step / 2.0, ratio=round(peak_ratio, 2),
                 messages=msgs, chatters=chatters, laugh=laugh, hype=hype,
                 top=[[w, c] for w, c in words.most_common(3)])
