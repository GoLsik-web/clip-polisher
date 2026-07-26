"""dev/test_timemap.py — тест слоя «неполная запись» + продвинутого звука (без Qt/рендера).

Запуск: python -m dev.test_timemap
"""
from __future__ import annotations

from core.timemap import TimeMap, Piece
from core.config import Segment
from core.audio import build_audio_filter


def _ok(cond: bool, msg: str) -> None:
    print(("  OK  " if cond else " FAIL ") + msg)
    assert cond, msg


def test_identity():
    print("\n[1] Полная запись (identity):")
    tm = TimeMap.identity(3600)
    _ok(tm.stream_to_file(100) == 100, "время стрима == позиция в файле")
    segs = tm.map_range(50, 90)
    _ok(len(segs) == 1 and segs[0].start == 50 and segs[0].end == 90, "отрезок 1:1")


def test_start_offset():
    print("\n[2] Сдвиг начала (запись стартовала на 300с позже):")
    tm = TimeMap.with_start_offset(file_duration=3000, stream_offset=300)
    _ok(tm.stream_to_file(300) == 0.0, "начало эфира-в-записи → позиция 0 в файле")
    _ok(tm.stream_to_file(1300) == 1000.0, "стрим 1300с → 1000с в файле")
    _ok(tm.stream_to_file(100) is None, "момент ДО старта записи → None (нет в файле)")
    segs = tm.map_range(1290, 1310)
    _ok(len(segs) == 1 and abs(segs[0].start - 990) < 0.01 and abs(segs[0].end - 1010) < 0.01,
        f"отрезок сдвинут на -300 ({[(round(s.start), round(s.end)) for s in segs]})")


def test_middle_gap():
    print("\n[3] Дыра в середине (запись из двух кусков):")
    # Кусок A: стрим 0..600 → файл 0..600. Кусок B: стрим 1200..1800 → файл 600..1200.
    # В стриме 600..1200 — дыра (записи нет).
    tm = TimeMap([Piece(0, 600, 0), Piece(1200, 1800, 600)])
    _ok(tm.stream_to_file(300) == 300, "кусок A: 300 → 300")
    _ok(tm.stream_to_file(900) is None, "в дыре (900) → None")
    _ok(tm.stream_to_file(1300) == 700, "кусок B: 1300 → 700 (файл сжат на дыру)")

    # Отрезок стрима 550..1250 накрывает конец A, всю дыру и начало B → ДВА файловых куска.
    segs = tm.map_range(550, 1250)
    _ok(len(segs) == 2, f"отрезок через дыру → 2 куска (получено {len(segs)})")
    _ok(abs(segs[0].start - 550) < 0.01 and abs(segs[0].end - 600) < 0.01, "1-й кусок: 550..600")
    _ok(abs(segs[1].start - 600) < 0.01 and abs(segs[1].end - 650) < 0.01,
        "2-й кусок: 600..650 (файл-время)")


def test_moment_in_hole():
    print("\n[4] Момент целиком в дыре → пусто:")
    tm = TimeMap([Piece(0, 600, 0), Piece(1200, 1800, 600)])
    _ok(tm.map_range(800, 1000) == [], "отрезок 800..1000 в дыре → нет файловых кусков")


def test_map_segments_order():
    print("\n[5] Список сегментов → плоский файловый список по порядку:")
    tm = TimeMap.identity(3600)
    segs = tm.map_segments([Segment(1000, 1040), Segment(200, 240)])
    _ok([round(s.start) for s in segs] == [200, 1000], "отсортировано по позиции в файле")


def test_audio_enhance():
    print("\n[6] Продвинутый звук в аудио-графе:")
    # Без интервалов мата, все улучшения включены.
    f = ";".join(build_audio_filter([], denoise=True, clarity=True, gate=True))
    _ok("afftdn" in f, "шумодав (afftdn) присутствует")
    _ok("agate" in f, "гейт (agate) присутствует")
    _ok("equalizer" in f and "highpass" in f, "чёткость (highpass+equalizer) присутствует")
    _ok("loudnorm" in f and "aresample=48000" in f.split("loudnorm", 1)[1],
        "loudnorm с обязательным aresample=48000 ПОСЛЕ (грабля 96кГц)")

    # Выключено — фильтров улучшений быть не должно.
    f0 = ";".join(build_audio_filter([]))
    _ok("afftdn" not in f0 and "agate" not in f0, "выключено → без улучшений")

    # Совместимость с бипом: интервалы + улучшения вместе.
    fb = ";".join(build_audio_filter([(1.0, 2.0)], denoise=True))
    _ok("a_beep" in fb and "afftdn" in fb, "бип и шумодав уживаются в одном графе")


if __name__ == "__main__":
    test_identity()
    test_start_offset()
    test_middle_gap()
    test_moment_in_hole()
    test_map_segments_order()
    test_audio_enhance()
    print("\nВСЕ ПРОВЕРКИ ПРОШЛИ ✔")
