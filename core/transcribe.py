"""transcribe.py — речь → текст через faster-whisper (large-v3) на GPU.

Возвращает СЛОВА со таймкодами (word_timestamps=True) — это основа для .ass
субтитров и мат-фильтра.

Грабли (см. SPEC):
  - Тихий откат на CPU: ЯВНО логируем устройство и compute_type.
  - VRAM 8 ГБ: compute_type=float16; при OOM — фолбэк int8_float16, затем модель medium.
  - На Windows DLL из колёс nvidia-*-cu12 надо явно добавить в путь (делаем в _add_cuda_dlls).
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from typing import Callable, Optional

log = logging.getLogger("clip_polisher.transcribe")


def _enhance_speech(src: str, out_wav: str) -> bool:
    """Очистить речь перед распознаванием: убрать шум/гул, поднять тихий голос.

    highpass — срез гула; afftdn — FFT-денойз (фоновая музыка/шум игры);
    lowpass — срез шипения; dynaudnorm — динамическая нормализация (тихие слова
    становятся слышны). Моно 16 кГц — как раз то, что ест Whisper.
    Возвращает True при успехе (иначе транскрайбим исходник).
    """
    from . import ffmpeg_utils as ff
    cmd = [ff.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
           "-ac", "1", "-ar", "16000",
           "-af", "highpass=f=95,afftdn=nf=-22,lowpass=f=7800,dynaudnorm=f=180:g=9",
           out_wav]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=ff._CREATE_NO_WINDOW, timeout=300)
        return r.returncode == 0 and os.path.isfile(out_wav) and os.path.getsize(out_wav) > 0
    except Exception as e:  # noqa: BLE001
        log.warning("Очистка речи не удалась (%r) — распознаю исходник", e)
        return False


# --------------------------------------------------------------------------
# Модель данных результата
# --------------------------------------------------------------------------

@dataclass
class Word:
    start: float          # секунды от начала переданного аудио
    end: float
    text: str             # слово с ведущим пробелом от Whisper — храним очищенным
    prob: float = 1.0     # вероятность (уверенность) слова 0..1


@dataclass
class TranscriptResult:
    words: list[Word]
    language: str
    device: str
    model: str
    compute_type: str

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words).strip()

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# --------------------------------------------------------------------------
# Настройка CUDA DLL на Windows
# --------------------------------------------------------------------------

def _add_cuda_dlls() -> None:
    """Добавить в путь DLL из колёс nvidia-*-cu12 (Windows).

    ctranslate2 не всегда находит cublas/cudnn сам. Безопасный no-op, если
    пакетов нет или мы не на Windows.
    """
    if os.name != "nt":
        return
    # Скачанные при первом запуске CUDA-DLL (в собранном .exe модуля nvidia нет).
    try:
        from . import provision
        provision.add_cuda_to_path()
    except Exception:  # noqa: BLE001
        pass
    try:
        import nvidia
    except ImportError:
        return
    # nvidia — namespace-пакет: __file__ может быть None, берём __path__.
    bases = list(getattr(nvidia, "__path__", []) or [])
    if not bases and getattr(nvidia, "__file__", None):
        bases = [os.path.dirname(nvidia.__file__)]
    for base in bases:
        for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
            for d in (os.path.join(base, sub, "bin"), os.path.join(base, sub, "lib")):
                if os.path.isdir(d):
                    try:
                        os.add_dll_directory(d)
                        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                    except OSError:
                        pass


# --------------------------------------------------------------------------
# Загрузка модели (с кэшем и фолбэками)
# --------------------------------------------------------------------------

_model_cache: dict[tuple, object] = {}


def _load_model(model_size: str, device: str, compute_type: str):
    key = (model_size, device, compute_type)
    if key in _model_cache:
        return _model_cache[key]
    from faster_whisper import WhisperModel
    from . import provision
    # Если модель уже докачана в папку пользователя — грузим оттуда (offline),
    # иначе faster-whisper скачает её в download_root (первый запуск).
    name_or_path = model_size
    if model_size == "large-v3" and provision.model_ready():
        name_or_path = provision.model_path()
    log.info("Загрузка Whisper: модель=%s device=%s compute_type=%s",
             name_or_path, device, compute_type)
    m = WhisperModel(name_or_path, device=device, compute_type=compute_type,
                     download_root=provision.models_dir())
    _model_cache[key] = m
    return m


def _load_with_fallbacks(model_size: str, prefer_gpu: bool):
    """Пытаемся GPU/float16 → GPU/int8_float16 → medium/float16 → CPU/int8.
    Возвращает (model, device, compute_type, model_size)."""
    attempts = []
    if prefer_gpu:
        attempts += [
            (model_size, "cuda", "float16"),
            (model_size, "cuda", "int8_float16"),
            ("medium", "cuda", "float16"),      # фолбэк при нехватке VRAM
        ]
    attempts += [(model_size, "cpu", "int8")]   # последний рубеж

    last_err: Optional[Exception] = None
    for size, dev, ct in attempts:
        try:
            m = _load_model(size, dev, ct)
            if dev == "cpu" and prefer_gpu:
                log.warning("!!! Whisper работает на CPU — GPU недоступен. "
                            "Проверьте CUDA/cuDNN и драйвер. Будет медленно.")
            return m, dev, ct, size
        except Exception as e:  # noqa: BLE001 — логируем и идём к следующему варианту
            last_err = e
            log.warning("Не удалось загрузить %s/%s/%s: %r", size, dev, ct, e)
    raise RuntimeError(f"Не удалось загрузить Whisper ни в одном режиме: {last_err}")


# --------------------------------------------------------------------------
# Основная функция
# --------------------------------------------------------------------------

def transcribe_file(
    path: str,
    language: Optional[str] = "ru",
    model_size: str = "large-v3",
    prefer_gpu: bool = True,
    vad_filter: bool = True,
    beam_size: int = 5,
    initial_prompt: Optional[str] = None,
    denoise: bool = True,
    on_progress: Optional[Callable[[float], None]] = None,
) -> TranscriptResult:
    """Расшифровать аудио файла (mp4/wav/...) со словными таймкодами.

    language=None → авто-определение. Для русских стримеров дефолт "ru".
    Параметры подобраны под НЕВНЯТНУЮ стримерскую речь: мягкий VAD (ловит тихое),
    анти-галлюцинации и анти-повторы (меньше «странных» слов), точные словные тайминги.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    _add_cuda_dlls()
    model, device, compute_type, used_size = _load_with_fallbacks(model_size, prefer_gpu)

    # Очистка речи (денойз + подъём тихого голоса) — «слышит лучше» на шумных клипах.
    audio_path = path
    if denoise:
        # Имя с PID — чтобы два запущенных экземпляра приложения не затирали
        # временный wav друг друга (это и есть мнимый «конфликт с другой прогой»).
        clean = os.path.join(tempfile.gettempdir(), f"clip_polisher_clean_{os.getpid()}.wav")
        if _enhance_speech(path, clean):
            audio_path = clean
            log.info("Речь очищена перед распознаванием (денойз+нормализация)")

    log.info("Транскрипция: %s (language=%s, vad=%s)", os.path.basename(path),
             language or "auto", vad_filter)
    # VAD помягче: ловим тихую/невнятную речь (порог 0.5→0.35), паддинг вокруг слов,
    # не режем короткие паузы слишком агрессивно.
    vad_params = dict(threshold=0.35, min_silence_duration_ms=500,
                      speech_pad_ms=250, min_speech_duration_ms=0)
    segments, info = model.transcribe(
        audio_path,
        language=language,
        task="transcribe",
        word_timestamps=True,                 # словные таймкоды (нужны и для анти-галлюцинаций)
        beam_size=beam_size, best_of=5, patience=2,   # шире поиск → точнее слова
        temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],   # фолбэк при плохом декодировании
        condition_on_previous_text=True,      # связность между сегментами
        no_repeat_ngram_size=3,               # меньше повторов-галлюцинаций
        repetition_penalty=1.05,
        no_speech_threshold=0.6,
        log_prob_threshold=-1.2,              # не выбрасываем не очень уверенные слова
        compression_ratio_threshold=2.6,
        hallucination_silence_threshold=2.0,  # пропускаем «бред» в тишине (странные слова)
        vad_filter=vad_filter,
        vad_parameters=vad_params if vad_filter else None,
        initial_prompt=initial_prompt,
    )

    words: list[Word] = []
    # info.duration — длительность аудио; используем для реального прогресса.
    audio_dur = float(getattr(info, "duration", 0.0) or 0.0)
    # segments — генератор; итерируя его, мы запускаем распознавание.
    for seg in segments:
        if on_progress and audio_dur > 0:
            on_progress(min(1.0, float(seg.end) / audio_dur))
        if seg.words:
            for w in seg.words:
                txt = (w.word or "").strip()
                if not txt:
                    continue
                ws, we = float(w.start), float(w.end)
                if we <= ws:                      # чиним битые тайминги
                    we = ws + 0.08
                words.append(Word(start=ws, end=we, text=txt,
                                  prob=float(getattr(w, "probability", 1.0))))
        else:
            # На случай, если словных таймкодов нет — берём сегмент целиком.
            txt = (seg.text or "").strip()
            if txt:
                words.append(Word(start=float(seg.start), end=float(seg.end), text=txt))

    detected = getattr(info, "language", language or "?")
    log.info("Готово: %d слов, язык=%s (p=%.2f), устройство=%s/%s",
             len(words), detected, getattr(info, "language_probability", 1.0),
             device, compute_type)

    return TranscriptResult(words=words, language=detected, device=device,
                            model=used_size, compute_type=compute_type)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main() -> None:
    import argparse
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Приглушить болтливые сторонние логгеры.
    for noisy in ("httpx", "huggingface_hub", "huggingface_hub.utils._http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Транскрипция клипа (faster-whisper)")
    ap.add_argument("input", help="файл mp4/wav")
    ap.add_argument("--lang", default="ru", help="язык (или 'auto')")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--cpu", action="store_true", help="форс CPU")
    ap.add_argument("--json", help="сохранить результат в JSON")
    args = ap.parse_args()

    lang = None if args.lang == "auto" else args.lang
    res = transcribe_file(args.input, language=lang, model_size=args.model,
                          prefer_gpu=not args.cpu)

    print("\n--- ТЕКСТ ---")
    print(res.text)
    print("\n--- ПЕРВЫЕ 20 СЛОВ С ТАЙМКОДАМИ ---")
    for w in res.words[:20]:
        print(f"  [{w.start:6.2f} - {w.end:6.2f}]  {w.text}  (p={w.prob:.2f})")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"\nJSON сохранён: {args.json}")


if __name__ == "__main__":
    _main()
