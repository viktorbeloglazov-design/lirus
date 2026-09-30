"""Работа с данными: настройки, журнал, сотрудники, подключения."""
from __future__ import annotations

import json
import logging
import secrets as pysecrets
import time
from dataclasses import dataclass, field
from typing import Any

from . import crypto, db

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- настройки

SETTING_DEFAULTS: dict[str, str] = {
    "company_name": "",
    "public_url": "",
    "proxy_url": "",
    "journal_days": "90",
    "auto_backup": "1",
    "backup_keep": "14",
    "auto_check_hours": "6",
    "https_port": "443",
    "instance_id": "",
    "executions_days": "30",
}
SECRET_SETTINGS = {"proxy_url"}


def get_setting(key: str) -> str:
    row = db.query_one("SELECT value, is_secret FROM settings WHERE key = ?", (key,))
    if row is None:
        return SETTING_DEFAULTS.get(key, "")
    if row["is_secret"] and row["value"]:
        try:
            return crypto.decrypt(row["value"])
        except crypto.SecretUnreadable:
            return ""
    return row["value"]


def set_setting(key: str, value: str) -> None:
    is_secret = key in SECRET_SETTINGS
    stored = crypto.encrypt(value) if (is_secret and value) else value
    db.execute(
        "INSERT INTO settings(key, value, is_secret) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, is_secret = excluded.is_secret",
        (key, stored, int(is_secret)),
    )


def get_int_setting(key: str, default: int) -> int:
    try:
        return int(float(get_setting(key)))
    except ValueError:
        return default


def instance_id() -> str:
    value = get_setting("instance_id")
    if not value:
        value = pysecrets.token_hex(8)
        set_setting("instance_id", value)
    return value


# ------------------------------------------------------------------ журнал

LEVELS = {"info": "Сведения", "warning": "Внимание", "error": "Ошибка"}
SECTIONS = {
    "auth": "Вход",
    "connections": "Подключения",
    "employees": "Сотрудники",
    "testing": "Тестирование",
    "settings": "Настройки",
    "backup": "Резервные копии",
    "workflows": "Сценарии",
    "system": "Система",
}


def log_event(level: str, section: str, message: str, details: str = "",
              user: str = "", ip: str = "") -> None:
    try:
        db.execute(
            "INSERT INTO events(ts, level, section, message, details, user_login, ip) "
            "VALUES(?,?,?,?,?,?,?)",
            (time.time(), level, section, message, details, user, ip),
        )
    except Exception:  # журнал не должен ронять основную работу
        log.exception("Не удалось записать событие в журнал")
    getattr(log, "error" if level == "error" else "warning" if level == "warning" else "info")(
        "[%s] %s %s", section, message, details
    )


def list_events(level: str = "", section: str = "", search: str = "",
                limit: int = 100, offset: int = 0) -> tuple[list, int]:
    where, params = [], []
    if level in LEVELS:
        where.append("level = ?")
        params.append(level)
    if section in SECTIONS:
        where.append("section = ?")
        params.append(section)
    if search:
        where.append("(message LIKE ? OR details LIKE ? OR user_login LIKE ?)")
        params += [f"%{search}%"] * 3
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.query_one(f"SELECT COUNT(*) AS n FROM events {sql_where}", tuple(params))["n"]
    rows = db.query(
        f"SELECT * FROM events {sql_where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
        tuple(params + [limit, offset]),
    )
    return rows, total


def cleanup_events(days: int) -> int:
    days = max(days, 1)
    return db.execute("DELETE FROM events WHERE ts < ?", (time.time() - days * 86400,))


# -------------------------------------------------------------- сотрудники

def get_user(user_id: int):
    return db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))


def get_user_by_login(login: str):
    return db.query_one("SELECT * FROM users WHERE login = ?", (login.strip(),))


def list_users():
    return db.query("SELECT * FROM users ORDER BY active DESC, full_name COLLATE NOCASE, login")


def count_active_admins(exclude_id: int | None = None) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1 "
        "AND password_hash IS NOT NULL AND id != ?",
        (exclude_id or -1,),
    )
    return row["n"]


# ------------------------------------------------------------- подключения

@dataclass
class Connection:
    id: int
    type: str
    name: str
    enabled: bool
    config: dict[str, Any]
    secrets_raw: dict[str, str]
    status: str
    status_message: str
    status_action: str
    checked_at: float | None
    created_at: float
    updated_at: float
    unreadable: list[str] = field(default_factory=list)

    def secret(self, name: str) -> str:
        """Расшифрованное значение секрета (пустая строка, если его нет)."""
        token = self.secrets_raw.get(name)
        if not token:
            return ""
        try:
            return crypto.decrypt(token)
        except crypto.SecretUnreadable:
            if name not in self.unreadable:
                self.unreadable.append(name)
            return ""

    def has_secret(self, name: str) -> bool:
        return bool(self.secrets_raw.get(name))

    def masked(self, name: str) -> str:
        value = self.secret(name)
        if not value and self.has_secret(name):
            return "не читается — введите заново"
        return crypto.mask(value)

    def values(self) -> dict[str, Any]:
        """Все значения для проверки связи: обычные поля + расшифрованные секреты."""
        data = dict(self.config)
        for key in self.secrets_raw:
            data[key] = self.secret(key)
        return data


