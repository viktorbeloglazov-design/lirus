"""Экран «Тестирование»: проверка сервера, сети и всех подключений разом."""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from . import backup, config, crypto, db, store, tls
from .connectors import BY_KEY, context_for, run_check
from .connectors.base import ERROR, OK, WARN, CheckResult, explain_network_error, ssl_context


def item(title: str, status: str, message: str, action: str = "", details: str = "") -> dict:
    return {"title": title, "status": status, "message": message, "action": action, "details": details}


def _fmt_size(n: float) -> str:
    return f"{n / 1024 ** 3:.1f}".replace(".", ",") + " ГБ"


# -------------------------------------------------------------------- сервер

def check_server(external_date: datetime | None) -> list[dict]:
    items = []
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 11):
        items.append(item("Python", OK, f"Версия {ver}."))
    else:
        items.append(item("Python", ERROR, f"Установлен Python {ver}, нужен 3.11 или новее.",
                          "Установите Python 3.11 или 3.12 с python.org и запустите установить.bat заново."))
    items.append(item("Система", OK, f"{platform.system()} {platform.release()} ({platform.machine()})"))

    usage = shutil.disk_usage(config.BASE_DIR)
    free = usage.free
    if free < 1 * 1024 ** 3:
        items.append(item("Место на диске", ERROR, f"Свободно всего {_fmt_size(free)}.",
                          "Освободите место на диске: удалите старые файлы или расширьте диск виртуальной "
                          "машины. При заполнении диска база может повредиться."))
    elif free < 5 * 1024 ** 3:
        items.append(item("Место на диске", WARN, f"Свободно {_fmt_size(free)} из {_fmt_size(usage.total)}.",
                          "Места мало. Освободите диск или увеличьте его в настройках виртуальной машины."))
    else:
        items.append(item("Место на диске", OK, f"Свободно {_fmt_size(free)} из {_fmt_size(usage.total)}."))

    try:
        with db.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _probe(x)")
            conn.execute("INSERT INTO _probe VALUES(?)", (time.time(),))
            conn.execute("DELETE FROM _probe")
        with db.session() as conn:
            ok = conn.execute("PRAGMA quick_check").fetchone()[0]
        size = config.DB_PATH.stat().st_size / 1024 ** 2
        if ok == "ok":
            items.append(item("База данных", OK, "Запись работает, повреждений нет. Размер " + f"{size:.1f}".replace(".", ",") + " МБ."))
        else:
            items.append(item("База данных", ERROR, "База данных повреждена.",
                              "Остановите платформу и восстановите последнюю резервную копию "
                              "(перетащите её на восстановить-из-копии.bat).", ok))
    except Exception as exc:  # noqa: BLE001
        items.append(item("База данных", ERROR, "Не удаётся записать в базу данных.",
                          "Проверьте, что папка платформы не только для чтения и на диске есть место. "
                          "Если платформа запущена дважды — перезапустите компьютер.", repr(exc)))

    if not crypto.key_exists():
        items.append(item("Ключ шифрования", ERROR, "Файл ключа data\\secret.key не найден.",
                          "Восстановите папку data из резервной копии. Без ключа сохранённые пароли не прочитать."))
    else:
        total, bad = 0, 0
        for c in store.list_connections():
            for key in c.secrets_raw:
                total += 1
                c.secret(key)
            bad += len(c.unreadable)
        if bad:
            items.append(item("Ключ шифрования", ERROR,
                              f"Ключ есть, но {bad} из {total} сохранённых паролей им не читаются.",
                              "Файл data\\secret.key заменён. Верните прежний ключ из резервной копии "
                              "(и перезапустите платформу) или введите пароли в подключениях заново."))
        else:
            items.append(item("Ключ шифрования", OK, f"Ключ на месте, все сохранённые секреты читаются ({total})."))

    now = datetime.now().astimezone()
    tz = now.strftime("%z")
    msg = f"{now:%d.%m.%Y %H:%M:%S}, часовой пояс UTC{tz[:3]}:{tz[3:]}."
    if external_date is not None:
        skew = abs((datetime.now().astimezone() - external_date).total_seconds())
        if skew > 120:
            items.append(item("Время сервера", WARN, msg + f" Часы расходятся с точным временем на {int(skew // 60)} мин.",
                              "Включите синхронизацию: «Параметры → Время и язык → Дата и время → "
                              "Установить время автоматически» и нажмите «Синхронизировать». Из-за неверного "
                              "времени перестают работать вход Google и токены маркетплейсов."))
        else:
            items.append(item("Время сервера", OK, msg + " Часы точные."))
    else:
        items.append(item("Время сервера", OK, msg + " Сверить с интернетом не удалось."))

    last = backup.last_backup_time()
    if last is None:
        items.append(item("Резервные копии", WARN, "Резервных копий ещё нет.",
                          "Нажмите «Сделать копию сейчас» в разделе «Настройки» или запустите "
                          "резервная-копия.bat. Копии хранятся в папке backups — периодически "
                          "переносите их на другой диск."))
    else:
        age_h = (time.time() - last) / 3600
        when = datetime.fromtimestamp(last).strftime("%d.%m.%Y %H:%M")
        if age_h > 24 * 7:
            items.append(item("Резервные копии", WARN, f"Последняя копия — {when}, больше недели назад.",
                              "Проверьте, что платформа работает постоянно (автозапуск), или сделайте "
                              "копию вручную."))
        else:
            items.append(item("Резервные копии", OK, f"Последняя копия — {when}, всего копий: {len(backup.list_backups())}."))

    if os.name == "nt":
        try:
            res = subprocess.run(["schtasks", "/Query", "/TN", config.TASK_NAME], capture_output=True,
                                 timeout=10, creationflags=0x08000000)
            if res.returncode == 0:
                items.append(item("Автозапуск", OK, "Платформа запускается сама при включении сервера."))
            else:
                items.append(item("Автозапуск", WARN, "Автозапуск не включён — после перезагрузки сервера "
                                                      "платформу придётся запускать вручную.",
                                  "Нажмите правой кнопкой на автозапуск-включить.bat → «Запуск от имени "
                                  "администратора»."))
        except Exception as exc:  # noqa: BLE001
            items.append(item("Автозапуск", WARN, "Не удалось проверить автозапуск.", "", repr(exc)))

    default_admins = db.query_one("SELECT COUNT(*) AS n FROM users WHERE must_change_password = 1 "
                                  "AND role = 'admin' AND active = 1 AND login = 'admin'")["n"]
    if default_admins:
        items.append(item("Пароль администратора", ERROR, "У входа admin всё ещё стандартный пароль.",
                          "Войдите как admin и смените пароль — иначе любой войдёт в панель."))
    return items


