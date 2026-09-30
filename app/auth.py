"""Вход, сессии, роли и проверка прав.

Все проверки прав выполняются на сервере: даже если человек наберёт адрес
раздела руками, без нужной роли он получит отказ.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from . import db, store

SESSION_COOKIE = "cp_session"
SESSION_TTL = 12 * 3600  # сессия живёт 12 часов с последнего действия

ROLES = {
    "admin": "Администратор",
    "director": "Руководитель",
    "manager": "Менеджер",
    "employee": "Сотрудник",
}

ROLE_DESCRIPTIONS = {
    "admin": "Настраивает платформу: подключения, сценарии, сотрудники, проверка, настройки. Видит всё.",
    "director": "Видит обзор, финансы и журнал. В боте получает ответы по всем данным, включая деньги.",
    "manager": "Видит обзор без финансов. В боте — продажи, остатки, клиенты, задачи; "
               "выручка, прибыль, себестоимость и расчёты скрыты.",
    "employee": "Панелью обычно не пользуется. В боте — остатки, задачи, документы.",
}

# Права на разделы панели.
PERMISSIONS: dict[str, set[str]] = {
    "overview": {"admin", "director", "manager", "employee"},
    "finance": {"admin", "director"},
    "journal": {"admin", "director"},
    "connections": {"admin"},
    "workflows": {"admin"},
    "employees": {"admin"},
    "testing": {"admin"},
    "settings": {"admin"},
}

# Категории данных, которые бот будет отдавать по ролям (этап 4).
DATA_SCOPES: dict[str, set[str]] = {
    "admin": {"sales", "stock", "clients", "tasks", "documents", "mail", "finance"},
    "director": {"sales", "stock", "clients", "tasks", "documents", "mail", "finance"},
    "manager": {"sales", "stock", "clients", "tasks", "documents", "mail"},
    "employee": {"stock", "tasks", "documents"},
}


def can(user, perm: str) -> bool:
    return bool(user) and user["role"] in PERMISSIONS.get(perm, set())


# ----------------------------------------------------------------- пароли

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, salt_hex, digest_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def password_problem(password: str, login: str = "") -> str:
    """Пустая строка, если пароль годится; иначе — что с ним не так."""
    if len(password) < 8:
        return "Пароль должен быть не короче 8 символов."
    if password.lower() in {"admin", "password", "12345678", "qwerty123", "11111111", "админ123"}:
        return "Этот пароль слишком простой — его подберут за секунды. Придумайте другой."
    if login and password.lower() == login.lower():
        return "Пароль не должен совпадать с логином."
    return ""


def ensure_default_admin() -> bool:
    """При первом запуске создаёт admin/admin с требованием сменить пароль."""
    row = db.query_one("SELECT COUNT(*) AS n FROM users")
    if row["n"]:
        return False
    db.execute(
        "INSERT INTO users(login, full_name, role, password_hash, must_change_password, created_at) "
        "VALUES('admin', 'Администратор', 'admin', ?, 1, ?)",
        (hash_password("admin"), time.time()),
    )
    store.log_event("warning", "auth", "Создан вход admin с паролем admin. Смените пароль при первом входе.")
    return True


# ------------------------------------------------ защита от подбора пароля

_failures: dict[str, deque] = defaultdict(deque)
FAIL_LIMIT = 5
FAIL_WINDOW = 10 * 60


def login_blocked(key: str) -> int:
    """Сколько секунд осталось до разблокировки (0 — не заблокирован)."""
    q = _failures[key]
    now = time.time()
    while q and now - q[0] > FAIL_WINDOW:
        q.popleft()
    if len(q) >= FAIL_LIMIT:
        return int(FAIL_WINDOW - (now - q[0])) + 1
    return 0


def register_failure(key: str) -> None:
    _failures[key].append(time.time())


def clear_failures(key: str) -> None:
    _failures.pop(key, None)


# ----------------------------------------------------------------- сессии

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int, ip: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.execute(
        "INSERT INTO sessions(token_hash, user_id, csrf, created_at, expires_at, ip) VALUES(?,?,?,?,?,?)",
        (_hash_token(token), user_id, secrets.token_urlsafe(24), now, now + SESSION_TTL, ip),
    )
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return token


def destroy_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def destroy_user_sessions(user_id: int, keep_token: str | None = None) -> None:
    if keep_token:
        db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                   (user_id, _hash_token(keep_token)))
    else:
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def load_session(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None, None
    row = db.query_one(
        "SELECT s.token_hash, s.csrf, s.expires_at, s.flash, u.* FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (_hash_token(token),),
    )
    if not row or row["expires_at"] < time.time() or not row["active"]:
        return None, None
    db.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
               (time.time() + SESSION_TTL, row["token_hash"]))
    return row, token


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )


# ------------------------------------------------------------- сообщения

def flash(request: Request, kind: str, text: str) -> None:
    """Сообщение, которое покажется на следующей странице (ok / warn / error)."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return
    h = _hash_token(token)
    row = db.query_one("SELECT flash FROM sessions WHERE token_hash = ?", (h,))
    if not row:
        return
    items = json.loads(row["flash"] or "[]")
    items.append({"kind": kind, "text": text})
    db.execute("UPDATE sessions SET flash = ? WHERE token_hash = ?", (json.dumps(items, ensure_ascii=False), h))


def pop_flash(request: Request) -> list[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return []
    h = _hash_token(token)
    row = db.query_one("SELECT flash FROM sessions WHERE token_hash = ?", (h,))
    if not row or row["flash"] in ("", "[]"):
        return []
    db.execute("UPDATE sessions SET flash = '[]' WHERE token_hash = ?", (h,))
    return json.loads(row["flash"])


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


# ------------------------------------------------------------ декоратор

def page(perm: str = "overview", allow_must_change: bool = False):
    """Оборачивает обработчик страницы: вход, смена пароля, права, защита форм."""

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(request: Request):
            from .main import render_error  # поздний импорт, чтобы избежать цикла

            user, token = load_session(request)
            if user is None:
                if request.method == "GET":
                    nxt = request.url.path
                    if request.url.query:
                        nxt += "?" + request.url.query
                    from urllib.parse import quote
                    return RedirectResponse("/login?next=" + quote(nxt), status_code=303)
                return RedirectResponse("/login", status_code=303)
            request.state.user = user
            request.state.csrf = user["csrf"]
            request.state.token = token
            if user["must_change_password"] and not allow_must_change:
                return RedirectResponse("/password", status_code=303)
            if not can(user, perm):
                store.log_event("warning", "auth",
                                f"Отказано в доступе к «{request.url.path}»: не хватает прав",
                                user=user["login"], ip=client_ip(request))
                return render_error(
                    request, 403, "Нет доступа",
                    "Этот раздел доступен только сотрудникам с другой ролью.",
                    "Если доступ нужен по работе — попросите администратора поменять вам роль "
                    "в разделе «Сотрудники».",
                )
            if request.method == "POST":
                form = await request.form()
                request.state.form = form
                sent = form.get("csrf", "")
                if not isinstance(sent, str) or not hmac.compare_digest(sent, user["csrf"]):
                    return render_error(
                        request, 400, "Страница устарела",
                        "Форма была открыта слишком давно или в другой вкладке.",
                        "Обновите страницу (клавиша F5) и повторите действие.",
                    )
            return await func(request)

        return wrapper

    return decorator
