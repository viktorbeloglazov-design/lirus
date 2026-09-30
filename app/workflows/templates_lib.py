"""Готовые шаблоны сценариев под задачи компании."""
from __future__ import annotations

import copy


def _n(nid, ntype, name, x, y, params=None, **extra):
    return {"id": nid, "type": ntype, "name": name, "position": [x, y], "params": params or {}, **extra}


def _e(a, b, out=0, inp=0):
    return {"from": a, "out": out, "to": b, "in": inp}


TEMPLATES = {
    "learn": {
        "title": "Учебный: разбор заказов без внешних сервисов",
        "description": "Работает сразу, без подключений: берёт пример заказов, делит на крупные и мелкие, "
                       "считает итоги. Удобно, чтобы разобраться в редакторе.",
        "nodes": [
            _n("t", "manual", "Запуск вручную", 0, 160),
            _n("data", "code", "Пример заказов", 260, 160, {"mode": "all", "code":
                "return [\n    {'заказ': 101, 'клиент': 'ООО Ромашка', 'сумма': 125000, 'канал': 'Wildberries'},\n"
                "    {'заказ': 102, 'клиент': 'ИП Иванов', 'сумма': 8900, 'канал': 'Ozon'},\n"
                "    {'заказ': 103, 'клиент': 'ООО Бассейн-Строй', 'сумма': 1850000, 'канал': 'Опт'},\n"
                "    {'заказ': 104, 'клиент': 'Петров А.', 'сумма': 3200, 'канал': 'Wildberries'},\n]\n"}),
            _n("if", "if", "Крупный заказ?", 520, 160,
               {"conditions": [{"left": "{{ $json.сумма }}", "op": "gte", "right": "100000"}], "combine": "and"}),
            _n("big", "set", "Пометить: крупный", 780, 60,
               {"fields": [{"name": "категория", "value": "крупный", "type": "string"},
                           {"name": "задача", "value": "Позвонить клиенту {{ $json.клиент }}", "type": "string"}],
                "keep_other": True}),
            _n("small", "set", "Пометить: обычный", 780, 260,
               {"fields": [{"name": "категория", "value": "обычный", "type": "string"}], "keep_other": True}),
            _n("merge", "merge", "Собрать вместе", 1040, 160, {"mode": "append"}),
            _n("sum", "summarize", "Итоги по каналам", 1300, 160,
               {"group_by": "канал", "aggregations": [{"op": "sum", "field": "сумма", "name": "выручка"},
                                                      {"op": "count", "field": "", "name": "заказов"}]}),
            _n("note", "note", "Заметка", 0, -60, {"text": "Нажмите «Выполнить» вверху, затем щёлкните по любому "
                                                          "узлу — справа будут видны его входные и выходные данные."}),
        ],
        "connections": [_e("t", "data"), _e("data", "if"), _e("if", "big", 0), _e("if", "small", 1),
                        _e("big", "merge", 0, 0), _e("small", "merge", 0, 1), _e("merge", "sum")],
    },
    "wb_stock": {
        "title": "Каждое утро: остатки Wildberries в Telegram",
        "description": "В 9:00 берёт остатки со складов WB, считает по складам и присылает сводку в Telegram.",
        "nodes": [
            _n("t", "schedule", "Каждый день в 9:00", 0, 100, {"mode": "daily", "time": "09:00"}),
            _n("wb", "wildberries", "Остатки WB", 260, 100,
               {"host": "statistics-api", "method": "GET", "path": "/api/v1/supplier/stocks",
                "query": [{"key": "dateFrom", "value": "2020-01-01"}]}),
            _n("sum", "summarize", "По складам", 520, 100,
               {"group_by": "warehouseName", "aggregations": [{"op": "sum", "field": "quantity", "name": "штук"}]}),
            _n("sort", "sort", "Больше — выше", 780, 100, {"field": "штук", "order": "desc"}),
            _n("text", "code", "Текст сводки", 1040, 100, {"mode": "all", "code":
                "lines = [f\"{i['warehouseName']}: {i['штук']} шт.\" for i in items]\n"
                "return [{'текст': 'Остатки WB на утро:\\n' + '\\n'.join(lines)}]\n"}),
            _n("tg", "telegram", "В Telegram", 1300, 100,
               {"operation": "send", "chat_id": "", "text": "{{ $json.текст }}"}),
        ],
        "connections": [_e("t", "wb"), _e("wb", "sum"), _e("sum", "sort"), _e("sort", "text"), _e("text", "tg")],
    },
    "order_to_b24": {
        "title": "Вебхук: новый заказ → сделка в Битрикс24",
        "description": "Сайт или сервис присылает заказ на адрес вебхука — платформа создаёт сделку и "
                       "сообщает менеджеру.",
        "nodes": [
            _n("t", "webhook", "Новый заказ", 0, 100,
               {"path": "novyi-zakaz", "method": "POST", "auth": "header", "auth_header": "X-Token",
                "auth_value": "", "response": "last_node",
                "test_data": '{\n  "body": {"клиент": "ООО Ромашка", "телефон": "+79001234567", "сумма": 150000}\n}'}),
            _n("b24", "bitrix24", "Сделка в Битрикс24", 260, 100,
               {"method": "crm.deal.add", "params": '{\n  "fields": {\n    "TITLE": "Заказ: {{ $json.body.клиент }}",\n'
                                                     '    "OPPORTUNITY": {{ $json.body.сумма }},\n    "CURRENCY_ID": "RUB"\n  }\n}'}),
            _n("tg", "telegram", "Сообщить менеджеру", 520, 100,
               {"operation": "send", "chat_id": "",
                "text": "Новый заказ от {{ $('Новый заказ').item.json.body.клиент }} на {{ $('Новый заказ').item.json.body.сумма }} ₽. Сделка №{{ $json.value }}"}),
        ],
        "connections": [_e("t", "b24"), _e("b24", "tg")],
    },
    "mail_claude": {
        "title": "Непрочитанные письма → черновик ответа от Claude",
        "description": "Каждые 15 минут берёт непрочитанные письма, Claude готовит ответ, менеджер получает "
                       "письмо и черновик в Telegram.",
        "nodes": [
            _n("t", "schedule", "Каждые 15 минут", 0, 100, {"mode": "interval", "minutes": 15}),
            _n("mail", "gmail", "Непрочитанные письма", 260, 100,
               {"operation": "search", "query": "is:unread newer_than:1d", "limit": 5}),
            _n("ai", "claude", "Черновик ответа", 520, 100,
               {"prompt": "Письмо от {{ $json.от }}, тема «{{ $json.тема }}»:\n\n{{ $json.текст }}\n\n"
                          "Составь вежливый короткий ответ от имени компании.", "output": "черновик"}),
            _n("tg", "telegram", "Менеджеру", 780, 100,
               {"operation": "send", "chat_id": "",
                "text": "Письмо: {{ $json.тема }} ({{ $json.от }})\n\nЧерновик ответа:\n{{ $json.черновик }}"}),
            _n("read", "gmail", "Отметить прочитанным", 1040, 100,
               {"operation": "mark_read", "message_id": "{{ $('Непрочитанные письма').item.json.id }}"}),
        ],
        "connections": [_e("t", "mail"), _e("mail", "ai"), _e("ai", "tg"), _e("tg", "read")],
    },
    "errors": {
        "title": "Уведомление об ошибках сценариев",
        "description": "Выберите этот сценарий как «сценарий для ошибок» в настройках других — "
                       "и получайте сообщение, когда что-то сломалось.",
        "nodes": [
            _n("t", "error_trigger", "При ошибке", 0, 100),
            _n("tg", "telegram", "Сообщить админу", 260, 100,
               {"operation": "send", "chat_id": "",
                "text": "⚠ Сценарий «{{ $json.сценарий.name }}» остановился с ошибкой в узле "
                        "«{{ $json.ошибка.узел }}»:\n{{ $json.ошибка.сообщение }}"}),
        ],
        "connections": [_e("t", "tg")],
    },
}


def build(key: str) -> tuple[str, list, list] | None:
    t = TEMPLATES.get(key)
    if not t:
        return None
    from .nodes import BY_KEY

    nodes = copy.deepcopy(t["nodes"])
    for n in nodes:
        nt = BY_KEY[n["type"]]
        n["params"] = {**nt.defaults(), **n["params"]}
    return t["title"], nodes, copy.deepcopy(t["connections"])
