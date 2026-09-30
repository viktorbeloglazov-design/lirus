from pathlib import Path
HEAD = """@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title {title}
"""
NOVENV = """if not exist "venv\\Scripts\\python.exe" goto no_venv
"""
NOVENV_LABEL = """
:no_venv
echo.
echo ОШИБКА: платформа ещё не установлена.
echo Что делать: сначала запустите установить.bat двойным щелчком.
echo.
pause
exit /b 1
"""
ADMIN_CHECK = """net session >nul 2>&1
if errorlevel 1 goto no_admin
"""
ADMIN_LABEL = """
:no_admin
echo.
echo ОШИБКА: этому файлу нужны права администратора.
echo Что делать: закройте окно, нажмите на файл ПРАВОЙ кнопкой мыши
echo и выберите «Запуск от имени администратора».
echo.
pause
exit /b 1
"""
files = {}

files["установить.bat"] = HEAD.format(title="Установка платформы") + r"""
echo ============================================================
echo   Установка платформы «Центр управления бизнесом»
echo ============================================================
echo.
echo Папка платформы: %CD%
echo.

rem ---- 1. Ищем Python 3.11 или новее
set "PY="
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.12"
if defined PY goto have_python
py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.11"
if defined PY goto have_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto have_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto have_python

echo ОШИБКА: на компьютере не найден Python версии 3.11 или новее.
echo.
echo Что делать:
echo   1. Откройте в браузере python.org, раздел Downloads,
echo      скачайте Python 3.12 для Windows ^(Windows installer 64-bit^).
echo   2. При установке ОБЯЗАТЕЛЬНО поставьте галочку
echo      «Add python.exe to PATH» внизу первого окна, затем «Install Now».
echo   3. После установки запустите установить.bat ещё раз.
echo.
pause
exit /b 1

:have_python
for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
echo [1/4] Найден Python %PYVER%

rem ---- 2. Отдельное окружение для платформы
if exist "venv\Scripts\python.exe" goto venv_ready
echo [2/4] Создаём окружение платформы (папка venv)...
%PY% -m venv venv
if errorlevel 1 goto venv_failed
goto venv_done
:venv_ready
echo [2/4] Окружение уже есть — используем его
:venv_done

rem ---- 3. Библиотеки
echo [3/4] Устанавливаем библиотеки (нужен интернет, 1-3 минуты)...
if exist "wheels" goto offline_install
"venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip >nul 2>&1
"venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto pip_failed
goto pip_done
:offline_install
echo       Найдена папка wheels — ставим без интернета
"venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --no-index --find-links wheels -r requirements.txt colorama
if not errorlevel 1 goto pip_done
echo       Из папки wheels поставилось не всё — докачиваем из интернета...
"venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --find-links wheels -r requirements.txt
if errorlevel 1 goto pip_failed
:pip_done

rem ---- 4. Настройки и база
echo [4/4] Готовим настройки и базу данных...
"venv\Scripts\python.exe" run.py setup
if errorlevel 1 goto setup_failed

rem ---- Брандмауэр (только если запущено от администратора)
for /f %%p in ('venv\Scripts\python.exe run.py port') do set "PORT=%%p"
net session >nul 2>&1
if errorlevel 1 goto no_firewall
netsh advfirewall firewall delete rule name="Центр управления бизнесом" >nul 2>&1
netsh advfirewall firewall add rule name="Центр управления бизнесом" dir=in action=allow protocol=TCP localport=%PORT%,443 >nul
echo       В брандмауэре Windows открыт порт %PORT% и 443
goto firewall_done
:no_firewall
echo       Брандмауэр не настроен: для этого нужен запуск от имени администратора.
echo       Если панель должна открываться с других компьютеров — запустите
echo       установить.bat ещё раз правой кнопкой → «Запуск от имени администратора».
:firewall_done

echo.
echo ============================================================
echo   Установка завершена.
echo   Дальше: запустите запустить.bat — откроется панель в браузере.
echo   Первый вход: логин admin, пароль admin.
echo ============================================================
echo.
pause
exit /b 0

:venv_failed
echo.
echo ОШИБКА: не удалось создать окружение Python.
echo Что делать: переустановите Python с python.org (галочка «Add python.exe to PATH»)
echo и проверьте, что в этой папке можно создавать файлы (не «Только чтение»).
echo.
pause
exit /b 1

:pip_failed
echo.
echo ОШИБКА: не удалось скачать и установить библиотеки.
echo Что делать:
echo   - проверьте, что на сервере работает интернет (откройте любой сайт в браузере);
echo   - если интернет через прокси — попросите разработчика прислать папку wheels
echo     с библиотеками и положите её рядом с этим файлом, затем повторите установку.
echo.
pause
exit /b 1

:setup_failed
echo.
echo ОШИБКА: не удалось подготовить базу данных.
echo Что делать: проверьте, что в папке платформы можно создавать файлы,
echo и пришлите разработчику текст этого окна.
echo.
pause
exit /b 1
"""

files["запустить.bat"] = HEAD.format(title="Запуск платформы") + NOVENV + r"""
"venv\Scripts\python.exe" run.py start
set "RC=%errorlevel%"
echo.
if "%RC%"=="0" goto ok
if "%RC%"=="2" goto busy
echo Не получилось запустить. Текст выше поможет разработчику.
pause
exit /b 1
:busy
pause
exit /b 1
:ok
timeout /t 5 >nul
exit /b 0
""" + NOVENV_LABEL