# ----------------------------------------------------------------------- сеть

SERVICES = [
    ("Claude API", "https://api.anthropic.com/v1/models", True, True),
    ("Wildberries", "https://common-api.wildberries.ru/ping", False, True),
    ("Ozon", "https://api-seller.ozon.ru/", False, True),
    ("Яндекс Маркет", "https://api.partner.market.yandex.ru/", False, False),
    ("Google", "https://oauth2.googleapis.com/", True, True),
    ("MAX", "https://platform-api.max.ru/", False, False),
    ("Telegram", "https://api.telegram.org/", True, False),
]


async def _reach(name: str, url: str, proxy: str) -> tuple[dict, datetime | None]:
    kwargs = {"timeout": 12, "verify": ssl_context(True)}
    if proxy:
        kwargs["proxy"] = proxy
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(url)
    except Exception as exc:  # noqa: BLE001
        err = explain_network_error(exc, url, name, 12, explicit_proxy=bool(proxy))
        via = " (через прокси)" if proxy else ""
        return item(name, ERROR, f"Сервер не достаёт до {name}{via}. {err.message}", err.action, err.details), None
    date = None
    try:
        date = parsedate_to_datetime(resp.headers.get("date", "")).astimezone() if resp.headers.get("date") else None
    except (TypeError, ValueError):
        pass
    if name == "Claude API" and resp.status_code == 403:
        return item(name, ERROR, "Claude API доступен, но отказывает серверам из этой страны.",
                    "Укажите в разделе «Настройки» прокси-сервер в поддерживаемой стране."), date
    return item(name, OK, f"Доступен{' через прокси' if proxy else ''}."), date


def _cert_expiry(host: str, port: int) -> int | None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=8) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ss:
            der = ss.getpeercert(binary_form=True)
    from cryptography import x509

    cert = x509.load_der_x509_certificate(der)
    return (cert.not_valid_after_utc - datetime.now().astimezone()).days


