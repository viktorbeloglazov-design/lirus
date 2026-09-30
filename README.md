# Центр управления бизнесом

Своё приложение вместо n8n: веб-панель управления, подключения ко всем системам компании
(1С, Битрикс24, Gmail и Google Диск, Wildberries, Ozon, Яндекс Маркет), ядро на Claude API
и бот в MAX / Telegram для сотрудников. Разворачивается на Windows (сервер или виртуальная машина)
или на Mac.

Инструкция для системного администратора — **[ИНСТРУКЦИЯ.md](ИНСТРУКЦИЯ.md)**, установка на Mac —
**[ИНСТРУКЦИЯ-MAC.md](ИНСТРУКЦИЯ-MAC.md)**.

## Этапы

1. **Каркас платформы** — панель, подключения с проверкой связи, роли, тестирование, журнал,
   резервные копии, установка .bat-файлами. ← сделано
2. Коннектор к 1С с выгрузкой в локальную базу, ТЗ программисту 1С на расширение с HTTP-сервисами.
3. Ядро на Claude.
4. Бот MAX (запасной — Telegram).
5. Маркетплейсы, Битрикс24, почта.
6. Действия с записью (задачи, письма, заказы) с подтверждением в боте.

## Для разработчика

Стек: Python 3.11+, Starlette, Jinja2, SQLite, uvicorn, httpx, cryptography. Серверный рендеринг,
без сборки фронтенда и внешних библиотек в браузере.

```
app/
  main.py          маршруты и страницы
  config.py        пути, .env (только HOST/PORT)
  db.py            SQLite, схема
  crypto.py        Fernet, ключ data/secret.key, маски секретов
  auth.py          вход, сессии, CSRF, роли и права
  store.py         настройки, журнал, подключения
  diagnostics.py   экран «Тестирование» и отчёт
  backup.py        резервные копии
  tls.py           сертификат HTTPS (.pfx или .crt+.key)
  runtime.py       перезапуск из панели
  connectors/      типы подключений: поля формы, подсказки, проверка связи
  templates/ static/
run.py             serve | start | stop | status | backup | restore | reset-admin | set-port | setup
tools/make_bat.py  генерирует .bat-файлы (UTF-8, CRLF, chcp 65001)
tools/make_command.py  генерирует .command-файлы для Mac (bash 3.2, LF, права 755)
tools/build_release.py  архивы для Windows и Mac с библиотеками внутри
tests/             test_connectors.py (подменённые ответы), scenarios.py (живой сервер), fake_1c.py
```

Новый тип подключения — один объект `ConnectorType` в `app/connectors/`: список `Field`,
шаги «где взять» и асинхронная функция проверки, которая возвращает `CheckResult`
(ok / warn / error + сообщение + «что делать»). Форма, маски секретов, шифрование и
экран тестирования подхватывают его автоматически.

Запуск для разработки:

```
python -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python run.py serve
venv/bin/python tests/test_connectors.py
venv/bin/python tests/fake_1c.py 8091 https & venv/bin/python tests/fake_1c.py 8092 &
venv/bin/python tests/scenarios.py      # на чистой базе
```
