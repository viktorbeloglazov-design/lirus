"""Клиент Bot API мессенджера MAX.

Токен бота передаётся в заголовке Authorization как есть, без слова Bearer.
Актуальный адрес API — platform-api2.max.ru (старый platform-api.max.ru отключён летом 2026).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..connectors.base import ssl_context

log = logging.getLogger(__name__)

MAX_API = "https://platform-api2.max.ru"


class MaxError(Exception):
    def __init__(self, message: str, action: str = "", details: str = "", status: int = 0):
        super().__init__(message)
        self.message, self.action, self.details, self.status = message, action, details, status


# ---------------------------------------------------------------- кнопки

def keyboard(rows: list[list[dict]]) -> dict:
    """Вложение «клавиатура под сообщением». rows — список рядов, ряд — список кнопок."""
    return {"type": "inline_keyboard", "payload": {"buttons": rows}}


def btn_callback(text: str, payload: str) -> dict:
    return {"type": "callback", "text": text, "payload": payload[:1024]}


def btn_link(text: str, url: str) -> dict:
    return {"type": "link", "text": text, "url": url}


def btn_open_app(text: str, bot_username: str, bot_id: int | str | None = None, payload: str = "") -> dict:
    """Кнопка, открывающая мини-приложение, привязанное к боту."""
    btn: dict[str, Any] = {"type": "open_app", "text": text, "web_app": bot_username}
    if bot_id:
        btn["contact_id"] = int(bot_id)
    if payload:
        btn["payload"] = payload
    return btn


# ---------------------------------------------------------------- клиент

class MaxApi:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30):
        self.token = token
        self.transport = transport
        self.timeout = timeout

    async def request(self, method: str, path: str, *, params: dict | None = None,
                      json: dict | None = None, timeout: float | None = None) -> Any:
        kwargs: dict[str, Any] = {"timeout": timeout or self.timeout, "verify": ssl_context(True)}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                resp = await client.request(method, MAX_API + path, params=params, json=json,
                                            headers={"Authorization": self.token})
        except httpx.HTTPError as exc:
            raise MaxError("Сервер MAX не отвечает.", "Проверьте доступ в интернет с сервера и повторите.",
                           f"{type(exc).__name__}: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code in (401, 403):
            raise MaxError("MAX не принял токен бота.", "Проверьте подключение «Бот MAX»: скопируйте токен заново.",
                           f"HTTP {resp.status_code}", resp.status_code)
        if resp.status_code == 429:
            raise MaxError("MAX просит подождать: слишком много запросов.", "Повторите через минуту.",
                           f"HTTP {resp.status_code}", resp.status_code)
        if resp.status_code >= 400:
            text = ""
            if isinstance(data, dict):
                text = str(data.get("message") or data.get("error") or data.get("code") or "")
            raise MaxError(f"MAX ответил ошибкой (код {resp.status_code}){': ' + text if text else '.'}",
                           "", f"HTTP {resp.status_code} {method} {path}: {resp.text[:300]}", resp.status_code)
        return data if data is not None else {}

    async def me(self) -> dict:
        data = await self.request("GET", "/me")
        return data if isinstance(data, dict) else {}

    async def send(self, text: str, *, user_id: int | str | None = None, chat_id: int | str | None = None,
                   buttons: list[list[dict]] | None = None, fmt: str = "") -> dict:
        """Отправляет сообщение пользователю (user_id) или в чат (chat_id)."""
        if not user_id and not chat_id:
            raise MaxError("Не указан получатель сообщения.")
        body: dict[str, Any] = {"text": text[:4000]}
        if fmt in ("markdown", "html"):
            body["format"] = fmt
        if buttons:
            body["attachments"] = [keyboard(buttons)]
        params = {"user_id": str(user_id)} if user_id else {"chat_id": str(chat_id)}
        return await self.request("POST", "/messages", params=params, json=body)

    async def answer_callback(self, callback_id: str, *, text: str = "", notification: str = "",
                              buttons: list[list[dict]] | None = None) -> dict:
        body: dict[str, Any] = {}
        if text:
            body["message"] = {"text": text[:4000]}
            if buttons:
                body["message"]["attachments"] = [keyboard(buttons)]
        if notification:
            body["notification"] = notification[:200]
        return await self.request("POST", "/answers", params={"callback_id": callback_id}, json=body)

    async def subscriptions(self) -> list[dict]:
        data = await self.request("GET", "/subscriptions")
        subs = data.get("subscriptions") if isinstance(data, dict) else None
        return subs if isinstance(subs, list) else []

    async def subscribe(self, url: str, secret: str, update_types: list[str] | None = None) -> dict:
        body: dict[str, Any] = {"url": url, "secret": secret}
        if update_types:
            body["update_types"] = update_types
        return await self.request("POST", "/subscriptions", json=body)

    async def unsubscribe(self, url: str) -> dict:
        return await self.request("DELETE", "/subscriptions", params={"url": url})


# ------------------------------------------------------------ обновления

def parse_update(update: dict) -> dict:
    """Приводит обновление MAX к плоскому виду: тип, user_id, chat_id, текст, payload кнопки."""
    kind = str(update.get("update_type") or "")
    msg = update.get("message") or {}
    sender = msg.get("sender") or {}
    callback = update.get("callback") or {}
    user = callback.get("user") or sender or update.get("user") or {}
    chat_id = (msg.get("recipient") or {}).get("chat_id") or update.get("chat_id")
    return {
        "type": kind,
        "user_id": user.get("user_id") or user.get("id"),
        "user_name": " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip()
        or user.get("name") or "",
        "username": user.get("username") or "",
        "chat_id": chat_id,
        "text": ((msg.get("body") or {}).get("text") or "").strip(),
        "callback_id": callback.get("callback_id") or "",
        "payload": callback.get("payload") or update.get("payload") or "",
        "message_id": (msg.get("body") or {}).get("mid") or "",
    }
