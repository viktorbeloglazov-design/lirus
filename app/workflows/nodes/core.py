"""Базовые узлы: запуск, логика, работа с данными."""
from __future__ import annotations

import asyncio
import copy
import csv
import hashlib
import io
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta

from ..expr import date_format, parse_date, to_number, unwrap
from .base import NodeContext, NodeError, NodeType, Param, item, items_json

G_TRIGGER = "Запуск"
G_FLOW = "Логика"
G_DATA = "Данные"

# ---------------------------------------------------------------- триггеры


async def passthrough(ctx: NodeContext):
    return [ctx.items or [item({})]]


MANUAL = NodeType(
    "manual", "Запуск вручную", G_TRIGGER,
    "Сценарий запускается кнопкой «Выполнить» в редакторе. Удобно для проверки.",
    passthrough, inputs=0, trigger=True, icon="▶", color="#2f5bea",
    n8n_types=["n8n-nodes-base.manualTrigger", "n8n-nodes-base.start"],
)

SCHEDULE_MODES = [("interval", "Каждые N минут"), ("hourly", "Каждый час"), ("daily", "Каждый день"),
                  ("weekdays", "По будням"), ("weekly", "Раз в неделю"), ("monthly", "Раз в месяц"),
                  ("cron", "Своё расписание (cron)")]
WEEKDAYS = [("1", "Понедельник"), ("2", "Вторник"), ("3", "Среда"), ("4", "Четверг"), ("5", "Пятница"),
            ("6", "Суббота"), ("0", "Воскресенье")]


async def schedule_exec(ctx: NodeContext):
    if ctx.items and ctx.items[0].get("json"):
        return [ctx.items]
    now = datetime.now()
    return [[item({"время": now.isoformat(timespec="seconds"), "дата": now.strftime("%d.%m.%Y"),
                   "час": now.hour, "минута": now.minute, "день_недели": now.isoweekday()})]]


SCHEDULE = NodeType(
    "schedule", "Расписание", G_TRIGGER,
    "Запускает сценарий автоматически: каждые N минут, каждый день в заданное время и т.д. "
    "Работает, только когда сценарий включён.",
    schedule_exec, inputs=0, trigger=True, icon="⏰", color="#2f5bea",
    n8n_types=["n8n-nodes-base.scheduleTrigger", "n8n-nodes-base.cron", "n8n-nodes-base.interval"],
    params=[
        Param("mode", "Как часто", "select", "daily", options=SCHEDULE_MODES),
        Param("minutes", "Каждые, минут", "number", 15, show_if={"mode": ["interval"]}),
        Param("time", "Во сколько", "string", "09:00", placeholder="09:00",
              show_if={"mode": ["hourly", "daily", "weekdays", "weekly", "monthly"]},
              hint="Для «Каждый час» учитываются только минуты."),
        Param("weekday", "День недели", "select", "1", options=WEEKDAYS, show_if={"mode": ["weekly"]}),
        Param("monthday", "Число месяца", "number", 1, show_if={"mode": ["monthly"]}),
        Param("cron", "Расписание cron", "string", "0 9 * * 1-5", show_if={"mode": ["cron"]},
              hint="минута час день месяц день_недели. «0 9 * * 1-5» — в 9:00 по будням."),
    ],
)

WEBHOOK = NodeType(
    "webhook", "Вебхук (входящий запрос)", G_TRIGGER,
    "Сценарий запускается, когда внешняя система отправляет запрос на адрес платформы. "
    "Адрес для рабочего режима работает, только когда сценарий включён.",
    passthrough, inputs=0, trigger=True, icon="⇲", color="#2f5bea",
    n8n_types=["n8n-nodes-base.webhook"],
    params=[
        Param("path", "Путь", "string", "", required=True, placeholder="novyi-zakaz", no_expr=True,
              hint="Часть адреса после /webhook/. Латиница, цифры, дефис."),
        Param("method", "Метод", "select", "POST",
              options=[("POST", "POST"), ("GET", "GET"), ("PUT", "PUT"), ("PATCH", "PATCH"), ("DELETE", "DELETE"),
                       ("ANY", "Любой")], no_expr=True),
        Param("auth", "Защита", "select", "none", no_expr=True,
              options=[("none", "Без защиты"), ("header", "Секрет в заголовке"), ("basic", "Логин и пароль")]),
        Param("auth_header", "Имя заголовка", "string", "X-Token", show_if={"auth": ["header"]}, no_expr=True),
        Param("auth_value", "Секрет / пароль", "string", "", show_if={"auth": ["header", "basic"]}, no_expr=True,
              hint="Любая длинная случайная строка. Отправитель должен передавать её в запросе."),
        Param("auth_user", "Логин", "string", "", show_if={"auth": ["basic"]}, no_expr=True),
        Param("response", "Когда отвечать", "select", "immediately", no_expr=True,
              options=[("immediately", "Сразу: «принято»"), ("last_node", "Когда сценарий закончится: данные последнего узла"),
                       ("respond_node", "Узлом «Ответ на вебхук»")]),
        Param("test_data", "Пример данных для ручного запуска", "json", '{\n  "body": {"пример": 1}\n}', no_expr=True,
              hint="Что подставить вместо запроса, когда сценарий запускают кнопкой «Выполнить»."),
    ],
)

