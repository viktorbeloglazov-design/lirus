@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Смена порта
if not exist "venv\Scripts\python.exe" goto no_venv

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

:no_venv
echo.
echo ОШИБКА: платформа ещё не установлена.
echo Что делать: сначала запустите установить.bat двойным щелчком.
echo.
pause
exit /b 1
