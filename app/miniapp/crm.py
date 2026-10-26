"""Битрикс24 для мини-приложения: контакты и лиды через входящий вебхук."""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from ..connectors.base import ssl_context
from ..connectors.bitrix import normalize_webhook

log = logging.getLogger(__name__)


class CrmError(Exception):
    def __init__(self, message: str, action: str = "", details: str = ""):
        super().__init__(message)
        self.message, self.action, self.details = message, action, details


def normalize_phone(value: str) -> str:
    """«8 (912) 345-67-89» → «+79123456789». Пустая строка, если это не телефон."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    if not 10 <= len(digits) <= 15:
        return ""
    return "+" + digits


class BitrixCrm:
    def __init__(self, webhook_url: str, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30):
        self.base = normalize_webhook(webhook_url)
        self.transport = transport
        self.timeout = timeout

    async def call(self, method: str, params: dict | None = None) -> Any:
        kwargs: dict[str, Any] = {"timeout": self.timeout, "verify": ssl_context(True)}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                resp = await client.post(self.base + method + ".json", json=params or {})
        except httpx.HTTPError as exc:
            raise CrmError("Битрикс24 не отвечает.", "Проверьте доступ в интернет с сервера и адрес вебхука.",
                           f"{type(exc).__name__}: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if resp.status_code != 200 or "error" in data:
            code = str(data.get("error") or resp.status_code)
            text = str(data.get("error_description") or "")
            action = "Проверьте подключение «Битрикс24» в панели: вебхук и права CRM."
            if code in ("NO_AUTH_FOUND", "INVALID_CREDENTIALS", "INVALID_TOKEN", "EXPIRED_TOKEN"):
                action = "Вебхук удалён или неверный — создайте новый в Битрикс24 и обновите подключение."
            elif code == "INSUFFICIENT_SCOPE":
                action = "У вебхука не хватает права «CRM» — откройте его в Битрикс24 и отметьте право."
            raise CrmError(f"Битрикс24 отклонил запрос {method}: {text or code}.", action,
                           f"HTTP {resp.status_code} {method}: {resp.text[:300]}")
        return data.get("result")

    # ------------------------------------------------------------ контакты

    async def find_contact_by_phone(self, phone: str) -> int | None:
        if not phone:
            return None
        result = await self.call("crm.duplicate.findByComm",
                                 {"type": "PHONE", "values": [phone], "entity_type": "CONTACT"})
        ids = (result or {}).get("CONTACT") if isinstance(result, dict) else None
        if isinstance(ids, list) and ids:
            try:
                return int(ids[0])
            except (TypeError, ValueError):
                return None
        return None

    async def create_contact(self, name: str, phone: str, comment: str = "", assigned: int | None = None) -> int:
        first, _, last = (name or "Клиент из MAX").strip().partition(" ")
        fields: dict[str, Any] = {"NAME": first, "LAST_NAME": last, "OPENED": "Y",
                                  "SOURCE_ID": "OTHER", "SOURCE_DESCRIPTION": "Мини-приложение MAX"}
        if phone:
            fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "MOBILE"}]
        if comment:
            fields["COMMENTS"] = comment
        if assigned:
            fields["ASSIGNED_BY_ID"] = assigned
        return int(await self.call("crm.contact.add", {"fields": fields}))

    async def ensure_contact(self, name: str, phone: str, assigned: int | None = None) -> int | None:
        found = await self.find_contact_by_phone(phone)
        if found:
            return found
        if not phone and not name:
            return None
        return await self.create_contact(name, phone, assigned=assigned)

    # ----------------------------------------------------------------- лиды

    async def create_lead(self, title: str, *, name: str = "", phone: str = "", comments: str = "",
                          contact_id: int | None = None, assigned: int | None = None,
                          address: str = "") -> int:
        first, _, last = (name or "").strip().partition(" ")
        fields: dict[str, Any] = {"TITLE": title[:250], "NAME": first, "LAST_NAME": last, "OPENED": "Y",
                                  "SOURCE_ID": "OTHER", "SOURCE_DESCRIPTION": "Мини-приложение MAX",
                                  "COMMENTS": comments}
        if phone:
            fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "MOBILE"}]
        if contact_id:
            fields["CONTACT_ID"] = contact_id
        if assigned:
            fields["ASSIGNED_BY_ID"] = assigned
        if address:
            fields["ADDRESS"] = address[:500]
        return int(await self.call("crm.lead.add", {"fields": fields, "params": {"REGISTER_SONET_EVENT": "Y"}}))

    async def lead_statuses(self) -> dict[str, str]:
        result = await self.call("crm.status.list", {"filter": {"ENTITY_ID": "STATUS"}})
        out: dict[str, str] = {}
        for row in result or []:
            if isinstance(row, dict) and row.get("STATUS_ID"):
                out[str(row["STATUS_ID"])] = str(row.get("NAME") or row["STATUS_ID"])
        return out

    async def leads_status(self, ids: list[int]) -> dict[int, str]:
        if not ids:
            return {}
        result = await self.call("crm.lead.list", {"filter": {"ID": ids}, "select": ["ID", "STATUS_ID"]})
        out: dict[int, str] = {}
        for row in result or []:
            if isinstance(row, dict) and row.get("ID"):
                out[int(row["ID"])] = str(row.get("STATUS_ID") or "")
        return out

    async def add_comment(self, lead_id: int, text: str) -> None:
        await self.call("crm.timeline.comment.add",
                        {"fields": {"ENTITY_ID": lead_id, "ENTITY_TYPE": "lead", "COMMENT": text[:4000]}})
