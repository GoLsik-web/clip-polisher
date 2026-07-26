"""audio.py — обработка звука: нормализация громкости (loudnorm EBU R128)
и «бипы» 1 кГц поверх интервалов мата.

Модуль возвращает ФРАГМЕНТ аудио-графа FFmpeg (список фильтров), который
render.py встраивает в общий filter_complex. Это позволяет сделать всё за один
проход кодирования. Отдельный CLI прожигает звук на реальном файле для проверки.

Логика бипа: на интервале мата оригинал глушится (volume→0), а вместо него
подставляется гейтованный тон 1 кГц (volume→1 только на интервале). Затем всё
суммируется и нормализуется по громкости.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("clip_polisher.audio")

# Дефолты нормализации (SPEC): I=-14 LUFS, TP=-1.5 dBTP.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.5
LOUDNORM_LRA = 11.0
BEEP_FREQ = 1000
BEEP_SR = 48000


def beep_input_args(freq: int = BEEP_FREQ, sr: int = BEEP_SR,
                    duration: Optional[float] = None) -> list[str]:
    """Аргументы дополнительного lavfi-входа с тоном (добавляются к ffmpeg -i).

    duration задаёт КОНЕЧНУЮ длину тона (сек). Раньше тон был бесконечным и его
    приходилось резать глобальным `-shortest`, а тот из-за задержки loudnorm мог
    обрезать видео раньше времени → битый/укороченный клип. Конечный тон убирает
    саму причину: даём длину чуть больше клипа, дальше его ограничит amix=first.
    """
    src = f"sine=frequency={freq}:sample_rate={sr}"
    if duration is not None:
        src += f":duration={max(0.1, duration):.3f}"
    return ["-f", "lavfi", "-i", src]


def _gate_expr(intervals: list[tuple[float, float]]) -> str:
    """Булево выражение «t внутри любого интервала» для volume:eval=frame."""
    if not intervals:
        return "0"
    return "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in intervals)


def _enhance_chain(denoise: bool, clarity: bool, gate: bool,
                   denoise_strength: float = 12.0) -> list[str]:
    """Фильтры «продвинутого звука», применяемые к итогу до loudnorm.

    denoise  — FFT-шумодав (afftdn): убирает ровный фон/шипение (сила в дБ).
    clarity  — чёткость голоса: срез низкого гула (highpass) + лёгкий подъём
               «присутствия» (~3 кГц), голос звонче без «бубнежа».
    gate     — шумовой гейт (agate): в паузах приглушает тихий фон, чтоб не шипело.
    Порядок: шумодав → гейт → эквалайзер (сначала чистим, потом красим).
    """
    chain: list[str] = []
    if denoise:
        chain.append(f"afftdn=nr={max(1.0, denoise_strength):.1f}:nf=-25")
    if gate:
        # Мягкий гейт: тихое (ниже порога) приглушается, речь проходит.
        chain.append("agate=threshold=0.03:ratio=2:attack=5:release=120")
    if clarity:
        chain.append("highpass=f=85")
        chain.append("equalizer=f=3000:t=q:w=1.2:g=2.5")
    return chain


def build_audio_filter(
    intervals: list[tuple[float, float]],
    src_label: str = "0:a",
    tone_label: str = "1:a",
    out_label: str = "aout",
    loudnorm: bool = True,
    mode: str = "beep",           # 'beep' (тон 1 кГц) | 'silence' (заглушить)
    denoise: bool = False,        # продвинутый звук: FFT-шумодав
    clarity: bool = False,        # продвинутый звук: чёткость голоса (EQ)
    gate: bool = False,           # продвинутый звук: шумовой гейт
    I: float = LOUDNORM_I,
    TP: float = LOUDNORM_TP,
    LRA: float = LOUDNORM_LRA,
) -> list[str]:
    """Построить фрагмент аудио-графа.

    intervals пуст → только (опц.) улучшение + нормализация. Иначе на интервалах глушим
    оригинал и, если mode='beep', подмешиваем гейтованный тон (tone_label); mode='silence'
    — просто тишина. Улучшения звука (denoise/clarity/gate) идут к суммарному сигналу
    ПОСЛЕ подмешивания бипа, но ДО loudnorm (чистим сигнал, затем нормализуем громкость).
    """
    fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    filters: list[str] = []

    if intervals:
        gate_expr = _gate_expr(intervals)
        filters.append(f"[{src_label}]{fmt},volume=volume='if({gate_expr},0,1)':eval=frame[a_voice]")
        if mode == "beep":
            filters.append(f"[{tone_label}]{fmt},volume=volume='if({gate_expr},1,0)':eval=frame[a_beep]")
            # duration=first — длину задаёт голос, иначе бесконечный sine → вечный рендер.
            filters.append(f"[a_voice][a_beep]amix=inputs=2:normalize=0:duration=first[a_sum]")
        else:  # silence
            filters.append(f"[a_voice]anull[a_sum]")
        cur = "a_sum"
    else:
        filters.append(f"[{src_label}]{fmt}[a_sum]")
        cur = "a_sum"

    # Продвинутый звук (шумодав/чёткость/гейт) — к суммарному сигналу до нормализации.
    enh = _enhance_chain(denoise, clarity, gate)
    if enh:
        filters.append(f"[{cur}]{','.join(enh)}[a_enh]")
        cur = "a_enh"

    if loudnorm:
        # ГРАБЛЯ: фильтр loudnorm внутри РЕСЕМПЛИТ звук до 192 кГц и отдаёт его
        # на этой частоте. Без явного aresample после него AAC кодирует 96 кГц —
        # такой mp4 «играет 2 сек и виsnет» / не открывается в части плееров и
        # соцсетях (TikTok/Instagram). Поэтому ОБЯЗАТЕЛЬНО возвращаем 48 кГц ПОСЛЕ.
        filters.append(f"[{cur}]loudnorm=I={I}:TP={TP}:LRA={LRA},aresample=48000[{out_label}]")
    else:
        filters.append(f"[{cur}]aresample=48000[{out_label}]")

    return filters


# --------------------------------------------------------------------------
# CLI: прожечь обработанный звук на файле (для проверки)
# --------------------------------------------------------------------------

def _main() -> None:
    import argparse
    import json
    import os
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    ap = argparse.ArgumentParser(description="Нормализация + бипы (тест на файле)")
    ap.add_argument("input", help="видео/аудио файл")
    ap.add_argument("--transcript", help="JSON транскрипта для авто-поиска мата")
    ap.add_argument("--interval", action="append", default=[],
                    help="интервал бипа 'start,end' (можно несколько)")
    ap.add_argument("--no-loudnorm", action="store_true")
    ap.add_argument("--out", default="out/audio_test.mp4")
    args = ap.parse_args()

    from . import ffmpeg_utils as ff

    intervals: list[tuple[float, float]] = []
    if args.transcript:
        from .profanity import analyze_words

        class _W:
            def __init__(self, d):
                self.text = d["text"]; self.start = d["start"]; self.end = d["end"]
        with open(args.transcript, encoding="utf-8") as f:
            data = json.load(f)
        words = [_W(d) for d in data["words"]]
        intervals = analyze_words(words, enabled=True).beep_intervals
    for it in args.interval:
        s, e = it.split(",")
        intervals.append((float(s), float(e)))

    log.info("Интервалов бипа: %d → %s", len(intervals), intervals)

    filters = build_audio_filter(intervals, loudnorm=not args.no_loudnorm)
    filter_complex = ";".join(filters)

    inputs = ["-i", args.input]
    if intervals:
        inputs += beep_input_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    codec = ["-c:v", "copy"]  # видео не трогаем — только звук
    ff_args = ["-y", *inputs, "-filter_complex", filter_complex,
               "-map", "0:v:0", "-map", "[aout]",
               *codec, "-c:a", "aac", "-b:a", "192k",
               "-shortest",  # подстраховка: не тянуть за бесконечным lavfi-входом
               "-movflags", "+faststart", args.out]
    ff.run_ffmpeg(ff_args)
    log.info("Готово: %s", args.out)


if __name__ == "__main__":
    _main()
