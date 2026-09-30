#!/bin/bash
# Восстановление из копии. Открывается двойным щелчком в Finder.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8
[ -n "$LANG" ] || export LANG=en_US.UTF-8
printf '\033]0;%s\007' "Восстановление из копии"
PY="venv/bin/python"
pause() { echo; read -r -p "Нажмите Enter, чтобы закрыть окно..." _; }
clear
if [ ! -x "$PY" ]; then
  echo
  echo "ОШИБКА: платформа ещё не установлена."
  echo "Что делать: сначала дважды щёлкните файл установить.command."
  pause
  exit 1
fi

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
