#!/bin/bash
# Включение автозапуска. Открывается двойным щелчком в Finder.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8
[ -n "$LANG" ] || export LANG=en_US.UTF-8
printf '\033]0;%s\007' "Включение автозапуска"
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
