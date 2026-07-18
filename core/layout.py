"""Раскладки 9:16 — генерация FFmpeg-фильтров для 4 пресетов.

Модуль НЕ запускает ffmpeg сам: он возвращает под-граф filter_complex
(список строк-фильтров), который render.py вставляет в общую команду.

Зоны задаются в долях исходного кадра (Zone.x/y/w/h ∈ 0..1) — crop выражается
через iw/ih, поэтому раскладка не зависит от разрешения источника.

Все итоговые размеры чётные (требование NVENC).

Пресеты (см. SPEC → Раскладки):
  A — вебка сверху (вписана по ширине, без кропа) + геймплей снизу (кроп). ДЕФОЛТ.
  B — геймплей на весь кадр + вебка кружком в углу.
  C — вебка (лицо) на весь экран (Just Chatting).
  D — геймплей сверху + вебка снизу (зеркально A).
"""
from __future__ import annotations

import json
import os

from .config import LayoutConfig, LayoutPreset, Zone, Corner


def load_preset(preset_id: str, presets_dir: str = "presets") -> LayoutConfig:
    """Загрузить LayoutConfig из presets/layout_<ID>.json.

    Если файла нет — вернуть LayoutConfig с дефолтными зонами для этого пресета.
    """
    path = os.path.join(presets_dir, f"layout_{preset_id}.json")
    if not os.path.isfile(path):
        return LayoutConfig(preset=LayoutPreset(preset_id))
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return LayoutConfig(
        preset=LayoutPreset(d.get("preset", preset_id)),
        webcam_zone=Zone(**d["webcam_zone"]) if "webcam_zone" in d else LayoutConfig().webcam_zone,
        gameplay_zone=Zone(**d["gameplay_zone"]) if "gameplay_zone" in d else LayoutConfig().gameplay_zone,
        webcam_corner=Corner(d.get("webcam_corner", "bottom_right")),
        webcam_circle_ratio=d.get("webcam_circle_ratio", 0.30),
    )


def _even(n: float) -> int:
    """Округлить к ближайшему чётному целому (>= 2)."""
    v = int(round(n / 2.0)) * 2
    return max(2, v)


def _crop_by_zone(zone: Zone) -> str:
    """FFmpeg-выражение crop для зоны в долях исходного кадра."""
    z = zone.clamped()
    # iw/ih — размеры входа фильтра (полный кадр источника после trim).
    return (
        f"crop=w=iw*{z.w:.6f}:h=ih*{z.h:.6f}"
        f":x=iw*{z.x:.6f}:y=ih*{z.y:.6f}"
    )


def _cover(zone: Zone, out_w: int, out_h: int) -> str:
    """Вырезать зону и масштабировать с ПЕРЕКРЫТИЕМ (cover) до out_w×out_h,
    затем центральный кроп до точного размера. Без чёрных полей."""
    return (
        f"{_crop_by_zone(zone)},"
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h}"
    )


def webcam_band_height(layout: LayoutConfig, src_w: int, src_h: int,
                       canvas_w: int, canvas_h: int) -> int:
    """Высота полосы вебки для пресетов A/D: вебка вписана по ширине канвы,
    высота вычисляется из аспекта зоны вебки. Возвращает чётное значение,
    гарантируя, что для геймплея остаётся минимум 2px."""
    wz = layout.webcam_zone.clamped()
    zone_w_px = max(1.0, src_w * wz.w)
    zone_h_px = max(1.0, src_h * wz.h)
    aspect = zone_w_px / zone_h_px  # ширина/высота зоны вебки
    band = canvas_w / aspect        # высота при вписывании по ширине канвы
    band = _even(band)
    band = min(band, canvas_h - 2)  # оставить место геймплею
    return max(2, band)


def build_layout_filtergraph(
    layout: LayoutConfig,
    src_w: int,
    src_h: int,
    canvas_w: int,
    canvas_h: int,
    in_label: str,
    out_label: str,
    tag: str,
) -> list[str]:
    """Собрать под-граф фильтров, превращающий поток [in_label] в [out_label]
    размером canvas_w×canvas_h по выбранному пресету.

    tag — уникальный префикс (напр. номер сегмента), чтобы промежуточные метки
    не пересекались при склейке нескольких сегментов.
    """
    p = layout.preset
    if p == LayoutPreset.A:
        return _preset_stacked(layout, src_w, src_h, canvas_w, canvas_h,
                               in_label, out_label, tag, webcam_on_top=True)
    if p == LayoutPreset.D:
        return _preset_stacked(layout, src_w, src_h, canvas_w, canvas_h,
                               in_label, out_label, tag, webcam_on_top=False)
    if p == LayoutPreset.C:
        return _preset_fullface(layout, canvas_w, canvas_h, in_label, out_label, tag)
    if p == LayoutPreset.B:
        return _preset_pip_circle(layout, canvas_w, canvas_h, in_label, out_label, tag)
    raise ValueError(f"Неизвестный пресет раскладки: {p}")


