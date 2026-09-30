"""Хранение сценариев и истории выполнений."""
from __future__ import annotations

import json
import time

from .. import db, store

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    graph TEXT NOT NULL DEFAULT '{"nodes": [], "connections": []}',
    settings TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER,
    workflow_name TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    error TEXT NOT NULL DEFAULT '',
    error_node TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS executions_wf ON executions(workflow_id, started_at);
CREATE INDEX IF NOT EXISTS executions_started ON executions(started_at);
"""

DEFAULT_SETTINGS = {"error_workflow_id": "", "timeout_minutes": 30, "save_success": True, "note": ""}


def init() -> None:
    with db.session() as conn:
        conn.executescript(SCHEMA)
        # выполнения, оборванные перезапуском сервера
        conn.execute("UPDATE executions SET status = 'error', error = 'Прервано перезапуском платформы', "
                     "finished_at = ? WHERE status IN ('running', 'waiting')", (time.time(),))


def _row(row) -> dict:
    graph = json.loads(row["graph"] or "{}")
    return {"id": row["id"], "name": row["name"], "active": bool(row["active"]),
            "nodes": graph.get("nodes", []), "connections": graph.get("connections", []),
            "settings": {**DEFAULT_SETTINGS, **json.loads(row["settings"] or "{}")},
            "created_at": row["created_at"], "updated_at": row["updated_at"], "updated_by": row["updated_by"]}


def list_workflows() -> list[dict]:
    return [_row(r) for r in db.query("SELECT * FROM workflows ORDER BY active DESC, name COLLATE NOCASE")]


def get_workflow(wf_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM workflows WHERE id = ?", (wf_id,))
    return _row(row) if row else None


def create_workflow(name: str, nodes: list, connections: list, settings: dict | None = None, user: str = "") -> int:
    now = time.time()
    return db.execute(
        "INSERT INTO workflows(name, graph, settings, created_at, updated_at, updated_by) VALUES(?,?,?,?,?,?)",
        (name, json.dumps({"nodes": nodes, "connections": connections}, ensure_ascii=False),
         json.dumps({**DEFAULT_SETTINGS, **(settings or {})}, ensure_ascii=False), now, now, user))


def save_workflow(wf_id: int, name: str, nodes: list, connections: list, settings: dict, user: str = "") -> None:
    db.execute("UPDATE workflows SET name = ?, graph = ?, settings = ?, updated_at = ?, updated_by = ? WHERE id = ?",
               (name, json.dumps({"nodes": nodes, "connections": connections}, ensure_ascii=False),
                json.dumps(settings, ensure_ascii=False), time.time(), user, wf_id))


def set_active(wf_id: int, active: bool) -> None:
    db.execute("UPDATE workflows SET active = ? WHERE id = ?", (int(active), wf_id))


def delete_workflow(wf_id: int) -> None:
    db.execute("DELETE FROM workflows WHERE id = ?", (wf_id,))


def active_workflows() -> list[dict]:
    return [_row(r) for r in db.query("SELECT * FROM workflows WHERE active = 1")]


# --------------------------------------------------------------- выполнения

def create_execution(wf: dict, mode: str) -> int:
    return db.execute("INSERT INTO executions(workflow_id, workflow_name, mode, status, started_at) VALUES(?,?,?,?,?)",
                      (wf.get("id"), wf.get("name", ""), mode, "running", time.time()))


def finish_execution(ex_id: int, status: str, error: str, error_node: str, data: dict, keep_data: bool = True) -> None:
    payload = json.dumps(data if keep_data else {}, ensure_ascii=False, default=str)
    if len(payload) > 5_000_000:  # защита от раздувания базы
        payload = json.dumps({"truncated": True, "note": "Данные выполнения слишком большие и не сохранены"})
    db.execute("UPDATE executions SET status = ?, finished_at = ?, error = ?, error_node = ?, data = ? WHERE id = ?",
               (status, time.time(), error, error_node, payload, ex_id))


def get_execution(ex_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM executions WHERE id = ?", (ex_id,))
    if not row:
        return None
    d = dict(row)
    d["data"] = json.loads(d["data"] or "{}")
    return d


def list_executions(workflow_id: int | None = None, status: str = "", limit: int = 100, offset: int = 0):
    where, params = [], []
    if workflow_id:
        where.append("workflow_id = ?")
        params.append(workflow_id)
    if status:
        where.append("status = ?")
        params.append(status)
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.query_one(f"SELECT COUNT(*) n FROM executions {sql_where}", tuple(params))["n"]
    rows = db.query(f"SELECT id, workflow_id, workflow_name, mode, status, started_at, finished_at, error, error_node "
                    f"FROM executions {sql_where} ORDER BY id DESC LIMIT ? OFFSET ?", tuple(params + [limit, offset]))
    return [dict(r) for r in rows], total


def cleanup_executions(days: int) -> int:
    return db.execute("DELETE FROM executions WHERE started_at < ?", (time.time() - max(days, 1) * 86400,))


def stats(workflow_id: int) -> dict:
    row = db.query_one("SELECT COUNT(*) total, SUM(status='error') errors, MAX(started_at) last FROM executions "
                       "WHERE workflow_id = ? AND started_at > ?", (workflow_id, time.time() - 7 * 86400))
    return {"total": row["total"] or 0, "errors": row["errors"] or 0, "last": row["last"]}


# ------------------------------------------------------------- переменные

def get_variables() -> dict:
    try:
        return json.loads(store.get_setting("wf_variables") or "{}")
    except json.JSONDecodeError:
        return {}


def set_variables(values: dict) -> None:
    store.set_setting("wf_variables", json.dumps(values, ensure_ascii=False))