async def check_public_url() -> list[dict]:
    items = []
    public = store.get_setting("public_url").strip()
    if not public:
        return [item("Внешний адрес", WARN, "Внешний адрес панели не указан.",
                     "Укажите в разделе «Настройки» адрес, по которому панель открывается из интернета "
                     "(например https://panel.company.ru). Он нужен для входа Google и для бота.")]
    parts = urlsplit(public)
    host = parts.hostname or ""
    try:
        async with httpx.AsyncClient(timeout=10, verify=ssl_context(True)) as client:
            resp = await client.get(public.rstrip("/") + "/health")
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if data.get("instance") == store.instance_id():
            items.append(item("Внешний адрес", OK, f"Панель открывается по адресу {public}."))
        else:
            items.append(item("Внешний адрес", ERROR, f"По адресу {public} отвечает другой сайт, а не эта панель.",
                              "Проверьте, что домен указывает на внешний IP этого сервера, а на роутере "
                              "порт перенаправлен именно на этот компьютер.", f"HTTP {resp.status_code}"))
    except Exception as exc:  # noqa: BLE001
        err = explain_network_error(exc, public, "панель", 10)
        if "сертификат" in err.message:
            items.append(item("Внешний адрес", ERROR, f"Панель по адресу {public}: {err.message}",
                              "Загрузите действующий сертификат на этот домен в разделе «Настройки → HTTPS».",
                              err.details))
        else:
            items.append(item("Внешний адрес", WARN, f"С самого сервера адрес {public} не открывается.",
                              "Откройте этот адрес с телефона через мобильный интернет. Если там открывается — "
                              "всё в порядке (роутер просто не умеет «петлю»). Если нет — проверьте DNS-запись "
                              "домена, проброс порта на роутере и правило брандмауэра Windows.", err.details))
    if parts.scheme != "https":
        items.append(item("HTTPS", WARN, "Панель открывается без шифрования (http).",
                          "Пароли и токены идут по сети открытым текстом. Загрузите сертификат в разделе "
                          "«Настройки → HTTPS» и укажите внешний адрес с https://."))
    else:
        try:
            days = await asyncio.to_thread(_cert_expiry, host, parts.port or 443)
            if days is None:
                raise ValueError("нет сертификата")
            if days < 14:
                items.append(item("HTTPS", WARN, f"Сертификат истекает через {days} дн.",
                                  "Продлите сертификат и загрузите новый в разделе «Настройки → HTTPS»."))
            else:
                items.append(item("HTTPS", OK, f"HTTPS работает, сертификат действует ещё {days} дн."))
        except Exception as exc:  # noqa: BLE001
            items.append(item("HTTPS", WARN, "Не удалось проверить сертификат по внешнему адресу.",
                              "Проверьте с телефона, что адрес открывается без предупреждений браузера.", repr(exc)))
    if host and tls.is_ip(host) and store.list_connections("google_app"):
        items.append(item("Вход Google по внешнему адресу", WARN,
                          "Внешний адрес — IP, а Google не разрешает вход на IP-адреса.",
                          "Подключайте аккаунты Google, открыв панель на самом сервере по http://localhost:"
                          f"{config.port()}, либо заведите домен."))
    return items


async def check_network() -> tuple[list[dict], datetime | None]:
    proxy = store.get_setting("proxy_url")
    tasks = [_reach(name, url, proxy if foreign else "") for name, url, foreign, _ in SERVICES]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    items, date = [], None
    for (name, _url, _foreign, required), res in zip(SERVICES, results):
        if isinstance(res, BaseException):
            items.append(item(name, WARN, "Проверка не выполнилась.", "", repr(res)))
            continue
        it, d = res
        if it["status"] == ERROR and not required:
            it["status"] = WARN
        items.append(it)
        date = date or d
    items += await check_public_url()
    return items, date


# --------------------------------------------------------------- подключения

async def check_one(conn) -> CheckResult:
    ctype = BY_KEY.get(conn.type)
    if ctype is None:
        return CheckResult(ERROR, "Неизвестный тип подключения.", "Удалите это подключение.")
    values = conn.values()
    if conn.unreadable:
        return CheckResult(ERROR, "Сохранённые пароли этого подключения не читаются ключом шифрования.",
                           "Откройте подключение и введите пароли и токены заново.")
    return await run_check(ctype, values, context_for(ctype, conn.id))


async def check_connections(record: bool = True) -> list[dict]:
    conns = [c for c in store.list_connections() if c.enabled]
    if not conns:
        return [item("Подключения", WARN, "Ни одного подключения ещё нет.",
                     "Откройте раздел «Подключения» и добавьте первое: ядро Claude, 1С, бот.")]
    results = await asyncio.gather(*(check_one(c) for c in conns), return_exceptions=True)
    items = []
    for conn, res in zip(conns, results):
        if isinstance(res, BaseException):
            res = CheckResult(ERROR, "Проверка завершилась сбоем.",
                              "Передайте отчёт разработчику.", repr(res))
        ctype = BY_KEY.get(conn.type)
        type_title = ctype.title if ctype else conn.type
        title = type_title if (not conn.name or conn.name == type_title) else f"{type_title}: {conn.name}"
        if record:
            store.set_connection_status(conn.id, res.status, res.message, res.action)
            if res.status != conn.status and res.status != OK:
                store.log_event("error" if res.status == ERROR else "warning", "connections",
                                f"«{title}»: {res.message}", scrub(res.details, [v for v in conn.values().values()
                                                                                   if isinstance(v, str)]))
        it = item(title, res.status, res.message, res.action, res.details)
        it["connection_id"] = conn.id
        items.append(it)
    return items


# ---------------------------------------------------------------- всё сразу

