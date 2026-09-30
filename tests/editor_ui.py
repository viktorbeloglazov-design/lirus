"""Проверка редактора сценариев в настоящем браузере (Chromium). Запускать после scenarios.py и workflows_live.py."""
import asyncio, json, re, sys
from playwright.async_api import async_playwright
BASE="http://localhost:8080"; OUT="/home/user/lirus/docs/screenshots"
ok=[]
def check(c,t,e=""):
    ok.append(bool(c)); print(("[OK  ] " if c else "[FAIL] ")+t+(("\n        "+str(e)) if e and not c else ""))
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell")
        for scheme in ("light","dark"):
            ctx = await b.new_context(viewport={"width":1500,"height":900}, color_scheme=scheme, locale="ru-RU")
            page = await ctx.new_page()
            errors=[]
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type=="error" and "status of 400" not in m.text else None)
            await page.goto(BASE+"/login"); await page.fill("#login","admin"); await page.fill("#password","Надёжный пароль 2026")
            await page.click("button[type=submit]")
            await page.goto(BASE+"/workflows")
            if scheme=="light": await page.screenshot(path=f"{OUT}/scenarii.png", full_page=True)
            # новый сценарий из учебного шаблона
            await page.locator("form:has(input[value=learn]) button").click()
            await page.wait_for_selector("g[data-node]")
            n = await page.locator("g[data-node]").count()
            if scheme=="light": check(n==8, f"Редактор нарисовал узлы шаблона: {n}")
            await page.click("#wf-run")
            await page.wait_for_selector(".wf-status.ok", timeout=30000)
            if scheme=="light": check(True, "Кнопка «Выполнить» — сценарий выполнен, статус зелёный")
            await page.locator("g[data-node]", has_text="Итоги по каналам").click()
            await page.click(".wf-tab[data-tab=data]")
            await page.wait_for_selector(".wf-table")
            txt = await page.inner_text("#wf-panel-body")
            if scheme=="light": check("Wildberries" in txt and "128200" in txt, "Вкладка «Данные» показывает выход узла")
            await page.screenshot(path=f"{OUT}/redaktor-{scheme}.png")
            if scheme=="dark":
                await ctx.close(); continue
            # параметры узла «Крупный заказ?»
            await page.locator("g[data-node]", has_text="Крупный заказ?").click()
            await page.click(".wf-tab[data-tab=params]")
            await page.wait_for_selector(".wf-list-row")
            await page.screenshot(path=f"{OUT}/redaktor-parametry.png")
            # предпросмотр выражения на данных запуска
            await page.locator("g[data-node]", has_text="Пометить: крупный").click()
            await page.click(".wf-tab[data-tab=params]")
            inp = page.locator(".wf-list-row input").nth(3)
            await inp.fill("Звонок: {{ $json.клиент.toUpperCase() }}")
            await page.wait_for_selector(".wf-preview.ok", timeout=5000)
            prev = await page.inner_text(".wf-preview.ok")
            check("ООО БАССЕЙН-СТРОЙ" in prev or "ООО РОМАШКА" in prev, "Предпросмотр выражения на данных последнего запуска", prev)
            # добавление узла через поиск (Tab) с автосвязью от выделенного
            await page.click("#wf-panel-close")
            await page.click("#wf-fit")
            await page.locator("g[data-node]", has_text="Итоги по каналам").click()
            await page.click("#wf-panel-close")
            await page.click("#wf-add")
            await page.fill("#wf-picker-search", "сортир")
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(300)
            n_after = await page.locator("g[data-node]").count()
            await page.click("#wf-panel-close"); await page.click("#wf-fit")
            check(n_after==9, f"Узел добавлен через поиск: узлов {n_after}")
            # соединение мышью: от выхода «Сортировка» нет, соединим «Собрать вместе» → новый узел вторым путём
            sort_node = page.locator("g[data-node]", has_text="Сортировка")
            src_port = page.locator("g[data-node]", has_text="Пример заказов").locator("circle.out")
            dst_port = sort_node.locator("circle.in")
            s = await src_port.bounding_box(); d = await dst_port.bounding_box()
            await page.mouse.move(s["x"]+7, s["y"]+7); await page.mouse.down()
            await page.mouse.move(d["x"]+7, d["y"]+7, steps=8); await page.mouse.up()
            edges = await page.locator("path.wf-edge:not(.temp)").count()
            check(edges>=9, f"Связь протянута мышью: связей {edges}")
            # перетаскивание узла и отмена
            await page.click("#wf-fit")
            box = await sort_node.bounding_box()
            await page.mouse.move(box["x"]+100, box["y"]+20); await page.mouse.down()
            await page.mouse.move(box["x"]+100, box["y"]+220, steps=6); await page.mouse.up()
            box2 = await sort_node.bounding_box()
            check(box2["y"]-box["y"]>150, "Узел перетаскивается мышью")
            await page.keyboard.press("Escape")
            await page.click("#wf-canvas", position={"x":5,"y":5})
            await page.keyboard.press("Control+z")
            box3 = await sort_node.bounding_box()
            check(abs(box3["y"]-box["y"])<5, "Ctrl+Z отменяет перемещение")
            # сохранение Ctrl+S и перезагрузка
            await page.keyboard.press("Control+s")
            for _ in range(50):
                if (await page.inner_text("#wf-saved")) == "Сохранено": break
                await page.wait_for_timeout(100)
            await page.reload(); await page.wait_for_selector("g[data-node]")
            n_reload = await page.locator("g[data-node]").count()
            e_reload = await page.locator("path.wf-edge").count()
            check(n_reload==9 and e_reload>=9, f"После сохранения и перезагрузки: узлов {n_reload}, связей {e_reload-1}")
            # удаление узла клавишей Delete
            await page.locator("g[data-node]", has_text="Сортировка").click()
            await page.click("#wf-panel-close")
            await page.keyboard.press("Delete")
            check(await page.locator("g[data-node]").count()==8, "Delete удаляет выделенный узел")
            # включение без триггера: учебный — ручной → понятная ошибка
            await page.click("#wf-active")
            await page.wait_for_selector(".wf-banner.error")
            ban = await page.inner_text("#wf-banner")
            check("Включать нечего" in ban, "Включение ручного сценария — понятное объяснение", ban)
            # просмотр выполнения
            await page.goto(BASE+"/executions")
            await page.screenshot(path=f"{OUT}/vypolneniya.png", full_page=True)
            first = page.locator("table a").first
            await first.click(); await page.wait_for_selector(".wf-banner")
            check("Просмотр выполнения" in await page.inner_text("#wf-banner"), "Выполнение открывается в редакторе только для просмотра")
            await page.screenshot(path=f"{OUT}/prosmotr-vypolneniya.png")
            # импортированный из n8n
            await page.goto(BASE+"/workflows")
            link = page.locator("a", has_text="Из n8n").first
            await link.click(); await page.wait_for_selector("g[data-node]")
            await page.locator("g[data-node]", has_text="Airtable").click()
            await page.wait_for_selector(".wf-error-box")
            await page.screenshot(path=f"{OUT}/import-n8n.png")
            check(True, "Неподдерживаемый узел из n8n показан с объяснением")
            # вебхук-узел: адреса
            await page.goto(BASE+"/workflows")
            await page.locator("a", has_text="Приём заказа").first.click(); await page.wait_for_selector("g[data-node]")
            await page.locator("g[data-node]", has_text="Вебхук").click()
            await page.wait_for_selector(".wf-webhook")
            check("/webhook/zakaz-" in await page.inner_text(".wf-webhook"), "У узла «Вебхук» показаны рабочий и тестовый адреса")
            await page.screenshot(path=f"{OUT}/uzel-vebhuk.png")
            # каталог узлов
            await page.click("#wf-add"); await page.wait_for_timeout(200)
            await page.screenshot(path=f"{OUT}/katalog-uzlov.png")
            check(not errors, "Ошибок JavaScript в браузере нет", errors[:5])
            await ctx.close()
        await b.close()
    print(f"\nИтого: {sum(ok)} из {len(ok)}")
asyncio.run(main())
