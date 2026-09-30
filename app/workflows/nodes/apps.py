"""Узлы интеграций: HTTP-запрос, Claude, 1С, Битрикс24, мессенджеры, Google, маркетплейсы, почта."""
from __future__ import annotations

import base64
import json
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import quote

from ...connectors.base import ConnectorError, basic_auth, http_request, json_or_none, short_body
from ...connectors.claude import MODELS as CLAUDE_MODELS
from .base import NodeContext, NodeError, NodeType, Param, item
from .core import get_path

G_APPS = "Сервисы"
G_AI = "ИИ"

KV_COLUMNS = [{"name": "key", "label": "Имя", "kind": "string"}, {"name": "value", "label": "Значение", "kind": "string"}]


def kv(ctx: NodeContext, name: str, index: int) -> dict:
    from ..expr import render

    ns = ctx.namespace(index)
    out = {}
    for row in ctx.raw(name) or []:
        key = str(row.get("key", "")).strip()
        if key:
            val = render(row.get("value", ""), ns)
            out[key] = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val)
    return out


def response_items(data, split_field: str = "") -> list[dict]:
    """Ответ сервиса → элементы: список становится отдельными элементами."""
    if split_field:
        data = get_path(data, split_field)
    if isinstance(data, list):
        return [item(x) for x in data]
    if data is None:
        return []
    return [item(data)]


async def call(ctx: NodeContext, method: str, url: str, *, service: str, cctx=None, **kwargs):
    try:
        return await http_request(method, url, cctx or ctx.check_context(), service=service, **kwargs)
    except ConnectorError as exc:
        raise NodeError(exc.message, exc.action, exc.details) from exc


def http_error(service: str, resp, hint: str = "") -> NodeError:
    body = short_body(resp, 500)
    return NodeError(f"{service} ответил ошибкой {resp.status_code}: {body or 'без описания'}",
                     hint or "Проверьте адрес и параметры запроса.", body)


SPLIT_PARAM = Param("split_field", "Разбить на элементы по полю", "string", "",
                    hint="Если нужный список лежит внутри ответа (например result.items) — укажите путь. "
                         "Пусто: список в ответе сам становится элементами.")

# --------------------------------------------------------------- HTTP-запрос

AUTH_BY_CONN = {
    "wildberries": lambda v: {"Authorization": v.get("token", "")},
    "ozon": lambda v: {"Client-Id": v.get("client_id", ""), "Api-Key": v.get("api_key", "")},
    "yandex_market": lambda v: {"Api-Key": v.get("api_key", "")},
    "claude": lambda v: {"x-api-key": v.get("api_key", ""), "anthropic-version": "2023-06-01"},
    "max_bot": lambda v: {"Authorization": v.get("token", "")},
    "onec": lambda v: {"Authorization": basic_auth(v.get("username", ""), v.get("password", ""))},
}


async def http_exec(ctx: NodeContext):
    out = []
    for i in range(max(1, len(ctx.items))):
        method = (ctx.raw("method") or "GET").upper()
        url = ctx.param_str("url", i).strip()
        if not url:
            raise NodeError("Не указан адрес запроса (URL).")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        headers = kv(ctx, "headers", i)
        query = kv(ctx, "query", i)
        auth = ctx.raw("auth") or "none"
        cctx = ctx.check_context()
        if auth == "connection":
            conn, values, ctype = ctx.connection("connection", i)
            if conn.type not in AUTH_BY_CONN:
                raise NodeError(f"Подключение «{conn.name}» нельзя использовать для HTTP-запроса.",
                                "Для этого сервиса есть свой узел в разделе «Сервисы».")
            headers.update(AUTH_BY_CONN[conn.type](values))
            cctx = ctx.check_context(ctype)
        elif auth == "bearer":
            headers["Authorization"] = "Bearer " + ctx.param_str("token", i).strip()
        elif auth == "basic":
            headers["Authorization"] = basic_auth(ctx.param_str("user", i), ctx.param_str("password", i))
        elif auth == "header":
            headers[ctx.param_str("header_name", i) or "X-Api-Key"] = ctx.param_str("header_value", i)
        kwargs: dict = {"headers": headers, "params": query or None}
        body_type = ctx.raw("body_type") or "none"
        if method in ("POST", "PUT", "PATCH", "DELETE") and body_type != "none":
            if body_type == "json":
                kwargs["json"] = ctx.param_json("body_json", i, {})
            elif body_type == "form":
                kwargs["data"] = kv(ctx, "body_form", i)
            else:
                kwargs["content"] = ctx.param_str("body_raw", i).encode("utf-8")
                headers.setdefault("Content-Type", ctx.param_str("content_type", i) or "text/plain; charset=utf-8")
        resp = await call(ctx, method, url, service=url.split("/")[2], cctx=cctx,
                          timeout=float(ctx.raw("timeout") or 30), verify=not ctx.param_bool("ignore_ssl"), **kwargs)
        if resp.status_code >= 400 and not ctx.param_bool("ignore_errors"):
            raise http_error("Сервер", resp, "Код 401/403 — проблема с доступом, 404 — неверный адрес, "
                                              "5xx — сбой на стороне сервиса.")
        data = json_or_none(resp)
        if data is None and (ctx.raw("response") or "auto") != "json":
            data = {"data": resp.text}
        if ctx.param_bool("full_response"):
            out.append(item({"statusCode": resp.status_code, "headers": dict(resp.headers), "body": data}))
        else:
            out.extend(response_items(data, ctx.param_str("split_field", i)))
        if not ctx.items:
            break
    return [out]


