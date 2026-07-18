"""profanity.py — русский мат-фильтр: детекция по корням (учёт словоформ),
маскировка текста (х**) + интервалы для звукового бипа.

Подход: словоформы русского мата ловим по КОРНЯМ (префиксам), а не по точному
списку слов — так один корень покрывает все склонения/спряжения. Плюс:
  - EXACT — короткие слова, которые опасно матчить префиксом;
  - WHITELIST — «чистые» слова, случайно попадающие под корень (мудрый, требовать…);
  - нормализация: нижний регистр, ё→е, только кириллица.

Список корней намеренно расширяемый (см. SPEC → Мат-фильтр).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

log = logging.getLogger("clip_polisher.profanity")


# Корни (префиксы). Слово считается матом, если его нормализованная форма
# начинается с одного из корней. Корни подобраны так, чтобы startswith был
# достаточно специфичен и не ловил обычные слова.
ROOTS: tuple[str, ...] = (
    # х*й и производные
    "хуй", "хуё", "хуе", "хуя", "хую", "хуи",
    # п*зд
    "пизд", "пезд",
    # е*/ё* (глаголы и производные) — специфичные формы, без голого "еб"
    "ебал", "ебан", "ебат", "ебач", "ебуч", "ебну", "ебло", "ебыр", "ебёт", "ебет",
    "ёбан", "ёбну", "ёбну", "ебанут", "ебош", "ебаш",
    "заеб", "наеб", "поеб", "въеб", "уеб", "съеб", "доеб", "отъеб", "разъеб",
    "переъеб", "выеб", "приеб", "объеб", "проеб",
    # бл*
    "блят", "бляд", "блях", "бляж",
    # муд* (без голого "муд" — мешает "мудрый")
    "мудак", "мудач", "мудил", "мудоз", "мудох",
    # оскорбления
    "пидор", "пидар", "пидр", "педик", "гандон", "гондон",
    "долбоеб", "долбоёб", "далбаеб",
    "дроч", "манда", "залуп", "гнид",
    "сученьк", "сучар", "сучек", "сучьё",
    "херн", "херов", "херас", "херач", "хренов",
    "срак", "сцык", "ссан", "обосра", "усра", "насра",
    "говн", "гавн",
)

# Точные короткие слова (и их очевидные формы), которые опасно ловить префиксом.
EXACT: frozenset[str] = frozenset({
    "бля", "блэт", "нах", "нахуй", "похуй", "нехуй", "хуй", "хер",
    "сука", "суки", "суке", "суку", "суко", "сук",
    "жопа", "жоп", "жопу", "жопе", "очко",
})

# «Чистые» слова, случайно попадающие под корни/EXACT — исключаем.
WHITELIST: frozenset[str] = frozenset({
    "мудрый", "мудрость", "мудрец", "требовать", "требование", "хлеб",
    "херувим", "хертц", "херсон", "судок", "сукно", "сукин",
    "губернатор",
})

_CYR_ONLY = re.compile(r"[^а-яё]", re.IGNORECASE)


def normalize(word: str) -> str:
    """Нижний регистр, ё→е, оставить только кириллицу."""
    w = word.lower().replace("ё", "е")
    return _CYR_ONLY.sub("", w)


def is_profane(word: str) -> bool:
    """Является ли слово матом/оскорблением."""
    norm = normalize(word)
    if not norm or norm in WHITELIST:
        return False
    if norm in EXACT:
        return True
    # EXACT с ё→е уже нормализованы? EXACT хранит формы без ё, сверяем как есть.
    return any(norm.startswith(root.replace("ё", "е")) for root in ROOTS)


def mask_word(word: str) -> str:
    """Замаскировать: первая буква + звёздочки на остальные (хуй → х**).

    Сохраняем ведущие/замыкающие не-буквенные символы (кавычки, тире) как есть.
    """
    # Найти границы буквенной части.
    letters = [i for i, ch in enumerate(word) if ch.isalpha()]
    if not letters:
        return word
    first, last = letters[0], letters[-1]
    core = word[first:last + 1]
    if len(core) <= 1:
        masked = core
    else:
        masked = core[0] + "*" * (len(core) - 1)
    return word[:first] + masked + word[last + 1:]


# --------------------------------------------------------------------------
# Анализ списка слов (из transcribe.Word)
# --------------------------------------------------------------------------

@dataclass
class ProfanityHit:
    index: int          # индекс слова в исходном списке
    start: float        # интервал для бипа
    end: float
    original: str
    masked: str


@dataclass
class ProfanityResult:
    hits: list[ProfanityHit]
    beep_intervals: list[tuple[float, float]]   # для audio.py

    @property
    def count(self) -> int:
        return len(self.hits)


def analyze_words(words: Iterable, enabled: bool = True,
                  beep_pad: float = 0.03) -> ProfanityResult:
    """Найти мат в списке слов (объекты с .text/.start/.end — transcribe.Word).

    enabled=False → пустой результат (фильтр выключен).
    beep_pad — небольшой запас (с) вокруг слова, чтобы бип точно накрыл его.
    Мутирует .text у найденных слов на замаскированный вариант (для субтитров).
    """
    hits: list[ProfanityHit] = []
    intervals: list[tuple[float, float]] = []
    if not enabled:
        return ProfanityResult(hits=[], beep_intervals=[])

    for i, w in enumerate(words):
        if is_profane(w.text):
            masked = mask_word(w.text)
            hits.append(ProfanityHit(index=i, start=float(w.start), end=float(w.end),
                                     original=w.text, masked=masked))
            intervals.append((max(0.0, float(w.start) - beep_pad), float(w.end) + beep_pad))
            w.text = masked  # для субтитров показываем уже замаскированное

    log.info("Мат-фильтр: найдено %d слов%s", len(hits),
             (": " + ", ".join(h.original for h in hits)) if hits else "")
    return ProfanityResult(hits=hits, beep_intervals=intervals)


# --------------------------------------------------------------------------
# CLI: прогнать по JSON-транскрипту
# --------------------------------------------------------------------------

def _main() -> None:
    import argparse
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Мат-фильтр по JSON-транскрипту")
    ap.add_argument("transcript_json", help="JSON от core.transcribe (--json)")
    args = ap.parse_args()

    with open(args.transcript_json, encoding="utf-8") as f:
        data = json.load(f)

    # Простой объект-обёртка со .text/.start/.end.
    class _W:
        def __init__(self, d):
            self.text = d["text"]; self.start = d["start"]; self.end = d["end"]

    words = [_W(d) for d in data["words"]]
    res = analyze_words(words, enabled=True)

    print(f"\nНайдено матов: {res.count}")
    for h in res.hits:
        print(f"  [{h.start:6.2f}-{h.end:6.2f}]  {h.original!r} → {h.masked!r}")
    print("\nТекст после маскировки:")
    print(" ".join(w.text for w in words))


if __name__ == "__main__":
    _main()
