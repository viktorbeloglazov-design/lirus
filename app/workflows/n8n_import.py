"""Импорт сценариев из n8n (файл .json, выгруженный через «Download» в n8n).

Переносятся узлы, связи и положения на холсте. Параметры самых частых узлов
переводятся автоматически. Узлы, для которых у нас нет аналога, переносятся как
«неподдерживаемые» — их нужно заменить вручную (часто подходит «HTTP-запрос»).
Выражения n8n ({{ $json.x }}) работают как есть; сложный JavaScript внутри выражений
может потребовать правки.
"""
from __future__ import annotations

import json
import uuid

from .nodes import BY_N8N


def _expr(v):
    if isinstance(v, str) and v.startswith("="):
        return v[1:]
    if isinstance(v, dict):
        return {k: _expr(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expr(x) for x in v]
    return v


def _kv(block) -> list[dict]:
    rows = (block or {}).get("parameters") if isinstance(block, dict) else None
    return [{"key": r.get("name", ""), "value": _expr(r.get("value", ""))} for r in rows or []]


_OP_MAP = {
    "equals": "equals", "notEquals": "not_equals", "equal": "equals", "notEqual": "not_equals",
    "contains": "contains", "notContains": "not_contains", "startsWith": "starts_with", "endsWith": "ends_with",
    "gt": "gt", "gte": "gte", "lt": "lt", "lte": "lte", "larger": "gt", "largerEqual": "gte", "smaller": "lt",
    "smallerEqual": "lte", "empty": "is_empty", "notEmpty": "is_not_empty", "exists": "is_not_empty",
    "notExists": "is_empty", "true": "is_true", "false": "is_false", "regex": "regex", "after": "date_after",
    "before": "date_before", "isEmpty": "is_empty", "isNotEmpty": "is_not_empty",
}


def _conditions(p: dict) -> tuple[list[dict], str]:
    cond = p.get("conditions") or {}
    rows = []
    if isinstance(cond, dict) and "conditions" in cond:  # формат If v2
        for c in cond.get("conditions", []):
            op = (c.get("operator") or {}).get("operation", "equals")
            rows.append({"left": _expr(c.get("leftValue", "")), "op": _OP_MAP.get(op, "equals"),
                         "right": _expr(c.get("rightValue", ""))})
        combine = "or" if (cond.get("combinator") or p.get("combinator")) == "or" else "and"
    else:  # If v1
        for group in ("string", "number", "boolean", "dateTime"):
            for c in (cond.get(group) or []) if isinstance(cond, dict) else []:
                rows.append({"left": _expr(c.get("value1", "")), "op": _OP_MAP.get(c.get("operation", "equal"), "equals"),
                             "right": _expr(c.get("value2", ""))})
        combine = "or" if p.get("combineOperation") == "any" else "and"
    return rows or [{"left": "", "op": "equals", "right": ""}], combine


def _schedule(p: dict) -> dict:
    rules = ((p.get("rule") or {}).get("interval") or [{}])
    r = rules[0] if rules else {}
    field = r.get("field", "days")
    time_s = f"{int(r.get('triggerAtHour', 9) or 0):02d}:{int(r.get('triggerAtMinute', 0) or 0):02d}"
    if field == "cronExpression":
        return {"mode": "cron", "cron": r.get("expression", "0 9 * * *")}
    if field == "minutes":
        return {"mode": "interval", "minutes": r.get("minutesInterval", 5)}
    if field == "hours":
        return {"mode": "interval", "minutes": 60 * int(r.get("hoursInterval", 1))}
    if field == "weeks":
        days = r.get("triggerAtDay") or [1]
        return {"mode": "weekly", "weekday": str(days[0] if isinstance(days, list) else days), "time": time_s}
    if field == "months":
        return {"mode": "monthly", "monthday": r.get("triggerAtDayOfMonth", 1), "time": time_s}
    return {"mode": "daily", "time": time_s}


def _set_fields(p: dict) -> dict:
    rows = []
    for a in ((p.get("assignments") or {}).get("assignments") or []):
        t = {"number": "number", "boolean": "boolean", "object": "json", "array": "json"}.get(a.get("type"), "auto")
        rows.append({"name": a.get("name", ""), "value": _expr(a.get("value", "")), "type": t})
    for group, t in (("string", "string"), ("number", "number"), ("boolean", "boolean")):
        for v in ((p.get("values") or {}).get(group) or []):
            rows.append({"name": v.get("name", ""), "value": _expr(v.get("value", "")), "type": t})
    for v in ((p.get("fields") or {}).get("values") or []):
        rows.append({"name": v.get("name", ""), "value": _expr(v.get("stringValue", v.get("value", ""))), "type": "auto"})
    keep = not (p.get("keepOnlySet") or p.get("include") == "none" or p.get("includeOtherFields") is False)
    return {"fields": rows or [{"name": "", "value": "", "type": "auto"}], "keep_other": keep}


def convert_params(n8n_type: str, key: str, p: dict) -> tuple[dict, list[str]]:
    """Параметры n8n → параметры нашего узла. Возвращает (параметры, замечания)."""
    notes: list[str] = []
    p = p or {}
    if key == "http_request":
        out = {"method": (p.get("method") or p.get("requestMethod") or "GET").upper(), "url": _expr(p.get("url", "")),
               "query": _kv(p.get("queryParameters")), "headers": _kv(p.get("headerParameters"))}
        if p.get("sendBody") or p.get("jsonParameters"):
            if p.get("specifyBody") == "json" or p.get("jsonBody") or p.get("bodyParametersJson"):
                out.update(body_type="json", body_json=_expr(p.get("jsonBody") or p.get("bodyParametersJson") or "{}"))
            else:
                out.update(body_type="form", body_form=_kv(p.get("bodyParameters")))
        if p.get("authentication") not in (None, "none"):
            notes.append("Авторизацию запроса нужно настроить заново (учётные данные n8n не переносятся).")
        return out, notes
    if key in ("if", "filter"):
        rows, combine = _conditions(p)
        return {"conditions": rows, "combine": combine}, notes
    if key == "set":
        return _set_fields(p), notes
    if key == "code":
        if p.get("language") == "python" or p.get("pythonCode"):
            return {"mode": "each" if p.get("mode") == "runOnceForEachItem" else "all",
                    "code": p.get("pythonCode", "return items")}, notes
        js = p.get("jsCode") or p.get("functionCode") or ""
        notes.append("Код на JavaScript нужно переписать на Python — оригинал сохранён в комментарии.")
        commented = "\n".join("# " + line for line in js.splitlines())
        return {"mode": "all", "code": "# Код из n8n (JavaScript) — перепишите на Python:\n" + commented +
                "\nraise Exception('Код из n8n нужно переписать на Python')\n"}, notes
    if key == "schedule":
        return _schedule(p), notes
    if key == "webhook":
        resp = {"onReceived": "immediately", "lastNode": "last_node", "responseNode": "respond_node"}.get(
            p.get("responseMode", "onReceived"), "immediately")
        if p.get("authentication") not in (None, "none"):
            notes.append("Защиту вебхука нужно настроить заново.")
        return {"path": p.get("path", str(uuid.uuid4())[:8]), "method": p.get("httpMethod", "GET"), "response": resp}, notes
    if key == "merge":
        mode = p.get("mode", "append")
        if mode == "chooseBranch":
            return {"mode": "choose_2" if str(p.get("output", "input1")).endswith("2") else "choose_1"}, notes
        if mode in ("combine", "mergeByKey", "mergeByIndex", "multiplex"):
            how = p.get("combineBy") or p.get("combinationMode") or mode
            if how in ("combineByPosition", "mergeByIndex", "mergeByPosition"):
                return {"mode": "position"}, notes
            if how in ("combineAll", "multiplex"):
                return {"mode": "multiplex"}, notes
            f1 = p.get("fieldsToMatchString") or p.get("propertyName1") or ""
            return {"mode": "field", "field1": str(f1).split(",")[0].strip(),
                    "field2": str(p.get("propertyName2") or f1).split(",")[0].strip()}, notes
        return {"mode": "append"}, notes
    if key == "loop":
        return {"batch_size": p.get("batchSize", 10)}, notes
    if key == "wait":
        return {"amount": p.get("amount", 5), "unit": p.get("unit", "seconds") if p.get("unit") in
                ("seconds", "minutes", "hours") else "seconds"}, notes
    if key == "limit":
        return {"max_items": p.get("maxItems", 1), "keep": "last" if p.get("keep") == "lastItems" else "first"}, notes
    if key == "sort":
        f = (((p.get("sortFieldsUi") or {}).get("sortField")) or [{}])[0]
        return {"field": f.get("fieldName", ""), "order": "desc" if f.get("order") == "descending" else "asc"}, notes
    if key == "split_out":
        return {"field": p.get("fieldToSplitOut", "")}, notes
    if key == "telegram":
        return {"chat_id": _expr(p.get("chatId", "")), "text": _expr(p.get("text", ""))}, notes
    if key == "email_send":
        return {"to": _expr(p.get("toEmail", "")), "subject": _expr(p.get("subject", "")),
                "body": _expr(p.get("text") or p.get("html") or ""), "html": bool(p.get("html"))}, notes
    if key == "gmail":
        return {"operation": "send", "to": _expr(p.get("sendTo", "")), "subject": _expr(p.get("subject", "")),
                "body": _expr(p.get("message", ""))}, notes
    if key == "respond_webhook":
        rw = p.get("respondWith", "firstIncomingItem")
        return {"respond_with": {"allIncomingItems": "all", "text": "text", "json": "text"}.get(rw, "first"),
                "body": _expr(p.get("responseBody", ""))}, notes
    if key == "note":
        return {"text": p.get("content", "")}, notes
    if key == "stop_error":
        return {"message": _expr(p.get("errorMessage", ""))}, notes
    if key in ("bitrix24", "claude"):
        notes.append("Параметры узла нужно заполнить заново.")
        return {}, notes
    return {}, notes


def convert(data: dict | str) -> tuple[str, list[dict], list[dict], list[str]]:
    """n8n JSON → (имя, узлы, связи, замечания)."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Файл не является JSON: {exc.msg} (строка {exc.lineno}).") from exc
    if not isinstance(data, dict) or "nodes" not in data:
        raise ValueError("Это не сценарий n8n: в файле нет списка узлов (nodes).")
    nodes, notes = [], []
    ids_by_name: dict[str, str] = {}
    for n in data.get("nodes", []):
        nid = "n" + uuid.uuid4().hex[:8]
        ids_by_name[n.get("name", nid)] = nid
        ntype = BY_N8N.get(n.get("type", ""))
        pos = n.get("position") or [0, 0]
        node = {"id": nid, "name": n.get("name", nid), "position": [int(pos[0]), int(pos[1])],
                "disabled": bool(n.get("disabled"))}
        if n.get("retryOnFail") or n.get("continueOnFail") or n.get("onError") in ("continueRegularOutput",):
            node["settings"] = {"retry": bool(n.get("retryOnFail")), "max_tries": n.get("maxTries", 3),
                                "wait_ms": n.get("waitBetweenTries", 1000),
                                "continue_on_fail": bool(n.get("continueOnFail")) or n.get("onError") == "continueRegularOutput"}
        if ntype is None:
            node.update(type="unsupported", params={"n8n_type": n.get("type", ""), "original": n.get("parameters", {})})
            notes.append(f"Узел «{node['name']}» ({n.get('type')}) не поддерживается — замените его вручную.")
        else:
            params, extra = convert_params(n.get("type", ""), ntype.key, n.get("parameters") or {})
            node.update(type=ntype.key, params={**ntype.defaults(), **params})
            notes += [f"«{node['name']}»: {x}" for x in extra]
        nodes.append(node)
    edges = []
    for src, outs in (data.get("connections") or {}).items():
        for out_idx, targets in enumerate((outs or {}).get("main") or []):
            for t in targets or []:
                if src in ids_by_name and t.get("node") in ids_by_name:
                    edges.append({"from": ids_by_name[src], "out": out_idx, "to": ids_by_name[t["node"]],
                                  "in": int(t.get("index", 0))})
    return data.get("name") or "Импорт из n8n", nodes, edges, notes


def export_n8n_like(wf: dict) -> dict:
    """Выгрузка в собственном формате (для переноса между платформами и резервного копирования)."""
    return {"format": "platforma-workflow", "version": 1, "name": wf["name"], "nodes": wf["nodes"],
            "connections": wf["connections"], "settings": wf.get("settings", {})}

