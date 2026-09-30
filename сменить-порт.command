#!/bin/bash
# Смена порта. Открывается двойным щелчком в Finder.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8
[ -n "$LANG" ] || export LANG=en_US.UTF-8
printf '\033]0;%s\007' "Смена порта"
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
