"""Логика мини-приложения: настройки, клиенты, заявки, ответы на вопросы, бот.

Ничего из этого не требует от сотрудника терминала: всё настраивается в разделе
панели «Сервис бассейнов», а клиент видит только мини-приложение и чат с ботом.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets as pysecrets
import time
from datetime import date, datetime
from typing import Any

import httpx

from .. import db, store
from ..connectors.claude import MODELS, PRICES
from .crm import BitrixCrm, CrmError, normalize_phone
from .maxapi import MaxApi, MaxError, btn_callback, btn_open_app, parse_update

log = logging.getLogger(__name__)

SECTION = "miniapp"  # раздел журнала

# Подменяются в тестах: вместо настоящих серверов отвечают заглушки.
TRANSPORTS: dict[str, httpx.AsyncBaseTransport | None] = {"max": None, "bitrix": None, "claude": None}

SCHEMA = """
CREATE TABLE IF NOT EXISTS miniapp_clients (
    max_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    bitrix_contact_id INTEGER,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS miniapp_bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    max_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT '',
    pool TEXT NOT NULL DEFAULT '',
    wish_date TEXT NOT NULL DEFAULT '',
    wish_time TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'booking',
    status TEXT NOT NULL DEFAULT 'new',
    bitrix_lead_id INTEGER,
    bitrix_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS miniapp_bookings_client ON miniapp_bookings(max_id, id);
CREATE TABLE IF NOT EXISTS miniapp_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    max_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'app',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS miniapp_messages_client ON miniapp_messages(max_id, id);
"""


def init() -> None:
    with db.session() as conn:
        conn.executescript(SCHEMA)


# -------------------------------------------------------------- настройки

DEFAULT_SERVICES = """Выезд мастера | диагностика бассейна и оборудования, консультация на месте
Чистка бассейна | дно, стенки, ватерлиния, промывка фильтра
Анализ воды и подбор химии | привезём и зальём нужную химию
Запуск после зимы | расконсервация, промывка, настройка оборудования
Консервация на зиму | слив, зимняя химия, укрытие
Ремонт оборудования | насос, фильтр, нагреватель, автоматика
Другое | опишите задачу в комментарии"""

DEFAULT_ABOUT = """Мы строим и обслуживаем бассейны: частные и общественные. Выполняем сервисное обслуживание,
чистку, запуск и консервацию, ремонт оборудования, подбираем и привозим химию для воды.
Выезд мастера согласовываем по телефону после заявки. Стоимость зависит от объёма бассейна
и состояния воды — точную цену называет менеджер."""

DEFAULT_FAQ = """Как часто нужно чистить бассейн?
Частный уличный бассейн в сезон чистят раз в неделю, крытый — раз в две недели. Между чистками воду поддерживает химия и фильтрация.

Сколько стоит выезд мастера?
Цена зависит от расстояния и объёма работ. Оставьте заявку — менеджер перезвонит и назовёт точную сумму.

Вода позеленела, что делать?
Нужна ударная обработка хлором и альгицидом, затем чистка и промывка фильтра. Лучше вызвать мастера: он подберёт дозы по объёму бассейна.

Когда закрывать бассейн на зиму?
Когда температура воды держится ниже 10–12 °C. Консервацию лучше сделать до первых заморозков."""

DEFAULTS: dict[str, str] = {
    "miniapp_enabled": "1",
    "miniapp_title": "Сервис бассейнов",
    "miniapp_about": DEFAULT_ABOUT,
    "miniapp_services": DEFAULT_SERVICES,
    "miniapp_hours": "Пн–Сб с 9:00 до 19:00",
    "miniapp_phone": "",
    "miniapp_faq": DEFAULT_FAQ,
    "miniapp_bitrix_conn": "",
    "miniapp_bitrix_assigned": "",
    "miniapp_manager_max_ids": "",
    "miniapp_use_claude": "1",
    "miniapp_webhook_secret": "",
    "miniapp_bot_username": "",
    "miniapp_bot_id": "",
    "miniapp_subscribed_url": "",
}
TIME_SLOTS = [("any", "Любое время"), ("morning", "Утро, 9:00–12:00"), ("day", "День, 12:00–15:00"),
              ("evening", "Вечер, 15:00–19:00")]
TIME_TITLES = dict(TIME_SLOTS)


def setting(key: str) -> str:
    row = db.query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row is not None else DEFAULTS.get(key, "")


def save_settings(values: dict[str, str]) -> None:
    for key, value in values.items():
        if key in DEFAULTS:
            store.set_setting(key, value)


def webhook_secret() -> str:
    value = setting("miniapp_webhook_secret")
    if not value:
        value = pysecrets.token_urlsafe(24).replace("-", "a").replace("_", "b")
        store.set_setting("miniapp_webhook_secret", value)
    return value


def public_url() -> str:
    return store.get_setting("public_url").rstrip("/")


def app_url() -> str:
    base = public_url()
    return base + "/max/app" if base else ""


def webhook_url() -> str:
    base = public_url()
    return base + "/max/webhook" if base else ""


def services() -> list[dict]:
    out = []
    for line in setting("miniapp_services").splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, note = line.partition("|")
        out.append({"name": name.strip(), "note": note.strip()})
    return out


def faq() -> list[tuple[str, str]]:
    blocks = re.split(r"\n\s*\n", setting("miniapp_faq").strip())
    out = []
    for block in blocks:
        lines = [x.strip() for x in block.strip().splitlines() if x.strip()]
        if len(lines) >= 2:
            q = re.sub(r"^(В|Вопрос)\s*:\s*", "", lines[0], flags=re.I)
            a = " ".join(re.sub(r"^(О|Ответ)\s*:\s*", "", x, flags=re.I) for x in lines[1:])
            out.append((q, a))
    return out


# ------------------------------------------------------------- подключения

def bot_connection():
    conns = [c for c in store.list_connections("max_bot") if c.enabled]
    return conns[0] if conns else None


def bot_token() -> str:
    conn = bot_connection()
    return conn.secret("token") if conn else ""


def max_api() -> MaxApi | None:
    token = bot_token()
    return MaxApi(token, transport=TRANSPORTS["max"]) if token else None


def bitrix_connection():
    conns = [c for c in store.list_connections("bitrix24") if c.enabled]
    wanted = setting("miniapp_bitrix_conn")
    if wanted:
        return next((c for c in conns if str(c.id) == wanted), None)
    return conns[0] if conns else None


def crm() -> BitrixCrm | None:
    conn = bitrix_connection()
    if not conn:
        return None
    hook = conn.secret("webhook_url")
    return BitrixCrm(hook, transport=TRANSPORTS["bitrix"]) if hook else None


def bitrix_assigned() -> int | None:
    try:
        return int(setting("miniapp_bitrix_assigned")) or None
    except ValueError:
        return None


def claude_connection():
    conns = [c for c in store.list_connections("claude") if c.enabled]
    return conns[0] if conns else None


async def bot_identity(refresh: bool = False) -> tuple[str, str]:
    """(имя бота @username, его user_id) — нужны для кнопки «Открыть приложение»."""
    username, bot_id = setting("miniapp_bot_username"), setting("miniapp_bot_id")
    if username and not refresh:
        return username, bot_id
    api = max_api()
    if not api:
        return username, bot_id
    me = await api.me()
    username = str(me.get("username") or username or "")
    bot_id = str(me.get("user_id") or me.get("id") or bot_id or "")
    store.set_setting("miniapp_bot_username", username)
    store.set_setting("miniapp_bot_id", bot_id)
    return username, bot_id


# ----------------------------------------------------------------- клиенты

def get_client(max_id: str):
    return db.query_one("SELECT * FROM miniapp_clients WHERE max_id = ?", (str(max_id),))


def touch_client(user: dict, **fields: str) -> None:
    now = time.time()
    row = get_client(user["id"])
    if row is None:
        db.execute("INSERT INTO miniapp_clients(max_id, name, username, phone, address, created_at, last_seen_at) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (str(user["id"]), fields.get("name") or user.get("name", ""), user.get("username", ""),
                    fields.get("phone", ""), fields.get("address", ""), now, now))
        return
    updates = {"last_seen_at": now}
    if user.get("username"):
        updates["username"] = user["username"]
    for key in ("name", "phone", "address"):
        if fields.get(key):
            updates[key] = fields[key]
    if "bitrix_contact_id" in fields and fields["bitrix_contact_id"]:
        updates["bitrix_contact_id"] = fields["bitrix_contact_id"]
    sets = ", ".join(f"{k} = ?" for k in updates)
    db.execute(f"UPDATE miniapp_clients SET {sets} WHERE max_id = ?", (*updates.values(), str(user["id"])))


# ------------------------------------------------------------------ заявки

def validate_booking(form: dict, known_services: list[str] | None = None) -> tuple[dict, dict]:
    """Проверяет форму записи. Возвращает (чистые значения, ошибки по полям)."""
    errors: dict[str, str] = {}
    clean = {k: str(form.get(k) or "").strip() for k in
             ("name", "phone", "address", "service", "pool", "date", "time", "comment")}
    if len(clean["name"]) < 2:
        errors["name"] = "Напишите, как к вам обращаться."
    phone = normalize_phone(clean["phone"])
    if not phone:
        errors["phone"] = "Укажите телефон, по которому с вами свяжется менеджер."
    clean["phone"] = phone
    if not clean["service"]:
        errors["service"] = "Выберите услугу."
    elif known_services and clean["service"] not in known_services:
        errors["service"] = "Такой услуги нет в списке — выберите из списка."
    if clean["date"]:
        try:
            wanted = date.fromisoformat(clean["date"])
            if wanted < date.today():
                errors["date"] = "Эта дата уже прошла."
            elif (wanted - date.today()).days > 365:
                errors["date"] = "Слишком далёкая дата."
        except ValueError:
            errors["date"] = "Дата указана неверно."
    if clean["time"] and clean["time"] not in TIME_TITLES:
        clean["time"] = "any"
    for key, limit in (("name", 100), ("address", 300), ("pool", 200), ("comment", 2000)):
        clean[key] = clean[key][:limit]
    return clean, errors


def _fmt_date(value: str) -> str:
    try:
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value or "не указана"


def booking_text(b: dict, for_client: bool = False) -> str:
    lines = [f"Заявка №{b['id']}: {b['service']}"]
    if not for_client:
        lines.append(f"Клиент: {b['name']}, тел. {b['phone']}")
        if b.get("username"):
            lines.append(f"MAX: @{b['username']} (id {b['max_id']})")
    if b.get("address"):
        lines.append(f"Адрес: {b['address']}")
    if b.get("pool"):
        lines.append(f"Бассейн: {b['pool']}")
    lines.append(f"Желаемая дата: {_fmt_date(b.get('wish_date', ''))}, "
                 f"{TIME_TITLES.get(b.get('wish_time') or 'any', 'любое время').lower()}")
    if b.get("comment"):
        lines.append(f"Комментарий: {b['comment']}")
    return "\n".join(lines)


def get_booking(booking_id: int) -> dict | None:
    row = db.query_one("SELECT b.*, c.username FROM miniapp_bookings b LEFT JOIN miniapp_clients c "
                       "ON c.max_id = b.max_id WHERE b.id = ?", (booking_id,))
    return dict(row) if row else None


def list_bookings(max_id: str | None = None, limit: int = 100) -> list[dict]:
    if max_id:
        rows = db.query("SELECT b.*, c.username FROM miniapp_bookings b LEFT JOIN miniapp_clients c "
                        "ON c.max_id = b.max_id WHERE b.max_id = ? ORDER BY b.id DESC LIMIT ?", (str(max_id), limit))
    else:
        rows = db.query("SELECT b.*, c.username FROM miniapp_bookings b LEFT JOIN miniapp_clients c "
                        "ON c.max_id = b.max_id ORDER BY b.id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


async def create_booking(user: dict, form: dict) -> tuple[dict | None, dict]:
    clean, errors = validate_booking(form, [s["name"] for s in services()])
    if errors:
        return None, errors
    now = time.time()
    booking_id = db.execute(
        "INSERT INTO miniapp_bookings(max_id, name, phone, address, service, pool, wish_date, wish_time, comment, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (str(user["id"]), clean["name"], clean["phone"], clean["address"], clean["service"], clean["pool"],
         clean["date"], clean["time"] or "any", clean["comment"], now, now))
    touch_client(user, name=clean["name"], phone=clean["phone"], address=clean["address"])
    booking = get_booking(booking_id)
    assert booking is not None
    store.log_event("info", SECTION, f"Новая заявка №{booking_id}: {clean['service']} — {clean['name']}")
    await push_to_bitrix(booking)
    booking = get_booking(booking_id) or booking
    await _notify_about(booking, user)
    return booking, {}


async def push_to_bitrix(booking: dict) -> bool:
    """Создаёт лид в Битрикс24. Ошибка не ломает заявку — она сохраняется и видна в панели."""
    api = crm()
    if api is None:
        db.execute("UPDATE miniapp_bookings SET status = 'no_crm', updated_at = ? WHERE id = ?",
                   (time.time(), booking["id"]))
        return False
    try:
        client = get_client(booking["max_id"])
        contact_id = client["bitrix_contact_id"] if client and client["bitrix_contact_id"] else None
        if not contact_id:
            contact_id = await api.ensure_contact(booking["name"], booking["phone"], assigned=bitrix_assigned())
            if contact_id:
                db.execute("UPDATE miniapp_clients SET bitrix_contact_id = ? WHERE max_id = ?",
                           (contact_id, booking["max_id"]))
        kind = "Вопрос" if booking.get("kind") == "question" else "Заявка"
        lead_id = await api.create_lead(
            f"{kind} из MAX: {booking['service']} — {booking['name']}", name=booking["name"],
            phone=booking["phone"], comments=booking_text(booking), contact_id=contact_id,
            assigned=bitrix_assigned(), address=booking.get("address", ""))
    except CrmError as exc:
        db.execute("UPDATE miniapp_bookings SET status = 'error', bitrix_error = ?, updated_at = ? WHERE id = ?",
                   (f"{exc.message} {exc.action}".strip(), time.time(), booking["id"]))
        store.log_event("error", SECTION, f"Заявка №{booking['id']} не передана в Битрикс24: {exc.message}",
                        exc.details)
        return False
    db.execute("UPDATE miniapp_bookings SET status = 'sent', bitrix_lead_id = ?, bitrix_error = '', updated_at = ? "
               "WHERE id = ?", (lead_id, time.time(), booking["id"]))
    return True


def manager_ids() -> list[str]:
    raw = [x.strip() for x in re.split(r"[,\s;]+", setting("miniapp_manager_max_ids")) if x.strip()]
    if raw:
        return raw
    rows = db.query("SELECT max_id FROM users WHERE active = 1 AND max_id != '' AND role IN ('admin','director','manager')")
    return [r["max_id"] for r in rows]


async def send_to_user(user_id: str, text: str, buttons: list[list[dict]] | None = None) -> bool:
    api = max_api()
    if not api or not user_id or str(user_id).startswith("preview"):
        return False
    try:
        await api.send(text, user_id=user_id, buttons=buttons)
        return True
    except MaxError as exc:
        store.log_event("warning", SECTION, f"Не удалось отправить сообщение в MAX пользователю {user_id}: {exc.message}",
                        exc.details)
        return False


async def notify_managers(text: str) -> int:
    sent = 0
    for mid in manager_ids():
        if await send_to_user(mid, text):
            sent += 1
    return sent


async def _notify_about(booking: dict, user: dict) -> None:
    status = ("Передана в Битрикс24 (лид №%s)." % booking["bitrix_lead_id"] if booking.get("bitrix_lead_id")
              else "В Битрикс24 передать не удалось — см. панель, раздел «Сервис бассейнов».")
    await notify_managers("Новая заявка из MAX\n" + booking_text(booking) + "\n" + status)
    phone = setting("miniapp_phone")
    tail = f" Если срочно — звоните: {phone}." if phone else ""
    await send_to_user(user["id"], f"Спасибо, {booking['name']}! Заявка принята.\n{booking_text(booking, True)}\n"
                                   f"Менеджер свяжется с вами по телефону {booking['phone']}.{tail}")


# --------------------------------------------------------- статусы заявок

STATUS_TITLES = {"new": "Принята", "sent": "Передана менеджеру", "error": "Принята", "no_crm": "Принята",
                 "done": "Выполнена", "cancelled": "Отменена"}
ADMIN_STATUS_TITLES = {"new": "Новая", "sent": "В Битрикс24", "error": "Ошибка Битрикс24",
                       "no_crm": "Без Битрикс24", "done": "Выполнена", "cancelled": "Отменена"}
_status_cache: dict[str, Any] = {"ts": 0.0, "names": {}, "leads": {}}


async def attach_statuses(bookings: list[dict], admin: bool = False) -> list[dict]:
    """Дописывает каждой заявке понятный статус; для лидов берёт стадию из Битрикс24 (кэш на минуту)."""
    titles = ADMIN_STATUS_TITLES if admin else STATUS_TITLES
    ids = [b["bitrix_lead_id"] for b in bookings if b.get("bitrix_lead_id")]
    api = crm()
    if api and ids:
        now = time.time()
        missing = [i for i in ids if i not in _status_cache["leads"]]
        if now - _status_cache["ts"] > 60 or missing:
            try:
                if not _status_cache["names"] or now - _status_cache["ts"] > 3600:
                    _status_cache["names"] = await api.lead_statuses()
                _status_cache["leads"].update(await api.leads_status(ids))
                _status_cache["ts"] = now
            except CrmError as exc:
                log.warning("Статусы лидов не получены: %s", exc.message)
    for b in bookings:
        b["status_title"] = titles.get(b["status"], b["status"])
        lead = b.get("bitrix_lead_id")
        stage = _status_cache["leads"].get(lead) if lead else None
        if stage:
            b["stage"] = _status_cache["names"].get(stage, stage)
            b["status_title"] = b["stage"] if admin or stage not in ("NEW",) else "Принята"
            if stage == "CONVERTED":
                b["status_title"] = "Выполнена"
            elif stage == "JUNK":
                b["status_title"] = "Отменена"
        b["date_title"] = _fmt_date(b.get("wish_date", ""))
        b["time_title"] = TIME_TITLES.get(b.get("wish_time") or "any", "Любое время")
    return bookings


# --------------------------------------------------------- ответы на вопросы

SYSTEM_PROMPT = """Ты — помощник сервисной службы компании «{company}», которая обслуживает бассейны.
Отвечаешь клиентам в мессенджере MAX. Пиши по-русски, коротко (до 5 предложений), дружелюбно и по делу.
Опирайся только на сведения ниже. Цены, сроки и условия, которых нет в сведениях, не выдумывай —
скажи, что точно ответит менеджер.
Если клиент хочет записаться, вызвать мастера или заказать услугу — предложи нажать «Записаться на сервис»
в приложении (кнопка внизу) и не проси данные в чате.
Если вопроса нет в сведениях, он про цену/сроки, или клиент просит связаться с ним — поставь handoff: true
и напиши, что передашь вопрос менеджеру.
Ответ верни строго в виде JSON без пояснений: {{"answer": "текст ответа", "handoff": true или false}}.

О КОМПАНИИ
{about}

УСЛУГИ
{services}

ЧАСЫ РАБОТЫ: {hours}
ТЕЛЕФОН: {phone}

ЧАСТЫЕ ВОПРОСЫ
{faq}"""


def _knowledge() -> dict[str, str]:
    return {
        "company": store.get_setting("company_name") or setting("miniapp_title"),
        "about": setting("miniapp_about").strip() or "—",
        "services": "\n".join(f"- {s['name']}" + (f": {s['note']}" if s["note"] else "") for s in services()) or "—",
        "hours": setting("miniapp_hours") or "уточните у менеджера",
        "phone": setting("miniapp_phone") or "не указан — свяжемся через заявку",
        "faq": "\n\n".join(f"В: {q}\nО: {a}" for q, a in faq()) or "—",
    }


def _words(text: str) -> set[str]:
    return {w[:6] for w in re.findall(r"[а-яёa-z0-9]{3,}", text.lower())}


def faq_answer(question: str) -> str:
    """Запасной ответ без Claude: ищем самый похожий вопрос в базе знаний."""
    qw = _words(question)
    best, score = "", 0
    for q, a in faq():
        s = len(qw & _words(q))
        if s > score:
            best, score = a, s
    return best if score >= 2 else ""


def history(max_id: str, limit: int = 8) -> list[dict]:
    rows = db.query("SELECT role, text FROM miniapp_messages WHERE max_id = ? ORDER BY id DESC LIMIT ?",
                    (str(max_id), limit))
    return [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]


def remember(max_id: str, role: str, text: str, source: str) -> None:
    db.execute("INSERT INTO miniapp_messages(max_id, role, text, source, ts) VALUES(?,?,?,?,?)",
               (str(max_id), role, text[:4000], source, time.time()))
    db.execute("DELETE FROM miniapp_messages WHERE max_id = ? AND id NOT IN "
               "(SELECT id FROM miniapp_messages WHERE max_id = ? ORDER BY id DESC LIMIT 40)", (str(max_id), str(max_id)))


async def ask_claude(messages: list[dict]) -> dict | None:
    """Спрашивает Claude. None — если ядро не подключено, лимит исчерпан или сервис недоступен."""
    conn = claude_connection()
    if conn is None or setting("miniapp_use_claude") != "1":
        return None
    values = conn.values()
    month = datetime.now().strftime("%Y-%m")
    limit = float(values.get("monthly_limit") or 0)
    if limit and store.month_usage(month) >= limit:
        store.log_event("warning", SECTION, "Месячный лимит расходов на Claude исчерпан — отвечаю по базе знаний.")
        return None
    model = values.get("model") or MODELS[0][0]
    body: dict[str, Any] = {"model": model, "max_tokens": 700, "system": SYSTEM_PROMPT.format(**_knowledge()),
                            "messages": messages}
    headers = {"x-api-key": values.get("api_key", ""), "anthropic-version": "2023-06-01"}
    if model in ("claude-opus-5", "claude-fable-5-1"):
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        body["fallbacks"] = "default"
    kwargs: dict[str, Any] = {"timeout": 90}
    if TRANSPORTS["claude"] is not None:
        kwargs["transport"] = TRANSPORTS["claude"]
    elif store.get_setting("proxy_url"):
        kwargs["proxy"] = store.get_setting("proxy_url")
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        store.log_event("warning", SECTION, "Claude недоступен — отвечаю по базе знаний.", f"{type(exc).__name__}: {exc}")
        return None
    if resp.status_code != 200 or not isinstance(data, dict):
        err = (data.get("error") or {}) if isinstance(data, dict) else {}
        store.log_event("warning", SECTION, f"Claude ответил ошибкой (код {resp.status_code}) — отвечаю по базе знаний.",
                        str(err.get("message") or "")[:300])
        return None
    usage = data.get("usage") or {}
    tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    price_in, price_out = PRICES.get(data.get("model") or model, PRICES.get(model, (5.0, 25.0)))
    store.add_usage(month, tin * price_in / 1e6 + tout * price_out / 1e6, tin, tout)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    cleaned = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and parsed.get("answer"):
            return {"answer": str(parsed["answer"]).strip(), "handoff": bool(parsed.get("handoff"))}
    except json.JSONDecodeError:
        pass
    return {"answer": text, "handoff": False} if text else None


async def answer_question(user: dict, question: str, source: str = "app") -> dict:
    """Отвечает на вопрос клиента. Если ответа нет — передаёт вопрос менеджеру и в Битрикс24."""
    question = question.strip()[:2000]
    remember(user["id"], "user", question, source)
    reply = await ask_claude(history(user["id"]))
    if reply is None:
        text = faq_answer(question)
        reply = {"answer": text, "handoff": False} if text else {
            "answer": "Передам ваш вопрос менеджеру — он ответит в рабочее время"
                      + (f" ({setting('miniapp_hours')})" if setting("miniapp_hours") else "") + ".",
            "handoff": True}
    remember(user["id"], "assistant", reply["answer"], source)
    if reply["handoff"]:
        await _handoff(user, question)
    return reply


async def _handoff(user: dict, question: str) -> None:
    client = get_client(user["id"])
    now = time.time()
    booking_id = db.execute(
        "INSERT INTO miniapp_bookings(max_id, name, phone, address, service, comment, kind, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (str(user["id"]), (client["name"] if client else "") or user.get("name") or "Клиент из MAX",
         client["phone"] if client else "", client["address"] if client else "", "Вопрос клиента", question,
         "question", now, now))
    booking = get_booking(booking_id)
    if booking:
        await push_to_bitrix(booking)
        booking = get_booking(booking_id) or booking
        await notify_managers("Вопрос из MAX, на который помощник не ответил:\n" + booking_text(booking)
                              + ("\nЛид в Битрикс24 №%s" % booking["bitrix_lead_id"] if booking.get("bitrix_lead_id") else ""))


# ------------------------------------------------------------------ бот

async def app_buttons(extra_callback: bool = True) -> list[list[dict]]:
    username, bot_id = await bot_identity()
    rows: list[list[dict]] = []
    if username:
        rows.append([btn_open_app("Открыть приложение", username, bot_id)])
    if extra_callback:
        rows.append([btn_callback("Записаться на сервис", "book"), btn_callback("Задать вопрос", "ask")])
    return rows


def welcome_text(name: str) -> str:
    company = store.get_setting("company_name") or setting("miniapp_title")
    hello = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
    phone = f"\nТелефон: {setting('miniapp_phone')}" if setting("miniapp_phone") else ""
    return (f"{hello} Это сервис бассейнов «{company}».\n"
            "Здесь можно задать вопрос, записаться на чистку, запуск или выезд мастера.\n"
            "Нажмите «Открыть приложение» или просто напишите вопрос в чат."
            f"\nЧасы работы: {setting('miniapp_hours')}{phone}")


async def handle_update(update: dict) -> str:
    """Обрабатывает одно событие от MAX. Возвращает, что сделано (для журнала и тестов)."""
    u = parse_update(update)
    api = max_api()
    if not api or not u["user_id"]:
        return "ignored"
    user = {"id": str(u["user_id"]), "name": u["user_name"], "username": u["username"]}
    touch_client(user)
    if u["type"] in ("bot_started", "bot_added") or u["text"].lower().startswith("/start"):
        await send_to_user(user["id"], welcome_text(user["name"]), await app_buttons())
        return "welcome"
    if u["type"] == "message_callback":
        payload = str(u["payload"])
        if payload == "book":
            text = "Запись на сервис — в приложении: нажмите «Открыть приложение», затем «Записаться на сервис»."
        elif payload == "ask":
            text = "Напишите вопрос прямо в этот чат — отвечу сразу, а если не смогу, передам менеджеру."
        else:
            text = welcome_text(user["name"])
        try:
            await api.answer_callback(u["callback_id"], text=text, buttons=await app_buttons(False))
        except MaxError as exc:
            store.log_event("warning", SECTION, f"Ответ на кнопку не доставлен: {exc.message}", exc.details)
        return "callback:" + payload
    if u["type"] == "message_created" and u["text"]:
        reply = await answer_question(user, u["text"], source="bot")
        await send_to_user(user["id"], reply["answer"], await app_buttons(False))
        return "answered" + (":handoff" if reply["handoff"] else "")
    return "ignored"


def handle_update_background(update: dict) -> None:
    async def runner() -> None:
        try:
            await handle_update(update)
        except Exception as exc:  # noqa: BLE001 — бот не должен падать из-за одного события
            log.exception("Сбой обработки события MAX")
            store.log_event("error", SECTION, "Сбой обработки события от бота MAX", f"{type(exc).__name__}: {exc}")
    asyncio.get_running_loop().create_task(runner())


# ------------------------------------------------------------- подписка

async def subscribe_webhook() -> str:
    """Подписывает бота на события по адресу панели. Возвращает текст для сообщения в панели."""
    api = max_api()
    if api is None:
        raise MaxError("Бот MAX не подключён.", "Сначала добавьте подключение «Бот MAX» в разделе «Подключения».")
    url = webhook_url()
    if not url:
        raise MaxError("Не указан внешний адрес панели.", "Заполните «Внешний адрес панели» в «Настройках»: "
                       "MAX должен достучаться до сервера из интернета.")
    if not url.startswith("https://"):
        raise MaxError("Внешний адрес панели должен начинаться с https://.",
                       "MAX присылает события только на адреса с сертификатом. Загрузите сертификат в «Настройках».")
    username, _ = await bot_identity(refresh=True)
    secret = webhook_secret()
    for sub in await api.subscriptions():
        old = str(sub.get("url") or "")
        if old and old != url:
            try:
                await api.unsubscribe(old)
            except MaxError:
                pass
    await api.subscribe(url, secret, ["message_created", "message_callback", "bot_started", "bot_added"])
    store.set_setting("miniapp_subscribed_url", url)
    store.log_event("info", SECTION, f"Бот MAX @{username} подписан на события: {url}")
    return f"Бот @{username} подписан. Теперь он отвечает на сообщения и показывает кнопку приложения."


async def subscription_state() -> dict:
    """Что показать в панели: подписан ли бот и на какой адрес."""
    api = max_api()
    if api is None:
        return {"ok": False, "text": "Бот MAX не подключён."}
    try:
        subs = await api.subscriptions()
    except MaxError as exc:
        return {"ok": False, "text": exc.message, "action": exc.action}
    urls = [str(s.get("url") or "") for s in subs]
    want = webhook_url()
    if want and want in urls:
        return {"ok": True, "text": f"Бот подписан на {want}."}
    if urls:
        return {"ok": False, "text": "Бот подписан на другой адрес: " + ", ".join(urls),
                "action": "Нажмите «Подписать бота» — подписка переедет на адрес этой панели."}
    return {"ok": False, "text": "Бот не подписан на события — сообщения клиентов до панели не доходят.",
            "action": "Нажмите «Подписать бота»."}
