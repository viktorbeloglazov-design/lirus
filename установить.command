#!/bin/bash
# Установка платформы. Открывается двойным щелчком в Finder.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8
[ -n "$LANG" ] || export LANG=en_US.UTF-8
printf '\033]0;%s\007' "Установка платформы"
PY="venv/bin/python"
pause() { echo; read -r -p "Нажмите Enter, чтобы закрыть окно..." _; }
clear

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
