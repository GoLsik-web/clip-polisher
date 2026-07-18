"""preview.py — быстрый стоп-кадр без полного рендера.

Вариант 1: кадр из клипа в момент t с наложенной раскладкой/брендингом/субтитрами
           (для проверки разметки зон и стиля до долгого рендера).
Вариант 2: пользовательская картинка-превью, вписанная в 9:16.

Живого видео-превью на Этапе 1 нет (по SPEC).
"""
from __future__ import annotations

import logging
import os
import random
from typing import Optional

from .config import LayoutConfig
from . import ffmpeg_utils as ff
from .layout import build_layout_filtergraph

log = logging.getLogger("clip_polisher.preview")


def _esc(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:")


def freeze_preview(
    input_path: str,
    layout: LayoutConfig,
    out_png: str = "out/preview.png",
    at_time: Optional[float] = None,
    canvas_w: int = 1080,
    canvas_h: int = 1920,
    branding: Optional[object] = None,
    ass_path: Optional[str] = None,
    composition: Optional[object] = None,
) -> str:
    """Собрать стоп-кадр 9:16: раскладка/КОМПОЗИЦИЯ (+брендинг, +субтитры) в момент at_time.

    Если задана composition — рендерим свободную композицию (как финалку). Иначе —
    старый путь по пресету (layout). Субтитры показываются активные в этот момент.
    """
    info = ff.probe_video(input_path)
    if at_time is None:
        at_time = random.uniform(0.1, max(0.2, info.duration - 0.1))

    parts: list[str] = []
    if composition is not None:
        from .compositor import build_composition_segment
        parts.extend(build_composition_segment(
            composition, canvas_w, canvas_h, in_label="0:v", out_label="lay", tag="pv"))
        cur = "lay"
        if branding is not None:
            from .branding import build_branding_at
            parts.extend(build_branding_at(
                branding.nickname, branding.platform,
                composition.nick, composition.platform, canvas_w, canvas_h, cur, "br"))
            cur = "br"
    else:
        # Раскладка прямо на входном потоке (без trim — нам нужен один кадр).
        parts.extend(build_layout_filtergraph(
            layout, info.width, info.height, canvas_w, canvas_h,
            in_label="0:v", out_label="lay", tag="pv"))
        cur = "lay"
        if branding is not None:
            from .branding import build_branding_filter
            parts.extend(build_branding_filter(branding, canvas_w, canvas_h, cur, "br"))
            cur = "br"

    if ass_path:
        from .resources import res
        parts.append(f"[{cur}]ass='{_esc(ass_path)}':fontsdir='{_esc(res('assets/fonts'))}'[sub]")
        cur = "sub"

    parts.append(f"[{cur}]format=yuv420p[vout]")
    graph = ";".join(parts)

    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    # -ss ПОСЛЕ -i (output seek) сохраняет PTS кадра → .ass показывает нужный момент.
    args = ["-y", "-i", input_path, "-ss", f"{at_time:.3f}",
            "-filter_complex", graph, "-map", "[vout]",
            "-frames:v", "1", out_png]
    ff.run_ffmpeg(args)
    log.info("Превью-кадр (t=%.2f): %s", at_time, out_png)
    return out_png


def image_preview(image_path: str, out_png: str = "out/preview.png",
                  canvas_w: int = 1080, canvas_h: int = 1920) -> str:
    """Вписать пользовательскую картинку в 9:16 (cover-кроп по центру)."""
    if not os.path.isfile(image_path):
        raise FileNotFoundError(image_path)
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    vf = (f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=increase,"
          f"crop={canvas_w}:{canvas_h}")
    ff.run_ffmpeg(["-y", "-i", image_path, "-vf", vf, "-frames:v", "1", out_png])
    log.info("Превью из картинки: %s", out_png)
    return out_png


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main() -> None:
    import argparse
    from .config import LayoutPreset
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Стоп-кадр превью")
    ap.add_argument("input")
    ap.add_argument("--preset", choices=[p.value for p in LayoutPreset], default="A")
    ap.add_argument("--time", type=float, default=None)
    ap.add_argument("--ass", default=None, help="путь к .ass для наложения субтитров")
    ap.add_argument("--nick", default="")
    ap.add_argument("--platform", default="none",
                    choices=["none", "twitch", "youtube", "kick"])
    ap.add_argument("--out", default="out/preview.png")
    args = ap.parse_args()

    branding = None
    if args.nick or args.platform != "none":
        from .branding import BrandingConfig, Platform
        branding = BrandingConfig(nickname=args.nick, platform=Platform(args.platform))

    freeze_preview(args.input, LayoutConfig(preset=LayoutPreset(args.preset)),
                   out_png=args.out, at_time=args.time,
                   branding=branding, ass_path=args.ass)


if __name__ == "__main__":
    _main()
