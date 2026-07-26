"""timemap.py — Этап 2: перевод «время стрима → позиция в файле записи».

Метки бота живут во времени СТРИМА (секунды от начала эфира). Локальный файл записи
может быть НЕПОЛНЫМ: начаться позже эфира (сдвиг начала), оборваться раньше (сдвиг
конца) или быть склеен с ДЫРАМИ в середине (запись прерывалась). Чтобы отрезок-момент
лёг на правильное место в файле, нужен маппинг.

Модель — список «кусков» (Piece): участок времени стрима [stream_start, stream_end)
присутствует в файле начиная с позиции file_start. Между кусками — дыры (стрим есть,
записи нет). Один кусок = обычная целая запись (возможно, со сдвигом начала).

    поток:   |----gap----[ A ]-------gap-------[ B ]----|
    файл:               [ A ][ B ]

Чистый core, без Qt.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Segment


@dataclass
class Piece:
    """Участок стрима [stream_start, stream_end) → лежит в файле с file_start."""
    stream_start: float
    stream_end: float
    file_start: float

    @property
    def duration(self) -> float:
        return max(0.0, self.stream_end - self.stream_start)


class TimeMap:
    """Маппинг время-стрима ↔ позиция-в-файле по списку кусков."""

    def __init__(self, pieces: list[Piece]):
        # Держим куски отсортированными по времени стрима.
        self.pieces = sorted(pieces, key=lambda p: p.stream_start)

    # ---- фабрики -------------------------------------------------------------

    @classmethod
    def identity(cls, file_duration: float) -> "TimeMap":
        """Полная запись: время стрима == позиция в файле (0..file_duration)."""
        return cls([Piece(0.0, file_duration, 0.0)])

    @classmethod
    def with_start_offset(cls, file_duration: float, stream_offset: float) -> "TimeMap":
        """Запись стартовала на stream_offset секунд ПОЗЖЕ начала эфира.

        Тогда стрим [offset, offset+len) лежит в файле [0, len). Частый случай.
        """
        o = max(0.0, stream_offset)
        return cls([Piece(o, o + file_duration, 0.0)])

    # ---- перевод -------------------------------------------------------------

    def stream_to_file(self, t: float) -> float | None:
        """Позиция в файле для момента стрима t, или None если t в дыре/вне записи."""
        for p in self.pieces:
            if p.stream_start <= t < p.stream_end:
                return p.file_start + (t - p.stream_start)
        return None

    def map_range(self, start: float, end: float) -> list[Segment]:
        """Отрезок стрима [start, end) → отрезки в ФАЙЛЕ.

        Если отрезок пересекает дыры — вернётся НЕСКОЛЬКО кусков (только то, что реально
        есть в записи). Если попал целиком в дыру — пустой список.
        """
        out: list[Segment] = []
        for p in self.pieces:
            a = max(start, p.stream_start)
            b = min(end, p.stream_end)
            if b <= a:
                continue
            fa = p.file_start + (a - p.stream_start)
            fb = p.file_start + (b - p.stream_start)
            out.append(Segment(start=fa, end=fb))
        return out

    def map_segments(self, segments: list[Segment]) -> list[Segment]:
        """Список отрезков стрима → плоский список файловых отрезков (в порядке файла)."""
        out: list[Segment] = []
        for seg in segments:
            out.extend(self.map_range(seg.start, seg.end))
        out.sort(key=lambda s: s.start)
        return out

    # ---- служебное -----------------------------------------------------------

    def covers(self, t: float) -> bool:
        """Есть ли момент стрима t в записи (не в дыре)."""
        return self.stream_to_file(t) is not None

    @property
    def stream_span(self) -> tuple[float, float]:
        """Диапазон времени стрима, покрытый записью (для UI-таймлайна)."""
        if not self.pieces:
            return (0.0, 0.0)
        return (self.pieces[0].stream_start, self.pieces[-1].stream_end)
