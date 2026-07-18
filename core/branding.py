"""branding.py — оверлей ника стримера + иконка платформы (Twitch/YouTube/Kick).

Возвращает фрагмент видео-графа FFmpeg. Иконка подгружается через источник
`movie=` (не занимает индекс -i, удобно для общего filter_complex). Ник рисуется
через drawtext шрифтом PT Sans (кириллица). Позиция — по углам, дефолт уголок.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .config import Corner

log = logging.getLogger("clip_polisher.branding")

RGB = tuple[int, int, int]

# Путь к шрифту для ника (кириллица). Абсолютный — работает и в собранном .exe.
from .resources import res as _res
DEFAULT_FONT = _res("assets/fonts/PTSans-Bold.ttf")
ICONS_DIR = _res("assets/icons")


class Platform(str, Enum):
    NONE = "none"
    TWITCH = "twitch"
    YOUTUBE = "youtube"
    KICK = "kick"


@dataclass
class BrandingConfig:
    nickname: str = ""
    platform: Platform = Platform.NONE
    corner: Corner = Corner.TOP_LEFT
    icon_height: int = 72
    text_size: int = 56
    text_color: RGB = (255, 255, 255)
    outline: bool = True
    margin: int = 44
    gap: int = 18                 # отступ между иконкой и ником
    font_path: str = DEFAULT_FONT

    def icon_path(self) -> Optional[str]:
        if self.platform == Platform.NONE:
            return None
        p = os.path.join(ICONS_DIR, f"{self.platform.value}.png")
        return p if os.path.isfile(p) else None


# --------------------------------------------------------------------------
# Экранирование путей/текста для фильтров FFmpeg
# --------------------------------------------------------------------------

def _esc_path(path: str) -> str:
    """Путь для movie=/fontfile=: прямые слэши, экранированное двоеточие диска."""
    return path.replace("\\", "/").replace(":", "\\:")


def _esc_text(text: str) -> str:
    """Текст для drawtext: экранировать спецсимволы."""
    return (text.replace("\\", "\\\\")
                .replace(":", "\\:")
                .replace("'", "\\'")
                .replace("%", "\\%"))


def _hex(rgb: RGB) -> str:
    r, g, b = rgb
    return f"0x{r:02X}{g:02X}{b:02X}"


# --------------------------------------------------------------------------
# Построение фрагмента графа
# --------------------------------------------------------------------------

def build_branding_filter(cfg: BrandingConfig, canvas_w: int, canvas_h: int,
                          in_label: str, out_label: str) -> list[str]:
    """Собрать фрагмент: [in_label] → (иконка + ник) → [out_label].

    Если нет ни ника, ни иконки — просто проброс (copy label).
    """
    icon = cfg.icon_path()
    has_text = bool(cfg.nickname.strip())
    m = cfg.margin
    ih = cfg.icon_height
    gap = cfg.gap
    right = cfg.corner in (Corner.TOP_RIGHT, Corner.BOTTOM_RIGHT)
    bottom = cfg.corner in (Corner.BOTTOM_LEFT, Corner.BOTTOM_RIGHT)

    # Вертикаль иконки (константа) — к ней центрируем текст.
    icon_y = (canvas_h - ih - m) if bottom else m

    filters: list[str] = []
    cur = in_label

    if not icon and not has_text:
        filters.append(f"[{cur}]null[{out_label}]")
        return filters
    # (продолжение ниже — угловая раскладка)

    # 1) Иконка через movie= + overlay.
    if icon:
        tmp = f"{out_label}_ic"
        filters.append(f"movie='{_esc_path(icon)}',scale=-1:{ih}[{tmp}]")
        ov_x = f"W-w-{m}" if right else f"{m}"
        ov_y = f"H-h-{m}" if bottom else f"{m}"
        nxt = f"{out_label}_b1"
        filters.append(f"[{cur}][{tmp}]overlay=x={ov_x}:y={ov_y}[{nxt}]")
        cur = nxt

    # 2) Ник через drawtext рядом с иконкой.
    if has_text:
        icon_space = (ih + gap) if icon else 0
        if right:
            x = f"w-tw-{m + icon_space}"
        else:
            x = f"{m + icon_space}"
        # Центрируем текст по высоте иконки (или просто по margin, если иконки нет).
        y = f"{icon_y}+({ih}-th)/2" if icon else (f"H-th-{m}" if bottom else f"{m}")

        border = f":borderw=3:bordercolor=black@0.9" if cfg.outline else ""
        draw = (f"drawtext=fontfile='{_esc_path(cfg.font_path)}'"
                f":text='{_esc_text(cfg.nickname)}'"
                f":fontsize={cfg.text_size}:fontcolor={_hex(cfg.text_color)}"
                f"{border}:x={x}:y={y}")
        filters.append(f"[{cur}]{draw}[{out_label}]")
    else:
        # Была только иконка — переименуем последнюю метку в out_label.
        # (overlay уже писал в out_label_b1; добавим null для чистоты имени)
        filters.append(f"[{cur}]null[{out_label}]")

    return filters


def build_branding_at(nickname: str, platform: Platform,
                      nick_pl, platform_pl, canvas_w: int, canvas_h: int,
                      in_label: str, out_label: str,
                      font_path: str = DEFAULT_FONT,
                      text_color: RGB = (255, 255, 255)) -> list[str]:
    """Разместить ник и иконку платформы по ЯВНЫМ прямоугольникам (доли 9:16).

    Используется в свободной композиции: nick_pl/platform_pl — Placement (x,y,w,h).
    """
    filters: list[str] = []
    cur = in_label
    has_text = bool(nickname.strip()) and getattr(nick_pl, "visible", True)
    icon = None
    if platform != Platform.NONE and getattr(platform_pl, "visible", True):
        p = os.path.join(ICONS_DIR, f"{platform.value}.png")
        icon = p if os.path.isfile(p) else None

    if not has_text and not icon:
        filters.append(f"[{cur}]null[{out_label}]")
        return filters

    if icon:
        px = int(platform_pl.x * canvas_w)
        py = int(platform_pl.y * canvas_h)
        ph = max(16, int(platform_pl.h * canvas_h))
        tmp = f"{out_label}_ic"
        filters.append(f"movie='{_esc_path(icon)}',scale=-1:{ph}[{tmp}]")
        nxt = f"{out_label}_b1"
        filters.append(f"[{cur}][{tmp}]overlay=x={px}:y={py}[{nxt}]")
        cur = nxt

    if has_text:
        nx = int(nick_pl.x * canvas_w)
        ny = int(nick_pl.y * canvas_h)
        fs = max(16, int(nick_pl.h * canvas_h * 0.8))
        draw = (f"drawtext=fontfile='{_esc_path(font_path)}'"
                f":text='{_esc_text(nickname)}'"
                f":fontsize={fs}:fontcolor={_hex(text_color)}"
                f":borderw=3:bordercolor=black@0.9:x={nx}:y={ny}")
        filters.append(f"[{cur}]{draw}[{out_label}]")
    else:
        filters.append(f"[{cur}]null[{out_label}]")
    return filters


# --------------------------------------------------------------------------
# CLI: прожечь брендинг на видео для проверки
# --------------------------------------------------------------------------

def _main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Брендинг (тест на видео)")
    ap.add_argument("input")
    ap.add_argument("--nick", default="eg0rl1ke")
    ap.add_argument("--platform", choices=[p.value for p in Platform], default="twitch")
    ap.add_argument("--corner", choices=[c.value for c in Corner], default="top_left")
    ap.add_argument("--out", default="out/branding_test.mp4")
    args = ap.parse_args()

    from . import ffmpeg_utils as ff
    info = ff.probe_video(args.input)

    cfg = BrandingConfig(nickname=args.nick, platform=Platform(args.platform),
                         corner=Corner(args.corner))
    filters = build_branding_filter(cfg, info.width, info.height, "0:v", "vout")
    fc = ";".join(filters)

    codec = ["-c:v", "h264_nvenc", "-preset", "p5", "-b:v", "8M"] if ff.nvenc_available() \
        else ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
    ff_args = ["-y", "-i", args.input, "-filter_complex", fc,
               "-map", "[vout]", "-map", "0:a?", "-c:a", "aac",
               *codec, "-movflags", "+faststart", args.out]
    ff.run_ffmpeg(ff_args)
    log.info("Готово: %s", args.out)


if __name__ == "__main__":
    _main()
