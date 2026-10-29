#!/bin/bash
# Сторож туннеля: проверяет адрес из docs/api.json и, если он мёртв,
# поднимает новый туннель и публикует свежий адрес в мини-апп.
# Запускается по cron раз в 5 минут. Ничего не делает, если всё живо.

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP VIRTUAL_ENV

PY="$ROOT/.venv/bin/python"
LOG="$ROOT/logs/watchdog.log"
mkdir -p "$ROOT/logs"
stamp() { date "+%Y-%m-%d %H:%M:%S"; }
say() { echo "$(stamp) $*" >> "$LOG"; }

API="$(/usr/bin/python3 -c 'import json,sys;print(json.load(open("docs/api.json"))["api"])' 2>/dev/null)"
[ -z "${API:-}" ] && { say "api.json пуст — пропускаю"; exit 0; }

# 1. локальный сервер жив?
if ! curl -s --max-time 5 "http://127.0.0.1:8891/api/pantry" | grep -q '"name"'; then
  say "локальный API мёртв — перезапускаю сервер"
  pkill -f "$ROOT/app/api_server.py" 2>/dev/null
  sleep 1
  nohup "$PY" "$ROOT/app/api_server.py" --port 8891 >> "$ROOT/logs/server.log" 2>&1 &
  sleep 4
fi

# 2. туннель отвечает?
if curl -s --max-time 10 "$API/api/pantry" | grep -q '"name"'; then
  exit 0                      # всё хорошо, молчим
fi

say "туннель не отвечает ($API) — поднимаю новый"
pkill -f "cloudflared tunnel" 2>/dev/null
sleep 2
: > "$ROOT/logs/tunnel.log"
nohup cloudflared tunnel --url http://127.0.0.1:8891 --no-autoupdate >> "$ROOT/logs/tunnel.log" 2>&1 &

NEW=""
for _ in $(seq 1 30); do
  sleep 2
  NEW="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$ROOT/logs/tunnel.log" | head -1)"
  [ -n "$NEW" ] && break
done

if [ -z "$NEW" ]; then
  say "не удалось получить новый адрес за 60 секунд"
  exit 1
fi

say "новый адрес: $NEW"
API_BASE="$NEW" "$PY" "$ROOT/scripts/publish.py" -m "api: tunnel watchdog" >> "$LOG" 2>&1
say "адрес опубликован"
