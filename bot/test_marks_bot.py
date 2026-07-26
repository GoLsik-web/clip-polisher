"""bot/test_marks_bot.py — тест ядра бота меток (без сети).

Проверяет: роли по IRC-тегам, распознавание команд (!clip/!клип/!метка + заметка),
антиспам-кулдаун зрителей, режим «только доверенные», запись/чтение файла .clipmarks,
разбор строки IRC. Запуск: python -m bot.test_marks_bot
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.marks_collector import MarkCollector, role_from_tags
from bot.twitch_marks_bot import parse_irc
from core.marks import MarksFile, AuthorType as A


def _ok(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    assert cond, msg


def test_roles():
    print("\n[1] Роли по тегам Twitch:")
    _ok(role_from_tags("egoric", "egoric", {}) == A.STREAMER, "логин==канал → стример")
    _ok(role_from_tags("x", "egoric", {"badges": "broadcaster/1"}) == A.STREAMER, "badge broadcaster → стример")
    _ok(role_from_tags("x", "egoric", {"mod": "1"}) == A.MODERATOR, "mod=1 → модер")
    _ok(role_from_tags("x", "egoric", {"badges": "vip/1"}) == A.VIP, "badge vip → вип")
    _ok(role_from_tags("x", "egoric", {}) == A.VIEWER, "без тегов → зритель")


def test_commands_and_note():
    print("\n[2] Команды и заметка:")
    c = MarkCollector("egoric", _tmp(), ref_epoch=1000.0)
    _ok(c.parse_command("!clip") == "", "!clip без заметки")
    _ok(c.parse_command("!клип рофл про кота") == "рофл про кота", "!клип с заметкой")
    _ok(c.parse_command("!метка эпик") == "эпик", "!метка с заметкой")
    _ok(c.parse_command("привет всем") is None, "обычное сообщение — не команда")
    _ok(c.parse_command("!CLIP КАПС") == "КАПС", "регистр команды не важен")


def test_mark_time_and_author():
    print("\n[3] Метка: время от нуля + автор/роль:")
    c = MarkCollector("egoric", _tmp(), ref_epoch=1000.0)
    c.handle_message("egoric", {"badges": "broadcaster/1", "display-name": "Egoric"},
                     "!clip старт", now=1012.0)
    _ok(len(c.marks) == 1, "метка добавлена")
    m = c.marks[0]
    _ok(abs(m.t - 12.0) < 0.01, f"t = now-ref = 12с (={m.t})")
    _ok(m.type == A.STREAMER and m.author == "Egoric" and m.note == "старт", "роль/автор/заметка")


def test_cooldown():
    print("\n[4] Антиспам-кулдаун зрителей (доверенных не режем):")
    c = MarkCollector("egoric", _tmp(), viewer_cooldown_sec=30, ref_epoch=0.0)
    _ok(c.handle_message("vasya", {}, "!clip", now=100.0), "1-я метка зрителя — принята")
    _ok(c.handle_message("vasya", {}, "!clip", now=110.0) is None, "через 10с (<30) — отклонена")
    _ok(c.handle_message("vasya", {}, "!clip", now=140.0), "через 40с (>30) — снова принята")
    # у стримера кулдауна нет
    _ok(c.handle_message("egoric", {"badges": "broadcaster/1"}, "!clip", now=141.0), "стример без кулдауна 1")
    _ok(c.handle_message("egoric", {"badges": "broadcaster/1"}, "!clip", now=141.5), "стример без кулдауна 2")


def test_trusted_only():
    print("\n[5] Режим «только доверенные»:")
    c = MarkCollector("egoric", _tmp(), who_can_mark="trusted", ref_epoch=0.0)
    _ok(c.handle_message("vasya", {}, "!clip", now=10.0) is None, "зритель проигнорирован")
    _ok(c.handle_message("mod1", {"mod": "1"}, "!clip", now=11.0), "модер принят")


def test_file_roundtrip():
    print("\n[6] Запись .clipmarks читается приложением:")
    path = _tmp()
    c = MarkCollector("egoric", path, ref_epoch=0.0)
    c.set_online(180)
    c.handle_message("egoric", {"badges": "broadcaster/1"}, "!clip рофл", now=8.0)
    c.handle_message("vasya", {}, "!метка", now=25.0)
    mf = MarksFile.from_json(path)      # тот же загрузчик, что в приложении
    _ok(mf.streamer == "egoric" and mf.online == 180, "шапка: канал/онлайн")
    _ok(len(mf.marks) == 2 and mf.marks[0].type == A.STREAMER, "метки/роли на месте")
    from core.marks import select_moments
    _ok(len(select_moments(mf)) >= 1, "приложение делает из них моменты")


def test_parse_irc():
    print("\n[7] Разбор строки IRC:")
    line = ("@badges=broadcaster/1;display-name=Egoric;mod=0;vip=0 "
            ":egoric!egoric@egoric.tmi.twitch.tv PRIVMSG #egoric :!clip рофл про кота")
    tags, login, cmd, params, trailing = parse_irc(line)
    _ok(tags.get("display-name") == "Egoric" and "broadcaster/1" in tags.get("badges", ""), "теги разобраны")
    _ok(login == "egoric" and cmd == "PRIVMSG", "логин/команда")
    _ok(trailing == "!clip рофл про кота", "текст сообщения")
    # PING
    t2 = parse_irc("PING :tmi.twitch.tv")
    _ok(t2[2] == "PING", "PING распознан")


def _tmp() -> str:
    d = tempfile.mkdtemp(prefix="clipbot_")
    return os.path.join(d, "marks.clipmarks")


if __name__ == "__main__":
    test_roles()
    test_commands_and_note()
    test_mark_time_and_author()
    test_cooldown()
    test_trusted_only()
    test_file_roundtrip()
    test_parse_irc()
    print("\nВСЕ ПРОВЕРКИ ПРОШЛИ ✔")
