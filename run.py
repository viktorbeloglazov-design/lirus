"""Запуск платформы. Обычно вызывается из .bat-файлов, а не вручную.

    run.py serve              — работать в этом окне (так запускает автозапуск)
    run.py start              — запустить в фоне, дождаться готовности, открыть браузер
    run.py stop               — остановить
    run.py status             — работает ли
    run.py set-port 8081      — сменить порт
    run.py backup             — сделать резервную копию
    run.py restore файл.zip   — восстановить из копии
    run.py reset-admin        — сбросить пароль admin на admin
    run.py setup              — создать .env и папки (вызывает установить.bat)
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from app import config  # noqa: E402

# Коды выхода для .bat-файлов
EXIT_OK, EXIT_FAIL, EXIT_PORT_BUSY, EXIT_NO_RIGHTS = 0, 1, 2, 3


def say(text: str = "") -> None:
    """Печать, которая не падает на старых консолях Windows и под pythonw."""
    if sys.stdout is None:
        return
    text = config.local(text)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode(), flush=True)


def setup_logging() -> None:
    config.ensure_dirs()
    handler = logging.handlers.RotatingFileHandler(config.LOGS_DIR / "app.log", maxBytes=5 * 1024 * 1024,
                                                   backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        root.addHandler(console)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def health(port: int, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def is_ours(info: dict | None) -> bool:
    return bool(info) and info.get("app") == config.APP_NAME


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------- serve

def serve() -> int:
    setup_logging()
    log = logging.getLogger("run")
    import uvicorn

    try:
        from app import runtime, store, tls
        from app.main import app, startup

        startup()
    except Exception:
        log.exception("Платформа не может запуститься")
        return EXIT_FAIL
    port = config.port()
    host = config.host()
    if port_busy(port):
        info = health(port)
        if is_ours(info):
            log.error("Платформа уже запущена на порту %s", port)
        else:
            log.error("Порт %s занят другой программой", port)
        return EXIT_PORT_BUSY
    config.PID_PATH.write_text(str(os.getpid()))
    if config.IS_MAC:
        _keep_mac_awake(log)
    servers = [uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False,
                                             proxy_headers=True, forwarded_allow_ips="127.0.0.1",
                                             timeout_graceful_shutdown=5))]
    cert = tls.installed()
    if cert is not None:
        https_port = store.get_int_setting("https_port", 443)
        from app import crypto

        servers.append(uvicorn.Server(uvicorn.Config(
            app, host=host, port=https_port, log_config=None, access_log=False, lifespan="off",
            ssl_certfile=str(tls.CERT_PATH), ssl_keyfile=str(tls.KEY_PATH),
            ssl_keyfile_password=crypto.tls_key_password().decode(), timeout_graceful_shutdown=5)))
        log.info("HTTPS включён на порту %s", https_port)
    runtime.servers[:] = servers
    log.info("Платформа запускается: http://localhost:%s", port)

    async def main():
        tasks = [asyncio.create_task(s.serve()) for s in servers]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            if t.exception():
                raise t.exception()

    try:
        asyncio.run(main())
    except SystemExit:
        pass
    except Exception:
        log.exception("Платформа остановилась из-за ошибки")
        return EXIT_FAIL
    finally:
        try:
            if config.PID_PATH.exists() and config.PID_PATH.read_text().strip() == str(os.getpid()):
                config.PID_PATH.unlink()
        except OSError:
            pass
    log.info("Платформа остановлена")
    return runtime.exit_code


def _keep_mac_awake(log: logging.Logger) -> None:
    """Не даём Mac уснуть, пока работает платформа (экран при этом гаснет как обычно)."""
    try:
        subprocess.Popen(["/usr/bin/caffeinate", "-i", "-s", "-w", str(os.getpid())],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("Сон Mac отключён, пока работает платформа")
    except OSError:
        log.warning("Не удалось запретить Mac засыпать (caffeinate)")


# ------------------------------------------------------------------- start

def _python_for_background() -> str:
    exe = sys.executable
    if os.name == "nt":
        pyw = Path(exe).with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
    return exe


def _last_log_lines(n: int = 15) -> str:
    path = config.LOGS_DIR / "app.log"
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def start(args: list[str]) -> int:
    config.ensure_dirs()
    if "--after-pid" in args:
        pid = int(args[args.index("--after-pid") + 1])
        deadline = time.time() + 30
        while pid_alive(pid) and time.time() < deadline:
            time.sleep(0.3)
        time.sleep(0.5)
    port = config.port()
    info = health(port)
    if is_ours(info):
        say(f"Платформа уже работает. Откройте в браузере: http://localhost:{port}")
        if "--no-browser" not in args:
            _open_browser(port)
        return EXIT_OK
    if port_busy(port):
        say(f"ОШИБКА: порт {port} занят другой программой (например, IIS, Skype или другой веб-сервер).")
        say("Что делать: закройте ту программу или выберите другой порт — запустите сменить-порт.bat.")
        return EXIT_PORT_BUSY
    if config.IS_MAC and "--after-pid" not in args and _mac_agent_loaded():
        # Автозапуск включён — запускаем через launchd, чтобы он и дальше присматривал за платформой.
        _launchctl("kickstart", f"gui/{os.getuid()}/{MAC_LABEL}")
        say("Запускаем платформу…")
        if wait(["40"]) == EXIT_OK:
            if "--no-browser" not in args:
                _open_browser(port)
            return EXIT_OK
        return EXIT_FAIL
    cmd = [_python_for_background(), str(BASE / "run.py"), "serve"]
    kwargs: dict = {"cwd": str(BASE), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    say("Запускаем платформу…")
    deadline = time.time() + 40
    while time.time() < deadline:
        if is_ours(health(port)):
            say(f"Готово. Панель открыта: http://localhost:{port}")
            say("Это окно можно закрыть — платформа продолжит работать.")
            if "--no-browser" not in args:
                _open_browser(port)
            return EXIT_OK
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    say("ОШИБКА: платформа не запустилась.")
    tail = _last_log_lines()
    if tail:
        say("Последние строки журнала (logs\\app.log):")
        say(tail)
    say("Что делать: запустите установить.bat ещё раз. Если не помогло — пришлите разработчику файл logs\\app.log.")
    return EXIT_FAIL


def _open_browser(port: int) -> None:
    try:
        import webbrowser

        webbrowser.open(f"http://localhost:{port}")
    except Exception:
        pass


# -------------------------------------------------------------------- stop

def stop() -> int:
    port = config.port()
    info = health(port)
    if not is_ours(info):
        pid = int(config.PID_PATH.read_text().strip()) if config.PID_PATH.exists() else 0
        if pid and pid_alive(pid):
            say("Платформа не отвечает, завершаем процесс принудительно…")
            return _kill(pid)
        say("Платформа не запущена — останавливать нечего.")
        return EXIT_OK
    token = config.CONTROL_TOKEN_PATH.read_text().strip() if config.CONTROL_TOKEN_PATH.exists() else ""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/internal/shutdown", method="POST",
                                     headers={"X-Control-Token": token})
        urllib.request.urlopen(req, timeout=5).read()
    except (urllib.error.URLError, OSError):
        pass
    for _ in range(40):
        if not port_busy(port):
            say("Платформа остановлена.")
            return EXIT_OK
        time.sleep(0.25)
    pid = int(config.PID_PATH.read_text().strip()) if config.PID_PATH.exists() else 0
    return _kill(pid) if pid else EXIT_FAIL


def _kill(pid: int) -> int:
    try:
        if os.name == "nt":
            res = subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=0x08000000)
            if res.returncode != 0:
                raise PermissionError(res.stderr.decode("cp866", "replace"))
        else:
            os.kill(pid, 15)
    except PermissionError:
        say("ОШИБКА: не хватает прав, чтобы остановить платформу — она запущена автозапуском от имени системы.")
        say("Что делать: нажмите на остановить.bat правой кнопкой → «Запуск от имени администратора».")
        return EXIT_NO_RIGHTS
    except OSError:
        pass
    time.sleep(1)
    say("Платформа остановлена.")
    return EXIT_OK


# ------------------------------------------------------------ прочие команды

def status() -> int:
    port = config.port()
    if is_ours(health(port)):
        say(f"Платформа работает: http://localhost:{port}")
        return EXIT_OK
    say("Платформа не запущена.")
    return EXIT_FAIL


def set_port(args: list[str]) -> int:
    value = (args[0] if args else "").strip()
    if not value.isdigit() or not 1024 <= int(value) <= 65535:
        say("ОШИБКА: порт — это число от 1024 до 65535, например 8081.")
        return EXIT_FAIL
    if port_busy(int(value)) and not is_ours(health(int(value))):
        say(f"ОШИБКА: порт {value} тоже занят. Попробуйте другой, например 8090.")
        return EXIT_PORT_BUSY
    config.write_env({"PORT": value})
    say(f"Порт изменён на {value}. Перезапустите платформу: остановить.bat, затем запустить.bat.")
    say(f"Новый адрес панели: http://localhost:{value}")
    return EXIT_OK


def do_backup() -> int:
    from app import backup, db, store

    db.init()
    path = backup.create(store.get_int_setting("backup_keep", 14))
    store.log_event("info", "backup", f"Резервная копия создана вручную: {path.name}")
    say(f"Резервная копия создана: {path}")
    say("Скопируйте её на другой диск или компьютер — в ней все настройки и ключи.")
    return EXIT_OK


def do_restore(args: list[str]) -> int:
    from app import backup

    if not args:
        items = backup.list_backups()
        if not items:
            say("ОШИБКА: резервных копий нет. Перетащите файл копии мышью на восстановить-из-копии.bat.")
            return EXIT_FAIL
        archive = items[0]
        say(f"Файл не указан — берём самую свежую копию: {archive.name}")
    else:
        archive = Path(args[0].strip('"'))
    if is_ours(health(config.port())):
        say("ОШИБКА: платформа сейчас работает. Сначала запустите остановить.bat, потом повторите.")
        return EXIT_FAIL
    try:
        safety = backup.restore(archive)
    except (FileNotFoundError, ValueError, OSError) as exc:
        say(f"ОШИБКА: {exc}")
        return EXIT_FAIL
    except Exception as exc:  # noqa: BLE001
        say(f"ОШИБКА: файл повреждён или это не резервная копия платформы ({exc}).")
        return EXIT_FAIL
    say(f"Данные восстановлены из {archive.name}.")
    if safety:
        say(f"Прежние данные на всякий случай сохранены в {Path(safety).name}.")
    say("Теперь запустите запустить.bat.")
    return EXIT_OK


def reset_admin() -> int:
    from app import auth, db, store

    db.init()
    row = db.query_one("SELECT id FROM users WHERE login = 'admin'")
    if row:
        db.execute("UPDATE users SET password_hash = ?, must_change_password = 1, active = 1, role = 'admin' "
                   "WHERE id = ?", (auth.hash_password("admin"), row["id"]))
    else:
        db.execute("INSERT INTO users(login, full_name, role, password_hash, must_change_password, created_at) "
                   "VALUES('admin', 'Администратор', 'admin', ?, 1, ?)", (auth.hash_password("admin"), time.time()))
    db.execute("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE login = 'admin')")
    store.log_event("warning", "auth", "Пароль admin сброшен на стандартный (сбросить-пароль-администратора" + config.SCRIPT_EXT + ")")
    say("Готово: вход admin, пароль admin. При входе панель попросит придумать новый пароль.")
    return EXIT_OK


def wait(args: list[str]) -> int:
    """Ждёт, пока платформа ответит (после запуска автозапуском)."""
    seconds = int(args[0]) if args and args[0].isdigit() else 60
    port = config.port()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if is_ours(health(port)):
            say(f"Платформа работает: http://localhost:{port}")
            return EXIT_OK
        time.sleep(1)
    say("Платформа не ответила вовремя.")
    tail = _last_log_lines()
    if tail:
        say("Последние строки журнала (logs\\app.log):")
        say(tail)
    return EXIT_FAIL


def autostart_xml(args: list[str]) -> int:
    """Готовит описание задачи для Планировщика Windows (schtasks /XML).

    Через XML — потому что у задачи, созданной простым schtasks /Create, по умолчанию стоит
    «останавливать через 3 дня», и платформа выключалась бы сама."""
    from xml.sax.saxutils import escape

    target = Path(args[0]) if args else config.DATA_DIR / "autostart.xml"
    python = _python_for_background()
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape(config.APP_NAME)}: запуск при включении сервера</Description></RegistrationInfo>
  <Triggers><BootTrigger><Enabled>true</Enabled><Delay>PT30S</Delay></BootTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>10</Count></RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(python)}</Command>
      <Arguments>{escape(chr(34) + str(BASE / "run.py") + chr(34))} serve</Arguments>
      <WorkingDirectory>{escape(str(BASE))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    config.ensure_dirs()
    target.write_text(xml.replace("\n", "\r\n"), encoding="utf-16")
    say(str(target))
    return EXIT_OK


# ---------------------------------------------------------------- Mac: launchd

MAC_LABEL = config.MAC_LABEL


def mac_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=20)


def _mac_agent_loaded() -> bool:
    try:
        return mac_plist_path().exists() and \
            _launchctl("print", f"gui/{os.getuid()}/{MAC_LABEL}").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def autostart_mac(args: list[str]) -> int:
    """Автозапуск на Mac: агент launchd запускает платформу после входа в систему и после сбоя.

    Агент пользователя, а не системная служба, — чтобы не просить пароль администратора
    в терминале. Поэтому на Mac нужен автоматический вход в систему (см. ИНСТРУКЦИЯ-MAC)."""
    import plistlib

    action = args[0] if args else "status"
    plist = mac_plist_path()
    domain = f"gui/{os.getuid()}"
    if action == "status":
        loaded = _mac_agent_loaded()
        say("Автозапуск включён." if loaded else "Автозапуск выключен.")
        return EXIT_OK if loaded else EXIT_FAIL
    if action == "off":
        if plist.exists():
            _launchctl("bootout", f"{domain}/{MAC_LABEL}")
            plist.unlink()
            say("Автозапуск выключен. Платформа остановлена; запускать её теперь — запустить.command.")
        else:
            say("Автозапуск и так не был включён.")
        return EXIT_OK
    # on
    home = Path.home()
    for name, title in (("Desktop", "Рабочий стол"), ("Documents", "Документы"), ("Downloads", "Загрузки")):
        if (home / name) in BASE.parents:
            # macOS не пускает службы автозапуска в эти папки без отдельного разрешения.
            say(f"ОШИБКА: папка платформы лежит в «{title}» — оттуда macOS не даст запускать её автоматически.")
            say(f"Что делать: остановите платформу, перетащите папку {BASE.name} в папку пользователя "
                f"(в Finder: меню «Переход» → «Домой»), затем запустите установить.command и "
                "автозапуск-включить.command из нового места.")
            return EXIT_FAIL
    config.ensure_dirs()
    plist.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "Label": MAC_LABEL,
        "ProgramArguments": [sys.executable, str(BASE / "run.py"), "serve"],
        "WorkingDirectory": str(BASE),
        "RunAtLoad": True,
        # Перезапускать, только если упала; после остановить.command — не поднимать.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 15,
        "StandardOutPath": str(config.LOGS_DIR / "launchd.log"),
        "StandardErrorPath": str(config.LOGS_DIR / "launchd.log"),
        "EnvironmentVariables": {"LANG": "ru_RU.UTF-8", "PYTHONIOENCODING": "utf-8", "PLATFORM_LAUNCHD": "1"},
    }
    if is_ours(health(config.port())):
        stop()
    _launchctl("bootout", f"{domain}/{MAC_LABEL}")
    with open(plist, "wb") as fh:
        plistlib.dump(data, fh)
    res = _launchctl("bootstrap", domain, str(plist))
    if res.returncode != 0:
        res = _launchctl("load", "-w", str(plist))  # старые версии macOS
    if res.returncode != 0:
        say("ОШИБКА: macOS не приняла автозапуск: " + (res.stderr or res.stdout).strip())
        say("Что делать: перезагрузите Mac и запустите автозапуск-включить.command ещё раз.")
        return EXIT_FAIL
    say("Автозапуск включён: платформа будет запускаться сама после входа в систему.")
    return wait(["60"])


def setup() -> int:
    config.ensure_dirs()
    if not config.ENV_FILE.exists():
        config.write_env({})
        say("Создан файл настроек .env (порт 8080).")
    else:
        say("Файл настроек .env уже есть — оставляем как есть.")
    from app import db

    db.init()
    say("База данных готова.")
    return EXIT_OK


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "serve"
    rest = argv[1:]
    commands = {
        "serve": lambda: serve(),
        "start": lambda: start(rest),
        "stop": lambda: stop(),
        "status": lambda: status(),
        "set-port": lambda: set_port(rest),
        "backup": lambda: do_backup(),
        "restore": lambda: do_restore(rest),
        "reset-admin": lambda: reset_admin(),
        "setup": lambda: setup(),
        "wait": lambda: wait(rest),
        "port": lambda: (say(str(config.port())), EXIT_OK)[1],
        "autostart-xml": lambda: autostart_xml(rest),
        "autostart-mac": lambda: autostart_mac(rest),
    }
    if cmd not in commands:
        say(__doc__ or "")
        return EXIT_FAIL
    return commands[cmd]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
