"""Страницы и API раздела «Сценарии» (редактор как в n8n)."""
from __future__ import annotations

import functools
import hmac
import json
import math
import re
import time
import uuid
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import auth, store
from ..auth import can, page
from . import engine, n8n_import, storage, templates_lib, triggers
from .cron import CronError, describe, schedule_to_cron
from .nodes import BY_KEY, GROUP_ORDER, NODES

AUTO_TRIGGERS = ("schedule", "webhook")


def _render(request, template, **ctx):
    from ..main import render

    return render(request, template, **ctx)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _who(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    return {"user": user["login"] if user else "", "ip": auth.client_ip(request)}


# ------------------------------------------------------------- API-декоратор

def api(perm: str = "workflows"):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(request: Request):
            user, token = auth.load_session(request)
            if user is None:
                return JSONResponse({"error": "Вы не вошли в панель или сессия истекла. Обновите страницу."}, 401)
            if user["must_change_password"] or not can(user, perm):
                return JSONResponse({"error": "Нет доступа к этому действию."}, 403)
            request.state.user = user
            if request.method in ("POST", "PUT", "DELETE"):
                sent = request.headers.get("x-csrf-token", "")
                if not hmac.compare_digest(sent, user["csrf"]):
                    return JSONResponse({"error": "Страница устарела. Обновите её (F5)."}, 400)
            try:
                return await func(request)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, 400)
        return wrapper
    return decorator


async def _json_body(request: Request) -> dict:
    try:
        data = json.loads((await request.body()).decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Не удалось прочитать данные запроса.") from None
    if not isinstance(data, dict):
        raise ValueError("Неверный формат данных.")
    return data


def _wf_or_404(request: Request) -> dict:
    wf = storage.get_workflow(int(request.path_params["wf_id"]))
    if wf is None:
        raise ValueError("Сценарий не найден — возможно, его удалили.")
    return wf


# ------------------------------------------------------------ проверка графа

def normalize_graph(nodes: list, connections: list) -> tuple[list, list]:
    if not isinstance(nodes, list) or not isinstance(connections, list):
        raise ValueError("Неверная структура сценария.")
    if len(nodes) > 500:
        raise ValueError("В сценарии больше 500 узлов — разбейте его на несколько.")
    ids, names, clean = set(), set(), []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "n" + uuid.uuid4().hex[:8])[:40]
        if nid in ids:
            nid = "n" + uuid.uuid4().hex[:8]
        ids.add(nid)
        name = str(n.get("name") or "Узел").strip()[:120] or "Узел"
        base, k = name, 2
        while name in names:
            name = f"{base} {k}"
            k += 1
        names.add(name)
        pos = n.get("position") or [0, 0]
        try:
            pos = [int(float(pos[0])), int(float(pos[1]))]
        except (TypeError, ValueError, IndexError):
            pos = [0, 0]
        settings = n.get("settings") if isinstance(n.get("settings"), dict) else {}
        clean.append({"id": nid, "name": name, "type": str(n.get("type", "")), "position": pos,
                      "params": n.get("params") if isinstance(n.get("params"), dict) else {},
                      "disabled": bool(n.get("disabled")), "settings": settings,
                      "notes": str(n.get("notes", ""))[:2000]})
    edges, seen = [], set()
    for e in connections:
        if not isinstance(e, dict):
            continue
        key = (str(e.get("from")), int(e.get("out", 0) or 0), str(e.get("to")), int(e.get("in", 0) or 0))
        if key[0] in ids and key[2] in ids and key not in seen and key[0] != key[2]:
            seen.add(key)
            edges.append({"from": key[0], "out": key[1], "to": key[2], "in": key[3]})
    return clean, edges


