"""chatbot.py — движок бота меток для Twitch-чата (ядро, без Qt).

Один и тот же движок используют:
  * приложение (вкладка «Бот») — через `BotService` с колбэком событий;
  * командная строка (`bot/twitch_marks_bot.py`) — тонкая обёртка вокруг этого модуля.

Что делает: сидит в чате канала, ловит `!clip` / `!клип` / `!метка` (можно с заметкой),
определяет роль автора по IRC-тегам Twitch (стример/модер/вип/зритель), режет спам
зрителей кулдауном, пишет файл `.clipmarks` (формат — `core.marks`), отвечает в чат.
Онлайн канала и момент старта эфира берёт из Helix пользовательским токеном.

Зависимостей нет — только стандартная библиотека (socket/ssl/threading/urllib).

История: раньше это лежало в `bot/marks_collector.py` + `bot/twitch_marks_bot.py`.
Переехало в `core/`, чтобы бот работал ВНУТРИ приложения (и попадал в .exe), а
`bot/*.py` остались обёртками для запуска из консоли.
"""
from __future__ import annotations

import datetime
import os
import socket
import ssl
import threading
import time
from typing import Callable, Optional

from core.banter import Banter, DEFAULT_MODE, build_report, is_report_command
from core.chatpulse import PulseCollector, pulse_path_for
from core.marks import Mark, MarksFile, AuthorType

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6697          # SSL
DEFAULT_COMMANDS = ["!clip", "!клип", "!метка"]

# Таймаут сокета ПОСЛЕ подключения. Чат может молчать часами, а Twitch пингует раз в
# ~5 минут — с коротким таймаутом бот отваливался на тишине и терял сообщения.
IDLE_TIMEOUT = 360.0


# ==========================================================================
# Роли и сбор меток (чистая логика, тестируется без сети)
# ==========================================================================

def role_from_tags(login: str, channel: str, tags: dict) -> AuthorType:
    """Роль автора сообщения по IRC-тегам Twitch."""
    badges = tags.get("badges", "") or ""
    if login.lower() == channel.lower() or "broadcaster/1" in badges:
        return AuthorType.STREAMER
    if tags.get("mod") == "1" or "moderator/1" in badges:
        return AuthorType.MODERATOR
    if tags.get("vip") == "1" or "vip/1" in badges:
        return AuthorType.VIP
    return AuthorType.VIEWER


