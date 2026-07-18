"""updater.py — встроенное обновление приложения.

Проверяет последний GitHub-релиз, сравнивает версии, качает новый установщик и
запускает его. Установщик Inno Setup ставится ПОВЕРХ (тот же AppId) — старую
версию заходить/удалять вручную не надо. Без Qt.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.request
from typing import Callable, Optional

from .version import __version__

log = logging.getLogger("clip_polisher.updater")

REPO = "GoLsik-web/clip-polisher"
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _ver_tuple(v: str) -> tuple:
    """'v1.2.3' → (1,2,3). Нечисловые хвосты игнорируем."""
    v = v.strip().lstrip("vV")
    out = []
    for part in v.split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    return tuple(out)


def is_newer(latest: str, current: str = __version__) -> bool:
    return _ver_tuple(latest) > _ver_tuple(current)


def current_version() -> str:
    return __version__


def latest_release(timeout: int = 15) -> Optional[dict]:
    """Инфо о последнем релизе: {'version','url','size','notes','newer'}.

    Возвращает None ТОЛЬКО при ошибке сети/данных (up-to-date — это валидный
    ответ с newer=False). Так меню обновлений различает «нет обновления» и «нет сети».
    """
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "User-Agent": "ClipPolisher", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    tag = data.get("tag_name", "") or data.get("name", "")
    asset_url = None
    asset_size = 0
    for a in data.get("assets", []):
        if a.get("name", "").lower().endswith(".exe"):
            asset_url = a.get("browser_download_url")
            asset_size = int(a.get("size", 0) or 0)
    if not tag:
        return None
    return {
        "version": tag.lstrip("vV"),
        "url": asset_url,
        "size": asset_size,
        "notes": (data.get("body") or "").strip(),
        "newer": bool(asset_url) and is_newer(tag),
    }


def check_for_update(timeout: int = 15) -> Optional[dict]:
    """Вернуть инфо релиза, если он НОВЕЕ текущего, иначе None (для тихой проверки)."""
    info = latest_release(timeout)
    return info if (info and info.get("newer")) else None


def download_installer(url: str, dst: str,
                       on_bytes: Optional[Callable[[int, int], None]] = None) -> str:
    """Скачать установщик обновления (переиспользуем атомарную загрузку provision)."""
    from .provision import _download
    _download(url, dst, on_bytes)
    return dst


def launch_installer(path: str) -> None:
    """Запустить скачанный установщик (обычный мастер) и вернуть управление.

    Вызывающий ДОЛЖЕН тут же закрыть приложение, чтобы установщик мог заменить
    файлы (в .iss включён CloseApplications=yes как подстраховка)."""
    subprocess.Popen([path], creationflags=_CREATE_NO_WINDOW)
