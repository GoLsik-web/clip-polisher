"""Модель данных настроек проекта (dataclass'ы) + сериализация в JSON.

Ключевая forward-compat идея: клип собирается из СПИСКА сегментов (segments),
а не из одного отрезка. На Этапе 1 список всегда из одного элемента, но модель
данных и рендер уже готовы к склейке нескольких (до 4) — фундамент под Этап 2.

Никаких зависимостей от Qt здесь быть не должно.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------
# Базовые примитивы
# --------------------------------------------------------------------------

@dataclass
class Segment:
    """Отрезок исходного видео в секундах (ручные границы обрезки из UI)."""
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Zone:
    """Прямоугольная зона в ДОЛЯХ (0..1) исходного кадра.

    Хранение в долях делает разметку независимой от разрешения источника:
    один и тот же пресет работает и для 1920x1080, и для 1280x720.
    x, y — левый верхний угол; w, h — ширина/высота. Все в диапазоне 0..1.
    """
    x: float
    y: float
    w: float
    h: float

    def clamped(self) -> "Zone":
        """Обрезать зону в пределы [0..1], чтобы crop не вышел за кадр."""
        x = min(max(self.x, 0.0), 1.0)
        y = min(max(self.y, 0.0), 1.0)
        w = min(max(self.w, 0.0), 1.0 - x)
        h = min(max(self.h, 0.0), 1.0 - y)
        return Zone(x, y, w, h)


class LayoutPreset(str, Enum):
    """4 пресета раскладки 9:16 (см. SPEC → Раскладки)."""
    A = "A"  # Вебка сверху + геймплей снизу (дефолт)
    B = "B"  # Геймплей на весь + вебка-кружок в углу
    C = "C"  # Лицо на весь экран (Just Chatting)
    D = "D"  # Геймплей сверху + вебка снизу (зеркально A)


class Corner(str, Enum):
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"


class VideoCodec(str, Enum):
    NVENC = "h264_nvenc"   # GPU (по умолчанию)
    X264 = "libx264"       # CPU-фолбэк


# --------------------------------------------------------------------------
# Настройки раскладки
# --------------------------------------------------------------------------

@dataclass
class LayoutConfig:
    """Какой пресет и какие исходные зоны использовать.

    webcam_zone / gameplay_zone — зоны в долях исходного кадра. Какие из них
    реально нужны, зависит от пресета:
      A, D — нужны обе (вебка и геймплей);
      B    — обе (геймплей на фон, вебка в кружок);
      C    — только webcam_zone (лицо на весь экран).
    """
    preset: LayoutPreset = LayoutPreset.A
    webcam_zone: Zone = field(default_factory=lambda: Zone(0.0, 0.0, 0.28, 0.28))
    gameplay_zone: Zone = field(default_factory=lambda: Zone(0.0, 0.0, 1.0, 1.0))
    # Для пресета B: угол и диаметр кружка вебки (доля ширины канвы 0..1).
    webcam_corner: Corner = Corner.BOTTOM_RIGHT
    webcam_circle_ratio: float = 0.30


# --------------------------------------------------------------------------
# Экспорт
# --------------------------------------------------------------------------

@dataclass
class Placement:
    """Положение элемента на ВЫХОДНОЙ канве 9:16 — в долях канвы (0..1) + поворот.

    shape: 'rect' | 'circle' (кружок для вебки). rotation — градусы (по часовой).
    Содержимое масштабируется с перекрытием (cover) в этот прямоугольник.
    """
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0
    rotation: float = 0.0
    shape: str = "rect"
    visible: bool = True


@dataclass
class Composition:
    """Свободная компоновка: ЧТО вырезать из 16:9 (source) + КУДА поставить на 9:16.

    Пресеты A/B/C/D задают дефолтную композицию (быстрый старт), дальше пользователь
    двигает/масштабирует/крутит элементы на 9:16-холсте.
    """
    webcam_source: "Zone"
    gameplay_source: "Zone"
    webcam: Placement
    gameplay: Placement
    subtitles: Placement
    nick: Placement
    platform: Placement

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Composition":
        return cls(
            webcam_source=Zone(**d["webcam_source"]),
            gameplay_source=Zone(**d["gameplay_source"]),
            webcam=Placement(**d["webcam"]),
            gameplay=Placement(**d["gameplay"]),
            subtitles=Placement(**d["subtitles"]),
            nick=Placement(**d["nick"]),
            platform=Placement(**d["platform"]),
        )


def composition_from_preset(preset: "LayoutPreset",
                            webcam_source: "Zone",
                            gameplay_source: "Zone",
                            canvas_w: int = 1080, canvas_h: int = 1920) -> Composition:
    """Собрать дефолтную Composition для пресета (выходные позиции на 9:16)."""
    # Общие дефолты для субтитров/ника/платформы.
    subs = Placement(0.08, 0.70, 0.84, 0.12)
    nick = Placement(0.04, 0.02, 0.42, 0.06)
    plat = Placement(0.84, 0.02, 0.12, 0.05)

    if preset == LayoutPreset.A:            # вебка сверху + геймплей снизу
        wc = Placement(0.0, 0.0, 1.0, 0.34)
        gp = Placement(0.0, 0.34, 1.0, 0.66)
    elif preset == LayoutPreset.D:          # геймплей сверху + вебка снизу
        gp = Placement(0.0, 0.0, 1.0, 0.6)
        wc = Placement(0.0, 0.6, 1.0, 0.4)
    elif preset == LayoutPreset.C:          # лицо на весь экран
        wc = Placement(0.0, 0.0, 1.0, 1.0)
        gp = Placement(0.0, 0.0, 0.3, 0.2, visible=False)
    else:                                   # B: геймплей на весь + вебка-кружок
        gp = Placement(0.0, 0.0, 1.0, 1.0)
        d_ratio_w = 0.30
        d_ratio_h = d_ratio_w * canvas_w / canvas_h   # квадрат в пикселях → кружок
        wc = Placement(0.66, 0.74, d_ratio_w, d_ratio_h, shape="circle")

    return Composition(webcam_source=webcam_source, gameplay_source=gameplay_source,
                       webcam=wc, gameplay=gp, subtitles=subs, nick=nick, platform=plat)


@dataclass
class ExportConfig:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    codec: VideoCodec = VideoCodec.NVENC
    video_bitrate: str = "8M"
    audio_bitrate: str = "192k"
    out_dir: str = "out"
    filename: str = "clip_vertical.mp4"

    def output_path(self) -> str:
        import os
        return os.path.join(self.out_dir, self.filename)


def unique_path(path: str) -> str:
    """Вернуть путь, который ещё не занят: если файл есть — «имя (2).ext»,
    «имя (3).ext» и т.д. Так рендер НЕ перезаписывает существующий клип,
    а кладёт рядом новый (как просил пользователь)."""
    import os
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while True:
        cand = f"{base} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


# --------------------------------------------------------------------------
# Полная конфигурация проекта
# --------------------------------------------------------------------------

@dataclass
class ProjectConfig:
    """Всё, что нужно для сборки одного вертикального клипа.

    segments — СПИСОК сегментов. Этап 1: всегда один. Этап 2: до 4 со склейкой.
    """
    input_path: str = ""
    segments: list[Segment] = field(default_factory=list)
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    composition: Optional[Composition] = None   # если задана — рендер по свободной композиции
    export: ExportConfig = field(default_factory=ExportConfig)
    # Дальнейшие блоки (субтитры/брендинг/мат/аудио) добавим по мере готовности модулей.

    # ---- Сериализация -----------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        # Enum'ы asdict превращает в их .value автоматически (str-Enum), ок.
        return d

    def to_json(self, path: str) -> None:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "ProjectConfig":
        segments = [Segment(**s) for s in d.get("segments", [])]

        lay = d.get("layout", {})
        layout = LayoutConfig(
            preset=LayoutPreset(lay.get("preset", "A")),
            webcam_zone=Zone(**lay["webcam_zone"]) if "webcam_zone" in lay else LayoutConfig().webcam_zone,
            gameplay_zone=Zone(**lay["gameplay_zone"]) if "gameplay_zone" in lay else LayoutConfig().gameplay_zone,
            webcam_corner=Corner(lay.get("webcam_corner", "bottom_right")),
            webcam_circle_ratio=lay.get("webcam_circle_ratio", 0.30),
        )

        exp = d.get("export", {})
        export = ExportConfig(
            width=exp.get("width", 1080),
            height=exp.get("height", 1920),
            fps=exp.get("fps", 30),
            codec=VideoCodec(exp.get("codec", "h264_nvenc")),
            video_bitrate=exp.get("video_bitrate", "8M"),
            audio_bitrate=exp.get("audio_bitrate", "192k"),
            out_dir=exp.get("out_dir", "out"),
            filename=exp.get("filename", "clip_vertical.mp4"),
        )

        return cls(
            input_path=d.get("input_path", ""),
            segments=segments,
            layout=layout,
            export=export,
        )

    @classmethod
    def from_json(cls, path: str) -> "ProjectConfig":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
