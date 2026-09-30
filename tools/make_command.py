"""Создаёт .command-файлы для Mac: они открываются двойным щелчком в Finder (в окне Терминала).

    python tools/make_command.py

Совместимы со старым bash 3.2, который стоит в macOS. Концы строк — LF, права — 755.
"""
from pathlib import Path

HEAD = """#!/bin/bash
# {title}. Открывается двойным щелчком в Finder.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8
[ -n "$LANG" ] || export LANG=en_US.UTF-8
printf '\\033]0;%s\\007' "{title}"
PY="venv/bin/python"
pause() {{ echo; read -r -p "Нажмите Enter, чтобы закрыть окно..." _; }}
clear
"""

NOVENV = """if [ ! -x "$PY" ]; then
  echo
  echo "ОШИБКА: платформа ещё не установлена."
  echo "Что делать: сначала дважды щёлкните файл установить.command."
  pause
  exit 1
fi
"""

files = {}

files["установить.command"] = HEAD.format(title="Установка платформы") + r"""
echo "============================================================"
echo "  Установка платформы «Центр управления бизнесом»"
echo "============================================================"
echo
echo "Папка платформы: $PWD"
echo

# Снимаем с папки пометку «скачано из интернета», чтобы остальные файлы
# открывались двойным щелчком без предупреждений macOS.
xattr -dr com.apple.quarantine . 2>/dev/null

case "$PWD/" in
  "$HOME/Desktop/"*|"$HOME/Documents/"*|"$HOME/Downloads/"*)
    echo "ВНИМАНИЕ: папка платформы лежит в «Рабочем столе», «Документах» или «Загрузках»."
    echo "Оттуда macOS не даст включить автозапуск. Лучше закрыть это окно, перетащить"
    echo "папку в папку пользователя (Finder: меню «Переход» → «Домой») и запустить"
    echo "установить.command уже оттуда."
    echo
    read -r -p "Продолжить здесь всё равно? Введите 1 — да, 2 — нет, и нажмите Enter: " ANSWER
    [ "$ANSWER" = "1" ] || exit 0
    ;;
esac

# ---- 1. Ищем Python 3.11 или новее. /usr/bin/python3 не трогаем: без инструментов
# разработчика он вместо запуска предлагает их установить.
SYS_PY=""
for c in \
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
  /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
  /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
  /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$c" ] && "$c" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >/dev/null 2>&1; then
    SYS_PY="$c"
    break
  fi
done

if [ -z "$SYS_PY" ]; then
  echo "ОШИБКА: на Mac не найден Python версии 3.11 или новее."
  echo
  echo "Что делать:"
  echo "  1. Сейчас откроется сайт python.org. Скачайте Python 3.12"
  echo "     («macOS 64-bit universal2 installer») и установите его,"
  echo "     нажимая «Продолжить» и «Установить»."
  echo "  2. После установки дважды щёлкните установить.command ещё раз."
  open "https://www.python.org/downloads/macos/" 2>/dev/null
  pause
  exit 1
fi
echo "[1/4] Найден $("$SYS_PY" --version 2>&1)"

# ---- 2. Отдельное окружение для платформы
if [ -x "$PY" ] && "$PY" -c "import sys" >/dev/null 2>&1; then
  echo "[2/4] Окружение уже есть — используем его"
else
  echo "[2/4] Создаём окружение платформы (папка venv)..."
  rm -rf venv
  if ! "$SYS_PY" -m venv venv; then
    echo
    echo "ОШИБКА: не удалось создать окружение Python."
    echo "Что делать: проверьте, что папку платформы можно изменять"
    echo "(она не на флешке «только для чтения» и не внутри скачанного архива),"
    echo "и переустановите Python с python.org."
    pause
    exit 1
  fi
fi

# ---- 3. Библиотеки
echo "[3/4] Устанавливаем библиотеки (1-3 минуты)..."
OK=0
if [ -d wheels ]; then
  echo "      Найдена папка wheels — ставим без интернета"
  if "$PY" -m pip install --disable-pip-version-check -q --no-index --find-links wheels -r requirements.txt; then
    OK=1
  else
    echo "      Из папки wheels поставилось не всё — докачиваем из интернета..."
  fi
fi
if [ "$OK" = "0" ]; then
  "$PY" -m pip install --disable-pip-version-check -q --upgrade pip >/dev/null 2>&1
  if "$PY" -m pip install --disable-pip-version-check -q -r requirements.txt; then
    OK=1
  fi
fi
if [ "$OK" = "0" ]; then
  echo
  echo "ОШИБКА: не удалось скачать и установить библиотеки."
  echo "Что делать:"
  echo "  - проверьте, что на Mac работает интернет (откройте любой сайт);"
  echo "  - или попросите разработчика прислать архив с папкой wheels."
  pause
  exit 1
fi

# ---- 4. Настройки и база
echo "[4/4] Готовим настройки и базу данных..."
if ! "$PY" run.py setup; then
  echo
  echo "ОШИБКА: не удалось подготовить базу данных."
  echo "Что делать: проверьте, что папку платформы можно изменять,"
  echo "и пришлите разработчику текст этого окна."
  pause
  exit 1
fi
chmod +x ./*.command 2>/dev/null

echo
echo "============================================================"
echo "  Установка завершена."
echo "  Дальше: дважды щёлкните запустить.command — откроется панель."
echo "  Первый вход: логин admin, пароль admin."
echo "  Если macOS спросит «Разрешить входящие подключения» — нажмите «Разрешить»."
echo "============================================================"
pause
exit 0
"""

