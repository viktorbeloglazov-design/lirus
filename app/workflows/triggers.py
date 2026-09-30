"""Автоматические запуски: расписание и вебхуки."""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import re
import time
from datetime import datetime

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from .. import store
from . import engine, storage
from .cron import Cron, CronError, schedule_to_cron
from .nodes.base import item

log = logging.getLogger(__name__)

PATH_RE = re.compile(r"^[A-Za-z0-9._\-/]{1,200}$")

# ------------------------------------------------------------------ расписание

_last_fire: dict[tuple[int, str], float] = {}
_started_at = time.time()


def _due(wf_id: int, node: dict, now: datetime) -> bool:
    params = node.get("params") or {}
    key = (wf_id, node["id"])
    last = _last_fire.get(key)
    mode = params.get("mode") or "daily"
    if mode == "interval":
        minutes = max(1.0, float(params.get("minutes") or 15))
        base = last if last is not None else _started_at
        return now.timestamp() - base >= minutes * 60 - 1
    try:
        cron = Cron(schedule_to_cron(params))
    except (CronError, ValueError):
        return False
    if not cron.matches(now):
        return False
    minute_key = now.replace(second=0, microsecond=0).timestamp()
    return last is None or last < minute_key


def reset_schedule(wf_id: int) -> None:
    for key in [k for k in _last_fire if k[0] == wf_id]:
        del _last_fire[key]
    now = time.time()
    wf = storage.get_workflow(wf_id)
    for n in (wf or {}).get("nodes", []):
        if n.get("type") == "schedule":
            _last_fire[(wf_id, n["id"])] = now if (n.get("params") or {}).get("mode") == "interval" else now - 1


async def scheduler_tick() -> int:
    now = datetime.now()
    fired = 0
    for wf in storage.active_workflows():
        for node in wf["nodes"]:
            if node.get("type") != "schedule" or node.get("disabled"):
                continue
            if _due(wf["id"], node, now):
                _last_fire[(wf["id"], node["id"])] = now.timestamp()
                engine.start_background(wf, "schedule", node, None)
                fired += 1
    return fired


async def scheduler_loop() -> None:
    while True:
        try:
            await scheduler_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Сбой планировщика сценариев")
        await asyncio.sleep(15 - (time.time() % 15))


# --------------------------------------------------------------------- вебхуки

# Тестовые вебхуки: редактор «слушает» адрес до указанного времени
LISTENING: dict[str, tuple[int, float]] = {}


def webhook_nodes(wf: dict) -> list[dict]:
    return [n for n in wf.get("nodes", []) if n.get("type") == "webhook" and not n.get("disabled")]


def _match(node: dict, path: str, method: str) -> bool:
    p = node.get("params") or {}
    want = str(p.get("path", "")).strip("/")
    m = (p.get("method") or "POST").upper()
    return want == path and (m == "ANY" or m == method)


def find_active(path: str, method: str) -> tuple[dict, dict] | None:
    for wf in storage.active_workflows():
        for n in webhook_nodes(wf):
            if _match(n, path, method):
                return wf, n
    return None


def path_conflicts(wf: dict) -> list[str]:
    mine = {(str((n.get("params") or {}).get("path", "")).strip("/"), (n.get("params") or {}).get("method", "POST"))
            for n in webhook_nodes(wf)}
    problems = []
    for other in storage.active_workflows():
        if other["id"] == wf.get("id"):
            continue
        for n in webhook_nodes(other):
            p = n.get("params") or {}
            key = (str(p.get("path", "")).strip("/"), p.get("method", "POST"))
            if key in mine:
                problems.append(f"Адрес /webhook/{key[0]} уже занят сценарием «{other['name']}».")
    return problems


