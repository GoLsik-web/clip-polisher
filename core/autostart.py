"""autostart.py — запуск программы вместе с Windows (для автопилота бота).

Автопилот может поймать начало эфира только если программа запущена. Чтобы стример
не вспоминал о ней каждый раз, приложение умеет прописываться в автозапуск текущего
пользователя и стартовать свёрнутым в трей (ключ `--tray`).

Реестр: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run, значение ClipPolisher.
Права администратора НЕ нужны (ветка текущего пользователя), как и у установщика.

Без Qt. На не-Windows всё превращается в «выключено» и молча ничего не делает.
"""
from __future__ import annotations

import os
import sys

APP_KEY = "ClipPolisher"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TRAY_FLAG = "--tray"

IS_WINDOWS = os.name == "nt"


def is_frozen() -> bool:
    """True — работаем из собранного .exe (PyInstaller), False — из исходников."""
    return bool(getattr(sys, "frozen", False))


def launch_command() -> str:
    """Команда автозапуска — то, что пропишем в реестр (с кавычками для пробелов)."""
    if is_frozen():
        return f'"{sys.executable}" {TRAY_FLAG}'
    # Из исходников: тем же интерпретатором, но без чёрного окна консоли (pythonw).
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    if os.path.isfile(pyw):
        py = pyw
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "app.py")
    return f'"{py}" "{script}" {TRAY_FLAG}'


def _open_key(write: bool = False):
    import winreg
    access = winreg.KEY_SET_VALUE if write else winreg.KEY_READ
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def is_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with _open_key() as key:
            value, _type = winreg.QueryValueEx(key, APP_KEY)
        return bool(value)
    except OSError:
        return False


def current_command() -> str:
    if not IS_WINDOWS:
        return ""
    import winreg
    try:
        with _open_key() as key:
            value, _type = winreg.QueryValueEx(key, APP_KEY)
        return str(value)
    except OSError:
        return ""


def enable() -> bool:
    """Прописать автозапуск. True — получилось."""
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with _open_key(write=True) as key:
            winreg.SetValueEx(key, APP_KEY, 0, winreg.REG_SZ, launch_command())
        return True
    except OSError:
        return False


def disable() -> bool:
    """Убрать из автозапуска. True — записи больше нет."""
    if not IS_WINDOWS:
        return True
    import winreg
    try:
        with _open_key(write=True) as key:
            winreg.DeleteValue(key, APP_KEY)
        return True
    except FileNotFoundError:
        return True          # её и не было
    except OSError:
        return False


def set_enabled(on: bool) -> bool:
    return enable() if on else disable()


def refresh_if_enabled() -> None:
    """Обновить путь в автозапуске, если программа переехала (например, обновилась)."""
    if is_enabled() and current_command() != launch_command():
        enable()


def started_in_tray(argv: list[str] | None = None) -> bool:
    """Программу запустили автозапуском (значит, стартуем свёрнутыми)."""
    return TRAY_FLAG in (argv if argv is not None else sys.argv)