def activation_problems(wf: dict) -> list[str]:
    problems = []
    autos = [n for n in wf["nodes"] if n.get("type") in AUTO_TRIGGERS and not n.get("disabled")]
    special = [n for n in wf["nodes"] if n.get("type") in ("error_trigger", "subworkflow_trigger")]
    if not autos and not special:
        problems.append("Включать нечего: в сценарии нет «Расписания» или «Вебхука». Сценарий с «Запуском вручную» "
                        "выполняется кнопкой «Выполнить».")
    for n in autos:
        p = n.get("params") or {}
        if n["type"] == "webhook":
            path = str(p.get("path", "")).strip("/")
            if not path or not triggers.PATH_RE.match(path):
                problems.append(f"Узел «{n['name']}»: укажите путь вебхука латиницей, например novyi-zakaz.")
            if p.get("auth") in ("header", "basic") and not p.get("auth_value"):
                problems.append(f"Узел «{n['name']}»: выбрана защита, но не задан секрет.")
        if n["type"] == "schedule":
            try:
                from .cron import Cron

                Cron(schedule_to_cron(p)) if p.get("mode") != "interval" else None
            except (CronError, ValueError) as exc:
                problems.append(f"Узел «{n['name']}»: {exc}")
    problems += triggers.path_conflicts(wf)
    for n in wf["nodes"]:
        if n.get("type") not in BY_KEY and not n.get("disabled"):
            problems.append(f"Узел «{n['name']}» не поддерживается — замените или отключите его.")
    return problems


# ------------------------------------------------------------------ страницы

@page("workflows")
async def workflows_page(request: Request):
    items = []
    for wf in storage.list_workflows():
        triggers_list = sorted({BY_KEY[n["type"]].title for n in wf["nodes"]
                                if n.get("type") in BY_KEY and BY_KEY[n["type"]].trigger})
        items.append({**wf, "stats": storage.stats(wf["id"]), "triggers": triggers_list})
    return _render(request, "workflows.html", workflows=items, templates=templates_lib.TEMPLATES,
                   running=len(engine.RUNNING))


@page("workflows")
async def workflow_create(request: Request):
    form = request.state.form
    tpl = str(form.get("template", "") or "")
    if tpl:
        built = templates_lib.build(tpl)
        if not built:
            auth.flash(request, "error", "Такого шаблона нет.")
            return _redirect("/workflows")
        name, nodes, edges = built
    else:
        name = str(form.get("name", "") or "").strip() or "Новый сценарий"
        nodes = [{"id": "n" + uuid.uuid4().hex[:8], "type": "manual", "name": "Запуск вручную", "position": [0, 0],
                  "params": {}}]
        edges = []
    wf_id = storage.create_workflow(name[:120], nodes, edges, user=request.state.user["login"])
    store.log_event("info", "workflows", f"Создан сценарий «{name}»", "", **_who(request))
    return _redirect(f"/workflows/{wf_id}")


@page("workflows")
async def workflow_import(request: Request):
    upload = request.state.form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        auth.flash(request, "error", "Выберите файл сценария (.json) и нажмите «Импортировать».")
        return _redirect("/workflows")
    raw = await upload.read(10 * 1024 * 1024)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        auth.flash(request, "error", "Файл не читается как JSON. Выгрузите сценарий заново: в n8n — меню «…» → «Download».")
        return _redirect("/workflows")
    try:
        if isinstance(data, dict) and data.get("format") == "platforma-workflow":
            name, nodes, edges, notes = data.get("name", "Импорт"), data.get("nodes", []), data.get("connections", []), []
            settings = data.get("settings") or {}
        else:
            name, nodes, edges, notes = n8n_import.convert(data)
            settings = {}
        nodes, edges = normalize_graph(nodes, edges)
    except ValueError as exc:
        auth.flash(request, "error", str(exc))
        return _redirect("/workflows")
    wf_id = storage.create_workflow(name[:120], nodes, edges, settings, user=request.state.user["login"])
    store.log_event("info", "workflows", f"Импортирован сценарий «{name}»", "; ".join(notes)[:2000], **_who(request))
    msg = f"Сценарий «{name}» импортирован: узлов — {len(nodes)}."
    if notes:
        msg += " Требует внимания: " + " ".join(notes[:6]) + (" …" if len(notes) > 6 else "")
    auth.flash(request, "warn" if notes else "ok", msg)
    return _redirect(f"/workflows/{wf_id}")


