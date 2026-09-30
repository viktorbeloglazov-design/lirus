"""Живая проверка работы с 1С: обозреватель, предпросмотр, узел «1С» в сценарии.

Запускать после tests/scenarios.py (там создаётся подключение «КА — основная база»
к поддельной 1С на https://127.0.0.1:8091/ka).
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
results: list[bool] = []


def check(cond, title, extra=""):
    results.append(bool(cond))
    print(f"[{'OK  ' if cond else 'FAIL'}] {title}" + (f"\n        {extra}" if extra and not cond else ""))


def main():
    c = httpx.Client(base_url=BASE, follow_redirects=False, timeout=120, trust_env=False)
    c.post("/login", data={"login": "admin", "password": "Надёжный пароль 2026", "next": "/"})
    token = re.search(r'name="csrf" value="([^"]+)"', c.get("/workflows").text).group(1)
    conn = next(x for x in c.get("/api/lookup").json()["connections"] if x["type"] == "onec")
    cid = conn["id"]

    page = c.get(f"/connections/{cid}/1c")
    check(page.status_code == 200 and "Справочники" in page.text and "Номенклатура" in page.text
          and "Регистры накопления" in page.text, "Обозреватель 1С: объекты базы по группам")
    check("Обозреватель 1С" in c.get(f"/connections/{cid}").text, "На странице подключения 1С есть ссылка на обозреватель")

    d = c.get(f"/api/onec/{cid}/entities").json()
    check(len(d["entities"]) == 4 and "Catalog_Номенклатура" in d["entities"], "Список объектов для подсказок в редакторе")
    check(c.get("/api/onec/auto/entities").json()["entities"] == d["entities"], "Подсказки без выбранного подключения берут единственную 1С")

    q = urllib.parse.urlencode({"entity": "Catalog_Номенклатура", "top": "5"})
    d = c.get(f"/api/onec/{cid}/preview?{q}").json()
    check(len(d.get("records", [])) == 5 and d["records"][0]["Description"] == "Товар 1", "Предпросмотр: первые записи справочника")
    q = urllib.parse.urlencode({"entity": "Catalog_Номенклатура", "filter": "Артикул eq 'АРТ-0007'",
                                "select": "Description,Артикул"})
    d = c.get(f"/api/onec/{cid}/preview?{q}").json()
    check(d.get("records") == [{"Description": "Товар 7", "Артикул": "АРТ-0007"}],
          "Отбор по русскому полю и значению, выбор полей", repr(d))
    q = urllib.parse.urlencode({"entity": "Catalog_Номенклатура", "filter": "Цена больше 5"})
    d = c.get(f"/api/onec/{cid}/preview?{q}").json()
    check("1С не поняла запрос" in d.get("error", "") and "Ошибка разбора" in d.get("error", "") and "datetime" in d.get("action", ""),
          "Ошибка в отборе: текст самой 1С и подсказка", repr(d))
    q = urllib.parse.urlencode({"entity": "Catalog_НетТакого"})
    d = c.get(f"/api/onec/{cid}/preview?{q}").json()
    check("не нашла объект «Catalog_НетТакого»" in d.get("error", ""), "Несуществующий объект — понятная ошибка", repr(d))
    q = urllib.parse.urlencode({"entity": "AccumulationRegister_ТоварыНаСкладах/Balance()"})
    d = c.get(f"/api/onec/{cid}/preview?{q}").json()
    check(len(d.get("records", [])) == 2 and "ВНаличииBalance" in d["records"][0], "Остатки регистра (/Balance())")

    mgr = httpx.Client(base_url=BASE, follow_redirects=False, timeout=30, trust_env=False)
    mgr.post("/login", data={"login": "manager1", "password": "Новый-менеджер-2", "next": "/"})
    check(mgr.get(f"/api/onec/{cid}/entities").status_code == 403 and mgr.get(f"/connections/{cid}/1c").status_code == 403,
          "Менеджеру обозреватель 1С закрыт")

    # --------------------------------------------------------- узел «1С»
    def run(nodes, edges):
        r = c.post("/workflows/new", data={"csrf": token, "name": "1С тест"})
        wf = int(r.headers["location"].rsplit("/", 1)[1])
        r = c.post(f"/api/workflows/{wf}/run", json={"workflow": {"nodes": nodes, "connections": edges}},
                   headers={"X-CSRF-Token": token})
        ex_id = r.json()["execution_id"]
        for _ in range(300):
            ex = c.get(f"/api/executions/{ex_id}").json()
            if not ex.get("running"):
                return ex
            time.sleep(0.2)
        return ex

    def node(params):
        return [{"id": "t", "type": "manual", "name": "Старт", "position": [0, 0], "params": {}},
                {"id": "o", "type": "onec", "name": "1С", "position": [250, 0],
                 "params": {"connection": str(cid), **params}}], [{"from": "t", "out": 0, "to": "o", "in": 0}]

    def count(ex):
        runs = ex["run_data"].get("o") or [{}]
        return (runs[-1].get("outputs") or [{}])[0].get("count")

    ex = run(*node({"operation": "list", "entity": "Catalog_Номенклатура", "all_pages": True}))
    check(ex["status"] == "success" and count(ex) == 1200, f"Узел 1С: все 1200 записей порциями по 500 ({ex.get('error', '')})")
    ex = run(*node({"operation": "list", "entity": "Catalog_Номенклатура", "all_pages": True, "filter": "DeletionMark eq false"}))
    check(count(ex) == 1176, "Узел 1С: отбор помеченных на удаление", str(count(ex)))
    ex = run(*node({"operation": "list", "entity": "Catalog_Номенклатура", "top": 10}))
    check(count(ex) == 10, "Узел 1С: ограничение количества")
    ex = run(*node({"operation": "list", "entity": "Catalog_Номенклатура", "all_pages": True, "max_records": 700}))
    check(count(ex) == 700, "Узел 1С: «не больше N записей»", str(count(ex)))
    ex = run(*node({"operation": "list", "entity": "Catalog_НетТакого"}))
    check(ex["status"] == "error" and "не нашла объект" in ex["error"] and "Обозревателе 1С" in ex.get("error_hint", ""),
          "Узел 1С: несуществующий объект — ошибка с подсказкой", ex.get("error", ""))

    ok = sum(results)
    print(f"\nИтого: {ok} из {len(results)} проверок пройдено")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
