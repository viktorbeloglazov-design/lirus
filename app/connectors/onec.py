"""1С:Предприятие — стандартный интерфейс OData и HTTP-сервисы расширения."""
from __future__ import annotations

from .base import (ERROR, OK, WARN, CheckContext, CheckResult, ConnectorError, ConnectorType, Field,
                   basic_auth, details_for, http_request, json_or_none, short_body, worst)

SERVICE = "1С"

ACTION_AUTH = (
    "Проверьте имя пользователя и пароль — те же, что вводятся при входе в 1С. "
    "В карточке пользователя 1С должна стоять галочка «Аутентификация 1С:Предприятия», "
    "а сам пользователь — не быть отключён."
)
ACTION_ODATA_RIGHTS = (
    "В 1С откройте «Администрирование → Синхронизация данных → Настройка стандартного интерфейса "
    "OData» и разрешите доступ этому пользователю (или создайте отдельного). Там же отметьте, "
    "какие справочники, документы и регистры отдавать."
)
ACTION_PUBLISH_ODATA = (
    "На сервере 1С откройте Конфигуратор → «Администрирование → Публикация на веб-сервере», "
    "поставьте галочку «Публиковать стандартный интерфейс OData», нажмите «Опубликовать» "
    "и перезапустите веб-сервер (IIS или Apache)."
)
ACTION_PUBLISH_HS = (
    "Проверьте, что расширение установлено и включено для этой базы, а в Конфигураторе → "
    "«Администрирование → Публикация на веб-сервере» стоит галочка «Публиковать HTTP-сервисы "
    "расширений по умолчанию». После изменения нажмите «Опубликовать» и перезапустите веб-сервер."
)


def _explain_server_error(resp) -> ConnectorError:
    body = short_body(resp, 600).lower()
    if "лиценз" in body or "licen" in body:
        return ConnectorError(
            "1С отвечает, но не хватает свободной лицензии.",
            "Освободите лицензию (закройте лишние сеансы в консоли кластера) или докупите лицензии.",
            details_for(resp))
    if resp.status_code in (502, 503, 504):
        return ConnectorError(
            "Веб-сервер работает, но не может связаться с сервером 1С.",
            "Проверьте на сервере 1С, что служба «Агент сервера 1С:Предприятия» запущена "
            "и база не заблокирована (регламентные операции, обновление).",
            details_for(resp))
    return ConnectorError(
        "1С вернула внутреннюю ошибку.",
        "Откройте 1С обычным способом и проверьте, что база работает. Если в 1С всё в порядке — "
        "скачайте отчёт в разделе «Тестирование» и передайте разработчику.",
        details_for(resp))


async def _probe_base(base: str, headers: dict, verify: bool, ctx: CheckContext) -> int:
    resp = await http_request("GET", base + "/", ctx, service=SERVICE, headers=headers, verify=verify,
                              timeout=20)
    return resp.status_code


