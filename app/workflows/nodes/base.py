"""Основа узлов сценария: описание параметров, контекст выполнения, ошибки."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..expr import ExpressionError, JSDate, JSDict, JSList, render, wrap

if TYPE_CHECKING:
    from ..engine import Execution

Item = dict  # {"json": {...}}


class NodeError(Exception):
    """Ошибка узла: понятный текст и, если есть, совет."""

    def __init__(self, message: str, hint: str = "", details: str = ""):
        super().__init__(message)
        self.message, self.hint, self.details = message, hint, details


@dataclass
class Param:
    name: str
    label: str
    kind: str = "string"  # string | text | number | boolean | select | json | code | connection |
    #                        workflow | list | notice
    default: Any = None
    hint: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)
    required: bool = False
    placeholder: str = ""
    show_if: dict[str, list] | None = None  # показывать, если другой параметр имеет одно из значений
    conn_types: list[str] = field(default_factory=list)  # для kind="connection"
    no_expr: bool = False  # значение не вычисляется как выражение (например, код)
    columns: list[dict] = field(default_factory=list)  # для kind="list": [{name, label, kind, options}]

    def to_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "kind": self.kind, "default": self.default,
                "hint": self.hint, "options": [list(o) for o in self.options], "required": self.required,
                "placeholder": self.placeholder, "show_if": self.show_if, "conn_types": self.conn_types,
                "columns": self.columns}


@dataclass
class NodeType:
    key: str
    title: str
    group: str
    description: str
    execute: Callable[["NodeContext"], Awaitable[list[list[Item]]]]
    params: list[Param] = field(default_factory=list)
    inputs: int = 1
    outputs: list[str] = field(default_factory=lambda: [""])
    trigger: bool = False
    icon: str = "•"
    color: str = "#64748b"
    n8n_types: list[str] = field(default_factory=list)  # соответствие типам n8n для импорта
    dynamic_outputs: Callable[[dict], list[str]] | None = None  # число выходов зависит от параметров

    def outputs_for(self, params: dict) -> list[str]:
        return self.dynamic_outputs(params) if self.dynamic_outputs else self.outputs

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "group": self.group, "description": self.description,
                "params": [p.to_dict() for p in self.params], "inputs": self.inputs, "outputs": self.outputs,
                "trigger": self.trigger, "icon": self.icon, "color": self.color,
                "dynamic_outputs": self.dynamic_outputs is not None}

    def defaults(self) -> dict:
        return {p.name: copy.deepcopy(p.default) for p in self.params if p.kind != "notice"}


def item(data: Any) -> Item:
    if isinstance(data, dict) and set(data.keys()) <= {"json", "binary", "pairedItem"} and "json" in data:
        return {"json": data["json"] if isinstance(data["json"], dict) else {"value": data["json"]}}
    if isinstance(data, dict):
        return {"json": data}
    return {"json": {"value": data}}


def items_json(items: list[Item]) -> list[dict]:
    return [i.get("json", {}) for i in items]


# ----------------------------------------------------------- ссылки на данные

class NodeRef:
    """$('Имя узла') и $node['Имя узла'] в выражениях."""

    def __init__(self, execution: "Execution", name: str, index: int):
        self._ex, self._name, self._index = execution, name, index

    def _items(self) -> list[Item]:
        out = self._ex.output_by_name(self._name)
        if out is None:
            raise ExpressionError(f"Узел «{self._name}» ещё не выполнялся в этом запуске (или такого узла нет)")
        return out

    def all(self):  # noqa: A003
        return JSList(wrap({"json": i["json"]}) for i in self._items())

    def first(self):
        items = self._items()
        return wrap({"json": items[0]["json"]}) if items else None

    def last(self):
        items = self._items()
        return wrap({"json": items[-1]["json"]}) if items else None

    @property
    def item(self):
        items = self._items()
        if not items:
            return None
        i = self._index if self._index < len(items) else 0
        return wrap({"json": items[i]["json"]})

    @property
    def json(self):
        it = self.item
        return it.json if it is not None else JSDict()

    def isExecuted(self):
        return self._ex.output_by_name(self._name) is not None


class NodeMap:
    def __init__(self, execution: "Execution", index: int):
        self._ex, self._index = execution, index

    def __getitem__(self, name):
        return NodeRef(self._ex, str(name), self._index)


class InputProxy:
    def __init__(self, items: list[Item], index: int):
        self._items, self._index = items, index

    def all(self):  # noqa: A003
        return JSList(wrap({"json": i["json"]}) for i in self._items)

    def first(self):
        return wrap({"json": self._items[0]["json"]}) if self._items else None

    def last(self):
        return wrap({"json": self._items[-1]["json"]}) if self._items else None

    @property
    def item(self):
        if not self._items:
            return None
        return wrap({"json": self._items[min(self._index, len(self._items) - 1)]["json"]})

    @property
    def length(self):
        return len(self._items)


# ------------------------------------------------------------------ контекст

class NodeContext:
    def __init__(self, execution: "Execution", node: dict, ntype: NodeType, inputs: list[list[Item]]):
        self.execution = execution
        self.node = node
        self.type = ntype
        self.inputs = inputs
        self.params_raw = {**ntype.defaults(), **(node.get("params") or {})}
        self.logs: list[str] = []

    @property
    def items(self) -> list[Item]:
        return self.inputs[0] if self.inputs else []

    @property
    def name(self) -> str:
        return self.node.get("name", "")

    def namespace(self, index: int = 0, items: list[Item] | None = None) -> dict:
        items = self.items if items is None else items
        cur = items[index]["json"] if 0 <= index < len(items) else {}
        now = datetime.now()
        ex = self.execution
        return {
            "_json": wrap(cur),
            "_input": InputProxy(items, index),
            "_node": NodeMap(ex, index),
            "_ref": lambda name: NodeRef(ex, str(name), index),
            "_now": JSDate.of(now),
            "_today": JSDate.of(datetime.combine(date.today(), datetime.min.time())),
            "_execution": wrap({"id": ex.id, "mode": ex.mode}),
            "_workflow": wrap({"id": ex.workflow.get("id"), "name": ex.workflow.get("name", "")}),
            "_itemIndex": index,
            "_runIndex": max(0, ex.run_count(self.node["id"]) - 1),
            "_vars": wrap(ex.variables),
        }

    def raw(self, name: str, default: Any = None) -> Any:
        return self.params_raw.get(name, default)

    def param(self, name: str, index: int = 0, default: Any = None) -> Any:
        value = self.params_raw.get(name, default)
        spec = next((p for p in self.type.params if p.name == name), None)
        if spec is not None and spec.no_expr:
            return value
        try:
            return render(value, self.namespace(index))
        except ExpressionError as exc:
            label = spec.label if spec else name
            raise NodeError(f"Параметр «{label}»: {exc}",
                            "Проверьте выражение в фигурных скобках. Пример: {{ $json.имя_поля }}") from exc

    def param_str(self, name: str, index: int = 0, default: str = "") -> str:
        v = self.param(name, index, default)
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            import json

            return json.dumps(v, ensure_ascii=False)
        return str(v)

    def param_num(self, name: str, index: int = 0, default: float = 0) -> float:
        v = self.param(name, index, default)
        from ..expr import to_number

        n = to_number(v)
        if n is None:
            spec = next((p for p in self.type.params if p.name == name), None)
            raise NodeError(f"Параметр «{spec.label if spec else name}» должен быть числом, а получено «{v}».")
        return n

    def param_bool(self, name: str, index: int = 0, default: bool = False) -> bool:
        v = self.param(name, index, default)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "да", "yes", "on")
        return bool(v)

    def param_json(self, name: str, index: int = 0, default: Any = None) -> Any:
        import json

        v = self.param(name, index, default)
        if isinstance(v, (dict, list)) or v is None:
            return v
        text = str(v).strip()
        if not text:
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            spec = next((p for p in self.type.params if p.name == name), None)
            raise NodeError(f"В поле «{spec.label if spec else name}» неверный JSON: {exc.msg} (строка {exc.lineno}, "
                            f"позиция {exc.colno}).", "Проверьте кавычки и запятые. Ключи и строки — в двойных кавычках.") from exc

    def log(self, text: str) -> None:
        self.logs.append(str(text)[:2000])

    def connection(self, param_name: str = "connection", index: int = 0):
        from ... import store
        from ...connectors import BY_KEY

        raw = self.param(param_name, index)
        spec = next((p for p in self.type.params if p.name == param_name), None)
        if not raw:
            conns = [c for c in store.list_connections() if not spec or not spec.conn_types or c.type in spec.conn_types]
            if len(conns) == 1:
                conn = conns[0]
            else:
                raise NodeError("В узле не выбрано подключение.",
                                "Откройте узел и выберите подключение в поле «Подключение».")
        else:
            try:
                conn = store.get_connection(int(raw))
            except (TypeError, ValueError):
                conn = None
            if conn is None:
                raise NodeError("Выбранное в узле подключение удалено.", "Выберите другое подключение в узле.")
        if spec and spec.conn_types and conn.type not in spec.conn_types:
            raise NodeError(f"Подключение «{conn.name}» не подходит для этого узла.")
        if not conn.enabled:
            raise NodeError(f"Подключение «{conn.name}» выключено.", "Включите его в разделе «Подключения».")
        values = conn.values()
        if conn.unreadable:
            raise NodeError(f"Пароли подключения «{conn.name}» не читаются ключом шифрования.",
                            "Откройте подключение и введите пароли заново.")
        ctype = BY_KEY.get(conn.type)
        return conn, values, ctype

    def check_context(self, ctype=None):
        from ...connectors import context_for
        from ...connectors.base import CheckContext

        if ctype is not None:
            return context_for(ctype)
        from ... import store

        return CheckContext(proxy=store.get_setting("proxy_url"))