async def run_all() -> dict:
    started = time.time()
    (net_items, ext_date), conn_items = await asyncio.gather(check_network(), check_connections())
    server_items = await asyncio.to_thread(check_server, ext_date)
    groups = [
        {"key": "server", "title": "Сервер", "items": server_items},
        {"key": "network", "title": "Сеть", "items": net_items},
        {"key": "connections", "title": "Подключения", "items": conn_items},
    ]
    summary = {OK: 0, WARN: 0, ERROR: 0}
    for g in groups:
        for it in g["items"]:
            summary[it["status"]] = summary.get(it["status"], 0) + 1
        g["status"] = max((it["status"] for it in g["items"]), key={OK: 0, WARN: 1, ERROR: 2}.get, default=OK)
    return {"ts": time.time(), "duration": round(time.time() - started, 1), "groups": groups,
            "summary": summary}


# --------------------------------------------------------------------- отчёт

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"\b\d{5,15}:[A-Za-z0-9_\-]{30,}\b"),            # токен Telegram
    re.compile(r"/rest/\d+/[A-Za-z0-9]+"),                       # вебхук Битрикс24
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWT
    re.compile(r"(?i)(bearer|basic)\s+[A-Za-z0-9+/=._\-]{8,}"),
    re.compile(r"ya29\.[A-Za-z0-9_\-.]+"),                       # токен доступа Google
    re.compile(r"1//[A-Za-z0-9_\-]{20,}"),                       # refresh token Google
    re.compile(r"GOCSPX-[A-Za-z0-9_\-]+"),
    re.compile(r"ACMA:[A-Za-z0-9:_\-]+"),
    re.compile(r"(?i)(://[^:/\s]+:)[^@/\s]+@"),                  # пароль в адресе прокси
]


def scrub(text: str, secrets: list[str]) -> str:
    for value in sorted(set(secrets), key=len, reverse=True):
        if value and len(value) >= 4:
            text = text.replace(value, "***")
    for pat in _PATTERNS:
        if pat.pattern.startswith("(?i)(://"):
            text = pat.sub(r"\1***@", text)
        else:
            text = pat.sub("***", text)
    return text


STATUS_WORDS = {OK: "РАБОТАЕТ", WARN: "ВНИМАНИЕ", ERROR: "ОШИБКА"}


def report_text(result: dict) -> str:
    lines = [
        f"Отчёт о проверке — {config.APP_NAME} {config.APP_VERSION}",
        f"Время проверки: {datetime.fromtimestamp(result['ts']):%d.%m.%Y %H:%M:%S} "
        f"(заняла {result.get('duration', '?')} с), запустил: {result.get('user', '')}",
        f"Python {sys.version.split()[0]}, {platform.platform()}",
        f"Порт панели: {config.port()}, внешний адрес: {store.get_setting('public_url') or 'не указан'}",
        f"Прокси: {'задан' if store.get_setting('proxy_url') else 'не задан'}",
        "Итого: работает {ok}, внимание {warn}, ошибок {error}".format(
            ok=result["summary"].get(OK, 0), warn=result["summary"].get(WARN, 0),
            error=result["summary"].get(ERROR, 0)),
        "",
        "Пароли, токены и ключи в этот отчёт не попадают.",
        "",
    ]
    for group in result["groups"]:
        lines.append("=" * 70)
        lines.append(group["title"].upper())
        lines.append("=" * 70)
        for it in group["items"]:
            lines.append(f"[{STATUS_WORDS.get(it['status'], it['status'])}] {it['title']}")
            lines.append(f"    {it['message']}")
            if it.get("action"):
                lines.append(f"    Что делать: {it['action']}")
            if it.get("details"):
                lines.append(f"    Подробности: {it['details']}")
        lines.append("")
    conns = store.list_connections()
    if conns:
        lines.append("=" * 70)
        lines.append("СПИСОК ПОДКЛЮЧЕНИЙ (без секретов)")
        lines.append("=" * 70)
        for c in conns:
            safe_cfg = {k: v for k, v in c.config.items()}
            lines.append(f"#{c.id} {c.type} «{c.name}» {'включено' if c.enabled else 'выключено'}; "
                         f"поля: {json.dumps(safe_cfg, ensure_ascii=False)}; "
                         f"секреты заданы: {', '.join(sorted(c.secrets_raw)) or 'нет'}")
        lines.append("")
    errors, _ = store.list_events(level="error", limit=20)
    if errors:
        lines.append("=" * 70)
        lines.append("ПОСЛЕДНИЕ ОШИБКИ ИЗ ЖУРНАЛА")
        lines.append("=" * 70)
        for e in errors:
            lines.append(f"{datetime.fromtimestamp(e['ts']):%d.%m.%Y %H:%M} [{e['section']}] {e['message']} {e['details']}")
    text = "\n".join(lines)
    return scrub(text, store.all_secret_values())
