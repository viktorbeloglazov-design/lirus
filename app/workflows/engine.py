"""Движок выполнения сценариев.

Сценарий — граф узлов. Выполнение начинается с узла-триггера; каждый узел получает
элементы (список {"json": {...}}) на входы и выдаёт их на выходы. Узел с двумя входами
(«Слияние») ждёт оба. Циклы разрешены (узел «Цикл по частям»), но число шагов ограничено.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections import deque

from .. import store
from . import storage
from .nodes import BY_KEY, NodeError
from .nodes.base import Item, NodeContext, item

log = logging.getLogger(__name__)

MAX_STEPS = 5000        # защита от бесконечного цикла
MAX_STORED_ITEMS = 50   # сколько элементов каждого выхода сохранять в истории
MAX_DEPTH = 5           # вложенность вызовов «Выполнить сценарий»

RUNNING: dict[int, "Execution"] = {}
MODES = {"manual": "Вручную", "schedule": "По расписанию", "webhook": "Вебхук", "error": "Обработка ошибки",
         "subworkflow": "Из другого сценария", "test_webhook": "Тестовый вебхук"}


class ExecutionCancelled(Exception):
    pass


class Execution:
    def __init__(self, workflow: dict, mode: str, depth: int = 0):
        self.workflow = workflow
        self.mode = mode
        self.depth = depth
        self.id: int | None = None
        self.status = "running"
        self.started = time.time()
        self.finished: float | None = None
        self.error = ""
        self.error_hint = ""
        self.error_node = ""
        self.run_data: dict[str, list[dict]] = {}
        self.last_outputs: dict[str, list[list[Item]]] = {}
        self.node_state: dict[str, dict] = {}
        self.webhook_response: dict | None = None
        self.result: list[Item] = []
        self.current_node = ""
        self.variables = storage.get_variables()
        self.nodes = {n["id"]: n for n in workflow.get("nodes", [])}
        self.by_name = {n.get("name"): n for n in workflow.get("nodes", [])}
        self.cancelled = False
        self.task: asyncio.Task | None = None
        self.done = asyncio.Event()

    # --- для выражений
    def output_by_name(self, name: str):
        node = self.by_name.get(name)
        if not node:
            return None
        outs = self.last_outputs.get(node["id"])
        if outs is None:
            return None
        for o in outs:
            if o:
                return o
        return outs[0] if outs else []

    def run_count(self, node_id: str) -> int:
        return len(self.run_data.get(node_id, []))

    # --- сохранение
    def snapshot(self) -> dict:
        return {"run_data": self.run_data, "error": self.error, "error_hint": self.error_hint,
                "error_node": self.error_node, "current_node": self.current_node,
                "workflow": {"nodes": self.workflow.get("nodes", []), "connections": self.workflow.get("connections", [])}}

    def summary(self) -> dict:
        return {"id": self.id, "status": self.status, "mode": self.mode, "started_at": self.started,
                "finished_at": self.finished, "error": self.error, "error_hint": self.error_hint,
                "error_node": self.error_node, "current_node": self.current_node, "run_data": self.run_data}


def _store_items(items: list[Item]) -> dict:
    return {"count": len(items), "items": json.loads(json.dumps([i.get("json", {}) for i in items[:MAX_STORED_ITEMS]],
                                                                ensure_ascii=False, default=str))}


def _outputs_count(node: dict) -> int:
    nt = BY_KEY.get(node.get("type", ""))
    return len(nt.outputs_for({**nt.defaults(), **(node.get("params") or {})})) if nt else 1


def trigger_items(node: dict) -> list[Item]:
    """Данные для ручного запуска триггера."""
    t = node.get("type")
    params = node.get("params") or {}
    if t == "webhook":
        raw = params.get("test_data") or "{}"
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            data = {}
        base = {"method": params.get("method", "POST"), "path": params.get("path", ""), "headers": {}, "query": {},
                "body": {}}
        if isinstance(data, dict):
            base.update(data)
        return [item(base)]
    if t == "error_trigger":
        return [item({"сценарий": {"id": 0, "name": "Пример сценария"},
                      "выполнение": {"id": 0, "режим": "manual"},
                      "ошибка": {"сообщение": "Пример ошибки для проверки", "узел": "HTTP-запрос", "совет": ""}})]
    return []


async def _run_node(ex: Execution, node: dict, inputs: list[list[Item]]) -> list[list[Item]]:
    ntype = BY_KEY.get(node.get("type", ""))
    n_out = _outputs_count(node)
    if ntype is None:
        raise NodeError(f"Узел «{node.get('name')}»: тип «{node.get('type')}» не поддерживается.",
                        "Удалите узел или замените его подходящим (часто подходит «HTTP-запрос» или «Код»).")
    if node.get("disabled"):
        outs = [inputs[0] if inputs else []] + [[] for _ in range(n_out - 1)]
        return outs
    settings = node.get("settings") or {}
    if settings.get("execute_once") and inputs and inputs[0]:
        inputs = [inputs[0][:1]] + inputs[1:]
    tries = max(1, int(settings.get("max_tries") or 3)) if settings.get("retry") else 1
    wait_ms = int(settings.get("wait_ms") or 1000)
    last_exc: Exception | None = None
    for attempt in range(tries):
        ctx = NodeContext(ex, node, ntype, copy.deepcopy(inputs))
        try:
            outs = await ntype.execute(ctx)
            outs = [list(o or []) for o in (outs or [])]
            while len(outs) < n_out:
                outs.append([])
            if settings.get("always_output") and not any(outs):
                outs[0] = [item({})]
            ex._pending_logs = ctx.logs  # noqa: SLF001
            if attempt:
                ctx.logs.insert(0, f"Получилось с попытки {attempt + 1}")
            return outs
        except (asyncio.CancelledError, ExecutionCancelled):
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            ex._pending_logs = ctx.logs  # noqa: SLF001
            if attempt + 1 < tries:
                await asyncio.sleep(wait_ms / 1000)
    assert last_exc is not None
    if settings.get("continue_on_fail"):
        msg = last_exc.message if isinstance(last_exc, NodeError) else f"{type(last_exc).__name__}: {last_exc}"
        return [[item({"ошибка": msg})]] + [[] for _ in range(n_out - 1)]
    raise last_exc


async def execute(ex: Execution, start_node_id: str, start_items: list[Item], stop_at: str | None = None) -> None:
    wf = ex.workflow
    edges = wf.get("connections", [])
    out_edges: dict[str, list[dict]] = {}
    connected_inputs: dict[str, set[int]] = {}
    for e in edges:
        if e.get("from") in ex.nodes and e.get("to") in ex.nodes:
            out_edges.setdefault(e["from"], []).append(e)
            connected_inputs.setdefault(e["to"], set()).add(int(e.get("in", 0)))

    queue: deque = deque([(start_node_id, [start_items])])
    pending: dict[str, dict[int, list[Item]]] = {}
    steps = 0
    while queue or pending:
        if ex.cancelled:
            raise ExecutionCancelled()
        if not queue:
            # у «Слияния» пришли не все входы — выполняем с тем, что есть
            nid, parts = next(iter(pending.items()))
            del pending[nid]
            n_in = max(BY_KEY[ex.nodes[nid]["type"]].inputs, 1) if ex.nodes[nid]["type"] in BY_KEY else 1
            queue.append((nid, [parts.get(i, []) for i in range(n_in)]))
            continue
        node_id, inputs = queue.popleft()
        node = ex.nodes[node_id]
        steps += 1
        if steps > MAX_STEPS:
            raise NodeError(f"Сценарий сделал больше {MAX_STEPS} шагов — похоже на бесконечный цикл.",
                            "Проверьте связи, ведущие назад, и узел «Цикл по частям».")
        ex.current_node = node_id
        started = time.time()
        ex._pending_logs = []  # noqa: SLF001
        run = {"started": started, "status": "running", "inputs": [len(i) for i in inputs]}
        ex.run_data.setdefault(node_id, []).append(run)
        if len(ex.run_data[node_id]) > 30:
            ex.run_data[node_id].pop(0)
        try:
            outs = await _run_node(ex, node, inputs)
        except NodeError as exc:
            run.update(status="error", ms=int((time.time() - started) * 1000), error=exc.message, hint=exc.hint,
                       details=exc.details[:3000], logs=ex._pending_logs, input_sample=_store_items(inputs[0] if inputs else []))  # noqa: SLF001
            ex.error_node = node.get("name", "")
            raise
        except (asyncio.CancelledError, ExecutionCancelled):
            run.update(status="cancelled", ms=int((time.time() - started) * 1000))
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Сбой узла %s", node.get("name"))
            run.update(status="error", ms=int((time.time() - started) * 1000),
                       error=f"Внутренняя ошибка узла: {type(exc).__name__}: {exc}",
                       hint="Скачайте журнал выполнения и передайте разработчику.", logs=ex._pending_logs)  # noqa: SLF001
            ex.error_node = node.get("name", "")
            raise NodeError(run["error"], run["hint"]) from exc
        run.update(status="success", ms=int((time.time() - started) * 1000), logs=ex._pending_logs,  # noqa: SLF001
                   outputs=[_store_items(o) for o in outs])
        ex.last_outputs[node_id] = outs
        if outs and any(outs):
            ex.result = next((o for o in outs if o), [])
        if stop_at and node_id == stop_at:
            break
        for e in out_edges.get(node_id, []):
            out_idx = int(e.get("out", 0))
            items = outs[out_idx] if out_idx < len(outs) else []
            if not items:
                continue
            target = ex.nodes[e["to"]]
            ttype = BY_KEY.get(target.get("type", ""))
            n_in = ttype.inputs if ttype else 1
            in_idx = int(e.get("in", 0))
            if n_in <= 1:
                queue.append((e["to"], [items]))
            else:
                slot = pending.setdefault(e["to"], {})
                slot.setdefault(in_idx, []).extend(items)
                if connected_inputs.get(e["to"], set()) <= set(slot):
                    del pending[e["to"]]
                    queue.append((e["to"], [slot.get(i, []) for i in range(n_in)]))


def pick_trigger(wf: dict, node_id: str | None = None) -> dict | None:
    nodes = wf.get("nodes", [])
    if node_id:
        n = next((x for x in nodes if x["id"] == node_id), None)
        if n and BY_KEY.get(n.get("type", "")) and BY_KEY[n["type"]].trigger:
            return n
    for preferred in ("manual", "webhook", "schedule", "subworkflow_trigger", "error_trigger"):
        n = next((x for x in nodes if x.get("type") == preferred and not x.get("disabled")), None)
        if n:
            return n
    return None


async def run_workflow(wf: dict, mode: str, trigger: dict | None = None, items: list[Item] | None = None,
                       stop_at: str | None = None, depth: int = 0, save: bool = True) -> Execution:
    ex = Execution(wf, mode, depth)
    trigger = trigger or pick_trigger(wf)
    if save:
        ex.id = storage.create_execution(wf, mode)
        RUNNING[ex.id] = ex
    timeout = float((wf.get("settings") or {}).get("timeout_minutes") or 30) * 60
    try:
        if trigger is None:
            raise NodeError("В сценарии нет узла запуска.",
                            "Добавьте узел из группы «Запуск»: «Запуск вручную», «Расписание» или «Вебхук».")
        start_items = items if items is not None else trigger_items(trigger)
        await asyncio.wait_for(execute(ex, trigger["id"], start_items, stop_at), timeout=timeout)
        ex.status = "success"
    except NodeError as exc:
        ex.status, ex.error, ex.error_hint = "error", exc.message, exc.hint
    except asyncio.TimeoutError:
        ex.status, ex.error = "error", f"Сценарий выполнялся дольше {int(timeout // 60)} минут и был остановлен."
        ex.error_hint = "Увеличьте ограничение в настройках сценария или ускорьте медленные узлы."
    except (asyncio.CancelledError, ExecutionCancelled):
        ex.status, ex.error = "cancelled", "Остановлено вручную"
    except Exception as exc:  # noqa: BLE001
        log.exception("Сбой выполнения сценария")
        ex.status, ex.error = "error", f"Внутренняя ошибка: {type(exc).__name__}: {exc}"
    finally:
        ex.finished = time.time()
        ex.current_node = ""
        if save and ex.id:
            keep = ex.status != "success" or (wf.get("settings") or {}).get("save_success", True) or mode == "manual"
            storage.finish_execution(ex.id, ex.status, ex.error, ex.error_node, ex.snapshot(), keep)
            RUNNING.pop(ex.id, None)
        ex.done.set()
    if ex.status == "error" and mode not in ("error", "manual"):
        _log_failure(ex)
        _start_error_workflow(ex)
    return ex


def _log_failure(ex: Execution) -> None:
    store.log_event("error", "workflows", f"Сценарий «{ex.workflow.get('name')}» завершился ошибкой"
                    + (f" в узле «{ex.error_node}»" if ex.error_node else ""), ex.error)


def _start_error_workflow(ex: Execution) -> None:
    target = (ex.workflow.get("settings") or {}).get("error_workflow_id")
    if not target:
        return
    wf = storage.get_workflow(int(target))
    if not wf or wf["id"] == ex.workflow.get("id"):
        return
    trig = next((n for n in wf["nodes"] if n.get("type") == "error_trigger" and not n.get("disabled")), None)
    if trig is None:
        store.log_event("warning", "workflows", f"Сценарий для ошибок «{wf['name']}» не начинается с узла "
                                                "«При ошибке в сценарии»")
        return
    data = item({"сценарий": {"id": ex.workflow.get("id"), "name": ex.workflow.get("name")},
                 "выполнение": {"id": ex.id, "режим": ex.mode, "адрес": f"/executions/{ex.id}"},
                 "ошибка": {"сообщение": ex.error, "узел": ex.error_node, "совет": ex.error_hint}})
    start_background(wf, "error", trig, [data])


def start_background(wf: dict, mode: str, trigger: dict | None = None, items: list[Item] | None = None,
                     stop_at: str | None = None) -> asyncio.Task:
    return asyncio.get_event_loop().create_task(run_workflow(wf, mode, trigger, items, stop_at))


async def start_and_get_id(wf: dict, mode: str, trigger: dict | None = None, items: list[Item] | None = None,
                           stop_at: str | None = None) -> int:
    """Запускает выполнение в фоне и сразу возвращает его номер (для редактора)."""
    task = start_background(wf, mode, trigger, items, stop_at)
    for _ in range(200):
        for ex in list(RUNNING.values()):
            if ex.workflow is wf:
                ex.task = task
                return ex.id
        if task.done():
            return task.result().id
        await asyncio.sleep(0.01)
    raise RuntimeError("Выполнение не запустилось")


async def run_subworkflow(ctx: NodeContext, wf_id: int, items: list[Item]) -> list[Item]:
    parent = ctx.execution
    if parent.depth + 1 > MAX_DEPTH:
        raise NodeError("Слишком глубокая вложенность вызовов сценариев (больше 5).",
                        "Похоже, сценарии вызывают друг друга по кругу.")
    wf = storage.get_workflow(wf_id)
    if wf is None:
        raise NodeError("Вызываемый сценарий удалён.", "Выберите другой сценарий в узле.")
    trig = next((n for n in wf["nodes"] if n.get("type") == "subworkflow_trigger" and not n.get("disabled")), None)
    if trig is None:
        raise NodeError(f"Сценарий «{wf['name']}» нельзя вызвать: в нём нет узла «При вызове из другого сценария».")
    child = await run_workflow(wf, "subworkflow", trig, [item(copy.deepcopy(i["json"])) for i in items],
                               depth=parent.depth + 1)
    ctx.log(f"Выполнение вызванного сценария №{child.id}")
    if child.status != "success":
        raise NodeError(f"Вызванный сценарий «{wf['name']}» завершился ошибкой: {child.error}",
                        f"Откройте выполнение №{child.id}.")
    return child.result


def cancel(ex_id: int) -> bool:
    ex = RUNNING.get(ex_id)
    if not ex:
        return False
    ex.cancelled = True
    if ex.task and not ex.task.done():
        ex.task.cancel()
    return True