files["остановить.bat"] = HEAD.format(title="Остановка платформы") + NOVENV + r"""
"venv\Scripts\python.exe" run.py stop
echo.
pause
exit /b 0
""" + NOVENV_LABEL

files["резервная-копия.bat"] = HEAD.format(title="Резервная копия") + NOVENV + r"""
echo Делаем резервную копию...
"venv\Scripts\python.exe" run.py backup
if errorlevel 1 goto failed
echo.
echo Копии лежат в папке backups. Раз в неделю переносите свежую копию
echo на другой диск, флешку или в облако — в ней все пароли и ключи.
echo.
pause
exit /b 0
:failed
echo.
echo ОШИБКА: копию сделать не удалось. Проверьте, что на диске есть свободное место.
pause
exit /b 1
""" + NOVENV_LABEL

files["восстановить-из-копии.bat"] = HEAD.format(title="Восстановление из копии") + NOVENV + r"""
echo ============================================================
echo   Восстановление данных из резервной копии
echo ============================================================
echo.
echo Как пользоваться: перетащите файл копии (backup-....zip) мышью
echo прямо на этот файл. Если просто запустить — возьмётся самая свежая
echo копия из папки backups.
echo.
echo Текущие данные будут заменены (перед этим они тоже сохранятся копией).
echo.
choice /c 12 /n /m "Продолжить? Нажмите 1 — да, 2 — нет: "
if errorlevel 2 exit /b 0
"venv\Scripts\python.exe" run.py stop >nul
"venv\Scripts\python.exe" run.py restore %1
if errorlevel 1 goto failed
echo.
echo Готово. Теперь запустите запустить.bat.
pause
exit /b 0
:failed
echo.
pause
exit /b 1
""" + NOVENV_LABEL

files["автозапуск-включить.bat"] = HEAD.format(title="Включение автозапуска") + NOVENV + ADMIN_CHECK + r"""
echo Включаем автозапуск платформы при включении сервера...
"venv\Scripts\python.exe" run.py autostart-xml "%~dp0data\autostart.xml" >nul
if errorlevel 1 goto failed
schtasks /Create /TN "BusinessPlatform" /XML "%~dp0data\autostart.xml" /F >nul
if errorlevel 1 goto failed
del "%~dp0data\autostart.xml" >nul 2>&1
echo Задача «BusinessPlatform» создана в Планировщике заданий Windows.
echo.
echo Перезапускаем платформу уже через автозапуск...
"venv\Scripts\python.exe" run.py stop >nul
schtasks /Run /TN "BusinessPlatform" >nul
"venv\Scripts\python.exe" run.py wait 60
if errorlevel 1 goto not_started
echo.
echo Готово. Теперь платформа будет запускаться сама после каждой перезагрузки,
echo даже если никто не вошёл в Windows.
echo.
pause
exit /b 0
:not_started
echo.
echo Автозапуск включён, но платформа пока не ответила. Подождите минуту и откройте
echo http://localhost:8080 — или перезагрузите сервер и проверьте снова.
pause
exit /b 1
:failed
echo.
echo ОШИБКА: Windows не дала создать задачу автозапуска.
echo Что делать: убедитесь, что файл запущен «от имени администратора».
pause
exit /b 1
""" + NOVENV_LABEL + ADMIN_LABEL

files["автозапуск-выключить.bat"] = HEAD.format(title="Выключение автозапуска") + ADMIN_CHECK + r"""
schtasks /Query /TN "BusinessPlatform" >nul 2>&1
if errorlevel 1 goto not_found
schtasks /End /TN "BusinessPlatform" >nul 2>&1
schtasks /Delete /TN "BusinessPlatform" /F >nul
if errorlevel 1 goto failed
echo Автозапуск выключен. Платформа остановлена.
echo Запускать её теперь нужно вручную: запустить.bat.
echo.
pause
exit /b 0
:not_found
echo Автозапуск и так не был включён — делать ничего не нужно.
echo.
pause
exit /b 0
:failed
echo ОШИБКА: не удалось удалить задачу. Запустите файл «от имени администратора».
pause
exit /b 1
""" + ADMIN_LABEL

files["сменить-порт.bat"] = HEAD.format(title="Смена порта") + NOVENV + r"""
echo Сейчас платформа работает на порту:
"venv\Scripts\python.exe" run.py port
echo.
echo Порт меняют, только если текущий занят другой программой.
echo Обычно подходят 8081, 8090, 8888.
echo.
set "NEWPORT="
set /p "NEWPORT=Введите новый порт и нажмите Enter: "
if "%NEWPORT%"=="" exit /b 0
"venv\Scripts\python.exe" run.py set-port %NEWPORT%
echo.
pause
exit /b 0
""" + NOVENV_LABEL

files["сбросить-пароль-администратора.bat"] = HEAD.format(title="Сброс пароля администратора") + NOVENV + r"""
echo Пароль входа admin будет сброшен на admin.
echo При следующем входе панель попросит придумать новый.
echo.
choice /c 12 /n /m "Сбросить? Нажмите 1 — да, 2 — нет: "
if errorlevel 2 exit /b 0
"venv\Scripts\python.exe" run.py reset-admin
echo.
pause
exit /b 0
""" + NOVENV_LABEL

for name, body in files.items():
    body = body.lstrip("\n")
    Path(name).write_bytes(body.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
print(len(files), "файлов")
