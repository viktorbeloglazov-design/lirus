"""Выражения в параметрах узлов: {{ $json.поле }}, как в n8n.

Внутри скобок — безопасное подмножество Python. Самые частые конструкции из
JavaScript (===, &&, ||, !, true/false/null, .length, .toUpperCase() и т.п.)
понимаются тоже, чтобы выражения из n8n работали без переписывания.

Безопасность: выражение разбирается в дерево и проверяется по белому списку.
Нельзя обращаться к атрибутам, начинающимся с «_», импортировать модули,
открывать файлы и вызывать что-либо, кроме разрешённых функций и методов.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import math
import re
import uuid as uuidlib
from datetime import date, datetime, timedelta
from typing import Any

EXPR_RE = re.compile(r"\{\{(.*?)\}\}", re.S)


class ExpressionError(Exception):
    pass


# ------------------------------------------------------ «удобные» типы значений

class JSStr(str):
    """Строка с методами из JavaScript и n8n."""

    @property
    def length(self):
        return len(self)

    def toUpperCase(self):
        return JSStr(self.upper())

    def toLowerCase(self):
        return JSStr(self.lower())

    def trim(self):
        return JSStr(self.strip())

    def includes(self, sub):
        return str(sub) in self

    def startsWith(self, s):
        return self.startswith(str(s))

    def endsWith(self, s):
        return self.endswith(str(s))

    def replaceAll(self, a, b):
        return JSStr(self.replace(str(a), str(b)))

    def slice(self, start=0, end=None):
        return JSStr(self[start:end])

    def substring(self, start=0, end=None):
        return JSStr(self[start:end])

    def indexOf(self, s):
        return self.find(str(s))

    def padStart(self, n, ch=" "):
        return JSStr(self.rjust(int(n), str(ch)[:1] or " "))

    def toNumber(self):
        return to_number(self)

    def toString(self):
        return JSStr(self)

    def isEmpty(self):
        return len(self.strip()) == 0

    def isNotEmpty(self):
        return not self.isEmpty()

    def toDate(self):
        return parse_date(self)

    def extractEmail(self):
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", self)
        return JSStr(m.group(0)) if m else None


class JSList(list):
    @property
    def length(self):
        return len(self)

    def includes(self, v):
        return v in self

    def join(self, sep=","):  # noqa: A003 — как в JavaScript
        return JSStr(str(sep).join("" if x is None else str(x) for x in self))

    def first(self):
        return wrap(self[0]) if self else None

    def last(self):
        return wrap(self[-1]) if self else None

    def isEmpty(self):
        return len(self) == 0

    def isNotEmpty(self):
        return len(self) > 0

    def sum(self):
        return sum(to_number(x) or 0 for x in self)

    def unique(self):
        out = []
        for x in self:
            if x not in out:
                out.append(x)
        return JSList(out)

    def pluck(self, key):
        return JSList(wrap(x.get(key)) if isinstance(x, dict) else None for x in self)

    def indexOf(self, v):
        return self.index(v) if v in self else -1

    def toString(self):
        return JSStr(json.dumps(unwrap(self), ensure_ascii=False))

    def __getitem__(self, i):
        try:
            return wrap(list.__getitem__(self, i))
        except IndexError:
            return None


class JSDict(dict):
    """Словарь с доступом через точку: $json.клиент.телефон. Нет поля — None, а не ошибка."""

    def __getattribute__(self, name):
        # Поле данных важнее одноимённого метода словаря: $json.items — это поле «items»
        if not name.startswith("_") and dict.__contains__(self, name):
            return wrap(dict.get(self, name))
        return object.__getattribute__(self, name)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return None

    def __getitem__(self, key):
        return wrap(self.get(key))

    def keys(self):
        return JSList(dict.keys(self))

    def values(self):
        return JSList(wrap(v) for v in dict.values(self))

    def isEmpty(self):
        return len(self) == 0

    def isNotEmpty(self):
        return len(self) > 0

    def hasField(self, name):
        return name in self

    def toString(self):
        return JSStr(json.dumps(unwrap(self), ensure_ascii=False))


class JSDate(datetime):
    """Дата и время с методами как в n8n (Luxon): $now.toFormat('dd.MM.yyyy'), $now.minus(1, 'days')."""

    _UNITS = {"days": "days", "day": "days", "hours": "hours", "hour": "hours", "minutes": "minutes",
              "minute": "minutes", "weeks": "weeks", "week": "weeks", "seconds": "seconds"}

    @classmethod
    def of(cls, d: datetime) -> "JSDate":
        return cls(d.year, d.month, d.day, d.hour, d.minute, d.second, d.microsecond, d.tzinfo)

    def toFormat(self, fmt="dd.MM.yyyy"):
        return date_format(self, fmt)

    def toISO(self):
        return JSStr(self.isoformat())

    def plus(self, n=1, unit="days"):
        if unit in ("months", "month"):
            return JSDate.of(date_add(self, months=int(n)))
        return JSDate.of(self + timedelta(**{self._UNITS.get(unit, "days"): n}))

    def minus(self, n=1, unit="days"):
        return self.plus(-n, unit)

    def startOf(self, unit="day"):
        if unit == "month":
            return JSDate.of(self.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
        if unit == "hour":
            return JSDate.of(self.replace(minute=0, second=0, microsecond=0))
        return JSDate.of(self.replace(hour=0, minute=0, second=0, microsecond=0))

    @property
    def weekday_ru(self):
        return ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][self.weekday()]

    def toString(self):
        return JSStr(self.isoformat())


def wrap(v: Any) -> Any:
    if isinstance(v, (JSStr, JSList, JSDict, JSDate)):
        return v
    if isinstance(v, datetime):
        return JSDate.of(v)
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, str):
        return JSStr(v)
    if isinstance(v, dict):
        return JSDict(v)
    if isinstance(v, (list, tuple)):
        return JSList(v)
    return v


def unwrap(v: Any) -> Any:
    """Обратно в обычные типы — чтобы сохранять в JSON."""
    if isinstance(v, JSStr):
        return str(v)
    if isinstance(v, dict):
        return {str(k): unwrap(x) for k, x in dict.items(v)}
    if isinstance(v, (list, tuple)):
        return [unwrap(x) for x in list.__iter__(v)] if isinstance(v, list) else [unwrap(x) for x in v]
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


# ------------------------------------------------------------ функции-помощники

def to_number(v: Any) -> float | int | None:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if v is None:
        return None
    s = str(v).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() and "." not in s else f


def parse_date(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v)
    if not v:
        return None
    s = str(v).strip().replace("Z", "+00:00")
    for fmt in (None, "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


_FMT_MAP = [("yyyy", "%Y"), ("yy", "%y"), ("MM", "%m"), ("dd", "%d"), ("HH", "%H"), ("mm", "%M"), ("ss", "%S")]


def date_format(v: Any, fmt: str = "dd.MM.yyyy") -> str | None:
    """Формат как в n8n/Luxon (dd.MM.yyyy HH:mm) или как в Python (%d.%m.%Y)."""
    d = parse_date(v)
    if d is None:
        return None
    if "%" not in fmt:
        for a, b in _FMT_MAP:
            fmt = fmt.replace(a, b)
    return JSStr(d.strftime(fmt))


def date_add(v: Any, days: float = 0, hours: float = 0, minutes: float = 0, months: int = 0) -> datetime | None:
    d = parse_date(v)
    if d is None:
        return None
    if months:
        m = d.month - 1 + int(months)
        y = d.year + m // 12
        m = m % 12 + 1
        import calendar

        d = d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))
    return d + timedelta(days=days, hours=hours, minutes=minutes)


def days_between(a: Any, b: Any) -> float | None:
    da, dbb = parse_date(a), parse_date(b)
    if da is None or dbb is None:
        return None
    if (da.tzinfo is None) != (dbb.tzinfo is None):
        da, dbb = da.replace(tzinfo=None), dbb.replace(tzinfo=None)
    return (dbb - da).total_seconds() / 86400


def _safe_pow(a, b):
    if abs(b) > 100:
        raise ExpressionError("Слишком большая степень")
    return a ** b


FUNCTIONS: dict[str, Any] = {
    "len": len, "str": str, "int": int, "float": float, "bool": bool, "round": round, "abs": abs,
    "min": min, "max": max, "sum": sum, "sorted": sorted, "list": list, "dict": dict, "any": any, "all": all,
    "isinstance": None,  # запрещено: заменяется ниже
    "number": to_number, "to_number": to_number,
    "json_dumps": lambda v: json.dumps(unwrap(v), ensure_ascii=False),
    "json_loads": json.loads,
    "parse_date": parse_date, "date_format": date_format, "date_add": date_add, "days_between": days_between,
    "uuid": lambda: str(uuidlib.uuid4()),
    "md5": lambda s: hashlib.md5(str(s).encode()).hexdigest(),
    "sha256": lambda s: hashlib.sha256(str(s).encode()).hexdigest(),
    "base64_encode": lambda s: base64.b64encode(str(s).encode()).decode(),
    "base64_decode": lambda s: base64.b64decode(str(s)).decode("utf-8", "replace"),
    "floor": math.floor, "ceil": math.ceil,
    "String": lambda v="": JSStr("" if v is None else v),
    "Number": to_number,
    "Math": None,  # заполняется ниже
    "JSON": None,
}
FUNCTIONS.pop("isinstance")


class _Math:
    floor = staticmethod(math.floor)
    ceil = staticmethod(math.ceil)
    round = staticmethod(lambda x: int(math.floor(x + 0.5)))
    abs = staticmethod(abs)
    max = staticmethod(max)
    min = staticmethod(min)
    sqrt = staticmethod(math.sqrt)
    PI = math.pi

    @staticmethod
    def random():
        import random

        return random.random()


class _JSON:
    stringify = staticmethod(lambda v, *a: JSStr(json.dumps(unwrap(v), ensure_ascii=False)))
    parse = staticmethod(lambda s: wrap(json.loads(s)))


FUNCTIONS["Math"] = _Math
FUNCTIONS["JSON"] = _JSON


# --------------------------------------------------------- перевод из JavaScript

_STRING_RE = re.compile(r"('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`(?:[^`\\]|\\.)*`)")


def _translate_code(code: str) -> str:
    code = code.replace("===", "==").replace("!==", "!=")
    code = code.replace("&&", " and ").replace("||", " or ")
    code = re.sub(r"!(?!=)", " not ", code)
    code = re.sub(r"\btrue\b", "True", code)
    code = re.sub(r"\bfalse\b", "False", code)
    code = re.sub(r"\b(null|undefined)\b", "None", code)
    code = re.sub(r"\$\(", "_ref(", code)
    code = re.sub(r"\$(\w+)", r"_\1", code)
    code = re.sub(r"\?\.", ".", code)  # опциональная цепочка: у нас и так None вместо ошибки
    code = re.sub(r"\s*\?\?\s*", " or ", code)
    return code


def translate(expr: str) -> str:
    """Переводит JS-синтаксис в Python вне строковых литералов."""
    parts = _STRING_RE.split(expr)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            if part.startswith("`"):
                inner = part[1:-1].replace('"', '\\"')
                inner = re.sub(r"\$\{(.*?)\}", lambda m: "{" + _translate_code(m.group(1)) + "}", inner)
                out.append('f"' + inner + '"')
            else:
                out.append(part)
        else:
            out.append(_translate_code(part))
    return "".join(out).strip()


# -------------------------------------------------------- проверка и вычисление

_ALLOWED = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.IfExp, ast.Call, ast.Attribute,
    ast.Subscript, ast.Slice, ast.Name, ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.JoinedStr, ast.FormattedValue, ast.keyword, ast.Load, ast.ListComp, ast.GeneratorExp,
    ast.DictComp, ast.comprehension, ast.Store, ast.Lambda, ast.arguments, ast.arg,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod,
    ast.FloorDiv, ast.Pow, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Is, ast.IsNot,
)

_cache: dict[str, Any] = {}
# Атрибуты, через которые из Python можно добраться до внутренностей интерпретатора
_DENY_ATTR = re.compile(r"^((gi|f|co|cr|ag|tb)_\w*|format|format_map|mro|func_\w*|im_\w*)$")


def _check(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ExpressionError(f"В выражении нельзя использовать «{type(node).__name__}»")
        if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or _DENY_ATTR.match(node.attr)):
            raise ExpressionError(f"Нельзя обращаться к служебному полю «{node.attr}»")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ExpressionError("Недопустимое имя в выражении")


class _PowGuard(ast.NodeTransformer):
    def visit_BinOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Pow):
            return ast.copy_location(ast.Call(func=ast.Name("_pow", ast.Load()), args=[node.left, node.right],
                                              keywords=[]), node)
        return node


def compile_expr(code: str):
    if code in _cache:
        return _cache[code]
    py = translate(code)
    try:
        tree = ast.parse(py, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"Выражение написано с ошибкой: {{{{ {code.strip()} }}}}") from exc
    _check(tree)
    tree = ast.fix_missing_locations(_PowGuard().visit(tree))
    compiled = compile(tree, "<выражение>", "eval")
    if len(_cache) > 2000:
        _cache.clear()
    _cache[code] = compiled
    return compiled


def evaluate(code: str, namespace: dict) -> Any:
    compiled = compile_expr(code)
    env = {"__builtins__": {}, "_pow": _safe_pow, **FUNCTIONS, **namespace}
    try:
        return eval(compiled, env)  # noqa: S307 — дерево проверено по белому списку
    except ExpressionError:
        raise
    except ZeroDivisionError as exc:
        raise ExpressionError(f"Деление на ноль в выражении {{{{ {code.strip()} }}}}") from exc
    except NameError as exc:
        name = str(exc).split("'")[1] if "'" in str(exc) else str(exc)
        raise ExpressionError(f"В выражении {{{{ {code.strip()} }}}} неизвестное имя «{name.lstrip('_')}». "
                              "Доступно: $json, $input, $node, $('Имя узла'), $now, $today, $execution, $workflow") from exc
    except Exception as exc:  # noqa: BLE001
        raise ExpressionError(f"Ошибка в выражении {{{{ {code.strip()} }}}}: {exc}") from exc


def has_expression(value: Any) -> bool:
    return isinstance(value, str) and "{{" in value and "}}" in value


def _to_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(unwrap(v), ensure_ascii=False)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def render(value: Any, namespace: dict) -> Any:
    """Подставляет выражения. Если вся строка — одно выражение, возвращает значение как есть (число, список…)."""
    if isinstance(value, dict):
        return {k: render(v, namespace) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, namespace) for v in value]
    if not has_expression(value):
        return value
    stripped = value.strip()
    found = list(EXPR_RE.finditer(stripped))
    if len(found) == 1 and found[0].start() == 0 and found[0].end() == len(stripped):
        # вся строка — одно выражение: возвращаем значение как есть (число, список, объект)
        return unwrap(evaluate(found[0].group(1), namespace))
    return EXPR_RE.sub(lambda m: _to_text(unwrap(evaluate(m.group(1), namespace))), value)