ERROR_TRIGGER = NodeType(
    "error_trigger", "При ошибке в сценарии", G_TRIGGER,
    "Запускается, когда другой сценарий завершился ошибкой (если в его настройках выбран этот сценарий "
    "как «сценарий для ошибок»). Данные: имя сценария, узел, текст ошибки.",
    passthrough, inputs=0, trigger=True, icon="⚠", color="#c62828",
    n8n_types=["n8n-nodes-base.errorTrigger"],
)

SUB_TRIGGER = NodeType(
    "subworkflow_trigger", "При вызове из другого сценария", G_TRIGGER,
    "Начало вспомогательного сценария, который вызывают узлом «Выполнить сценарий».",
    passthrough, inputs=0, trigger=True, icon="↳", color="#2f5bea",
    n8n_types=["n8n-nodes-base.executeWorkflowTrigger"],
)

# ------------------------------------------------------------------- условия

OPS = [
    ("equals", "равно"), ("not_equals", "не равно"), ("contains", "содержит"), ("not_contains", "не содержит"),
    ("starts_with", "начинается с"), ("ends_with", "заканчивается на"), ("gt", "больше"), ("gte", "больше или равно"),
    ("lt", "меньше"), ("lte", "меньше или равно"), ("is_empty", "пусто"), ("is_not_empty", "не пусто"),
    ("is_true", "истина"), ("is_false", "ложь"), ("regex", "подходит под шаблон (regex)"),
    ("date_after", "дата позже"), ("date_before", "дата раньше"),
]
COND_COLUMNS = [
    {"name": "left", "label": "Значение", "kind": "string", "placeholder": "{{ $json.сумма }}"},
    {"name": "op", "label": "Условие", "kind": "select", "options": [list(o) for o in OPS]},
    {"name": "right", "label": "С чем сравнить", "kind": "string"},
]


def _empty(v) -> bool:
    return v is None or (isinstance(v, (str, list, dict)) and len(v) == 0) or (isinstance(v, str) and not v.strip())


def compare(left, op: str, right) -> bool:
    if op == "is_empty":
        return _empty(left)
    if op == "is_not_empty":
        return not _empty(left)
    if op == "is_true":
        return left is True or str(left).strip().lower() in ("true", "1", "да", "yes")
    if op == "is_false":
        return left is False or str(left).strip().lower() in ("false", "0", "нет", "no", "")
    if op in ("gt", "gte", "lt", "lte"):
        a, b = to_number(left), to_number(right)
        if a is None or b is None:
            a, b = str(left or ""), str(right or "")
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    if op in ("date_after", "date_before"):
        a, b = parse_date(left), parse_date(right)
        if a is None or b is None:
            return False
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
        return a > b if op == "date_after" else a < b
    if op in ("equals", "not_equals"):
        a, b = to_number(left), to_number(right)
        same = (a == b) if (a is not None and b is not None and not isinstance(left, bool)) else \
            (str(unwrap(left)) if left is not None else "") == (str(unwrap(right)) if right is not None else "")
        return same if op == "equals" else not same
    ls = json.dumps(unwrap(left), ensure_ascii=False) if isinstance(left, (list, dict)) else str(left if left is not None else "")
    rs = str(right if right is not None else "")
    if op == "contains":
        return (rs in left) if isinstance(left, list) else rs.lower() in ls.lower()
    if op == "not_contains":
        return not ((rs in left) if isinstance(left, list) else rs.lower() in ls.lower())
    if op == "starts_with":
        return ls.lower().startswith(rs.lower())
    if op == "ends_with":
        return ls.lower().endswith(rs.lower())
    if op == "regex":
        try:
            return re.search(rs, ls) is not None
        except re.error as exc:
            raise NodeError(f"Шаблон «{rs}» написан с ошибкой: {exc}") from exc
    raise NodeError(f"Неизвестное условие «{op}»")


def check_conditions(ctx: NodeContext, index: int, param: str = "conditions", combine_param: str = "combine") -> bool:
    rows = ctx.raw(param) or []
    if not rows:
        return True
    ns = ctx.namespace(index)
    from ..expr import ExpressionError, render

    results = []
    for row in rows:
        try:
            left = render(row.get("left", ""), ns)
            right = render(row.get("right", ""), ns)
        except ExpressionError as exc:
            raise NodeError(f"Условие: {exc}") from exc
        results.append(compare(left, row.get("op", "equals"), right))
    return all(results) if (ctx.raw(combine_param) or "and") == "and" else any(results)


async def if_exec(ctx: NodeContext):
    yes, no = [], []
    for i, it in enumerate(ctx.items):
        (yes if check_conditions(ctx, i) else no).append(it)
    return [yes, no]


COND_PARAMS = [
    Param("conditions", "Условия", "list", [{"left": "", "op": "equals", "right": ""}], columns=COND_COLUMNS,
          hint="Слева обычно выражение вида {{ $json.поле }}, справа — значение."),
    Param("combine", "Если условий несколько", "select", "and",
          options=[("and", "должны выполниться все"), ("or", "достаточно одного")]),
]