async def _check_odata(base: str, headers: dict, verify: bool, ctx: CheckContext) -> CheckResult:
    url = base + "/odata/standard.odata/?$format=json"
    resp = await http_request("GET", url, ctx, service=SERVICE, headers=headers, verify=verify, timeout=40)
    code = resp.status_code
    if code == 200:
        data = json_or_none(resp)
        if not isinstance(data, dict) or "value" not in data:
            return CheckResult(
                ERROR, "По этому адресу отвечает не OData 1С (пришла обычная веб-страница).",
                "Проверьте адрес базы: он должен совпадать с адресом, по которому открывается 1С "
                "в браузере, например https://192.168.1.10/ka — без /ru_RU и других хвостов.",
                details_for(resp))
        count = len(data.get("value") or [])
        if count == 0:
            return CheckResult(
                WARN, "OData включён, но в нём не выбран ни один справочник или документ.",
                "В 1С откройте «Администрирование → Синхронизация данных → Настройка стандартного "
                "интерфейса OData» и отметьте нужные объекты (номенклатура, контрагенты, заказы, "
                "остатки и т.д.).", details_for(resp))
        return CheckResult(OK, f"OData работает: доступно объектов — {count}.")
    if code == 401:
        raise ConnectorError("1С не пустила: неверное имя пользователя или пароль.", ACTION_AUTH,
                             details_for(resp))
    if code == 403:
        raise ConnectorError("Пользователь вошёл в 1С, но у него нет прав на OData.", ACTION_ODATA_RIGHTS,
                             details_for(resp))
    if code == 404:
        base_code = await _probe_base(base, headers, verify, ctx)
        if base_code == 404:
            raise ConnectorError(
                "По этому адресу нет опубликованной базы 1С.",
                "Проверьте имя публикации в конце адреса (например, /ka) — оно должно совпадать с тем, "
                "по которому 1С открывается в браузере. Регистр букв важен.", details_for(resp))
        raise ConnectorError("1С отвечает, но интерфейс OData не опубликован.", ACTION_PUBLISH_ODATA,
                             details_for(resp))
    if code in (301, 302, 307, 308):
        location = resp.headers.get("location", "")
        raise ConnectorError(
            "1С перенаправляет запрос на другой адрес.",
            f"Укажите в поле «Адрес базы» итоговый адрес{': ' + location if location else ''}. "
            "Часто это значит, что нужно заменить http:// на https://.", details_for(resp))
    if code >= 500:
        raise _explain_server_error(resp)
    raise ConnectorError(f"1С ответила неожиданно (код {code}).",
                         "Скачайте отчёт в разделе «Тестирование» и передайте разработчику.",
                         details_for(resp))


async def _check_hs(base: str, path: str, headers: dict, verify: bool, ctx: CheckContext) -> CheckResult:
    url = base + path
    resp = await http_request("GET", url, ctx, service=SERVICE, headers=headers, verify=verify, timeout=30)
    code = resp.status_code
    if code in (200, 204, 405):
        return CheckResult(OK, f"HTTP-сервис {path} отвечает.")
    if code == 401:
        raise ConnectorError("1С не пустила к HTTP-сервису: неверное имя пользователя или пароль.",
                             ACTION_AUTH, details_for(resp))
    if code == 403:
        return CheckResult(WARN, f"HTTP-сервис {path} есть, но у пользователя нет прав на него.",
                           "Добавьте пользователю роль из расширения (её название — в инструкции "
                           "к расширению).", details_for(resp))
    if code == 404:
        return CheckResult(WARN, f"HTTP-сервис {path} не найден.", ACTION_PUBLISH_HS, details_for(resp))
    if code >= 500:
        err = _explain_server_error(resp)
        return CheckResult(WARN, "HTTP-сервис: " + err.message, err.action, err.details)
    return CheckResult(WARN, f"HTTP-сервис ответил неожиданно (код {code}).",
                       "Проверьте путь к сервису.", details_for(resp))


async def check(values: dict, ctx: CheckContext) -> CheckResult:
    base = (values.get("base_url") or "").rstrip("/")
    verify = bool(values.get("verify_ssl", True))
    headers = {"Authorization": basic_auth(values.get("username", ""), values.get("password", "")),
               "Accept": "application/json"}
    results: list[CheckResult] = []
    if values.get("use_odata", True):
        results.append(await _check_odata(base, headers, verify, ctx))
    path = (values.get("http_service_path") or "").strip()
    if path:
        if not path.startswith("/"):
            path = "/" + path
        results.append(await _check_hs(base, path, headers, verify, ctx))
    if not results:
        code = await _probe_base(base, headers, verify, ctx)
        if code == 401:
            raise ConnectorError("1С не пустила: неверное имя пользователя или пароль.", ACTION_AUTH)
        return CheckResult(WARN, f"Сервер 1С отвечает (код {code}), но не выбран ни один способ обмена.",
                           "Поставьте галочку «Использовать OData» или укажите путь к HTTP-сервису.")
    status = worst(*[r.status for r in results])
    message = " ".join(r.message for r in results)
    if not verify:
        message += " Проверка сертификата отключена."
    action = " ".join(r.action for r in results if r.action)
    details = " | ".join(r.details for r in results if r.details)
    return CheckResult(status, message, action, details)


