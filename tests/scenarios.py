"""Сценарная проверка живого сервера — как это делал бы человек в браузере.

Формы отправляются так же, как их отправляет браузер: application/x-www-form-urlencoded,
русский текст — в percent-encoding (UTF-8).

Нужно: запущенная платформа (run.py start) и поддельная 1С (tests/fake_1c.py 8091 https,
tests/fake_1c.py 8092). Запуск: python tests/scenarios.py [http://localhost:8080]
"""
from __future__ import annotations

import base64
import json
import re
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, crypto  # noqa: E402

NEW_ADMIN_PASSWORD = "Надёжный пароль 2026"
FAKE_CLAUDE_KEY = "sk-ant-api03-FAKEkey1234567890abcdefXYZ"
WB_TOKEN = "eyJhbGciOiJFUzI1NiJ9." + base64.urlsafe_b64encode(
    json.dumps({"exp": int(time.time()) + 150 * 86400, "sid": "test"}).encode()).decode().rstrip("=") + ".c2lnbmF0dXJl"
TG_TOKEN = "1234567890:AAHfakeTokenForTestsOnly_abcdefghijk"
WEBHOOK_CODE = "abc123secretcode"

results: list[tuple[bool, str]] = []


def check(cond: bool, title: str, extra: str = "") -> None:
    results.append((bool(cond), title))
    mark = "OK  " if cond else "FAIL"
    print(f"[{mark}] {title}" + (f"\n        {extra}" if (extra and not cond) else ""))


