"""Боты для сотрудников: MAX (основной) и Telegram (запасной)."""
from __future__ import annotations

import re

from .base import (OK, WARN, CheckContext, CheckResult, ConnectorError, ConnectorType, Field, details_for,
                   http_request, json_or_none)

# ----------------------------------------------------------------------- MAX

MAX_API = "https://platform-api.max.ru"
MAX_ACTION = (
    "Откройте в MAX чат с @MasterBot (платформа для партнёров: business.max.ru → «Чат-боты»), "
    "выберите своего бота, скопируйте токен заново и вставьте сюда."
)


async def check_max(values: dict, ctx: CheckContext) -> CheckResult:
    resp = await http_request("GET", MAX_API + "/me", ctx, service="MAX",
                              headers={"Authorization": values.get("token", "")})
    data = json_or_none(resp) or {}
    if resp.status_code == 200 and isinstance(data, dict):
        name = data.get("name") or data.get("first_name") or "бот"
        username = data.get("username")
        return CheckResult(OK, f"Бот MAX «{name}»{' (@' + username + ')' if username else ''} на связи.")
    if resp.status_code in (401, 403):
        raise ConnectorError("MAX не принял токен бота.", MAX_ACTION, details_for(resp))
    if resp.status_code == 429:
        return CheckResult(WARN, "MAX просит подождать: слишком частые запросы.",
                           "Повторите проверку через минуту.", details_for(resp))
    raise ConnectorError(f"MAX ответил ошибкой (код {resp.status_code}).",
                         "Повторите проверку через несколько минут.", details_for(resp),
                         status=WARN if resp.status_code >= 500 else "error")


MAX_BOT = ConnectorType(
    key="max_bot",
    title="Бот MAX",
    category="Боты",
    description="Основной бот для сотрудников в мессенджере MAX: через него задают вопросы "
                "и получают ответы из всех систем.",
    singleton=True,
    name_placeholder="Бот MAX",
    fields=[
        Field("token", "Токен бота", "secret", required=True, ascii_only=True,
              hint="Выдаётся при создании бота на платформе MAX для партнёров."),
    ],
    check=check_max,
    help_steps=[
        "Бота в MAX может создать только организация: зарегистрируйте компанию на business.max.ru "
        "(понадобится вход через Госуслуги/ИНН).",
        "В кабинете партнёра: «Чат-боты» → «Создать бота», задайте имя и адрес (@...).",
        "После модерации откройте бота → «Интеграция» → «Токен» и скопируйте его сюда.",
    ],
)

# ------------------------------------------------------------------ Telegram

TG_TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$")
TG_ACTION = (
    "Откройте в Telegram @BotFather → /mybots → выберите бота → «API Token». Если токен "
    "перевыпускали («Revoke»), старый больше не работает — вставьте новый."
)


def validate_telegram(values: dict) -> dict:
    token = values.get("token", "")
    if token.lower().startswith("bot") and TG_TOKEN_RE.match(token[3:]):
        values["token"] = token = token[3:]
    if token and not TG_TOKEN_RE.match(token):
        return {"token": "Это не похоже на токен Telegram. Он выглядит так: 1234567890:AAH... "
                         "(цифры, двоеточие и длинная строка). Скопируйте его из @BotFather целиком."}
    return {}


async def check_telegram(values: dict, ctx: CheckContext) -> CheckResult:
    token = values.get("token", "")
    if not TG_TOKEN_RE.match(token):
        raise ConnectorError("Токен Telegram указан в неверном формате.", TG_ACTION)
    resp = await http_request("GET", f"https://api.telegram.org/bot{token}/getMe", ctx,
                              service="Telegram", use_proxy=True, timeout=15)
    data = json_or_none(resp) or {}
    if resp.status_code == 200 and data.get("ok"):
        bot = data.get("result") or {}
        return CheckResult(OK, f"Бот Telegram @{bot.get('username', '?')} на связи.")
    if resp.status_code in (401, 404):
        raise ConnectorError("Telegram не принял токен бота: он неверный или перевыпущен.", TG_ACTION,
                             f"HTTP {resp.status_code}")
    raise ConnectorError(f"Telegram ответил ошибкой (код {resp.status_code}).",
                         "Повторите проверку через несколько минут.", f"HTTP {resp.status_code}", status=WARN)


async def check_telegram_safe(values: dict, ctx: CheckContext) -> CheckResult:
    """В адресе запроса к Telegram есть токен — не даём ему попасть в подробности ошибки."""
    try:
        return await check_telegram(values, ctx)
    except ConnectorError as exc:
        token = values.get("token", "")
        if token:
            exc.details = exc.details.replace(token, "***")
        if "не удаётся соединиться" in exc.message.lower() or "не ответил" in exc.message.lower():
            exc.action = ("Сервер не достаёт до api.telegram.org — в России Telegram часто недоступен "
                          "напрямую. Укажите прокси в разделе «Настройки» или пользуйтесь ботом MAX.")
        raise exc


TELEGRAM_BOT = ConnectorType(
    key="telegram_bot",
    title="Бот Telegram",
    category="Боты",
    description="Запасной бот в Telegram — на случай, если MAX недоступен.",
    singleton=True,
    foreign=True,
    name_placeholder="Бот Telegram",
    fields=[
        Field("token", "Токен бота", "secret", required=True, ascii_only=True,
              placeholder="1234567890:AAH...", hint="Выдаёт @BotFather при создании бота."),
    ],
    validate=validate_telegram,
    check=check_telegram_safe,
    help_steps=[
        "Откройте в Telegram бота @BotFather и отправьте ему /newbot.",
        "Придумайте имя (как бот будет называться) и адрес — должен заканчиваться на bot.",
        "BotFather пришлёт токен вида 1234567890:AAH… — скопируйте его сюда.",
        "Если сервер в России и Telegram с него не открывается — укажите прокси в «Настройках».",
    ],
)
