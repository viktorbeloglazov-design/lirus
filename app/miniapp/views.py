"""Маршруты мини-приложения, вебхука бота и раздела панели «Сервис бассейнов»."""
from __future__ import annotations

import hmac
import json
import logging
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from .. import auth, db, store
from ..auth import can, page
from . import service
from .initdata import InitDataError, validate
from .maxapi import MaxError

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

APP_CSP = ("default-src 'self'; script-src 'self' https://st.max.ru; style-src 'self'; img-src 'self' data: https:; "
           "connect-src 'self'; frame-ancestors 'self' https://max.ru https://*.max.ru")


# ------------------------------------------------------- мини-приложение

async def app_page(request: Request) -> Response:
    """Страница мини-приложения. Открывается внутри MAX; из панели — как предпросмотр."""
    from ..config import APP_VERSION

    if service.setting("miniapp_enabled") != "1":
        return HTMLResponse("<p style='font-family:sans-serif;padding:24px'>Приложение временно выключено.</p>",
                            status_code=503)
    user, _ = auth.load_session(request)
    preview = bool(user and can(user, "pool_service"))
    ctx = {"request": request, "title": service.setting("miniapp_title"),
           "company": store.get_setting("company_name"), "preview": preview, "APP_VERSION": APP_VERSION}
    resp = templates.TemplateResponse(request, "miniapp.html", ctx)
    resp.headers["Content-Security-Policy"] = APP_CSP
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