HTTP = NodeType(
    "http_request", "HTTP-запрос", G_APPS,
    "Запрос к любому API по адресу. Для сервисов из «Подключений» ключ подставляется сам.",
    http_exec, icon="⇄", color="#0369a1", n8n_types=["n8n-nodes-base.httpRequest"],
    params=[
        Param("method", "Метод", "select", "GET", no_expr=True,
              options=[(m, m) for m in ("GET", "POST", "PUT", "PATCH", "DELETE")]),
        Param("url", "Адрес (URL)", "string", "", required=True, placeholder="https://api.example.ru/v1/orders"),
        Param("auth", "Авторизация", "select", "none", no_expr=True, options=[
            ("none", "Без авторизации"), ("connection", "Ключ из подключения"), ("bearer", "Bearer-токен"),
            ("basic", "Логин и пароль"), ("header", "Ключ в заголовке")]),
        Param("connection", "Подключение", "connection", "", show_if={"auth": ["connection"]},
              conn_types=list(AUTH_BY_CONN)),
        Param("token", "Токен", "string", "", show_if={"auth": ["bearer"]}),
        Param("user", "Логин", "string", "", show_if={"auth": ["basic"]}),
        Param("password", "Пароль", "string", "", show_if={"auth": ["basic"]}),
        Param("header_name", "Имя заголовка", "string", "X-Api-Key", show_if={"auth": ["header"]}),
        Param("header_value", "Значение", "string", "", show_if={"auth": ["header"]}),
        Param("query", "Параметры адреса (?a=1)", "list", [], columns=KV_COLUMNS),
        Param("headers", "Заголовки", "list", [], columns=KV_COLUMNS),
        Param("body_type", "Тело запроса", "select", "none", no_expr=True,
              options=[("none", "Без тела"), ("json", "JSON"), ("form", "Форма"), ("raw", "Текст")]),
        Param("body_json", "JSON", "json", "{\n  \n}", show_if={"body_type": ["json"]}),
        Param("body_form", "Поля формы", "list", [], columns=KV_COLUMNS, show_if={"body_type": ["form"]}),
        Param("body_raw", "Текст", "text", "", show_if={"body_type": ["raw"]}),
        Param("content_type", "Content-Type", "string", "text/plain; charset=utf-8", show_if={"body_type": ["raw"]}),
        SPLIT_PARAM,
        Param("full_response", "Вернуть код ответа и заголовки", "boolean", False),
        Param("ignore_errors", "Не считать ошибкой коды 4xx/5xx", "boolean", False),
        Param("ignore_ssl", "Не проверять сертификат", "boolean", False),
        Param("timeout", "Ждать ответа, секунд", "number", 30, no_expr=True),
    ],
)

# -------------------------------------------------------------------- Claude


