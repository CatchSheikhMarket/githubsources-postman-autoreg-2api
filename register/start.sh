#!/bin/bash
# Панель реггера Postman → http://127.0.0.1:<PORT>
# Запуск:  bash start.sh               — поднимает панель и открывает в браузере
#          bash start.sh --no-browser  — без окна браузера (для тестов/скриптов)
cd "$(dirname "$0")" || exit 1
export -n PYTHONHOME PYTHONPATH 2>/dev/null; unset PYTHONHOME PYTHONPATH LD_LIBRARY_PATH

NO_BROWSER=0
for arg in "$@"; do
  case "$arg" in
    --no-browser) NO_BROWSER=1 ;;
    -h|--help)
      echo "usage: bash start.sh [--no-browser]"
      exit 0 ;;
    *)
      echo "[!] неизвестный аргумент: $arg (см. bash start.sh --help)"
      exit 2 ;;
  esac
done

PORT=$(env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH python3 -c \
  "import json;print(json.load(open('config.json')).get('panel_port',9924))" 2>/dev/null)
PORT="${PORT:-9924}"

if [ ! -f config.json ]; then
  echo "нет config.json. Скопируй шаблон и заполни:"
  echo "  cp config.example.json config.json"
  exit 1
fi

if command -v fuser >/dev/null 2>&1; then
  fuser -k -n tcp "$PORT" 2>/dev/null && sleep 1
fi

mkdir -p data
PANEL_URL="http://127.0.0.1:$PORT"
echo "== панель реггера на $PANEL_URL ($(date '+%Y-%m-%d %H:%M:%S')) ==" | tee -a data/panel.log

if [[ "$NO_BROWSER" != "1" ]]; then
  (
    for i in $(seq 1 40); do
      env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH \
        curl -s -o /dev/null -m 1 "$PANEL_URL/api/summary" && break
      sleep 0.3
    done
    xdg-open "$PANEL_URL" >/dev/null 2>&1 || true
    echo "[реггер] браузер открыт: $PANEL_URL (--no-browser отключает)"
  ) &
fi

# Python с camoufox: из config.json → camoufox_python, иначе системный python3
PY=$(env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH python3 -c \
  "import json;print(json.load(open('config.json')).get('camoufox_python') or '')" 2>/dev/null)
[ -z "$PY" ] || [ ! -x "$PY" ] && PY="$(command -v python3)"

exec env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH \
  "$PY" -u panel/server.py --port "$PORT" > >(tee -a data/panel.log) 2>&1