@page("workflows")
async def workflow_action(request: Request):
    wf = storage.get_workflow(int(request.path_params["wf_id"]))
    action = request.path_params["action"]
    if wf is None:
        auth.flash(request, "error", "Сценарий не найден.")
        return _redirect("/workflows")
    if action == "delete":
        storage.delete_workflow(wf["id"])
        store.log_event("warning", "workflows", f"Удалён сценарий «{wf['name']}»", "", **_who(request))
        auth.flash(request, "ok", f"Сценарий «{wf['name']}» удалён.")
    elif action == "duplicate":
        new_id = storage.create_workflow(wf["name"] + " (копия)", wf["nodes"], wf["connections"], wf["settings"],
                                         user=request.state.user["login"])
        auth.flash(request, "ok", "Копия создана. Она выключена — проверьте и включите.")
        return _redirect(f"/workflows/{new_id}")
    elif action == "toggle":
        if not wf["active"]:
            problems = activation_problems(wf)
            if problems:
                auth.flash(request, "error", "Сценарий не включён. " + " ".join(problems))
                return _redirect("/workflows")
        storage.set_active(wf["id"], not wf["active"])
        triggers.reset_schedule(wf["id"])
        state = "выключен" if wf["active"] else "включён"
        store.log_event("info", "workflows", f"Сценарий «{wf['name']}» {state}", "", **_who(request))
        auth.flash(request, "ok", f"Сценарий «{wf['name']}» {state}.")
    return _redirect("/workflows")


@page("workflows")
async def workflow_editor(request: Request):
    wf = storage.get_workflow(int(request.path_params["wf_id"]))
    if wf is None:
        from ..main import render_error

        return render_error(request, 404, "Сценарий не найден", "Возможно, его удалили.")
    return _render(request, "workflow_editor.html", wf=wf, execution_id=request.query_params.get("execution", ""),
                   public_url=store.get_setting("public_url").rstrip("/"))


@page("workflows")
async def workflow_export(request: Request):
    wf = storage.get_workflow(int(request.path_params["wf_id"]))
    if wf is None:
        return _redirect("/workflows")
    body = json.dumps(n8n_import.export_n8n_like(wf), ensure_ascii=False, indent=2)
    safe = re.sub(r"[^\w\-]+", "_", wf["name"], flags=re.U)[:60] or "scenariy"
    return Response(body.encode("utf-8"), media_type="application/json", headers={
        "Content-Disposition": f"attachment; filename=\"workflow-{wf['id']}.json\"; filename*=UTF-8''{quote(safe)}.json"})


@page("workflows")
async def executions_page(request: Request):
    q = request.query_params
    wf_id = int(q["workflow"]) if q.get("workflow", "").isdigit() else None
    status = q.get("status", "")
    try:
        page_no = max(1, int(q.get("page", "1")))
    except ValueError:
        page_no = 1
    rows, total = storage.list_executions(wf_id, status, 100, (page_no - 1) * 100)
    for r in rows:
        live = engine.RUNNING.get(r["id"])
        if live:
            r["status"] = live.status
    return _render(request, "executions.html", rows=rows, total=total, wf_id=wf_id, status=status, page_no=page_no,
                   pages=max(1, math.ceil(total / 100)), workflows=storage.list_workflows(), MODES=engine.MODES)


@page("workflows")
async def variables_page(request: Request):
    if request.method == "POST":
        form = request.state.form
        keys, values = form.getlist("key"), form.getlist("value")
        data = {}
        for k, v in zip(keys, values):
            k = str(k).strip()
            if k:
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                    auth.flash(request, "error", f"Имя переменной «{k}» — только латиница, цифры и «_», "
                                                 "например MANAGER_CHAT_ID.")
                    return _redirect("/workflows/variables")
                data[k] = str(v)
        storage.set_variables(data)
        store.log_event("info", "workflows", "Изменены переменные сценариев", ", ".join(data), **_who(request))
        auth.flash(request, "ok", "Переменные сохранены.")
        return _redirect("/workflows/variables")
    return _render(request, "wf_variables.html", variables=storage.get_variables())


# ---------------------------------------------------------------------- API

@api()
async def api_node_types(request: Request):
    groups = [{"title": g, "nodes": [n.to_dict() for n in NODES if n.group == g]} for g in GROUP_ORDER]
    return JSONResponse({"groups": groups})