class MarkCollector:
    """Копит метки и пишет `.clipmarks`. Время метки — секунды от «нуля» (ref_epoch)."""

    def __init__(self, channel: str, output_path: str,
                 commands: Optional[list[str]] = None,
                 who_can_mark: str = "all",         # 'all' | 'trusted'
                 viewer_cooldown_sec: float = 30.0,
                 ref_epoch: Optional[float] = None,
                 broadcast_id: str = "", title: str = "", game: str = ""):
        self.channel = channel
        self.output_path = output_path
        self.commands = [c.lower() for c in (commands or DEFAULT_COMMANDS)]
        self.who_can_mark = who_can_mark
        self.viewer_cooldown_sec = viewer_cooldown_sec
        self.ref_epoch = ref_epoch if ref_epoch is not None else time.time()
        self.online: Optional[int] = None
        self.broadcast_id = broadcast_id
        self.title = title
        self.game = game
        self.duration: Optional[float] = None
        self.marks: list[Mark] = []
        self._last_by_user: dict[str, float] = {}
        self._started_iso = _iso(self.ref_epoch)

    # ---- онлайн / точка отсчёта ----
    def set_online(self, n: Optional[int]) -> None:
        self.online = n

    def set_reference(self, epoch: float) -> None:
        """Задать «ноль» времени (например, момент выхода в эфир из Twitch API)."""
        self.ref_epoch = epoch
        self._started_iso = _iso(epoch)

    def set_stream_info(self, broadcast_id: str = "", title: str = "", game: str = "") -> None:
        """Чей это эфир: id/название/игра — чтобы потом не путать записи между собой."""
        self.broadcast_id = broadcast_id or self.broadcast_id
        self.title = title or self.title
        self.game = game or self.game

    def resume_existing(self) -> int:
        """Подхватить метки из уже существующего файла этого же эфира.

        Нужно, когда программу перезапустили посреди стрима: файл эфира тот же, и
        начинать его с нуля нельзя — иначе утренние метки затрутся (так и было).
        """
        if not os.path.isfile(self.output_path):
            return 0
        try:
            old = MarksFile.from_json(self.output_path)
        except (OSError, ValueError):
            return 0
        self.marks = list(old.marks) + self.marks
        return len(old.marks)

    # ---- обработка сообщения ----
    def parse_command(self, text: str) -> Optional[str]:
        """Если текст — команда метки, вернуть заметку (может быть пустой), иначе None."""
        t = text.strip()
        low = t.lower()
        for cmd in self.commands:
            if low == cmd:
                return ""
            if low.startswith(cmd + " "):
                return t[len(cmd):].strip()
        return None

    def handle_message(self, login: str, tags: dict, text: str,
                       now: Optional[float] = None) -> Optional[str]:
        """Обработать сообщение чата. Вернуть текст ответа в чат (или None)."""
        note = self.parse_command(text)
        if note is None:
            return None
        now = time.time() if now is None else now
        role = role_from_tags(login, self.channel, tags)

        # Кто может метить.
        if self.who_can_mark == "trusted" and role == AuthorType.VIEWER:
            return None

        # Антиспам: кулдаун для ЗРИТЕЛЕЙ (доверенных не режем).
        if role == AuthorType.VIEWER:
            last = self._last_by_user.get(login.lower())
            if last is not None and (now - last) < self.viewer_cooldown_sec:
                return None
            self._last_by_user[login.lower()] = now

        t = max(0.0, now - self.ref_epoch)
        author = tags.get("display-name") or login
        self.marks.append(Mark(t=round(t, 2), type=role,
                               author=author, note=(note or "")[:80]))
        self.save()
        return self._confirm(role, note)

    def _confirm(self, role: AuthorType, note: str) -> str:
        n = len(self.marks)
        extra = f" «{note}»" if note else ""
        return f"✓ метка #{n} записана{extra}"

    # ---- запись файла (атомарно) ----
    def _as_file(self) -> MarksFile:
        return MarksFile(platform="twitch", streamer=self.channel,
                         broadcast_id=self.broadcast_id, started_at=self._started_iso,
                         duration=self.duration, online=self.online,
                         title=self.title, game=self.game, marks=list(self.marks))

    def save(self) -> None:
        path = self.output_path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        self._as_file().to_json(tmp)
        os.replace(tmp, path)   # атомарно — файл всегда целый, даже если бот упадёт

    def finalize(self, end_epoch: Optional[float] = None) -> None:
        """Закрыть файл: дописать длительность эфира (нужна для сверки с записью)."""
        end = end_epoch if end_epoch is not None else time.time()
        self.duration = max(0.0, round(end - self.ref_epoch, 1)) or None
        self.save()


