"""Почта SMTP: отправка писем через Яндекс, Mail.ru, корпоративный сервер."""
from __future__ import annotations

import asyncio
import smtplib
import socket
import ssl
from email.message import EmailMessage

from .base import OK, CheckContext, CheckResult, ConnectorError, ConnectorType, Field

ACTION_APP_PASSWORD = (
    "Для Яндекс Почты и Mail.ru нужен не обычный пароль, а «пароль приложения»: Яндекс ID → Безопасность → "
    "Пароли приложений (для Mail.ru — Настройки → Безопасность → Пароли для внешних приложений). "
    "В настройках почтового ящика должен быть разрешён доступ по протоколу SMTP."
)


def _open(values: dict) -> smtplib.SMTP:
    host = values.get("host", "")
    port = int(values.get("port") or 465)
    security = values.get("security") or "ssl"
    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.ehlo()
            if security == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
        if values.get("username"):
            server.login(values.get("username", ""), values.get("password", ""))
        return server
    except smtplib.SMTPAuthenticationError as exc:
        raise ConnectorError("Почтовый сервер не принял логин или пароль.", ACTION_APP_PASSWORD, repr(exc)) from exc
    except (socket.gaierror,) as exc:
        raise ConnectorError(f"Почтовый сервер {host} не найден.", "Проверьте адрес: smtp.yandex.ru, smtp.mail.ru…",
                             repr(exc)) from exc
    except ssl.SSLError as exc:
        raise ConnectorError("Не удалось установить защищённое соединение с почтовым сервером.",
                             "Для порта 465 выберите «SSL», для 587 — «STARTTLS».", repr(exc)) from exc
    except (socket.timeout, TimeoutError, ConnectionRefusedError, OSError) as exc:
        raise ConnectorError(f"Почтовый сервер {host}:{port} не отвечает.",
                             "Проверьте порт (обычно 465 или 587) и что провайдер не блокирует исходящую почту.",
                             repr(exc)) from exc
    except smtplib.SMTPException as exc:
        raise ConnectorError(f"Почтовый сервер ответил ошибкой: {exc}", "", repr(exc)) from exc


def send_mail(values: dict, msg: EmailMessage) -> None:
    server = _open(values)
    try:
        server.send_message(msg)
    except smtplib.SMTPRecipientsRefused as exc:
        raise ConnectorError("Почтовый сервер отклонил адрес получателя.", "Проверьте адрес «Кому».", repr(exc)) from exc
    except smtplib.SMTPSenderRefused as exc:
        raise ConnectorError("Почтовый сервер не разрешает отправлять от этого адреса.",
                             "Адрес отправителя должен совпадать с логином почты.", repr(exc)) from exc
    except smtplib.SMTPException as exc:
        raise ConnectorError(f"Письмо не отправлено: {exc}", "", repr(exc)) from exc
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass


async def check(values: dict, ctx: CheckContext) -> CheckResult:
    def probe():
        server = _open(values)
        server.quit()

    await asyncio.to_thread(probe)
    return CheckResult(OK, f"Почта {values.get('username') or values.get('host')} подключена: вход на сервер выполнен.")


CONNECTOR = ConnectorType(
    key="smtp",
    title="Почта (SMTP)",
    category="Почта и файлы",
    description="Отправка писем из сценариев через любой почтовый сервер: Яндекс, Mail.ru, корпоративный.",
    name_placeholder="Например: robot@company.ru",
    check=check,
    fields=[
        Field("host", "Сервер SMTP", required=True, placeholder="smtp.yandex.ru",
              hint="Яндекс: smtp.yandex.ru, Mail.ru: smtp.mail.ru, Google: smtp.gmail.com."),
        Field("port", "Порт", default="465", placeholder="465"),
        Field("security", "Защита", "select", default="ssl",
              options=[("ssl", "SSL (порт 465)"), ("starttls", "STARTTLS (порт 587)"), ("none", "Без шифрования (25)")]),
        Field("username", "Логин", placeholder="robot@company.ru", hint="Обычно — полный адрес почты."),
        Field("password", "Пароль приложения", "secret", ascii_only=True,
              hint="Не пароль от почты, а отдельный «пароль приложения» — см. «Где взять данные»."),
        Field("from_email", "Адрес отправителя", placeholder="robot@company.ru",
              hint="Пусто — как логин."),
    ],
    help_steps=[
        "Заведите отдельный ящик для рассылок, например robot@company.ru.",
        "Яндекс: Яндекс ID → Безопасность → «Пароли приложений» → создайте пароль для почты. "
        "В настройках почты включите «Разрешить доступ с помощью почтовых клиентов».",
        "Mail.ru: Настройки → Безопасность → «Пароли для внешних приложений».",
        "Впишите сервер, порт 465, защиту SSL, логин и пароль приложения.",
    ],
)
