"""resources.py — абсолютные пути к вложенным ресурсам (шрифты/иконки/пресеты).

В dev корень — папка проекта; в собранном .exe (PyInstaller) — sys._MEIPASS.
Без этого относительные пути вроде "assets/fonts" ломались бы, если приложение
запущено не из своей папки (а в exe так и есть).
"""
from __future__ import annotations

import os
import sys


def resource_root() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    # core/resources.py → корень проекта на уровень выше папки core/
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def res(rel: str) -> str:
    """Абсолютный путь к ресурсу по относительному (напр. 'assets/fonts')."""
    return os.path.join(resource_root(), *rel.split("/"))