def _iso(epoch: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""   # started_at — необязательно; не роняем бота из-за экзотического времени


# ==========================================================================
# IRC
# ==========================================================================

def parse_irc(line: str):
    """Разобрать строку IRC → (tags, login, command, params, trailing)."""
    tags: dict = {}
    if line.startswith("@"):
        tagstr, line = line[1:].split(" ", 1)
        for kv in tagstr.split(";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                tags[k] = v
            else:
                tags[kv] = ""
    prefix = ""
    if line.startswith(":"):
        prefix, line = line[1:].split(" ", 1)
    if " :" in line:
        head, trailing = line.split(" :", 1)
    else:
        head, trailing = line, ""
    parts = head.split(" ")
    command = parts[0] if parts else ""
    params = parts[1:]
    login = prefix.split("!", 1)[0] if prefix else ""
    return tags, login, command, params, trailing


class AuthFailed(RuntimeError):
    """Twitch не пустил в чат (токен протух/без нужных прав) — переподключаться бесполезно."""


class TwitchChat:
    """Одно соединение с чатом. Живёт до обрыва/stop()."""

    def __init__(self, channel: str, nick: str, token: str,
                 on_message: Callable[[dict, str, str], None],
                 on_log: Optional[Callable[[str], None]] = None,
                 reply_in_chat: bool = True):
        self.channel = channel.lower()
        self.nick = nick.lower()
        self.token = token
        self.on_message = on_message
        self.on_log = on_log or (lambda _s: None)
        self.reply_in_chat = reply_in_chat
        self.sock: Optional[ssl.SSLSocket] = None
        self._buf = ""
        self._stop = threading.Event()

    # ---- соединение ----
    def connect(self) -> None:
        raw = socket.create_connection((IRC_HOST, IRC_PORT), timeout=20)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=IRC_HOST)
        self.sock.settimeout(IDLE_TIMEOUT)
        token = self.token if self.token.startswith("oauth:") else "oauth:" + self.token
        self._send_raw(f"PASS {token}")
        self._send_raw(f"NICK {self.nick}")
        self._send_raw("CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership")
        self._send_raw(f"JOIN #{self.channel}")

    def _send_raw(self, line: str) -> None:
        if self.sock:
            self.sock.sendall((line + "\r\n").encode("utf-8"))

    def reply(self, text: str) -> None:
        if self.reply_in_chat:
            try:
                self._send_raw(f"PRIVMSG #{self.channel} :{text}")
            except OSError as e:
                self.on_log(f"не смог ответить в чат: {e}")

    def stop(self) -> None:
        """Разбудить recv и закрыть соединение (можно звать из другого потока)."""
        self._stop.set()
        try:
            if self.sock:
                self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    # ---- цикл ----
    def run(self) -> None:
        self.connect()
        while not self._stop.is_set():
            try:
                data = self.sock.recv(4096)
            except (socket.timeout, TimeoutError):
                self._send_raw("PING :tmi.twitch.tv")   # тишина дольше обычного — проверим связь
                continue
            except OSError:
                if self._stop.is_set():
                    return                              # это мы сами закрыли сокет
                raise
            if not data:
                if self._stop.is_set():
                    return
                raise ConnectionError("соединение закрыто")
            self._buf += data.decode("utf-8", errors="ignore")
            while "\r\n" in self._buf:
                line, self._buf = self._buf.split("\r\n", 1)
                self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        if line.startswith("PING"):
            self._send_raw("PONG :tmi.twitch.tv")
            return
        tags, login, command, _params, trailing = parse_irc(line)
        if command == "PRIVMSG":
            self.on_message(tags, login, trailing)
        elif command == "NOTICE":
            low = trailing.lower()
            if "authentication failed" in low or "improperly formatted auth" in low:
                raise AuthFailed(trailing)
            self.on_log("NOTICE: " + trailing)
        elif command == "001":
            self.on_log("вошли в чат Twitch")


# ==========================================================================
# Сервис: чат + онлайн + переподключение (то, что дёргает приложение)
# ==========================================================================

def default_marks_dir() -> str:
    """Куда писать метки: рядом с настройками пользователя, а не в папку программы."""
    from core.provision import app_data_dir
    d = os.path.join(app_data_dir(), "marks")
    os.makedirs(d, exist_ok=True)
    return d


def session_output(channel: str, started_epoch: Optional[float] = None,
                   broadcast_id: str = "") -> str:
    """Имя файла ОДНОГО эфира: канал + дата + время начала + id эфира.

    Раньше имя было только по дате — второй стрим за день писал в тот же файл и
    затирал метки первого. Теперь у каждого эфира свой файл, затирать нечего.
    """
    ep = started_epoch if started_epoch is not None else time.time()
    stamp = datetime.datetime.fromtimestamp(ep).strftime("%Y-%m-%d_%H-%M")
    tail = broadcast_id if broadcast_id else "вне-эфира"
    return os.path.join(default_marks_dir(), f"{channel}_{stamp}_{tail}.clipmarks")


def default_output(channel: str) -> str:
    """Совместимость со старым кодом (консоль): файл текущей сессии."""
    return session_output(channel)


# ==========================================================================
# Наблюдение за эфиром (нужно и для автопилота, и для «нуля времени»)
# ==========================================================================

class StreamInfo:
    """Что Twitch говорит про эфир канала прямо сейчас."""

    def __init__(self, live: bool = False, broadcast_id: str = "", title: str = "",
                 game: str = "", started_at: str = "", viewers: Optional[int] = None):
        self.live = live
        self.broadcast_id = broadcast_id
        self.title = title
        self.game = game
        self.started_at = started_at
        self.viewers = viewers

    @property
    def started_epoch(self) -> Optional[float]:
        return _iso_to_epoch(self.started_at) if self.started_at else None

    @classmethod
    def from_helix(cls, d: Optional[dict]) -> "StreamInfo":
        if not d:
            return cls(live=False)
        return cls(live=True, broadcast_id=str(d.get("id", "")),
                   title=d.get("title", ""), game=d.get("game_name", ""),
                   started_at=d.get("started_at", ""),
                   viewers=int(d.get("viewer_count", 0)))


class StreamWatcher(threading.Thread):
    """Раз в минуту спрашивает Twitch, идёт ли эфир, и зовёт колбэк.

    Короткие «провалы» ответа сглаживаем: эфир считается законченным только если
    Twitch говорит «офлайн» дольше `offline_grace` (иначе моргание сети выключало бы
    бота посреди стрима).
    """

    def __init__(self, channel: str, token: str, on_change: Callable[[StreamInfo], None],
                 period: float = 60.0, offline_grace: float = 180.0):
        super().__init__(daemon=True, name="clipbot-watch")
        self.channel = channel
        self.token = token
        self.on_change = on_change
        self.period = period
        self.offline_grace = offline_grace
        self._stop = threading.Event()
        self._last_live_at: Optional[float] = None
        self.info = StreamInfo(live=False)

    def run(self) -> None:
        from core.twitch_auth import helix_stream
        while not self._stop.is_set():
            raw = StreamInfo.from_helix(helix_stream(self.token, self.channel))
            now = time.time()
            if raw.live:
                self._last_live_at = now
                self.info = raw
            elif self._last_live_at and (now - self._last_live_at) < self.offline_grace:
                pass                      # моргнуло — считаем, что эфир ещё идёт
            else:
                self.info = raw           # офлайн подтверждён
            try:
                self.on_change(self.info)
            except Exception:             # noqa: BLE001 — колбэк не должен ронять поток
                pass
            if self._stop.wait(self.period):
                return

    def stop(self) -> None:
        self._stop.set()


class BotService:
    """Бот в фоновых потоках. Все события — через колбэк `on_event(kind, payload)`.

    kind: 'status' (текст состояния) | 'mark' (метка) | 'online' | 'session'
          (начался/сменился эфир — новый файл) | 'error' (фатально) | 'log'.

    Два режима:
      * обычный — заходит в чат сразу (метки можно ставить и вне эфира);
      * автопилот — ждёт начала эфира, заходит в чат сам, а после конца эфира
        выходит и закрывает файл меток.

    Про смешивание эфиров: КАЖДЫЙ эфир — своя «сессия» со своим файлом (имя с датой,
    временем и id эфира) и своим нулём времени (= момент выхода в эфир). Если посреди
    стрима перезапустить программу, сессия продолжится в тот же файл, а не затрёт его.
    """

    def __init__(self, channel: str, token: str, nick: str = "",
                 output_path: str = "", commands: Optional[list[str]] = None,
                 who_can_mark: str = "all", viewer_cooldown_sec: float = 30.0,
                 reply_in_chat: bool = True, use_stream_start_as_zero: bool = True,
                 autopilot: bool = False, watch_period: float = 60.0,
                 write_pulse: bool = True, banter_mode: str = DEFAULT_MODE,
                 banter_period_min: float = 12.0, greet_newcomers: bool = True,
                 react_to_hype: bool = True, banter: Optional[Banter] = None,
                 tick_period: float = 5.0,
                 on_event: Optional[Callable[[str, dict], None]] = None):
        self.channel = channel.strip().lstrip("#").lower()
        self.token = token
        self.nick = (nick or self.channel).lower()
        self.fixed_output = output_path          # задан снаружи (консоль/тесты) — не ротируем
        self.use_stream_start_as_zero = use_stream_start_as_zero
        self.reply_in_chat = reply_in_chat
        self.autopilot = autopilot
        self.watch_period = watch_period
        self.write_pulse = write_pulse
        self.tick_period = tick_period
        self.on_event = on_event or (lambda _k, _p: None)
        self.banter = banter or Banter(mode=banter_mode, period_min=banter_period_min,
                                       greet_newcomers=greet_newcomers,
                                       react_to_hype=react_to_hype)
        self._collector_opts = {"commands": commands, "who_can_mark": who_can_mark,
                                "viewer_cooldown_sec": viewer_cooldown_sec}
        self.collector = self._new_collector(StreamInfo(live=False))
        self.pulse = self._new_pulse(self.collector)
        self.stream = StreamInfo(live=False)
        self._stop = threading.Event()
        self._live = threading.Event()           # идёт ли эфир (для автопилота)
        self._chat: Optional[TwitchChat] = None
        self._thread: Optional[threading.Thread] = None
        self._ticker: Optional[threading.Thread] = None
        self._watcher: Optional[StreamWatcher] = None
        self._lock = threading.Lock()
        self._last_report = 0.0

    # ---- сессии эфиров ----
    def _new_collector(self, info: StreamInfo) -> MarkCollector:
        """Свой файл и свой ноль времени на каждый эфир."""
        if info.live and self.use_stream_start_as_zero and info.started_epoch:
            ref = info.started_epoch
        else:
            # Twitch не сказал время старта — не сдвигаем отсчёт, если он уже был
            # (иначе метки этой же сессии разъехались бы по времени).
            prev = getattr(self, "collector", None)
            ref = prev.ref_epoch if prev is not None else time.time()
        path = self.fixed_output or session_output(self.channel, ref, info.broadcast_id)
        return MarkCollector(channel=self.channel, output_path=path, ref_epoch=ref,
                             broadcast_id=info.broadcast_id, title=info.title,
                             game=info.game, **self._collector_opts)

    def _new_pulse(self, col: MarkCollector) -> Optional[PulseCollector]:
        """Журнал пульса чата — рядом с файлом меток, с тем же нулём времени.

        Это НЕВИДИМЫЙ файл: в чат из него ничего не уходит, он нужен разбору стрима
        (Этап 3), чтобы знать, где чат взрывался.
        """
        if not self.write_pulse:
            return None
        return PulseCollector(channel=self.channel, output_path=pulse_path_for(col.output_path),
                              ref_epoch=col.ref_epoch, broadcast_id=col.broadcast_id,
                              started_at=col._started_iso)

    def _begin_session(self, info: StreamInfo) -> None:
        """Начать новую сессию: эфир начался или сразу сменился следующим."""
        with self._lock:
            old, old_pulse = self.collector, self.pulse
            new = self._new_collector(info)
            new.set_online(info.viewers if info.live else None)
            resumed = new.resume_existing()      # перезапуск посреди стрима — дописываем
            new_pulse = self._new_pulse(new)
            if new_pulse is not None:
                new_pulse.resume_existing()
            self.collector = new
            self.pulse = new_pulse
        if old is not None and old.output_path != new.output_path:
            if old.marks:
                try:
                    old.finalize()               # прошлый файл закрываем как есть
                except OSError:
                    pass
            if old_pulse is not None and old_pulse is not new_pulse and old_pulse.has_data:
                try:
                    old_pulse.finalize()
                except OSError:
                    pass
        self.banter.reset_session()              # новый эфир — знакомимся со зрителями заново
        self._emit("session", {"path": new.output_path, "live": info.live,
                               "title": info.title, "game": info.game,
                               "broadcast_id": info.broadcast_id,
                               "started_at": new._started_iso, "resumed": resumed,
                               "marks": len(new.marks)})
        if resumed:
            self._emit("log", {"text": f"продолжаю файл этого эфира "
                                       f"({resumed} меток уже есть)"})

    def _end_session(self) -> None:
        with self._lock:
            col, pulse = self.collector, self.pulse
        try:
            col.finalize()
        except OSError as e:
            self._emit("log", {"text": f"не смог дописать файл меток: {e}"})
        if pulse is not None and pulse.has_data:
            try:
                pulse.finalize()
            except OSError as e:
                self._emit("log", {"text": f"не смог дописать журнал чата: {e}"})

    # ---- управление ----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._watcher = StreamWatcher(self.channel, self.token, self._on_stream,
                                      period=self.watch_period)
        self._watcher.start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="clipbot-chat")
        self._thread.start()
        self._ticker = threading.Thread(target=self._tick_loop, daemon=True,
                                        name="clipbot-tick")
        self._ticker.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._watcher:
            self._watcher.stop()
        if self._chat:
            self._chat.stop()
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._ticker:
            self._ticker.join(timeout=timeout)
        self._end_session()
        self._emit("status", {"text": "Бот остановлен", "running": False})

    def connect_now(self) -> None:
        """Зайти в чат немедленно, не дожидаясь эфира (проверка бота без стрима).

        Чат канала работает и вне эфира, поэтому так удобно проверять всю цепочку.
        Автопилот при этом не выключается: когда начнётся настоящий эфир, заведётся
        обычная сессия со своим файлом.
        """
        self._live.set()
        self._emit("status", {"text": "Захожу в чат без эфира (проверка)…", "running": True})

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def marks_count(self) -> int:
        return len(self.collector.marks)

    @property
    def output_path(self) -> str:
        return self.collector.output_path

    @property
    def pulse_path(self) -> str:
        return self.pulse.output_path if self.pulse is not None else ""

    # ---- события эфира ----
    def _on_stream(self, info: StreamInfo) -> None:
        """Колбэк наблюдателя: начало/конец эфира, смена эфира, онлайн."""
        was_live, prev_id = self.stream.live, self.stream.broadcast_id
        self.stream = info
        self.collector.set_online(info.viewers if info.live else None)
        self._emit("online", {"viewers": info.viewers, "live": info.live,
                              "title": info.title, "game": info.game})

        if info.live and (not was_live or info.broadcast_id != prev_id):
            # Начался эфир (или сразу следующий) — новая сессия со своим файлом.
            self._begin_session(info)
            self._emit("status", {"text": f"Эфир начался: {info.title or self.channel}",
                                  "running": True})
            self._live.set()
        elif not info.live and was_live:
            self._live.clear()
            self._end_session()
            self._emit("status", {"text": "Эфир закончился — файл меток закрыт",
                                  "running": self.running})
            if self.autopilot and self._chat:
                self._chat.stop()                 # выходим из чата до следующего эфира
        elif info.live:
            self._live.set()

    # ---- внутреннее ----
    def _emit(self, kind: str, payload: dict) -> None:
        try:
            self.on_event(kind, payload)
        except Exception:            # noqa: BLE001 — колбэк UI не должен ронять бота
            pass

    def _say(self, text: str, announce: bool = True) -> None:
        """Сказать что-то в чат от лица бота.

        `announce=False` — для подтверждения метки: она уже показана в журнале UI
        отдельной строкой, второй раз её печатать незачем.
        """
        if not text:
            return
        if self._chat:
            self._chat.reply(text)
        self.banter.note_sent()          # любая наша реплика сдвигает кулдаун болталки
        if announce:
            self._emit("say", {"text": text})

    def _on_message(self, tags: dict, login: str, text: str) -> None:
        now = time.time()
        report = is_report_command(text)

        # Под тем же замком, что и смена сессии: иначе метка могла уйти в СТАРЫЙ
        # сборщик, а новый следом переписал бы файл эфира без неё (метка терялась).
        with self._lock:
            col, pulse = self.collector, self.pulse
            if pulse is not None:
                pulse.feed(login, text, now)          # пульс копит ЛЮБОЕ сообщение
            reply = None if report else col.handle_message(login, tags, text, now)
            mark = col.marks[-1] if reply else None
            total, path = len(col.marks), col.output_path

        if report:
            self._on_report(tags, login, now, col, pulse)
            return

        if reply and mark is not None:
            role = role_from_tags(login, self.channel, tags)
            self._emit("mark", {"n": total, "t": mark.t, "author": mark.author,
                                "role": role.value, "note": mark.note,
                                "path": path, "live": self.stream.live})
            self._say(reply, announce=False)
            return

        # Поздороваться с тем, кто написал впервые за эфир.
        rate = pulse.rate_per_min(now=now) if pulse is not None else 0.0
        hello = self.banter.greet(login, tags.get("display-name") or login,
                                  rate_per_min=rate, now=now)
        if hello:
            self._say(hello)

    def _on_report(self, tags: dict, login: str, now: float,
                   col: MarkCollector, pulse) -> None:
        """Команда !отчёт: сводка эфира в чат. Доступ — стример и модераторы."""
        role = role_from_tags(login, self.channel, tags)
        if role not in (AuthorType.STREAMER, AuthorType.MODERATOR):
            return
        if (now - self._last_report) < 60.0:
            return
        self._last_report = now

        peak_ratio, peak_t, rate, chatters = 0.0, None, 0.0, 0
        if pulse is not None:
            rate = pulse.rate_per_min(now=now)
            chatters = pulse.last_bucket.u if pulse.last_bucket else 0
            from core.chatpulse import find_spikes
            spikes = find_spikes(pulse.as_log())
            if spikes:
                best = max(spikes, key=lambda s: s.ratio)
                peak_ratio, peak_t = best.ratio, best.center
        text = build_report(uptime_sec=max(0.0, now - col.ref_epoch), marks=len(col.marks),
                            rate_per_min=rate, live=self.stream.live,
                            peak_ratio=peak_ratio, peak_t=peak_t, chatters=chatters)
        self._say(text)                  # в журнале UI видно ровно то, что ушло в чат
        self._emit("report", {"text": text})

    def _tick_loop(self) -> None:
        """Фоновый тик: закрывает корзины пульса, пишет журнал, даёт слово болталке."""
        while not self._stop.wait(self.tick_period):
            now = time.time()
            try:
                closed = self.pulse.tick(now) if self.pulse is not None else []
            except OSError as e:
                closed = []
                self._emit("log", {"text": f"журнал чата не пишется: {e}"})
            if not self._chat:
                continue                      # в чате нас нет — говорить некуда
            try:
                # Взрыв чата — поддержать волну.
                if closed and self.pulse is not None:
                    ratio = self.pulse.live_ratio(now)
                    said = self.banter.hype(ratio, now)
                    if said:
                        self._say(said)
                        continue
                # Чат затих — расшевелить.
                silence = self.pulse.silence_sec(now) if self.pulse is not None else None
                if silence is not None:
                    said = self.banter.revive(silence, now)
                    if said:
                        self._say(said)
                        continue
                # Просто по таймеру.
                said = self.banter.idle(now)
                if said:
                    self._say(said)
            except OSError as e:                # noqa: BLE001 — болталка не роняет бота
                self._emit("log", {"text": f"болталка: {e}"})

    def _run(self) -> None:
        delay = 5.0
        while not self._stop.is_set():
            # Автопилот: пока эфира нет — просто ждём, в чат не заходим.
            if self.autopilot and not self._live.is_set():
                self._emit("status", {"text": "Автопилот: жду начала эфира…",
                                      "running": True})
                while not self._stop.is_set() and not self._live.is_set():
                    self._stop.wait(1.0)
                if self._stop.is_set():
                    return
            try:
                self._emit("status", {"text": f"Подключаюсь к чату #{self.channel}…",
                                      "running": True})
                self._chat = TwitchChat(
                    self.channel, self.nick, self.token, self._on_message,
                    on_log=lambda s: self._emit("log", {"text": s}),
                    reply_in_chat=self.reply_in_chat)
                self._chat.connect()
                self._emit("status", {"text": f"Бот в чате #{self.channel} — жду !clip",
                                      "running": True})
                delay = 5.0                      # успешное подключение сбрасывает паузу
                self._chat.run()
                self._chat = None
                if self._stop.is_set():
                    return
                if self.autopilot and not self._live.is_set():
                    continue                     # это мы сами вышли после конца эфира
                raise ConnectionError("соединение закрыто")
            except AuthFailed as e:
                self._emit("error", {"text": "Twitch не пустил в чат — нужно войти заново "
                                             f"({e})", "relogin": True})
                return
            except (ConnectionError, ssl.SSLError, OSError) as e:
                self._chat = None
                if self._stop.is_set():
                    return
                self._emit("status", {"text": f"Обрыв связи ({e}) — переподключаюсь "
                                              f"через {int(delay)} с…", "running": True})
                if self._stop.wait(delay):
                    return
                delay = min(delay * 2, 60.0)     # не долбим Twitch при долгой аварии


def _iso_to_epoch(iso: str) -> Optional[float]:
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
