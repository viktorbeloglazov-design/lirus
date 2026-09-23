@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Резервная копия
if not exist "venv\Scripts\python.exe" goto no_venv

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

:no_venv
echo.
echo ОШИБКА: платформа ещё не установлена.
echo Что делать: сначала запустите установить.bat двойным щелчком.
echo.
pause
exit /b 1
