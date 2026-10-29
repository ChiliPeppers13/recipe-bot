#!/usr/bin/env bash
# Поднимает всё сразу: сервер + туннель + бот. Останавливается по Ctrl+C.
set -uo pipefail
cd "$(dirname "$0")"

PY_SERVER="/Users/chilipeppers/.hermes/installs/702cdcf0f0d70170/environments/ed360d58e0d04daabcb8285d84e5c396/venv/bin/python"
PY_BOT=".venv/bin/python"
PORT=8891

mkdir -p logs

# подчищаем прошлые запуски (шаблон по полному пути — чтобы не задеть чужие процессы)
pkill -f "$PWD/app/server.py" 2>/dev/null
pkill -f "$PWD/app/api_server.py" 2>/dev/null
pkill -f "$PWD/app/bot.py" 2>/dev/null
pkill -f "cloudflared tunnel --url http://127.0.0.1:$PORT" 2>/dev/null
sleep 1

# сначала туннель: сервер должен знать публичный адрес, иначе затрёт api.json пустым
cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate > logs/tunnel.log 2>&1 &
TUNNEL_PID=$!

URL=""
for _ in $(seq 1 45); do
  URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" logs/tunnel.log 2>/dev/null | head -1)
  [ -n "$URL" ] && break
  sleep 1
done
if [ -z "$URL" ]; then
  echo "❌ туннель не поднялся — смотри logs/tunnel.log"
  exit 1
fi
echo "🔌 API для правок: $URL"
export API_BASE="$URL"

"$PY_SERVER" "$PWD/app/api_server.py" --port "$PORT" > logs/server.log 2>&1 &
SERVER_PID=$!

# публикуем адрес API в api.json, чтобы экран знал, куда отправлять правки
"$PY_BOT" scripts/publish.py -m "api endpoint" >/dev/null 2>&1 || true

cleanup() { kill "$SERVER_PID" "$TUNNEL_PID" "${BOT_PID:-}" 2>/dev/null; }
trap cleanup EXIT INT TERM

# постоянный адрес мини-аппа — GitHub Pages (можно переопределить через WEBAPP_URL в .env)
WEBAPP_URL="${WEBAPP_URL:-https://chilipeppers13.github.io/recipe-bot}"
echo "🌐 мини-апп: $WEBAPP_URL"

export WEBAPP_URL
"$PY_BOT" "$PWD/app/bot.py" &
BOT_PID=$!

echo "🤖 бот запущен — открой Telegram и пришли ссылку"
wait "$BOT_PID"
