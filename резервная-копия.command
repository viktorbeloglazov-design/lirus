#!/bin/bash
# Резервная копия. Открывается двойным щелчком в Finder.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8
[ -n "$LANG" ] || export LANG=en_US.UTF-8
printf '\033]0;%s\007' "Резервная копия"
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
