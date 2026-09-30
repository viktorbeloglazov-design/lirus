"""Расписание: разбор cron-выражений (минута час день месяц день_недели) и человекочитаемое описание."""
from __future__ import annotations

from datetime import datetime, timedelta

FIELDS = [("минута", 0, 59), ("час", 0, 23), ("день месяца", 1, 31), ("месяц", 1, 12), ("день недели", 0, 6)]
DAYS = ["воскресенье", "понедельник", "вторник", "среду", "четверг", "пятницу", "субботу"]


class CronError(ValueError):
    pass


def _parse_field(text: str, lo: int, hi: int, title: str) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"Неверный шаг в поле «{title}»: {step_s}")
            step = int(step_s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            if not (a_s.isdigit() and b_s.isdigit()):
                raise CronError(f"Неверный диапазон в поле «{title}»: {part}")
            a, b = int(a_s), int(b_s)
        elif part.isdigit():
            a = b = int(part)
            if step > 1:
                b = hi
        else:
            raise CronError(f"Непонятное значение в поле «{title}»: {part}")
        if title == "день недели":
            a, b = (0 if a == 7 else a), (0 if b == 7 and a != 0 else b)
        if a < lo or b > hi or a > b:
            raise CronError(f"Поле «{title}» должно быть от {lo} до {hi}, а указано {part}")
        values.update(range(a, b + 1, step))
    return values


class Cron:
    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise CronError("В расписании cron должно быть 5 частей: минута час день месяц день_недели. "
                            "Например «0 9 * * 1-5» — в 9:00 по будням.")
        self.expr = expr
        self.sets = [_parse_field(p, lo, hi, title) for p, (title, lo, hi) in zip(parts, FIELDS)]
        self.dom_any = parts[2] == "*"
        self.dow_any = parts[4] == "*"

    def matches(self, dt: datetime) -> bool:
        minute, hour, dom, month, dow = self.sets
        if dt.minute not in minute or dt.hour not in hour or dt.month not in month:
            return False
        wd = (dt.weekday() + 1) % 7  # cron: 0 — воскресенье
        day_ok = dt.day in dom
        dow_ok = wd in dow
        if self.dom_any or self.dow_any:
            return day_ok and dow_ok
        return day_ok or dow_ok

    def next_after(self, dt: datetime, limit_days: int = 400) -> datetime | None:
        t = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = t + timedelta(days=limit_days)
        while t < end:
            if t.month not in self.sets[3]:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if self.matches(t):
                return t
            if t.hour not in self.sets[1]:
                t = t.replace(minute=0) + timedelta(hours=1)
                continue
            t += timedelta(minutes=1)
        return None


def schedule_to_cron(params: dict) -> str:
    """Параметры узла «Расписание» → cron-выражение."""
    mode = params.get("mode") or "interval"
    if mode == "cron":
        return (params.get("cron") or "").strip()
    if mode == "interval":
        n = max(1, int(float(params.get("minutes") or 15)))
        if n >= 60 and n % 60 == 0:
            h = n // 60
            return f"0 */{h} * * *" if h < 24 else "0 0 * * *"
        return f"*/{n} * * * *" if n < 60 else f"*/{n % 60 or 1} * * * *"
    hh, mm = _time(params.get("time") or "09:00")
    if mode == "hourly":
        return f"{mm} * * * *"
    if mode == "daily":
        return f"{mm} {hh} * * *"
    if mode == "weekdays":
        return f"{mm} {hh} * * 1-5"
    if mode == "weekly":
        return f"{mm} {hh} * * {int(params.get('weekday') or 1) % 7}"
    if mode == "monthly":
        return f"{mm} {hh} {int(params.get('monthday') or 1)} * *"
    raise CronError("Неизвестный режим расписания")


def _time(text: str) -> tuple[int, int]:
    try:
        hh, mm = str(text).replace(".", ":").split(":")[:2]
        hh_i, mm_i = int(hh), int(mm)
        if 0 <= hh_i < 24 and 0 <= mm_i < 60:
            return hh_i, mm_i
    except ValueError:
        pass
    raise CronError(f"Время «{text}» указано неверно. Пишите как 09:30.")


def describe(expr: str) -> str:
    try:
        c = Cron(expr)
    except CronError as exc:
        return str(exc)
    nxt = c.next_after(datetime.now())
    return f"Следующий запуск: {nxt:%d.%m.%Y %H:%M}" if nxt else "Запусков в ближайший год не будет"
