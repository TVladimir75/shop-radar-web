#!/bin/bash
# Двойной щелчок в Finder — не закрывайте чёрное окно, пока пользуетесь сайтом.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PY="$DIR/.venv/bin/python"
PIP="$DIR/.venv/bin/pip"

if [[ ! -x "$PY" ]]; then
  echo "Создаю окружение Python (первый раз может занять минуту)…"
  /usr/bin/env python3 -m venv "$DIR/.venv"
  "$PIP" install -r "$DIR/requirements.txt"
fi

echo ""
echo "  → Если браузер сам не открылся, вставьте в адресную строку:"
echo "    http://127.0.0.1:8765"
echo "  → Это окно НЕ закрывайте — иначе сайт выключится."
echo ""

"$PY" "$DIR/start.py"
EXIT_CODE=$?

# Не считаем ошибкой обычный выход по Ctrl+C
if [[ $EXIT_CODE -ne 0 && $EXIT_CODE -ne 130 ]]; then
  echo ""
  echo "Код ошибки: $EXIT_CODE"
  echo "Нажмите Enter, чтобы закрыть окно."
  read -r
fi
