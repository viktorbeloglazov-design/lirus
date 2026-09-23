@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Запуск платформы
if not exist "venv\Scripts\python.exe" goto no_venv

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

:no_venv
echo.
echo ОШИБКА: платформа ещё не установлена.
echo Что делать: сначала запустите установить.bat двойным щелчком.
echo.
pause
exit /b 1
