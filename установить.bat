@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Установка платформы

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
