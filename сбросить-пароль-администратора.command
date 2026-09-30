#!/bin/bash
# Сброс пароля администратора. Открывается двойным щелчком в Finder.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8
[ -n "$LANG" ] || export LANG=en_US.UTF-8
printf '\033]0;%s\007' "Сброс пароля администратора"
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

echo "Пароль входа admin будет сброшен на admin."
echo "При следующем входе панель попросит придумать новый."
echo
read -r -p "Сбросить? Введите 1 — да, 2 — нет, и нажмите Enter: " ANSWER
[ "$ANSWER" = "1" ] || exit 0
"$PY" run.py reset-admin
pause
exit 0
