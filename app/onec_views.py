"""Обозреватель 1С: какие объекты отдаёт база и что в них лежит."""
from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, store
from .auth import can, page
from .connectors import BY_KEY, context_for
from .connectors import onec
from .connectors.base import ConnectorError

_cache: dict[int, tuple[float, list[str]]] = {}

GROUPS = [
    ("Catalog_", "Справочники"), ("Document_", "Документы"), ("DocumentJournal_", "Журналы документов"),
    ("AccumulationRegister_", "Регистры накопления"), ("InformationRegister_", "Регистры сведений"),
    ("AccountingRegister_", "Регистры бухгалтерии"), ("ChartOfCharacteristicTypes_", "Планы видов характеристик"),
    ("ChartOfAccounts_", "Планы счетов"), ("Constant_", "Константы"), ("Enum_", "Перечисления"),
    ("ExchangePlan_", "Планы обмена"), ("BusinessProcess_", "Бизнес-процессы"), ("Task_", "Задачи"),
]


def group_of(name: str) -> str:
    for prefix, title in GROUPS:
        if name.startswith(prefix):
            return title
    return "Прочее"


def _connection(conn_id: int):
    conn = store.get_connection(conn_id)
    if conn is None or conn.type != "onec":
        raise ConnectorError("Подключение 1С не найдено.", "Откройте раздел «Подключения».")
    values = conn.values()
    if conn.unreadable:
        raise ConnectorError("Пароль подключения 1С не читается ключом шифрования.", "Введите пароль заново.")
    return conn, values


async def entities_for(conn_id: int, fresh: bool = False) -> list[str]:
    cached = _cache.get(conn_id)
    if cached and not fresh and time.time() - cached[0] < 300:
        return cached[1]
    conn, values = _connection(conn_id)
    names = await onec.odata_entities(values, context_for(BY_KEY["onec"], conn.id))
    _cache[conn_id] = (time.time(), names)
    return names


def _api_user(request: Request):
    user, _ = auth.load_session(request)
    if user is None or user["must_change_password"] or not (can(user, "connections") or can(user, "workflows")):
        return None
    return user


async def api_entities(request: Request):
    if _api_user(request) is None:
        return JSONResponse({"error": "Нет доступа."}, 403)
    raw = request.path_params.get("conn_id")
    try:
        if raw == "auto":
            conns = store.list_connections("onec")
            if not conns:
                return JSONResponse({"entities": [], "error": "Подключение 1С ещё не создано."})
            conn_id = conns[0].id
        else:
            conn_id = int(raw)
        names = await entities_for(conn_id, fresh=request.query_params.get("fresh") == "1")
        return JSONResponse({"entities": names})
    except (ValueError, ConnectorError) as exc:
        msg = exc.message if isinstance(exc, ConnectorError) else "Неверный номер подключения."
        return JSONResponse({"entities": [], "error": msg, "action": getattr(exc, "action", "")})


async def api_preview(request: Request):
    if _api_user(request) is None:
        return JSONResponse({"error": "Нет доступа."}, 403)
    q = request.query_params
    entity = q.get("entity", "").strip()
    if not entity:
        return JSONResponse({"error": "Не указан объект."}, 400)
    try:
        conn, values = _connection(int(request.path_params["conn_id"]))
        started = time.time()
        records = await onec.odata_query(values, context_for(BY_KEY["onec"], conn.id), entity,
                                         {"$filter": q.get("filter", ""), "$select": q.get("select", "")},
                                         top=max(1, min(int(q.get("top", "20") or 20), 200)))
        return JSONResponse({"records": records, "ms": int((time.time() - started) * 1000)})
    except ConnectorError as exc:
        return JSONResponse({"error": exc.message, "action": exc.action}, 400)
    except ValueError:
        return JSONResponse({"error": "Неверные параметры."}, 400)


@page("connections")
async def explorer(request: Request):
    from .main import render, render_error

    conn = store.get_connection(int(request.path_params["conn_id"]))
    if conn is None or conn.type != "onec":
        return render_error(request, 404, "Подключение 1С не найдено", "Возможно, его удалили.")
    error, action, groups = "", "", []
    try:
        names = await entities_for(conn.id, fresh=request.query_params.get("fresh") == "1")
        by_group: dict[str, list[str]] = {}
        for n in names:
            by_group.setdefault(group_of(n), []).append(n)
        order = [t for _, t in GROUPS] + ["Прочее"]
        groups = [(g, by_group[g]) for g in order if g in by_group]
    except ConnectorError as exc:
        error, action = exc.message, exc.action
    return render(request, "onec_explorer.html", conn=conn, groups=groups, error=error, action=action,
                  total=sum(len(x) for _, x in groups))


def routes() -> list[Route]:
    return [
        Route("/connections/{conn_id:int}/1c", explorer),
        Route("/api/onec/{conn_id}/entities", api_entities),
        Route("/api/onec/{conn_id:int}/preview", api_preview),
    ]
