#!/bin/bash
# Остановка платформы. Открывается двойным щелчком в Finder.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8
[ -n "$LANG" ] || export LANG=en_US.UTF-8
printf '\033]0;%s\007' "Остановка платформы"
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

"$PY" run.py stop
pause
exit 0