IF = NodeType(
    "if", "Условие (если)", G_FLOW,
    "Делит элементы на два потока: где условие выполняется («да») и где нет («нет»).",
    if_exec, outputs=["да", "нет"], icon="⋔", color="#0f766e", params=COND_PARAMS,
    n8n_types=["n8n-nodes-base.if"],
)


async def filter_exec(ctx: NodeContext):
    return [[it for i, it in enumerate(ctx.items) if check_conditions(ctx, i)]]


FILTER = NodeType(
    "filter", "Фильтр", G_FLOW, "Пропускает дальше только элементы, подходящие под условия.",
    filter_exec, icon="⏷", color="#0f766e", params=COND_PARAMS, n8n_types=["n8n-nodes-base.filter"],
)


def switch_outputs(params: dict) -> list[str]:
    rules = params.get("rules") or []
    names = [(r.get("output") or f"Правило {i + 1}") for i, r in enumerate(rules)]
    return names + ["иначе"]


async def switch_exec(ctx: NodeContext):
    rules = ctx.raw("rules") or []
    outs: list[list] = [[] for _ in range(len(rules) + 1)]
    mode = ctx.raw("mode") or "first"
    from ..expr import ExpressionError, render

    for i, it in enumerate(ctx.items):
        ns = ctx.namespace(i)
        matched = False
        for r_i, rule in enumerate(rules):
            try:
                ok = compare(render(rule.get("left", ""), ns), rule.get("op", "equals"), render(rule.get("right", ""), ns))
            except ExpressionError as exc:
                raise NodeError(f"Правило {r_i + 1}: {exc}") from exc
            if ok:
                outs[r_i].append(it)
                matched = True
                if mode == "first":
                    break
        if not matched:
            outs[-1].append(it)
    return outs


SWITCH = NodeType(
    "switch", "Переключатель", G_FLOW,
    "Раскладывает элементы по нескольким выходам по правилам. Что не подошло ни под одно — в выход «иначе».",
    switch_exec, outputs=["Правило 1", "иначе"], icon="⑂", color="#0f766e", dynamic_outputs=switch_outputs,
    n8n_types=["n8n-nodes-base.switch"],
    params=[
        Param("rules", "Правила", "list", [{"left": "", "op": "equals", "right": "", "output": "Правило 1"}],
              columns=COND_COLUMNS + [{"name": "output", "label": "Название выхода", "kind": "string"}]),
        Param("mode", "Если подходит несколько правил", "select", "first",
              options=[("first", "отправить в первое подходящее"), ("all", "отправить во все подходящие")]),
    ],
)

# --------------------------------------------------------------- слияние


async def merge_exec(ctx: NodeContext):
    a = ctx.inputs[0] if len(ctx.inputs) > 0 else []
    b = ctx.inputs[1] if len(ctx.inputs) > 1 else []
    mode = ctx.raw("mode") or "append"
    if mode == "append":
        return [a + b]
    if mode == "choose_1":
        return [a]
    if mode == "choose_2":
        return [b]
    if mode == "position":
        out = []
        for i in range(max(len(a), len(b))):
            merged = {}
            if i < len(a):
                merged.update(a[i]["json"])
            if i < len(b):
                merged.update(b[i]["json"])
            out.append(item(merged))
        return [out]
    if mode == "field":
        f1 = ctx.param_str("field1")
        f2 = ctx.param_str("field2") or f1
        join = ctx.raw("join") or "inner"
        index: dict[str, list] = {}
        for it in b:
            index.setdefault(str(it["json"].get(f2)), []).append(it)
        out = []
        for it in a:
            matches = index.get(str(it["json"].get(f1)), [])
            if matches:
                for m in matches:
                    out.append(item({**it["json"], **m["json"]}))
            elif join == "left":
                out.append(item(dict(it["json"])))
        return [out]
    if mode == "multiplex":
        return [[item({**x["json"], **y["json"]}) for x in a for y in b]]
    raise NodeError("Неизвестный режим слияния")


MERGE = NodeType(
    "merge", "Слияние", G_FLOW,
    "Объединяет данные двух потоков. Ждёт, пока придут оба входа.",
    merge_exec, inputs=2, icon="⧉", color="#0f766e", n8n_types=["n8n-nodes-base.merge"],
    params=[
        Param("mode", "Как объединить", "select", "append", options=[
            ("append", "Друг за другом (сначала вход 1, потом вход 2)"),
            ("position", "Попарно по порядку (1-й с 1-м, 2-й со 2-м)"),
            ("field", "По совпадению поля (как ВПР в Excel)"),
            ("multiplex", "Каждый с каждым"),
            ("choose_1", "Взять только вход 1 (дождавшись обоих)"),
            ("choose_2", "Взять только вход 2 (дождавшись обоих)"),
        ]),
        Param("field1", "Поле во входе 1", "string", "", show_if={"mode": ["field"]}, placeholder="артикул"),
        Param("field2", "Поле во входе 2", "string", "", show_if={"mode": ["field"]}, placeholder="артикул",
              hint="Если пусто — такое же, как во входе 1."),
        Param("join", "Если пары не нашлось", "select", "inner", show_if={"mode": ["field"]},
              options=[("inner", "не выводить элемент"), ("left", "вывести элемент из входа 1 как есть")]),
    ],
)

# ------------------------------------------------------------------- цикл


