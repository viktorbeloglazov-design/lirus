@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Включение автозапуска
if not exist "venv\Scripts\python.exe" goto no_venv
net session >nul 2>&1
if errorlevel 1 goto no_admin

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

:no_venv
echo.
echo ОШИБКА: платформа ещё не установлена.
echo Что делать: сначала запустите установить.bat двойным щелчком.
echo.
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
