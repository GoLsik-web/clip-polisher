"""compositor.py — свободная композиция видео-слоёв на канве 9:16.

Собирает под-граф FFmpeg: чёрная база → геймплей → вебка (вебка последней = поверх).
Каждый слой: вырезать источник (crop по долям 16:9) → cover-масштаб в выходной
прямоугольник (доли канвы) → круглая маска (если shape=circle) → поворот → overlay.

Субтитры/ник/платформа накладываются позже (в render), т.к. это .ass и drawtext.
Все размеры чётные (NVENC). Никакого Qt.
"""
from __future__ import annotations

import math

from .config import Composition, Placement, Zone


def _even(n: float) -> int:
    return max(2, int(round(n / 2.0)) * 2)


def _crop_zone(z: Zone) -> str:
    z = z.clamped()
    return (f"crop=w=iw*{z.w:.6f}:h=ih*{z.h:.6f}"
            f":x=iw*{z.x:.6f}:y=ih*{z.y:.6f}")


def _place_px(pl: Placement, cw: int, ch: int) -> tuple[int, int, int, int]:
    x = _even(pl.x * cw)
    y = _even(pl.y * ch)
    w = _even(pl.w * cw)
    h = _even(pl.h * ch)
    w = min(w, cw); h = min(h, ch)
    return x, y, w, h


def _layer(src_label: str, src_zone: Zone, pl: Placement, cw: int, ch: int,
           out_label: str) -> tuple[list[str], int, int]:
    """Собрать один слой (crop→cover→[circle]→[rotate]) из src_label в out_label.

    Возвращает (фильтры, overlay_x, overlay_y) — координаты левого-верхнего угла overlay.
    """
    x, y, w, h = _place_px(pl, cw, ch)
    filters = [
        f"[{src_label}]{_crop_zone(src_zone)},"
        f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}[{out_label}_s]"
    ]
    cur = f"{out_label}_s"
    ox, oy = x, y

    # Круглая маска (для кружка-вебки): берём квадрат min(w,h).
    if pl.shape == "circle":
        d = min(w, h)
        r = d // 2
        filters.append(
            f"[{cur}]crop={d}:{d},format=yuva420p,"
            f"geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)'"
            f":a='if(gt((X-{r})*(X-{r})+(Y-{r})*(Y-{r}),{r}*{r}),0,255)'[{out_label}_c]"
        )
        cur = f"{out_label}_c"
        # центрируем кружок в исходном прямоугольнике
        ox = x + (w - d) // 2
        oy = y + (h - d) // 2
        w = h = d

    # Поворот (если задан): расширяем bbox, центрируем на центре элемента.
    if abs(pl.rotation) > 0.01:
        ang = pl.rotation * math.pi / 180.0
        bb = _even(math.hypot(w, h))
        filters.append(
            f"[{cur}]format=rgba,rotate={ang:.5f}:c=none:ow={bb}:oh={bb}[{out_label}_r]"
        )
        cur = f"{out_label}_r"
        cx, cy = ox + w / 2, oy + h / 2
        ox = int(cx - bb / 2)
        oy = int(cy - bb / 2)

    filters.append(f"[{cur}]null[{out_label}]")
    return filters, ox, oy


def build_composition_segment(comp: Composition, cw: int, ch: int,
                              in_label: str, out_label: str, tag: str) -> list[str]:
    """Под-граф одного сегмента: in_label (кадр 16:9) → out_label (канва 9:16)."""
    layers = []  # (label, x, y) в порядке наложения (снизу вверх)
    parts: list[str] = []

    want_gp = comp.gameplay.visible
    want_wc = comp.webcam.visible
    n_split = 1 + int(want_gp) + int(want_wc)   # база + геймплей + вебка

    split_labels = [f"{tag}_base_in"]
    if want_gp:
        split_labels.append(f"{tag}_gp_in")
    if want_wc:
        split_labels.append(f"{tag}_wc_in")
    parts.append(f"[{in_label}]split={n_split}" + "".join(f"[{l}]" for l in split_labels))

    # База — чёрная канва нужной длины (из копии входа, чтобы длительность совпала).
    parts.append(
        f"[{tag}_base_in]scale={cw}:{ch}:force_original_aspect_ratio=increase,"
        f"crop={cw}:{ch},drawbox=x=0:y=0:w={cw}:h={ch}:color=black:t=fill[{tag}_base]"
    )
    cur = f"{tag}_base"

    if want_gp:
        f, ox, oy = _layer(f"{tag}_gp_in", comp.gameplay_source, comp.gameplay, cw, ch, f"{tag}_gp")
        parts.extend(f)
        parts.append(f"[{cur}][{tag}_gp]overlay=x={ox}:y={oy}[{tag}_o1]")
        cur = f"{tag}_o1"

    if want_wc:
        f, ox, oy = _layer(f"{tag}_wc_in", comp.webcam_source, comp.webcam, cw, ch, f"{tag}_wc")
        parts.extend(f)
        parts.append(f"[{cur}][{tag}_wc]overlay=x={ox}:y={oy}[{tag}_o2]")
        cur = f"{tag}_o2"

    parts.append(f"[{cur}]null[{out_label}]")
    return parts
