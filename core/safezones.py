"""safezones.py — «занятые» интерфейсом площадки зоны на выходном кадре 9:16.

Когда клип публикуют в TikTok / Shorts / Reels, интерфейс приложения перекрывает
часть кадра: справа — столбец иконок (лайк/коммент/шер), снизу — ник, подпись,
музыка, кнопки; сверху — строка поиска/статуса. Если субтитры или ник попадут
туда, их закроет интерфейс.

Здесь — приблизительные прямоугольники этих зон в ДОЛЯХ выходного кадра (0..1),
чтобы редактор нарисовал их подсказкой и пользователь туда ничего не ставил.
Это ТОЛЬКО ориентир в редакторе — в готовое видео ничего не вжигается.

Значения приблизительные (интерфейсы площадок меняются), но по ним удобно держать
важное в центральной «чистой» области.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SafeRect:
    """Прямоугольник занятой зоны в долях кадра 9:16 + подпись, что там за элемент."""
    x: float
    y: float
    w: float
    h: float
    label: str


@dataclass(frozen=True)
class SafeZonePreset:
    key: str
    title: str
    rects: tuple[SafeRect, ...]


# Пресеты по площадкам. Правый столбец иконок + нижняя панель + верхняя строка.
PRESETS: dict[str, SafeZonePreset] = {
    "tiktok": SafeZonePreset(
        key="tiktok", title="TikTok",
        rects=(
            SafeRect(0.86, 0.40, 0.14, 0.44, "Иконки (лайк/коммент/шер)"),
            SafeRect(0.00, 0.78, 0.86, 0.22, "Ник, подпись, музыка"),
            SafeRect(0.00, 0.00, 1.00, 0.08, "Поиск / статус"),
        ),
    ),
    "shorts": SafeZonePreset(
        key="shorts", title="YouTube Shorts",
        rects=(
            SafeRect(0.86, 0.45, 0.14, 0.40, "Иконки (лайк/дизлайк/коммент)"),
            SafeRect(0.00, 0.82, 0.86, 0.18, "Название, канал, подписка"),
            SafeRect(0.00, 0.00, 1.00, 0.07, "Верхняя строка"),
        ),
    ),
    "reels": SafeZonePreset(
        key="reels", title="Instagram Reels",
        rects=(
            SafeRect(0.87, 0.38, 0.13, 0.44, "Иконки (лайк/коммент/шер)"),
            SafeRect(0.00, 0.80, 0.87, 0.20, "Подпись, аудио, ник"),
            SafeRect(0.00, 0.00, 1.00, 0.07, "Верхняя строка"),
        ),
    ),
}

ORDER: tuple[str, ...] = ("tiktok", "shorts", "reels")


def preset(key: str) -> SafeZonePreset | None:
    return PRESETS.get(key)


def all_presets() -> list[SafeZonePreset]:
    return [PRESETS[k] for k in ORDER]
