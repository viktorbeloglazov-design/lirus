"""Управление запущенным процессом: остановка и перезапуск из панели."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

from . import config

log = logging.getLogger(__name__)

servers: list = []  # uvicorn.Server, заполняет run.py
# Код выхода serve. 75 — «перезапусти меня»: так перезапуск просит у launchd на Mac.
exit_code = 0


def can_restart() -> bool:
    return bool(servers)


def request_shutdown(delay: float = 0.5) -> None:
    async def _stop():
        await asyncio.sleep(delay)
        for s in servers:
            s.should_exit = True

    asyncio.get_event_loop().create_task(_stop())


def request_restart() -> None:
    """Запускает помощника, который дождётся остановки и поднимет платформу заново."""
    global exit_code
    if os.environ.get("PLATFORM_LAUNCHD"):
        # Под присмотром launchd: выходим с ошибкой — он сам запустит платформу заново.
        exit_code = 75
        log.info("Перезапуск: выходим, launchd запустит заново")
        request_shutdown(1.0)
        return
    python = sys.executable
    if os.name == "nt":
        pyw = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.exists(pyw):
            python = pyw
    cmd = [python, str(config.BASE_DIR / "run.py"), "start", "--after-pid", str(os.getpid()), "--no-browser"]
    kwargs: dict = {"cwd": str(config.BASE_DIR), "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    log.info("Перезапуск: помощник запущен")
    request_shutdown(1.0)