async def loop_exec(ctx: NodeContext):
    state = ctx.execution.node_state.setdefault(ctx.node["id"], {})
    size = max(1, int(ctx.param_num("batch_size", 0, 10)))
    if not state.get("active"):
        state.update(active=True, remaining=list(ctx.items), done=[])
    else:
        state["done"].extend(ctx.items)
    if state["remaining"]:
        batch, state["remaining"] = state["remaining"][:size], state["remaining"][size:]
        return [[], batch]
    done = state["done"]
    state.clear()
    return [done, []]


LOOP = NodeType(
    "loop", "Цикл по частям", G_FLOW,
    "Обрабатывает элементы порциями. Выход «цикл» соедините с узлами обработки, а их конец — обратно со входом "
    "этого узла. Когда всё обработано, данные выйдут через «готово».",
    loop_exec, outputs=["готово", "цикл"], icon="↻", color="#0f766e",
    n8n_types=["n8n-nodes-base.splitInBatches"],
    params=[Param("batch_size", "Размер порции", "number", 10)],
)


async def wait_exec(ctx: NodeContext):
    seconds = ctx.param_num("amount", 0, 5) * {"seconds": 1, "minutes": 60, "hours": 3600}[ctx.raw("unit") or "seconds"]
    if seconds > 3600:
        raise NodeError("Ожидание больше часа не поддерживается.",
                        "Разбейте сценарий на два и запускайте второй по расписанию.")
    await asyncio.sleep(max(0.0, seconds))
    return [ctx.items]


WAIT = NodeType(
    "wait", "Пауза", G_FLOW, "Ждёт заданное время и передаёт данные дальше (до 1 часа).",
    wait_exec, icon="⏸", color="#0f766e", n8n_types=["n8n-nodes-base.wait"],
    params=[Param("amount", "Сколько ждать", "number", 5),
            Param("unit", "Единица", "select", "seconds",
                  options=[("seconds", "секунд"), ("minutes", "минут"), ("hours", "часов")])],
)


async def noop_exec(ctx: NodeContext):
    return [ctx.items]


NOOP = NodeType("noop", "Ничего не делать", G_FLOW, "Передаёт данные дальше без изменений. Удобно как точка сбора.",
                noop_exec, icon="○", color="#64748b", n8n_types=["n8n-nodes-base.noOp"])


async def stop_exec(ctx: NodeContext):
    raise NodeError(ctx.param_str("message") or "Сценарий остановлен узлом «Остановить с ошибкой».")


STOP = NodeType(
    "stop_error", "Остановить с ошибкой", G_FLOW,
    "Прерывает сценарий с вашим текстом ошибки (сработает сценарий для ошибок, если он настроен).",
    stop_exec, outputs=[], icon="■", color="#c62828", n8n_types=["n8n-nodes-base.stopAndError"],
    params=[Param("message", "Текст ошибки", "string", "Что-то пошло не так")],
)


async def execute_workflow_exec(ctx: NodeContext):
    from ..engine import run_subworkflow

    wf_id = ctx.raw("workflow_id")
    if not wf_id:
        raise NodeError("Не выбран сценарий для вызова.", "Откройте узел и выберите сценарий.")
    mode = ctx.raw("mode") or "once"
    if mode == "once":
        return [await run_subworkflow(ctx, int(wf_id), ctx.items)]
    out = []
    for it in ctx.items:
        out.extend(await run_subworkflow(ctx, int(wf_id), [it]))
    return [out]


EXECUTE_WORKFLOW = NodeType(
    "execute_workflow", "Выполнить сценарий", G_FLOW,
    "Вызывает другой сценарий (он должен начинаться с узла «При вызове из другого сценария») и возвращает "
    "данные его последнего узла.",
    execute_workflow_exec, icon="⇉", color="#0f766e", n8n_types=["n8n-nodes-base.executeWorkflow"],
    params=[Param("workflow_id", "Сценарий", "workflow", ""),
            Param("mode", "Как вызывать", "select", "once",
                  options=[("once", "один раз со всеми элементами"), ("each", "отдельно для каждого элемента")])],
)


async def respond_exec(ctx: NodeContext):
    kind = ctx.raw("respond_with") or "first"
    if kind == "first":
        body = ctx.items[0]["json"] if ctx.items else {}
    elif kind == "all":
        body = items_json(ctx.items)
    else:
        body = ctx.param_str("body")
    from ..expr import render

    ns = ctx.namespace(0)
    headers = {str(r["key"]): str(render(r.get("value", ""), ns)) for r in (ctx.raw("headers") or []) if r.get("key")}
    ctx.execution.webhook_response = {"status": int(ctx.param_num("status", 0, 200)), "body": body, "headers": headers}
    return [ctx.items]


RESPOND = NodeType(
    "respond_webhook", "Ответ на вебхук", G_FLOW,
    "Что вернуть отправителю вебхука. Работает, если в узле «Вебхук» выбрано «Отвечать узлом «Ответ на вебхук»».",
    respond_exec, icon="⇱", color="#0f766e", n8n_types=["n8n-nodes-base.respondToWebhook"],
    params=[
        Param("respond_with", "Что вернуть", "select", "first",
              options=[("first", "Данные первого элемента (JSON)"), ("all", "Все элементы (список JSON)"),
                       ("text", "Свой текст")]),
        Param("body", "Текст ответа", "text", "", show_if={"respond_with": ["text"]}),
        Param("status", "Код ответа", "number", 200),
        Param("headers", "Заголовки", "list", [], columns=[{"name": "key", "label": "Заголовок", "kind": "string"},
                                                           {"name": "value", "label": "Значение", "kind": "string"}]),
    ],
)


