"""Пути и параметры запуска.

Почти все настройки хранятся в базе и меняются в браузере (раздел «Настройки»).
В файле .env лежат только адрес и порт, на которых панель слушает: их нужно
знать ещё до того, как открыта база.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Центр управления бизнесом"
APP_VERSION = "0.3.0"

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
BACKUP_DIR = BASE_DIR / "backups"
ENV_FILE = BASE_DIR / ".env"

DB_PATH = DATA_DIR / "platform.db"
KEY_PATH = DATA_DIR / "secret.key"
PID_PATH = DATA_DIR / "app.pid"
CONTROL_TOKEN_PATH = DATA_DIR / "control.token"
TLS_DIR = DATA_DIR / "tls"

TASK_NAME = "BusinessPlatform"  # имя задачи в Планировщике Windows
MAC_LABEL = "ru.platforma.business"  # имя службы автозапуска на Mac (launchd)

IS_MAC = sys.platform == "darwin"
SCRIPT_EXT = ".command" if IS_MAC else ".bat"


def local(text: str) -> str:
    """Подставляет в подсказку названия файлов этой системы: на Mac вместо .bat — .command."""
    if not IS_MAC:
        return text
    text = text.replace(".bat", ".command").replace("logs\\app.log", "logs/app.log")
    return text.replace("перетащите файл копии мышью на", "дважды щёлкните").replace(
        "перетащите этот файл мышью на", "дважды щёлкните").replace(
        "Перетащите файл копии мышью на", "Дважды щёлкните")

DEFAULTS = {"HOST": "0.0.0.0", "PORT": "8080"}

ENV_HEADER = (
    "# Параметры запуска платформы.\n"
    "# Обычно менять не нужно: порт меняется в панели (Настройки) или файлом сменить-порт" + SCRIPT_EXT + ".\n"
)


def read_env() -> dict[str, str]:
    values = dict(DEFAULTS)
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip().upper()] = value.strip().strip('"').strip("'")
    for key in list(values):
        if os.environ.get("PLATFORM_" + key):
            values[key] = os.environ["PLATFORM_" + key]
    return values


def write_env(values: dict[str, str]) -> None:
    current = read_env()
    current.update({k.upper(): str(v) for k, v in values.items()})
    body = ENV_HEADER + "".join(f"{k}={v}\n" for k, v in current.items())
    if not IS_MAC:
        body = body.replace("\n", "\r\n")
    ENV_FILE.write_text(body, encoding="utf-8", newline="")


def host() -> str:
    return read_env().get("HOST") or DEFAULTS["HOST"]


def port() -> int:
    try:
        value = int(read_env().get("PORT") or DEFAULTS["PORT"])
        if 1 <= value <= 65535:
            return value
    except ValueError:
        pass
    return int(DEFAULTS["PORT"])


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOGS_DIR, BACKUP_DIR, TLS_DIR):
        d.mkdir(parents=True, exist_ok=True)