files["запустить.command"] = HEAD.format(title="Запуск платформы") + NOVENV + r"""
"$PY" run.py start
RC=$?
if [ "$RC" != "0" ]; then
  echo
  [ "$RC" = "2" ] || echo "Не получилось запустить. Текст выше поможет разработчику."
  pause
  exit 1
fi
sleep 3
exit 0
"""

files["остановить.command"] = HEAD.format(title="Остановка платформы") + NOVENV + r"""
"$PY" run.py stop
pause
exit 0
"""

files["резервная-копия.command"] = HEAD.format(title="Резервная копия") + NOVENV + r"""
echo "Делаем резервную копию..."
if ! "$PY" run.py backup; then
  echo
  echo "ОШИБКА: копию сделать не удалось. Проверьте, что на диске есть свободное место."
  pause
  exit 1
fi
echo
echo "Копии лежат в папке backups. Раз в неделю переносите свежую копию"
echo "на другой диск, флешку или в облако — в ней все пароли и ключи."
open backups 2>/dev/null
pause
exit 0
"""

files["восстановить-из-копии.command"] = HEAD.format(title="Восстановление из копии") + NOVENV + r"""
echo "============================================================"
echo "  Восстановление данных из резервной копии"
echo "============================================================"
echo
echo "Сейчас откроется окно выбора файла: выберите копию (backup-....zip)."
echo "Текущие данные будут заменены (перед этим они тоже сохранятся копией)."
echo
mkdir -p backups
FILE=$(osascript -e "POSIX path of (choose file with prompt \"Выберите резервную копию (backup-....zip)\" of type {\"zip\", \"public.zip-archive\"} default location (POSIX file \"$PWD/backups\"))" 2>/dev/null)
if [ -z "$FILE" ]; then
  echo "Файл не выбран — ничего не меняем."
  pause
  exit 0
fi
echo "Выбран файл: $FILE"
read -r -p "Восстановить? Введите 1 — да, 2 — нет, и нажмите Enter: " ANSWER
[ "$ANSWER" = "1" ] || exit 0
"$PY" run.py stop >/dev/null
if ! "$PY" run.py restore "$FILE"; then
  pause
  exit 1
fi
echo
echo "Готово. Теперь дважды щёлкните запустить.command."
pause
exit 0
"""

files["автозапуск-включить.command"] = HEAD.format(title="Включение автозапуска") + NOVENV + r"""
echo "Включаем автозапуск: платформа будет запускаться сама после входа в систему"
echo "и подниматься заново, если вдруг упадёт."
echo
if "$PY" run.py autostart-mac on; then
  echo
  echo "Готово. Чтобы платформа работала и после перезагрузки без вас, включите"
  echo "автоматический вход в систему (как — написано в ИНСТРУКЦИЯ-MAC)."
  pause
  exit 0
fi
pause
exit 1
"""

files["автозапуск-выключить.command"] = HEAD.format(title="Выключение автозапуска") + NOVENV + r"""
"$PY" run.py autostart-mac off
pause
exit 0
"""

files["сменить-порт.command"] = HEAD.format(title="Смена порта") + NOVENV + r"""
echo "Сейчас платформа работает на порту:"
"$PY" run.py port
echo
echo "Порт меняют, только если текущий занят другой программой."
echo "Обычно подходят 8081, 8090, 8888."
echo
read -r -p "Введите новый порт и нажмите Enter: " NEWPORT
[ -n "$NEWPORT" ] || exit 0
"$PY" run.py set-port "$NEWPORT"
pause
exit 0
"""

files["сбросить-пароль-администратора.command"] = HEAD.format(title="Сброс пароля администратора") + NOVENV + r"""
echo "Пароль входа admin будет сброшен на admin."
echo "При следующем входе панель попросит придумать новый."
echo
read -r -p "Сбросить? Введите 1 — да, 2 — нет, и нажмите Enter: " ANSWER
[ "$ANSWER" = "1" ] || exit 0
"$PY" run.py reset-admin
pause
exit 0
"""

for name, body in files.items():
    path = Path(name)
    path.write_bytes(body.replace("\r\n", "\n").encode("utf-8"))
    path.chmod(0o755)
print(len(files), "файлов")
