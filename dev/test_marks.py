"""dev/test_marks.py — функц.тест ядра меток (Этап 2), без Qt и без рендера.

Проверяем: схлопывание близких меток (15с), окно −30/+10, пороги режимов
(камерный/средний/большой + AUTO по онлайну), слияние перекрытий, фильтр автора.
Запуск: python -m dev.test_marks
"""
from __future__ import annotations

from core.marks import (Mark, MarksFile, AuthorType as A, AudienceMode as M,
                        select_moments, resolve_mode, moments_to_segments, by_heat)


def _ok(cond: bool, msg: str) -> None:
    print(("  OK  " if cond else " FAIL ") + msg)
    assert cond, msg


def test_cluster_and_window():
    print("\n[1] Схлопывание + окно (−30/+10):")
    # Три метки в пределах 15с → один момент; центр ≈ 1002.
    mf = MarksFile(online=200, duration=7200, marks=[
        Mark(1000, A.STREAMER), Mark(1002, A.VIEWER, "a"), Mark(1004, A.MODERATOR),
    ])
    ms = select_moments(mf)
    _ok(len(ms) == 1, f"одна группа → один момент (получено {len(ms)})")
    mo = ms[0]
    _ok(abs(mo.center - 1002) < 1.5, f"центр ≈ 1002 (={mo.center:.1f})")
    _ok(abs(mo.start - (mo.center - 30)) < 0.01 and abs(mo.end - (mo.center + 10)) < 0.01,
        f"окно −30/+10 (start={mo.start:.1f}, end={mo.end:.1f})")


def test_gap_splits():
    print("\n[2] Разрыв > 15с рвёт на два момента:")
    mf = MarksFile(online=200, duration=7200, marks=[
        Mark(500, A.STREAMER), Mark(508, A.VIEWER, "a"),   # группа 1
        Mark(600, A.STREAMER),                              # разрыв 92с → группа 2
    ])
    ms = select_moments(mf)
    _ok(len(ms) == 2, f"две группы → два момента (получено {len(ms)})")


def test_viewer_thresholds():
    print("\n[3] Пороги по режимам (только зрительские метки):")
    # 2 разных зрителя рядом, без доверенных авторов.
    mf = MarksFile(marks=[Mark(1000, A.VIEWER, "a"), Mark(1003, A.VIEWER, "b")])
    _ok(len(select_moments(mf, M.SMALL)) == 1, "камерный: хватает 1+ → момент есть")
    _ok(len(select_moments(mf, M.MEDIUM)) == 0, "средний: нужно 3 → момента нет (только 2)")
    _ok(len(select_moments(mf, M.LARGE)) == 0, "большой: нужно 10 → момента нет")

    # Метка стримера рядом → момент всегда, любой режим.
    mf2 = MarksFile(marks=[Mark(1000, A.VIEWER, "a"), Mark(1002, A.STREAMER)])
    _ok(len(select_moments(mf2, M.LARGE)) == 1, "стример в группе → момент даже в 'большом'")


def test_auto_by_online():
    print("\n[4] AUTO выбирает режим по онлайну:")
    _ok(resolve_mode(M.AUTO, 20) == M.SMALL, "онлайн 20 → камерный")
    _ok(resolve_mode(M.AUTO, 200) == M.MEDIUM, "онлайн 200 → средний")
    _ok(resolve_mode(M.AUTO, 5000) == M.LARGE, "онлайн 5000 → большой")
    _ok(resolve_mode(M.AUTO, None) == M.MEDIUM, "нет онлайна → средний (фолбэк)")

    # 4 разных зрителя: при малом онлайне момент есть, при большом — нет.
    marks = [Mark(1000 + i, A.VIEWER, f"u{i}") for i in range(4)]
    _ok(len(select_moments(MarksFile(online=20, marks=marks))) == 1,
        "AUTO@онлайн20: 4 зрителя → момент есть")
    _ok(len(select_moments(MarksFile(online=5000, marks=marks))) == 0,
        "AUTO@онлайн5000: 4 зрителя < 10 → момента нет")


def test_split_adjacent():
    print("\n[5] Разведение перекрывающихся окон (2 метки → 2 момента):")
    # Короткое видео, метки >15с врозь (разные группы), но окна -30/+10 налезают → ДВА.
    mf = MarksFile(duration=30, online=180, marks=[
        Mark(5.0, A.STREAMER), Mark(25.0, A.STREAMER)])   # 20с врозь > gap 15с
    ms = select_moments(mf)                      # окно по умолчанию -30/+10
    _ok(len(ms) == 2, f"две метки → два момента (получено {len(ms)})")
    _ok(ms[0].end <= ms[1].start + 0.01, "моменты не перекрываются")
    mid = (5.0 + 25.0) / 2
    _ok(abs(ms[0].end - mid) < 0.01 and abs(ms[1].start - mid) < 0.01,
        f"граница по середине между центрами (~{mid})")


def test_close_marks_one_moment():
    print("\n[5b] Метки ближе 15с — остаются ОДНИМ моментом (правило схлопывания):")
    mf = MarksFile(duration=60, marks=[Mark(20.0, A.STREAMER), Mark(32.0, A.STREAMER)])  # 12с
    _ok(len(select_moments(mf)) == 1, "12с врозь ≤ 15с → один момент")


def test_filter_and_heat():
    print("\n[6] Фильтр по автору + сортировка по жару:")
    mf = MarksFile(marks=[
        Mark(1000, A.VIEWER, "a"), Mark(1001, A.VIEWER, "b"), Mark(1002, A.VIEWER, "c"),  # жар 3
        Mark(3000, A.STREAMER, note="топ момент"),                                        # жар 4
    ])
    only_stream = select_moments(mf, M.SMALL, author_filter={A.STREAMER})
    _ok(len(only_stream) == 1 and only_stream[0].center == 3000,
        "фильтр 'только стример' → остаётся один момент стримера")
    hot = by_heat(select_moments(mf, M.SMALL))
    _ok(hot[0].label == "топ момент", "по жару первым идёт момент стримера (жар 4)")


def test_to_segments():
    print("\n[7] Моменты → сегменты для склейки:")
    mf = MarksFile(marks=[Mark(500, A.STREAMER), Mark(2000, A.STREAMER)])
    segs = moments_to_segments(select_moments(mf))
    _ok(len(segs) == 2, f"два момента → два сегмента (получено {len(segs)})")
    total = sum(s.duration for s in segs)
    _ok(abs(total - 80.0) < 0.1, f"суммарная длина ≈ 80с (2×40; ={total:.1f})")
    print(f"      сегменты: {[(round(s.start), round(s.end)) for s in segs]}")


if __name__ == "__main__":
    test_cluster_and_window()
    test_gap_splits()
    test_viewer_thresholds()
    test_auto_by_online()
    test_split_adjacent()
    test_close_marks_one_moment()
    test_filter_and_heat()
    test_to_segments()
    print("\nВСЕ ПРОВЕРКИ ПРОШЛИ ✔")