@api()
async def api_lookup(request: Request):
    conns = [{"id": c.id, "name": c.name, "type": c.type, "status": c.status} for c in store.list_connections()]
    wfs = [{"id": w["id"], "name": w["name"]} for w in storage.list_workflows()]
    return JSONResponse({"connections": conns, "workflows": wfs, "variables": sorted(storage.get_variables())})


@api()
async def api_workflow(request: Request):
    wf = _wf_or_404(request)
    if request.method == "GET":
        return JSONResponse(wf)
    data = await _json_body(request)
    name = str(data.get("name") or wf["name"]).strip()[:120] or "Сценарий"
    nodes, edges = normalize_graph(data.get("nodes", []), data.get("connections", []))
    settings = {**storage.DEFAULT_SETTINGS, **(data.get("settings") or {})}
    try:
        settings["timeout_minutes"] = max(1, min(int(float(settings.get("timeout_minutes") or 30)), 1440))
    except (TypeError, ValueError):
        settings["timeout_minutes"] = 30
    candidate = {**wf, "name": name, "nodes": nodes, "connections": edges, "settings": settings}
    warning = ""
    if wf["active"]:
        problems = activation_problems(candidate)
        if problems:
            storage.set_active(wf["id"], False)
            warning = "Сценарий сохранён и выключен: " + " ".join(problems)
    storage.save_workflow(wf["id"], name, nodes, edges, settings, request.state.user["login"])
    if wf["active"]:
        triggers.reset_schedule(wf["id"])
    return JSONResponse({"ok": True, "updated_at": time.time(), "warning": warning,
                         "active": bool(wf["active"] and not warning), "nodes": nodes, "connections": edges})


@api()
async def api_activate(request: Request):
    wf = _wf_or_404(request)
    data = await _json_body(request)
    want = bool(data.get("active"))
    if want:
        problems = activation_problems(wf)
        if problems:
            return JSONResponse({"error": " ".join(problems)}, 400)
    storage.set_active(wf["id"], want)
    triggers.reset_schedule(wf["id"])
    store.log_event("info", "workflows", f"Сценарий «{wf['name']}» {'включён' if want else 'выключен'}", "",
                    **_who(request))
    return JSONResponse({"ok": True, "active": want})


@api()
async def api_run(request: Request):
    wf = _wf_or_404(request)
    data = await _json_body(request)
    if isinstance(data.get("workflow"), dict):  # выполняем то, что сейчас в редакторе, даже несохранённое
        nodes, edges = normalize_graph(data["workflow"].get("nodes", []), data["workflow"].get("connections", []))
        wf = {**wf, "nodes": nodes, "connections": edges,
              "settings": {**wf["settings"], **(data["workflow"].get("settings") or {})}}
    trigger = engine.pick_trigger(wf, data.get("trigger_id"))
    stop_at = data.get("stop_at") or None
    ex_id = await engine.start_and_get_id(wf, "manual", trigger, None, stop_at)
    return JSONResponse({"execution_id": ex_id})


@api()
async def api_execution(request: Request):
    ex_id = int(request.path_params["ex_id"])
    live = engine.RUNNING.get(ex_id)
    if live:
        return JSONResponse({**live.summary(), "running": True}, headers={"Cache-Control": "no-store"})
    row = storage.get_execution(ex_id)
    if row is None:
        return JSONResponse({"error": "Выполнение не найдено (возможно, удалено автоочисткой)."}, 404)
    data = row["data"] or {}
    return JSONResponse({"id": row["id"], "status": row["status"], "mode": row["mode"], "started_at": row["started_at"],
                         "finished_at": row["finished_at"], "error": row["error"], "error_node": row["error_node"],
                         "error_hint": data.get("error_hint", ""), "run_data": data.get("run_data", {}),
                         "workflow_snapshot": data.get("workflow"), "workflow_id": row["workflow_id"],
                         "running": False})


@api()
async def api_stop(request: Request):
    ok = engine.cancel(int(request.path_params["ex_id"]))
    return JSONResponse({"ok": ok})


