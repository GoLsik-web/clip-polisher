"""speech.py — Этап 3, блок 3: РЕЧЬ по кандидатам + название момента.

Зачем отдельный модуль, если распознавание речи в программе уже есть: `transcribe.py`
расшифровывает файл ЦЕЛИКОМ, а трёхчасовой стрим так слушать бессмысленно — уйдут часы.
Здесь речь распознаётся ТОЛЬКО в окнах кандидатов, которые нашли клипы, чат и звук.
Это те самые «два прохода» из ТЗ: 20-40 кусков по полминуты — это 15-25 минут звука
вместо трёх часов.

Что даёт речь:
  * **сигнал** — эмоциональная реплика («да ты гонишь», мат, «я в шоке», крик повторами)
    подтверждает момент независимо от чата и клипов;
  * **имя момента** — живая цитата вместо «Момент 3». Если у момента уже есть имя от
    зрительского клипа, оно остаётся: человек назвал лучше машины.

Про слабые машины (решение пользователя): молча заставлять человека ждать полчаса
нельзя. Поэтому есть `plan()` — честная оценка времени ДО начала работы, выбор модели
(от самой точной до самой быстрой), возможность пропустить речь и прервать на полпути.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Optional

Progress = Callable[[str], None]
ShouldStop = Callable[[], bool]

# Модели по убыванию точности. «Насколько дольше звука» — грубая прикидка для
# честной оценки времени (на глаз хуже, чем ничего: человек хотя бы знает, чего ждать).
# ⚠️ Множители ЗАМЕРЕНЫ, а не выдуманы: large-v3 на RTX 3060 Ti в быстром режиме —
# 30 с на 180 с звука (≈0,11 чистого распознавания + накладные). Врать в оценке нельзя:
# человек по ней решает, ждать или переключиться на модель поменьше.
MODELS: list[tuple[str, str, float, float]] = [
    # (имя, как показать, во сколько раз медленнее реального времени на GPU / на CPU)
    ("large-v3", "Самая точная (по умолчанию)", 0.11, 1.30),
    ("medium", "Точная", 0.07, 0.70),
    ("small", "Быстрая", 0.03, 0.25),
    ("base", "Самая быстрая (грубее)", 0.02, 0.12),
]
MODEL_NAMES = [m[0] for m in MODELS]
DEFAULT_MODEL = "auto"

# Сколько секунд ожидания считаем «долго» — дальше предупреждаем человека.
LONG_WAIT_SEC = 300.0


# --------------------------------------------------------------------------
# Эмоциональные маркеры речи
# --------------------------------------------------------------------------

# Фразы-реакции. Ловим по куску слова, чтобы формы («гонишь/гоните») тоже считались.
_MARKERS = (
    "да ты гонишь", "ты гонишь", "не может быть", "я в шоке", "что это было",
    "вот это да", "офиге", "капец", "жесть", "нифига", "ничего себе", "серьёзно",
    "ты видел", "вы видели", "боже", "господи", "с ума сойти", "не верю",
    "что за", "как так", "я не могу", "это конец", "легенда", "имба", "красава",
    "невероятно", "ужас", "кошмар", "стоп стоп", "погоди", "оу", "вау", "ого",
)
_EXCLAIM = re.compile(r"[!?]{1,}")
_WORD = re.compile(r"[А-Яа-яЁёA-Za-z]+")


@dataclass
class WindowSpeech:
    """Что услышали в окне одного кандидата."""
    start: float                  # начало окна во времени записи
    end: float
    text: str = ""
    quote: str = ""               # самая «живая» фраза — кандидат в имя момента
    markers: list[str] = field(default_factory=list)
    profanity: int = 0
    repeats: int = 0              # повторы одного слова подряд («нет нет нет»)
    exclaims: int = 0

    @property
    def heat(self) -> float:
        """Насколько эмоциональной была речь в окне (0 — ровная)."""
        return (len(self.markers) * 1.0 + self.profanity * 0.8
                + self.repeats * 0.9 + min(self.exclaims, 3) * 0.4)

    def describe(self) -> str:
        bits = []
        if self.quote:
            bits.append(f"сказал «{self.quote}»")
        elif self.markers:
            bits.append("эмоциональная реплика")
        if self.profanity:
            bits.append("на эмоциях")
        return " · ".join(bits) or "речь"


def analyze_text(text: str, words: Optional[list] = None) -> WindowSpeech:
    """Разобрать расшифровку окна: маркеры, мат, повторы, цитата."""
    ws = WindowSpeech(start=0.0, end=0.0, text=(text or "").strip())
    low = ws.text.lower()

    ws.markers = [m for m in _MARKERS if m in low]
    ws.exclaims = len(_EXCLAIM.findall(ws.text))

    # Мат — берём из уже готового модуля программы (там же и нормализация форм).
    try:
        from core.profanity import is_profane, normalize
        ws.profanity = sum(1 for w in _WORD.findall(low) if is_profane(normalize(w)))
    except Exception:                       # noqa: BLE001 — без мата тоже проживём
        ws.profanity = 0

    # Повтор одного слова подряд — верный признак эмоции («нет-нет-нет», «что что»).
    seq = [w.lower() for w in _WORD.findall(low) if len(w) > 1]
    run = 1
    for a, b in zip(seq, seq[1:]):
        if a == b:
            run += 1
            if run == 3:                    # три подряд — засчитали один «повтор»
                ws.repeats += 1
        else:
            run = 1

    ws.quote = pick_quote(ws.text, ws.markers)
    return ws


def pick_quote(text: str, markers: Optional[list[str]] = None) -> str:
    """Выбрать короткую живую фразу — она станет именем момента.

    Правила простые и честные: берём предложение с эмоцией, режем до 60 символов,
    выкидываем мусорные обрывки. Ничего не сочиняем — только то, что реально сказано.
    """
    text = (text or "").strip()
    if not text:
        return ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
    if not parts:
        parts = [text]

    def ok(p: str) -> bool:
        n = len(_WORD.findall(p))
        return 2 <= n <= 12

    good = [p for p in parts if ok(p)]
    pool = good or parts
    marked = [p for p in pool if any(m in p.lower() for m in (markers or []))]
    best = (marked or pool)[0]
    if len(best) > 60:
        best = best[:57].rsplit(" ", 1)[0] + "…"
    return best.strip(" .,-—")


# --------------------------------------------------------------------------
# План: что и сколько это займёт
# --------------------------------------------------------------------------

@dataclass
class SpeechPlan:
    """Честный ответ на вопрос «сколько ждать и на чём считаем»."""
    model: str = "large-v3"
    device: str = "cpu"
    windows: int = 0
    audio_sec: float = 0.0
    est_sec: float = 0.0
    skip: bool = False

    @property
    def long(self) -> bool:
        return self.est_sec >= LONG_WAIT_SEC

    def human(self) -> str:
        if self.skip:
            return "Речь не распознаём (выключено)"
        where = "на видеокарте" if self.device == "cuda" else "на процессоре"
        return (f"Речь по {self.windows} местам ({_mmss(self.audio_sec)} звука), "
                f"модель «{self.model}» {where} — примерно {_mmss(self.est_sec)}")

    def advice(self) -> str:
        """Что предложить человеку, если ждать долго."""
        if self.skip or not self.long:
            return ""
        faster = next((m for m in MODELS if m[0] != self.model
                       and _mult(m[0], self.device) < _mult(self.model, self.device)), None)
        tip = ""
        if faster:
            secs = self.audio_sec * _mult(faster[0], self.device)
            tip = (f" Можно взять модель «{faster[0]}» — выйдет примерно "
                   f"{_mmss(secs)}, но названия из речи будут грубее.")
        return ("Это долго, потому что нет видеокарты (или она не подхватилась)." + tip
                + " Речь можно и пропустить: моменты всё равно найдутся, просто без "
                  "названий из сказанного.")


def _mult(model: str, device: str) -> float:
    for name, _title, gpu, cpu in MODELS:
        if name == model:
            return gpu if device == "cuda" else cpu
    return 1.0


def detect_device(prefer_gpu: bool = True) -> str:
    """Что реально доступно: 'cuda' или 'cpu'."""
    if not prefer_gpu:
        return "cpu"
    try:
        from core import provision
        return "cuda" if (provision.has_nvidia() and provision.cuda_ready()) else "cpu"
    except Exception:                       # noqa: BLE001
        return "cpu"


def plan(windows: list[tuple[float, float]], model: str = DEFAULT_MODEL,
         prefer_gpu: bool = True) -> SpeechPlan:
    """Сколько займёт распознавание речи по этим окнам — ДО того, как начать."""
    audio = sum(max(0.0, e - s) for s, e in windows)
    device = detect_device(prefer_gpu)
    if model == "skip":
        return SpeechPlan(model="skip", device=device, windows=len(windows),
                          audio_sec=audio, est_sec=0.0, skip=True)
    if model in (DEFAULT_MODEL, "", None):
        # «Авто»: на видеокарте не жалеем точности, на процессоре берём разумный
        # компромисс — иначе человек ждёт полчаса и думает, что программа зависла.
        model = "large-v3" if device == "cuda" else "small"
    if model not in MODEL_NAMES:
        model = "large-v3"
    # Само распознавание + вырезание каждого окна (~1,5 с) + загрузка модели (~6 с).
    est = audio * _mult(model, device) + 1.5 * len(windows) + 6.0
    return SpeechPlan(model=model, device=device, windows=len(windows),
                      audio_sec=audio, est_sec=est)


def with_model(base: SpeechPlan, model: str) -> SpeechPlan:
    """Тот же план, но другой моделью (человек выбрал побыстрее) — с пересчётом времени."""
    if model == "skip":
        return SpeechPlan(model="skip", device=base.device, windows=base.windows,
                          audio_sec=base.audio_sec, est_sec=0.0, skip=True)
    if model not in MODEL_NAMES:
        return base
    est = base.audio_sec * _mult(model, base.device) + 1.5 * base.windows + 6.0
    return SpeechPlan(model=model, device=base.device, windows=base.windows,
                      audio_sec=base.audio_sec, est_sec=est)


# --------------------------------------------------------------------------
# Распознавание по окнам
# --------------------------------------------------------------------------

def transcribe_windows(audio_path: str, windows: list[tuple[float, float]],
                       speech_plan: Optional[SpeechPlan] = None,
                       language: str = "ru",
                       progress: Optional[Progress] = None,
                       should_stop: Optional[ShouldStop] = None) -> list[WindowSpeech]:
    """Расшифровать ТОЛЬКО окна кандидатов. Прерывается в любой момент."""
    say = progress or (lambda _s: None)
    stop = should_stop or (lambda: False)
    p = speech_plan or plan(windows)
    if p.skip or not windows:
        return []

    from core.media import extract_window
    from core.transcribe import transcribe_file

    out: list[WindowSpeech] = []
    tmp = os.path.join(tempfile.gettempdir(), f"clip_polisher_speech_{os.getpid()}.wav")
    for i, (start, end) in enumerate(windows, 1):
        if stop():
            say(f"Речь прервана — успели разобрать {len(out)} из {len(windows)}")
            break
        try:
            extract_window(audio_path, start, max(1.0, end - start), tmp)
            res = transcribe_file(tmp, language=language, model_size=p.model,
                                  prefer_gpu=(p.device == "cuda"), denoise=False,
                                  quality="fast")     # ищем смысл, а не тайминг слов
            ws = analyze_text(res.text)
        except Exception as e:              # noqa: BLE001 — одно окно не роняет разбор
            say(f"Окно {i}: не получилось разобрать речь ({e})")
            continue
        ws.start, ws.end = start, end
        out.append(ws)
        if ws.quote:
            say(f"Речь {i}/{len(windows)}: «{ws.quote}»")
        else:
            say(f"Речь {i}/{len(windows)}: тихо")
    try:
        os.remove(tmp)
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------
# В сигналы и в названия моментов
# --------------------------------------------------------------------------

def signals_from_speech(results: list[WindowSpeech]) -> list:
    """Эмоциональные реплики → сигналы разбора."""
    from core.clipscan import Kind, Signal
    out = []
    for ws in results:
        if ws.heat <= 0:
            continue
        weight = min(2.4, 0.8 + ws.heat * 0.5)
        center = (ws.start + ws.end) / 2.0
        out.append(Signal(kind=Kind.SPEECH, t=center, weight=round(weight, 3),
                          detail=ws.describe(),
                          meta={"quote": ws.quote, "markers": ws.markers,
                                "profanity": ws.profanity, "repeats": ws.repeats,
                                "start": round(ws.start, 2), "end": round(ws.end, 2)}))
    return out


def apply_names(moments: list, results: list[WindowSpeech]) -> int:
    """Дать безымянным моментам название из речи. Имя от зрителя не трогаем.

    Возвращает, скольким моментам имя досталось.
    """
    named = 0
    for m in moments:
        if getattr(m, "label", ""):
            continue                        # клип зрителя назвали лучше — не спорим
        best = None
        for ws in results:
            if not ws.quote:
                continue
            if ws.start <= m.center <= ws.end or abs((ws.start + ws.end) / 2 - m.center) < 30:
                if best is None or ws.heat > best.heat:
                    best = ws
        if best is not None:
            m.label = best.quote
            named += 1
    return named


def _mmss(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec} с"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} мин" + (f" {s} с" if s and m < 10 else "")
    return f"{m // 60} ч {m % 60} мин"
