"""Веб-панель: страницы, формы и маршруты."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import math
import re
import secrets as pysecrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import auth, backup, config, crypto, db, diagnostics, runtime, store, tls
from .auth import ROLES, can, page
from .connectors import BY_KEY, CATEGORY_ORDER, TYPES, context_for, get_type, run_check, validate_form
from .connectors import google as g
from .connectors.base import ERROR, OK, STATUS_TITLES, WARN, ConnectorError, normalize_url

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def fmt_dt(ts, with_seconds: bool = False) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(float(ts)).strftime("%d.%m.%Y %H:%M:%S" if with_seconds else "%d.%m.%Y %H:%M")


def fmt_ago(ts) -> str:
    if not ts:
        return "никогда"
    sec = max(0, time.time() - float(ts))
    if sec < 60:
        return "только что"
    if sec < 3600:
        return f"{int(sec // 60)} мин назад"
    if sec < 86400:
        return f"{int(sec // 3600)} ч назад"
    return fmt_dt(ts)


templates.env.filters["dt"] = fmt_dt
templates.env.filters["ago"] = fmt_ago
templates.env.globals.update(STATUS_TITLES=STATUS_TITLES, ROLES=ROLES, APP_NAME=config.APP_NAME,
                             APP_VERSION=config.APP_VERSION)

NAV = [
    ("overview", "/", "Обзор"),
    ("connections", "/connections", "Подключения"),
    ("employees", "/employees", "Сотрудники"),
    ("testing", "/testing", "Тестирование"),
    ("journal", "/journal", "Журнал"),
    ("settings", "/settings", "Настройки"),
]


def render(request: Request, template: str, status_code: int = 200, **ctx) -> Response:
    user = getattr(request.state, "user", None)
    base = {
        "request": request,
        "user": user,
        "csrf": getattr(request.state, "csrf", ""),
        "flashes": auth.pop_flash(request) if user else [],
        "nav": [(k, href, title) for k, href, title in NAV if can(user, k)] if user else [],
        "can": lambda perm: can(user, perm),
        "company": store.get_setting("company_name"),
    }
    base.update(ctx)
    return templates.TemplateResponse(request, template, base, status_code=status_code)


def render_error(request: Request, code: int, title: str, message: str, action: str = "") -> Response:
    return render(request, "error.html", status_code=code, code=code, title=title, message=message, action=action)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def form_str(request: Request, name: str) -> str:
    value = request.state.form.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def who(request: Request) -> dict:
    """Кто и откуда выполнил действие — для журнала."""
    user = getattr(request.state, "user", None)
    return {"user": user["login"] if user else "", "ip": auth.client_ip(request)}


def safe_next(url: str) -> str:
    return url if url.startswith("/") and not url.startswith("//") else "/"


# ------------------------------------------------------------------ служебное

async def health(request: Request) -> Response:
    return JSONResponse({"ok": True, "app": config.APP_NAME, "version": config.APP_VERSION,
                         "instance": store.instance_id()})


async def internal_shutdown(request: Request) -> Response:
    """Остановка из остановить.bat: только с этого же компьютера и с ключом из data/control.token."""
    host = auth.client_ip(request)
    token = request.headers.get("x-control-token", "")
    expected = config.CONTROL_TOKEN_PATH.read_text().strip() if config.CONTROL_TOKEN_PATH.exists() else ""
    if host not in ("127.0.0.1", "::1") or not expected or not hmac.compare_digest(token, expected):
        return PlainTextResponse("forbidden", status_code=403)
    store.log_event("info", "system", "Платформа остановлена (остановить.bat)")
    if runtime.can_restart():
        runtime.request_shutdown()
        return PlainTextResponse("stopping")
    return PlainTextResponse("no-runtime", status_code=409)


# ------------------------------------------------------------------------ вход

async def login(request: Request) -> Response:
    user, _ = auth.load_session(request)
    nxt = safe_next(request.query_params.get("next", "/"))
    if request.method == "GET":
        if user:
            return redirect(nxt)
        first = db.query_one("SELECT must_change_password FROM users WHERE login = 'admin'")
        return render(request, "login.html", next=nxt, error="", login_value="",
                      first_run=bool(first and first["must_change_password"]))
    form = await request.form()
    login_value = str(form.get("login", "")).strip()
    password = str(form.get("password", ""))
    nxt = safe_next(str(form.get("next", "/")))
    ip = auth.client_ip(request)
    key = f"{ip}|{login_value.lower()}"
    wait = auth.login_blocked(key)
    if wait:
        return render(request, "login.html", status_code=429, next=nxt, login_value=login_value, first_run=False,
                      error=f"Слишком много неудачных попыток. Подождите {math.ceil(wait / 60)} мин и попробуйте снова.")
    row = store.get_user_by_login(login_value) if login_value else None
    if not row or not row["active"] or not auth.verify_password(password, row["password_hash"]):
        auth.register_failure(key)
        store.log_event("warning", "auth", f"Неудачная попытка входа под логином «{login_value}»", ip=ip)
        msg = "Неверный логин или пароль."
        if row and not row["active"] and auth.verify_password(password, row["password_hash"]):
            msg = "Этот сотрудник отключён. Обратитесь к администратору."
        if password and password != password.strip():
            msg += " Обратите внимание: в пароле есть пробел в начале или в конце."
        return render(request, "login.html", status_code=401, next=nxt, error=msg, login_value=login_value,
                      first_run=False)
    auth.clear_failures(key)
    token = auth.create_session(row["id"], ip)
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (time.time(), row["id"]))
    store.log_event("info", "auth", "Вход в панель", user=row["login"], ip=ip)
    resp = redirect("/password" if row["must_change_password"] else nxt)
    auth.set_session_cookie(resp, request, token)
    return resp


@page(allow_must_change=True)
async def logout(request: Request) -> Response:
    auth.destroy_session(request.state.token)
    resp = redirect("/login")
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@page(allow_must_change=True)
async def change_password(request: Request) -> Response:
    user = request.state.user
    forced = bool(user["must_change_password"])
    if request.method == "GET":
        return render(request, "password.html", forced=forced, error="")
    old = str(request.state.form.get("old_password", ""))
    new = str(request.state.form.get("new_password", ""))
    new2 = str(request.state.form.get("new_password2", ""))
    error = ""
    if not auth.verify_password(old, user["password_hash"]):
        error = "Текущий пароль введён неверно."
    elif new != new2:
        error = "Новый пароль и повтор не совпадают."
    elif new == old:
        error = "Новый пароль должен отличаться от старого."
    else:
        error = auth.password_problem(new, user["login"])
    if error:
        return render(request, "password.html", status_code=400, forced=forced, error=error)
    db.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
               (auth.hash_password(new), user["id"]))
    auth.destroy_user_sessions(user["id"], keep_token=request.state.token)
    store.log_event("info", "auth", "Пароль изменён", **who(request))
    auth.flash(request, "ok", "Пароль изменён.")
    return redirect("/")


# ---------------------------------------------------------------------- обзор

@page("overview")
async def overview(request: Request) -> Response:
    user = request.state.user
    ctx: dict = {}
    if can(user, "connections"):
        conns = store.list_connections()
        counts = {OK: 0, WARN: 0, ERROR: 0, "new": 0}
        for c in conns:
            if c.enabled:
                counts[c.status if c.status in counts else "new"] += 1
        existing = {c.type for c in conns}
        ctx["conns"] = [(c, BY_KEY.get(c.type)) for c in conns]
        ctx["counts"] = counts
        ctx["checklist"] = [
            ("Сменить пароль администратора", True, "/password"),
            ("Указать внешний адрес панели", bool(store.get_setting("public_url")), "/settings"),
            ("Подключить ядро Claude", "claude" in existing, "/connections/new?type=claude"),
            ("Подключить 1С", "onec" in existing, "/connections/new?type=onec"),
            ("Подключить бота MAX или Telegram", bool({"max_bot", "telegram_bot"} & existing), "/connections"),
            ("Добавить сотрудников", db.query_one("SELECT COUNT(*) n FROM users")["n"] > 1, "/employees"),
            ("Сделать резервную копию", backup.last_backup_time() is not None, "/settings#backup"),
            ("Проверить всё на экране «Тестирование»", store.last_test_run() is not None, "/testing"),
        ]
        ctx["last_run"] = store.last_test_run()
        ctx["last_backup"] = backup.last_backup_time()
    if can(user, "journal"):
        ctx["problems"], _ = store.list_events(level="error", limit=5)
    if can(user, "finance"):
        claude_conn = next(iter(store.list_connections("claude")), None)
        month = datetime.now().strftime("%Y-%m")
        spent = store.month_usage(month)
        limit = float(claude_conn.config.get("monthly_limit") or 0) if claude_conn else 0
        ctx["finance"] = {"spent": spent, "limit": limit, "connected": claude_conn is not None,
                          "percent": min(100, int(spent / limit * 100)) if limit else 0,
                          "cabinets": sum(len(store.list_connections(t)) for t in ("wildberries", "ozon", "yandex_market"))}
    ctx["scopes"] = sorted(auth.DATA_SCOPES.get(user["role"], set()))
    return render(request, "overview.html", **ctx)


# ---------------------------------------------------------------- подключения

def _field_views(ctype, existing=None, submitted=None, errors=None) -> list[dict]:
    views = []
    for f in ctype.form_fields:
        if submitted is not None and not f.secret:
            if f.kind == "checkbox":
                value = submitted.get(f.name) in ("1", "on")
            else:
                value = submitted.get(f.name, "")
        elif existing is not None:
            value = existing.config.get(f.name, f.default)
        else:
            value = f.default
        if f.kind == "number" and isinstance(value, float):
            value = f"{value:g}".replace(".", ",")
        views.append({
            "f": f, "value": "" if value is None else value,
            "masked": existing.masked(f.name) if (existing is not None and f.secret) else "",
            "error": (errors or {}).get(f.name, ""),
        })
    return views


def _redirect_uris(request: Request) -> list[str]:
    uris = [f"http://localhost:{config.port()}{g.CALLBACK_PATH}"]
    public = store.get_setting("public_url").rstrip("/")
    if public:
        uris.append(public + g.CALLBACK_PATH)
    current = str(request.base_url).rstrip("/") + g.CALLBACK_PATH
    host = request.url.hostname or ""
    if current not in uris and not tls.is_ip(host) and host not in ("127.0.0.1",):
        uris.append(current)
    return uris


@page("connections")
async def connections_list(request: Request) -> Response:
    conns = store.list_connections()
    by_type: dict[str, list] = {}
    for c in conns:
        by_type.setdefault(c.type, []).append(c)
    groups = []
    for cat in CATEGORY_ORDER:
        types = [t for t in TYPES if t.category == cat]
        if types:
            groups.append((cat, [(t, by_type.get(t.key, [])) for t in types]))
    return render(request, "connections.html", groups=groups)


def _can_create_google_account() -> str:
    app = next(iter(store.list_connections("google_app")), None)
    if app is None or not app.values().get("client_id"):
        return "Сначала заполните подключение «Google: ключи приложения» — без него Google не пустит."
    return ""


@page("connections")
async def connection_new(request: Request) -> Response:
    type_key = request.query_params.get("type", "")
    ctype = get_type(type_key)
    if ctype is None:
        return render_error(request, 404, "Нет такого типа подключения",
                            "Ссылка устарела или набрана с ошибкой.", "Вернитесь в раздел «Подключения».")
    if ctype.singleton:
        existing = store.list_connections(ctype.key)
        if existing:
            msg = (f"«{ctype.title}» уже подключено, а такое подключение может быть только одно. "
                   "Откройте существующее и измените его.")
            if request.method == "POST":
                store.log_event("warning", "connections", f"Попытка создать второе подключение «{ctype.title}» отклонена",
                                **who(request))
                return render_error(request, 409, "Такое подключение уже есть", msg,
                                    f"Откройте раздел «Подключения» → «{ctype.title}».")
            auth.flash(request, "warn", msg)
            return redirect(f"/connections/{existing[0].id}")
    if ctype.oauth:
        return render(request, "connection_form.html", ctype=ctype, conn=None, fields=[], name="",
                      oauth_block=_can_create_google_account(), redirect_uris=_redirect_uris(request))
    if request.method == "GET":
        return render(request, "connection_form.html", ctype=ctype, conn=None, name="",
                      fields=_field_views(ctype), redirect_uris=_redirect_uris(request))
    form = request.state.form
    name, cfg, secrets, errors = validate_form(ctype, form)
    if errors:
        return render(request, "connection_form.html", status_code=400, ctype=ctype, conn=None, name=name,
                      fields=_field_views(ctype, submitted=form, errors=errors), redirect_uris=_redirect_uris(request),
                      form_error="Исправьте поля, отмеченные красным.")
    try:
        conn_id = store.save_connection(None, ctype.key, name or ctype.title, cfg, secrets)
    except sqlite3.IntegrityError:
        return render_error(request, 409, "Такое подключение уже есть",
                            f"«{ctype.title}» может быть только одно.", "Откройте существующее и измените его.")
    store.log_event("info", "connections", f"Создано подключение «{name or ctype.title}» ({ctype.title})", **who(request))
    if form.get("action") == "save_check":
        return await _do_check(request, conn_id)
    auth.flash(request, "ok", "Подключение сохранено. Нажмите «Проверить связь», чтобы убедиться, что всё работает.")
    return redirect(f"/connections/{conn_id}")


def _get_conn_or_404(request: Request):
    try:
        conn_id = int(request.path_params["conn_id"])
    except (KeyError, ValueError):
        return None, None
    conn = store.get_connection(conn_id)
    if conn is None:
        return None, None
    return conn, get_type(conn.type)


@page("connections")
async def connection_edit(request: Request) -> Response:
    conn, ctype = _get_conn_or_404(request)
    if conn is None or ctype is None:
        return render_error(request, 404, "Подключение не найдено", "Возможно, его уже удалили.",
                            "Вернитесь в раздел «Подключения».")
    if request.method == "GET":
        return render(request, "connection_form.html", ctype=ctype, conn=conn, name=conn.name,
                      fields=_field_views(ctype, existing=conn), redirect_uris=_redirect_uris(request),
                      oauth_block=_can_create_google_account() if ctype.oauth else "")
    form = request.state.form
    name, cfg, secrets, errors = validate_form(ctype, form, existing=conn)
    if errors:
        return render(request, "connection_form.html", status_code=400, ctype=ctype, conn=conn, name=name,
                      fields=_field_views(ctype, existing=conn, submitted=form, errors=errors),
                      redirect_uris=_redirect_uris(request), form_error="Исправьте поля, отмеченные красным.")
    if ctype.oauth and not name:
        name = conn.config.get("email") or conn.name
    store.save_connection(conn.id, ctype.key, name or ctype.title, cfg, secrets, enabled=conn.enabled)
    changed = ", ".join(sorted(secrets)) if secrets else "секреты не менялись"
    store.log_event("info", "connections", f"Изменено подключение «{name or ctype.title}»",
                    f"{changed}", **who(request))
    if form.get("action") == "save_check":
        return await _do_check(request, conn.id)
    auth.flash(request, "ok", "Изменения сохранены.")
    return redirect(f"/connections/{conn.id}")


async def _do_check(request: Request, conn_id: int) -> Response:
    conn = store.get_connection(conn_id)
    ctype = get_type(conn.type)
    res = await diagnostics.check_one(conn)
    old = conn.status
    store.set_connection_status(conn.id, res.status, res.message, res.action)
    if res.status != OK or old != res.status:
        store.log_event({OK: "info", WARN: "warning"}.get(res.status, "error"), "connections",
                        f"Проверка «{conn.name}»: {res.message}", diagnostics.scrub(res.details, [v for v in conn.values().values() if isinstance(v, str)]),
                        **who(request))
    kind = {OK: "ok", WARN: "warn"}.get(res.status, "error")
    text = res.message + (f" Что делать: {res.action}" if res.action else "")
    auth.flash(request, kind, text)
    return redirect(f"/connections/{conn.id}")


@page("connections")
async def connection_check(request: Request) -> Response:
    conn, ctype = _get_conn_or_404(request)
    if conn is None:
        return render_error(request, 404, "Подключение не найдено", "Возможно, его уже удалили.")
    return await _do_check(request, conn.id)


@page("connections")
async def connection_toggle(request: Request) -> Response:
    conn, _ = _get_conn_or_404(request)
    if conn is None:
        return render_error(request, 404, "Подключение не найдено", "Возможно, его уже удалили.")
    db.execute("UPDATE connections SET enabled = ? WHERE id = ?", (0 if conn.enabled else 1, conn.id))
    state = "выключено" if conn.enabled else "включено"
    store.log_event("info", "connections", f"Подключение «{conn.name}» {state}", "", **who(request))
    auth.flash(request, "ok", f"Подключение {state}.")
    return redirect(f"/connections/{conn.id}")


@page("connections")
async def connection_delete(request: Request) -> Response:
    conn, _ = _get_conn_or_404(request)
    if conn is None:
        return render_error(request, 404, "Подключение не найдено", "Возможно, его уже удалили.")
    store.delete_connection(conn.id)
    store.log_event("warning", "connections", f"Удалено подключение «{conn.name}»", "", **who(request))
    auth.flash(request, "ok", f"Подключение «{conn.name}» удалено.")
    return redirect("/connections")


# ------------------------------------------------------------- вход в Google

@page("connections")
async def google_start(request: Request) -> Response:
    problem = _can_create_google_account()
    if problem:
        auth.flash(request, "error", problem)
        return redirect("/connections/new?type=google_app")
    host = request.url.hostname or ""
    base = str(request.base_url).rstrip("/")
    port = config.port()
    if tls.is_ip(host) or (request.url.scheme != "https" and host != "localhost"):
        public = store.get_setting("public_url")
        tip = (f"Откройте панель на самом сервере по адресу http://localhost:{port} и нажмите кнопку там"
               + (f", или откройте её по внешнему адресу {public}" if public.startswith("https://") else "")
               + ".")
        return render_error(request, 400, "Google не разрешает вход по этому адресу",
                            f"Панель открыта по адресу {base}. Google пускает только на адреса с доменом и "
                            "https, либо на localhost.", tip)
    redirect_uri = base + g.CALLBACK_PATH
    conn_id = form_str(request, "conn_id")
    target = store.get_connection(int(conn_id)) if conn_id.isdigit() else None
    state = pysecrets.token_urlsafe(24)
    db.execute("DELETE FROM oauth_states WHERE created_at < ?", (time.time() - 3600,))
    db.execute("INSERT INTO oauth_states(state, user_id, connection_id, redirect_uri, created_at) VALUES(?,?,?,?,?)",
               (state, request.state.user["id"], target.id if target else None, redirect_uri, time.time()))
    app_values = store.list_connections("google_app")[0].values()
    url = g.authorize_url(app_values["client_id"], redirect_uri, state,
                          login_hint=(target.config.get("email", "") if target else ""))
    return RedirectResponse(url, status_code=303)


@page("connections")
async def google_callback(request: Request) -> Response:
    params = request.query_params
    state = params.get("state", "")
    row = db.query_one("SELECT * FROM oauth_states WHERE state = ?", (state,)) if state else None
    if row:
        db.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
    if not row or row["user_id"] != request.state.user["id"] or time.time() - row["created_at"] > 900:
        return render_error(request, 400, "Ссылка входа Google устарела",
                            "Вход через Google нужно пройти за 15 минут, в той же вкладке, где нажали кнопку.",
                            "Вернитесь в «Подключения» и нажмите «Подключить аккаунт Google» ещё раз.")
    back = f"/connections/{row['connection_id']}" if row["connection_id"] else "/connections"
    if params.get("error"):
        err = params.get("error")
        text = ("Вы нажали «Отмена» на странице Google — доступ не выдан." if err == "access_denied"
                else f"Google вернул ошибку: {err}.")
        auth.flash(request, "error", text + " Нажмите «Подключить аккаунт Google» ещё раз и разрешите доступ.")
        store.log_event("warning", "connections", "Вход в Google не завершён", err, **who(request))
        return redirect(back)
    code = params.get("code", "")
    app_conn = next(iter(store.list_connections("google_app")), None)
    ctype = get_type("google_account")
    try:
        if app_conn is None:
            raise ConnectorError("Не заполнены ключи приложения Google.", "Заполните «Google: ключи приложения».")
        result = await g.exchange_code(app_conn.values(), code, row["redirect_uri"], context_for(ctype))
    except ConnectorError as exc:
        auth.flash(request, "error", exc.message + (" Что делать: " + exc.action if exc.action else ""))
        store.log_event("error", "connections", "Не удалось подключить аккаунт Google: " + exc.message,
                        diagnostics.scrub(exc.details, []), **who(request))
        return redirect(back)
    email = result["email"]
    target = None
    for c in store.list_connections("google_account"):
        if email and c.config.get("email", "").lower() == email.lower():
            target = c
            break
    if target is None and row["connection_id"]:
        target = store.get_connection(row["connection_id"])
    name = target.name if (target and target.name) else (email or "Аккаунт Google")
    conn_id = store.save_connection(target.id if target else None, "google_account", name,
                                    {"email": email}, {"refresh_token": result["refresh_token"]},
                                    enabled=target.enabled if target else True)
    store.log_event("info", "connections", f"Подключён аккаунт Google {email}", "", **who(request))
    if result["missing_scopes"]:
        auth.flash(request, "warn", "Аккаунт подключён, но на странице Google отмечены не все галочки. "
                                    "Нажмите «Переподключить аккаунт Google» и отметьте все пункты.")
    return await _do_check(request, conn_id)


# --------------------------------------------------------------- сотрудники

LOGIN_RE = re.compile(r"^[\w.\-@]{2,64}$", re.UNICODE)


@page("employees")
async def employees_list(request: Request) -> Response:
    return render(request, "employees.html", users=store.list_users(), descriptions=auth.ROLE_DESCRIPTIONS)


def _employee_form_values(form) -> dict:
    return {k: (form.get(k, "") or "").strip() if isinstance(form.get(k, ""), str) else ""
            for k in ("full_name", "login", "role", "phone", "telegram_id", "max_id", "note")}


@page("employees")
async def employee_edit(request: Request) -> Response:
    me = request.state.user
    raw_id = request.path_params.get("user_id")
    target = store.get_user(int(raw_id)) if raw_id and raw_id.isdigit() else None
    if raw_id and target is None:
        return render_error(request, 404, "Сотрудник не найден", "Возможно, его уже удалили.")
    if request.method == "GET":
        values = dict(target) if target else {"role": "employee", "active": 1}
        return render(request, "employee_form.html", target=target, v=values, errors={},
                      descriptions=auth.ROLE_DESCRIPTIONS)
    form = request.state.form
    v = _employee_form_values(form)
    v["active"] = 1 if form.get("active") in ("1", "on") else 0
    password = str(form.get("password", ""))
    must_change = form.get("must_change") in ("1", "on")
    errors: dict[str, str] = {}
    if not v["full_name"]:
        errors["full_name"] = "Укажите имя сотрудника."
    if v["role"] not in ROLES:
        errors["role"] = "Выберите роль из списка."
    if v["login"] and not LOGIN_RE.match(v["login"]):
        errors["login"] = "Логин — одно слово без пробелов: буквы, цифры, точка, дефис."
    if v["login"]:
        other = store.get_user_by_login(v["login"])
        if other and (target is None or other["id"] != target["id"]):
            errors["login"] = "Такой логин уже занят другим сотрудником."
    if password:
        if not v["login"]:
            errors["login"] = "Чтобы сотрудник мог входить в панель, задайте логин."
        problem = auth.password_problem(password, v["login"])
        if problem:
            errors["password"] = problem
    for key, label in (("telegram_id", "Telegram"), ("max_id", "MAX")):
        if v[key] and not v[key].lstrip("-").isdigit():
            errors[key] = f"ID в {label} — это число. Бот подскажет его сотруднику на этапе подключения бота."
    if target is not None and target["id"] == me["id"]:
        if v["role"] != "admin":
            errors["role"] = "Нельзя снять роль администратора с самого себя — так можно потерять доступ."
        if not v["active"]:
            errors["active"] = "Нельзя отключить самого себя."
    if target is not None and target["role"] == "admin" and (v["role"] != "admin" or not v["active"]):
        if store.count_active_admins(exclude_id=target["id"]) == 0:
            errors.setdefault("role", "Это последний администратор. Сначала назначьте администратором кого-то ещё.")
    if errors:
        merged = dict(target) if target else {}
        merged.update(v)
        return render(request, "employee_form.html", status_code=400, target=target, v=merged, errors=errors,
                      descriptions=auth.ROLE_DESCRIPTIONS, form_error="Исправьте поля, отмеченные красным.")
    login_value = v["login"] or None
    if target is None:
        new_id = db.execute(
            "INSERT INTO users(login, full_name, role, password_hash, must_change_password, active, phone, "
            "telegram_id, max_id, note, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (login_value, v["full_name"], v["role"], auth.hash_password(password) if password else None,
             int(must_change and bool(password)), v["active"], v["phone"], v["telegram_id"], v["max_id"],
             v["note"], time.time()))
        store.log_event("info", "employees", f"Добавлен сотрудник «{v['full_name']}» ({ROLES[v['role']]})", "",
                        **who(request))
        auth.flash(request, "ok", "Сотрудник добавлен." + (" Передайте ему логин и пароль — при первом входе "
                                                           "панель попросит сменить пароль." if password and must_change else ""))
        return redirect(f"/employees/{new_id}")
    db.execute(
        "UPDATE users SET login = ?, full_name = ?, role = ?, active = ?, phone = ?, telegram_id = ?, max_id = ?, "
        "note = ? WHERE id = ?",
        (login_value, v["full_name"], v["role"], v["active"], v["phone"], v["telegram_id"], v["max_id"],
         v["note"], target["id"]))
    if password:
        db.execute("UPDATE users SET password_hash = ?, must_change_password = ? WHERE id = ?",
                   (auth.hash_password(password), int(must_change and target["id"] != me["id"]), target["id"]))
    if password or not v["active"] or v["role"] != target["role"]:
        auth.destroy_user_sessions(target["id"], keep_token=request.state.token if target["id"] == me["id"] else None)
    details = []
    if v["role"] != target["role"]:
        details.append(f"роль: {ROLES.get(target['role'])} → {ROLES[v['role']]}")
    if password:
        details.append("задан новый пароль")
    if bool(v["active"]) != bool(target["active"]):
        details.append("включён" if v["active"] else "отключён")
    store.log_event("info", "employees", f"Изменён сотрудник «{v['full_name']}»", "; ".join(details), **who(request))
    auth.flash(request, "ok", "Изменения сохранены.")
    return redirect(f"/employees/{target['id']}")


@page("employees")
async def employee_delete(request: Request) -> Response:
    me = request.state.user
    raw_id = request.path_params.get("user_id", "")
    target = store.get_user(int(raw_id)) if raw_id.isdigit() else None
    if target is None:
        return render_error(request, 404, "Сотрудник не найден", "Возможно, его уже удалили.")
    if target["id"] == me["id"]:
        auth.flash(request, "error", "Нельзя удалить самого себя.")
        return redirect(f"/employees/{target['id']}")
    if target["role"] == "admin" and store.count_active_admins(exclude_id=target["id"]) == 0:
        auth.flash(request, "error", "Это последний администратор — удалить нельзя.")
        return redirect(f"/employees/{target['id']}")
    db.execute("DELETE FROM users WHERE id = ?", (target["id"],))
    store.log_event("warning", "employees", f"Удалён сотрудник «{target['full_name']}»", "", **who(request))
    auth.flash(request, "ok", f"Сотрудник «{target['full_name']}» удалён.")
    return redirect("/employees")


# ------------------------------------------------------------- тестирование

@page("testing")
async def testing(request: Request) -> Response:
    return render(request, "testing.html", result=store.last_test_run())


@page("testing")
async def testing_run(request: Request) -> Response:
    user = request.state.user["login"]
    result = await diagnostics.run_all()
    result["user"] = user
    store.save_test_run(result, user)
    s = result["summary"]
    level = "error" if s.get(ERROR) else "warning" if s.get(WARN) else "info"
    store.log_event(level, "testing",
                    f"Проверка всего: работает {s.get(OK, 0)}, внимание {s.get(WARN, 0)}, ошибок {s.get(ERROR, 0)}",
                    "", **who(request))
    return redirect("/testing")


@page("testing")
async def testing_report(request: Request) -> Response:
    result = store.last_test_run()
    if result is None:
        auth.flash(request, "warn", "Сначала нажмите «Проверить всё» — отчёт составляется по последней проверке.")
        return redirect("/testing")
    text = diagnostics.report_text(result)
    stamp = datetime.fromtimestamp(result["ts"]).strftime("%Y-%m-%d_%H-%M")
    ascii_name = f"otchet-proverki-{stamp}.txt"
    ru_name = quote(f"отчёт-проверки-{stamp}.txt")
    body = "﻿" + text.replace("\r\n", "\n").replace("\n", "\r\n")
    return Response(body.encode("utf-8"), media_type="text/plain; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{ru_name}"})


# ------------------------------------------------------------------- журнал

@page("journal")
async def journal(request: Request) -> Response:
    q = request.query_params
    level = q.get("level", "")
    section = q.get("section", "")
    search = q.get("q", "").strip()
    try:
        page_no = max(1, int(q.get("page", "1")))
    except ValueError:
        page_no = 1
    per_page = 100
    rows, total = store.list_events(level, section, search, per_page, (page_no - 1) * per_page)
    pages = max(1, math.ceil(total / per_page))
    return render(request, "journal.html", rows=rows, total=total, level=level, section=section, q=search,
                  page_no=page_no, pages=pages, LEVELS=store.LEVELS, SECTIONS=store.SECTIONS,
                  retention=store.get_int_setting("journal_days", 90))


# ---------------------------------------------------------------- настройки

def _settings_ctx(**extra) -> dict:
    ctx = {
        "s": {k: store.get_setting(k) for k in store.SETTING_DEFAULTS if k not in store.SECRET_SETTINGS},
        "proxy_masked": crypto.mask(store.get_setting("proxy_url")),
        "port": config.port(),
        "cert": tls.installed(),
        "backups": [(p.name, p.stat().st_mtime, p.stat().st_size) for p in backup.list_backups()[:15]],
        "can_restart": runtime.can_restart(),
        "errors": {},
    }
    ctx.update(extra)
    return ctx


def _int_in(value: str, label: str, lo: int, hi: int, errors: dict, key: str) -> int | None:
    try:
        n = int(value.strip())
    except ValueError:
        errors[key] = f"«{label}» — целое число от {lo} до {hi}."
        return None
    if not lo <= n <= hi:
        errors[key] = f"«{label}» — число от {lo} до {hi}."
        return None
    return n


@page("settings")
async def settings_page(request: Request) -> Response:
    if request.method == "GET":
        return render(request, "settings.html", **_settings_ctx())
    form = request.state.form
    errors: dict[str, str] = {}
    new: dict[str, str] = {"company_name": form_str(request, "company_name")[:100]}
    public = form_str(request, "public_url")
    if public:
        try:
            url = normalize_url(public, "Внешний адрес")
            parts = urlsplit(url)
            new["public_url"] = f"{parts.scheme}://{parts.netloc}"
        except ValueError as exc:
            errors["public_url"] = str(exc)
    else:
        new["public_url"] = ""
    proxy = form_str(request, "proxy_url")
    clear_proxy = form.get("proxy_clear") in ("1", "on")
    if proxy and not clear_proxy:
        proxy = "".join(proxy.split())
        if not re.match(r"^https?://", proxy):
            if re.match(r"^socks", proxy, re.I):
                errors["proxy_url"] = "Поддерживаются только HTTP-прокси (адрес начинается с http://)."
            else:
                proxy = "http://" + proxy
        if "proxy_url" not in errors and not urlsplit(proxy).hostname:
            errors["proxy_url"] = "Адрес прокси выглядит так: http://логин:пароль@1.2.3.4:3128"
    for key, label, lo, hi in (("journal_days", "Хранить журнал, дней", 7, 3650),
                               ("backup_keep", "Хранить копий", 1, 365),
                               ("auto_check_hours", "Проверять подключения каждые, часов", 0, 168),
                               ("https_port", "Порт HTTPS", 1, 65535)):
        n = _int_in(form_str(request, key), label, lo, hi, errors, key)
        if n is not None:
            new[key] = str(n)
    new["auto_backup"] = "1" if form.get("auto_backup") in ("1", "on") else "0"
    port = _int_in(form_str(request, "port"), "Порт панели", 1024, 65535, errors, "port")
    if port is not None and new.get("https_port") and int(new["https_port"]) == port:
        errors["https_port"] = "Порт HTTPS должен отличаться от порта панели."
    if errors:
        return render(request, "settings.html", status_code=400,
                      **_settings_ctx(errors=errors, form_error="Исправьте поля, отмеченные красным."))
    for key, value in new.items():
        store.set_setting(key, value)
    if clear_proxy:
        store.set_setting("proxy_url", "")
    elif proxy:
        store.set_setting("proxy_url", proxy)
    notes = ["Настройки сохранены."]
    if port is not None and port != config.port():
        config.write_env({"PORT": str(port)})
        notes.append(f"Новый порт {port} заработает после перезапуска платформы (кнопка внизу страницы).")
    store.log_event("info", "settings", "Изменены настройки", "прокси изменён" if (proxy or clear_proxy) else "",
                    **who(request))
    auth.flash(request, "ok", " ".join(notes))
    return redirect("/settings")


@page("settings")
async def settings_tls(request: Request) -> Response:
    form = request.state.form
    pfx = form.get("pfx")
    crt = form.get("crt")
    key = form.get("key")
    password = str(form.get("tls_password", ""))

    async def read(upload) -> bytes:
        data = await upload.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise tls.CertError("Файл слишком большой для сертификата — проверьте, что выбран нужный файл.")
        return data

    try:
        if pfx is not None and getattr(pfx, "filename", ""):
            info = tls.install_pfx(await read(pfx), password)
        elif crt is not None and getattr(crt, "filename", "") and key is not None and getattr(key, "filename", ""):
            info = tls.install_pem(await read(crt), await read(key), password)
        else:
            raise tls.CertError("Выберите файл .pfx (или пару файлов .crt и .key) и нажмите «Загрузить».")
    except tls.CertError as exc:
        return render(request, "settings.html", status_code=400,
                      **_settings_ctx(tls_error=str(exc)))
    public_host = urlsplit(store.get_setting("public_url")).hostname or ""
    msg = f"Сертификат загружен: {', '.join(info.names)}, действует до {info.not_after:%d.%m.%Y}."
    kind = "ok"
    if public_host and not info.covers(public_host):
        msg += f" Внимание: он выдан не на {public_host} — браузер будет ругаться."
        kind = "warn"
    msg += " HTTPS заработает после перезапуска платформы."
    store.log_event("info", "settings", "Загружен сертификат HTTPS", ", ".join(info.names), **who(request))
    auth.flash(request, kind, msg)
    return redirect("/settings#https")


@page("settings")
async def settings_tls_remove(request: Request) -> Response:
    tls.remove()
    store.log_event("warning", "settings", "Сертификат HTTPS удалён", "", **who(request))
    auth.flash(request, "ok", "Сертификат удалён. После перезапуска панель будет работать только по http.")
    return redirect("/settings#https")


@page("settings")
async def settings_backup(request: Request) -> Response:
    try:
        path = await asyncio.to_thread(backup.create, store.get_int_setting("backup_keep", 14))
    except Exception as exc:  # noqa: BLE001
        store.log_event("error", "backup", "Не удалось сделать резервную копию", repr(exc), **who(request))
        auth.flash(request, "error", "Не удалось сделать резервную копию. Проверьте, что на диске есть место.")
        return redirect("/settings#backup")
    store.log_event("info", "backup", f"Резервная копия создана: {path.name}", "", **who(request))
    auth.flash(request, "ok", f"Резервная копия создана: {path.name}. Скачайте её и сохраните на другом диске.")
    return redirect("/settings#backup")


@page("settings")
async def settings_backup_download(request: Request) -> Response:
    name = request.path_params.get("name", "")
    path = config.BACKUP_DIR / name
    if not re.match(r"^backup-[\d_-]+\.zip$", name) or not path.exists():
        return render_error(request, 404, "Копия не найдена", "Возможно, она уже удалена как устаревшая.")
    store.log_event("warning", "backup", f"Скачана резервная копия {name}", "", **who(request))
    return FileResponse(path, filename=name, media_type="application/zip")


@page("settings")
async def settings_restart(request: Request) -> Response:
    if not runtime.can_restart():
        auth.flash(request, "error", "Перезапуск из панели недоступен в этом режиме. Запустите остановить.bat, "
                                     "затем запустить.bat.")
        return redirect("/settings")
    store.log_event("info", "system", "Перезапуск платформы из панели", "", **who(request))
    runtime.request_restart()
    return render(request, "restarting.html", port=config.port())


# ------------------------------------------------------------------ ошибки

async def not_found(request: Request, exc: Exception) -> Response:
    request.state.user, _ = auth.load_session(request)
    code = getattr(exc, "status_code", 404)
    if code == 405:
        return render_error(request, 405, "Так эту страницу не открыть",
                            "Похоже, страницу открыли по ссылке, которая предназначена для кнопки.",
                            "Вернитесь назад и нажмите кнопку на странице.")
    return render_error(request, 404, "Страница не найдена", "Такой страницы в панели нет.",
                        "Проверьте адрес или вернитесь на главную.")


async def server_error(request: Request, exc: Exception) -> Response:
    log.exception("Ошибка при обработке %s", request.url.path, exc_info=exc)
    store.log_event("error", "system", f"Внутренняя ошибка на странице {request.url.path}",
                    f"{type(exc).__name__}: {exc}")
    return render_error(request, 500, "Что-то пошло не так",
                        "Во время выполнения действия произошла внутренняя ошибка. Данные не потеряны.",
                        "Повторите действие. Если ошибка повторяется — откройте «Тестирование», нажмите "
                        "«Скачать отчёт» и передайте файл разработчику.")


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "form-action 'self' https://accounts.google.com; frame-ancestors 'none'")
        if request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        return response


# ------------------------------------------------------------- фоновая работа

async def _periodic() -> None:
    last_cleanup = 0.0
    last_auto_check = time.time()
    while True:
        try:
            now = time.time()
            if now - last_cleanup > 86400:
                removed = store.cleanup_events(store.get_int_setting("journal_days", 90))
                db.execute("DELETE FROM oauth_states WHERE created_at < ?", (now - 3600,))
                if removed:
                    store.log_event("info", "system", f"Автоочистка журнала: удалено старых записей — {removed}")
                last_cleanup = now
            if store.get_setting("auto_backup") == "1":
                last = backup.last_backup_time()
                if last is None or now - last > 86400:
                    path = await asyncio.to_thread(backup.create, store.get_int_setting("backup_keep", 14))
                    store.log_event("info", "backup", f"Автоматическая резервная копия: {path.name}")
            hours = store.get_int_setting("auto_check_hours", 6)
            if hours > 0 and now - last_auto_check > hours * 3600:
                last_auto_check = now
                await diagnostics.check_connections()  # сама пишет в журнал, если что-то сломалось
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Сбой фоновой задачи")
            store.log_event("error", "system", "Сбой фоновой задачи (автокопия/очистка/проверка)")
        await asyncio.sleep(600)


def startup() -> None:
    config.ensure_dirs()
    had_db = config.DB_PATH.exists()
    had_key = crypto.key_exists()
    db.init()
    crypto.encrypt("проверка")  # создаёт ключ, если его нет
    store.instance_id()
    auth.ensure_default_admin()
    if had_db and not had_key and any(c.secrets_raw for c in store.list_connections()):
        store.log_event("error", "system", "Ключ шифрования data\\secret.key отсутствовал — создан новый. "
                        "Сохранённые пароли подключений больше не читаются: восстановите старый ключ из "
                        "резервной копии или введите пароли заново.")
    if not config.CONTROL_TOKEN_PATH.exists():
        config.CONTROL_TOKEN_PATH.write_text(pysecrets.token_urlsafe(32))


@contextlib.asynccontextmanager
async def lifespan(app):
    startup()
    store.log_event("info", "system", f"Платформа запущена, версия {config.APP_VERSION}")
    task = asyncio.create_task(_periodic())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> Starlette:
    routes = [
        Route("/health", health),
        Route("/internal/shutdown", internal_shutdown, methods=["POST"]),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/password", change_password, methods=["GET", "POST"]),
        Route("/", overview),
        Route("/connections", connections_list),
        Route("/connections/new", connection_new, methods=["GET", "POST"]),
        Route("/connections/{conn_id:int}", connection_edit, methods=["GET", "POST"]),
        Route("/connections/{conn_id:int}/check", connection_check, methods=["POST"]),
        Route("/connections/{conn_id:int}/toggle", connection_toggle, methods=["POST"]),
        Route("/connections/{conn_id:int}/delete", connection_delete, methods=["POST"]),
        Route("/oauth/google/start", google_start, methods=["POST"]),
        Route(g.CALLBACK_PATH, google_callback),
        Route("/employees", employees_list),
        Route("/employees/new", employee_edit, methods=["GET", "POST"]),
        Route("/employees/{user_id}", employee_edit, methods=["GET", "POST"]),
        Route("/employees/{user_id}/delete", employee_delete, methods=["POST"]),
        Route("/testing", testing),
        Route("/testing/run", testing_run, methods=["POST"]),
        Route("/testing/report", testing_report),
        Route("/journal", journal),
        Route("/settings", settings_page, methods=["GET", "POST"]),
        Route("/settings/tls", settings_tls, methods=["POST"]),
        Route("/settings/tls/remove", settings_tls_remove, methods=["POST"]),
        Route("/settings/backup", settings_backup, methods=["POST"]),
        Route("/settings/backup/{name}", settings_backup_download),
        Route("/settings/restart", settings_restart, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static"),
    ]
    return Starlette(
        routes=routes,
        middleware=[Middleware(SecurityHeaders)],
        exception_handlers={404: not_found, 405: not_found, HTTPException: not_found, Exception: server_error},
        lifespan=lifespan,
    )


app = create_app()
