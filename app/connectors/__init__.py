"""Реестр типов подключений."""
from __future__ import annotations

from . import bitrix, bots, claude, google, marketplaces, onec, smtp
from .base import CheckContext, CheckResult, ConnectorType, run_check, validate_form  # noqa: F401


def _google_app_values() -> dict:
    from .. import store

    apps = store.list_connections("google_app")
    return apps[0].values() if apps else {}


TYPES: list[ConnectorType] = [
    claude.CONNECTOR,
    onec.CONNECTOR,
    bitrix.CONNECTOR,
    google.APP,
    google.account_type(_google_app_values),
    smtp.CONNECTOR,
    marketplaces.WILDBERRIES,
    marketplaces.OZON,
    marketplaces.YANDEX_MARKET,
    bots.MAX_BOT,
    bots.TELEGRAM_BOT,
]
BY_KEY = {t.key: t for t in TYPES}

CATEGORY_ORDER = ["Искусственный интеллект", "Учёт", "CRM и задачи", "Почта и файлы", "Маркетплейсы", "Боты"]


def get_type(key: str) -> ConnectorType | None:
    return BY_KEY.get(key)


def context_for(ctype: ConnectorType, connection_id: int | None = None) -> CheckContext:
    from .. import store

    return CheckContext(proxy=store.get_setting("proxy_url") if ctype.foreign else "",
                        connection_id=connection_id)