def _row_to_connection(row) -> Connection:
    return Connection(
        id=row["id"], type=row["type"], name=row["name"], enabled=bool(row["enabled"]),
        config=json.loads(row["config"] or "{}"), secrets_raw=json.loads(row["secrets"] or "{}"),
        status=row["status"], status_message=row["status_message"],
        status_action=row["status_action"], checked_at=row["checked_at"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def get_connection(conn_id: int) -> Connection | None:
    row = db.query_one("SELECT * FROM connections WHERE id = ?", (conn_id,))
    return _row_to_connection(row) if row else None


def list_connections(type_key: str | None = None) -> list[Connection]:
    if type_key:
        rows = db.query("SELECT * FROM connections WHERE type = ? ORDER BY id", (type_key,))
    else:
        rows = db.query("SELECT * FROM connections ORDER BY id")
    return [_row_to_connection(r) for r in rows]


def save_connection(conn_id: int | None, type_key: str, name: str, cfg: dict,
                    new_secrets: dict[str, str], enabled: bool = True) -> int:
    """Сохраняет подключение. Секреты из new_secrets заменяют старые;
    секреты, которых в new_secrets нет, остаются как были."""
    now = time.time()
    with db.transaction() as conn:
        if conn_id is None:
            enc = {k: crypto.encrypt(v) for k, v in new_secrets.items() if v}
            cur = conn.execute(
                "INSERT INTO connections(type, name, enabled, config, secrets, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (type_key, name, int(enabled), json.dumps(cfg, ensure_ascii=False),
                 json.dumps(enc), now, now),
            )
            return cur.lastrowid
        row = conn.execute("SELECT secrets FROM connections WHERE id = ?", (conn_id,)).fetchone()
        enc = json.loads(row["secrets"] or "{}") if row else {}
        for key, value in new_secrets.items():
            if value:
                enc[key] = crypto.encrypt(value)
        conn.execute(
            "UPDATE connections SET name = ?, enabled = ?, config = ?, secrets = ?, updated_at = ?, "
            "status = 'new', status_message = '', status_action = '' WHERE id = ?",
            (name, int(enabled), json.dumps(cfg, ensure_ascii=False), json.dumps(enc), now, conn_id),
        )
        return conn_id


def set_connection_status(conn_id: int, status: str, message: str, action: str) -> None:
    db.execute(
        "UPDATE connections SET status = ?, status_message = ?, status_action = ?, checked_at = ? "
        "WHERE id = ?",
        (status, message, action, time.time(), conn_id),
    )


def delete_connection(conn_id: int) -> None:
    db.execute("DELETE FROM connections WHERE id = ?", (conn_id,))


def all_secret_values() -> list[str]:
    """Все расшифрованные секреты — чтобы вычищать их из отчётов и журналов."""
    values: list[str] = []
    for c in list_connections():
        for key in c.secrets_raw:
            v = c.secret(key)
            if v:
                values.append(v)
    proxy = get_setting("proxy_url")
    if proxy:
        values.append(proxy)
    return values


# ------------------------------------------------------------ тестирование

def save_test_run(result: dict, user: str) -> None:
    db.execute("INSERT INTO test_runs(ts, user_login, result) VALUES(?,?,?)",
               (time.time(), user, json.dumps(result, ensure_ascii=False)))
    db.execute("DELETE FROM test_runs WHERE id NOT IN (SELECT id FROM test_runs ORDER BY id DESC LIMIT 20)")


def last_test_run() -> dict | None:
    row = db.query_one("SELECT * FROM test_runs ORDER BY id DESC LIMIT 1")
    if not row:
        return None
    data = json.loads(row["result"])
    data.setdefault("user", row["user_login"])
    return data


def month_usage(month: str) -> float:
    row = db.query_one("SELECT cost_usd FROM usage WHERE month = ?", (month,))
    return float(row["cost_usd"]) if row else 0.0


def add_usage(month: str, cost: float, input_tokens: int, output_tokens: int) -> None:
    db.execute(
        "INSERT INTO usage(month, cost_usd, input_tokens, output_tokens) VALUES(?,?,?,?) "
        "ON CONFLICT(month) DO UPDATE SET cost_usd = cost_usd + excluded.cost_usd, "
        "input_tokens = input_tokens + excluded.input_tokens, output_tokens = output_tokens + excluded.output_tokens",
        (month, cost, input_tokens, output_tokens))
