"""Битрикс24 через входящий вебхук."""
from __future__ import annotations

import re

from .base import (ERROR, OK, WARN, CheckContext, CheckResult, ConnectorError, ConnectorType, Field,
                   details_for, http_request, json_or_none)

SERVICE = "Битрикс24"
WEBHOOK_RE = re.compile(r"^(https?://[^/]+/rest/\d+/[A-Za-z0-9]+)(/|$)")
NEEDED_SCOPES = {"crm": "CRM", "task": "Задачи", "user": "Пользователи"}

ACTION_NEW_WEBHOOK = (
    "Создайте вебхук заново: Битрикс24 → «Разработчикам» → «Другое» → «Входящий вебхук», "
    "отметьте права CRM, Задачи, Пользователи, нажмите «Сохранить» и вставьте сюда адрес из поля "
    "«Вебхук для вызова rest api»."
)


def normalize_webhook(value: str) -> str:
    match = WEBHOOK_RE.match(value.strip())
    return match.group(1) + "/" if match else value


def validate(values: dict) -> dict:
    url = "".join(ch for ch in values.get("webhook_url", "") if not ch.isspace() and ch not in "\u200b\ufeff")
    if url and not re.match(r"^https?://", url):
        url = "https://" + url
    code_part = url.split("/rest/", 1)[1] if "/rest/" in url else ""
    if re.search(r"[\u0400-\u04FF]", code_part):
        return {"webhook_url": "В коде вебхука (после /rest/) попали русские буквы. Скорее всего, "
                               "при копировании захватился лишний текст. Скопируйте адрес заново."}
    if url and not WEBHOOK_RE.match(url):
        return {"webhook_url": "Это не похоже на адрес вебхука. Он выглядит так: "
                               "https://ваша-компания.bitrix24.ru/rest/1/abc123xyz/"}
    if url:
        values["webhook_url"] = normalize_webhook(url)
    return {}


def _bitrix_error(resp) -> ConnectorError:
    data = json_or_none(resp) or {}
    code = str(data.get("error", "")).upper() if isinstance(data, dict) else ""
    text = str(data.get("error_description", "")) if isinstance(data, dict) else ""
    if code in ("NO_AUTH_FOUND", "INVALID_CREDENTIALS", "INVALID_TOKEN", "EXPIRED_TOKEN") or resp.status_code == 401:
        return ConnectorError("Битрикс24 не принял вебхук: код неверный или вебхук удалён.",
                              ACTION_NEW_WEBHOOK, details_for(resp))
    if code == "INSUFFICIENT_SCOPE":
        return ConnectorError("У вебхука не хватает прав.", ACTION_NEW_WEBHOOK, details_for(resp))
    if code in ("ACCESS_DENIED", "PAYMENT_REQUIRED") or "tariff" in text.lower() or "тариф" in text.lower():
        return ConnectorError("Битрикс24 запрещает доступ по API — вероятно, из-за тарифа.",
                              "Проверьте тариф портала: входящие вебхуки доступны не на всех тарифах.",
                              details_for(resp))
    if code == "QUERY_LIMIT_EXCEEDED" or resp.status_code == 503:
        return ConnectorError("Битрикс24 просит подождать: слишком много запросов.",
                              "Повторите проверку через минуту.", details_for(resp), status=WARN)
    if resp.status_code == 404 or data == {}:
        return ConnectorError("По этому адресу не отвечает Битрикс24.",
                              "Проверьте адрес портала в начале ссылки вебхука.", details_for(resp))
    return ConnectorError(f"Битрикс24 ответил ошибкой: {text or code or resp.status_code}.",
                          ACTION_NEW_WEBHOOK, details_for(resp))


async def check(values: dict, ctx: CheckContext) -> CheckResult:
    hook = normalize_webhook(values.get("webhook_url", ""))
    if not WEBHOOK_RE.match(hook):
        raise ConnectorError("Адрес вебхука не заполнен или неверный.", ACTION_NEW_WEBHOOK)
    resp = await http_request("GET", hook + "profile.json", ctx, service=SERVICE)
    data = json_or_none(resp)
    if resp.status_code in (301, 302):
        raise ConnectorError("По этому адресу не отвечает Битрикс24 (перенаправление на другой сайт).",
                             "Проверьте адрес портала в начале ссылки вебхука.", details_for(resp))
    if resp.status_code != 200 or not isinstance(data, dict) or "result" not in data:
        raise _bitrix_error(resp)
    profile = data["result"] or {}
    who = " ".join(x for x in (profile.get("NAME"), profile.get("LAST_NAME")) if x).strip() or "пользователь"
    message = f"Битрикс24 подключён. Вебхук работает от имени: {who}."

    scope_resp = await http_request("GET", hook + "scope.json", ctx, service=SERVICE)
    scopes = (json_or_none(scope_resp) or {}).get("result") if scope_resp.status_code == 200 else None
    if isinstance(scopes, list):
        missing = [title for key, title in NEEDED_SCOPES.items() if key not in scopes]
        if missing:
            return CheckResult(WARN, message + " Не хватает прав: " + ", ".join(missing) + ".",
                               "Откройте вебхук в Битрикс24 («Разработчикам → Интеграции»), отметьте "
                               "недостающие права и сохраните. Адрес вебхука при этом не меняется.")
    return CheckResult(OK, message)


CONNECTOR = ConnectorType(
    key="bitrix24",
    title="Битрикс24",
    category="CRM и задачи",
    description="CRM, сделки, задачи и сотрудники из Битрикс24. Подключается входящим вебхуком — "
                "это ссылка с секретным кодом внутри.",
    name_placeholder="Например: Битрикс24 компании",
    check=check,
    fields=[
        Field("webhook_url", "Адрес входящего вебхука", "secret", required=True,
              placeholder="https://компания.bitrix24.ru/rest/1/abc123xyz/",
              hint="Ссылка целиком. В ней зашит секретный код, поэтому она хранится зашифрованной "
                   "и показывается маской."),
    ],
    validate=validate,
    help_steps=[
        "Войдите в Битрикс24 под администратором.",
        "Слева в меню: «Разработчикам» (или «Приложения → Разработчикам») → вкладка «Другое» → "
        "«Входящий вебхук».",
        "В «Настройке прав» отметьте: CRM, Задачи, Пользователи (позже понадобятся и другие).",
        "Нажмите «Сохранить» и скопируйте адрес из поля «Вебхук для вызова rest api».",
        "Если адрес портала с русскими буквами (например, .рф) — вставьте как есть, он сохранится.",
    ],
)
