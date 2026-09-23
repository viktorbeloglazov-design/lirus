@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Выключение автозапуска
net session >nul 2>&1
if errorlevel 1 goto no_admin

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

:no_admin
echo.
echo ОШИБКА: этому файлу нужны права администратора.
echo Что делать: закройте окно, нажмите на файл ПРАВОЙ кнопкой мыши
echo и выберите «Запуск от имени администратора».
echo.
pause
exit /b 1
