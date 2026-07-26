"""twitch_marks_bot.py — запуск бота меток из командной строки.

Обычному пользователю это НЕ нужно: бот встроен в приложение (режим «Метки через бота»
→ вкладка «Бот» → кнопка «Войти через Twitch»). Этот скрипт — для запуска без окна
(например, на другом компьютере) и для отладки.

Движок — в `core/chatbot.py`. Здесь только разбор аргументов, конфиг и печать в консоль.

Запуск:
    python bot/twitch_marks_bot.py                 # вход, сохранённый приложением
    python bot/twitch_marks_bot.py --login         # войти через Twitch (покажет код)
    python bot/twitch_marks_bot.py --config путь   # старый способ: свой config.json с токеном
    python bot/twitch_marks_bot.py --simulate      # без Twitch: печатай сообщения вручную

Чат канала работает, даже когда стример НЕ в эфире — метки можно тестировать без стрима.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.chatbot import (BotService, MarkCollector, default_output,  # noqa: E402
                          parse_irc, role_from_tags)                   # noqa: F401
from core import twitch_auth as auth                                   # noqa: E402


# --------------------------------------------------------------------------
# Конфиг (нужен только для старого способа — со своим токеном)
# --------------------------------------------------------------------------

def load_config(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("commands", ["!clip", "!клип", "!метка"])
    cfg.setdefault("who_can_mark", "all")
    cfg.setdefault("viewer_cooldown_sec", 30)
    cfg.setdefault("reply_in_chat", True)
    cfg.setdefault("output", "")
    return cfg


def resolve_output(pattern: str, channel: str) -> str:
    if not pattern:
        return default_output(channel)
    date = datetime.date.today().isoformat()
    return pattern.replace("<channel>", channel).replace("<date>", date)


# --------------------------------------------------------------------------
# Вход через Twitch в консоли
# --------------------------------------------------------------------------

def do_login() -> dict:
    flow = auth.DeviceFlow()
    dc = flow.start()
    print("\n=== Вход через Twitch ===")
    print(f"1. Открой в браузере: {dc.verification_uri}")
    print(f"2. Код подтверждения:  {dc.user_code}")
    print("3. Подтверди вход. Жду…\n")
    tok = flow.wait(on_tick=lambda left: print(f"   …ещё жду ({left} с до истечения кода)"))
    auth.save_tokens(tok)
    info = auth.validate(tok["access_token"]) or {}
    print(f"Вход выполнен: {info.get('login', '?')}\n")
    return tok


# --------------------------------------------------------------------------
# Режим симуляции (без Twitch)
# --------------------------------------------------------------------------

def run_simulate(collector: MarkCollector) -> None:
    print("Симуляция (без Twitch). Печатай сообщения. Префикс роли: "
          "@streamer/@mod/@vip (по умолчанию зритель). Ctrl+C — выход.\n"
          "Пример:  @streamer !clip рофл про кота")
    roles = {"@streamer": {"badges": "broadcaster/1"}, "@mod": {"mod": "1"},
             "@vip": {"vip": "1"}}
    user_n = 0
    try:
        for raw in sys.stdin:
            text = raw.rstrip("\n")
            if not text:
                continue
            tags = {}
            login = f"viewer{user_n}"; user_n += 1
            for pref, t in roles.items():
                if text.startswith(pref + " "):
                    tags = dict(t); login = pref[1:]; text = text[len(pref) + 1:]
                    break
            r = collector.handle_message(login, tags, text)
            print("  ->", r if r else "(не команда/проигнорировано)")
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> None:
    # Консоль Windows по умолчанию cp1251 — символы вроде «✓» роняли бы печать,
    # а русские команды из --simulate читались бы кракозябрами.
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Бот меток для Twitch-чата")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "config.json"),
                    help="config.json со своим токеном (старый способ)")
    ap.add_argument("--channel", default="", help="канал (по умолчанию — тот, под кем вошли)")
    ap.add_argument("--login", action="store_true", help="войти через Twitch и сохранить вход")
    ap.add_argument("--logout", action="store_true", help="забыть вход")
    ap.add_argument("--autopilot", action="store_true",
                    help="ждать начала эфира и заходить в чат автоматически")
    ap.add_argument("--simulate", action="store_true",
                    help="локальная симуляция без подключения к Twitch")
    args = ap.parse_args()

    if args.logout:
        auth.logout()
        print("Вход забыт.")
        return

    cfg = load_config(args.config)

    if args.simulate:
        channel = args.channel or cfg.get("channel") or "test"
        out = resolve_output(cfg.get("output", ""), channel)
        collector = MarkCollector(
            channel=channel, output_path=out, commands=cfg.get("commands"),
            who_can_mark=cfg.get("who_can_mark", "all"),
            viewer_cooldown_sec=cfg.get("viewer_cooldown_sec", 30))
        print(f"Файл меток: {out}")
        run_simulate(collector)
        collector.finalize()
        print(f"\nГотово. Меток: {len(collector.marks)} → {out}")
        return

    # Токен: 1) свой из config.json (старый способ), 2) сохранённый вход, 3) войти сейчас.
    token, nick = cfg.get("bot_oauth", ""), cfg.get("bot_username", "")
    if token:
        channel = args.channel or cfg.get("channel") or ""
        if not channel:
            print("В config.json не указан channel."); sys.exit(2)
    else:
        acc = None if args.login else auth.current_account()
        if not acc:
            do_login()
            acc = auth.current_account()
        if not acc:
            print("Не удалось войти."); sys.exit(2)
        token, nick = acc["token"], acc["login"]
        channel = args.channel or cfg.get("channel") or acc["login"]

    # Своё имя файла — только если оно явно задано в конфиге. Иначе бот сам заводит
    # отдельный файл на каждый эфир (иначе второй стрим за день затирал первый).
    out = resolve_output(cfg["output"], channel) if cfg.get("output") else ""
    print(f"Канал: #{channel} | бот пишет от: {nick or channel}"
          + (" | автопилот: жду эфира" if args.autopilot else ""))

    def on_event(kind: str, p: dict) -> None:
        if kind == "mark":
            print(f"  метка #{p['n']} от {p['author']} ({p['role']}) "
                  f"t={p['t']:.1f}с {p['note'] or ''}")
        elif kind == "session":
            what = p.get("title") or ("эфир" if p.get("live") else "вне эфира")
            print(f"  [{what}] файл: {p['path']}"
                  + (f" (продолжаю, меток уже {p['marks']})" if p.get("resumed") else ""))
        elif kind in ("status", "error", "log"):
            print(("!! " if kind == "error" else "") + p.get("text", ""))
        elif kind == "online":
            print(f"  онлайн: {p['viewers'] if p['live'] else 'офлайн'}")

    svc = BotService(channel=channel, token=token, nick=nick or channel,
                     output_path=out, commands=cfg.get("commands"),
                     who_can_mark=cfg.get("who_can_mark", "all"),
                     viewer_cooldown_sec=cfg.get("viewer_cooldown_sec", 30),
                     reply_in_chat=cfg.get("reply_in_chat", True),
                     autopilot=args.autopilot, on_event=on_event)
    print(f"Файл меток: {svc.output_path}")
    svc.start()
    try:
        while svc.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nОстановка…")
    finally:
        svc.stop()
        print(f"Меток записано: {svc.marks_count} → {svc.output_path}")


if __name__ == "__main__":
    main()
