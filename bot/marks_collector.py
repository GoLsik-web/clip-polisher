"""marks_collector.py — обёртка совместимости.

Ядро бота меток переехало в `core/chatbot.py` (чтобы бот работал ВНУТРИ приложения и
попадал в .exe). Этот модуль оставлен, чтобы старые команды и тесты
(`from bot.marks_collector import MarkCollector`) продолжали работать.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.chatbot import MarkCollector, role_from_tags  # noqa: E402,F401

__all__ = ["MarkCollector", "role_from_tags"]
