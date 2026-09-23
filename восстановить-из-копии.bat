@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Восстановление из копии
if not exist "venv\Scripts\python.exe" goto no_venv

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

:no_venv
echo.
echo ОШИБКА: платформа ещё не установлена.
echo Что делать: сначала запустите установить.bat двойным щелчком.
echo.
pause
exit /b 1
