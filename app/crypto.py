"""Шифрование паролей и токенов.

Ключ лежит в data/secret.key и создаётся при первом запуске. Без этого файла
сохранённые пароли не прочитать — поэтому он входит в резервную копию.
"""
from __future__ import annotations

import logging
import os
import threading

from cryptography.fernet import Fernet, InvalidToken

from . import config

log = logging.getLogger(__name__)

_lock = threading.Lock()
_fernet: Fernet | None = None
key_was_created = False  # ключ создан в этом запуске (для журнала)


class SecretUnreadable(Exception):
    """Секрет зашифрован другим ключом — прочитать нельзя."""


def _load() -> Fernet:
    global _fernet, key_was_created
    with _lock:
        if _fernet is not None:
            return _fernet
        config.ensure_dirs()
        if config.KEY_PATH.exists():
            key = config.KEY_PATH.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            tmp = config.KEY_PATH.with_suffix(".tmp")
            tmp.write_bytes(key)
            os.replace(tmp, config.KEY_PATH)
            key_was_created = True
            log.warning("Создан новый ключ шифрования: %s", config.KEY_PATH)
        _fernet = Fernet(key)
        return _fernet


def reset_cache() -> None:
    global _fernet
    with _lock:
        _fernet = None


def key_exists() -> bool:
    return config.KEY_PATH.exists()


def encrypt(value: str) -> str:
    return _load().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _load().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretUnreadable() from exc


def mask(value: str | None) -> str:
    """Маска для показа в интерфейсе: ********t123."""
    if not value:
        return ""
    tail = value[-4:] if len(value) >= 12 else ""
    return "********" + tail


def tls_key_password() -> bytes:
    """Пароль для закрытого ключа сертификата HTTPS на диске."""
    import hashlib

    _load()
    return hashlib.sha256(b"tls:" + config.KEY_PATH.read_bytes().strip()).hexdigest().encode()