async def note_exec(ctx: NodeContext):
    return []


NOTE = NodeType("note", "Заметка", G_FLOW, "Текстовая заметка на холсте. Не выполняется.",
                note_exec, inputs=0, outputs=[], icon="✎", color="#eab308",
                n8n_types=["n8n-nodes-base.stickyNote"],
                params=[Param("text", "Текст", "text", "Заметка", no_expr=True)])

# ------------------------------------------------------------------- данные


def set_path(obj: dict, path: str, value) -> None:
    parts = [p for p in path.split(".") if p]
    cur = obj
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    if parts:
        cur[parts[-1]] = value


def get_path(obj, path: str):
    cur = obj
    for p in [p for p in str(path).split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list) and p.isdigit() and int(p) < len(cur):
            cur = cur[int(p)]
        else:
            return None
    return cur


def convert(value, kind: str):
    if kind == "number":
        n = to_number(value)
        if n is None and value not in (None, ""):
            raise NodeError(f"Значение «{value}» нельзя превратить в число.")
        return n
    if kind == "boolean":
        return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "да", "yes")
    if kind == "json":
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value) if value not in (None, "") else None
        except json.JSONDecodeError as exc:
            raise NodeError(f"Значение не является JSON: {exc.msg}") from exc
    if kind == "string":
        return "" if value is None else (json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
    return value


async def set_exec(ctx: NodeContext):
    from ..expr import ExpressionError, render

    rows = ctx.raw("fields") or []
    keep = ctx.param_bool("keep_other", 0, True)
    out = []
    for i, it in enumerate(ctx.items or [item({})]):
        ns = ctx.namespace(i, ctx.items or [item({})])
        data = copy.deepcopy(it["json"]) if keep else {}
        for row in rows:
            name = str(row.get("name", "")).strip()
            if not name:
                continue
            try:
                value = render(row.get("value", ""), ns)
            except ExpressionError as exc:
                raise NodeError(f"Поле «{name}»: {exc}") from exc
            set_path(data, name, convert(value, row.get("type") or "auto"))
        out.append(item(data))
    return [out]


SET = NodeType(
    "set", "Задать поля", G_DATA,
    "Добавляет или меняет поля у каждого элемента. Можно писать выражения: {{ $json.цена * 1.2 }}. "
    "Вложенные поля — через точку: клиент.телефон.",
    set_exec, icon="✎", color="#7c3aed", n8n_types=["n8n-nodes-base.set"],
    params=[
        Param("fields", "Поля", "list", [{"name": "", "value": "", "type": "auto"}], columns=[
            {"name": "name", "label": "Имя поля", "kind": "string"},
            {"name": "value", "label": "Значение", "kind": "string"},
            {"name": "type", "label": "Тип", "kind": "select",
             "options": [["auto", "как есть"], ["string", "текст"], ["number", "число"], ["boolean", "да/нет"],
                         ["json", "JSON"]]},
        ]),
        Param("keep_other", "Оставить остальные поля", "boolean", True),
    ],
)


async def code_exec(ctx: NodeContext):
    from ..code_runner import run_code

    result, logs = await run_code(ctx.raw("code") or "", items_json(ctx.items), ctx.raw("mode") or "all",
                                  timeout=float(ctx.raw("timeout") or 30))
    for line in logs:
        ctx.log(line)
    return [[item(x) for x in result]]


CODE = NodeType(
    "code", "Код (Python)", G_DATA,
    "Свой код на Python для сложной обработки. Данные — в переменной items (список словарей). "
    "Верните список словарей через return. print() попадает в журнал узла.",
    code_exec, icon="{}", color="#7c3aed", n8n_types=["n8n-nodes-base.code", "n8n-nodes-base.function",
                                                        "n8n-nodes-base.functionItem"],
    params=[
        Param("mode", "Как выполнять", "select", "all",
              options=[("all", "один раз для всех элементов (items)"), ("each", "для каждого элемента (item)")]),
        Param("code", "Код", "code", "# items — список словарей\nfor item in items:\n    item['обработано'] = True\n"
                                     "return items\n", no_expr=True,
              hint="В режиме «для каждого элемента» доступна переменная item, верните словарь. "
                   "Доступны модули json, re, math, datetime, decimal, statistics."),
        Param("timeout", "Ограничение времени, секунд", "number", 30, no_expr=True),
    ],
)


async def sort_exec(ctx: NodeContext):
    field = ctx.param_str("field")
    desc = (ctx.raw("order") or "asc") == "desc"

    def key(it):
        v = get_path(it["json"], field)
        n = to_number(v)
        return (0, n, "") if n is not None else (1, 0, str(v or "").lower())
    return [sorted(ctx.items, key=key, reverse=desc)]


SORT = NodeType("sort", "Сортировка", G_DATA, "Упорядочивает элементы по полю (числа — как числа, текст — по алфавиту).",
                sort_exec, icon="⇅", color="#7c3aed", n8n_types=["n8n-nodes-base.sort"],
                params=[Param("field", "Поле", "string", "", placeholder="сумма"),
                        Param("order", "Порядок", "select", "asc",
                              options=[("asc", "по возрастанию"), ("desc", "по убыванию")])])


async def limit_exec(ctx: NodeContext):
    n = max(0, int(ctx.param_num("max_items", 0, 10)))
    return [ctx.items[-n:] if (ctx.raw("keep") == "last" and n) else ctx.items[:n]]


LIMIT = NodeType("limit", "Ограничить количество", G_DATA, "Оставляет только первые (или последние) N элементов.",
                 limit_exec, icon="≤", color="#7c3aed", n8n_types=["n8n-nodes-base.limit"],
                 params=[Param("max_items", "Сколько оставить", "number", 10),
                         Param("keep", "Какие", "select", "first",
                               options=[("first", "первые"), ("last", "последние")])])


async def dedup_exec(ctx: NodeContext):
    fields = [f.strip() for f in ctx.param_str("fields").split(",") if f.strip()]
    seen, out = set(), []
    for it in ctx.items:
        key = json.dumps([get_path(it["json"], f) for f in fields] if fields else it["json"], sort_keys=True,
                         ensure_ascii=False, default=str)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return [out]


DEDUP = NodeType("remove_duplicates", "Убрать повторы", G_DATA, "Удаляет повторяющиеся элементы.",
                 dedup_exec, icon="≠", color="#7c3aed", n8n_types=["n8n-nodes-base.removeDuplicates"],
                 params=[Param("fields", "Сравнивать по полям", "string", "", placeholder="артикул, склад",
                               hint="Через запятую. Пусто — сравниваются элементы целиком.")])


async def aggregate_exec(ctx: NodeContext):
    if (ctx.raw("mode") or "all") == "all":
        return [[item({ctx.param_str("output") or "элементы": items_json(ctx.items)})]]
    field = ctx.param_str("field")
    return [[item({ctx.param_str("output") or field: [get_path(it["json"], field) for it in ctx.items]})]]


AGGREGATE = NodeType(
    "aggregate", "Собрать в один элемент", G_DATA, "Собирает все элементы (или значения одного поля) в один элемент со списком.",
    aggregate_exec, icon="⊕", color="#7c3aed", n8n_types=["n8n-nodes-base.aggregate", "n8n-nodes-base.itemLists"],
    params=[Param("mode", "Что собрать", "select", "all",
                  options=[("all", "все элементы целиком"), ("field", "значения одного поля")]),
            Param("field", "Поле", "string", "", show_if={"mode": ["field"]}),
            Param("output", "Имя поля для списка", "string", "элементы")])


async def split_out_exec(ctx: NodeContext):
    field = ctx.param_str("field")
    include = ctx.param_bool("include_other", 0, False)
    out = []
    for it in ctx.items:
        values = get_path(it["json"], field)
        if values is None:
            continue
        if not isinstance(values, list):
            values = [values]
        for v in values:
            base = {k: v2 for k, v2 in it["json"].items() if k != field} if include else {}
            if isinstance(v, dict):
                out.append(item({**base, **v}))
            else:
                out.append(item({**base, field.split(".")[-1]: v}))
    return [out]


SPLIT_OUT = NodeType(
    "split_out", "Разделить список на элементы", G_DATA,
    "Превращает поле-список в отдельные элементы: один заказ с 5 товарами → 5 элементов.",
    split_out_exec, icon="⊘", color="#7c3aed", n8n_types=["n8n-nodes-base.splitOut"],
    params=[Param("field", "Поле со списком", "string", "", placeholder="товары"),
            Param("include_other", "Добавить к каждому остальные поля", "boolean", False)])

AGG_OPS = [["sum", "сумма"], ["count", "количество"], ["avg", "среднее"], ["min", "минимум"], ["max", "максимум"],
           ["concat", "перечислить через запятую"], ["unique_count", "количество разных"]]


async def summarize_exec(ctx: NodeContext):
    group_fields = [f.strip() for f in ctx.param_str("group_by").split(",") if f.strip()]
    aggs = ctx.raw("aggregations") or []
    groups: dict[str, dict] = {}
    for it in ctx.items:
        key_vals = [get_path(it["json"], f) for f in group_fields]
        key = json.dumps(key_vals, ensure_ascii=False, default=str)
        g = groups.setdefault(key, {"keys": key_vals, "rows": []})
        g["rows"].append(it["json"])
    out = []
    for g in groups.values():
        rec = dict(zip(group_fields, g["keys"]))
        for a in aggs:
            f, op = a.get("field", ""), a.get("op", "sum")
            vals = [get_path(r, f) for r in g["rows"]]
            nums = [n for n in (to_number(v) for v in vals) if n is not None]
            name = a.get("name") or f"{op}_{f}"
            if op == "sum":
                rec[name] = round(sum(nums), 10)
            elif op == "count":
                rec[name] = len([v for v in vals if v not in (None, "")]) if f else len(vals)
            elif op == "avg":
                rec[name] = round(sum(nums) / len(nums), 10) if nums else None
            elif op == "min":
                rec[name] = min(nums) if nums else None
            elif op == "max":
                rec[name] = max(nums) if nums else None
            elif op == "concat":
                rec[name] = ", ".join(str(v) for v in vals if v not in (None, ""))
            elif op == "unique_count":
                rec[name] = len({json.dumps(v, default=str) for v in vals})
        out.append(item(rec))
    return [out]


SUMMARIZE = NodeType(
    "summarize", "Итоги (сводная)", G_DATA,
    "Группирует элементы и считает сумму, количество, среднее — как сводная таблица в Excel.",
    summarize_exec, icon="Σ", color="#7c3aed", n8n_types=["n8n-nodes-base.summarize"],
    params=[
        Param("group_by", "Группировать по полям", "string", "", placeholder="склад, категория",
              hint="Через запятую. Пусто — один итог по всем элементам."),
        Param("aggregations", "Что посчитать", "list", [{"op": "sum", "field": "", "name": ""}], columns=[
            {"name": "op", "label": "Действие", "kind": "select", "options": AGG_OPS},
            {"name": "field", "label": "Поле", "kind": "string"},
            {"name": "name", "label": "Назвать результат", "kind": "string"},
        ]),
    ],
)


async def rename_exec(ctx: NodeContext):
    pairs = [(r.get("from", ""), r.get("to", "")) for r in (ctx.raw("pairs") or []) if r.get("from") and r.get("to")]
    out = []
    for it in ctx.items:
        data = copy.deepcopy(it["json"])
        for a, b in pairs:
            if a in data:
                data[b] = data.pop(a)
        out.append(item(data))
    return [out]


RENAME = NodeType("rename_keys", "Переименовать поля", G_DATA, "Меняет имена полей.", rename_exec, icon="Aa",
                  color="#7c3aed", n8n_types=["n8n-nodes-base.renameKeys"],
                  params=[Param("pairs", "Переименования", "list", [{"from": "", "to": ""}],
                                columns=[{"name": "from", "label": "Было", "kind": "string"},
                                         {"name": "to", "label": "Стало", "kind": "string"}])])


async def remove_fields_exec(ctx: NodeContext):
    fields = [f.strip() for f in ctx.param_str("fields").split(",") if f.strip()]
    only = (ctx.raw("mode") or "remove") == "keep"
    out = []
    for it in ctx.items:
        d = it["json"]
        out.append(item({k: v for k, v in d.items() if (k in fields) == only}))
    return [out]


REMOVE_FIELDS = NodeType(
    "remove_fields", "Оставить / убрать поля", G_DATA, "Удаляет лишние поля или оставляет только нужные.",
    remove_fields_exec, icon="✂", color="#7c3aed",
    params=[Param("mode", "Действие", "select", "keep", options=[("keep", "оставить только эти поля"),
                                                                  ("remove", "убрать эти поля")]),
            Param("fields", "Поля", "string", "", placeholder="артикул, остаток", hint="Через запятую.")])


async def datetime_exec(ctx: NodeContext):
    op = ctx.raw("operation") or "now"
    out = []
    for i, it in enumerate(ctx.items or [item({})]):
        data = dict(it["json"])
        target = ctx.param_str("output", i) or "дата"
        fmt = ctx.param_str("format", i) or "dd.MM.yyyy HH:mm"
        if op == "now":
            data[target] = date_format(datetime.now(), fmt)
        else:
            value = ctx.param("value", i)
            d = parse_date(value)
            if d is None:
                raise NodeError(f"Не удалось распознать дату «{value}».",
                                "Подходят форматы 2026-09-30, 30.09.2026, 30.09.2026 14:05, ISO с временем.")
            if op == "format":
                data[target] = date_format(d, fmt)
            elif op == "add":
                amount = ctx.param_num("amount", i, 1)
                unit = ctx.raw("unit") or "days"
                if unit == "months":
                    from ..expr import date_add

                    d = date_add(d, months=int(amount))
                else:
                    d = d + timedelta(**{unit: amount})
                data[target] = date_format(d, fmt)
            elif op == "diff":
                other = parse_date(ctx.param("value2", i)) or datetime.now()
                days = (other.replace(tzinfo=None) - d.replace(tzinfo=None)).total_seconds() / 86400
                data[target] = round(days, 2)
        out.append(item(data))
    return [out]


DATETIME = NodeType(
    "date_time", "Дата и время", G_DATA, "Текущее время, форматирование дат, прибавление дней, разница между датами.",
    datetime_exec, icon="📅", color="#7c3aed", n8n_types=["n8n-nodes-base.dateTime"],
    params=[
        Param("operation", "Что сделать", "select", "now", options=[
            ("now", "Записать текущее время"), ("format", "Переформатировать дату"),
            ("add", "Прибавить / отнять время"), ("diff", "Разница в днях между датами")]),
        Param("value", "Дата", "string", "", placeholder="{{ $json.дата_заказа }}",
              show_if={"operation": ["format", "add", "diff"]}),
        Param("value2", "Вторая дата", "string", "", show_if={"operation": ["diff"]}, hint="Пусто — сейчас."),
        Param("amount", "Сколько", "number", 1, show_if={"operation": ["add"]}, hint="Отрицательное число — отнять."),
        Param("unit", "Единица", "select", "days", show_if={"operation": ["add"]},
              options=[("minutes", "минут"), ("hours", "часов"), ("days", "дней"), ("weeks", "недель"),
                       ("months", "месяцев")]),
        Param("format", "Формат", "string", "dd.MM.yyyy HH:mm", show_if={"operation": ["now", "format", "add"]},
              hint="dd — день, MM — месяц, yyyy — год, HH — часы, mm — минуты."),
        Param("output", "Записать в поле", "string", "дата"),
    ],
)


async def crypto_exec(ctx: NodeContext):
    op = ctx.raw("operation") or "sha256"
    out = []
    import base64

    for i, it in enumerate(ctx.items or [item({})]):
        data = dict(it["json"])
        v = ctx.param_str("value", i)
        target = ctx.param_str("output", i) or "результат"
        if op in ("md5", "sha1", "sha256", "sha512"):
            data[target] = hashlib.new(op, v.encode()).hexdigest()
        elif op == "uuid":
            data[target] = str(uuid.uuid4())
        elif op == "random":
            data[target] = secrets.token_urlsafe(24)
        elif op == "b64enc":
            data[target] = base64.b64encode(v.encode()).decode()
        elif op == "b64dec":
            try:
                data[target] = base64.b64decode(v).decode("utf-8", "replace")
            except ValueError as exc:
                raise NodeError("Строка не в формате base64.") from exc
        out.append(item(data))
    return [out]


CRYPTO = NodeType(
    "crypto", "Хеш и шифры", G_DATA, "Хеш (md5, sha256), случайные строки, UUID, base64.",
    crypto_exec, icon="#", color="#7c3aed", n8n_types=["n8n-nodes-base.crypto"],
    params=[Param("operation", "Что сделать", "select", "sha256", options=[
        ("sha256", "Хеш SHA-256"), ("md5", "Хеш MD5"), ("sha1", "Хеш SHA-1"), ("sha512", "Хеш SHA-512"),
        ("uuid", "Новый UUID"), ("random", "Случайная строка"), ("b64enc", "Закодировать в base64"),
        ("b64dec", "Раскодировать из base64")]),
            Param("value", "Значение", "string", "", show_if={"operation": ["sha256", "md5", "sha1", "sha512",
                                                                          "b64enc", "b64dec"]}),
            Param("output", "Записать в поле", "string", "результат")])


async def csv_exec(ctx: NodeContext):
    op = ctx.raw("operation") or "to_items"
    delim = ctx.raw("delimiter") or ";"
    if op == "to_items":
        out = []
        for i, it in enumerate(ctx.items or [item({})]):
            text = ctx.param_str("text", i)
            reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=delim)
            out.extend(item(dict(r)) for r in reader)
        return [out]
    rows = items_json(ctx.items)
    headers: list[str] = []
    for r in rows:
        for k in r:
            if k not in headers:
                headers.append(k)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=headers, delimiter=delim, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
    return [[item({ctx.param_str("output") or "csv": buf.getvalue(), "строк": len(rows)})]]


