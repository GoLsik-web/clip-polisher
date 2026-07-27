"""scanner.py — Этап 3: «вставил ссылку → получил моменты».

Здесь связывается всё, что сделано порознь:

    ссылка → запись (VOD) → клипы зрителей (twitch_clips)
                          → метки и пульс чата с этого же эфира (если бот их писал)
                          → сигналы (clipscan) → очки → 2-8 моментов → .clipscan

Никакого видео пока не качается — это ровно то, ради чего затевалась «работа по ссылке
без скачивания стрима». Звук/речь/лицо добавят блоки 2–4: они просто досыпят сигналы
в тот же файл.

Как сшиваются метки бота и запись: у VOD есть `stream_id` — это тот же id эфира, что
бот кладёт в имя и в тело файла меток. Совпал — значит, это данные ИМЕННО этого стрима.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.chatpulse import PulseLog, pulse_path_for
from core.clipscan import (ScanFile, Signal, build_scan, candidates, signals_from_clips,
                           signals_from_marks, signals_from_pulse)
from core.marks import MarksFile
from core.speech import SpeechPlan, apply_names, signals_from_speech
from core.twitch_clips import (HttpGet, Source, TwitchError, VodInfo, clips_for_vod,
                               expired_clip_count, fetch_last_vods, fetch_vod,
                               parse_source)

Progress = Callable[[str], None]
# Спросить человека перед долгим распознаванием речи: вернуть план (можно изменённый)
# или None, чтобы речь пропустить.
PlanCb = Callable[[SpeechPlan], Optional[SpeechPlan]]


@dataclass
class LocalData:
    """Что нашлось на диске от нашего же бота по этому эфиру."""
    marks: Optional[MarksFile] = None
    pulse: Optional[PulseLog] = None
    marks_path: str = ""
    pulse_path: str = ""


def find_local_data(vod: VodInfo, marks_dir: str = "") -> LocalData:
    """Найти метки и журнал пульса ЭТОГО эфира (по id эфира, иначе по каналу и дате)."""
    if not marks_dir:
        try:
            from core.chatbot import default_marks_dir
            marks_dir = default_marks_dir()
        except OSError:
            return LocalData()
    if not os.path.isdir(marks_dir):
        return LocalData()

    best = ""
    for path in sorted(glob.glob(os.path.join(marks_dir, "*.clipmarks"))):
        try:
            mf = MarksFile.from_json(path)
        except (OSError, ValueError):
            continue
        if vod.broadcast_id and mf.broadcast_id == vod.broadcast_id:
            best = path
            break
        # Запасной путь: тот же канал и файл длиннее прочих (id эфира мог не записаться).
        if not best and vod.channel and mf.streamer.lower() == vod.channel.lower():
            best = path
    if not best:
        return LocalData()

    out = LocalData(marks_path=best)
    try:
        out.marks = MarksFile.from_json(best)
    except (OSError, ValueError):
        out.marks = None
    ppath = pulse_path_for(best)
    if os.path.isfile(ppath):
        try:
            out.pulse = PulseLog.from_json(ppath)
            out.pulse_path = ppath
        except (OSError, ValueError):
            out.pulse = None
    return out


@dataclass
class ScanResult:
    """Итог разбора: файл разбора + запись + что стоит честно сказать пользователю."""
    scan: ScanFile
    vod: VodInfo
    local: LocalData = field(default_factory=LocalData)
    speech: list = field(default_factory=list)      # что услышали в речи кандидатов
    audio_source: object = None                     # откуда брали звук (или None)

    @property
    def moments(self):
        return self.scan.moments

    @property
    def notes(self) -> list[str]:
        return self.scan.notes


def resolve_vod(token: str, source: Source, http: Optional[HttpGet] = None,
                progress: Optional[Progress] = None) -> VodInfo:
    """Ссылка → конкретная запись. Для канала берётся последний доступный стрим."""
    say = progress or (lambda _s: None)
    if source.kind == "vod":
        say("Спрашиваю Twitch про запись…")
        return fetch_vod(token, source.vod_id, http=http)
    say(f"Ищу последний стрим канала {source.channel}…")
    return fetch_last_vods(token, source.channel, limit=1, http=http)[0]


def scan_link(link: str, token: str, strictness: float = 50.0,
              http: Optional[HttpGet] = None, marks_dir: str = "",
              use_local: bool = True, composition=None,
              audio: bool = True, speech: str = "auto", video_path: str = "",
              should_stop: Optional[Callable[[], bool]] = None,
              on_plan: Optional[PlanCb] = None,
              progress: Optional[Progress] = None) -> ScanResult:
    """Разбор стрима по ссылке — ДВА ПРОХОДА (блоки 1-3).

    1) быстрый: клипы зрителей + метки бота + пульс чата + громкость/смех по всей
       записи → кандидаты;
    2) глубокий: речь ТОЛЬКО в окнах кандидатов → эмоции и названия моментов.

    Параметры:
      `audio`   — слушать ли звук (блок 2). Выключить = как было в блоке 1.
      `speech`  — 'auto' | 'large-v3' | 'medium' | 'small' | 'base' | 'skip' (блок 3).
      `video_path` — локальная запись: если она есть, звук берём из неё и НИЧЕГО не качаем.
      `on_plan` — спросить человека перед долгим распознаванием речи (см. `SpeechPlan`).
      `composition` — раскладка: нужна будущему сигналу «лицо» (вебка есть не у всех).
    """
    say = progress or (lambda _s: None)
    stop = should_stop or (lambda: False)
    source = parse_source(link)
    vod = resolve_vod(token, source, http=http, progress=say)

    say("Спрашиваю Twitch, какие клипы нарезали зрители…")
    clips = clips_for_vod(token, vod, http=http)

    notes: list[str] = []
    signals: list[Signal] = list(signals_from_clips(clips))
    if clips:
        say(f"Клипов зрителей: {len(clips)}")
    else:
        notes.append("Зрители не нарезали ни одного клипа из этой записи — "
                     "по клипам искать нечего.")

    local = find_local_data(vod, marks_dir) if use_local else LocalData()
    if local.marks and local.marks.marks:
        signals += signals_from_marks(local.marks)
        say(f"Нашёл метки бота с этого эфира: {len(local.marks.marks)}")
    if local.pulse and local.pulse.buckets:
        chat_signals = signals_from_pulse(local.pulse)
        signals += chat_signals
        say(f"Журнал чата: {len(local.pulse.buckets)} корзин, "
            f"взрывов чата: {len(chat_signals)}")
    elif not local.marks:
        notes.append("Журнала чата с этого эфира нет (бот тогда не работал или это "
                     "чужой канал) — сигнал «взрыв чата» не участвует.")

    expired = expired_clip_count(clips)
    if expired:
        notes.append(f"{expired} клипов пропущено: у них истекла запись.")

    # ---- блок 2: звук (работает даже там, где нет ни клипов, ни чата) ----
    audio_src = None
    if audio and not stop():
        audio_src, audio_notes, audio_signals = _listen(
            vod=vod, video_path=video_path, progress=say)
        signals += audio_signals
        notes += audio_notes

    # ---- блок 3: речь ТОЛЬКО по кандидатам ----
    speech_results: list = []
    if audio_src is not None and speech not in ("off", "none") and not stop():
        speech_results, speech_notes = _listen_speech(
            audio_src, signals, duration=vod.duration, strictness=strictness,
            model=speech, on_plan=on_plan, should_stop=stop, progress=say)
        signals += signals_from_speech(speech_results)
        notes += speech_notes

    # Честно сказать, когда искать было НЕ ПО ЧЕМУ. Иначе «нашёлся один момент»
    # выглядит поломкой, хотя программа отработала верно: улика была ровно одна.
    if len(signals) <= 2:
        have = []
        if clips:
            have.append(f"клипов зрителей: {len(clips)}")
        if local.marks and local.marks.marks:
            have.append(f"меток бота: {len(local.marks.marks)}")
        if local.pulse and local.pulse.buckets:
            have.append("журнал чата есть")
        what = "; ".join(have) if have else "улик нет вовсе"
        tail = ("" if audio else
                " Включи разбор звука — крик, смех и тишина→взрыв есть на любом стриме.")
        notes.append(
            f"Искать почти не по чему ({what}). Это не сбой: улик на эту запись "
            f"существует ровно столько." + tail)
    if not local.pulse and not (local.marks and local.marks.marks):
        notes.append("Совет: включи бота на своих эфирах — он ведёт журнал чата, и "
                     "следующие стримы будут разбираться заметно точнее.")

    # Записываем в разбор, была ли вообще вебка: блок 4 (лицо) по этому полю поймёт,
    # что сигнала «лицо» здесь не будет в принципе, и не станет его ждать.
    from core.clipscan import face_available
    has_face = face_available(composition)

    source_info = {"platform": "twitch", "channel": vod.channel, "vod_id": vod.id,
                   "url": vod.url or f"https://www.twitch.tv/videos/{vod.id}",
                   "title": vod.title, "duration": vod.duration,
                   "started_at": vod.created_at, "broadcast_id": vod.broadcast_id,
                   "clips": len(clips), "face_source": has_face,
                   "audio": bool(audio_src), "speech": len(speech_results)}
    scan = build_scan(source_info, signals, strictness=strictness, notes=notes)
    if speech_results:
        named = apply_names(scan.moments, speech_results)
        if named:
            say(f"Названий из речи: {named}")
    return ScanResult(scan=scan, vod=vod, local=local, speech=speech_results,
                      audio_source=audio_src)


# --------------------------------------------------------------------------
# Проход 1: звук
# --------------------------------------------------------------------------

def _listen(vod: VodInfo, video_path: str = "",
            progress: Optional[Progress] = None) -> tuple:
    """Достать звук (файл или загрузка) и услышать громкость/смех. Не роняет разбор."""
    from core import media, sound_events
    say = progress or (lambda _s: None)
    notes: list[str] = []
    try:
        if video_path:
            src = media.audio_from_file(video_path, progress=say)
        else:
            src = media.audio_from_vod(vod.id, progress=say,
                                       url=vod.url or "")
    except media.MediaError as e:
        notes.append(f"Звук разобрать не вышло: {e}")
        return None, notes, []

    try:
        analysis = sound_events.analyze(src, progress=say)
    except Exception as e:                       # noqa: BLE001 — звук не рушит остальное
        notes.append(f"Разбор звука не удался: {e}")
        return src, notes, []

    if analysis.note:
        notes.append(analysis.note)
    signals = sound_events.signals_from_audio(analysis)
    laughs = len(analysis.of_kind(sound_events.SoundKind.LAUGH))
    say(f"Звук разобран: {len(signals)} зацепок"
        + (f", из них смех — {laughs}" if analysis.model_used else ""))
    return src, notes, signals


# --------------------------------------------------------------------------
# Проход 2: речь по кандидатам
# --------------------------------------------------------------------------

def _listen_speech(src, signals: list[Signal], duration: Optional[float],
                   strictness: float, model: str = "auto",
                   max_windows: int = 30, pad: float = 5.0,
                   on_plan: Optional[PlanCb] = None,
                   should_stop: Optional[Callable[[], bool]] = None,
                   progress: Optional[Progress] = None) -> tuple:
    """Расшифровать речь ТОЛЬКО там, где уже есть подозрение на момент."""
    from core import speech as sp
    say = progress or (lambda _s: None)
    notes: list[str] = []

    # Кандидаты по всем уликам, что набрались к этому моменту.
    cands = candidates(signals, duration=duration)
    if not cands:
        return [], notes
    top = sorted(cands, key=lambda m: m.score, reverse=True)[:max_windows]
    top.sort(key=lambda m: m.start)
    windows = [(max(0.0, m.start - pad), m.end + pad) for m in top]

    speech_plan = sp.plan(windows, model=model)
    say(speech_plan.human())
    if speech_plan.advice():
        say(speech_plan.advice())
    if on_plan is not None:
        decided = on_plan(speech_plan)          # спросить человека: ждать/сменить/пропустить
        if decided is None:
            notes.append("Речь не распознавали — так решил пользователь.")
            return [], notes
        speech_plan = decided
    if speech_plan.skip:
        notes.append("Речь не распознавали: названия моментов будут без цитат.")
        return [], notes

    results = sp.transcribe_windows(src.path, windows, speech_plan,
                                    progress=say, should_stop=should_stop)
    if not results:
        notes.append("Речь распознать не вышло — моменты остались без цитат.")
    elif len(results) < len(windows):
        notes.append(f"Речь разобрана не везде ({len(results)} из {len(windows)} мест).")
    return results, notes


def scan_file(video_path: str, strictness: float = 50.0, marks_path: str = "",
              speech: str = "auto", composition=None,
              should_stop: Optional[Callable[[], bool]] = None,
              on_plan: Optional[PlanCb] = None,
              progress: Optional[Progress] = None) -> ScanResult:
    """Разбор ЛОКАЛЬНОЙ записи — без Twitch и без единого байта из сети.

    Клипов зрителей и пульса чата тут нет (это данные Twitch), зато звук и речь
    работают полностью. Если рядом есть файл меток от бота — его тоже подмешиваем.
    """
    say = progress or (lambda _s: None)
    stop = should_stop or (lambda: False)

    local = LocalData()
    signals: list[Signal] = []
    notes: list[str] = []
    if marks_path and os.path.isfile(marks_path):
        try:
            local.marks = MarksFile.from_json(marks_path)
            local.marks_path = marks_path
            signals += signals_from_marks(local.marks)
            say(f"Метки из файла: {len(local.marks.marks)}")
        except (OSError, ValueError) as e:
            notes.append(f"Файл меток не прочитался: {e}")
        ppath = pulse_path_for(marks_path)
        if os.path.isfile(ppath):
            try:
                local.pulse = PulseLog.from_json(ppath)
                local.pulse_path = ppath
                signals += signals_from_pulse(local.pulse)
            except (OSError, ValueError):
                local.pulse = None

    # Псевдо-«запись»: так дальше всё работает теми же путями, что и по ссылке.
    vod = VodInfo(id="", channel=(local.marks.streamer if local.marks else ""),
                  title=os.path.basename(video_path))

    src, audio_notes, audio_signals = _listen(vod=vod, video_path=video_path, progress=say)
    if src is None:
        from core.media import MediaError
        raise MediaError(f"Не удалось прочитать звук записи: {video_path}")
    vod.duration = src.duration
    signals += audio_signals
    notes += audio_notes

    speech_results: list = []
    if speech not in ("off", "none") and not stop():
        speech_results, speech_notes = _listen_speech(
            src, signals, duration=src.duration, strictness=strictness,
            model=speech, on_plan=on_plan, should_stop=stop, progress=say)
        signals += signals_from_speech(speech_results)
        notes += speech_notes

    if not local.marks:
        notes.append("Это локальная запись: клипов зрителей и чата у неё нет — "
                     "разбор идёт по звуку и речи. Файл меток можно подложить рядом.")

    from core.clipscan import face_available
    source_info = {"platform": "file", "channel": vod.channel, "vod_id": "",
                   "url": "", "title": vod.title, "duration": src.duration,
                   "started_at": "", "broadcast_id": "", "clips": 0,
                   "face_source": face_available(composition),
                   "audio": True, "speech": len(speech_results), "path": video_path}
    scan = build_scan(source_info, signals, strictness=strictness, notes=notes)
    if speech_results:
        apply_names(scan.moments, speech_results)
    return ScanResult(scan=scan, vod=vod, local=local, speech=speech_results,
                      audio_source=src)


def default_scan_path(vod: VodInfo) -> str:
    """Куда класть разбор: рядом с метками, имя — канал и id записи."""
    from core.chatbot import default_marks_dir
    if not vod.id:                       # локальная запись — имя по файлу
        stem = os.path.splitext(os.path.basename(vod.title or "запись"))[0]
        return os.path.join(default_marks_dir(), f"{stem}.clipscan")
    return os.path.join(default_marks_dir(), f"{vod.channel}_{vod.id}.clipscan")


__all__ = ["LocalData", "ScanResult", "TwitchError", "find_local_data", "resolve_vod",
           "scan_link", "scan_file", "default_scan_path"]
