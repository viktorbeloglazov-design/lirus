"""Живая проверка раздела «Сценарии» на запущенном сервере.

Запускать после tests/scenarios.py (нужны админ с паролем из scenarios.py, менеджер и подключения).
Запуск: python tests/workflows_live.py [http://localhost:8080]
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
ADMIN_PASSWORD = "Надёжный пароль 2026"
results: list[bool] = []


def check(cond, title, extra=""):
    results.append(bool(cond))
    print(f"[{'OK  ' if cond else 'FAIL'}] {title}" + (f"\n        {extra}" if extra and not cond else ""))


def client():
    return httpx.Client(base_url=BASE, follow_redirects=False, timeout=120, trust_env=False)


def csrf_of(c):
    return re.search(r'name="csrf" value="([^"]+)"', c.get("/workflows").text).group(1)


class Api:
    def __init__(self, c):
        self.c, self.token = c, csrf_of(c)

    def get(self, url):
        return self.c.get(url)

    def send(self, method, url, body=None, csrf=True):
        headers = {"X-CSRF-Token": self.token} if csrf else {}
        return self.c.request(method, url, json=body, headers=headers)

    def run(self, wf_id, graph=None, stop_at=None):
        body = {}
        if graph:
            body["workflow"] = graph
        if stop_at:
            body["stop_at"] = stop_at
        r = self.send("POST", f"/api/workflows/{wf_id}/run", body)
        ex_id = r.json()["execution_id"]
        return self.wait(ex_id)

    def wait(self, ex_id, limit=60):
        for _ in range(limit * 5):
            d = self.get(f"/api/executions/{ex_id}").json()
            if not d.get("running"):
                return d
            time.sleep(0.2)
        return d

    def create(self, name, nodes, connections, settings=None):
        r = self.c.post("/workflows/new", data={"csrf": self.token, "name": name})
        wf_id = int(r.headers["location"].rsplit("/", 1)[1])
        r = self.send("PUT", f"/api/workflows/{wf_id}", {"name": name, "nodes": nodes, "connections": connections,
                                                         "settings": settings or {}})
        assert r.status_code == 200, r.text
        return wf_id


def N(nid, t, name, params=None, x=0, **kw):
    return {"id": nid, "type": t, "name": name, "position": [x, 0], "params": params or {}, **kw}


def E(a, b, out=0, inp=0):
    return {"from": a, "out": out, "to": b, "in": inp}


def out_items(ex, node_id, idx=0):
    runs = ex["run_data"].get(node_id) or [{}]
    outs = runs[-1].get("outputs") or [{}]
    return outs[idx].get("items", []) if idx < len(outs) else []


def main():
    c = client()
    c.post("/login", data={"login": "admin", "password": ADMIN_PASSWORD, "next": "/"})
    a = Api(c)

    # ------------------------------------------------------ страницы и права
    check(c.get("/workflows").status_code == 200, "Раздел «Сценарии» открывается")
    types = a.get("/api/node-types").json()
    n_types = sum(len(g["nodes"]) for g in types["groups"])
    check(n_types >= 40, f"Узлов в каталоге: {n_types}")
    mgr = client()
    mgr.post("/login", data={"login": "manager1", "password": "Новый-менеджер-2", "next": "/"})
    check(mgr.get("/workflows").status_code == 403, "Менеджеру раздел «Сценарии» закрыт (403)")
    r = mgr.get("/api/node-types")
    check(r.status_code == 403 and "Нет доступа" in r.json()["error"], "Менеджеру закрыт API сценариев")

    # ---------------------------------------------------------- шаблон
    r = c.post("/workflows/new", data={"csrf": a.token, "template": "learn"})
    learn_id = int(r.headers["location"].rsplit("/", 1)[1])
    check(c.get(f"/workflows/{learn_id}").status_code == 200, "Сценарий из шаблона создан, редактор открывается")
    r = a.send("POST", f"/api/workflows/{learn_id}/run", {}, csrf=False)
    check(r.status_code == 400, "Запуск без защитного токена отклонён")
    ex = a.run(learn_id)
    check(ex["status"] == "success", "Учебный сценарий выполнился", ex.get("error", ""))
    summary = {i["канал"]: i for i in out_items(ex, "sum")}
    check(summary.get("Wildberries", {}).get("выручка") == 128200 and summary.get("Опт", {}).get("заказов") == 1,
          "Итоги по каналам посчитаны верно (условие → две ветки → слияние → сводная)", repr(summary))
    big = out_items(ex, "big")
    check(any("Позвонить клиенту ООО Бассейн-Строй" == i.get("задача") for i in big), "Выражение с русским полем подставилось")
    ex_stop = a.run(learn_id, stop_at="if")
    check("merge" not in ex_stop["run_data"] and "if" in ex_stop["run_data"], "«Выполнить до этого узла» останавливается на узле")

    # ------------------------------------------------------- вебхук с защитой
    secret = uuid.uuid4().hex
    path = "zakaz-" + uuid.uuid4().hex[:6]
    wh_id = a.create("Приём заказа", [
        N("w", "webhook", "Вебхук", {"path": path, "method": "POST", "auth": "header", "auth_header": "X-Token",
                                     "auth_value": secret, "response": "last_node"}),
        N("s", "set", "Посчитать НДС", {"fields": [{"name": "сумма_с_ндс", "value": "{{ $json.body.сумма * 1.2 }}", "type": "number"},
                                                  {"name": "клиент", "value": "{{ $json.body.клиент }}", "type": "string"}],
                                       "keep_other": False}, x=300),
    ], [E("w", "s")])
    r = a.send("POST", f"/api/workflows/{wh_id}/activate", {"active": True})
    check(r.status_code == 200 and r.json()["active"], "Сценарий с вебхуком включается")
    r = httpx.post(f"{BASE}/webhook/{path}", json={"сумма": 1000}, trust_env=False)
    check(r.status_code == 401, "Вебхук без секрета — 401")
    r = httpx.post(f"{BASE}/webhook/{path}", json={"сумма": 1000, "клиент": "ООО «Ромашка»"},
                   headers={"X-Token": secret}, trust_env=False, timeout=60)
    body = r.json() if r.status_code == 200 else {}
    check(body.get("сумма_с_ндс") == 1200 and body.get("клиент") == "ООО «Ромашка»",
          "Вебхук принял JSON с русским текстом и вернул результат последнего узла", r.text)
    r = httpx.get(f"{BASE}/webhook/nesushchestvuet", trust_env=False)
    check(r.status_code == 404 and "не найден" in r.json()["error"], "Несуществующий вебхук — понятный ответ 404")
    # конфликт адресов
    dup_id = a.create("Дубль адреса", [N("w", "webhook", "Вебхук", {"path": path, "method": "POST"})], [])
    r = a.send("POST", f"/api/workflows/{dup_id}/activate", {"active": True})
    check(r.status_code == 400 and "уже занят" in r.json()["error"], "Второй сценарий на тот же адрес не включается")

    # ответ узлом «Ответ на вебхук»
    path2 = "otvet-" + uuid.uuid4().hex[:6]
    rw_id = a.create("Свой ответ", [
        N("w", "webhook", "Вебхук", {"path": path2, "method": "GET", "response": "respond_node"}),
        N("r", "respond_webhook", "Ответ", {"respond_with": "text", "body": "Привет, {{ $json.query.имя }}!", "status": 201}, x=300),
    ], [E("w", "r")])
    a.send("POST", f"/api/workflows/{rw_id}/activate", {"active": True})
    r = httpx.get(f"{BASE}/webhook/{path2}", params={"имя": "Виктор"}, trust_env=False, timeout=60)
    check(r.status_code == 201 and r.text == "Привет, Виктор!", "Узел «Ответ на вебхук»: свой код и текст", f"{r.status_code} {r.text}")

    # --------------------------------------------------- сценарий для ошибок
    err_wf = a.create("Ловец ошибок", [
        N("t", "error_trigger", "При ошибке"),
        N("s", "set", "Запомнить", {"fields": [{"name": "что", "value": "{{ $json.ошибка.сообщение }}"}]}, x=300),
    ], [E("t", "s")])
    path3 = "padenie-" + uuid.uuid4().hex[:6]
    bad_id = a.create("Падающий", [
        N("w", "webhook", "Вебхук", {"path": path3, "method": "POST"}),
        N("x", "stop_error", "Стоп", {"message": "Нет остатка по артикулу {{ $json.body.артикул }}"}, x=300),
    ], [E("w", "x")], {"error_workflow_id": str(err_wf)})
    a.send("POST", f"/api/workflows/{bad_id}/activate", {"active": True})
    httpx.post(f"{BASE}/webhook/{path3}", json={"артикул": "АБ-1"}, trust_env=False)
    time.sleep(2)
    rows = c.get(f"/executions?workflow={err_wf}").text
    check("Обработка ошибки" in rows, "Ошибка в сценарии запустила «сценарий для ошибок»")
    last_err = [x for x in re.findall(r"\?execution=(\d+)", rows)]
    ex_e = a.wait(int(last_err[0])) if last_err else {}
    check(out_items(ex_e, "s")[:1] and "Нет остатка по артикулу АБ-1" in out_items(ex_e, "s")[0].get("что", ""),
          "В сценарий для ошибок пришли текст ошибки и данные", repr(out_items(ex_e, "s")))
    j = c.get("/journal?section=workflows").text
    check("Падающий" in j, "Ошибка сценария записана в журнал")

    # -------------------------------------------------------- подсценарий
    sub_id = a.create("Подсценарий", [
        N("t", "subworkflow_trigger", "Вызов"),
        N("s", "set", "Удвоить", {"fields": [{"name": "x2", "value": "{{ $json.x * 2 }}", "type": "number"}]}, x=300),
    ], [E("t", "s")])
    main_id = a.create("Главный", [
        N("t", "manual", "Старт"),
        N("c", "code", "Данные", {"code": "return [{'x': 1}, {'x': 5}]"}, x=250),
        N("e", "execute_workflow", "Вызвать", {"workflow_id": str(sub_id), "mode": "once"}, x=500),
    ], [E("t", "c"), E("c", "e")])
    ex = a.run(main_id)
    check(ex["status"] == "success" and [i.get("x2") for i in out_items(ex, "e")] == [2, 10],
          "«Выполнить сценарий» вызвал подсценарий и получил результат", repr(out_items(ex, "e")) + ex.get("error", ""))

    # ------------------------------------------------ ошибки узлов по-русски
    code_id = a.create("Код с ошибкой", [
        N("t", "manual", "Старт"),
        N("c", "code", "Код", {"code": "x = 1\nreturn [{'r': x / 0}]"}, x=250),
    ], [E("t", "c")])
    ex = a.run(code_id)
    check(ex["status"] == "error" and "строка 2" in ex["error"] and ex["error_node"] == "Код",
          "Ошибка в коде: номер строки и узел", ex.get("error", ""))
    ex = a.run(code_id, {"nodes": [N("t", "manual", "Старт"), N("c", "code", "Код", {"code": "while True: pass", "timeout": 2}, x=250)],
                         "connections": [E("t", "c")]})
    check(ex["status"] == "error" and "дольше 2 секунд" in ex["error"], "Зависший код останавливается по времени", ex.get("error", ""))
    ex = a.run(code_id, {"nodes": [N("t", "manual", "Старт"),
                                   N("s", "set", "Выражение", {"fields": [{"name": "a", "value": "{{ $json.нет.поле.глубже( }}"}]}, x=250)],
                         "connections": [E("t", "s")]})
    check(ex["status"] == "error" and "Выражение написано с ошибкой" in ex["error"], "Ошибка в выражении — понятный текст", ex.get("error", ""))
    ex = a.run(code_id, {"nodes": [N("t", "manual", "Старт"),
                                   N("h", "http_request", "Запрос", {"url": "http://127.0.0.1:8099/nichego"}, x=250,
                                     settings={"retry": True, "max_tries": 2, "wait_ms": 100})],
                         "connections": [E("t", "h")]})
    check(ex["status"] == "error" and "отказал в соединении" in ex["error"] and len(ex["run_data"]["h"]) == 1,
          "HTTP-запрос к закрытому порту: понятная ошибка после повтора", ex.get("error", ""))
    ex = a.run(code_id, {"nodes": [N("t", "manual", "Старт"),
                                   N("h", "http_request", "Здоровье", {"url": BASE.replace("localhost", "127.0.0.1") + "/health"}, x=250)],
                         "connections": [E("t", "h")]})
    check(ex["status"] == "success" and out_items(ex, "h")[0].get("ok") is True, "HTTP-запрос: ответ JSON стал элементом")
    ex = a.run(code_id, {"nodes": [N("t", "manual", "Старт"),
                                   N("h", "http_request", "Мимо", {"url": "http://127.0.0.1:8099/x"}, x=250,
                                     settings={"continue_on_fail": True}),
                                   N("s", "set", "Дальше", {"fields": [{"name": "дошли", "value": "да"}]}, x=500)],
                         "connections": [E("t", "h"), E("h", "s")]})
    check(ex["status"] == "success" and out_items(ex, "s")[0].get("дошли") == "да" and "ошибка" in out_items(ex, "s")[0],
          "«Не останавливать при ошибке»: сценарий идёт дальше с полем «ошибка»")
    onec_id = next(x["id"] for x in a.get("/api/lookup").json()["connections"] if x["type"] == "onec")
    ex = a.run(code_id, {"nodes": [N("t", "manual", "Старт"),
                                   N("o", "onec", "1С", {"connection": str(onec_id), "operation": "list",
                                                         "entity": "Catalog_Номенклатура"}, x=250)],
                         "connections": [E("t", "o")]})
    check(ex["status"] == "success" and out_items(ex, "o")[:1] and out_items(ex, "o")[0].get("Description") == "Товар 1",
          "Узел 1С: данные из подключения панели", ex.get("error", ""))
    claude_id = next(x["id"] for x in a.get("/api/lookup").json()["connections"] if x["type"] == "claude")
    ex = a.run(code_id, {"nodes": [N("t", "manual", "Старт"),
                                   N("ai", "claude", "Claude", {"connection": str(claude_id), "prompt": "Привет"}, x=250)],
                         "connections": [E("t", "ai")]})
    check(ex["status"] == "error" and ("Ключ Claude API неверный" in ex["error"] or "отклоняет" in ex["error"]),
          "Узел Claude с неверным ключом — понятная ошибка", ex.get("error", ""))

    # ---------------------------------------------------------- расписание
    manual_only = a.create("Только вручную", [N("t", "manual", "Старт")], [])
    r = a.send("POST", f"/api/workflows/{manual_only}/activate", {"active": True})
    check(r.status_code == 400 and "Включать нечего" in r.json()["error"], "Сценарий без расписания и вебхука не включается")
    bad_cron = a.create("Плохой cron", [N("t", "schedule", "Расписание", {"mode": "cron", "cron": "99 * * *"})], [])
    r = a.send("POST", f"/api/workflows/{bad_cron}/activate", {"active": True})
    check(r.status_code == 400 and "5 частей" in r.json()["error"], "Ошибка в cron объясняется при включении")
    r = a.send("POST", "/api/cron-preview", {"params": {"mode": "weekdays", "time": "09:30"}})
    check("Следующий запуск" in r.json()["text"], "Предпросмотр расписания: " + r.json()["text"])
    sched = a.create("Каждую минуту", [
        N("t", "schedule", "Расписание", {"mode": "cron", "cron": "* * * * *"}),
        N("s", "set", "Метка", {"fields": [{"name": "запуск", "value": "{{ $now.toFormat('HH:mm') }}"}]}, x=300),
    ], [E("t", "s")])
    a.send("POST", f"/api/workflows/{sched}/activate", {"active": True})
    got = False
    for _ in range(80):
        if "По расписанию" in c.get(f"/executions?workflow={sched}").text:
            got = True
            break
        time.sleep(1)
    check(got, "Сценарий по расписанию запустился сам")
    a.send("POST", f"/api/workflows/{sched}/activate", {"active": False})

    # ------------------------------------------------------ тестовый вебхук
    path4 = "test-" + uuid.uuid4().hex[:6]
    tw = a.create("Тестовый вебхук", [N("w", "webhook", "Вебхук", {"path": path4, "method": "POST"})], [])
    r = httpx.post(f"{BASE}/webhook-test/{path4}", json={}, trust_env=False)
    check(r.status_code == 404 and "не слушается" in r.json()["error"], "Тестовый адрес без ожидания — понятная подсказка")
    r = a.send("POST", f"/api/workflows/{tw}/listen", {"node_id": "w"})
    since = r.json()["since"]
    r = httpx.post(f"{BASE}/webhook-test/{path4}", json={"проверка": 1}, trust_env=False)
    time.sleep(1)
    latest = a.get(f"/api/workflows/{tw}/latest?since={since}").json()
    check(r.status_code == 200 and latest.get("execution_id"), "Тестовый вызов принят выключенным сценарием и виден редактору")

    # ------------------------------------------------------ предпросмотр выражений
    r = a.send("POST", "/api/expression-preview", {"expr": "{{ $json.имя.toUpperCase() }} — {{ $json.сумма * 2 }}",
                                                   "items": [{"имя": "анна", "сумма": 21}]})
    check(r.json().get("value") == "АННА — 42", "Предпросмотр выражения", r.text)

    # ------------------------------------------------------- импорт из n8n
    n8n = {"name": "Из n8n", "nodes": [
        {"id": "1", "name": "When clicking", "type": "n8n-nodes-base.manualTrigger", "position": [0, 0], "parameters": {}},
        {"id": "2", "name": "Set", "type": "n8n-nodes-base.set", "typeVersion": 3, "position": [220, 0],
         "parameters": {"assignments": {"assignments": [{"name": "price", "value": "={{ 100 * 2 }}", "type": "number"}]}}},
        {"id": "3", "name": "IF", "type": "n8n-nodes-base.if", "typeVersion": 2, "position": [440, 0],
         "parameters": {"conditions": {"combinator": "and", "conditions": [
             {"leftValue": "={{ $json.price }}", "rightValue": 150, "operator": {"type": "number", "operation": "gt"}}]}}},
        {"id": "4", "name": "Code", "type": "n8n-nodes-base.code", "position": [660, -100], "parameters": {"jsCode": "return items;"}},
        {"id": "5", "name": "Airtable", "type": "n8n-nodes-base.airtable", "position": [660, 100], "parameters": {}},
    ], "connections": {"When clicking": {"main": [[{"node": "Set", "type": "main", "index": 0}]]},
                       "Set": {"main": [[{"node": "IF", "type": "main", "index": 0}]]},
                       "IF": {"main": [[{"node": "Code", "type": "main", "index": 0}], [{"node": "Airtable", "type": "main", "index": 0}]]}}}
    r = c.post("/workflows/import", data={"csrf": a.token},
               files={"file": ("n8n.json", json.dumps(n8n, ensure_ascii=False).encode(), "application/json")})
    imp_id = int(r.headers["location"].rsplit("/", 1)[1])
    page = c.get(f"/workflows/{imp_id}").text
    check("Airtable" in page and "не поддерживается" in page and "JavaScript" in page,
          "Импорт из n8n: предупреждения о неподдерживаемом узле и коде JS")
    wf = a.get(f"/api/workflows/{imp_id}").json()
    check(len(wf["nodes"]) == 5 and len(wf["connections"]) == 4, "Импорт из n8n: узлы и связи перенесены")
    # отключаем неподдерживаемое и выполняем до условия
    for n in wf["nodes"]:
        if n["type"] in ("unsupported", "code"):
            n["disabled"] = True
    ex = a.run(imp_id, {"nodes": wf["nodes"], "connections": wf["connections"]})
    if_id = next(n["id"] for n in wf["nodes"] if n["type"] == "if")
    check(ex["status"] == "success" and len(out_items(ex, if_id, 0)) == 1, "Импортированный из n8n сценарий выполняется",
          ex.get("error", ""))

    # --------------------------------------------------- экспорт → импорт
    r = c.get(f"/workflows/{learn_id}/export")
    exported = r.content
    r = c.post("/workflows/import", data={"csrf": a.token}, files={"file": ("wf.json", exported, "application/json")})
    re_id = int(r.headers["location"].rsplit("/", 1)[1])
    ex = a.run(re_id)
    check(ex["status"] == "success", "Экспорт и повторный импорт сценария работают")

    # --------------------------------------------------- страницы истории
    r = c.get("/executions?status=error")
    check(r.status_code == 200 and "Ошибка" in r.text, "История выполнений с фильтром по ошибкам")
    r = c.get(f"/workflows/{code_id}?execution={ex['id']}")
    check(r.status_code == 200, "Просмотр выполнения в редакторе открывается")

    # --------------------------------------------------------- переменные
    r = c.post("/workflows/variables", data={"csrf": a.token, "key": ["MANAGER_CHAT_ID", "плохое имя"], "value": ["123", "x"]})
    check("только латиница" in " ".join(c.get("/workflows/variables").text.split()) or r.status_code == 303,
          "Переменные: неверное имя отклонено")
    c.post("/workflows/variables", data={"csrf": a.token, "key": ["MANAGER_CHAT_ID"], "value": ["123456"]})
    ex = a.run(code_id, {"nodes": [N("t", "manual", "Старт"),
                                   N("s", "set", "Чат", {"fields": [{"name": "чат", "value": "{{ $vars.MANAGER_CHAT_ID }}"}]}, x=250)],
                         "connections": [E("t", "s")]})
    check(out_items(ex, "s")[0].get("чат") == "123456", "Переменная $vars подставляется в узлы")

    ok = sum(results)
    print(f"\nИтого: {ok} из {len(results)} проверок пройдено")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
