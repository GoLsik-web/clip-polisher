"""profiles.py — именованные профили стримеров (зоны/ник/платформа).

Профиль хранит настройки под конкретного стримера, чтобы не размечать зоны и не
вводить ник каждый раз: выбрал профиль из списка — подтянулись его зоны камеры/
геймплея, ник и платформа.

Хранение — ЛОКАЛЬНО у пользователя, в `%APPDATA%\\ClipPolisher\\profiles.json`
(Roaming — настройки пользователя, а не кэш; кэш модели/CUDA лежит в LOCALAPPDATA).
Файл — простой JSON: {имя_профиля: {nickname, platform, composition, safezone}}.

Модуль без Qt — чистое чтение/запись JSON. Что класть в поле профиля, решает UI
(словарь произвольной формы), здесь — только надёжная персистентность.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger("clip_polisher.profiles")


def profiles_dir() -> str:
    """Папка с настройками пользователя (%APPDATA%\\ClipPolisher, с фолбэком)."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "ClipPolisher")


def profiles_path() -> str:
    return os.path.join(profiles_dir(), "profiles.json")


def load_all() -> dict[str, dict]:
    """Прочитать все профили. При отсутствии/битом файле — пустой словарь (не падаем)."""
    path = profiles_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # только словари-значения (защита от мусора)
            return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError) as e:
        log.warning("Не удалось прочитать профили (%s): %s", path, e)
    return {}


def save_all(profiles: dict[str, dict]) -> None:
    """Записать все профили (атомарно: во временный файл, затем replace)."""
    os.makedirs(profiles_dir(), exist_ok=True)
    path = profiles_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    log.info("Профили сохранены: %s (%d шт.)", path, len(profiles))


def names() -> list[str]:
    """Имена профилей по алфавиту (регистронезависимо)."""
    return sorted(load_all().keys(), key=str.lower)


def get(name: str) -> dict | None:
    return load_all().get(name)


def save(name: str, data: dict[str, Any]) -> None:
    """Создать/обновить профиль. Имя нормализуем (без крайних пробелов)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Пустое имя профиля")
    profiles = load_all()
    profiles[name] = data
    save_all(profiles)


def delete(name: str) -> bool:
    """Удалить профиль. True — если был и удалён."""
    profiles = load_all()
    if name in profiles:
        del profiles[name]
        save_all(profiles)
        return True
    return False


def rename(old: str, new: str) -> None:
    new = (new or "").strip()
    if not new:
        raise ValueError("Пустое имя профиля")
    profiles = load_all()
    if old in profiles:
        profiles[new] = profiles.pop(old)
        save_all(profiles)
