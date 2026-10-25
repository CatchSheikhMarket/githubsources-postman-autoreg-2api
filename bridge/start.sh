#!/bin/bash
# postman2api — мост Postman AI (Agent Mode) → OpenAI/Anthropic API.
# Чистый Python: без Go и без сборки exe.
# Запуск: bash start.sh
cd "$(dirname "$0")" || exit 1
unset PYTHONHOME PYTHONPATH LD_LIBRARY_PATH

PORT=$(python3 -c "import json;print(json.load(open('config.json')).get('port',12049))" 2>/dev/null)
export PORT="${PORT:-12049}"
export SESSIONS_FILE="${SESSIONS_FILE:-sessions.txt}"

mkdir -p data
LOG="$PWD/data/bridge.log"

if [ ! -f config.json ] && [ -f config.example.json ]; then
  cp config.example.json config.json
  echo "[!] создан config.json из примера"
fi

if [ ! -f sessions.txt ]; then
  printf '# по одной сессии на строку: cookie:<postman.sid>\n' > sessions.txt
  echo "[!] sessions.txt создан пустым — добавь сессию или запусти реггер"
fi

# зависимости
if ! python3 -c "import requests" >/dev/null 2>&1; then
  echo "[*] ставлю requests..."
  python3 -m pip install --quiet requests || {
    echo "[ERROR] не удалось установить requests: pip install requests"
    exit 1
  }
fi

if command -v fuser >/dev/null 2>&1; then
  fuser -k -n tcp "$PORT" >/dev/null 2>&1
fi

echo "== postman2api (python) · 127.0.0.1:$PORT ==" | tee -a "$LOG"
exec python3 -u server.py 2>&1 | tee -a "$LOG"