def csrf(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def flashes(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", m).strip() for m in re.findall(r'class="flash flash-\w+"[^>]*>(.*?)</div>', html, re.S)]


def db_row(sql: str, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def secret_of(conn_id: int, name: str) -> str:
    row = db_row("SELECT secrets FROM connections WHERE id = ?", (conn_id,))
    token = json.loads(row["secrets"]).get(name)
    return crypto.decrypt(token) if token else ""


def new_client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, follow_redirects=False, timeout=90, trust_env=False)


def post(client: httpx.Client, url: str, data: dict, page_url: str | None = None) -> httpx.Response:
    """Как браузер: открываем страницу, берём csrf из формы, отправляем форму."""
    token = csrf(client.get(page_url or url).text)
    body = dict(data)
    body["csrf"] = token
    # httpx кодирует форму в UTF-8 percent-encoding — ровно как браузер
    return client.post(url, data=body)


def follow(client: httpx.Client, resp: httpx.Response) -> httpx.Response:
    while resp.status_code in (302, 303):
        resp = client.get(resp.headers["location"])
    return resp


def login(client: httpx.Client, user: str, password: str) -> httpx.Response:
    return client.post("/login", data={"login": user, "password": password, "next": "/"})


def main() -> int:
    admin = new_client()

    # --------------------------------------------------------------- вход
    r = admin.get("/")
    check(r.status_code == 303 and r.headers["location"].startswith("/login"), "Без входа панель отправляет на /login")
    r = admin.get("/login")
    check("admin" in r.text and "Первый запуск" in r.text, "На странице входа подсказка про admin/admin")
    r = login(admin, "admin", "неправильный")
    check(r.status_code == 401 and "Неверный логин или пароль" in r.text, "Неверный пароль — понятное сообщение")
    r = login(admin, "admin", "admin")
    check(r.status_code == 303 and r.headers["location"] == "/password", "admin/admin пускает, но сразу на смену пароля")
    r = admin.get("/connections")
    check(r.status_code == 303 and r.headers["location"] == "/password", "До смены пароля остальные разделы закрыты")
    r = post(admin, "/password", {"old_password": "admin", "new_password": "admin", "new_password2": "admin"})
    check(r.status_code == 400 and "короче 8" in r.text, "Слишком простой пароль отклонён по-русски")
    r = post(admin, "/password", {"old_password": "admin", "new_password": NEW_ADMIN_PASSWORD,
                                  "new_password2": NEW_ADMIN_PASSWORD})
    check(r.status_code == 303 and r.headers["location"] == "/", "Пароль сменён (русские буквы и пробелы в пароле)")
    post(admin, "/logout", {}, page_url="/")
    r = login(admin, "admin", NEW_ADMIN_PASSWORD)
    check(r.status_code == 303 and r.headers["location"] == "/", "Вход с новым русским паролем работает")

    # ------------------------------------------------ русский текст в формах
    company = "ООО «Ромашка и Ко» — Москва & Казань"
    r = post(admin, "/settings", {"company_name": company, "public_url": "", "port": "8080", "https_port": "8443",
                                  "journal_days": "90", "auto_check_hours": "6", "backup_keep": "14",
                                  "auto_backup": "1", "proxy_url": ""})
    check(r.status_code == 303, "Настройки сохраняются")
    stored = db_row("SELECT value FROM settings WHERE key = 'company_name'")["value"]
    check(stored == company, "Русский текст из формы сохранился в базе без искажений", repr(stored))
    html = admin.get("/settings").text
    check("ООО «Ромашка и Ко» — Москва &amp; Казань" in html, "…и так же показывается в форме")
    raw_body = "csrf=" + urllib.parse.quote(csrf(html)) + "&company_name=" + urllib.parse.quote("Тест %+&=") + \
        "&port=8080&https_port=8443&journal_days=90&auto_check_hours=6&backup_keep=14"
    admin.post("/settings", content=raw_body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    stored = db_row("SELECT value FROM settings WHERE key = 'company_name'")["value"]
    check(stored == "Тест %+&=", "Спецсимволы % + & = в percent-encoded запросе не ломают значение", repr(stored))
    post(admin, "/settings", {"company_name": company, "port": "8080", "https_port": "8443", "journal_days": "90",
                              "auto_check_hours": "6", "backup_keep": "14", "auto_backup": "1"})

    # ------------------------------------------------------------ 1С
    form_1c = {"name": "КА — основная база", "base_url": "  127.0.0.1:8091/ka/  ", "username": "Робот-интеграция",
               "password": "пароль-123", "use_odata": "1", "http_service_path": "/hs/platform/ping",
               "verify_ssl": "1", "action": "save_check"}
    r = post(admin, "/connections/new?type=onec", form_1c)
    check(r.status_code == 303, "Подключение 1С создано")
    conn_1c = int(r.headers["location"].rsplit("/", 1)[1])
    page = admin.get(f"/connections/{conn_1c}").text
    fl = " ".join(flashes(page))
    check("сертификат" in fl and "Проверять сертификат" in fl,
          "Самоподписанный сертификат 1С — сообщение про галочку «Проверять сертификат»", fl)
    row = db_row("SELECT name, config FROM connections WHERE id = ?", (conn_1c,))
    cfg = json.loads(row["config"])
    check(row["name"] == "КА — основная база" and cfg["username"] == "Робот-интеграция",
          "Название и русское имя пользователя 1С сохранены правильно", repr((row["name"], cfg)))
    check(cfg["base_url"] == "https://127.0.0.1:8091/ka", "Адрес 1С нормализован (https://, без пробелов и /)",
          cfg["base_url"])
    check(secret_of(conn_1c, "password") == "пароль-123", "Пароль 1С хранится зашифрованным и расшифровывается")
    check("пароль-123" not in page and "********" in page or "Сохранено" in page,
          "Пароль в форме не показывается, только маска")

    # Сохранение с пустым полем пароля: старый пароль должен остаться
    edit = dict(form_1c, password="", verify_ssl="")
    edit.pop("verify_ssl")
    r = post(admin, f"/connections/{conn_1c}", edit)
    page = follow(admin, r).text
    fl = " ".join(flashes(page))
    check(secret_of(conn_1c, "password") == "пароль-123", "Пустое поле пароля при сохранении — старый пароль сохранён")
    check("OData работает" in fl and "доступно объектов — 4" in fl and "HTTP-сервис /hs/platform/ping отвечает" in fl,
          "Проверка связи 1С проходит (OData + HTTP-сервис, Basic-авторизация в UTF-8)", fl)
    st = db_row("SELECT status FROM connections WHERE id = ?", (conn_1c,))["status"]
    check(st == "ok", "Статус подключения 1С — «работает»", st)

    r = post(admin, f"/connections/{conn_1c}", dict(edit, password="неверный"))
    fl = " ".join(flashes(follow(admin, r).text))
    check("неверное имя пользователя или пароль" in fl, "Неверный пароль 1С — «неверное имя пользователя или пароль»", fl)
    post(admin, f"/connections/{conn_1c}", dict(edit, password="пароль-123", action="save"))

    r = post(admin, "/connections/new?type=onec", dict(form_1c, name="Без OData", base_url="http://127.0.0.1:8092/noodata",
                                                          http_service_path="", verify_ssl=""))
    fl = " ".join(flashes(follow(admin, r).text))
    check("OData не опубликован" in fl and "Публиковать стандартный интерфейс OData" in fl,
          "404 от OData — «интерфейс OData не опубликован, включите галочку…»", fl)
    no_odata_id = int(r.headers["location"].rsplit("/", 1)[1])
    r = post(admin, f"/connections/{no_odata_id}", dict(form_1c, name="Без OData", base_url="http://127.0.0.1:8092/kaa",
                                                          password="", http_service_path=""))
    fl = " ".join(flashes(follow(admin, r).text))
    check("нет опубликованной базы" in fl, "Неверное имя публикации — «нет опубликованной базы 1С»", fl)
    r = post(admin, f"/connections/{no_odata_id}", dict(form_1c, name="Без OData", base_url="https://127.0.0.1:8092/ka",
                                                          password="", http_service_path="", verify_ssl=""))
    fl = " ".join(flashes(follow(admin, r).text))
    check("работает обычный http" in fl, "https на http-порт — «замените https на http»", fl)
    r = post(admin, f"/connections/{no_odata_id}", dict(form_1c, name="Без OData", base_url="http://127.0.0.1:8099/ka",
                                                          password="", http_service_path="", verify_ssl=""))
    fl = " ".join(flashes(follow(admin, r).text))
    check("отказал в соединении" in fl, "Закрытый порт — «сервер отказал в соединении»", fl)
    post(admin, f"/connections/{no_odata_id}/delete", {}, page_url=f"/connections/{no_odata_id}")
    check(db_row("SELECT id FROM connections WHERE id = ?", (no_odata_id,)) is None, "Подключение удаляется")

    # ------------------------------------------------------ Claude и токены
    bad_key = "sk-ant-api03-FAKEkeу12345"  # «у» — русская
    r = post(admin, "/connections/new?type=claude", {"api_key": bad_key, "model": "claude-opus-5",
                                                     "monthly_limit": "150,50", "action": "save"})
    check(r.status_code == 400 and "попали русские буквы" in r.text and "«у»" in r.text,
          "Русская буква в ключе Claude — человеческое сообщение, а не ошибка кодировки")
    check("ascii" not in r.text.lower() and "codec" not in r.text.lower(), "…и никакого «ascii codec» на странице")
    spaced = f"  {FAKE_CLAUDE_KEY[:20]} {FAKE_CLAUDE_KEY[20:]} \n"
    r = post(admin, "/connections/new?type=claude", {"api_key": spaced, "model": "claude-opus-5",
                                                     "monthly_limit": "150,50", "action": "save_check"})
    check(r.status_code == 303, "Ключ с лишними пробелами и переносом принят")
    claude_id = int(r.headers["location"].rsplit("/", 1)[1])
    check(secret_of(claude_id, "api_key") == FAKE_CLAUDE_KEY, "Лишние пробелы из ключа вырезаны автоматически")
    cfg = json.loads(db_row("SELECT config FROM connections WHERE id = ?", (claude_id,))["config"])
    check(cfg.get("monthly_limit") == 150.5, "Лимит «150,50» (с запятой) понят как 150.5", repr(cfg))
    fl = " ".join(flashes(admin.get(f"/connections/{claude_id}").text))
    check("Ключ Claude API неверный" in fl or "из страны" in fl or "Не удаётся" in fl,
          "Проверка ключа Claude даёт понятный ответ", fl)
    r = post(admin, "/connections/new?type=claude", {"api_key": FAKE_CLAUDE_KEY, "model": "claude-opus-5",
                                                     "monthly_limit": "10", "action": "save"},
             page_url="/connections")
    check(r.status_code == 409 and "может быть только одно" in r.text, "Второе ядро Claude — отказ на сервере (409)")
    n = db_row("SELECT COUNT(*) n FROM connections WHERE type = 'claude'")["n"]
    check(n == 1, "В базе по-прежнему одно ядро Claude", str(n))
    r = admin.get("/connections/new?type=claude")
    check(r.status_code == 303 and r.headers["location"] == f"/connections/{claude_id}",
          "Кнопка «новое ядро» ведёт к существующему")

    r = post(admin, "/connections/new?type=wildberries", {"name": "WB — ИП Иванов", "token": "еyJhbGc.abc.def",
                                                          "action": "save"})
    check(r.status_code == 400 and "русские буквы" in r.text, "Русская «е» в начале токена WB — понятная ошибка")
    r = post(admin, "/connections/new?type=wildberries", {"name": "WB — ИП Иванов", "token": " " + WB_TOKEN + " \n",
                                                          "action": "save_check"})
    wb_id = int(r.headers["location"].rsplit("/", 1)[1])
    fl = " ".join(flashes(admin.get(f"/connections/{wb_id}").text))
    check(bool(fl) and "Traceback" not in fl and "ascii" not in fl.lower(), "Проверка WB — понятный ответ", fl)
    print("        WB:", fl[:160])

    r = post(admin, "/connections/new?type=ozon", {"name": "Ozon", "client_id": "12 34 56", "api_key": "ключ-ozon",
                                                   "action": "save"})
    check(r.status_code == 400 and "русские буквы" in r.text, "Русские буквы в ключе Ozon — понятная ошибка")

    r = post(admin, "/connections/new?type=bitrix24",
             {"name": "Битрикс24", "webhook_url": f" https://romashka.bitrix24.ru/rest/1/{WEBHOOK_CODE}/profile.json ",
              "action": "save"})
    b24 = int(r.headers["location"].rsplit("/", 1)[1])
    check(secret_of(b24, "webhook_url") == f"https://romashka.bitrix24.ru/rest/1/{WEBHOOK_CODE}/",
          "Вебхук Битрикс24: пробелы и хвост profile.json отрезаны", secret_of(b24, "webhook_url"))

    r = post(admin, "/connections/new?type=telegram_bot", {"token": TG_TOKEN, "action": "save_check"})
    tg = int(r.headers["location"].rsplit("/", 1)[1])
    fl = " ".join(flashes(admin.get(f"/connections/{tg}").text))
    check(bool(fl) and TG_TOKEN not in fl, "Проверка Telegram — понятный ответ без токена в тексте", fl)
    print("        Telegram:", fl[:160])

    # ------------------------------------------------------------- Google
    r = post(admin, "/connections/new?type=google_app",
             {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-fakeSecretForTests",
              "action": "save_check"})
    gapp = int(r.headers["location"].rsplit("/", 1)[1])
    page = admin.get(f"/connections/{gapp}").text
    fl = " ".join(flashes(page))
    check("неверные" in fl or "Не удаётся" in fl or "не ответил" in fl, "Ключи Google проверяются и объясняются", fl)
    check("/oauth/google/callback" in page, "На форме Google показаны адреса для «Authorized redirect URIs»")
    r = post(admin, "/oauth/google/start", {}, page_url="/connections/new?type=google_account")
    check(r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?"),
          "«Подключить аккаунт Google» ведёт на страницу входа Google")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)
    check(q.get("access_type") == ["offline"] and q.get("prompt") == ["consent"]
          and "https://www.googleapis.com/auth/gmail.modify" in q["scope"][0]
          and "https://www.googleapis.com/auth/drive" in q["scope"][0]
          and "https://www.googleapis.com/auth/userinfo.email" in q["scope"][0]
          and q["redirect_uri"][0].endswith("/oauth/google/callback"),
          "Параметры OAuth: offline, consent, gmail.modify + drive + userinfo.email", repr(q))
    state = q["state"][0]
    r = admin.get(f"/oauth/google/callback?state={state}&error=access_denied")
    fl = " ".join(flashes(follow(admin, r).text))
    check("Отмена" in fl, "Отказ на странице Google — понятное сообщение", fl)
    r = admin.get("/oauth/google/callback?state=poddelka&code=x")
    check(r.status_code == 400 and "устарела" in r.text, "Поддельный state в ответе Google отклонён")
    ip_client = httpx.Client(base_url=BASE_URL.replace("localhost", "127.0.0.1"), follow_redirects=False, timeout=30,
                             trust_env=False)
    login(ip_client, "admin", NEW_ADMIN_PASSWORD)
    r = post(ip_client, "/oauth/google/start", {}, page_url="/connections/new?type=google_account")
    check(r.status_code == 400 and "localhost" in r.text, "Вход Google с IP-адреса — объяснение, как быть")

    # ------------------------------------------------------------ роли
    r = post(admin, "/employees/new", {"full_name": "Мария Менеджерова", "login": "manager1", "role": "manager",
                                       "password": "Менеджер-пароль-1", "must_change": "1", "active": "1",
                                       "telegram_id": "", "max_id": ""})
    check(r.status_code == 303, "Сотрудник-менеджер создан")
    r = post(admin, "/employees/new", {"full_name": "Директор Иван", "login": "director1", "role": "director",
                                       "password": "Директор-пароль-1", "active": "1"})
    check(r.status_code == 303, "Сотрудник-руководитель создан")
    r = post(admin, "/employees/new", {"full_name": "Кто-то", "login": "manager1", "role": "employee", "active": "1",
                                       "telegram_id": "abc"})
    check(r.status_code == 400 and "занят" in r.text and "это число" in r.text, "Занятый логин и неверный ID — ошибки по-русски")

    mgr = new_client()
    r = login(mgr, "manager1", "Менеджер-пароль-1")
    check(r.headers.get("location") == "/password", "Менеджер при первом входе меняет пароль")
    post(mgr, "/password", {"old_password": "Менеджер-пароль-1", "new_password": "Новый-менеджер-2",
                            "new_password2": "Новый-менеджер-2"})
    for url in ("/connections", "/connections/new?type=claude", f"/connections/{conn_1c}", "/employees", "/testing",
                "/testing/report", "/settings", "/journal", "/settings/backup/x.zip"):
        r = mgr.get(url)
        check(r.status_code == 403 and "Нет доступа" in r.text, f"Менеджеру закрыт {url} (403)", str(r.status_code))
    mtoken = csrf(mgr.get("/").text)
    r = mgr.post("/connections/new?type=onec", data={"csrf": mtoken, "name": "взлом", "base_url": "http://x",
                                                     "username": "a"})
    check(r.status_code == 403, "Менеджер не может создать подключение даже прямым POST", str(r.status_code))
    r = mgr.post(f"/connections/{conn_1c}/delete", data={"csrf": mtoken})
    check(r.status_code == 403 and db_row("SELECT id FROM connections WHERE id=?", (conn_1c,)) is not None,
          "Менеджер не может удалить подключение прямым POST")
    r = mgr.post("/testing/run", data={"csrf": mtoken})
    check(r.status_code == 403, "Менеджер не может запустить проверку прямым POST")
    home = mgr.get("/").text
    check("Расходы на Claude" not in home and "Подключения" not in home.split("<main")[0] if "<main" in home else True,
          "На обзоре менеджера нет финансов и раздела «Подключения»")
    check("финансы" not in home.split("Данные, которые бот будет показывать")[-1][:300],
          "В списке данных для бота у менеджера нет финансов")

    boss = new_client()
    login(boss, "director1", "Директор-пароль-1")
    home = boss.get("/").text
    check("Расходы на Claude" in home, "Руководитель видит финансы на обзоре")
    check(boss.get("/journal").status_code == 200, "Руководитель видит журнал")
    check(boss.get("/connections").status_code == 403, "Руководителю закрыт раздел «Подключения»")

    # Нельзя разжаловать последнего админа / себя
    me_id = db_row("SELECT id FROM users WHERE login='admin'")["id"]
    r = post(admin, f"/employees/{me_id}", {"full_name": "Администратор", "login": "admin", "role": "manager", "active": "1"})
    check(r.status_code == 400 and "самого себя" in r.text, "Администратор не может снять роль с самого себя")

    # ---------------------------------------------------------- защита форм
    r = admin.post("/settings/backup", data={"csrf": "podbor"})
    check(r.status_code == 400 and "Страница устарела" in r.text, "Форма без правильного csrf отклоняется понятно")

    # --------------------------------------------------------- сертификат
    pfx = (ROOT / "tests" / "_certs" / "panel.pfx").read_bytes()
    token = csrf(admin.get("/settings").text)
    r = admin.post("/settings/tls", data={"csrf": token, "tls_password": "не тот"},
                   files={"pfx": ("panel.pfx", pfx, "application/x-pkcs12")})
    check(r.status_code == 400 and "Пароль от файла .pfx неверный" in r.text, "Неверный пароль .pfx — понятная ошибка")
    r = admin.post("/settings/tls", data={"csrf": token, "tls_password": "пароль"},
                   files={"pfx": ("panel.pfx", pfx, "application/x-pkcs12")})
    fl = " ".join(flashes(follow(admin, r).text))
    check("Сертификат загружен" in fl, "Сертификат .pfx с русским паролем загружается", fl)

    # ------------------------------------------------------ резервная копия
    r = post(admin, "/settings/backup", {}, page_url="/settings")
    fl = " ".join(flashes(follow(admin, r).text))
    check("Резервная копия создана" in fl, "Резервная копия делается из панели", fl)

    # ---------------------------------------------------------- тестирование
    t0 = time.time()
    r = post(admin, "/testing/run", {}, page_url="/testing")
    took = time.time() - t0
    page = follow(admin, r).text
    check(all(x in page for x in ("Сервер", "Сеть", "Подключения")), f"«Проверить всё»: три группы ({took:.1f} с)")
    check("Что делать" in page, "У жёлтых и красных пунктов есть «Что делать»")
    names = ["КА — основная база", "Ядро Claude", "WB — ИП Иванов", "Бот Telegram", "Битрикс24"]
    check(all(n in page for n in names), "Проверены все подключения — ошибка одного не уронила остальные")
    r = admin.get("/testing/report")
    report = r.content.decode("utf-8-sig")
    check(r.status_code == 200 and "attachment" in r.headers.get("content-disposition", ""), "Отчёт скачивается файлом")
    leaked = [s for s in ("пароль-123", FAKE_CLAUDE_KEY, WB_TOKEN, WEBHOOK_CODE, TG_TOKEN, "GOCSPX-fakeSecretForTests",
                          NEW_ADMIN_PASSWORD) if s in report]
    check(not leaked, "В отчёте нет паролей и токенов", repr(leaked))
    check("СЕРВЕР" in report and "СЕТЬ" in report and "ПОДКЛЮЧЕНИЯ" in report, "В отчёте все три группы")
    (ROOT / "tests" / "_last_report.txt").write_text(report, encoding="utf-8")

    # --------------------------------------------------------------- журнал
    r = admin.get("/journal?level=warning&section=auth")
    check(r.status_code == 200 and "Отказано в доступе" in r.text, "Журнал: фильтр по важности и разделу, видны отказы")
    r = admin.get("/journal?q=" + urllib.parse.quote("Ромашка"))
    check(r.status_code == 200, "Журнал: поиск по русскому слову")

    # ------------------------------------------------------ страницы ошибок
    r = admin.get("/nesushchestvuet")
    check(r.status_code == 404 and "Страница не найдена" in r.text, "404 — человеческая страница")

    ok = sum(1 for c, _ in results if c)
    print(f"\nИтого: {ok} из {len(results)} проверок пройдено")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