@api()
async def api_listen(request: Request):
    wf = _wf_or_404(request)
    data = await _json_body(request)
    node = next((n for n in wf["nodes"] if n["id"] == data.get("node_id") and n.get("type") == "webhook"), None)
    if node is None:
        raise ValueError("Сначала сохраните сценарий с узлом «Вебхук».")
    path = str((node.get("params") or {}).get("path", "")).strip("/")
    if not path or not triggers.PATH_RE.match(path):
        raise ValueError("Укажите в узле путь вебхука латиницей и сохраните сценарий.")
    triggers.LISTENING[path] = (wf["id"], time.time() + 120)
    return JSONResponse({"ok": True, "path": path, "until": time.time() + 120, "since": time.time()})


@api()
async def api_latest(request: Request):
    wf = _wf_or_404(request)
    since = float(request.query_params.get("since", "0") or 0)
    rows, _ = storage.list_executions(wf["id"], "", 1, 0)
    if rows and rows[0]["started_at"] >= since:
        return JSONResponse({"execution_id": rows[0]["id"]})
    return JSONResponse({"execution_id": None})


@api()
async def api_cron(request: Request):
    data = await _json_body(request)
    params = data.get("params") or {}
    if (params.get("mode") or "") == "interval":
        return JSONResponse({"text": f"Каждые {params.get('minutes') or 15} мин. после включения сценария"})
    try:
        return JSONResponse({"text": describe(schedule_to_cron(params))})
    except (CronError, ValueError) as exc:
        return JSONResponse({"text": str(exc)})


@api()
async def api_preview(request: Request):
    """Предпросмотр выражения на данных последнего выполнения."""
    from .expr import ExpressionError, render
    from .nodes.base import NodeContext
    from .nodes import BY_KEY as types

    data = await _json_body(request)
    ex_id = data.get("execution_id")
    items = data.get("items") or [{}]
    fake_wf = {"id": 0, "name": "", "nodes": [], "connections": []}
    ex = engine.Execution(fake_wf, "manual")
    if ex_id:
        live = engine.RUNNING.get(int(ex_id))
        row = None if live else storage.get_execution(int(ex_id))
        run_data = live.run_data if live else ((row or {}).get("data") or {}).get("run_data", {})
        snapshot = ((row or {}).get("data") or {}).get("workflow") or (live.workflow if live else {})
        ex.by_name = {n["name"]: n for n in (snapshot or {}).get("nodes", [])}
        for nid, runs in run_data.items():
            last = runs[-1] if runs else {}
            ex.last_outputs[nid] = [[{"json": x} for x in o.get("items", [])] for o in last.get("outputs", [])]
    ctx = NodeContext(ex, {"id": "preview", "name": "", "params": {}}, types["noop"], [[{"json": i} for i in items]])
    try:
        value = render(str(data.get("expr", "")), ctx.namespace(0))
        return JSONResponse({"ok": True, "value": value}, headers={"Cache-Control": "no-store"})
    except ExpressionError as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


def routes() -> list[Route]:
    return [
        Route("/workflows", workflows_page),
        Route("/workflows/new", workflow_create, methods=["POST"]),
        Route("/workflows/import", workflow_import, methods=["POST"]),
        Route("/workflows/variables", variables_page, methods=["GET", "POST"]),
        Route("/workflows/{wf_id:int}", workflow_editor),
        Route("/workflows/{wf_id:int}/export", workflow_export),
        Route("/workflows/{wf_id:int}/{action:str}", workflow_action, methods=["POST"]),
        Route("/executions", executions_page),
        Route("/api/node-types", api_node_types),
        Route("/api/lookup", api_lookup),
        Route("/api/workflows/{wf_id:int}", api_workflow, methods=["GET", "PUT"]),
        Route("/api/workflows/{wf_id:int}/activate", api_activate, methods=["POST"]),
        Route("/api/workflows/{wf_id:int}/run", api_run, methods=["POST"]),
        Route("/api/workflows/{wf_id:int}/listen", api_listen, methods=["POST"]),
        Route("/api/workflows/{wf_id:int}/latest", api_latest),
        Route("/api/executions/{ex_id:int}", api_execution),
        Route("/api/executions/{ex_id:int}/stop", api_stop, methods=["POST"]),
        Route("/api/cron-preview", api_cron, methods=["POST"]),
        Route("/api/expression-preview", api_preview, methods=["POST"]),
        Route("/webhook/{path:path}", triggers.webhook_prod, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        Route("/webhook-test/{path:path}", triggers.webhook_test, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    ]
