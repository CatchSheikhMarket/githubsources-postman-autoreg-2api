#!/bin/bash
# postman2api — мост Postman AI (Agent Mode) → OpenAI/Anthropic API.
# Порт по умолчанию 12049. Сессии: sessions.txt (access_token или cookie:<postman.sid>).
# Запуск: bash start.sh   (первый раз сам соберёт бинарник, если Go установлен)
cd "$(dirname "$0")" || exit 1
unset PYTHONHOME PYTHONPATH LD_LIBRARY_PATH

PORT=$(python3 -c "import json;print(json.load(open('config.json'))['port'])" 2>/dev/null)
export PORT="${PORT:-12049}"
export SESSIONS_FILE="${SESSIONS_FILE:-sessions.txt}"
export POSTMAN_APP_VERSION=$(python3 -c "import json;print(json.load(open('config.json')).get('app_version','12.26.4'))" 2>/dev/null)

mkdir -p data
LOG="$PWD/data/bridge.log"

# Собрать бинарник, если его нет
if [ ! -x ./postman2api ]; then
  if command -v go >/dev/null 2>&1; then
    echo "== сборка postman2api ==" | tee -a "$LOG"
    CGO_ENABLED=0 go build -o postman2api ./cmd/server || { echo "сборка упала"; exit 1; }
  else
    echo "нет бинарника ./postman2api и не найден go."
    echo "Поставь Go 1.24+ и запусти: CGO_ENABLED=0 go build -o postman2api ./cmd/server"
    exit 1
  fi
fi

if [ ! -f sessions.txt ]; then
  echo "# по одной сессии на строку: access_token или cookie:<postman.sid>" > sessions.txt
  echo "== sessions.txt создан пустым: добавь сессию, иначе мост не сможет отвечать ==" | tee -a "$LOG"
fi

# Мёртвый процесс на своём порту гасим только свой
if command -v fuser >/dev/null 2>&1; then
  fuser -k -n tcp "$PORT" >/dev/null 2>&1
fi

echo "== postman2api: мост на 127.0.0.1:$PORT ==" | tee -a "$LOG"
exec ./postman2api 2>&1 | tee -a "$LOG"
