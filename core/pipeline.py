"""pipeline.py — оркестратор: source → готовый вертикальный клип.

Шаги (см. SPEC → ПАЙПЛАЙН):
  1. ingest      — файл или yt-dlp
  2. transcribe  — faster-whisper (словные таймкоды)
  3. remap       — перенос таймкодов в координаты ВЫХОДНОГО клипа (учёт сегментов)
  4. profanity   — маскировка мата в тексте + интервалы для бипа (если вкл)
  5. captions    — .ass по стилю/анимации (если вкл)
  6. render      — раскладка + брендинг + субтитры + звук(бипы+loudnorm) за один проход

Forward-compat: сегментов может быть несколько (Этап 2). Здесь список из одного,
но remap уже умеет несколько отрезков со сдвигом по накопленному смещению.
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Optional


def _default_work_dir() -> str:
    """Служебная папка для промежуточных файлов (субтитры и т.п.) — в temp,
    чтобы рядом с итоговым клипом оставался ТОЛЬКО он, без мусора."""
    return os.path.join(tempfile.gettempdir(), "clip_polisher_work")

from .config import (ProjectConfig, Segment, LayoutConfig, LayoutPreset,
                     ExportConfig, VideoCodec)
from .captions import CaptionStyle, CaptionAnimation
from .branding import BrandingConfig
from . import ingest as ingest_mod
from . import transcribe as transcribe_mod
from . import profanity as profanity_mod
from . import captions as captions_mod
from . import render as render_mod

log = logging.getLogger("clip_polisher.pipeline")

ProgressCb = Callable[[float, str], None]  # (доля 0..1, текст стадии)


@dataclass
class PipelineConfig:
    source: str                                   # файл или URL
    start: float = 0.0
    end: Optional[float] = None                   # None → до конца
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    # Субтитры
    captions_enabled: bool = True
    caption_style: CaptionStyle = field(default_factory=CaptionStyle)
    caption_animation: CaptionAnimation = CaptionAnimation.POP
    # Мат
    profanity_enabled: bool = True
    profanity_mode: str = "beep"          # 'beep' (тон) | 'silence' (заглушить)
    # Свободная композиция на 9:16 (если задана — рендер по ней вместо пресета)
    composition: object = None
    # Брендинг
    branding: BrandingConfig = field(default_factory=BrandingConfig)
    # Звук
    loudnorm: bool = True
    # Транскрипция
    language: Optional[str] = "ru"
    model_size: str = "large-v3"
    # Служебное — промежуточные файлы (в temp, не рядом с итогом)
    work_dir: str = field(default_factory=_default_work_dir)


def _remap_words(words: list, segments: list[Segment]) -> list:
    """Перенести слова из времени ИСТОЧНИКА в время ВЫХОДНОГО клипа.

    Для каждого сегмента k слово со start в [s_k, e_k] сдвигается на накопленное
    смещение off_k = сумма длительностей предыдущих сегментов.
    """
    from .transcribe import Word
    out: list = []
    offset = 0.0
    for seg in segments:
        s, e = float(seg.start), float(seg.end)
        for w in words:
            if w.start >= s and w.start < e:
                ns = offset + (w.start - s)
                ne = offset + (min(w.end, e) - s)
                if ne > ns:
                    out.append(Word(start=ns, end=ne, text=w.text, prob=w.prob))
        offset += (e - s)
    return out


def run(pcfg: PipelineConfig, on_progress: Optional[ProgressCb] = None) -> str:
    """Выполнить пайплайн. Вернуть путь к готовому клипу."""
    def prog(frac: float, stage: str) -> None:
        if on_progress:
            on_progress(frac, stage)
        log.info("[%3.0f%%] %s", frac * 100, stage)

    os.makedirs(pcfg.work_dir, exist_ok=True)

    # 1) Ingest ------------------------------------------------------------
    prog(0.02, "Получение входного файла")
    input_path = ingest_mod.ingest(pcfg.source, dest_dir="tests/sample_clips")

    from . import ffmpeg_utils as ff
    info = ff.probe_video(input_path)
    end = pcfg.end if pcfg.end is not None else info.duration
    segments = [Segment(start=pcfg.start, end=end)]

    # 2) Transcribe --------------------------------------------------------
    prog(0.10, "Распознавание речи (Whisper, GPU)")
    def transcribe_prog(frac: float) -> None:
        prog(0.10 + 0.42 * frac, "Распознавание речи (Whisper)")
    result = transcribe_mod.transcribe_file(
        input_path, language=pcfg.language, model_size=pcfg.model_size,
        on_progress=transcribe_prog)

    # 3) Remap в координаты выхода ----------------------------------------
    words = _remap_words(result.words, segments)
    prog(0.55, f"Распознано слов: {len(words)}")

    # 4) Profanity ---------------------------------------------------------
    beep_intervals: list[tuple[float, float]] = []
    if pcfg.profanity_enabled:
        prof = profanity_mod.analyze_words(words, enabled=True)
        beep_intervals = prof.beep_intervals
        prog(0.58, f"Мат-фильтр: {prof.count} слов замаскировано")

    # 5) Captions (.ass) ---------------------------------------------------
    ass_path = None
    if pcfg.captions_enabled and words:
        ass_path = os.path.join(pcfg.work_dir, "subs.ass")
        # Позиция субтитров из композиции (центр зоны субтитров на 9:16).
        if pcfg.composition is not None:
            sub = pcfg.composition.subtitles
            pcfg.caption_style.position_v = min(0.95, max(0.05, sub.y + sub.h / 2))
        captions_mod.write_ass(ass_path, words, style=pcfg.caption_style,
                               animation=pcfg.caption_animation,
                               canvas_w=pcfg.export.width, canvas_h=pcfg.export.height)
        prog(0.62, "Субтитры .ass готовы")

    # 6) Render ------------------------------------------------------------
    prog(0.63, "Рендер (раскладка + брендинг + субтитры + звук)")
    cfg = ProjectConfig(input_path=input_path, segments=segments,
                        layout=pcfg.layout, composition=pcfg.composition, export=pcfg.export)
    # В режиме композиции брендинг ставится по прямоугольникам ника/платформы всегда.
    use_branding = (pcfg.composition is not None) or \
        (pcfg.branding.nickname or pcfg.branding.platform.value != "none")
    extras = render_mod.RenderExtras(
        ass_path=ass_path,
        branding=pcfg.branding if use_branding else None,
        beep_intervals=beep_intervals,
        beep_mode=pcfg.profanity_mode,
        loudnorm=pcfg.loudnorm,
    )

    def render_prog(frac: float) -> None:
        prog(0.63 + 0.37 * frac, "Рендер")

    out_path = render_mod.render(cfg, extras=extras, on_progress=render_prog)
    prog(1.0, f"Готово: {out_path}")
    return out_path


def run_batch(pcfgs: list[PipelineConfig],
              on_progress: Optional[ProgressCb] = None) -> list[str]:
    """Режим «пачкой»: несколько клипов, у каждого свой PipelineConfig."""
    results = []
    n = len(pcfgs)
    for i, pc in enumerate(pcfgs):
        def prog(frac, stage, _i=i):
            if on_progress:
                on_progress((_i + frac) / n, f"[{_i+1}/{n}] {stage}")
        results.append(run(pc, on_progress=prog))
    return results


# --------------------------------------------------------------------------
# CLI: полный прогон
# --------------------------------------------------------------------------

def _main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "huggingface_hub", "huggingface_hub.utils._http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(description="Полный пайплайн: клип → вертикаль")
    ap.add_argument("source", help="файл или URL Twitch-клипа")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--preset", choices=[p.value for p in LayoutPreset], default="A")
    ap.add_argument("--anim", choices=[a.value for a in CaptionAnimation], default="pop")
    ap.add_argument("--nick", default="")
    ap.add_argument("--platform", default="none",
                    choices=["none", "twitch", "youtube", "kick"])
    ap.add_argument("--no-profanity", action="store_true")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--box", action="store_true", help="плашка под субтитрами")
    ap.add_argument("--out", default="out/final.mp4")
    ap.add_argument("--cpu", action="store_true")
    args = ap.parse_args()

    from .branding import Platform
    from .layout import load_preset
    pcfg = PipelineConfig(
        source=args.source, start=args.start, end=args.end,
        layout=load_preset(args.preset),  # зоны из presets/layout_<ID>.json
        export=ExportConfig(codec=VideoCodec.X264 if args.cpu else VideoCodec.NVENC,
                            out_dir=os.path.dirname(args.out) or ".",
                            filename=os.path.basename(args.out)),
        caption_style=CaptionStyle(box=args.box),
        caption_animation=CaptionAnimation(args.anim),
        captions_enabled=not args.no_captions,
        profanity_enabled=not args.no_profanity,
        branding=BrandingConfig(nickname=args.nick, platform=Platform(args.platform)),
    )
    path = run(pcfg)
    print(f"\nГОТОВО: {path}")


if __name__ == "__main__":
    _main()
