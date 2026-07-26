"""twitch_auth.py — вход в Twitch по кнопке (OAuth Device Code Flow).

Зачем: раздаваемой программе НЕЛЬЗЯ вшивать client_secret, а обычному стримеру нельзя
предлагать «сгенерируй токен на стороннем сайте». Device Code Flow решает оба вопроса:
приложение показывает короткий код, юзер подтверждает вход на twitch.tv/activate, и мы
получаем ПОЛЬЗОВАТЕЛЬСКИЙ токен. Такой токен годится и для чата (chat:read/chat:edit),
и для Helix (онлайн канала, старт эфира) — секрет не нужен.

Только стандартная библиотека (без Qt, без requests) — модуль спокойно живёт в .exe.

Токены лежат в %LOCALAPPDATA%\\ClipPolisher\\twitch_auth.json (у каждого юзера свои,
в git/установщик не попадают).

Схема использования:

    flow = DeviceFlow()
    dc = flow.start()                  # dc.user_code, dc.verification_uri — показать юзеру
    tok = flow.wait(should_stop=...)   # ждём подтверждения (блокирует поток)
    save_tokens(tok)

    token = ensure_token()             # дальше — просто берём валидный токен
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from core.provision import app_data_dir

# Client ID приложения «Clip Polisher» на dev.twitch.tv (ПУБЛИЧНЫЙ клиент).
# Это не секрет: он открыто ездит в каждом запросе, его вшивают все чат-боты.
CLIENT_ID = "hcvws331csxduqidpoccvuqbqxu8eh"

# Права: читать чат и писать в чат. Больше боту меток ничего не нужно —
# чем короче список, тем спокойнее юзеру на экране подтверждения.
SCOPES = ["chat:read", "chat:edit"]

ID_BASE = "https://id.twitch.tv/oauth2"
HELIX = "https://api.twitch.tv/helix"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

TOKENS_FILE = "twitch_auth.json"
_UA = "ClipPolisher/1.0"


# --------------------------------------------------------------------------
# Мелкий HTTP
# --------------------------------------------------------------------------

def _post(url: str, fields: dict, timeout: float = 20.0) -> tuple[int, dict]:
    """POST form-urlencoded → (http-код, json). Ошибки 4xx возвращаем, а не бросаем:
    в device flow «ещё не подтвердил» приходит именно как 400."""
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except ValueError:
            return e.code, {"message": body}


def _get(url: str, headers: dict, timeout: float = 20.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={**headers, "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except ValueError:
            return e.code, {"message": body}


# --------------------------------------------------------------------------
# Хранение токенов
# --------------------------------------------------------------------------

def tokens_path() -> str:
    return os.path.join(app_data_dir(), TOKENS_FILE)


def save_tokens(tok: dict) -> None:
    """Сохранить токены (атомарно). Ожидаются ключи access_token/refresh_token/expires_at."""
    path = tokens_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tok, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:                      # не даём файлу быть «для всех» на всякий случай
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_tokens() -> Optional[dict]:
    try:
        with open(tokens_path(), encoding="utf-8") as f:
            tok = json.load(f)
        return tok if tok.get("access_token") else None
    except (OSError, ValueError):
        return None


def clear_tokens() -> None:
    try:
        os.remove(tokens_path())
    except OSError:
        pass


def logout() -> None:
    """Выйти: отозвать токен на стороне Twitch и стереть локальный файл."""
    tok = load_tokens()
    if tok and tok.get("access_token"):
        try:
            _post(f"{ID_BASE}/revoke",
                  {"client_id": CLIENT_ID, "token": tok["access_token"]}, timeout=10)
        except OSError:
            pass          # нет сети — просто забываем локально
    clear_tokens()


# --------------------------------------------------------------------------
# Device Code Flow
# --------------------------------------------------------------------------

@dataclass
class DeviceCode:
    device_code: str
    user_code: str            # то, что юзер вводит/видит («MYBGVSBY»)
    verification_uri: str     # ссылка на страницу подтверждения
    interval: float           # как часто опрашивать (сек)
    expires_at: float         # когда код протухнет (epoch)

    @property
    def seconds_left(self) -> int:
        return max(0, int(self.expires_at - time.time()))


class AuthError(RuntimeError):
    """Вход не удался (юзер отказал, код протух, нет сети)."""


class DeviceFlow:
    """Одна попытка входа: start() → показать код → wait() → токены."""

    def __init__(self, scopes: Optional[list[str]] = None, client_id: str = CLIENT_ID):
        self.client_id = client_id
        self.scopes = scopes or SCOPES
        self.code: Optional[DeviceCode] = None

    def start(self) -> DeviceCode:
        st, j = _post(f"{ID_BASE}/device",
                      {"client_id": self.client_id, "scopes": " ".join(self.scopes)})
        if st != 200 or "device_code" not in j:
            raise AuthError(j.get("message") or f"Twitch ответил {st}")
        self.code = DeviceCode(
            device_code=j["device_code"], user_code=j.get("user_code", ""),
            verification_uri=j.get("verification_uri")
            or f"https://www.twitch.tv/activate?device-code={j.get('user_code','')}",
            interval=float(j.get("interval") or 5),
            expires_at=time.time() + float(j.get("expires_in") or 1800))
        return self.code

    def poll_once(self) -> Optional[dict]:
        """Один опрос: токены (готово) или None (юзер ещё не подтвердил)."""
        if not self.code:
            raise AuthError("сначала start()")
        st, j = _post(f"{ID_BASE}/token", {
            "client_id": self.client_id, "device_code": self.code.device_code,
            "grant_type": DEVICE_GRANT, "scopes": " ".join(self.scopes)})
        if st == 200 and j.get("access_token"):
            return _with_expiry(j)
        msg = (j.get("message") or j.get("error") or "").lower()
        if "expired" in msg:
            raise AuthError("Код истёк — нажми «Войти через Twitch» ещё раз.")
        if "denied" in msg or "declined" in msg:
            raise AuthError("Вход отклонён в браузере.")
        if "slow" in msg:                  # просят опрашивать реже
            self.code.interval += 1
            return None
        if st == 400:
            # Штатное «юзер ещё не подтвердил» (authorization_pending); пустое тело
            # трактуем так же — ожидание всё равно ограничено сроком жизни кода.
            return None
        raise AuthError(j.get("message") or f"Twitch ответил {st}")

    def wait(self, should_stop: Optional[Callable[[], bool]] = None,
             on_tick: Optional[Callable[[int], None]] = None) -> dict:
        """Ждать подтверждения. should_stop — отмена, on_tick(осталось_сек) — для UI."""
        if not self.code:
            self.start()
        while True:
            if should_stop and should_stop():
                raise AuthError("Вход отменён.")
            if self.code.seconds_left <= 0:
                raise AuthError("Код истёк — нажми «Войти через Twitch» ещё раз.")
            tok = self.poll_once()
            if tok:
                return tok
            if on_tick:
                on_tick(self.code.seconds_left)
            # Ждём порциями по 0.5 с, чтобы «Отмена» срабатывала сразу.
            end = time.time() + self.code.interval
            while time.time() < end:
                if should_stop and should_stop():
                    raise AuthError("Вход отменён.")
                time.sleep(0.5)


def _with_expiry(j: dict) -> dict:
    return {"access_token": j["access_token"],
            "refresh_token": j.get("refresh_token", ""),
            "scope": j.get("scope") or [],
            "expires_at": time.time() + float(j.get("expires_in") or 3600)}


# --------------------------------------------------------------------------
# Валидация / обновление токена
# --------------------------------------------------------------------------

def validate(access_token: str) -> Optional[dict]:
    """Проверить токен у Twitch → {login, user_id, scopes, expires_in} или None.

    Таймаут короткий: этот запрос делается при запуске программы и при закрытии окна
    его приходится ждать — 20 секунд ожидания на выходе никому не нужны.
    """
    st, j = _get(f"{ID_BASE}/validate", {"Authorization": "OAuth " + access_token},
                 timeout=8.0)
    if st == 200 and j.get("login"):
        return {"login": j["login"], "user_id": j.get("user_id", ""),
                "scopes": j.get("scopes") or [], "expires_in": j.get("expires_in", 0)}
    return None


def refresh(refresh_token: str, client_id: str = CLIENT_ID) -> Optional[dict]:
    """Обновить протухший токен по refresh_token (для публичного клиента — без секрета)."""
    if not refresh_token:
        return None
    st, j = _post(f"{ID_BASE}/token", {
        "client_id": client_id, "grant_type": "refresh_token",
        "refresh_token": refresh_token})
    if st == 200 and j.get("access_token"):
        out = _with_expiry(j)
        if not out["refresh_token"]:
            out["refresh_token"] = refresh_token
        return out
    return None


def ensure_token() -> Optional[str]:
    """Валидный access-токен из хранилища (сам обновит, если протух). None — нужен вход."""
    tok = load_tokens()
    if not tok:
        return None
    # Небольшой запас: если жить осталось <5 мин — сразу обновляем.
    if tok.get("expires_at", 0) - time.time() > 300 and validate(tok["access_token"]):
        return tok["access_token"]
    new = refresh(tok.get("refresh_token", ""))
    if new:
        save_tokens({**tok, **new})
        return new["access_token"]
    # Не вышло обновить — но, может, токен ещё живой (часы могли сбиться).
    if validate(tok.get("access_token", "")):
        return tok["access_token"]
    clear_tokens()
    return None


def current_account() -> Optional[dict]:
    """Кто вошёл: {login, user_id} — или None, если входа нет."""
    tok = ensure_token()
    if not tok:
        return None
    info = validate(tok)
    if not info:
        return None
    return {"login": info["login"], "user_id": info["user_id"], "token": tok}


# --------------------------------------------------------------------------
# Helix с пользовательским токеном (онлайн канала / старт эфира)
# --------------------------------------------------------------------------

def helix_stream(access_token: str, channel: str) -> Optional[dict]:
    """Инфо о текущем эфире канала (None — офлайн/ошибка). Секрет не нужен."""
    url = f"{HELIX}/streams?user_login=" + urllib.parse.quote(channel)
    st, j = _get(url, {"Client-Id": CLIENT_ID, "Authorization": "Bearer " + access_token})
    if st != 200:
        return None
    data = j.get("data") or []
    return data[0] if data else None


def helix_user(access_token: str, login: str = "") -> Optional[dict]:
    """Данные пользователя (по умолчанию — владельца токена)."""
    url = f"{HELIX}/users" + (f"?login={urllib.parse.quote(login)}" if login else "")
    st, j = _get(url, {"Client-Id": CLIENT_ID, "Authorization": "Bearer " + access_token})
    if st != 200:
        return None
    data = j.get("data") or []
    return data[0] if data else None
