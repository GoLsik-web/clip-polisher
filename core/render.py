"""render.py — сборка вертикального клипа через FFmpeg (интегратор пайплайна).

Собирает ВСЁ за один проход кодирования:
  видео: [сегменты → раскладка] → concat → брендинг → субтитры(.ass) → [vout]
  звук:  [сегменты] → concat → бипы(мат) → loudnorm → [aout]

Forward-compat: клип собирается из СПИСКА сегментов (Этап 1 — один сегмент).
Кодек h264_nvenc (GPU) с авто-фолбэком на libx264 (CPU).

Тайминги субтитров и бипов должны быть в координатах ВЫХОДНОГО клипа
(после trim+concat). За это отвечает pipeline.py.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional

from .config import ProjectConfig, VideoCodec, unique_path
from . import ffmpeg_utils as ff
from . import audio as audio_mod
from .layout import build_layout_filtergraph
from .compositor import build_composition_segment

log = logging.getLogger("clip_polisher.render")


@dataclass
class RenderExtras:
    """Доп. слои поверх базового рендера (готовит pipeline)."""
    ass_path: Optional[str] = None                      # путь к .ass субтитрам
    branding: Optional[object] = None                   # BrandingConfig | None
    beep_intervals: Optional[list[tuple[float, float]]] = None  # в координатах выхода
    beep_mode: str = "beep"                             # 'beep' | 'silence'
    loudnorm: bool = True


def _esc_filter_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:")


def _build_graph(cfg: ProjectConfig, info: ff.VideoInfo,
                 extras: RenderExtras) -> tuple[str, bool, bool]:
    """Построить filter_complex. Возвращает (граф, есть_аудио, нужен_тон_sine)."""
    cw, ch = cfg.export.width, cfg.export.height
    use_audio = info.has_audio
    parts: list[str] = []
    v_concat: list[str] = []
    a_concat: list[str] = []

    for i, seg in enumerate(cfg.segments):
        s, e = float(seg.start), float(seg.end)
        vsrc, vlay = f"v{i}_src", f"v{i}_lay"
        parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[{vsrc}]")
        if cfg.composition is not None:
            parts.extend(build_composition_segment(
                cfg.composition, cw, ch, in_label=vsrc, out_label=vlay, tag=f"s{i}"))
        else:
            parts.extend(build_layout_filtergraph(
                cfg.layout, info.width, info.height, cw, ch,
                in_label=vsrc, out_label=vlay, tag=f"s{i}"))
        v_concat.append(f"[{vlay}]")
        if use_audio:
            parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
            a_concat.append(f"[a{i}]")

    n = len(cfg.segments)
    parts.append(f"{''.join(v_concat)}concat=n={n}:v=1:a=0[vcat]")
    if use_audio:
        parts.append(f"{''.join(a_concat)}concat=n={n}:v=0:a=1[acat]")

    # ----- Видео: брендинг → субтитры → нормализация -----
    cur = "vcat"
    if extras.branding is not None:
        if cfg.composition is not None:
            from .branding import build_branding_at
            parts.extend(build_branding_at(
                extras.branding.nickname, extras.branding.platform,
                cfg.composition.nick, cfg.composition.platform, cw, ch, cur, "vbr"))
        else:
            from .branding import build_branding_filter
            parts.extend(build_branding_filter(extras.branding, cw, ch, cur, "vbr"))
        cur = "vbr"
    if extras.ass_path:
        from .resources import res
        ass = _esc_filter_path(extras.ass_path)
        fonts = _esc_filter_path(res("assets/fonts"))
        parts.append(f"[{cur}]ass='{ass}':fontsdir='{fonts}'[vsub]")
        cur = "vsub"
    parts.append(f"[{cur}]fps={cfg.export.fps},format=yuv420p[vout]")

    # ----- Звук: бипы + loudnorm -----
    need_tone = False
    if use_audio:
        intervals = extras.beep_intervals or []
        need_tone = bool(intervals) and extras.beep_mode == "beep"
        # tone_label — вход sine (индекс 1), нужен только для режима 'beep'.
        parts.extend(audio_mod.build_audio_filter(
            intervals, src_label="acat", tone_label="1:a",
            out_label="aout", loudnorm=extras.loudnorm, mode=extras.beep_mode))

    return ";".join(parts), use_audio, need_tone


def _validate_output(path: str, expected_dur: float) -> None:
    """Проверить, что рендер дал валидный воспроизводимый файл.

    Ловит классику битого выхода ДО того, как пользователь понесёт клип другу/
    в соцсеть: файла нет/пустой, нет видео-потока, аудио на «неправильной» частоте
    (>48 кГц → грабля loudnorm), длительность сильно короче ожидаемой (обрезка).
    """
    if not os.path.isfile(path) or os.path.getsize(path) < 1024:
        raise RuntimeError(f"Рендер не создал файл (или он пустой): {path}")
    try:
        info = ff.probe_video(path)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Готовый файл не читается ffprobe (битый mp4): {e}")
    if info.width <= 0 or info.height <= 0:
        raise RuntimeError("В готовом файле нет корректного видео-потока")
    if expected_dur > 1.0 and info.duration < expected_dur * 0.5:
        raise RuntimeError(
            f"Клип обрезан: {info.duration:.1f} c вместо ~{expected_dur:.1f} c "
            "(вероятно проблема с потоками/‑shortest)")
    log.info("Валидация выхода OK: %dx%d, %.1f c", info.width, info.height, info.duration)


def _encoder_args(codec: VideoCodec, cfg: ProjectConfig) -> list[str]:
    if codec == VideoCodec.NVENC:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                "-b:v", cfg.export.video_bitrate, "-profile:v", "high"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]


def render(cfg: ProjectConfig, extras: Optional[RenderExtras] = None,
           on_progress: Optional[Callable[[float], None]] = None) -> str:
    """Отрендерить вертикальный клип. Вернуть путь к результату."""
    extras = extras or RenderExtras()
    if not cfg.segments:
        raise ValueError("Нет сегментов для рендера")
    if not os.path.isfile(cfg.input_path):
        raise FileNotFoundError(cfg.input_path)

    info = ff.probe_video(cfg.input_path)
    log.info("Источник: %dx%d, %.2f fps, %.1f c, звук=%s",
             info.width, info.height, info.fps, info.duration, info.has_audio)

    codec = cfg.export.codec
    if codec == VideoCodec.NVENC and not ff.nvenc_available():
        log.warning("h264_nvenc недоступен → фолбэк на libx264 (CPU).")
        codec = VideoCodec.X264
    log.info("Кодек видео: %s", codec.value)

    graph, has_audio, need_tone = _build_graph(cfg, info, extras)

    os.makedirs(os.path.dirname(os.path.abspath(cfg.export.output_path())), exist_ok=True)
    # Не перезаписываем существующий клип — кладём рядом «имя (2).mp4».
    out_path = unique_path(cfg.export.output_path())

    total = sum(seg.duration for seg in cfg.segments)

    inputs = ["-i", cfg.input_path]
    if need_tone:
        # Конечный тон (длиннее клипа на 1 c) — не нужен глобальный -shortest,
        # который из-за задержки loudnorm мог укорачивать видео.
        inputs += audio_mod.beep_input_args(duration=total + 1.0)

    args = ["-y", *inputs, "-filter_complex", graph, "-map", "[vout]"]
    if has_audio:
        args += ["-map", "[aout]", "-c:a", "aac", "-b:a", cfg.export.audio_bitrate,
                 "-ar", "48000"]
    args += _encoder_args(codec, cfg)
    # CFR по целевому fps: гарантирует монотонные timestamps (иначе часть плееров
    # «застывает» на VFR-выходе). Видео уже CFR из фильтра fps=, тут — подстраховка.
    args += ["-fps_mode", "cfr", "-movflags", "+faststart", out_path]

    ff.run_ffmpeg(args, total_duration=total, on_progress=on_progress)
    _validate_output(out_path, total)
    log.info("Готово: %s", out_path)
    return out_path


# --------------------------------------------------------------------------
# CLI (базовый рендер без субтитров/брендинга — для быстрой проверки раскладки)
# --------------------------------------------------------------------------

def _main() -> None:
    import argparse
    from .config import LayoutConfig, LayoutPreset, ExportConfig, Segment

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Базовый рендер вертикали (раскладка)")
    ap.add_argument("input")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--preset", choices=[p.value for p in LayoutPreset], default="A")
    ap.add_argument("--out", default="out/clip_vertical.mp4")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--cpu", action="store_true")
    args = ap.parse_args()

    info = ff.probe_video(args.input)
    end = args.end if args.end is not None else info.duration
    cfg = ProjectConfig(
        input_path=args.input,
        segments=[Segment(args.start, end)],
        layout=LayoutConfig(preset=LayoutPreset(args.preset)),
        export=ExportConfig(fps=args.fps,
                            codec=VideoCodec.X264 if args.cpu else VideoCodec.NVENC,
                            out_dir=os.path.dirname(args.out) or ".",
                            filename=os.path.basename(args.out)),
    )

    def prog(f): print(f"\rПрогресс: {f*100:5.1f}%", end="", flush=True)
    path = render(cfg, on_progress=prog)
    print(f"\nСохранено: {path}")


if __name__ == "__main__":
    _main()