async def claude_exec(ctx: NodeContext):
    from ... import store
    from ...connectors.claude import MODELS, PRICES

    out = []
    for i in range(max(1, len(ctx.items))):
        conn, values, ctype = ctx.connection("connection", i)
        model = ctx.raw("model") or ""
        if not model or model == "default":
            model = values.get("model") or MODELS[0][0]
        month = datetime.now().strftime("%Y-%m")
        limit = float(values.get("monthly_limit") or 0)
        spent = store.month_usage(month)
        if limit and spent >= limit:
            raise NodeError(f"Месячный лимит расходов на Claude исчерпан: ${spent:.2f} из ${limit:.0f}.",
                            "Увеличьте лимит в подключении «Ядро Claude» или дождитесь начала месяца.")
        prompt = ctx.param_str("prompt", i)
        if not prompt.strip():
            raise NodeError("Пустой запрос к Claude.", "Заполните поле «Запрос».")
        system = ctx.param_str("system", i)
        want_json = ctx.param_bool("json_output")
        if want_json:
            system = (system + "\n\n" if system else "") + "Ответь только корректным JSON без пояснений и без ```."
        body: dict = {"model": model, "max_tokens": int(ctx.raw("max_tokens") or 4096),
                      "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        headers = {"x-api-key": values.get("api_key", ""), "anthropic-version": "2023-06-01"}
        if model in ("claude-opus-5", "claude-fable-5-1"):
            # при отказе модели по соображениям безопасности сервер сам попробует запасную модель
            headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
            body["fallbacks"] = "default"
        resp = await call(ctx, "POST", "https://api.anthropic.com/v1/messages", service="Claude API",
                          cctx=ctx.check_context(ctype), headers=headers, json=body, timeout=300, use_proxy=True)
        data = json_or_none(resp) or {}
        if resp.status_code != 200:
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            msg = str(err.get("message", ""))
            if resp.status_code == 401:
                raise NodeError("Ключ Claude API неверный.", "Проверьте подключение «Ядро Claude».")
            if resp.status_code == 403:
                raise NodeError("Claude API отклоняет запрос (страна сервера или права ключа).",
                                "Укажите прокси в «Настройках».", msg)
            if "credit balance" in msg.lower():
                raise NodeError("На счёте Anthropic закончились деньги.", "Пополните баланс на platform.claude.com → Billing.")
            if resp.status_code in (429, 529) or resp.status_code >= 500:
                raise NodeError(f"Claude сейчас перегружен или ограничивает частоту (код {resp.status_code}).",
                                "Включите в настройках узла «Повторять при ошибке».", msg)
            raise NodeError(f"Claude API вернул ошибку: {msg or resp.status_code}", "", short_body(resp))
        usage = data.get("usage") or {}
        tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        price_in, price_out = PRICES.get(data.get("model") or model, PRICES.get(model, (5.0, 25.0)))
        store.add_usage(month, tin * price_in / 1e6 + tout * price_out / 1e6, tin, tout)
        if data.get("stop_reason") == "refusal":
            raise NodeError("Claude отказался отвечать на этот запрос (сработала защита модели).",
                            "Переформулируйте запрос.")
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        if data.get("stop_reason") == "max_tokens":
            ctx.log("Ответ обрезан: не хватило «Максимум токенов в ответе».")
        result = dict(ctx.items[i]["json"]) if (ctx.items and ctx.param_bool("keep_input")) else {}
        field = ctx.param_str("output", i) or "ответ"
        if want_json:
            cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            try:
                result[field] = json.loads(cleaned)
            except json.JSONDecodeError:
                raise NodeError("Claude вернул не JSON, хотя его просили.", "Уточните запрос.", text[:1000]) from None
        else:
            result[field] = text
        result["_токены"] = {"вход": tin, "выход": tout}
        out.append(item(result))
        if not ctx.items:
            break
    return [out]


CLAUDE = NodeType(
    "claude", "Claude (ИИ)", G_AI,
    "Задаёт вопрос Claude и записывает ответ в поле. Можно подставлять данные: «Кратко перескажи: {{ $json.текст }}».",
    claude_exec, icon="✦", color="#c2410c",
    n8n_types=["@n8n/n8n-nodes-langchain.anthropic", "@n8n/n8n-nodes-langchain.lmChatAnthropic"],
    params=[
        Param("connection", "Подключение", "connection", "", conn_types=["claude"]),
        Param("prompt", "Запрос", "text", "", required=True, placeholder="Составь короткий ответ клиенту: {{ $json.вопрос }}"),
        Param("system", "Инструкция (роль)", "text", "Ты помощник компании. Отвечай кратко и по-русски."),
        Param("model", "Модель", "select", "default", no_expr=True,
              options=[("default", "Как в подключении")] + list(CLAUDE_MODELS)),
        Param("max_tokens", "Максимум токенов в ответе", "number", 4096, no_expr=True),
        Param("json_output", "Ответ в формате JSON", "boolean", False,
              hint="Claude вернёт JSON, и его поля можно будет использовать дальше."),
        Param("output", "Записать ответ в поле", "string", "ответ"),
        Param("keep_input", "Сохранить входные поля", "boolean", True),
    ],
)

# ------------------------------------------------------------------------ 1С


async def onec_exec(ctx: NodeContext):
    from ...connectors import onec

    out = []
    for i in range(max(1, len(ctx.items))):
        conn, values, ctype = ctx.connection("connection", i)
        op = ctx.raw("operation") or "list"
        try:
            if op in ("list", "get"):
                entity = ctx.param_str("entity", i).strip()
                if not entity:
                    raise NodeError("Не указан объект 1С.",
                                    "Выберите его из подсказок или посмотрите в «Обозревателе 1С» на странице подключения.")
                params = {}
                allowed = ("filter", "select", "orderby", "expand") if op == "list" else ("select", "expand")
                for p in allowed:
                    params["$" + p] = ctx.param_str(p, i).strip()
                if op == "get":
                    entity += onec.odata_key(ctx.param_str("key", i))
                records = await onec.odata_query(
                    values, ctx.check_context(ctype), entity, params,
                    top=int(ctx.param_num("top", i, 100)), all_pages=ctx.param_bool("all_pages", i),
                    max_records=int(ctx.param_num("max_records", i, 100000)))
                out.extend(item(r) for r in records)
                ctx.log(f"{entity}: получено записей — {len(records)}")
            else:
                base = (values.get("base_url") or "").rstrip("/")
                path = ctx.param_str("hs_path", i).strip()
                if not path.startswith("/"):
                    path = "/" + path
                method = (ctx.raw("hs_method") or "GET").upper()
                kwargs = {"json": ctx.param_json("hs_body", i, {})} if method != "GET" else {}
                resp = await http_request(method, base + path, ctx.check_context(ctype), service="1С",
                                          headers=onec.odata_headers(values),
                                          verify=bool(values.get("verify_ssl", True)), timeout=180, **kwargs)
                onec.odata_raise(resp, path)
                data = json_or_none(resp)
                out.extend(response_items(data) if data is not None else [item({"data": resp.text})])
        except ConnectorError as exc:
            raise NodeError(exc.message, exc.action, exc.details) from exc
        if not ctx.items:
            break
    return [out]


ONEC = NodeType(
    "onec", "1С:Предприятие", G_APPS,
    "Читает справочники, документы и регистры 1С через OData или вызывает HTTP-сервис расширения.",
    onec_exec, icon="1С", color="#b91c1c",
    params=[
        Param("connection", "Подключение", "connection", "", conn_types=["onec"]),
        Param("operation", "Что сделать", "select", "list", no_expr=True, options=[
            ("list", "Получить список (OData)"), ("get", "Получить один объект по ссылке (OData)"),
            ("hs", "Вызвать HTTP-сервис")]),
        Param("entity", "Объект", "string", "Catalog_Номенклатура", show_if={"operation": ["list", "get"]},
              suggest="onec_entities",
              hint="Начните вводить — появятся объекты вашей 1С. Catalog_… — справочник, Document_… — документ, "
                   "AccumulationRegister_…/Balance() — остатки регистра."),
        Param("key", "Ссылка (GUID)", "string", "", show_if={"operation": ["get"]}),
        Param("filter", "Отбор ($filter)", "string", "", show_if={"operation": ["list"]},
              placeholder="DeletionMark eq false", hint="Синтаксис OData: eq, ne, gt, lt, and, or."),
        Param("select", "Какие поля ($select)", "string", "", show_if={"operation": ["list", "get"]},
              placeholder="Ref_Key,Description,Артикул"),
        Param("orderby", "Сортировка ($orderby)", "string", "", show_if={"operation": ["list"]}),
        Param("expand", "Раскрыть ссылки ($expand)", "string", "", show_if={"operation": ["list", "get"]}),
        Param("top", "Сколько записей", "number", 100, show_if={"operation": ["list"]}),
        Param("all_pages", "Забрать все записи (порциями по 500)", "boolean", False, show_if={"operation": ["list"]},
              hint="Для больших справочников: 1С отдаёт их частями, платформа склеит."),
        Param("max_records", "Но не больше", "number", 100000, show_if={"all_pages": [True]}),
        Param("hs_method", "Метод", "select", "GET", show_if={"operation": ["hs"]}, no_expr=True,
              options=[("GET", "GET"), ("POST", "POST")]),
        Param("hs_path", "Путь", "string", "/hs/platform/ping", show_if={"operation": ["hs"]}),
        Param("hs_body", "Тело (JSON)", "json", "{}", show_if={"operation": ["hs"]}),
    ],
)

# --------------------------------------------------------------- Битрикс24


async def bitrix_exec(ctx: NodeContext):
    out = []
    for i in range(max(1, len(ctx.items))):
        conn, values, ctype = ctx.connection("connection", i)
        hook = values.get("webhook_url", "")
        method = ctx.param_str("method", i).strip().removesuffix(".json")
        if not method:
            raise NodeError("Не указан метод Битрикс24.", "Например: crm.deal.list, tasks.task.add, im.notify.")
        params = ctx.param_json("params", i, {}) or {}
        pages = int(ctx.param_num("pages", i, 1)) if ctx.param_bool("all_pages") else 1
        start = 0
        for _ in range(max(1, min(pages, 200))):
            body = dict(params)
            if start:
                body["start"] = start
            resp = await call(ctx, "POST", hook + method + ".json", service="Битрикс24", json=body, timeout=60)
            data = json_or_none(resp) or {}
            if resp.status_code >= 400 or "error" in data:
                desc = data.get("error_description") or data.get("error") or resp.status_code
                if data.get("error") == "ERROR_METHOD_NOT_FOUND":
                    raise NodeError(f"В Битрикс24 нет метода «{method}».", "Проверьте название метода в документации "
                                                                            "apidocs.bitrix24.ru.")
                if data.get("error") == "insufficient_scope":
                    raise NodeError("У вебхука не хватает прав на этот метод.",
                                    "Откройте вебхук в Битрикс24 и добавьте нужное право.")
                raise NodeError(f"Битрикс24: {desc}")
            result = data.get("result")
            if isinstance(result, dict) and len(result) == 1 and isinstance(next(iter(result.values())), list):
                result = next(iter(result.values()))  # tasks.task.list → {"tasks": [...]}
            out.extend(response_items(result))
            nxt = data.get("next")
            if not nxt:
                break
            start = nxt
        if not ctx.items:
            break
    return [out]


BITRIX = NodeType(
    "bitrix24", "Битрикс24", G_APPS,
    "Вызывает любой метод REST API Битрикс24: сделки, контакты, задачи, уведомления.",
    bitrix_exec, icon="B24", color="#0284c7", n8n_types=["n8n-nodes-base.bitrix24"],
    params=[
        Param("connection", "Подключение", "connection", "", conn_types=["bitrix24"]),
        Param("method", "Метод", "string", "crm.deal.list",
              hint="crm.deal.list — сделки, crm.deal.add — новая сделка, tasks.task.add — задача, "
                   "im.notify.system.add — уведомление."),
        Param("params", "Параметры (JSON)", "json", '{\n  "select": ["ID", "TITLE", "OPPORTUNITY"]\n}'),
        Param("all_pages", "Получить все страницы (по 50 записей)", "boolean", False),
        Param("pages", "Не больше страниц", "number", 20, show_if={"all_pages": [True]}),
    ],
)

# ------------------------------------------------------------ мессенджеры


async def telegram_exec(ctx: NodeContext):
    out = []
    op = ctx.raw("operation") or "send"
    for i in range(max(1, len(ctx.items))):
        conn, values, ctype = ctx.connection("connection", i)
        token = values.get("token", "")
        base = f"https://api.telegram.org/bot{token}/"
        if op == "updates":
            resp = await call(ctx, "GET", base + "getUpdates", service="Telegram", cctx=ctx.check_context(ctype),
                              use_proxy=True, timeout=30)
            data = json_or_none(resp) or {}
            out.extend(item({"chat_id": (u.get("message") or {}).get("chat", {}).get("id"),
                             "от": (u.get("message") or {}).get("from", {}).get("first_name"),
                             "текст": (u.get("message") or {}).get("text"), "update": u}) for u in data.get("result", []))
            break
        body = {"chat_id": ctx.param_str("chat_id", i), "text": ctx.param_str("text", i)[:4096],
                "disable_web_page_preview": True}
        if (ctx.raw("parse_mode") or "") in ("HTML", "MarkdownV2"):
            body["parse_mode"] = ctx.raw("parse_mode")
        resp = await call(ctx, "POST", base + "sendMessage", service="Telegram", cctx=ctx.check_context(ctype),
                          use_proxy=True, json=body, timeout=30)
        data = json_or_none(resp) or {}
        if not data.get("ok"):
            desc = str(data.get("description", resp.status_code))
            if "chat not found" in desc:
                raise NodeError("Telegram: чат не найден.", "Сотрудник должен сначала написать боту /start. "
                                                           "Узнать chat_id можно операцией «Получить сообщения боту».")
            if "blocked" in desc:
                raise NodeError("Telegram: пользователь заблокировал бота.")
            raise NodeError(f"Telegram: {desc}".replace(token, "***"))
        out.append(item({"отправлено": True, "message_id": data["result"].get("message_id"), "chat_id": body["chat_id"]}))
        if not ctx.items:
            break
    return [out]


TELEGRAM = NodeType(
    "telegram", "Telegram", G_APPS, "Отправляет сообщение от бота Telegram или получает последние сообщения боту.",
    telegram_exec, icon="✈", color="#0ea5e9", n8n_types=["n8n-nodes-base.telegram"],
    params=[
        Param("connection", "Подключение", "connection", "", conn_types=["telegram_bot"]),
        Param("operation", "Что сделать", "select", "send", no_expr=True,
              options=[("send", "Отправить сообщение"), ("updates", "Получить сообщения боту (узнать chat_id)")]),
        Param("chat_id", "Кому (chat_id)", "string", "", show_if={"operation": ["send"]}),
        Param("text", "Текст", "text", "", show_if={"operation": ["send"]}),
        Param("parse_mode", "Оформление", "select", "", show_if={"operation": ["send"]}, no_expr=True,
              options=[("", "Обычный текст"), ("HTML", "HTML (<b>жирный</b>)"), ("MarkdownV2", "Markdown")]),
    ],
)

MAX_API = "https://platform-api.max.ru"


async def max_exec(ctx: NodeContext):
    out = []
    op = ctx.raw("operation") or "send"
    for i in range(max(1, len(ctx.items))):
        conn, values, ctype = ctx.connection("connection", i)
        headers = {"Authorization": values.get("token", "")}
        if op == "updates":
            resp = await call(ctx, "GET", MAX_API + "/updates", service="MAX", headers=headers, timeout=40,
                              params={"limit": "100", "timeout": "0"})
            data = json_or_none(resp) or {}
            for u in data.get("updates", []):
                msg = u.get("message") or {}
                out.append(item({"тип": u.get("update_type"), "user_id": (msg.get("sender") or {}).get("user_id"),
                                 "chat_id": (msg.get("recipient") or {}).get("chat_id"),
                                 "текст": (msg.get("body") or {}).get("text"), "update": u}))
            break
        kind = ctx.raw("recipient") or "user_id"
        rid = ctx.param_str("recipient_id", i).strip()
        body = {"text": ctx.param_str("text", i)[:4000]}
        if (ctx.raw("format") or "") in ("markdown", "html"):
            body["format"] = ctx.raw("format")
        resp = await call(ctx, "POST", MAX_API + "/messages", service="MAX", headers=headers, json=body,
                          params={kind: rid}, timeout=30)
        if resp.status_code in (401, 403):
            raise NodeError("MAX не принял токен бота.", "Проверьте подключение «Бот MAX».")
        if resp.status_code >= 400:
            raise http_error("MAX", resp, "Проверьте ID получателя: пользователь должен сначала написать боту.")
        data = json_or_none(resp) or {}
        out.append(item({"отправлено": True, "message": data.get("message", data)}))
        if not ctx.items:
            break
    return [out]


MAX_NODE = NodeType(
    "max", "MAX", G_APPS, "Отправляет сообщение от бота MAX или получает последние сообщения боту.",
    max_exec, icon="M", color="#6d28d9",
    params=[
        Param("connection", "Подключение", "connection", "", conn_types=["max_bot"]),
        Param("operation", "Что сделать", "select", "send", no_expr=True,
              options=[("send", "Отправить сообщение"), ("updates", "Получить сообщения боту")]),
        Param("recipient", "Кому", "select", "user_id", show_if={"operation": ["send"]}, no_expr=True,
              options=[("user_id", "Пользователю (user_id)"), ("chat_id", "В чат (chat_id)")]),
        Param("recipient_id", "ID получателя", "string", "", show_if={"operation": ["send"]}),
        Param("text", "Текст", "text", "", show_if={"operation": ["send"]}),
        Param("format", "Оформление", "select", "", show_if={"operation": ["send"]}, no_expr=True,
              options=[("", "Обычный текст"), ("markdown", "Markdown"), ("html", "HTML")]),
    ],
)

# ------------------------------------------------------------------- Google


async def google_token(ctx: NodeContext, i: int):
    from ...connectors import _google_app_values
    from ...connectors import google as g

    conn, values, ctype = ctx.connection("connection", i)
    cctx = ctx.check_context(ctype)
    try:
        token = await g._access_token(values, _google_app_values(), cctx)  # noqa: SLF001
    except ConnectorError as exc:
        raise NodeError(exc.message, exc.action) from exc
    return {"Authorization": "Bearer " + token}, cctx


def _header(msg: dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _body_text(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and (payload.get("body") or {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        t = _body_text(part)
        if t:
            return t
    return ""


async def gmail_exec(ctx: NodeContext):
    out = []
    op = ctx.raw("operation") or "send"
    api = "https://gmail.googleapis.com/gmail/v1/users/me"
    for i in range(max(1, len(ctx.items))):
        headers, cctx = await google_token(ctx, i)
        kw = {"service": "Gmail", "cctx": cctx, "use_proxy": True, "headers": headers, "timeout": 60}
        if op == "send":
            msg = EmailMessage()
            msg["To"] = ctx.param_str("to", i)
            if ctx.param_str("cc", i):
                msg["Cc"] = ctx.param_str("cc", i)
            msg["Subject"] = ctx.param_str("subject", i)
            text = ctx.param_str("body", i)
            if ctx.param_bool("html"):
                msg.set_content("Письмо в формате HTML")
                msg.add_alternative(text, subtype="html")
            else:
                msg.set_content(text)
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            resp = await call(ctx, "POST", api + "/messages/send", json={"raw": raw}, **kw)
            if resp.status_code >= 400:
                raise http_error("Gmail", resp)
            out.append(item({"отправлено": True, "id": (json_or_none(resp) or {}).get("id"), "кому": msg["To"]}))
        elif op == "search":
            resp = await call(ctx, "GET", api + "/messages", params={"q": ctx.param_str("query", i),
                                                                     "maxResults": str(int(ctx.param_num("limit", i, 10)))}, **kw)
            if resp.status_code >= 400:
                raise http_error("Gmail", resp)
            for m in (json_or_none(resp) or {}).get("messages", []):
                full = await call(ctx, "GET", f"{api}/messages/{m['id']}", params={"format": "full"}, **kw)
                d = json_or_none(full) or {}
                out.append(item({"id": d.get("id"), "thread_id": d.get("threadId"), "от": _header(d, "From"),
                                 "тема": _header(d, "Subject"), "дата": _header(d, "Date"), "кратко": d.get("snippet"),
                                 "текст": _body_text(d.get("payload") or {})[:20000],
                                 "непрочитано": "UNREAD" in (d.get("labelIds") or [])}))
        elif op == "mark_read":
            mid = ctx.param_str("message_id", i)
            resp = await call(ctx, "POST", f"{api}/messages/{mid}/modify", json={"removeLabelIds": ["UNREAD"]}, **kw)
            if resp.status_code >= 400:
                raise http_error("Gmail", resp)
            out.append(item({"id": mid, "прочитано": True}))
        if not ctx.items:
            break
    return [out]


GMAIL = NodeType(
    "gmail", "Gmail", G_APPS, "Отправляет письма, ищет письма и отмечает прочитанными — от имени подключённого аккаунта.",
    gmail_exec, icon="✉", color="#dc2626", n8n_types=["n8n-nodes-base.gmail"],
    params=[
        Param("connection", "Аккаунт", "connection", "", conn_types=["google_account"]),
        Param("operation", "Что сделать", "select", "send", no_expr=True, options=[
            ("send", "Отправить письмо"), ("search", "Найти письма"), ("mark_read", "Отметить прочитанным")]),
        Param("to", "Кому", "string", "", show_if={"operation": ["send"]}, hint="Несколько адресов — через запятую."),
        Param("cc", "Копия", "string", "", show_if={"operation": ["send"]}),
        Param("subject", "Тема", "string", "", show_if={"operation": ["send"]}),
        Param("body", "Текст письма", "text", "", show_if={"operation": ["send"]}),
        Param("html", "Текст в HTML", "boolean", False, show_if={"operation": ["send"]}),
        Param("query", "Поиск", "string", "is:unread newer_than:1d", show_if={"operation": ["search"]},
              hint="Как в строке поиска Gmail: from:ozon.ru subject:заказ is:unread."),
        Param("limit", "Сколько писем", "number", 10, show_if={"operation": ["search"]}),
        Param("message_id", "ID письма", "string", "{{ $json.id }}", show_if={"operation": ["mark_read"]}),
    ],
)


async def drive_exec(ctx: NodeContext):
    out = []
    op = ctx.raw("operation") or "list"
    for i in range(max(1, len(ctx.items))):
        headers, cctx = await google_token(ctx, i)
        kw = {"service": "Google Диск", "cctx": cctx, "use_proxy": True, "headers": headers, "timeout": 120}
        if op == "list":
            q = ctx.param_str("query", i).strip()
            params = {"pageSize": str(int(ctx.param_num("limit", i, 50))),
                      "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink,parents)"}
            if q:
                params["q"] = q if ("=" in q or " contains " in q) else f"name contains '{q}' and trashed = false"
            resp = await call(ctx, "GET", "https://www.googleapis.com/drive/v3/files", params=params, **kw)
            if resp.status_code >= 400:
                raise http_error("Google Диск", resp)
            out.extend(item(f) for f in (json_or_none(resp) or {}).get("files", []))
        elif op == "upload":
            meta = {"name": ctx.param_str("name", i) or "файл.txt"}
            if ctx.param_str("folder_id", i):
                meta["parents"] = [ctx.param_str("folder_id", i)]
            mime = ctx.param_str("mime", i) or "text/plain"
            boundary = "platforma-boundary"
            content = ctx.param_str("content", i)
            body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                    f"{json.dumps(meta, ensure_ascii=False)}\r\n--{boundary}\r\nContent-Type: {mime}; charset=UTF-8\r\n\r\n"
                    f"{content}\r\n--{boundary}--").encode("utf-8")
            h = dict(headers, **{"Content-Type": f"multipart/related; boundary={boundary}"})
            kw["headers"] = h
            resp = await call(ctx, "POST", "https://www.googleapis.com/upload/drive/v3/files",
                              params={"uploadType": "multipart", "fields": "id,name,webViewLink"}, content=body, **kw)
            if resp.status_code >= 400:
                raise http_error("Google Диск", resp)
            out.append(item(json_or_none(resp) or {}))
        elif op == "download":
            fid = ctx.param_str("file_id", i).strip()
            meta = await call(ctx, "GET", f"https://www.googleapis.com/drive/v3/files/{fid}",
                              params={"fields": "id,name,mimeType"}, **kw)
            if meta.status_code >= 400:
                raise http_error("Google Диск", meta, "Проверьте ID файла и что у аккаунта есть к нему доступ.")
            m = json_or_none(meta) or {}
            export = {"application/vnd.google-apps.document": "text/plain",
                      "application/vnd.google-apps.spreadsheet": "text/csv"}.get(m.get("mimeType", ""))
            if export:
                resp = await call(ctx, "GET", f"https://www.googleapis.com/drive/v3/files/{fid}/export",
                                  params={"mimeType": export}, **kw)
            else:
                resp = await call(ctx, "GET", f"https://www.googleapis.com/drive/v3/files/{fid}", params={"alt": "media"},
                                  **kw)
            if resp.status_code >= 400:
                raise http_error("Google Диск", resp)
            out.append(item({"id": fid, "имя": m.get("name"), "текст": resp.text[:500000]}))
        if not ctx.items:
            break
    return [out]


DRIVE = NodeType(
    "gdrive", "Google Диск", G_APPS, "Список файлов, загрузка текста или таблицы, чтение содержимого файла.",
    drive_exec, icon="▲", color="#16a34a", n8n_types=["n8n-nodes-base.googleDrive"],
    params=[
        Param("connection", "Аккаунт", "connection", "", conn_types=["google_account"]),
        Param("operation", "Что сделать", "select", "list", no_expr=True, options=[
            ("list", "Найти файлы"), ("upload", "Загрузить файл (текст / CSV)"), ("download", "Прочитать файл")]),
        Param("query", "Название содержит", "string", "", show_if={"operation": ["list"]},
              hint="Можно и запрос Google Drive: mimeType = 'application/vnd.google-apps.spreadsheet'."),
        Param("limit", "Сколько файлов", "number", 50, show_if={"operation": ["list"]}),
        Param("name", "Имя файла", "string", "отчёт.csv", show_if={"operation": ["upload"]}),
        Param("content", "Содержимое", "text", "{{ $json.csv }}", show_if={"operation": ["upload"]}),
        Param("mime", "Тип", "select", "text/csv", show_if={"operation": ["upload"]}, no_expr=True,
              options=[("text/csv", "CSV (таблица)"), ("text/plain", "Текст"), ("application/json", "JSON")]),
        Param("folder_id", "ID папки", "string", "", show_if={"operation": ["upload"]},
              hint="Из адреса папки: drive.google.com/drive/folders/<ID>. Пусто — в корень."),
        Param("file_id", "ID файла", "string", "{{ $json.id }}", show_if={"operation": ["download"]}),
    ],
)

# ------------------------------------------------------------- маркетплейсы

WB_HOSTS = [
    ("content-api", "Контент (карточки)"), ("marketplace-api", "Маркетплейс (сборочные задания, остатки FBS)"),
    ("statistics-api", "Статистика (продажи, заказы, остатки FBO)"), ("seller-analytics-api", "Аналитика"),
    ("discounts-prices-api", "Цены и скидки"), ("advert-api", "Продвижение"), ("feedbacks-api", "Вопросы и отзывы"),
    ("finance-api", "Финансы"), ("common-api", "Общее"), ("supplies-api", "Поставки"),
]


def market_node(key: str, title: str, conn_type: str, desc: str, base_url, default_method: str, default_path: str,
                icon: str, color: str, extra: list[Param] | None = None, hint: str = "") -> NodeType:
    async def execute(ctx: NodeContext):
        out = []
        for i in range(max(1, len(ctx.items))):
            conn, values, ctype = ctx.connection("connection", i)
            headers = AUTH_BY_CONN[conn_type](values)
            method = (ctx.raw("method") or default_method).upper()
            path = ctx.param_str("path", i).strip()
            if not path.startswith("/"):
                path = "/" + path
            url = base_url(ctx) + path
            kwargs: dict = {"headers": headers, "params": kv(ctx, "query", i) or None}
            if method != "GET":
                kwargs["json"] = ctx.param_json("body", i, {})
            resp = await call(ctx, method, url, service=title, cctx=ctx.check_context(ctype), timeout=90, **kwargs)
            if resp.status_code == 401:
                raise NodeError(f"{title} не принял ключ из подключения «{conn.name}».", "Проверьте подключение.")
            if resp.status_code == 429:
                raise NodeError(f"{title}: слишком частые запросы.",
                                "Включите «Повторять при ошибке» с паузой 60 секунд в настройках узла.")
            if resp.status_code >= 400:
                raise http_error(title, resp, "Сверьте путь и параметры с документацией API.")
            out.extend(response_items(json_or_none(resp) if json_or_none(resp) is not None else {"data": resp.text},
                                      ctx.param_str("split_field", i)))
            if not ctx.items:
                break
        return [out]

    params = [Param("connection", "Кабинет", "connection", "", conn_types=[conn_type])] + (extra or []) + [
        Param("method", "Метод", "select", default_method, no_expr=True,
              options=[(m, m) for m in ("GET", "POST", "PUT", "PATCH", "DELETE")]),
        Param("path", "Путь", "string", default_path, hint=hint),
        Param("query", "Параметры адреса", "list", [], columns=KV_COLUMNS),
        Param("body", "Тело (JSON)", "json", "{}", show_if={"method": ["POST", "PUT", "PATCH", "DELETE"]}),
        SPLIT_PARAM,
    ]
    return NodeType(key, title, G_APPS, desc, execute, params=params, icon=icon, color=color)


WB = market_node("wildberries", "Wildberries", "wildberries",
                 "Любой метод API Wildberries: заказы, остатки, продажи, цены, отчёты. Токен — из выбранного кабинета.",
                 lambda ctx: f"https://{ctx.raw('host') or 'statistics-api'}.wildberries.ru", "GET",
                 "/api/v1/supplier/stocks", "WB", "#a21caf",
                 extra=[Param("host", "Раздел API", "select", "statistics-api", options=WB_HOSTS, no_expr=True)],
                 hint="Например /api/v1/supplier/orders?dateFrom=… — см. dev.wildberries.ru.")
OZON = market_node("ozon", "Ozon", "ozon", "Любой метод Seller API Ozon: товары, остатки, отправления, финансы.",
                   lambda ctx: "https://api-seller.ozon.ru", "POST", "/v3/product/list", "Oz", "#1d4ed8",
                   hint="Например /v2/posting/fbs/list — см. docs.ozon.ru/api/seller.")
YM = market_node("yandex_market", "Яндекс Маркет", "yandex_market",
                 "Любой метод Partner API Яндекс Маркета: заказы, остатки, цены, отчёты.",
                 lambda ctx: "https://api.partner.market.yandex.ru", "GET", "/v2/campaigns", "ЯМ", "#ca8a04",
                 hint="Например /v2/campaigns/{id}/orders — см. yandex.ru/dev/market/partner-api.")

# ------------------------------------------------------------------ почта


async def smtp_exec(ctx: NodeContext):
    import asyncio

    from ...connectors.smtp import send_mail

    out = []
    for i in range(max(1, len(ctx.items))):
        conn, values, ctype = ctx.connection("connection", i)
        msg = EmailMessage()
        msg["From"] = values.get("from_email") or values.get("username")
        msg["To"] = ctx.param_str("to", i)
        msg["Subject"] = ctx.param_str("subject", i)
        text = ctx.param_str("body", i)
        if ctx.param_bool("html"):
            msg.set_content("Письмо в формате HTML")
            msg.add_alternative(text, subtype="html")
        else:
            msg.set_content(text)
        try:
            await asyncio.to_thread(send_mail, values, msg)
        except ConnectorError as exc:
            raise NodeError(exc.message, exc.action, exc.details) from exc
        out.append(item({"отправлено": True, "кому": msg["To"]}))
        if not ctx.items:
            break
    return [out]


SMTP = NodeType(
    "email_send", "Отправить письмо (SMTP)", G_APPS,
    "Отправляет письмо через любую почту: Яндекс, Mail.ru, корпоративный сервер.",
    smtp_exec, icon="@", color="#0369a1", n8n_types=["n8n-nodes-base.emailSend"],
    params=[Param("connection", "Почта", "connection", "", conn_types=["smtp"]),
            Param("to", "Кому", "string", "", hint="Несколько адресов — через запятую."),
            Param("subject", "Тема", "string", ""),
            Param("body", "Текст", "text", ""),
            Param("html", "Текст в HTML", "boolean", False)],
)

NODES = [HTTP, CLAUDE, ONEC, BITRIX, WB, OZON, YM, TELEGRAM, MAX_NODE, GMAIL, DRIVE, SMTP]
