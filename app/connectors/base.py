"""Общая основа подключений.

Тип подключения описывается одним объектом ConnectorType: список полей,
подсказки «где взять» и функция проверки связи. Форма в панели строится
из этого описания автоматически.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import ssl
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger(__name__)

OK, WARN, ERROR = "ok", "warn", "error"
STATUS_TITLES = {OK: "Работает", WARN: "Внимание", ERROR: "Ошибка", "new": "Не проверялось"}
_RANK = {OK: 0, WARN: 1, ERROR: 2}


def worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: _RANK.get(s, 0)) if statuses else OK


@dataclass
class Field:
    name: str
    label: str
    kind: str = "text"  # text | secret | url | checkbox | select | number | textarea
    hint: str = ""
    required: bool = False
    placeholder: str = ""
    default: Any = None
    options: list[tuple[str, str]] = field(default_factory=list)
    ascii_only: bool = False  # токены и ключи: только латиница, пробелы вырезаются
    in_form: bool = True      # False — поле заполняется программой (например, после входа в Google)

    @property
    def secret(self) -> bool:
        return self.kind == "secret"


@dataclass
class CheckResult:
    status: str
    message: str
    action: str = ""
    details: str = ""  # технические подробности — только для отчёта разработчику


class ConnectorError(Exception):
    def __init__(self, message: str, action: str = "", details: str = "", status: str = ERROR):
        super().__init__(message)
        self.message, self.action, self.details, self.status = message, action, details, status

    def result(self) -> CheckResult:
        return CheckResult(self.status, self.message, self.action, self.details)


@dataclass
class CheckContext:
    """То, что проверке нужно помимо значений полей."""
    proxy: str = ""
    connection_id: int | None = None
    transport: httpx.AsyncBaseTransport | None = None  # подменяется в тестах


CheckFunc = Callable[[dict, CheckContext], Awaitable[CheckResult]]


@dataclass
class ConnectorType:
    key: str
    title: str
    category: str
    description: str
    fields: list[Field]
    check: CheckFunc
    help_title: str = "Где взять данные"
    help_steps: list[str] = field(default_factory=list)
    singleton: bool = False     # может быть только одно подключение такого типа
    finance: bool = False       # даёт доступ к финансовым данным
    foreign: bool = False       # зарубежный сервис — ходим через прокси из настроек
    oauth: bool = False         # подключается кнопкой «Войти через Google»
    name_label: str = "Название"
    name_hint: str = ""
    name_placeholder: str = ""
    validate: Callable[[dict], dict[str, str]] | None = None  # доп. проверка формы

    def field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def form_fields(self) -> list[Field]:
        return [f for f in self.fields if f.in_form]


# ---------------------------------------------------------- чистка значений

_INVISIBLE = "            ​‌‍⁠﻿  　"
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")


def clean_token(value: str, label: str) -> str:
    """Вырезает пробелы и переносы; если остались русские буквы — понятная ошибка."""
    cleaned = "".join(ch for ch in value if not ch.isspace() and ch not in _INVISIBLE)
    bad = [ch for ch in cleaned if ord(ch) > 126 or ord(ch) < 33]
    if bad:
        uniq = []
        for ch in bad:
            if ch not in uniq:
                uniq.append(ch)
        shown = ", ".join(f"«{ch}»" for ch in uniq[:5])
        if any(_CYRILLIC.match(ch) for ch in uniq):
            raise ValueError(
                f"В поле «{label}» попали русские буквы: {shown}. В ключах и токенах бывают только "
                "латинские буквы, цифры и знаки. Скорее всего, при копировании захватился лишний "
                "текст или была включена русская раскладка. Скопируйте значение заново."
            )
        raise ValueError(
            f"В поле «{label}» есть недопустимые символы: {shown}. Скопируйте значение заново "
            "прямо со страницы, где его выдали, без кавычек и пояснений."
        )
    return cleaned


def normalize_url(value: str, label: str) -> str:
    value = value.strip().strip("\"'«»").replace("\\", "/")
    if not value:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"«{label}» должен начинаться с https:// или http://.")
    if not parts.hostname:
        raise ValueError(f"В «{label}» не указан адрес сервера. Пример: https://192.168.1.10/ka")
    if " " in value:
        raise ValueError(f"В «{label}» есть пробелы — адрес пишется без пробелов.")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), parts.query, ""))


def parse_number(value: str, label: str) -> float | None:
    value = value.strip().replace(" ", "").replace(" ", "").replace(",", ".").lstrip("$")
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        raise ValueError(f"В «{label}» нужно число, например 100 или 49,90.") from None
    if number < 0:
        raise ValueError(f"«{label}» не может быть отрицательным.")
    return number


def validate_form(ctype: ConnectorType, form: dict, existing=None) -> tuple[str, dict, dict, dict]:
    """Разбирает форму. Возвращает (название, обычные поля, новые секреты, ошибки).

    Пустое поле секрета при редактировании означает «не менять»."""
    errors: dict[str, str] = {}
    cfg: dict[str, Any] = {}
    secrets: dict[str, str] = {}
    name = str(form.get("name", "") or "").strip()
    for f in ctype.fields:
        if not f.in_form:
            if existing is not None and f.name in existing.config:
                cfg[f.name] = existing.config[f.name]
            continue
        raw = form.get(f.name)
        try:
            if f.kind == "checkbox":
                cfg[f.name] = raw in ("1", "on", "true", "yes")
                continue
            value = str(raw or "")
            if f.kind == "number":
                num = parse_number(value, f.label)
                if num is None and f.required:
                    raise ValueError(f"Заполните поле «{f.label}».")
                cfg[f.name] = num if num is not None else f.default
                continue
            if f.kind == "url":
                value = normalize_url(value, f.label)
            elif f.ascii_only:
                value = clean_token(value, f.label)
            else:
                value = value.strip()
            if f.kind == "select" and f.options and value not in {o[0] for o in f.options}:
                value = f.default or f.options[0][0]
            if f.secret:
                if value:
                    secrets[f.name] = value
                elif f.required and not (existing is not None and existing.has_secret(f.name)):
                    raise ValueError(f"Заполните поле «{f.label}».")
                continue
            if f.required and not value:
                raise ValueError(f"Заполните поле «{f.label}».")
            cfg[f.name] = value
        except ValueError as exc:
            errors[f.name] = str(exc)
    if not errors and ctype.validate:
        merged = dict(cfg)
        merged.update(secrets)
        errors.update(ctype.validate(merged) or {})
        for key in cfg:
            cfg[key] = merged.get(key, cfg[key])
        for key in secrets:
            secrets[key] = merged.get(key, secrets[key])
    return name, cfg, secrets, errors


# ------------------------------------------------------------- HTTP-запросы

def ssl_context(verify: bool = True) -> ssl.SSLContext | bool:
    if not verify:
        return False
    ctx = ssl.create_default_context()  # на Windows — хранилище сертификатов системы
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except Exception:  # certifi необязателен
        pass
    return ctx


def basic_auth(user: str, password: str) -> str:
    """Basic-авторизация в UTF-8: имена пользователей 1С бывают русскими."""
    return "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


def _chain_text(exc: BaseException) -> str:
    """Текст ошибки вместе с причинами — errno часто лежит во вложенной ошибке."""
    parts, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        parts.append(f"{type(exc).__name__}: {exc}")
        exc = exc.__cause__ or exc.__context__
    return " <- ".join(parts)


def explain_network_error(exc: Exception, url: str, service: str, timeout: float,
                          explicit_proxy: bool = False) -> ConnectorError:
    host = _host(url)
    text = _chain_text(exc)
    low = text.lower()
    if isinstance(exc, UnicodeEncodeError):
        return ConnectorError(
            "В ключе или токене есть русские буквы или другие недопустимые символы.",
            "Откройте подключение, вставьте ключ заново (скопировав его со страницы, где его выдали) "
            "и сохраните.", text)
    if isinstance(exc, httpx.ProxyError) or ("proxy" in low and "connect" in low):
        if explicit_proxy:
            return ConnectorError(
                f"Не удалось пройти через прокси-сервер к {service}.",
                "Проверьте адрес, логин и пароль прокси в разделе «Настройки» или очистите это поле, "
                "если прокси не нужен.", text)
        return ConnectorError(
            f"Системный прокси-сервер не пропускает запросы к {service}.",
            "На этом компьютере в Windows включён прокси («Параметры → Сеть и Интернет → Прокси»). "
            f"Разрешите на нём доступ к {host} или отключите прокси для сервера платформы.", text)
    if isinstance(exc, httpx.TimeoutException):
        return ConnectorError(
            f"{service} не ответил за {int(timeout)} секунд.",
            f"Проверьте, что сервер {host} включён и доступен с этого компьютера. Если адрес "
            "внутренний — сервер платформы должен быть в той же сети или подключён к ней через VPN.",
            text)
    if isinstance(exc, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        return ConnectorError(f"Адрес {service} указан неверно.",
                              "Проверьте адрес: он должен начинаться с https:// и не содержать пробелов.", text)
    if any(s in low for s in ("certificate_verify_failed", "certificate verify failed",
                              "self signed", "self-signed", "hostname mismatch", "ip address mismatch",
                              "certificate has expired", "unable to get local issuer")):
        if "expired" in low:
            reason = "у сертификата сервера истёк срок действия"
        elif "mismatch" in low:
            reason = "сертификат выдан на другое имя, а не на этот адрес"
        else:
            reason = "сертификат сервера не подтверждён (самоподписанный или выдан неизвестным центром)"
        return ConnectorError(
            f"{service} отвечает, но {reason}.",
            "Если сервер открывается по IP-адресу с самоподписанным сертификатом — снимите галочку "
            "«Проверять сертификат» (если она есть в форме). Правильное решение — выпустить "
            "нормальный сертификат на доменное имя.", text)
    if "wrong version number" in low or "record layer failure" in low or "packet length too long" in low:
        return ConnectorError(
            f"По адресу {host} работает обычный http, а в адресе указан https.",
            "Замените в адресе https:// на http:// (или включите https на сервере).", text)
    if any(s in low for s in ("name or service not known", "getaddrinfo failed", "nodename nor servname",
                              "errno -2", "errno -3", "errno 11001", "errno 11004",
                              "temporary failure in name resolution", "no address associated")):
        return ConnectorError(
            f"Адрес {host} не найден в интернете.",
            "Проверьте, нет ли опечатки в адресе. Если адрес верный — проверьте, что у сервера "
            "есть доступ в интернет и работает DNS.", text)
    if any(s in low for s in ("connection refused", "errno 111", "10061", "actively refused")):
        return ConnectorError(
            f"Сервер {host} отказал в соединении — на этом порту ничего не работает.",
            "Проверьте номер порта в адресе и что веб-сервер (например, IIS или Apache для 1С) запущен.",
            text)
    if any(s in low for s in ("network is unreachable", "no route to host", "errno 101", "errno 113",
                              "10051", "10065")):
        return ConnectorError(
            f"Нет сетевого пути до {host}.",
            "Проверьте подключение сервера к сети/интернету и настройки брандмауэра.", text)
    if isinstance(exc, httpx.RemoteProtocolError):
        return ConnectorError(
            f"{service} оборвал соединение.",
            "Возможно, указан неверный порт или вместо https нужен http. Проверьте адрес.", text)
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return ConnectorError(
            f"Не удаётся соединиться с {host}.",
            "Проверьте адрес, доступ сервера в интернет и брандмауэр. Если сервис зарубежный и "
            "заблокирован — укажите прокси в разделе «Настройки».", text)
    return ConnectorError(f"Не удалось обратиться к {service}.",
                          "Повторите проверку через минуту. Если ошибка повторяется — скачайте отчёт "
                          "в разделе «Тестирование» и передайте разработчику.", text)


async def http_request(method: str, url: str, ctx: CheckContext, *, service: str,
                       headers: dict | None = None, verify: bool = True, timeout: float = 20,
                       use_proxy: bool = False, **kwargs) -> httpx.Response:
    """HTTP-запрос, при сбое сети — ConnectorError с понятным текстом."""
    client_kwargs: dict[str, Any] = {"timeout": timeout, "verify": ssl_context(verify),
                                     "follow_redirects": False}
    if ctx.transport is not None:
        client_kwargs["transport"] = ctx.transport
    elif use_proxy and ctx.proxy:
        client_kwargs["proxy"] = ctx.proxy
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            return await client.request(method, url, headers=headers, **kwargs)
    except ConnectorError:
        raise
    except Exception as exc:  # noqa: BLE001 — любую ошибку переводим на русский
        raise explain_network_error(exc, url, service, timeout,
                                    explicit_proxy=bool(use_proxy and ctx.proxy)) from exc


def json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None


def short_body(resp: httpx.Response, limit: int = 300) -> str:
    try:
        text = resp.text
    except Exception:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def details_for(resp: httpx.Response) -> str:
    return f"HTTP {resp.status_code} {resp.request.method} {_host(str(resp.request.url))}: {short_body(resp)}"


async def run_check(ctype: ConnectorType, values: dict, ctx: CheckContext, timeout: float = 45) -> CheckResult:
    """Запускает проверку так, чтобы никакая ошибка не вылетела наружу."""
    try:
        return await asyncio.wait_for(ctype.check(values, ctx), timeout=timeout)
    except ConnectorError as exc:
        return exc.result()
    except asyncio.TimeoutError:
        return CheckResult(ERROR, f"Проверка «{ctype.title}» не уложилась в {int(timeout)} секунд.",
                           "Сервис отвечает слишком медленно. Повторите проверку позже.")
    except UnicodeEncodeError as exc:
        return explain_network_error(exc, "", ctype.title, timeout).result()
    except Exception as exc:  # noqa: BLE001
        log.exception("Сбой проверки %s", ctype.key)
        return CheckResult(ERROR, "Внутренняя ошибка при проверке.",
                           "Скачайте отчёт в разделе «Тестирование» и передайте разработчику.",
                           f"{type(exc).__name__}: {exc}")
