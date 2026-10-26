"""Проверка мини-приложения MAX на подменённых ответах MAX, Битрикс24 и Claude.

Работает на временной базе, настоящие серверы не трогает. Запуск: python tests/test_miniapp.py
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="miniapp-test-")
from app import config  # noqa: E402

config.DATA_DIR = Path(_tmp) / "data"
config.LOGS_DIR = Path(_tmp) / "logs"
config.BACKUP_DIR = Path(_tmp) / "backups"
config.TLS_DIR = config.DATA_DIR / "tls"
config.DB_PATH = config.DATA_DIR / "platform.db"
config.KEY_PATH = config.DATA_DIR / "secret.key"
config.PID_PATH = config.DATA_DIR / "app.pid"
config.CONTROL_TOKEN_PATH = config.DATA_DIR / "control.token"
config.ENV_FILE = Path(_tmp) / ".env"

from app import auth, db, store  # noqa: E402
from app.main import create_app, startup  # noqa: E402
from app.miniapp import initdata, service  # noqa: E402
from app.miniapp.crm import normalize_phone  # noqa: E402
from app.miniapp.maxapi import parse_update  # noqa: E402

BOT_TOKEN = "test-bot-token-123"
HOOK = "https://company.bitrix24.ru/rest/1/abc123xyz/"
results: list[bool] = []
WISH = date.today() + timedelta(days=30)
WISH_ISO, WISH_RU = WISH.isoformat(), WISH.strftime("%d.%m.%Y")


def check(cond: bool, title: str, extra: str = "") -> None:
    results.append(bool(cond))
    print(f"[{'OK  ' if cond else 'FAIL'}] {title}" + (f"\n        {extra}" if extra and not cond else ""))


def make_init_data(user: dict, token: str = BOT_TOKEN, auth_date: int | None = None) -> str:
    params = {"user": json.dumps(user, ensure_ascii=False), "auth_date": str(auth_date or int(time.time())),
              "query_id": "q1"}
    params["hash"] = initdata.sign(params, token)
    return urlencode(params)


# ------------------------------------------------------------- заглушки

MAX_CALLS: list[tuple[str, str, dict, dict]] = []
BITRIX_CALLS: list[tuple[str, dict]] = []
CLAUDE_REPLY: dict = {"answer": "Чистят раз в неделю.", "handoff": False}
CLAUDE_CALLS: list[dict] = []
BITRIX_FAIL = {"on": False}  # пока включено — Битрикс24 «отвергает вебхук»


def max_handler(req: httpx.Request) -> httpx.Response:
    body = json.loads(req.content.decode() or "{}") if req.content else {}
    MAX_CALLS.append((req.method, req.url.path, dict(req.url.params), body))
    if req.headers.get("authorization") != BOT_TOKEN:
        return httpx.Response(401, json={"code": "verify.token", "message": "Invalid access_token"})
    if req.url.path == "/me":
        return httpx.Response(200, json={"user_id": 777, "name": "Сервис", "username": "kupibas_bot", "is_bot": True})
    if req.url.path == "/subscriptions" and req.method == "GET":
        return httpx.Response(200, json={"subscriptions": [{"url": "https://old.example/hook", "time": 1}]})
    if req.url.path in ("/subscriptions", "/messages", "/answers"):
        return httpx.Response(200, json={"success": True, "message": {"body": {"mid": "m1"}}})
    return httpx.Response(404, json={"message": "not found"})


def bitrix_handler(req: httpx.Request) -> httpx.Response:
    method = req.url.path.rsplit("/", 1)[-1].removesuffix(".json")
    body = json.loads(req.content.decode() or "{}")
    BITRIX_CALLS.append((method, body))
    if method == "crm.duplicate.findByComm":
        return httpx.Response(200, json={"result": {"CONTACT": [55]} if body["values"][0] == "+79123456789" else {}})
    if method == "crm.contact.add":
        return httpx.Response(200, json={"result": 56})
    if method == "crm.lead.add":
        if BITRIX_FAIL["on"]:
            return httpx.Response(401, json={"error": "NO_AUTH_FOUND", "error_description": "Wrong authorization data"})
        return httpx.Response(200, json={"result": 901})
    if method == "crm.status.list":
        return httpx.Response(200, json={"result": [{"STATUS_ID": "NEW", "NAME": "Не обработан"},
                                                     {"STATUS_ID": "IN_PROCESS", "NAME": "В работе"}]})
    if method == "crm.lead.list":
        return httpx.Response(200, json={"result": [{"ID": "901", "STATUS_ID": "IN_PROCESS"}]})
    return httpx.Response(200, json={"result": True})


def claude_handler(req: httpx.Request) -> httpx.Response:
    body = json.loads(req.content.decode())
    CLAUDE_CALLS.append(body)
    return httpx.Response(200, json={"model": body["model"], "stop_reason": "end_turn",
                                     "usage": {"input_tokens": 500, "output_tokens": 50},
                                     "content": [{"type": "text", "text": json.dumps(CLAUDE_REPLY, ensure_ascii=False)}]})


def main() -> int:
    startup()
    service.TRANSPORTS.update({"max": httpx.MockTransport(max_handler), "bitrix": httpx.MockTransport(bitrix_handler),
                               "claude": httpx.MockTransport(claude_handler)})

    # ------------------------------------------------------- подпись initData
    user = {"id": 100500, "first_name": "Иван", "last_name": "Петров", "username": "ivan"}
    good = make_init_data(user)
    data = initdata.validate(good, BOT_TOKEN)
    check(data["user"]["id"] == "100500" and data["user"]["name"] == "Иван Петров", "initData: верная подпись принимается")
    try:
        initdata.validate(good, "another-token")
        check(False, "initData: чужой токен — ошибка")
    except initdata.InitDataError as exc:
        check("Подпись" in str(exc), "initData: чужой токен — ошибка", str(exc))
    try:
        initdata.validate(good.replace("100500", "100501"), BOT_TOKEN)
        check(False, "initData: подмена id — ошибка")
    except initdata.InitDataError:
        check(True, "initData: подмена id — ошибка")
    try:
        initdata.validate(make_init_data(user, auth_date=int(time.time()) - 3 * 86400), BOT_TOKEN)
        check(False, "initData: устаревшие данные — ошибка")
    except initdata.InitDataError as exc:
        check("устарели" in str(exc), "initData: устаревшие данные — ошибка", str(exc))

    # ------------------------------------------------------------- телефоны
    check(normalize_phone("8 (912) 345-67-89") == "+79123456789", "Телефон: 8 (912)… → +7912…")
    check(normalize_phone("912 345 67 89") == "+79123456789", "Телефон: 10 цифр с 9 → +7…")
    check(normalize_phone("12345") == "", "Телефон: короткий — не телефон")

    # --------------------------------------------------------- события MAX
    u = parse_update({"update_type": "message_created", "timestamp": 1,
                      "message": {"sender": {"user_id": 5, "first_name": "Анна", "username": "anna"},
                                  "recipient": {"chat_id": 9, "chat_type": "dialog"},
                                  "body": {"mid": "x", "text": " Привет "}}})
    check(u["user_id"] == 5 and u["chat_id"] == 9 and u["text"] == "Привет" and u["user_name"] == "Анна",
          "Событие message_created разобрано")
    u = parse_update({"update_type": "message_callback", "callback": {"callback_id": "cb1", "payload": "book",
                                                                       "user": {"user_id": 6, "name": "Пётр"}}})
    check(u["callback_id"] == "cb1" and u["payload"] == "book" and u["user_id"] == 6, "Событие message_callback разобрано")

    # -------------------------------------------------------- подключения
    store.save_connection(None, "max_bot", "Бот MAX", {}, {"token": BOT_TOKEN})
    store.save_connection(None, "bitrix24", "Битрикс24", {}, {"webhook_url": HOOK})
    store.save_connection(None, "claude", "Ядро Claude", {"model": "claude-sonnet-5", "monthly_limit": 100},
                          {"api_key": "sk-ant-test"})
    store.set_setting("public_url", "https://panel.example.ru")
    store.set_setting("company_name", "KUPIBAS")
    service.save_settings({"miniapp_phone": "+7 999 000-00-00", "miniapp_manager_max_ids": "4242"})

    # --------------------------------------------------- HTTP: приложение
    app = create_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))

    async def http() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/max/app")
            check(r.status_code == 200 and "st.max.ru/js/max-web-app.js" in r.text and "frame-ancestors 'self' https://max.ru"
                  in r.headers.get("content-security-policy", ""), "Страница приложения отдаётся с мостом MAX и CSP")
            r = await client.get("/max/api/bootstrap")
            check(r.status_code == 401 and r.json().get("auth"), "API без подписи — 401", r.text)
            headers = {"Authorization": "MaxInitData " + good}
            r = await client.get("/max/api/bootstrap", headers=headers)
            d = r.json()
            check(r.status_code == 200 and d["user"]["name"] == "Иван Петров" and len(d["services"]) >= 5
                  and d["phone"] == "+7 999 000-00-00", "bootstrap: пользователь, услуги, телефон", r.text[:300])
            r = await client.post("/max/api/booking", headers=headers, json={"name": "И", "phone": "12", "service": ""})
            e = r.json().get("errors", {})
            check(r.status_code == 400 and {"name", "phone", "service"} <= set(e), "Заявка: ошибки по полям", r.text)
            MAX_CALLS.clear(); BITRIX_CALLS.clear()
            r = await client.post("/max/api/booking", headers=headers, json={
                "name": "Иван Петров", "phone": "8 912 345-67-89", "address": "пос. Солнечный, 5",
                "service": "Чистка бассейна", "pool": "уличный 50 м³", "date": WISH_ISO, "time": "morning",
                "comment": "Вода мутная"})
            d = r.json()
            check(r.status_code == 200 and d["ok"] and d["booking"]["id"] == 1, "Заявка создана", r.text[:300])
            methods = [m for m, _ in BITRIX_CALLS]
            check(methods[:2] == ["crm.duplicate.findByComm", "crm.lead.add"], "Битрикс24: контакт найден по телефону, лид создан", str(methods))
            lead = next(b for m, b in BITRIX_CALLS if m == "crm.lead.add")["fields"]
            check(lead["CONTACT_ID"] == 55 and lead["PHONE"][0]["VALUE"] == "+79123456789"
                  and "Чистка бассейна" in lead["TITLE"] and "Вода мутная" in lead["COMMENTS"]
                  and WISH_RU in lead["COMMENTS"], "Лид: контакт, телефон, заголовок, описание", json.dumps(lead, ensure_ascii=False))
            sent = [(p, c) for m, path, p, c in MAX_CALLS if path == "/messages"]
            check(len(sent) == 2 and sent[0][0]["user_id"] == "4242" and "Новая заявка" in sent[0][1]["text"]
                  and sent[1][0]["user_id"] == "100500" and "Заявка принята" in sent[1][1]["text"],
                  "MAX: уведомление менеджеру и подтверждение клиенту", str(sent)[:400])
            row = db.query_one("SELECT * FROM miniapp_bookings WHERE id = 1")
            check(row["status"] == "sent" and row["bitrix_lead_id"] == 901, "Заявка помечена «в Битрикс24»")
            r = await client.get("/max/api/bookings", headers=headers)
            d = r.json()
            check(d["ok"] and d["bookings"][0]["status"] == "В работе" and d["bookings"][0]["date"] == WISH_RU,
                  "Список заявок: стадия из Битрикс24", r.text[:300])

            # ошибка Битрикс24 не ломает заявку
            BITRIX_FAIL["on"] = True
            r = await client.post("/max/api/booking", headers=headers, json={
                "name": "Иван Петров", "phone": "8 912 345-67-89", "service": "Выезд мастера"})
            d = r.json()
            BITRIX_FAIL["on"] = False
            row = db.query_one("SELECT * FROM miniapp_bookings WHERE id = 2")
            check(r.status_code == 200 and d["ok"] and row["status"] == "error" and "Вебхук удалён" in row["bitrix_error"],
                  "Ошибка Битрикс24: заявка сохранена, причина записана", r.text[:300] + " " + str(dict(row)))

            # вопрос — Claude
            CLAUDE_CALLS.clear()
            r = await client.post("/max/api/ask", headers=headers, json={"text": "Как часто чистить бассейн?"})
            d = r.json()
            check(r.status_code == 200 and d["answer"] == "Чистят раз в неделю." and not d["handoff"], "Вопрос: ответ Claude", r.text)
            sys_prompt = CLAUDE_CALLS[-1]["system"]
            check("KUPIBAS" in sys_prompt and "Чистка бассейна" in sys_prompt and "+7 999 000-00-00" in sys_prompt
                  and CLAUDE_CALLS[-1]["messages"][-1]["content"] == "Как часто чистить бассейн?", "Claude получил сведения о компании и вопрос")
            check(store.month_usage(time.strftime("%Y-%m")) > 0, "Расход на Claude учтён")

            # вопрос, который помощник передаёт менеджеру
            CLAUDE_REPLY.update({"answer": "Передам менеджеру.", "handoff": True})
            BITRIX_CALLS.clear(); MAX_CALLS.clear()
            r = await client.post("/max/api/ask", headers=headers, json={"text": "Сколько стоит ремонт насоса Hayward?"})
            d = r.json()
            q = db.query_one("SELECT * FROM miniapp_bookings WHERE kind = 'question'")
            check(d["handoff"] and q is not None and q["bitrix_lead_id"] == 901 and q["phone"] == "+79123456789",
                  "Вопрос без ответа: лид в Битрикс24 с телефоном клиента", str(dict(q)) if q else r.text)
            check(any(p.get("user_id") == "4242" and "Вопрос из MAX" in c.get("text", "") for _, path, p, c in MAX_CALLS if path == "/messages"),
                  "Вопрос без ответа: менеджер уведомлён")

            # без Claude — ответ из базы знаний
            service.save_settings({"miniapp_use_claude": "0"})
            r = await client.post("/max/api/ask", headers=headers, json={"text": "Когда закрывать бассейн на зиму?"})
            d = r.json()
            check("заморозков" in d["answer"] and not d["handoff"], "Без Claude: ответ из частых вопросов", r.text)
            service.save_settings({"miniapp_use_claude": "1"})
            CLAUDE_REPLY.update({"answer": "Чистят раз в неделю.", "handoff": False})

            # ---------------------------------------------------- вебхук бота
            secret = service.webhook_secret()
            r = await client.post("/max/webhook", json={"update_type": "bot_started", "user": {"user_id": 1}})
            check(r.status_code == 403, "Вебхук без секрета — 403")
            MAX_CALLS.clear()
            r = await client.post("/max/webhook?sync=1", headers={"X-Max-Bot-Api-Secret": secret},
                                  json={"update_type": "bot_started", "timestamp": 1, "chat_id": 9,
                                        "user": {"user_id": 31, "first_name": "Ольга", "username": "olga"}})
            d = r.json()
            welcome = [c for _, path, p, c in MAX_CALLS if path == "/messages" and p.get("user_id") == "31"]
            btn = welcome[0]["attachments"][0]["payload"]["buttons"][0][0] if welcome and welcome[0].get("attachments") else {}
            check(d.get("result") == "welcome" and welcome and "Ольга" in welcome[0]["text"]
                  and btn.get("type") == "open_app" and btn.get("web_app") == "kupibas_bot" and btn.get("contact_id") == 777,
                  "Вебхук bot_started: приветствие с кнопкой «Открыть приложение»", r.text + " " + str(welcome)[:300])
            MAX_CALLS.clear()
            r = await client.post("/max/webhook?sync=1", headers={"X-Max-Bot-Api-Secret": secret},
                                  json={"update_type": "message_created", "timestamp": 1,
                                        "message": {"sender": {"user_id": 31, "first_name": "Ольга"},
                                                    "recipient": {"chat_id": 9}, "body": {"mid": "m2", "text": "Как часто чистить?"}}})
            answered = [c for _, path, p, c in MAX_CALLS if path == "/messages" and p.get("user_id") == "31"]
            check(r.json().get("result") == "answered" and answered and answered[0]["text"] == "Чистят раз в неделю.",
                  "Вебхук message_created: ответ отправлен в чат", r.text + str(answered)[:200])
            MAX_CALLS.clear()
            r = await client.post("/max/webhook?sync=1", headers={"X-Max-Bot-Api-Secret": secret},
                                  json={"update_type": "message_callback", "timestamp": 1,
                                        "callback": {"callback_id": "cb9", "payload": "book", "user": {"user_id": 31}}})
            ans = [(p, c) for _, path, p, c in MAX_CALLS if path == "/answers"]
            check(r.json().get("result") == "callback:book" and ans and ans[0][0]["callback_id"] == "cb9"
                  and "Записаться" in ans[0][1]["message"]["text"], "Вебхук message_callback: ответ на кнопку", str(ans)[:300])

            # -------------------------------------------- панель: вход и раздел
            admin = db.query_one("SELECT id FROM users WHERE login = 'admin'")
            db.execute("UPDATE users SET must_change_password = 0 WHERE id = ?", (admin["id"],))
            token = auth.create_session(admin["id"], "127.0.0.1")
            client.cookies.set(auth.SESSION_COOKIE, token)
            r = await client.get("/pool-service")
            check(r.status_code == 200 and "Сервис бассейнов" in r.text and "Иван Петров" in r.text
                  and "лид №901" in r.text and "Вебхук удалён" in r.text, "Панель: раздел с заявками и ошибкой Битрикс24", r.text[:200])
            csrf = db.query_one("SELECT csrf FROM sessions")["csrf"]
            MAX_CALLS.clear()
            r = await client.post("/pool-service/subscribe", data={"csrf": csrf})
            subs = [(m, p, c) for m, path, p, c in MAX_CALLS if path == "/subscriptions"]
            posted = next((c for m, p, c in subs if m == "POST"), {})
            check(r.status_code == 303 and posted.get("url") == "https://panel.example.ru/max/webhook"
                  and posted.get("secret") == secret and "message_created" in posted.get("update_types", [])
                  and any(m == "DELETE" and p.get("url") == "https://old.example/hook" for m, p, c in subs),
                  "Панель: подписка бота на вебхук, старая подписка снята", str(subs))
            r = await client.get("/pool-service")
            check("Бот подписан" in r.text or "подписан" in r.text, "Панель: показано сообщение о подписке")
            r = await client.post("/pool-service", data={"csrf": csrf, "miniapp_title": "", "miniapp_services": "x"})
            check(r.status_code == 200 and "Название не может быть пустым" in r.text, "Панель: пустое название — ошибка")
            r = await client.post("/pool-service", data={"csrf": csrf, "miniapp_enabled": "1", "miniapp_use_claude": "1",
                                                        "miniapp_title": "Сервис KUPIBAS", "miniapp_services": "Чистка | раз в неделю",
                                                        "miniapp_about": "О нас", "miniapp_hours": "", "miniapp_phone": "",
                                                        "miniapp_faq": "", "miniapp_bitrix_conn": "", "miniapp_bitrix_assigned": "12",
                                                        "miniapp_manager_max_ids": "4242"})
            check(r.status_code == 303 and service.setting("miniapp_title") == "Сервис KUPIBAS"
                  and service.services() == [{"name": "Чистка", "note": "раз в неделю"}] and service.bitrix_assigned() == 12,
                  "Панель: настройки сохранены")
            r = await client.get("/max/api/bootstrap")  # предпросмотр по сессии панели
            d = r.json()
            check(r.status_code == 200 and d["user"]["preview"] and d["title"] == "Сервис KUPIBAS", "Предпросмотр из панели без initData", r.text[:200])
            # повторная отправка заявки в Битрикс24
            r = await client.post("/pool-service/bookings/2/resend", data={"csrf": csrf})
            row = db.query_one("SELECT * FROM miniapp_bookings WHERE id = 2")
            check(r.status_code == 303 and row["status"] == "sent" and row["bitrix_lead_id"] == 901
                  and [b for m, b in BITRIX_CALLS if m == "crm.lead.add"][-1]["fields"]["ASSIGNED_BY_ID"] == 12,
                  "Панель: повторная отправка в Битрикс24 с ответственным", str(dict(row)))
            # выключенное приложение
            service.save_settings({"miniapp_enabled": "0"})
            r = await client.get("/max/app")
            check(r.status_code == 503, "Выключенное приложение — 503")

    asyncio.run(http())
    ok, total = sum(results), len(results)
    print(f"\nИтог: {ok} из {total}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
