"""shot_bot.py — офскрин-снимки вкладки «Бот» (вход через Twitch + бот меток).

Снимает три состояния в обеих темах: «не вошёл», «показан код подтверждения»,
«вошёл + бот работает и ловит метки». Сеть не трогаем — аккаунт и события бота
подставляем вручную, чтобы снимок был воспроизводимым.

Запуск:  python dev/shot_bot.py   → PNG в out/shots/
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFontDatabase           # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea   # noqa: E402

from ui.bot_panel import BotPanel                 # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "shots")


def _fonts() -> None:
    # Офскрин без системных шрифтов — иначе текст «квадратами».
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for f in ("PTSans-Regular.ttf", "PTSans-Bold.ttf"):
        QFontDatabase.addApplicationFont(os.path.join(root, "assets", "fonts", f))


def _shot(w, name: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    w.grab().save(path)
    print("  ->", path)


def main() -> None:
    app = QApplication(sys.argv)
    _fonts()
    from ui.theme import build_qss

    for theme in ("dark", "light"):
        # 1) не вошёл
        p = BotPanel(theme)
        # Отключаем фоновую проверку сохранённого входа: иначе она доедет посреди
        # съёмки и перетрёт состояние, которое мы подставили руками.
        p._refresh_account = lambda: None
        p.setStyleSheet(build_qss(theme))
        p.apply_theme(theme)            # как делает главное окно (красит тумблеры)
        p.resize(720, 900)
        p._on_account({})                       # без сети: считаем, что входа нет
        p.show()
        app.processEvents()
        _shot(p, f"bot_1_login_{theme}.png")

        # 2) показан код подтверждения
        p._start_login = lambda: None           # поток не запускаем
        p.login_btn.setVisible(False); p.login_hint.setVisible(False)
        p.code_box.setVisible(True)
        p._on_code("WHFYNRWP", "https://www.twitch.tv/activate", 1740)
        app.processEvents()
        _shot(p, f"bot_2_code_{theme}.png")

        # 3) вошёл + бот работает и уже поймал метки
        p.code_box.setVisible(False)
        p._auto_started = True          # не поднимаем настоящего бота ради снимка
        p._on_account({"login": "golsik__", "user_id": "1", "token": "x"})
        p._on_bot_event("session", {
            "path": "…/marks/golsik___2026-07-26_21-04_51234.clipmarks", "live": True,
            "title": "Вечерний каток в CS2", "game": "Counter-Strike 2",
            "broadcast_id": "51234", "started_at": "2026-07-26T21:04:00+03:00",
            "resumed": 0, "marks": 0})
        p.connect_btn.setText("Выключить автопилот")
        p.channel_edit.setEnabled(False)
        p._on_bot_event("status", {"text": "Бот в чате #golsik__ — жду !clip", "running": True})
        p._on_bot_event("online", {"viewers": 137, "live": True})
        for n, (t, who, role, note) in enumerate([
                (62.0, "GoLsik__", "streamer", "рофл про кота"),
                (415.5, "modervasya", "moderator", ""),
                (980.0, "kirill_2007", "viewer", "клатч"),
                (1322.0, "vip_masha", "vip", "падение с моста")], start=1):
            p._on_bot_event("mark", {"n": n, "t": t, "author": who, "role": role,
                                     "note": note, "path": "C:/…/marks/golsik___2026-07-26.clipmarks"})
        app.processEvents()
        _shot(p, f"bot_3_running_{theme}.png")

        # 4) автопилот ждёт эфира — видна кнопка «зайти в чат сейчас» (проверка без стрима)
        p._on_bot_event("status", {"text": "Автопилот: жду начала эфира…", "running": True})
        p._on_bot_event("online", {"viewers": None, "live": False})
        p._on_bot_event("session", {"path": "…/marks/friend_вне-эфира.clipmarks",
                                    "live": False, "marks": 0})
        app.processEvents()
        _shot(p, f"bot_4_waiting_{theme}.png")

        # 5) низ вкладки: болталка, автозапуск и журнал эфира (они ниже сгиба)
        p._on_bot_event("say", {"text": "Чат ожил — значит, было за что."})
        p._on_bot_event("say", {"text": "Эфир 1 ч 12 мин · 4 метки · чат 31 сообщ/мин"})
        scroll = p.findChild(QScrollArea)
        if scroll:
            bar = scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        app.processEvents()
        _shot(p, f"bot_5_bottom_{theme}.png")
        p.deleteLater()

    print("Готово.")


if __name__ == "__main__":
    main()
