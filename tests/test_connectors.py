"""Проверка коннекторов на подменённых ответах сервисов: каждому коду ответа — понятное сообщение.

Запуск: python tests/test_connectors.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.connectors import BY_KEY, run_check  # noqa: E402
from app.connectors import google as g  # noqa: E402
from app.connectors.base import CheckContext, clean_token, validate_form  # noqa: E402
from app.diagnostics import scrub  # noqa: E402

results: list[bool] = []


def check(cond: bool, title: str, extra: str = "") -> None:
    results.append(bool(cond))
    print(f"[{'OK  ' if cond else 'FAIL'}] {title}" + (f"\n        {extra}" if extra and not cond else ""))


def ctx(handler) -> CheckContext:
    return CheckContext(transport=httpx.MockTransport(handler))


def run(key: str, values: dict, handler):
    return asyncio.run(run_check(BY_KEY[key], values, ctx(handler)))


def jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJFUzI1NiJ9.{body}.c2ln"


def main() -> int:
    # ------------------------------------------------------ чистка токенов
    check(clean_token("  abc def\n\t​", "Токен") == "abcdef", "Пробелы, NBSP и невидимые символы вырезаются")
    try:
        clean_token("sk-ant-аbc", "API-ключ")
        check(False, "Русская буква в токене даёт ошибку")
    except ValueError as exc:
        check("русские буквы" in str(exc) and "«а»" in str(exc), "Русская буква в токене — понятная ошибка", str(exc))
    name, cfg, secrets, errors = validate_form(BY_KEY["claude"], {"api_key": "sk-ant-x" + "а" * 3, "model": "x"})
    check("api_key" in errors and "ascii" not in errors["api_key"], "Форма Claude: русские буквы — ошибка у поля")

    # ------------------------------------------------------------- Wildberries
    good = jwt({"exp": time.time() + 100 * 86400})
    r = run("wildberries", {"token": good}, lambda req: httpx.Response(200, json={"name": "ИП Иванов", "sid": "1"}))
    check(r.status == "ok" and "ИП Иванов" in r.message, "WB: 200 — кабинет назван", r.message)
    r = run("wildberries", {"token": good}, lambda req: httpx.Response(401, json={"title": "unauthorized", "detail": "token expired"}))
    check(r.status == "error" and "истёк" in r.message and "Интеграции по API" in r.action, "WB: 401 expired — «срок истёк» + где создать", r.message)
    r = run("wildberries", {"token": jwt({"exp": time.time() - 86400})}, lambda req: httpx.Response(200))
    check(r.status == "error" and "истёк" in r.message, "WB: просроченный токен распознан без запроса", r.message)
    r = run("wildberries", {"token": jwt({"exp": time.time() + 5 * 86400})}, lambda req: httpx.Response(200, json={"name": "X"}))
    check(r.status == "warn" and "истекает через" in r.message, "WB: токен скоро истечёт — предупреждение", r.message)
    r = run("wildberries", {"token": good}, lambda req: httpx.Response(429))
    check(r.status == "warn" and "раза в минуту" in r.message, "WB: 429 — «не чаще раза в минуту»", r.message)

    # -------------------------------------------------------------------- Ozon
    ozon = {"client_id": "123456", "api_key": "1a2b3c4d-1111-2222-3333-444455556666"}
    r = run("ozon", ozon, lambda req: httpx.Response(403, json={"code": 7, "message": "Invalid Api-Key, please contact support"}))
    check(r.status == "error" and "не принял Client ID и API-ключ" in r.message, "Ozon: неверный ключ", r.message)
    r = run("ozon", ozon, lambda req: httpx.Response(403, json={"code": 7, "message": "Api-key is deactivated"}))
    check("отключён" in r.message, "Ozon: ключ отключён", r.message)
    r = run("ozon", ozon, lambda req: httpx.Response(200, json={"company": {"name": "ООО Ромашка"}}))
    check(r.status == "ok" and "ООО Ромашка" in r.message, "Ozon: 200 — кабинет назван", r.message)
    seen = []

    def ozon_fallback(req):
        seen.append(req.url.path)
        return httpx.Response(404) if req.url.path == "/v1/seller/info" else httpx.Response(200, json={"result": []})
    r = run("ozon", ozon, ozon_fallback)
    check(r.status == "ok" and seen == ["/v1/seller/info", "/v1/warehouse/list"], "Ozon: запасной метод, если основного нет", repr(seen))
    check(validate_form(BY_KEY["ozon"], {"client_id": "12ab", "api_key": "x" * 30})[3].get("client_id"),
          "Ozon: Client ID не из цифр — ошибка у поля")

    # ---------------------------------------------------------- Яндекс Маркет
    r = run("yandex_market", {"api_key": "ACMA:abc"}, lambda req: httpx.Response(401, json={"errors": []}))
    check(r.status == "error" and "не принял API-ключ" in r.message, "Маркет: 401", r.message)
    r = run("yandex_market", {"api_key": "ACMA:abc"}, lambda req: httpx.Response(200, json={"campaigns": [{"id": 1, "domain": "shop.ru"}]}))
    check(r.status == "ok" and "shop.ru" in r.message, "Маркет: магазины перечислены", r.message)
    check("OAuth" in validate_form(BY_KEY["yandex_market"], {"api_key": "y0_AgAAAA"})[3].get("api_key", ""),
          "Маркет: вместо API-ключа вставлен OAuth-токен — подсказка")

    # --------------------------------------------------------------------- MAX
    r = run("max_bot", {"token": "abc"}, lambda req: httpx.Response(401, json={"code": "verify.token"}))
    check(r.status == "error" and "не принял токен" in r.message, "MAX: неверный токен", r.message)
    r = run("max_bot", {"token": "abc"}, lambda req: httpx.Response(200, json={"name": "Помощник", "username": "pomoshnik_bot"}))
    check(r.status == "ok" and "Помощник" in r.message, "MAX: бот на связи", r.message)

    # ---------------------------------------------------------------- Telegram
    tg = "1234567890:AAHfakeTokenForTestsOnly_abcdefghijk"
    r = run("telegram_bot", {"token": tg}, lambda req: httpx.Response(401, json={"ok": False}))
    check(r.status == "error" and "перевыпущен" in r.message and tg not in r.details, "Telegram: 401, токена нет в подробностях", r.details)

    # --------------------------------------------------------------- Битрикс24
    hook = "https://a.bitrix24.ru/rest/1/abc123/"
    r = run("bitrix24", {"webhook_url": hook}, lambda req: httpx.Response(401, json={"error": "NO_AUTH_FOUND", "error_description": "Wrong authorization data"}))
    check(r.status == "error" and "вебхук" in r.message.lower(), "Битрикс24: неверный вебхук", r.message)

    def b24(req):
        if req.url.path.endswith("profile.json"):
            return httpx.Response(200, json={"result": {"NAME": "Иван", "LAST_NAME": "Петров"}})
        return httpx.Response(200, json={"result": ["crm", "user"]})
    r = run("bitrix24", {"webhook_url": hook}, b24)
    check(r.status == "warn" and "Иван Петров" in r.message and "Задачи" in r.message, "Битрикс24: не хватает прав на задачи", r.message)

    # ------------------------------------------------------------------ Claude
    claude = {"api_key": "sk-ant-x", "model": "claude-opus-5", "monthly_limit": 100.0}
    r = run("claude", claude, lambda req: httpx.Response(403, json={"error": {"type": "forbidden", "message": "Request not allowed"}}))
    check(r.status == "error" and "из страны" in r.message and "прокси" in r.action, "Claude: блок по стране — совет про прокси", r.message)
    r = run("claude", claude, lambda req: httpx.Response(401, json={"error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
    check(r.message == "Ключ Claude API неверный.", "Claude: 401 — «Ключ API неверный»", r.message)
    r = run("claude", claude, lambda req: httpx.Response(200, json={"data": [{"id": "claude-sonnet-5"}]}))
    check(r.status == "warn" and "недоступна" in r.message, "Claude: выбранная модель недоступна", r.message)
    r = run("claude", claude, lambda req: httpx.Response(200, json={"data": [{"id": "claude-opus-5"}]}))
    check(r.status == "ok" and "$100" in r.message, "Claude: ключ верный, лимит показан", r.message)
    r = run("claude", claude, lambda req: httpx.Response(529, json={"error": {"type": "overloaded_error"}}))
    check(r.status == "warn" and "перегружены" in r.message, "Claude: 529 — перегрузка, это временно", r.message)

    # ------------------------------------------------------------------ Google
    app_vals = {"client_id": "1-a.apps.googleusercontent.com", "client_secret": "GOCSPX-x"}
    r = run("google_app", app_vals, lambda req: httpx.Response(401, json={"error": "invalid_client"}))
    check(r.status == "error" and "неверные" in r.message, "Google: неверные ключи приложения", r.message)
    r = run("google_app", app_vals, lambda req: httpx.Response(400, json={"error": "invalid_grant"}))
    check(r.status == "ok", "Google: ключи верные (Google знает клиента)", r.message)
    acc_type = g.account_type(lambda: app_vals)
    acc = {"refresh_token": "1//x", "email": "sales@romashka.ru"}
    r = asyncio.run(run_check(acc_type, acc, ctx(lambda req: httpx.Response(400, json={"error": "invalid_grant"}))))
    check(r.status == "error" and "отозван" in r.message and "Publish app" in r.action, "Google: доступ отозван — совет про Testing/Publish", r.message)

    def gmail_off(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "ya29.x"})
        if req.url.host == "gmail.googleapis.com":
            return httpx.Response(403, json={"error": {"message": "Gmail API has not been used in project 1 before or it is disabled", "status": "PERMISSION_DENIED"}})
        return httpx.Response(200, json={"storageQuota": {"limit": str(15 * 1024 ** 3), "usage": str(3 * 1024 ** 3)}})
    r = asyncio.run(run_check(acc_type, acc, ctx(gmail_off)))
    check(r.status == "error" and "Gmail API не включён" in r.message and "Диск: занято 3,0 ГБ из 15,0 ГБ" in r.message,
          "Google: Gmail API выключен, Диск работает — оба факта в сообщении", r.message)
    url = g.authorize_url("cid", "https://panel.ru/oauth/google/callback", "st")
    check("access_type=offline" in url and "prompt=consent" in url and "gmail.modify" in url, "Google: ссылка входа с offline и consent")

    # --------------------------------------------------------------------- 1С
    onec = {"base_url": "https://1c.local/ka", "username": "Робот", "password": "п", "use_odata": True, "verify_ssl": True}
    r = run("onec", onec, lambda req: httpx.Response(500, text="Нет свободной лицензии на сервере 1С"))
    check("лицензии" in r.message, "1С: нет лицензии — так и сказано", r.message)
    r = run("onec", onec, lambda req: httpx.Response(503, text="Service Unavailable"))
    check("Агент сервера" in r.action, "1С: 503 — проверить службу «Агент сервера 1С»", r.action)
    r = run("onec", onec, lambda req: httpx.Response(200, json={"value": []}))
    check(r.status == "warn" and "не выбран ни один" in r.message, "1С: OData пустой — подсказка, где отметить объекты", r.message)
    r = run("onec", onec, lambda req: httpx.Response(200, text="<html>1C</html>"))
    check(r.status == "error" and "не OData" in r.message, "1С: вместо OData пришла веб-страница", r.message)
    r = run("onec", onec, lambda req: httpx.Response(403))
    check(r.status == "error" and "нет прав на OData" in r.message, "1С: 403 — нет прав на OData", r.message)

    def boom(req):
        raise RuntimeError("что-то сломалось внутри")
    r = run("onec", onec, boom)
    check(r.status == "error" and r.action, "Любая неожиданная ошибка превращается в понятный результат", r.message)

    # -------------------------------------------------------------- отчёт
    text = "Bearer abc.def.ghi sk-ant-api03-SECRET https://x.bitrix24.ru/rest/1/code123/ http://u:pa55@proxy:3128 1234567890:AAHfakeTokenForTestsOnly_abcdefghijk"
    clean = scrub(text, ["pa55"])
    check(all(s not in clean for s in ("SECRET", "code123", "pa55", "AAHfake", "abc.def")), "Отчёт: секреты вычищаются по шаблонам", clean)

    ok = sum(results)
    print(f"\nИтого: {ok} из {len(results)} проверок пройдено")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