def _authorized(node: dict, request: Request) -> bool:
    p = node.get("params") or {}
    mode = p.get("auth") or "none"
    secret = str(p.get("auth_value") or "")
    if mode == "none":
        return True
    if mode == "header":
        got = request.headers.get(p.get("auth_header") or "X-Token", "")
        return bool(secret) and hmac.compare_digest(got, secret)
    if mode == "basic":
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except ValueError:
            return False
        return hmac.compare_digest(user, str(p.get("auth_user") or "")) and hmac.compare_digest(pwd, secret)
    return False


async def _request_item(request: Request, path: str) -> dict:
    raw = await request.body()
    body: object = {}
    ctype = request.headers.get("content-type", "")
    if raw:
        if "json" in ctype:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {"_raw": raw.decode("utf-8", "replace")}
        elif "form" in ctype:
            form = await request.form()
            body = {k: v for k, v in form.items() if isinstance(v, str)}
        else:
            text = raw.decode("utf-8", "replace")
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = {"text": text}
    skip = {"cookie", "authorization"}
    return item({"method": request.method, "path": path, "query": dict(request.query_params),
                 "headers": {k: v for k, v in request.headers.items() if k.lower() not in skip},
                 "body": body, "ip": request.client.host if request.client else ""})


def _json_response(data, status: int = 200, headers: dict | None = None) -> Response:
    if isinstance(data, (dict, list)):
        return JSONResponse(data, status_code=status, headers=headers)
    return PlainTextResponse(str(data), status_code=status, headers=headers)


async def handle(request: Request, test: bool = False) -> Response:
    path = request.path_params.get("path", "").strip("/")
    method = request.method.upper()
    try:
        if int(request.headers.get("content-length") or 0) > 10 * 1024 * 1024:
            return JSONResponse({"ok": False, "error": "Слишком большой запрос (больше 10 МБ)"}, status_code=413)
    except ValueError:
        pass
    if test:
        listen = LISTENING.get(path)
        if not listen or listen[1] < time.time():
            return JSONResponse({"ok": False, "error": "Тестовый адрес сейчас не слушается. Нажмите в редакторе "
                                                       "«Ждать тестовый вызов» и повторите запрос."}, status_code=404)
        wf = storage.get_workflow(listen[0])
        node = next((n for n in webhook_nodes(wf or {}) if _match(n, path, method)), None)
        if wf is None or node is None:
            return JSONResponse({"ok": False, "error": "Метод запроса не совпадает с настройкой узла «Вебхук»."},
                                status_code=405)
        LISTENING.pop(path, None)
        mode = "test_webhook"
    else:
        found = find_active(path, method)
        if found is None:
            return JSONResponse({"ok": False, "error": f"Адрес /webhook/{path} ({method}) не найден. Проверьте, "
                                                       "что сценарий включён и метод запроса совпадает."},
                                status_code=404)
        wf, node = found
        mode = "webhook"
    if not _authorized(node, request):
        store.log_event("warning", "workflows", f"Вебхук /webhook/{path}: запрос без правильного секрета отклонён",
                        ip=request.client.host if request.client else "")
        return JSONResponse({"ok": False, "error": "Неверный секрет"}, status_code=401)
    it = await _request_item(request, path)
    respond = (node.get("params") or {}).get("response") or "immediately"
    if respond == "immediately":
        ex_id = await engine.start_and_get_id(wf, mode, node, [it])
        return JSONResponse({"ok": True, "message": "Принято", "execution_id": ex_id})
    ex = await asyncio.wait_for(engine.run_workflow(wf, mode, node, [it]), timeout=120)
    if respond == "respond_node" and ex.webhook_response is not None:
        r = ex.webhook_response
        return _json_response(r["body"], r["status"], r["headers"])
    if ex.status != "success":
        return JSONResponse({"ok": False, "error": ex.error, "execution_id": ex.id}, status_code=500)
    data = [i["json"] for i in ex.result]
    return _json_response(data[0] if len(data) == 1 else data)


async def webhook_prod(request: Request) -> Response:
    return await handle(request, test=False)


async def webhook_test(request: Request) -> Response:
    return await handle(request, test=True)
