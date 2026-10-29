#!/usr/bin/env bash
# Поднимает локальную часть: API на маке + бота. Туннель больше не нужен —
# чтение, правки, план и покупки идут через Cloudflare Workers + D1,
# мак требуется только для импорта новых ссылок (yt-dlp + whisper).
set -uo pipefail
cd "$(dirname "$0")"

unset PYTHONPATH PYTHONHOME PYTHONSTARTUP VIRTUAL_ENV

PY_SERVER="/Users/chilipeppers/.hermes/installs/702cdcf0f0d70170/environments/ed360d58e0d04daabcb8285d84e5c396/venv/bin/python"
PY_BOT=".venv/bin/python"
PORT=8891

mkdir -p logs

# подчищаем прошлые запуски (по полному пути, чтобы не задеть чужие процессы)
pkill -f "$PWD/app/api_server.py" 2>/dev/null
pkill -f "$PWD/app/bot.py" 2>/dev/null
pkill -f "cloudflared tunnel --url http://127.0.0.1:$PORT" 2>/dev/null
sleep 1

# адрес облачного API: сначала .worker_url, иначе последний docs/api.json
API_BASE=""
[ -f .worker_url ] && API_BASE="$(tr -d '\n' < .worker_url)"
if [ -z "$API_BASE" ] && [ -f docs/api.json ]; then
  API_BASE="$(/usr/bin/python3 -c 'import json;print(json.load(open("docs/api.json")).get("api",""))' 2>/dev/null)"
fi
export API_BASE

if [ -n "$API_BASE" ]; then
  echo "☁️  API (облако): $API_BASE"
else
  echo "⚠️  облачный API не настроен — правки пойдут на мак"
fi

"$PY_SERVER" "$PWD/app/api_server.py" --port "$PORT" > logs/server.log 2>&1 &
SERVER_PID=$!

# публикуем актуальный адрес API в мини-апп
"$PY_BOT" scripts/publish.py -m "api endpoint" >/dev/null 2>&1 || true

cleanup() { kill "$SERVER_PID" "${BOT_PID:-}" 2>/dev/null; }
trap cleanup EXIT INT TERM

# постоянный адрес мини-аппа — GitHub Pages (можно переопределить через WEBAPP_URL в .env)
WEBAPP_URL="${WEBAPP_URL:-https://chilipeppers13.github.io/recipe-bot}"
echo "🌐 мини-апп: $WEBAPP_URL"

export WEBAPP_URL
"$PY_BOT" "$PWD/app/bot.py" &
BOT_PID=$!

echo "🤖 бот запущен — открой Telegram и пришли ссылку"
wait "$BOT_PID"