def _preset_stacked(layout, src_w, src_h, canvas_w, canvas_h,
                    in_label, out_label, tag, webcam_on_top: bool) -> list[str]:
    """Пресеты A и D: вебка (вписана по ширине, без кропа) + геймплей (кроп),
    сложенные вертикально. webcam_on_top=True → A, False → D."""
    band = webcam_band_height(layout, src_w, src_h, canvas_w, canvas_h)
    game_h = _even(canvas_h - band)
    # После двух _even сумма может отличаться на 2 — подгоняем геймплей точно.
    game_h = canvas_h - band

    wc = f"{tag}_wc"
    gp = f"{tag}_gp"
    wcf = f"{tag}_wcf"
    gpf = f"{tag}_gpf"

    filters = [
        f"[{in_label}]split=2[{wc}][{gp}]",
        # Вебка: кроп зоны + вписать по ширине (высота = band, аспект сохранён).
        f"[{wc}]{_crop_by_zone(layout.webcam_zone)},scale={canvas_w}:{band}[{wcf}]",
        # Геймплей: кроп зоны + cover до оставшейся высоты.
        f"[{gp}]{_cover(layout.gameplay_zone, canvas_w, game_h)}[{gpf}]",
    ]
    if webcam_on_top:
        filters.append(f"[{wcf}][{gpf}]vstack=inputs=2[{out_label}]")
    else:
        filters.append(f"[{gpf}][{wcf}]vstack=inputs=2[{out_label}]")
    return filters


def _preset_fullface(layout, canvas_w, canvas_h, in_label, out_label, tag) -> list[str]:
    """Пресет C: вебка (лицо) на весь экран 9:16."""
    return [
        f"[{in_label}]{_cover(layout.webcam_zone, canvas_w, canvas_h)}[{out_label}]"
    ]


def _corner_xy(corner: Corner, canvas_w: int, canvas_h: int, d: int, margin: int) -> tuple[str, str]:
    """Координаты левого верхнего угла оверлея для заданного угла канвы."""
    if corner == Corner.TOP_LEFT:
        return str(margin), str(margin)
    if corner == Corner.TOP_RIGHT:
        return str(canvas_w - d - margin), str(margin)
    if corner == Corner.BOTTOM_LEFT:
        return str(margin), str(canvas_h - d - margin)
    # BOTTOM_RIGHT
    return str(canvas_w - d - margin), str(canvas_h - d - margin)


def _preset_pip_circle(layout, canvas_w, canvas_h, in_label, out_label, tag) -> list[str]:
    """Пресет B: геймплей на весь кадр + вебка круглым PiP в углу."""
    d = _even(canvas_w * layout.webcam_circle_ratio)  # диаметр кружка
    margin = _even(canvas_w * 0.03)
    r = d // 2

    bg = f"{tag}_bg"
    pip = f"{tag}_pip"
    bgf = f"{tag}_bgf"
    pipf = f"{tag}_pipf"

    x, y = _corner_xy(layout.webcam_corner, canvas_w, canvas_h, d, margin)

    # Круглая альфа-маска через geq: пиксели вне круга радиуса r → alpha 0.
    circle_alpha = (
        f"format=yuva420p,"
        f"geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)'"
        f":a='if(gt((X-{r})*(X-{r})+(Y-{r})*(Y-{r}),{r}*{r}),0,255)'"
    )

    return [
        f"[{in_label}]split=2[{bg}][{pip}]",
        f"[{bg}]{_cover(layout.gameplay_zone, canvas_w, canvas_h)}[{bgf}]",
        f"[{pip}]{_cover(layout.webcam_zone, d, d)},{circle_alpha}[{pipf}]",
        f"[{bgf}][{pipf}]overlay=x={x}:y={y}[{out_label}]",
    ]
