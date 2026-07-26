"""dev/test_autostart.py — проверка автозапуска Windows (реестр) и разбора --tray.

Пишет и стирает СВОЙ ключ в HKCU\\...\\Run, возвращая всё как было: если автозапуск
у пользователя уже включён, после теста он останется включённым.

Запуск: .venv\\Scripts\\python.exe -m dev.test_autostart
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import autostart  # noqa: E402


def _ok(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    assert cond, msg


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    print("[1] Команда автозапуска:")
    cmd = autostart.launch_command()
    print("   ", cmd)
    _ok(autostart.TRAY_FLAG in cmd, "в команде есть ключ --tray (стартуем свёрнутыми)")
    _ok(cmd.startswith('"'), "путь в кавычках — переживёт пробелы в пути")
    if not autostart.is_frozen():
        _ok("pythonw" in cmd.lower() or "python" in cmd.lower(),
            "из исходников запускаем тем же интерпретатором")

    print("\n[2] Разбор ключа запуска:")
    _ok(autostart.started_in_tray(["app.py", "--tray"]), "--tray распознан")
    _ok(not autostart.started_in_tray(["app.py"]), "без ключа — обычный запуск")

    print("\n[3] Реестр (HKCU, без прав админа):")
    was = autostart.is_enabled()
    print(f"    было включено: {was}")
    try:
        _ok(autostart.enable(), "включение автозапуска прошло")
        _ok(autostart.is_enabled(), "Windows видит запись автозапуска")
        _ok(autostart.current_command() == cmd, "в реестре ровно наша команда")
        _ok(autostart.disable(), "выключение прошло")
        _ok(not autostart.is_enabled(), "записи больше нет")
        _ok(autostart.disable(), "повторное выключение не падает")
    finally:
        autostart.set_enabled(was)          # возвращаем как было
        _ok(autostart.is_enabled() == was, f"состояние пользователя восстановлено ({was})")

    print("\nВСЕ ПРОВЕРКИ ПРОШЛИ ✔")


if __name__ == "__main__":
    main()
