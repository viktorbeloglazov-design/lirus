"""Проверка подписи данных запуска мини-приложения (WebApp.initData).

Алгоритм как в документации MAX (dev.max.ru → Мини-приложения → Валидация):
secret = HMAC-SHA256(ключ «WebAppData», сообщение — токен бота);
подпись = HMAC-SHA256(secret, строка «ключ=значение» по алфавиту через перевод строки, без hash).
Если подпись совпала с параметром hash — данные прислал MAX, а не кто-то с улицы.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE = 24 * 3600  # мини-приложение может быть открыто долго; сутки — разумный срок


class InitDataError(Exception):
    pass


def sign(params: dict[str, str], bot_token: str) -> str:
    check_string = "\n".join(f"{k}={params[k]}" for k in sorted(params) if k != "hash")
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()


def validate(init_data: str, bot_token: str, max_age: int = MAX_AGE, now: float | None = None) -> dict:
    """Возвращает словарь: user (id, name, username, ...), start_param, auth_date. Иначе InitDataError."""
    if not init_data or not init_data.strip():
        raise InitDataError("Нет данных запуска: приложение открыто не из MAX.")
    if not bot_token:
        raise InitDataError("Не настроен бот MAX: нечем проверить подпись.")
    params = dict(parse_qsl(init_data.strip(), keep_blank_values=True))
    received = params.get("hash", "")
    if not received:
        raise InitDataError("В данных запуска нет подписи.")
    if not hmac.compare_digest(sign(params, bot_token), received):
        raise InitDataError("Подпись данных запуска не сошлась.")
    try:
        auth_date = int(params.get("auth_date") or 0)
    except ValueError:
        auth_date = 0
    if auth_date and max_age and (now or time.time()) - auth_date > max_age:
        raise InitDataError("Данные запуска устарели: закройте и откройте приложение заново.")
    try:
        raw_user = json.loads(params.get("user") or "{}")
    except json.JSONDecodeError:
        raw_user = {}
    if not isinstance(raw_user, dict):
        raw_user = {}
    user_id = raw_user.get("id") or raw_user.get("user_id")
    if not user_id:
        raise InitDataError("В данных запуска нет пользователя.")
    name = " ".join(x for x in (raw_user.get("first_name"), raw_user.get("last_name")) if x).strip()
    return {
        "user": {
            "id": str(user_id),
            "name": name or str(raw_user.get("name") or ""),
            "username": str(raw_user.get("username") or ""),
            "photo": str(raw_user.get("photo_url") or ""),
        },
        "start_param": params.get("start_param") or params.get("WebAppStartParam") or "",
        "auth_date": auth_date,
        "query_id": params.get("query_id") or "",
    }