def _current_user(request: Request) -> dict | None:
    """Кто открыл приложение: по подписи initData из MAX или по сессии панели (предпросмотр)."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme == "MaxInitData" and value.strip():
        data = validate(value.strip(), service.bot_token())
        return data["user"]
    user, _ = auth.load_session(request)
    if user and can(user, "pool_service"):
        return {"id": f"preview-{user['id']}", "name": user["full_name"] or user["login"], "username": "",
                "preview": True}
    raise InitDataError("Откройте приложение из MAX: вне мессенджера оно не знает, кто вы.")


async def _json(request: Request) -> dict:
    try:
        data = json.loads((await request.body()).decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def api(func):
    async def wrapper(request: Request) -> Response:
        if service.setting("miniapp_enabled") != "1":
            return JSONResponse({"ok": False, "error": "Приложение временно выключено."}, status_code=503)
        try:
            user = _current_user(request)
        except InitDataError as exc:
            return JSONResponse({"ok": False, "error": str(exc), "auth": True}, status_code=401)
        try:
            return await func(request, user)
        except Exception as exc:  # noqa: BLE001 — клиенту нужен понятный ответ, а не стек
            log.exception("Сбой API мини-приложения")
            store.log_event("error", service.SECTION, f"Сбой мини-приложения на {request.url.path}",
                            f"{type(exc).__name__}: {exc}")
            return JSONResponse({"ok": False, "error": "Что-то пошло не так. Попробуйте ещё раз через минуту."},
                                status_code=500)
    return wrapper


@api
async def api_bootstrap(request: Request, user: dict) -> Response:
    service.touch_client(user)
    client = service.get_client(user["id"])
    bookings = await service.attach_statuses(service.list_bookings(user["id"], 20))
    return JSONResponse({
        "ok": True,
        "user": {"id": user["id"], "name": user["name"], "preview": bool(user.get("preview"))},
        "client": {"name": client["name"] if client else user["name"], "phone": client["phone"] if client else "",
                   "address": client["address"] if client else ""},
        "title": service.setting("miniapp_title"),
        "company": store.get_setting("company_name"),
        "about": service.setting("miniapp_about"),
        "hours": service.setting("miniapp_hours"),
        "phone": service.setting("miniapp_phone"),
        "services": service.services(),
        "times": [{"key": k, "title": t} for k, t in service.TIME_SLOTS],
        "faq": [{"q": q, "a": a} for q, a in service.faq()],
        "bookings": _public(bookings),
        "history": service.history(user["id"], 20),
    })


def _public(bookings: list[dict]) -> list[dict]:
    return [{"id": b["id"], "service": b["service"], "status": b["status_title"], "date": b["date_title"],
             "time": b["time_title"], "kind": b.get("kind", "booking"), "comment": b.get("comment", ""),
             "created_at": b["created_at"]} for b in bookings if b.get("kind") != "question"]


@api
async def api_booking(request: Request, user: dict) -> Response:
    form = await _json(request)
    booking, errors = await service.create_booking(user, form)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    bookings = await service.attach_statuses([booking])
    return JSONResponse({"ok": True, "booking": _public(bookings)[0],
                         "message": f"Заявка №{booking['id']} принята. Менеджер свяжется с вами по телефону "
                                    f"{booking['phone']}."})


@api
async def api_bookings(request: Request, user: dict) -> Response:
    bookings = await service.attach_statuses(service.list_bookings(user["id"], 20))
    return JSONResponse({"ok": True, "bookings": _public(bookings)})


@api
async def api_ask(request: Request, user: dict) -> Response:
    data = await _json(request)
    question = str(data.get("text") or "").strip()
    if len(question) < 2:
        return JSONResponse({"ok": False, "error": "Напишите вопрос."}, status_code=400)
    service.touch_client(user)
    reply = await service.answer_question(user, question)
    return JSONResponse({"ok": True, "answer": reply["answer"], "handoff": reply["handoff"]})


# --------------------------------------------------------------- вебхук

async def webhook(request: Request) -> Response:
    """Сюда MAX присылает события бота. Проверяем секрет из подписки и отвечаем сразу."""
    secret = service.setting("miniapp_webhook_secret")
    got = request.headers.get("x-max-bot-api-secret", "")
    if not secret or not hmac.compare_digest(got, secret):
        store.log_event("warning", service.SECTION, "Запрос на вебхук бота с неверным секретом отклонён",
                        ip=request.client.host if request.client else "")
        return JSONResponse({"ok": False}, status_code=403)
    update = await _json(request)
    if not update:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
    if request.query_params.get("sync") == "1" and request.client and request.client.host in ("127.0.0.1", "::1"):
        result = await service.handle_update(update)  # для проверок с этого же компьютера
        return JSONResponse({"ok": True, "result": result})
    service.handle_update_background(update)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- панель

def _render(request: Request, template: str, **ctx) -> Response:
    from ..main import render

    return render(request, template, **ctx)


async def _panel_ctx(request: Request, errors: dict | None = None, submitted: dict | None = None) -> dict:
    s = {k: service.setting(k) for k in service.DEFAULTS}
    if submitted:
        s.update(submitted)
    bookings = await service.attach_statuses(service.list_bookings(limit=100), admin=True)
    bot = service.bot_connection()
    state = await service.subscription_state() if bot else {"ok": False, "text": "Бот MAX не подключён.",
                                                              "action": "Добавьте подключение «Бот MAX»."}
    return {
        "s": s, "errors": errors or {}, "bookings": bookings,
        "bot": bot, "bitrix_list": [c for c in store.list_connections("bitrix24") if c.enabled],
        "bitrix": service.bitrix_connection(), "claude": service.claude_connection(),
        "app_url": service.app_url(), "webhook_url": service.webhook_url(), "state": state,
        "public_url": store.get_setting("public_url"),
        "managers": service.manager_ids(), "bot_username": service.setting("miniapp_bot_username"),
        "admin": can(request.state.user, "settings"),
    }


@page("pool_service")
async def panel(request: Request) -> Response:
    if request.method == "GET":
        return _render(request, "pool_service.html", **await _panel_ctx(request))
    if not can(request.state.user, "settings"):
        from ..main import render_error
        return render_error(request, 403, "Нет доступа", "Менять настройки может только администратор.")
    form = request.state.form
    values = {k: str(form.get(k, "")).strip() for k in
              ("miniapp_title", "miniapp_about", "miniapp_services", "miniapp_hours", "miniapp_phone",
               "miniapp_faq", "miniapp_bitrix_conn", "miniapp_bitrix_assigned", "miniapp_manager_max_ids")}
    values["miniapp_enabled"] = "1" if form.get("miniapp_enabled") else "0"
    values["miniapp_use_claude"] = "1" if form.get("miniapp_use_claude") else "0"
    errors: dict[str, str] = {}
    if not values["miniapp_title"]:
        errors["miniapp_title"] = "Название не может быть пустым."
    if not any(line.strip() for line in values["miniapp_services"].splitlines()):
        errors["miniapp_services"] = "Нужна хотя бы одна услуга — по строке на услугу."
    if values["miniapp_bitrix_assigned"] and not values["miniapp_bitrix_assigned"].isdigit():
        errors["miniapp_bitrix_assigned"] = "ID ответственного в Битрикс24 — число (видно в адресе его профиля)."
    if errors:
        return _render(request, "pool_service.html", **await _panel_ctx(request, errors, values))
    service.save_settings(values)
    store.log_event("info", service.SECTION, "Настройки мини-приложения сохранены", user=request.state.user["login"])
    auth.flash(request, "ok", "Настройки сохранены.")
    from ..main import redirect
    return redirect("/pool-service")


@page("settings")
async def subscribe(request: Request) -> Response:
    from ..main import redirect
    try:
        auth.flash(request, "ok", await service.subscribe_webhook())
    except MaxError as exc:
        auth.flash(request, "error", f"{exc.message} {exc.action}".strip())
        store.log_event("error", service.SECTION, f"Подписка бота не удалась: {exc.message}", exc.details,
                        user=request.state.user["login"])
    return redirect("/pool-service")


@page("settings")
async def resend(request: Request) -> Response:
    from ..main import redirect
    booking = service.get_booking(int(request.path_params["booking_id"]))
    if booking is None:
        auth.flash(request, "error", "Заявка не найдена.")
    elif await service.push_to_bitrix(booking):
        auth.flash(request, "ok", f"Заявка №{booking['id']} передана в Битрикс24.")
    else:
        fresh = service.get_booking(booking["id"]) or booking
        auth.flash(request, "error", fresh.get("bitrix_error") or "Битрикс24 не подключён.")
    return redirect("/pool-service#bookings")


@page("settings")
async def set_status(request: Request) -> Response:
    from ..main import redirect
    status = str(request.state.form.get("status", ""))
    if status in ("done", "cancelled", "new"):
        db.execute("UPDATE miniapp_bookings SET status = ?, updated_at = ? WHERE id = ?",
                   (status, time.time(), int(request.path_params["booking_id"])))
    return redirect("/pool-service#bookings")


def routes() -> list[Route]:
    return [
        Route("/max/app", app_page),
        Route("/max/api/bootstrap", api_bootstrap),
        Route("/max/api/booking", api_booking, methods=["POST"]),
        Route("/max/api/bookings", api_bookings),
        Route("/max/api/ask", api_ask, methods=["POST"]),
        Route("/max/webhook", webhook, methods=["POST"]),
        Route("/pool-service", panel, methods=["GET", "POST"]),
        Route("/pool-service/subscribe", subscribe, methods=["POST"]),
        Route("/pool-service/bookings/{booking_id:int}/resend", resend, methods=["POST"]),
        Route("/pool-service/bookings/{booking_id:int}/status", set_status, methods=["POST"]),
    ]
