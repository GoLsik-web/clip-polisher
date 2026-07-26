"""dev/test_bot.py — тест бота в приложении БЕЗ сети и без Twitch.

Проверяет то, что раньше можно было увидеть только вживую:
  1. вход через Twitch (device flow) — на подменённых ответах Twitch;
  2. хранение/обновление токена (сохранить → прочитать → забыть);
  3. BotService целиком: чат-заглушка шлёт сообщения → метки записаны, события
     дошли до UI-колбэка, файл читается приложением и даёт моменты;
  4. остановка бота не вешает поток.

Запуск: .venv\\Scripts\\python.exe -m dev.test_bot
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Токены и метки — во временную папку, чтобы не трогать реальные данные пользователя.
_TMP = tempfile.mkdtemp(prefix="clipbot_test_")
os.environ["LOCALAPPDATA"] = _TMP

from core import chatbot, twitch_auth as auth              # noqa: E402
from core.marks import MarksFile, AudienceMode, select_moments  # noqa: E402


def _ok(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    assert cond, msg


# --------------------------------------------------------------------------
# 1. Device flow на подменённых ответах Twitch
# --------------------------------------------------------------------------

def test_device_flow():
    print("\n[1] Вход через Twitch (device flow, ответы подменены):")
    calls = {"n": 0}

    def fake_post(url, fields, timeout=20.0):
        if url.endswith("/device"):
            _ok(fields["client_id"] == auth.CLIENT_ID, "шлём наш Client ID")
            _ok("chat:read" in fields["scopes"] and "chat:edit" in fields["scopes"],
                "просим только права чата")
            return 200, {"device_code": "DEV123", "user_code": "ABCD1234",
                         "verification_uri": "https://www.twitch.tv/activate",
                         "interval": 0, "expires_in": 1800}
        calls["n"] += 1
        if calls["n"] == 1:                       # юзер ещё не подтвердил
            return 400, {"message": "authorization_pending"}
        return 200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

    old, auth._post = auth._post, fake_post
    try:
        flow = auth.DeviceFlow()
        dc = flow.start()
        _ok(dc.user_code == "ABCD1234", "код для юзера получен")
        _ok(dc.seconds_left > 1700, "срок жизни кода посчитан")
        _ok(flow.poll_once() is None, "«ещё не подтвердил» — это не ошибка, а ожидание")
        tok = flow.wait()
        _ok(tok["access_token"] == "AT" and tok["refresh_token"] == "RT",
            "после подтверждения пришли токены")
        _ok(tok["expires_at"] > time.time(), "срок годности токена посчитан")
    finally:
        auth._post = old


def test_device_flow_errors():
    print("\n[2] Ошибки входа понятным текстом:")
    def expired(url, fields, timeout=20.0):
        if url.endswith("/device"):
            return 200, {"device_code": "D", "user_code": "U", "interval": 0,
                         "expires_in": 60}
        return 400, {"message": "device code expired"}

    old, auth._post = auth._post, expired
    try:
        f = auth.DeviceFlow(); f.start()
        try:
            f.poll_once()
            _ok(False, "истёкший код должен давать ошибку")
        except auth.AuthError as e:
            _ok("истёк" in str(e).lower(), f"истёкший код → понятный текст: «{e}»")
    finally:
        auth._post = old


# --------------------------------------------------------------------------
# 3. Хранение токенов
# --------------------------------------------------------------------------

def test_token_store():
    print("\n[3] Хранение входа:")
    auth.save_tokens({"access_token": "AT", "refresh_token": "RT",
                      "expires_at": time.time() + 3600})
    _ok(os.path.isfile(auth.tokens_path()), "файл входа создан в папке пользователя")
    _ok((auth.load_tokens() or {}).get("access_token") == "AT", "токен читается обратно")

    # ensure_token: токен «живой» → просто отдаём (validate подменяем).
    old_v = auth.validate
    auth.validate = lambda t: {"login": "golsik__", "user_id": "1", "scopes": [], "expires_in": 3000}
    try:
        _ok(auth.ensure_token() == "AT", "живой токен отдаётся как есть")
        acc = auth.current_account()
        _ok(acc and acc["login"] == "golsik__", "видно, кто вошёл")
    finally:
        auth.validate = old_v

    # Протухший токен обновляется по refresh_token.
    auth.save_tokens({"access_token": "OLD", "refresh_token": "RT",
                      "expires_at": time.time() - 10})
    old_r, old_v = auth.refresh, auth.validate
    auth.refresh = lambda rt, client_id=auth.CLIENT_ID: (
        {"access_token": "NEW", "refresh_token": rt, "expires_at": time.time() + 3600}
        if rt == "RT" else None)
    auth.validate = lambda t: {"login": "golsik__", "user_id": "1", "scopes": [], "expires_in": 3000}
    try:
        _ok(auth.ensure_token() == "NEW", "протухший токен обновился сам")
        _ok((auth.load_tokens() or {}).get("access_token") == "NEW", "новый токен сохранён")
    finally:
        auth.refresh, auth.validate = old_r, old_v

    auth.clear_tokens()
    _ok(auth.load_tokens() is None, "выход стирает вход")


# --------------------------------------------------------------------------
# 4. BotService с чатом-заглушкой
# --------------------------------------------------------------------------

class FakeChat:
    """Вместо настоящего IRC: сразу «подключается» и отдаёт заранее заданные сообщения."""
    SCRIPT = [
        ({"badges": "broadcaster/1", "display-name": "GoLsik__"}, "golsik__", "!clip рофл про кота"),
        ({"display-name": "Kirill"}, "kirill", "просто болтовня"),
        ({"display-name": "Kirill"}, "kirill", "!клип клатч"),
        ({"mod": "1", "display-name": "ModerVasya"}, "modervasya", "!метка эпик"),
    ]
    replies: list = []

    def __init__(self, channel, nick, token, on_message, on_log=None, reply_in_chat=True):
        self.on_message = on_message
        self.on_log = on_log or (lambda s: None)
        self._stop = threading.Event()

    def connect(self):
        self.on_log("вошли в чат Twitch")

    def run(self):
        for tags, login, text in self.SCRIPT:
            if self._stop.is_set():
                return
            self.on_message(tags, login, text)
            time.sleep(0.02)
        self._stop.wait(10)          # дальше «молчим», как настоящий чат

    def reply(self, text):
        FakeChat.replies.append(text)

    def stop(self):
        self._stop.set()


def test_bot_service():
    print("\n[4] Бот целиком (чат подменён, сети нет):")
    events: list = []
    old_chat, chatbot.TwitchChat = chatbot.TwitchChat, FakeChat
    old_helix, auth.helix_stream = auth.helix_stream, lambda tok, ch: {
        "viewer_count": 137, "started_at": ""}
    try:
        out = os.path.join(_TMP, "marks", "test.clipmarks")
        svc = chatbot.BotService(channel="golsik__", token="AT", nick="golsik__",
                                 output_path=out,
                                 on_event=lambda k, p: events.append((k, p)))
        svc.start()
        deadline = time.time() + 5
        while svc.marks_count < 3 and time.time() < deadline:
            time.sleep(0.05)
        _ok(svc.marks_count == 3, f"записано 3 метки из 4 сообщений (болтовня не в счёт): {svc.marks_count}")
        kinds = [k for k, _ in events]
        _ok("status" in kinds, "UI получил статус подключения")
        marks_ev = [p for k, p in events if k == "mark"]
        _ok(len(marks_ev) == 3, "каждая метка прилетела в UI отдельным событием")
        _ok(marks_ev[0]["role"] == "streamer" and marks_ev[0]["note"] == "рофл про кота",
            "роль и заметка в событии верные")
        _ok(marks_ev[2]["role"] == "moderator", "модер распознан по тегу mod=1")
        _ok(any(k == "online" and p.get("viewers") == 137 for k, p in events),
            "онлайн канала доехал до UI")
        _ok(len(FakeChat.replies) == 3, "бот подтвердил каждую метку в чат")

        # Файл читается приложением и даёт моменты.
        _ok(os.path.isfile(out), "файл меток создан")
        mf = MarksFile.from_json(out)
        _ok(len(mf.marks) == 3 and mf.streamer == "golsik__", "файл читается приложением")
        _ok(mf.online == 137, "онлайн записан в файл (нужен режиму «Авто»)")
        moments = select_moments(mf, AudienceMode.SMALL)
        _ok(len(moments) >= 1, f"из меток получаются моменты для нарезки: {len(moments)}")

        t0 = time.time()
        svc.stop()
        _ok(not svc.running, "бот остановился")
        _ok(time.time() - t0 < 4, "остановка не подвисает")
        _ok(any(k == "status" and not p.get("running") for k, p in events),
            "UI узнал об остановке")
    finally:
        chatbot.TwitchChat = old_chat
        auth.helix_stream = old_helix


def test_paths():
    print("\n[5] Куда пишутся метки:")
    d = chatbot.default_marks_dir()
    _ok(d.startswith(_TMP), "метки идут в папку пользователя, а не в папку программы")
    p = chatbot.default_output("golsik__")
    _ok(p.endswith(".clipmarks") and "golsik__" in p, f"имя файла с каналом и датой: {os.path.basename(p)}")


# --------------------------------------------------------------------------
# 6. Эфиры не смешиваются
# --------------------------------------------------------------------------

def _live(bid: str, title: str, started_epoch: float) -> chatbot.StreamInfo:
    import datetime
    iso = datetime.datetime.fromtimestamp(started_epoch,
                                          datetime.timezone.utc).isoformat()
    return chatbot.StreamInfo(live=True, broadcast_id=bid, title=title,
                              game="Just Chatting", started_at=iso, viewers=100)


def _mark(svc, text: str = "!clip момент"):
    svc._on_message({"badges": "broadcaster/1", "display-name": "GoLsik__"},
                    "golsik__", text)


def test_two_streams_dont_mix():
    print("\n[6] Два эфира за день НЕ смешиваются (был баг — затирались):")
    now = time.time()
    svc = chatbot.BotService(channel="golsik__", token="AT", nick="golsik__")

    # Утренний эфир.
    svc._on_stream(_live("111", "Утренний стрим", now - 7200))
    p1 = svc.output_path
    _mark(svc); _mark(svc, "!clip второй")
    _ok(svc.marks_count == 2, "в утреннем эфире 2 метки")
    _ok("111" in os.path.basename(p1), f"в имени файла id эфира: {os.path.basename(p1)}")

    # Эфир кончился, вечером начался ДРУГОЙ.
    svc._on_stream(chatbot.StreamInfo(live=False))
    svc._on_stream(_live("222", "Вечерний стрим", now - 600))
    p2 = svc.output_path
    _ok(p2 != p1, "у вечернего эфира СВОЙ файл")
    _mark(svc, "!clip вечерний момент")

    m1, m2 = MarksFile.from_json(p1), MarksFile.from_json(p2)
    _ok(len(m1.marks) == 2, f"утренние метки на месте: {len(m1.marks)} (раньше становилось 1)")
    _ok(len(m2.marks) == 1, "вечерние метки в своём файле")
    _ok(m1.broadcast_id == "111" and m2.broadcast_id == "222", "id эфира записан в оба файла")
    _ok(m1.title == "Утренний стрим" and m2.title == "Вечерний стрим",
        "название эфира видно в файле — понятно, от какого стрима клипы")
    _ok(m1.duration and m1.duration > 0, "у закрытого эфира записана длительность")

    # Ноль времени = старт ЭТОГО эфира.
    _ok(abs(m2.marks[0].t - 600) < 30, f"время метки считается от старта эфира: {m2.marks[0].t:.0f}с")


def test_restart_midstream():
    print("\n[7] Перезапуск программы посреди эфира:")
    now = time.time()
    info = _live("333", "Длинный стрим", now - 3600)
    svc1 = chatbot.BotService(channel="golsik__", token="AT", nick="golsik__")
    svc1._on_stream(info)
    _mark(svc1); _mark(svc1)
    path = svc1.output_path

    svc2 = chatbot.BotService(channel="golsik__", token="AT", nick="golsik__")
    svc2._on_stream(info)                       # тот же эфир после перезапуска
    _ok(svc2.output_path == path, "тот же эфир — тот же файл")
    _ok(svc2.marks_count == 2, "старые метки подхвачены, а не затёрты")
    _mark(svc2, "!clip после перезапуска")
    _ok(len(MarksFile.from_json(path).marks) == 3, "в файле все три метки")


def test_autopilot():
    print("\n[8] Автопилот (в чат заходим только на эфире):")
    events = []
    old_chat, chatbot.TwitchChat = chatbot.TwitchChat, FakeChat
    FakeChat.replies = []
    try:
        svc = chatbot.BotService(channel="golsik__", token="AT", nick="golsik__",
                                 autopilot=True, watch_period=3600,
                                 on_event=lambda k, p: events.append((k, p)))
        svc._watcher = None
        svc._stop.clear()
        svc._thread = threading.Thread(target=svc._run, daemon=True)
        svc._thread.start()
        time.sleep(0.4)
        _ok(any("жду начала эфира" in p.get("text", "") for k, p in events if k == "status"),
            "без эфира бот ждёт и в чат не заходит")
        _ok(svc._chat is None, "соединения с чатом нет")

        svc._on_stream(_live("444", "Стрим начался", time.time() - 10))
        deadline = time.time() + 5
        while svc.marks_count < 3 and time.time() < deadline:
            time.sleep(0.05)
        _ok(svc.marks_count == 3, f"эфир начался → бот сам зашёл и ловит метки ({svc.marks_count})")
        _ok(any(k == "session" for k, _ in events), "заведён файл этого эфира")

        svc._on_stream(chatbot.StreamInfo(live=False))
        time.sleep(0.5)
        _ok(svc._chat is None, "эфир кончился → бот вышел из чата")
        _ok(svc.running, "бот жив и ждёт следующего эфира")
        _ok(svc.running, "но сам бот жив и ждёт следующего эфира")
        mf = MarksFile.from_json(svc.output_path)
        _ok(mf.duration and mf.duration > 0, "файл эфира закрыт (записана длительность)")
        svc.stop()
        _ok(not svc.running, "остановка автопилота работает")
    finally:
        chatbot.TwitchChat = old_chat


def test_check_without_stream():
    """Сценарий «дать другу проверить без стрима»: автопилот ждёт, жмём «зайти сейчас»."""
    print("\n[9] Проверка бота БЕЗ эфира (кнопка «Зайти в чат сейчас»):")
    events = []
    old_chat, chatbot.TwitchChat = chatbot.TwitchChat, FakeChat
    FakeChat.replies = []
    try:
        svc = chatbot.BotService(channel="friend", token="AT", nick="friend",
                                 autopilot=True, watch_period=3600,
                                 on_event=lambda k, p: events.append((k, p)))
        svc._watcher = None
        svc._stop.clear()
        svc._thread = threading.Thread(target=svc._run, daemon=True)
        svc._thread.start()
        time.sleep(0.4)
        _ok(svc._chat is None, "эфира нет — бот сам в чат не пошёл")
        waiting = [p["text"] for k, p in events if k == "status" and "жду начала эфира" in p.get("text", "")]
        _ok(waiting, "в интерфейсе видно «жду начала эфира» (по этому тексту показывается кнопка)")

        svc.connect_now()                       # это и делает кнопка
        deadline = time.time() + 5
        while svc.marks_count < 3 and time.time() < deadline:
            time.sleep(0.05)
        _ok(svc.marks_count == 3, f"бот зашёл в чат без эфира и ловит !clip ({svc.marks_count})")
        _ok(len(FakeChat.replies) == 3, "и отвечает в чат")
        path = svc.output_path
        _ok("вне-эфира" in os.path.basename(path),
            f"метки идут в файл «вне эфира» — с записью стрима не смешаются: {os.path.basename(path)}")
        mf = MarksFile.from_json(path)
        _ok(len(select_moments(mf, AudienceMode.SMALL)) >= 1,
            "из них получаются моменты — можно дойти до нарезки")
        svc.stop()
    finally:
        chatbot.TwitchChat = old_chat


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    test_device_flow()
    test_device_flow_errors()
    test_token_store()
    test_bot_service()
    test_paths()
    test_two_streams_dont_mix()
    test_restart_midstream()
    test_autopilot()
    test_check_without_stream()
    print("\nВСЕ ПРОВЕРКИ ПРОШЛИ ✔")


if __name__ == "__main__":
    main()
