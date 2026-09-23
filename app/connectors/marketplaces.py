"""Маркетплейсы: Wildberries, Ozon, Яндекс Маркет. Одно подключение — один кабинет."""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime

from .base import (ERROR, OK, WARN, CheckContext, CheckResult, ConnectorError, ConnectorType, Field,
                   details_for, http_request, json_or_none)

# ---------------------------------------------------------------- Wildberries

WB_SELLER_INFO = "https://common-api.wildberries.ru/api/v1/seller-info"
WB_ACTION_NEW = (
    "Создайте новый токен: личный кабинет WB → «Профиль» (справа вверху) → «Интеграции по API» → "
    "«Создать новый токен». Отметьте нужные категории (как минимум: Контент, Статистика, Аналитика, "
    "Цены и скидки, Маркетплейс, Финансы) и вставьте токен сюда."
)


def _wb_payload(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None


def validate_wb(values: dict) -> dict:
    token = values.get("token", "")
    if token and _wb_payload(token) is None:
        return {"token": "Это не похоже на токен Wildberries. Токен WB — длинная строка из трёх частей, "
                         "разделённых точками (начинается обычно с eyJ). Скопируйте его целиком."}
    return {}


def _fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


async def check_wb(values: dict, ctx: CheckContext) -> CheckResult:
    token = values.get("token", "")
    payload = _wb_payload(token)
    if payload is None:
        raise ConnectorError("Токен Wildberries неполный или испорчен.", WB_ACTION_NEW)
    exp = payload.get("exp")
    note, note_status = "", OK
    if isinstance(exp, (int, float)):
        if exp < time.time():
            raise ConnectorError(f"Срок действия токена Wildberries истёк {_fmt_date(exp)}.", WB_ACTION_NEW)
        days = int((exp - time.time()) // 86400)
        if days < 14:
            note, note_status = f" Токен истекает через {days} дн. ({_fmt_date(exp)}).", WARN
    resp = await http_request("GET", WB_SELLER_INFO, ctx, service="Wildberries",
                              headers={"Authorization": token})
    data = json_or_none(resp) or {}
    if resp.status_code == 200:
        name = data.get("name") or data.get("tradeMark") or "кабинет"
        return CheckResult(note_status, f"Wildberries: подключён кабинет «{name}».{note}",
                           WB_ACTION_NEW if note_status == WARN else "")
    detail = str(data.get("detail") or data.get("title") or "").lower() if isinstance(data, dict) else ""
    if resp.status_code == 401:
        if "expired" in detail:
            raise ConnectorError("Срок действия токена Wildberries истёк.", WB_ACTION_NEW, details_for(resp))
        if "withdrawn" in detail or "revoked" in detail:
            raise ConnectorError("Токен Wildberries отозван (удалён в кабинете).", WB_ACTION_NEW, details_for(resp))
        raise ConnectorError("Wildberries не принял токен.", WB_ACTION_NEW, details_for(resp))
    if resp.status_code == 403:
        raise ConnectorError("У токена Wildberries не хватает прав.", WB_ACTION_NEW, details_for(resp))
    if resp.status_code == 429:
        return CheckResult(WARN, "Wildberries ограничивает частоту проверок (не чаще раза в минуту).",
                           "Подождите минуту и нажмите «Проверить связь» ещё раз.", details_for(resp))
    raise ConnectorError(f"Wildberries ответил ошибкой (код {resp.status_code}).",
                         "Повторите проверку через несколько минут — возможно, у WB сбой.", details_for(resp),
                         status=WARN if resp.status_code >= 500 else ERROR)


WILDBERRIES = ConnectorType(
    key="wildberries",
    title="Wildberries",
    category="Маркетплейсы",
    description="Кабинет продавца Wildberries: заказы, остатки, цены, продажи, финансовые отчёты. "
                "Для каждого кабинета — отдельное подключение.",
    finance=True,
    name_placeholder="Например: WB — ИП Иванов",
    fields=[
        Field("token", "Токен API", "secret", required=True, ascii_only=True,
              placeholder="eyJhbGciOi...",
              hint="Токен из личного кабинета WB. Действует 180 дней — платформа заранее "
                   "предупредит, что он скоро истечёт."),
    ],
    validate=validate_wb,
    check=check_wb,
    help_steps=[
        "Войдите в seller.wildberries.ru под владельцем кабинета.",
        "Справа вверху — имя кабинета → «Настройки» → «Доступ к API» (или «Профиль → Интеграции по API»).",
        "«Создать новый токен»: тип «Персональный», отметьте категории — Контент, Статистика, "
        "Аналитика, Цены и скидки, Маркетплейс, Финансы, Продвижение. Режим «Только чтение» пока подойдёт.",
        "Скопируйте токен сразу — второй раз WB его не покажет.",
    ],
)

# ----------------------------------------------------------------------- Ozon

OZON_BASE = "https://api-seller.ozon.ru"
OZON_ACTION = (
    "Проверьте, что Client ID и API-ключ из одного кабинета. Ключ можно создать заново: "
    "seller.ozon.ru → «Настройки» → «API-ключи» → «Сгенерировать ключ» (роль Admin read only "
    "или Admin)."
)


def validate_ozon(values: dict) -> dict:
    cid = values.get("client_id", "")
    if cid and not cid.isdigit():
        return {"client_id": "Client ID у Ozon состоит только из цифр, например 123456."}
    key = values.get("api_key", "")
    if key and len(key) < 20:
        return {"api_key": "API-ключ Ozon слишком короткий — скопируйте его целиком "
                           "(вида 1a2b3c4d-....-....)."}
    return {}


async def check_ozon(values: dict, ctx: CheckContext) -> CheckResult:
    headers = {"Client-Id": values.get("client_id", ""), "Api-Key": values.get("api_key", ""),
               "Content-Type": "application/json"}
    resp = await http_request("POST", OZON_BASE + "/v1/seller/info", ctx, service="Ozon",
                              headers=headers, json={})
    if resp.status_code == 404:
        resp = await http_request("POST", OZON_BASE + "/v1/warehouse/list", ctx, service="Ozon",
                                  headers=headers, json={})
    data = json_or_none(resp) or {}
    if resp.status_code == 200:
        name = ""
        if isinstance(data, dict):
            company = data.get("company") or data.get("result") or {}
            if isinstance(company, dict):
                name = company.get("name") or company.get("legal_name") or ""
        return CheckResult(OK, f"Ozon: подключён кабинет{' «' + name + '»' if name else ''} "
                               f"(Client ID {values.get('client_id')}).")
    message = str(data.get("message", "")) if isinstance(data, dict) else ""
    low = message.lower()
    if resp.status_code in (401, 403) and ("deactivated" in low or "expired" in low):
        raise ConnectorError("API-ключ Ozon отключён или истёк.", OZON_ACTION, details_for(resp))
    if resp.status_code in (401, 403) and ("invalid" in low or "not found" in low or "required" in low
                                           or resp.status_code == 401):
        raise ConnectorError("Ozon не принял Client ID и API-ключ.", OZON_ACTION, details_for(resp))
    if resp.status_code == 403:
        return CheckResult(WARN, "Ozon принял ключ, но у него не хватает прав для этой операции.",
                           "Создайте ключ с ролью «Admin read only» или «Admin».", details_for(resp))
    if resp.status_code == 429:
        return CheckResult(WARN, "Ozon просит подождать: слишком частые запросы.",
                           "Повторите проверку через минуту.", details_for(resp))
    raise ConnectorError(f"Ozon ответил ошибкой (код {resp.status_code}).",
                         "Повторите проверку через несколько минут.", details_for(resp),
                         status=WARN if resp.status_code >= 500 else ERROR)


OZON = ConnectorType(
    key="ozon",
    title="Ozon",
    category="Маркетплейсы",
    description="Кабинет продавца Ozon: товары, остатки, заказы, финансовые отчёты. "
                "Для каждого кабинета — отдельное подключение.",
    finance=True,
    name_placeholder="Например: Ozon — ООО «Ромашка»",
    fields=[
        Field("client_id", "Client ID", required=True, ascii_only=True, placeholder="123456",
              hint="Номер кабинета — только цифры."),
        Field("api_key", "API-ключ", "secret", required=True, ascii_only=True,
              placeholder="1a2b3c4d-5e6f-...", hint="Ключ показывается один раз при создании."),
    ],
    validate=validate_ozon,
    check=check_ozon,
    help_steps=[
        "Войдите в seller.ozon.ru под владельцем кабинета.",
        "«Настройки» (шестерёнка) → «API-ключи» (раздел Seller API).",
        "Client ID указан там же вверху страницы — скопируйте его.",
        "«Сгенерировать ключ»: название «Платформа», роль «Admin read only» (для чтения) — позже, "
        "когда понадобятся действия, выпустите ключ с ролью «Admin».",
        "Скопируйте ключ сразу — второй раз Ozon его не покажет.",
    ],
)

# -------------------------------------------------------------- Яндекс Маркет

YM_CAMPAIGNS = "https://api.partner.market.yandex.ru/v2/campaigns"
YM_ACTION = (
    "Создайте API-ключ заново: partner.market.yandex.ru → «Настройки» → «API и модули» → "
    "«Авторизационные токены» → «Создать токен», отметьте нужные доступы и вставьте ключ сюда."
)


def validate_ym(values: dict) -> dict:
    key = values.get("api_key", "")
    if key.startswith("y0_") or key.startswith("AQAAAA"):
        return {"api_key": "Это OAuth-токен Яндекса, а нужен API-ключ Маркета (начинается с ACMA:). "
                           "Как его получить — в блоке «Где взять данные»."}
    return {}


async def check_ym(values: dict, ctx: CheckContext) -> CheckResult:
    resp = await http_request("GET", YM_CAMPAIGNS, ctx, service="Яндекс Маркет",
                              headers={"Api-Key": values.get("api_key", "")})
    data = json_or_none(resp) or {}
    if resp.status_code == 200:
        campaigns = data.get("campaigns") or []
        names = [c.get("domain") or (c.get("business") or {}).get("name") or str(c.get("id")) for c in campaigns]
        if not campaigns:
            return CheckResult(WARN, "Ключ Яндекс Маркета верный, но магазинов к нему не привязано.",
                               "Проверьте, что ключ выпущен в нужном кабинете и у него есть доступ к магазинам.")
        return CheckResult(OK, f"Яндекс Маркет: доступно магазинов — {len(campaigns)} "
                               f"({', '.join(names[:5])}{'…' if len(names) > 5 else ''}).")
    if resp.status_code == 401:
        raise ConnectorError("Яндекс Маркет не принял API-ключ.", YM_ACTION, details_for(resp))
    if resp.status_code == 403:
        raise ConnectorError("У API-ключа Яндекс Маркета нет нужного доступа.", YM_ACTION, details_for(resp))
    if resp.status_code == 420 or resp.status_code == 429:
        return CheckResult(WARN, "Яндекс Маркет просит подождать: слишком частые запросы.",
                           "Повторите проверку через минуту.", details_for(resp))
    raise ConnectorError(f"Яндекс Маркет ответил ошибкой (код {resp.status_code}).",
                         "Повторите проверку через несколько минут.", details_for(resp),
                         status=WARN if resp.status_code >= 500 else ERROR)


YANDEX_MARKET = ConnectorType(
    key="yandex_market",
    title="Яндекс Маркет",
    category="Маркетплейсы",
    description="Кабинет продавца Яндекс Маркета: магазины, заказы, остатки, отчёты. "
                "Для каждого кабинета — отдельное подключение.",
    finance=True,
    name_placeholder="Например: Маркет — основной кабинет",
    fields=[
        Field("api_key", "API-ключ", "secret", required=True, ascii_only=True,
              placeholder="ACMA:...", hint="Начинается с ACMA:"),
    ],
    validate=validate_ym,
    check=check_ym,
    help_steps=[
        "Войдите в partner.market.yandex.ru под владельцем кабинета.",
        "«Настройки» → «API и модули» → «Авторизационные токены» (API-ключи).",
        "«Создать токен»: название «Платформа», отметьте доступы — «Управление заказами», "
        "«Управление товарами», «Цены», «Финансы», «Отчёты» (для начала хватит просмотра).",
        "Скопируйте ключ целиком, вместе с началом ACMA:.",
    ],
)
