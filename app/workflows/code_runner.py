"""Запуск кода Python из узла «Код» в отдельном процессе.

Отдельный процесс нужен, чтобы ошибка или зависание в коде пользователя не уронили
панель: по истечении времени процесс принудительно завершается.
Код может всё, что может сервер, поэтому редактировать сценарии разрешено только администратору.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

from .nodes.base import NodeError

RUNNER = r'''
import json, sys, io, traceback, contextlib
import re, math, datetime, decimal, statistics, collections, itertools, random, hashlib, base64, uuid

payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
code, items, mode = payload["code"], payload["items"], payload["mode"]
src = "def __user__(items, item):\n" + "\n".join("    " + line for line in code.splitlines()) + "\n    return None\n"
out = io.StringIO()
class _Item:
    """Совместимость с кодом из n8n: _input.all()[0].json."""
    def __init__(self, d):
        self.json = d


class _Input:
    def all(self):
        return [_Item(i) for i in items]
    def first(self):
        return _Item(items[0]) if items else None
    def last(self):
        return _Item(items[-1]) if items else None


env = {"_input": _Input(), "json": json, "re": re, "math": math, "datetime": datetime, "decimal": decimal, "statistics": statistics,
       "collections": collections, "itertools": itertools, "random": random, "hashlib": hashlib,
       "base64": base64, "uuid": uuid}
try:
    exec(compile(src, "<код узла>", "exec"), env)
    with contextlib.redirect_stdout(out):
        if mode == "each":
            result = []
            for it in items:
                r = env["__user__"](items, it)
                result.append(it if r is None else r)
        else:
            result = env["__user__"](items, None)
            if result is None:
                result = items
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        raise TypeError("Код должен вернуть список словарей (return items), а вернул " + type(result).__name__)
    clean = []
    for r in result:
        if isinstance(r, _Item):
            r = r.json
        if isinstance(r, dict) and set(r) == {"json"} and isinstance(r["json"], dict):
            r = r["json"]
        clean.append(r if isinstance(r, dict) else {"value": r})
    sys.stdout.write(json.dumps({"ok": True, "items": clean, "log": out.getvalue()}, ensure_ascii=False, default=str))
except Exception as exc:
    tb = traceback.extract_tb(exc.__traceback__)
    line = next((f.lineno - 1 for f in reversed(tb) if f.filename == "<код узла>"), None)
    sys.stdout.write(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}", "line": line,
                                 "log": out.getvalue()}, ensure_ascii=False, default=str))
'''


async def run_code(code: str, items: list[dict], mode: str = "all", timeout: float = 30) -> tuple[list[dict], list[str]]:
    if not code.strip():
        raise NodeError("В узле «Код» нет кода.")
    try:
        compile("def f():\n" + "\n".join("    " + l for l in code.splitlines()) + "\n    pass\n", "<код узла>", "exec")
    except SyntaxError as exc:
        line = (exc.lineno or 2) - 1
        raise NodeError(f"Ошибка в написании кода, строка {line}: {exc.msg}",
                        "Проверьте отступы (4 пробела), двоеточия и скобки.") from exc
    payload = json.dumps({"code": code, "items": items, "mode": mode}, ensure_ascii=False, default=str).encode("utf-8")
    python = sys.executable
    kwargs: dict = {}
    if os.name == "nt":
        # pythonw.exe не даёт вывода — берём python.exe, но без окна консоли
        alt = os.path.join(os.path.dirname(python), "python.exe")
        if os.path.exists(alt):
            python = alt
        kwargs["creationflags"] = 0x08000000
    limit = max(1.0, min(timeout, 600))
    try:
        proc = await asyncio.to_thread(subprocess.run, [python, "-I", "-c", RUNNER], input=payload,
                                       capture_output=True, timeout=limit, **kwargs)
    except subprocess.TimeoutExpired:
        raise NodeError(f"Код выполнялся дольше {int(limit)} секунд и был остановлен.",
                        "Проверьте, нет ли бесконечного цикла, или увеличьте ограничение времени в узле.") from None
    stdout, stderr = proc.stdout, proc.stderr
    try:
        res = json.loads(stdout.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise NodeError("Код завершился аварийно.", "", stderr.decode("utf-8", "replace")[-2000:]) from None
    logs = [l for l in (res.get("log") or "").splitlines() if l.strip()]
    if not res.get("ok"):
        where = f" (строка {res['line']})" if res.get("line") else ""
        raise NodeError(f"Ошибка в коде{where}: {res.get('error')}", "Посмотрите вывод print() в журнале узла.",
                        "\n".join(logs))
    return res["items"], logs