CSV_NODE = NodeType(
    "csv", "Таблица CSV", G_DATA, "Разбирает CSV-текст в элементы или собирает элементы в CSV (открывается в Excel).",
    csv_exec, icon="▦", color="#7c3aed", n8n_types=["n8n-nodes-base.spreadsheetFile", "n8n-nodes-base.convertToFile",
                                                      "n8n-nodes-base.extractFromFile"],
    params=[Param("operation", "Что сделать", "select", "to_items",
                  options=[("to_items", "CSV → элементы"), ("to_csv", "Элементы → CSV")]),
            Param("text", "Текст CSV", "text", "{{ $json.csv }}", show_if={"operation": ["to_items"]}),
            Param("delimiter", "Разделитель", "select", ";", no_expr=True,
                  options=[(";", "точка с запятой (Excel в России)"), (",", "запятая"), ("\t", "табуляция")]),
            Param("output", "Поле для CSV", "string", "csv", show_if={"operation": ["to_csv"]})])


async def json_parse_exec(ctx: NodeContext):
    field = ctx.param_str("field")
    out = []
    for it in ctx.items:
        data = dict(it["json"])
        raw = get_path(data, field)
        if isinstance(raw, str):
            try:
                set_path(data, ctx.param_str("output") or field, json.loads(raw))
            except json.JSONDecodeError as exc:
                raise NodeError(f"В поле «{field}» не JSON: {exc.msg}") from exc
        out.append(item(data))
    return [out]


JSON_PARSE = NodeType("json_parse", "Разобрать JSON", G_DATA, "Превращает строку с JSON в объект.",
                      json_parse_exec, icon="{ }", color="#7c3aed",
                      params=[Param("field", "Поле с JSON-строкой", "string", "body"),
                              Param("output", "Записать в поле", "string", "", hint="Пусто — в то же поле.")])


NODES = [MANUAL, SCHEDULE, WEBHOOK, ERROR_TRIGGER, SUB_TRIGGER,
         IF, SWITCH, FILTER, MERGE, LOOP, WAIT, EXECUTE_WORKFLOW, RESPOND, STOP, NOOP, NOTE,
         SET, CODE, SORT, LIMIT, DEDUP, AGGREGATE, SPLIT_OUT, SUMMARIZE, RENAME, REMOVE_FIELDS, DATETIME, CRYPTO,
         CSV_NODE, JSON_PARSE]