CONNECTOR = ConnectorType(
    key="onec",
    title="1С:Предприятие",
    category="Учёт",
    description="Учётная база 1С (Комплексная автоматизация, УТ, ERP и др.), опубликованная на "
                "веб-сервере. Отсюда платформа берёт остатки, продажи, заказы, взаиморасчёты.",
    finance=True,
    name_placeholder="Например: КА — основная база",
    check=check,
    fields=[
        Field("base_url", "Адрес базы", "url", required=True,
              placeholder="https://192.168.1.10/ka",
              hint="Тот же адрес, по которому 1С открывается в браузере (тонкий клиент), "
                   "без /ru_RU/ и других хвостов."),
        Field("username", "Пользователь 1С", required=True, placeholder="Робот-интеграция",
              hint="Лучше завести в 1С отдельного пользователя только для платформы. "
                   "Русские буквы в имени допустимы."),
        Field("password", "Пароль", "secret",
              hint="Пароль этого пользователя. Если пароля нет — оставьте пустым."),
        Field("use_odata", "Использовать OData", "checkbox", default=True,
              hint="Стандартный интерфейс OData: чтение справочников, документов и регистров. "
                   "Включается галочкой при публикации базы."),
        Field("http_service_path", "Путь к HTTP-сервису", placeholder="/hs/platform/ping",
              hint="Необязательно. Адрес метода HTTP-сервиса из расширения, начиная с /hs/. "
                   "Понадобится на следующем этапе."),
        Field("verify_ssl", "Проверять сертификат", "checkbox", default=True,
              hint="Снимите галочку, если 1С открывается по IP-адресу и браузер ругается на "
                   "сертификат (самоподписанный). Когда появится нормальный сертификат на домен — "
                   "поставьте обратно."),
    ],
    help_steps=[
        "Адрес базы — откройте 1С в браузере и скопируйте адрес из адресной строки до названия "
        "публикации включительно, например https://192.168.1.10/ka.",
        "В 1С: «Администрирование → Настройки пользователей и прав → Пользователи» создайте "
        "пользователя «Робот-интеграция» с паролем и галочкой «Аутентификация 1С:Предприятия».",
        "В Конфигураторе: «Администрирование → Публикация на веб-сервере» — отметьте «Публиковать "
        "стандартный интерфейс OData» и «Публиковать HTTP-сервисы расширений по умолчанию», "
        "нажмите «Опубликовать».",
        "В 1С: «Администрирование → Синхронизация данных → Настройка стандартного интерфейса OData» — "
        "дайте доступ пользователю и отметьте нужные объекты.",
    ],
)


# ------------------------------------------------------------ запросы OData
# Общие функции для обозревателя 1С в панели и для узла «1С» в сценариях.

from urllib.parse import quote as _quote  # noqa: E402

# Скобки, запятые и кавычки — часть синтаксиса OData: 1С ждёт их как есть.
ODATA_SAFE = "/()',=$:"


def odata_headers(values: dict) -> dict:
    return {"Authorization": basic_auth(values.get("username", ""), values.get("password", "")),
            "Accept": "application/json"}


def odata_url(values: dict, entity: str = "") -> str:
    base = (values.get("base_url") or "").rstrip("/")
    return base + "/odata/standard.odata/" + _quote(entity.strip().lstrip("/"), safe=ODATA_SAFE)


def odata_error_text(resp) -> str:
    """Текст ошибки, который вернула сама 1С (в odata.error), если он есть."""
    data = json_or_none(resp)
    if isinstance(data, dict):
        err = data.get("odata.error") or data.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else None
        if isinstance(msg, dict):
            msg = msg.get("value")
        if msg:
            return str(msg).strip()
    return ""


