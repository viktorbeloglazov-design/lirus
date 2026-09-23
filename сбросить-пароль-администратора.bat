@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Сброс пароля администратора
if not exist "venv\Scripts\python.exe" goto no_venv

echo Пароль входа admin будет сброшен на admin.
echo При следующем входе панель попросит придумать новый.
echo.
choice /c 12 /n /m "Сбросить? Нажмите 1 — да, 2 — нет: "
if errorlevel 2 exit /b 0
"venv\Scripts\python.exe" run.py reset-admin
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
