"""Резервные копии: база, ключ шифрования, сертификат, .env — в один zip-файл."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

from . import config

PREFIX = "backup-"


def create(keep: int | None = None) -> Path:
    config.ensure_dirs()
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    target = config.BACKUP_DIR / f"{PREFIX}{stamp}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        db_copy = Path(tmp) / "platform.db"
        if config.DB_PATH.exists():
            src = sqlite3.connect(config.DB_PATH)
            dst = sqlite3.connect(db_copy)
            with dst:
                src.backup(dst)  # корректная копия даже во время работы
            src.close()
            dst.close()
        partial = target.with_suffix(".part")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
            if db_copy.exists():
                zf.write(db_copy, "data/platform.db")
            if config.KEY_PATH.exists():
                zf.write(config.KEY_PATH, "data/secret.key")
            if config.TLS_DIR.exists():
                for f in config.TLS_DIR.iterdir():
                    if f.is_file():
                        zf.write(f, f"data/tls/{f.name}")
            if config.ENV_FILE.exists():
                zf.write(config.ENV_FILE, ".env")
            zf.writestr("ПРОЧТИ.txt",
                        "Резервная копия платформы от " + datetime.now().strftime("%d.%m.%Y %H:%M") + ".\r\n"
                        "Внутри ключ шифрования и все пароли подключений — храните файл в надёжном месте.\r\n"
                        "Восстановление: перетащите этот файл мышью на восстановить-из-копии.bat.\r\n")
        os.replace(partial, target)
    if keep:
        prune(keep)
    return target


def list_backups() -> list[Path]:
    if not config.BACKUP_DIR.exists():
        return []
    return sorted(config.BACKUP_DIR.glob(PREFIX + "*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)


def last_backup_time() -> float | None:
    items = list_backups()
    return items[0].stat().st_mtime if items else None


def prune(keep: int) -> None:
    for old in list_backups()[max(keep, 1):]:
        try:
            old.unlink()
        except OSError:
            pass


def restore(archive: Path) -> str:
    """Восстанавливает данные из копии. Текущие данные перед этим сохраняются отдельной копией."""
    if not archive.exists():
        raise FileNotFoundError(f"Файл не найден: {archive}")
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        if "data/platform.db" not in names:
            raise ValueError("Это не резервная копия платформы: внутри нет базы data/platform.db.")
        safety = create() if config.DB_PATH.exists() else None
        config.ensure_dirs()
        for suffix in ("-wal", "-shm"):
            extra = Path(str(config.DB_PATH) + suffix)
            if extra.exists():
                extra.unlink()
        with zf.open("data/platform.db") as src, open(config.DB_PATH, "wb") as dst:
            shutil.copyfileobj(src, dst)
        if "data/secret.key" in names:
            config.KEY_PATH.write_bytes(zf.read("data/secret.key"))
        for name in names:
            if name.startswith("data/tls/") and not name.endswith("/"):
                (config.TLS_DIR / Path(name).name).write_bytes(zf.read(name))
        if ".env" in names:
            config.ENV_FILE.write_bytes(zf.read(".env"))
    time.sleep(0.1)
    return str(safety) if safety else ""
