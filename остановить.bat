@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Остановка платформы
if not exist "venv\Scripts\python.exe" goto no_venv

"venv\Scripts\python.exe" run.py stop
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
