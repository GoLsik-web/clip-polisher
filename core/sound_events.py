"""sound_events.py — Этап 3, блок 2: ЧТО СЛЫШНО в стриме.

Это тот сигнал, ради которого блок 2 и делался: клипы зрителей и чат бывают не у всех,
а звук есть ВСЕГДА. Тут ловится три вещи:

  1. **Громкость** — где стример резко повысил голос относительно СВОЕЙ обычной манеры
     (как и со взрывом чата: сравнение с самим собой, а не с абсолютными децибелами —
     иначе тихий стример не «взрывался» бы никогда).
  2. **Тишина → взрыв** — классика момента: повисла пауза, а потом все заорали. Такой
     рисунок ценнее просто громкого места.
  3. **Смех, крик, аплодисменты** — распознаются моделью YAMNet (AudioSet, 521 класс)
     через уже вшитый onnxruntime. Модель качается один раз (16 МБ) и считает на
     процессоре: 5 минут звука ≈ 0,35 секунды, 3-часовой стрим ≈ 20 секунд.

Если модели нет (не скачалась, нет сети) — ничего не падает: остаются громкость и
тишина→взрыв, а пользователю честно говорится, что смех не распознавался.

Звук читается ПОТОКОМ кусками по 5 минут (`media.iter_pcm`) — 3-часовой стрим не
превращается в 350 МБ в памяти. За один проход считаются и громкость, и модель.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from core.media import SR, AudioSource, iter_pcm

Progress = Callable[[str], None]

FRAME_SEC = 0.5             # шаг «кардиограммы громкости»
YAMNET_HOP = 0.48           # шаг кадров модели (0,96 с окно, половинное перекрытие)

# Классы AudioSet, которые нас интересуют (индексы из assets/yamnet_class_map.csv).
LAUGH_CLASSES = (13, 14, 15, 16, 17, 18)     # Laughter, Giggle, Chuckle, Belly laugh…
HYPE_CLASSES = (61, 62, 64)                  # Cheering, Applause, Crowd
SHOUT_CLASSES = (6, 8, 9, 10, 11)            # Shout, Whoop, Yell, Screaming
GASP_CLASSES = (39,)                         # Gasp — вздох удивления

# Пороги ВЫВЕРЕНЫ НА ЖИВОМ СТРИМЕ (Buster, 2 ч 07 мин, см. CLAUDE.md). Модель даёт
# независимую вероятность 0..1 на каждый класс. На реальном стриме (голос + музыка +
# игра в одной дорожке) уверенность падает: медиана «смеха» 0.00, p99.9 = 0.27,
# максимум 0.63. Поэтому 0.30 — это уже «почти ничего не находим», а рабочий порог
# ниже. Абсолютный пол обязателен: без него на стриме, где смеха НЕТ вообще,
# перцентиль всё равно что-нибудь «найдёт».
LAUGH_MIN = 0.15
HYPE_MIN = 0.15
SHOUT_MIN = 0.15

# Громкость: сглаживаем 3 секундами и берём верхние 2% кадров, но не мягче ×2.5.
# Почему не «×2 от медианы», как было сначала: у речи громкость скачет на каждом слоге,
# и такой порог давал 427 «взрывов» за два часа — сигнал, который срабатывает каждые
# 18 секунд, не значит НИЧЕГО (он «попадает» в любой момент случайно).
LOUD_SMOOTH_SEC = 3.0
LOUD_PERCENTILE = 98.0
LOUD_RATIO_MIN = 2.5
LOUD_MAX_PER_HOUR = 25


class SoundKind:
    LAUGH = "laugh"
    HYPE = "hype"
    SHOUT = "shout"
    LOUD = "loud"
    SILENCE_BURST = "silence_burst"


RU_NAMES = {
    SoundKind.LAUGH: "смех",
    SoundKind.HYPE: "овация/крик толпы",
    SoundKind.SHOUT: "крик",
    SoundKind.LOUD: "резко громче",
    SoundKind.SILENCE_BURST: "пауза, а потом взрыв",
}


@dataclass
class SoundEvent:
    """Одно услышанное событие."""
    kind: str
    start: float
    end: float
    peak_t: float
    score: float                 # 0..1 для модели; для громкости — во сколько раз выше
    detail: str = ""

    @property
    def center(self) -> float:
        return self.peak_t

    def describe(self) -> str:
        return self.detail or RU_NAMES.get(self.kind, self.kind)


@dataclass
class AudioAnalysis:
    """Результат одного прохода по звуку."""
    duration: float = 0.0
    frame_sec: float = FRAME_SEC
    rms: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    events: list[SoundEvent] = field(default_factory=list)
    model_used: bool = False
    note: str = ""

    def of_kind(self, kind: str) -> list[SoundEvent]:
        return [e for e in self.events if e.kind == kind]


# --------------------------------------------------------------------------
# Модель звуков
# --------------------------------------------------------------------------

_session = None
_session_path = ""


def load_class_names() -> list[str]:
    """Названия 521 класса (файл лежит рядом с программой)."""
    from core.resources import res
    path = res("assets/yamnet_class_map.csv")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [row["display_name"] for row in csv.DictReader(f)]


def load_model(path: str = ""):
    """Загрузить ONNX-модель звуков (кэшируется между вызовами)."""
    global _session, _session_path
    from core.provision import yamnet_path
    path = path or yamnet_path()
    if _session is not None and _session_path == path:
        return _session
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.log_severity_level = 3                     # без болтовни в консоль
    _session = ort.InferenceSession(path, sess_options=opts,
                                    providers=["CPUExecutionProvider"])
    _session_path = path
    return _session


def model_available(auto_download: bool = True,
                    progress: Optional[Progress] = None) -> bool:
    """Есть ли модель звуков (при необходимости — скачать)."""
    from core import provision
    if provision.yamnet_ready():
        return True
    if not auto_download:
        return False
    say = progress or (lambda _s: None)
    try:
        provision.ensure_yamnet(lambda _f, text: say(text))
        return True
    except Exception:                               # noqa: BLE001 — нет сети и т.п.
        return False


# --------------------------------------------------------------------------
# Один проход по звуку
# --------------------------------------------------------------------------

def analyze(source: AudioSource, use_model: bool = True,
            progress: Optional[Progress] = None,
            chunk_sec: float = 300.0) -> AudioAnalysis:
    """Пройти по звуку один раз: громкость + (если есть) модель звуков."""
    say = progress or (lambda _s: None)

    session = None
    note = ""
    if use_model:
        if model_available(progress=say):
            try:
                session = load_model()
            except Exception as e:                  # noqa: BLE001
                note = f"Модель звуков не запустилась ({e}) — смех не распознаём."
        else:
            note = ("Модель распознавания звуков не скачалась (нет сети?) — смех и "
                    "аплодисменты в этот раз не ищем, только громкость.")
    if note:
        say(note)

    frame = int(FRAME_SEC * SR)
    rms_parts: list[np.ndarray] = []
    scores_parts: list[np.ndarray] = []
    # (время начала куска, сколько кадров модель по нему выдала) — чтобы время события
    # считалось честно, даже если последний кусок короткий.
    chunk_meta: list[tuple[float, int]] = []
    total = 0.0
    last_said = -60.0

    for t0, block in iter_pcm(source.path, chunk_sec=chunk_sec):
        total = t0 + block.size / SR
        # --- громкость по кадрам ---
        n = block.size // frame
        if n:
            head = block[:n * frame].reshape(n, frame)
            rms_parts.append(np.sqrt((head.astype(np.float64) ** 2).mean(axis=1)))
        # --- модель звуков ---
        if session is not None and block.size > SR:
            try:
                out = session.run(None, {"waveform": block})[0]
                scores_parts.append(out.astype(np.float32))
                chunk_meta.append((t0, int(out.shape[0])))
            except Exception as e:                  # noqa: BLE001 — не роняем разбор
                session = None
                note = f"Модель звуков сломалась на середине ({e}) — дальше по громкости."
                say(note)
        if t0 - last_said >= 900:                   # раз в 15 минут звука — весточка
            last_said = t0
            say(f"Слушаю запись… {int(t0 // 60)} мин")

    rms = (np.concatenate(rms_parts) if rms_parts else np.zeros(0, dtype=np.float64))
    duration = source.duration or total

    events: list[SoundEvent] = []
    events += find_loud_events(rms, FRAME_SEC)
    if scores_parts:
        scores = np.concatenate(scores_parts, axis=0)
        events += find_model_events(scores, chunk_meta)
    events.sort(key=lambda e: e.center)

    return AudioAnalysis(duration=duration, frame_sec=FRAME_SEC,
                         rms=rms.astype(np.float32), events=events,
                         model_used=bool(scores_parts), note=note)


# --------------------------------------------------------------------------
# Громкость: взрыв относительно собственной манеры стримера
# --------------------------------------------------------------------------

def find_loud_events(rms: np.ndarray, step: float, ratio_min: float = LOUD_RATIO_MIN,
                     history_sec: float = 120.0, join_gap: float = 3.0,
                     silence_ratio: float = 0.45, silence_min: float = 1.5,
                     percentile: float = LOUD_PERCENTILE,
                     max_per_hour: float = LOUD_MAX_PER_HOUR) -> list[SoundEvent]:
    """Найти места, где стало РЕЗКО громче обычного (и особо — «тишина → взрыв»).

    Три вещи, без которых сигнал бесполезен (проверено на живом стриме):
      * СГЛАЖИВАНИЕ 3 с — момент это «стал громче на несколько секунд», а не пик
        на одном слоге;
      * обычный уровень = скользящая медиана за 2 минуты ДО места, поэтому тихий
        стример и крикливый меряются каждый по себе;
      * порог берётся по САМОЙ записи (верхние 2% кадров), но не мягче ×2.5 — иначе
        на ровном стриме «взрывом» объявится обычный разговор.
    """
    if rms.size < 10:
        return []
    hist = max(4, int(history_sec / step))
    x = rms.astype(np.float64)
    win = max(1, int(round(LOUD_SMOOTH_SEC / step)))
    smooth = np.convolve(x, np.ones(win) / win, mode="same")
    floor = max(float(np.median(smooth)) * 0.05, 1e-4)    # чтобы не делить на тишину

    base = np.empty_like(smooth)
    for i in range(smooth.size):
        window = smooth[max(0, i - hist):i] if i else smooth[:1]
        base[i] = max(float(np.median(window)), floor)
    ratio = smooth / base

    thr = max(ratio_min, float(np.percentile(ratio, percentile)))
    hot = np.nonzero(ratio >= thr)[0]
    if hot.size == 0:
        return []

    out: list[SoundEvent] = []
    group: list[int] = []
    for i in hot:
        if group and (i - group[-1]) * step > join_gap:
            out.append(_loud_event(group, x, ratio, base, step,
                                   silence_ratio, silence_min))
            group = []
        group.append(int(i))
    if group:
        out.append(_loud_event(group, x, ratio, base, step, silence_ratio,
                               silence_min))

    # Ограничение сверху: на длинной записи даже верхние 2% дают перебор. Оставляем
    # самые сильные — остальные всё равно потонули бы в отборе моментов.
    limit = max(5, int(max_per_hour * (rms.size * step) / 3600.0))
    if len(out) > limit:
        out = sorted(sorted(out, key=lambda e: e.score, reverse=True)[:limit],
                     key=lambda e: e.center)
    return out


def _loud_event(group: list[int], raw: np.ndarray, ratio: np.ndarray, base: np.ndarray,
                step: float, silence_ratio: float, silence_min: float) -> SoundEvent:
    """Собрать событие. ⚠️ Паузу ищем по СЫРОЙ громкости: сглаживание 3 с «замазывает»
    тишину соседним взрывом, и «тишина→взрыв» переставала распознаваться."""
    peak_i = int(max(group, key=lambda i: ratio[i]))
    r = float(ratio[peak_i])

    # Была ли перед взрывом ПАУЗА: несколько кадров заметно тише обычного.
    quiet_frames = max(2, int(silence_min / step))
    lo = max(0, group[0] - quiet_frames - 1)
    before = raw[lo:group[0]]
    was_silence = (before.size >= quiet_frames
                   and float(before.max()) < base[group[0]] * silence_ratio)

    kind = SoundKind.SILENCE_BURST if was_silence else SoundKind.LOUD
    detail = (f"тишина, а потом резко громче (×{r:.1f})" if was_silence
              else f"стало громче обычного в {r:.1f} раза")
    return SoundEvent(kind=kind, start=group[0] * step, end=(group[-1] + 1) * step,
                      peak_t=peak_i * step + step / 2, score=r, detail=detail)


# --------------------------------------------------------------------------
# Модель: смех, крик, овации
# --------------------------------------------------------------------------

def find_model_events(scores: np.ndarray, chunk_meta: list[tuple[float, int]],
                      hop: float = YAMNET_HOP, join_gap: float = 1.5) -> list[SoundEvent]:
    """Кадры модели → события «смех/крик/овация».

    ⚠️ Время кадра считается по РЕАЛЬНОЙ раскладке кусков (`chunk_meta` = время начала
    куска + сколько кадров он дал). Считать «по формуле» нельзя: последний кусок короче
    остальных, и время событий в конце стрима поехало бы.
    """
    if scores.size == 0:
        return []

    # Таблица: индекс кадра → время в записи.
    times = np.empty(scores.shape[0], dtype=np.float64)
    pos = 0
    for t0, n in chunk_meta:
        n = min(n, times.size - pos)
        if n <= 0:
            break
        times[pos:pos + n] = t0 + np.arange(n) * hop
        pos += n
    if pos < times.size:                       # на всякий случай — хвост без метаданных
        last = times[pos - 1] if pos else 0.0
        times[pos:] = last + np.arange(1, times.size - pos + 1) * hop

    def frame_time(i: int) -> float:
        return float(times[i])

    groups = [(SoundKind.LAUGH, LAUGH_CLASSES, LAUGH_MIN),
              (SoundKind.HYPE, HYPE_CLASSES, HYPE_MIN),
              (SoundKind.SHOUT, SHOUT_CLASSES, SHOUT_MIN)]

    out: list[SoundEvent] = []
    for kind, classes, thr in groups:
        col = scores[:, list(classes)].max(axis=1)
        hot = np.nonzero(col >= thr)[0]
        if hot.size == 0:
            continue
        group: list[int] = []
        for i in hot:
            if group and (frame_time(int(i)) - frame_time(group[-1])) > join_gap:
                out.append(_model_event(kind, group, col, frame_time))
                group = []
            group.append(int(i))
        if group:
            out.append(_model_event(kind, group, col, frame_time))
    return out


def _model_event(kind: str, group: list[int], col: np.ndarray, frame_time) -> SoundEvent:
    peak_i = int(max(group, key=lambda i: col[i]))
    score = float(col[peak_i])
    length = frame_time(group[-1]) - frame_time(group[0]) + 0.96
    if kind == SoundKind.LAUGH:
        detail = "смеются" + (" долго" if length >= 4 else "")
    elif kind == SoundKind.HYPE:
        detail = "овации/шум толпы"
    else:
        detail = "крик"
    return SoundEvent(kind=kind, start=frame_time(group[0]), end=frame_time(group[-1]) + 0.96,
                      peak_t=frame_time(peak_i) + 0.48, score=score, detail=detail)


# --------------------------------------------------------------------------
# В сигналы разбора
# --------------------------------------------------------------------------

def signals_from_audio(analysis: AudioAnalysis) -> list:
    """События звука → сигналы для `clipscan` (веса подобраны против клипов зрителей)."""
    from core.clipscan import Kind, Signal

    weights = {
        SoundKind.LAUGH: 2.6,            # самый честный признак «смешно»
        SoundKind.HYPE: 2.0,
        SoundKind.SHOUT: 1.6,
        SoundKind.SILENCE_BURST: 1.6,    # тишина→взрыв ценнее просто громкого
        SoundKind.LOUD: 1.0,
    }
    out = []
    for e in analysis.events:
        base = weights.get(e.kind, 1.0)
        if e.kind in (SoundKind.LOUD, SoundKind.SILENCE_BURST):
            # score здесь — во сколько раз громче: ×2 → как есть, ×4 и выше → в 1,6 раза
            base *= min(1.6, 0.6 + e.score / 5.0)
            kind = Kind.LOUD
        else:
            base *= min(1.4, 0.7 + e.score)         # уверенность модели
            kind = Kind.LAUGH if e.kind == SoundKind.LAUGH else Kind.LOUD
        out.append(Signal(kind=kind, t=e.center, weight=round(base, 3),
                          detail=e.describe(),
                          meta={"sound": e.kind, "score": round(e.score, 3),
                                "start": round(e.start, 2), "end": round(e.end, 2)}))
    return out
