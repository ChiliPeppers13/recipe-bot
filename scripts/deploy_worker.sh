#!/bin/bash
# Разворачивает воркер и базу D1. Запускать после `wrangler login`.
#   ./scripts/deploy_worker.sh
# Печатает адрес API, который нужно подставить в мини-апп.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/worker" || exit 1

WR="npx -y wrangler@latest"
DB_NAME="recipes"

# токен бота и ключ синхронизации берём локально, в репозиторий они не попадают
BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ROOT/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")"
[ -z "$BOT_TOKEN" ] && { echo "нет TELEGRAM_BOT_TOKEN в .env"; exit 1; }

SYNC_KEY_FILE="$ROOT/.sync_key"
if [ ! -f "$SYNC_KEY_FILE" ]; then
  /usr/bin/python3 -c "import secrets;print(secrets.token_urlsafe(32))" > "$SYNC_KEY_FILE"
  chmod 600 "$SYNC_KEY_FILE"
fi
SYNC_KEY="$(cat "$SYNC_KEY_FILE")"

# 1. база
DB_ID="$($WR d1 list --json 2>/dev/null | /usr/bin/python3 -c "
import json,sys
try: rows=json.load(sys.stdin)
except Exception: rows=[]
print(next((r['uuid'] for r in rows if r.get('name')=='$DB_NAME'), ''))")"

if [ -z "$DB_ID" ]; then
  echo "создаю базу $DB_NAME…"
  $WR d1 create "$DB_NAME"
  DB_ID="$($WR d1 list --json | /usr/bin/python3 -c "
import json,sys
rows=json.load(sys.stdin)
print(next(r['uuid'] for r in rows if r.get('name')=='$DB_NAME'))")"
else
  echo "база уже есть: $DB_ID"
fi

# 2. прописываем binding в wrangler.toml (перезаписываем блок целиком)
/usr/bin/python3 - "$DB_ID" <<'PY'
import re, sys, pathlib
db_id = sys.argv[1]
p = pathlib.Path("wrangler.toml")
t = p.read_text()
block = f'''[[d1_databases]]
binding = "DB"
database_name = "recipes"
database_id = "{db_id}"
'''
if "[[d1_databases]]" in t:
    t = re.sub(r"\[\[d1_databases\]\][\s\S]*?(?=\n\[|\Z)", block, t)
else:
    t = t.replace("[vars]", block + "\n[vars]")
p.write_text(t)
print("wrangler.toml → database_id", db_id)
PY

# 3. схема и данные
echo "заливаю схему…"
$WR d1 execute "$DB_NAME" --remote --file=schema.sql --yes
echo "заливаю данные…"
$WR d1 execute "$DB_NAME" --remote --file=seed.sql --yes

# 4. секреты
printf '%s' "$BOT_TOKEN" | $WR secret put BOT_TOKEN >/dev/null
printf '%s' "$SYNC_KEY" | $WR secret put SYNC_KEY >/dev/null
echo "секреты установлены"

# 5. деплой
$WR deploy 2>&1 | tee "$ROOT/logs/worker_deploy.log"

URL="$(grep -oE 'https://[a-z0-9.-]+\.workers\.dev' "$ROOT/logs/worker_deploy.log" | head -1)"
if [ -z "$URL" ]; then
  echo "адрес не найден в логе деплоя — посмотри logs/worker_deploy.log"
  exit 1
fi

echo "API: $URL"
echo "$URL" > "$ROOT/.worker_url"
echo "проверка: $(curl -s --max-time 15 "$URL/api/health")"
