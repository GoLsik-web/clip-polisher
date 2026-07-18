"""captions.py — генерация субтитров .ass (libass) из словных таймкодов.

Стиль TikTok/Shorts: КРУПНОЕ слово по центру (по одному слову за раз) для
анимаций Pop/Fade/Slide-up; для Karaoke — фраза из нескольких слов с подсветкой
текущего. Реестр анимаций data-driven — добавить новую = дописать функцию в ANIMATIONS.

Грабли .ass (см. SPEC):
  - Цвет: &HAABBGGRR — BGR + ИНВЕРТИРОВАННАЯ альфа (00=непрозрачно, FF=прозрачно).
  - Тайминги: H:MM:SS.cs (сотые секунды).
  - Экранирование: {} и \\ ломают разметку; переводы строк → \\N.
  - Кириллица: libass берёт шрифт по имени; нужен fontsdir с TTF, где есть кириллица.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Optional

log = logging.getLogger("clip_polisher.captions")


# --------------------------------------------------------------------------
# Стиль субтитров
# --------------------------------------------------------------------------

class CaptionAnimation(str, Enum):
    POP = "pop"
    FADE = "fade"
    SLIDE_UP = "slide_up"
    KARAOKE = "karaoke"


RGB = tuple[int, int, int]


@dataclass
class CaptionStyle:
    font_name: str = "PT Sans"
    font_size: int = 96
    bold: bool = True
    primary: RGB = (255, 255, 255)      # цвет текста
    outline_color: RGB = (0, 0, 0)      # цвет обводки
    outline_width: float = 4.0          # толщина обводки
    shadow: float = 0.0
    # Фон-плашка (BorderStyle=3). Если box=False — обычная обводка (BorderStyle=1).
    box: bool = False
    box_color: RGB = (0, 0, 0)
    box_opacity: float = 0.6            # 0..1 (1 = непрозрачная плашка)
    # Подсветка текущего слова в Karaoke.
    highlight: RGB = (255, 214, 10)     # янтарный
    # Вертикальная позиция центра текста как доля высоты канвы (0..1).
    position_v: float = 0.72


# --------------------------------------------------------------------------
# Утилиты формата .ass
# --------------------------------------------------------------------------

def ass_color(rgb: RGB, opacity: float = 1.0) -> str:
    """RGB + непрозрачность(0..1) → &HAABBGGRR (альфа инвертирована)."""
    r, g, b = rgb
    a = round((1.0 - max(0.0, min(1.0, opacity))) * 255)
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def ass_time(t: float) -> str:
    """Секунды → H:MM:SS.cs (сотые)."""
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    cs = int(round((t - int(t)) * 100))
    if cs == 100:  # округление вверх до целой секунды
        cs = 0
        s += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    """Экранировать текст для строки Dialogue."""
    return (text.replace("\\", "")
                .replace("{", "(").replace("}", ")")
                .replace("\r", " ").replace("\n", r"\N"))


# Пунктуация, которую убираем по краям слова (внутренние дефис/апостроф оставляем).
_EDGE_PUNCT = " ,.!?;:…«»\"'()—–-"


def _clean(text: str) -> str:
    """Слово без окаймляющей пунктуации (может стать пустым для токенов-знаков)."""
    return text.strip(_EDGE_PUNCT)


def _disp(text: str) -> str:
    """Подготовить слово к показу: снять окаймляющую пунктуацию + экранировать."""
    return ass_escape(_clean(text))


# --------------------------------------------------------------------------
# Заголовок + стиль
# --------------------------------------------------------------------------

def _header(style: CaptionStyle, canvas_w: int, canvas_h: int) -> str:
    border_style = 3 if style.box else 1
    outline_colour = ass_color(style.box_color, style.box_opacity) if style.box \
        else ass_color(style.outline_color, 1.0)
    back_colour = ass_color(style.box_color, style.box_opacity)
    primary = ass_color(style.primary, 1.0)
    secondary = ass_color(style.highlight, 1.0)  # для \k: «ещё не спетый» цвет
    bold = -1 if style.bold else 0

    # Alignment=5 (центр по обеим осям) — позицию задаём через \pos в событиях.
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {canvas_w}
PlayResY: {canvas_h}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{style.font_name},{style.font_size},{primary},{secondary},{outline_colour},{back_colour},{bold},0,0,0,100,100,0,0,{border_style},{style.outline_width},{style.shadow},5,40,40,60,204

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _dialogue(start: float, end: float, text: str) -> str:
    return f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Main,,0,0,0,,{text}\n"


# --------------------------------------------------------------------------
# Группировка слов во фразы (для Karaoke и переносов)
# --------------------------------------------------------------------------

def group_phrases(words: list, max_words: int = 4, max_gap: float = 0.6,
                  max_span: float = 2.5) -> list[list]:
    """Разбить слова на фразы по кол-ву слов / паузам / длительности."""
    phrases: list[list] = []
    cur: list = []
    for w in words:
        if cur:
            gap = w.start - cur[-1].end
            span = w.end - cur[0].start
            if len(cur) >= max_words or gap > max_gap or span > max_span:
                phrases.append(cur)
                cur = []
        cur.append(w)
    if cur:
        phrases.append(cur)
    return phrases


# --------------------------------------------------------------------------
# Анимации (реестр). Каждая возвращает список строк Dialogue.
# --------------------------------------------------------------------------

def _word_end(words: list, i: int, hold: float = 0.35, max_gap: float = 0.7) -> float:
    """Конец показа слова i.

    Если следующее слово близко (пауза ≤ max_gap) — держим слово непрерывно до него
    (без мельканий). Если дальше большая пауза — прячем слово после короткого удержания
    (не зависает на экране в тишине)."""
    w = words[i]
    if i + 1 < len(words):
        nxt = words[i + 1].start
        if nxt - w.end <= max_gap:
            return max(w.end, nxt)
        return w.end + hold
    return w.end + hold


def _cx(canvas_w: int) -> int:
    return canvas_w // 2


def _cy(style: CaptionStyle, canvas_h: int) -> int:
    return int(canvas_h * style.position_v)


def anim_pop(words, style, canvas_w, canvas_h) -> list[str]:
    """Слово появляется с «пружинным» увеличением (\\t fscx/fscy)."""
    cx, cy = _cx(canvas_w), _cy(style, canvas_h)
    out = []
    for i, w in enumerate(words):
        end = _word_end(words, i)
        tags = (f"{{\\an5\\pos({cx},{cy})"
                f"\\fscx55\\fscy55"
                f"\\t(0,110,\\fscx116\\fscy116)"
                f"\\t(110,200,\\fscx100\\fscy100)}}")
        out.append(_dialogue(w.start, end, tags + _disp(w.text)))
    return out


def anim_fade(words, style, canvas_w, canvas_h) -> list[str]:
    """Плавное проявление (\\fad)."""
    cx, cy = _cx(canvas_w), _cy(style, canvas_h)
    out = []
    for i, w in enumerate(words):
        end = _word_end(words, i)
        tags = f"{{\\an5\\pos({cx},{cy})\\fad(140,90)}}"
        out.append(_dialogue(w.start, end, tags + _disp(w.text)))
    return out


def anim_slide_up(words, style, canvas_w, canvas_h) -> list[str]:
    """Выезжает снизу вверх (\\move) с лёгким fade."""
    cx, cy = _cx(canvas_w), _cy(style, canvas_h)
    dy = 55
    out = []
    for i, w in enumerate(words):
        end = _word_end(words, i)
        tags = (f"{{\\an5\\fad(120,70)"
                f"\\move({cx},{cy + dy},{cx},{cy},0,160)}}")
        out.append(_dialogue(w.start, end, tags + _disp(w.text)))
    return out


def anim_karaoke(words, style, canvas_w, canvas_h) -> list[str]:
    """Фраза целиком; текущее слово подсвечивается (\\k)."""
    cx, cy = _cx(canvas_w), _cy(style, canvas_h)
    out = []
    for phrase in group_phrases(words):
        p_start = phrase[0].start
        p_end = _word_end(words, words.index(phrase[-1]))
        parts = [f"{{\\an5\\pos({cx},{cy})}}"]
        for j, w in enumerate(phrase):
            # Длительность слога в сотых секунды.
            nxt = phrase[j + 1].start if j + 1 < len(phrase) else w.end
            k = max(1, int(round((nxt - w.start) * 100)))
            parts.append(f"{{\\k{k}}}{_disp(w.text)} ")
        out.append(_dialogue(p_start, p_end, "".join(parts).rstrip()))
    return out


AnimationFn = Callable[[list, CaptionStyle, int, int], list[str]]

ANIMATIONS: dict[CaptionAnimation, AnimationFn] = {
    CaptionAnimation.POP: anim_pop,
    CaptionAnimation.FADE: anim_fade,
    CaptionAnimation.SLIDE_UP: anim_slide_up,
    CaptionAnimation.KARAOKE: anim_karaoke,
}


# --------------------------------------------------------------------------
# Сборка .ass
# --------------------------------------------------------------------------

def build_ass(words: list, style: Optional[CaptionStyle] = None,
              animation: CaptionAnimation = CaptionAnimation.POP,
              canvas_w: int = 1080, canvas_h: int = 1920) -> str:
    """Построить полный текст .ass из списка слов (объекты .text/.start/.end)."""
    style = style or CaptionStyle()
    # Отбрасываем пустые и чисто-пунктуационные токены (Whisper иногда выдаёт «,»).
    words = [w for w in words if _clean(w.text or "")]
    fn = ANIMATIONS[animation]
    body = fn(words, style, canvas_w, canvas_h)
    return _header(style, canvas_w, canvas_h) + "".join(body)


def write_ass(path: str, words: list, style: Optional[CaptionStyle] = None,
              animation: CaptionAnimation = CaptionAnimation.POP,
              canvas_w: int = 1080, canvas_h: int = 1920) -> str:
    text = build_ass(words, style, animation, canvas_w, canvas_h)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # ВАЖНО: .ass в UTF-8 (с BOM libass тоже ок, но пишем без BOM).
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    log.info("Субтитры .ass записаны: %s (%d слов, анимация=%s)",
             path, len(words), animation.value)
    return path


# --------------------------------------------------------------------------
# CLI: собрать .ass из транскрипта и (опц.) прожечь на видео для проверки
# --------------------------------------------------------------------------

def _main() -> None:
    import argparse
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    ap = argparse.ArgumentParser(description="Генерация .ass субтитров")
    ap.add_argument("transcript_json", help="JSON от core.transcribe")
    ap.add_argument("--anim", choices=[a.value for a in CaptionAnimation], default="pop")
    ap.add_argument("--out", default="out/subs.ass")
    ap.add_argument("--box", action="store_true", help="фон-плашка под текстом")
    ap.add_argument("--burn", help="видео для прожига субтитров (тест libass/кириллицы)")
    ap.add_argument("--burn-out", default="out/captioned.mp4")
    args = ap.parse_args()

    class _W:
        def __init__(self, d):
            self.text = d["text"]; self.start = d["start"]; self.end = d["end"]

    with open(args.transcript_json, encoding="utf-8") as f:
        data = json.load(f)
    words = [_W(d) for d in data["words"]]

    style = CaptionStyle(box=args.box)
    write_ass(args.out, words, style=style, animation=CaptionAnimation(args.anim))

    if args.burn:
        from . import ffmpeg_utils as ff
        # Простая вертикаль для теста: вписать 16:9 по ширине + чёрные поля, прожечь .ass.
        ass_path = args.out.replace("\\", "/")
        vf = (f"scale=1080:-2,pad=1080:1920:0:(1920-ih)/2:black,"
              f"ass={ass_path}:fontsdir=assets/fonts")
        codec = ["-c:v", "h264_nvenc", "-preset", "p5", "-b:v", "8M"] if ff.nvenc_available() \
            else ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
        args_ff = ["-y", "-i", args.burn, "-vf", vf, "-c:a", "aac", "-b:a", "192k",
                   *codec, "-movflags", "+faststart", args.burn_out]
        ff.run_ffmpeg(args_ff)
        log.info("Прожжено: %s", args.burn_out)


if __name__ == "__main__":
    _main()