def odata_raise(resp, entity: str = "") -> None:
    """Понятная ошибка по ответу 1С. Ничего не делает, если ответ успешный."""
    code = resp.status_code
    if code < 400:
        return
    said = odata_error_text(resp)
    tail = f" 1С пишет: «{said}»" if said else ""
    if code == 401:
        raise ConnectorError("1С не пустила: неверное имя пользователя или пароль." + tail, ACTION_AUTH, details_for(resp))
    if code == 403:
        raise ConnectorError(f"У пользователя 1С нет прав на объект {entity or ''}.".replace(" .", ".") + tail,
                             ACTION_ODATA_RIGHTS, details_for(resp))
    if code == 404:
        raise ConnectorError(
            f"1С не нашла объект «{entity}»." + tail if entity else "1С не нашла такой адрес." + tail,
            "Проверьте имя объекта: регистр букв важен, объект должен быть отмечен в «Настройке стандартного "
            "интерфейса OData». Список доступных объектов — в «Обозревателе 1С» на странице подключения.",
            details_for(resp))
    if code == 400:
        raise ConnectorError("1С не поняла запрос." + tail,
                             "Проверьте отбор ($filter) и список полей ($select). Даты в отборе пишутся так: "
                             "Date gt datetime'2026-01-01T00:00:00', строки — в одинарных кавычках.",
                             details_for(resp))
    if code >= 500:
        err = _explain_server_error(resp)
        raise ConnectorError(err.message + tail, err.action, err.details)
    raise ConnectorError(f"1С ответила неожиданно (код {code})." + tail, "", details_for(resp))


async def odata_entities(values: dict, ctx: CheckContext) -> list[str]:
    """Список объектов, которые 1С отдаёт через OData."""
    resp = await http_request("GET", odata_url(values) + "?$format=json", ctx, service=SERVICE,
                              headers=odata_headers(values), verify=bool(values.get("verify_ssl", True)), timeout=60)
    odata_raise(resp)
    data = json_or_none(resp)
    if not isinstance(data, dict) or "value" not in data:
        raise ConnectorError("По этому адресу отвечает не OData 1С.",
                             "Проверьте адрес базы в подключении.", details_for(resp))
    names = [str(x.get("name") or x.get("url") or "") for x in data.get("value", []) if isinstance(x, dict)]
    return sorted(n for n in names if n)


async def odata_query(values: dict, ctx: CheckContext, entity: str, params: dict | None = None,
                      top: int = 100, all_pages: bool = False, page_size: int = 500,
                      max_records: int = 100000) -> list[dict]:
    """Записи объекта 1С. С all_pages забирает всё порциями (1С не любит огромные ответы)."""
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    params["$format"] = "json"
    headers = odata_headers(values)
    verify = bool(values.get("verify_ssl", True))
    url = odata_url(values, entity)
    records: list[dict] = []
    skip = 0
    while True:
        batch = page_size if all_pages else top
        page = dict(params, **{"$top": str(batch)})
        if skip:
            page["$skip"] = str(skip)
        resp = await http_request("GET", url, ctx, service=SERVICE, headers=headers, verify=verify,
                                  params=page, timeout=180)
        odata_raise(resp, entity)
        data = json_or_none(resp)
        if isinstance(data, dict) and "value" in data:
            chunk = [x for x in data.get("value", []) if isinstance(x, dict)]
        elif isinstance(data, dict):
            chunk = [data]  # запрос одного объекта по ссылке
        else:
            raise ConnectorError("1С вернула не JSON.", "Проверьте, что в адресе нет лишнего.", details_for(resp))
        records.extend(chunk)
        if not all_pages or len(chunk) < batch or len(records) >= max_records:
            return records[:max_records]
        skip += batch


def odata_key(guid: str) -> str:
    """Ссылка на объект в формате OData 1С: (guid'…')."""
    return f"(guid'{guid.strip()}')"
