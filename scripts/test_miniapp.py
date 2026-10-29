"""Проверка правок из мини-аппа без Телеграма: сами подписываем initData токеном бота."""
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
API = json.loads((ROOT / "docs" / "api.json").read_text())["api"]


def bot_token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("нет токена")


def make_init_data(user_id: int = 184333195) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHtest123",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token().encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(pairs)


def post(path: str, payload: dict, init: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if init:
        headers["X-TG-Init-Data"] = init
    req = urllib.request.Request(f"{API}{path}", data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip().startswith(b"{") else {"raw": raw[:200].decode(errors="replace")})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200].decode(errors="replace")}


def main():
    print("API:", API)

    status, data = post("/api/text", {"text": (
        "Тестовый суп. Ингредиенты: 2 картофелины, 1 луковица, 1 л воды, соль, укроп. "
        "Порезать овощи, варить 20 минут, добавить укроп и подавать."
    )})
    rid = data.get("id")
    print("1. создан рецепт:", status, rid, data.get("title"))
    if not rid:
        raise SystemExit("не создался тестовый рецепт")

    status, data = post("/api/save", {"id": rid, "recipe": {"title": "Без подписи"}})
    print("2. правка без подписи Telegram:", status, data.get("error", "")[:60])

    init = make_init_data()
    status, data = post("/api/save", {"id": rid, "recipe": {
        "title": "Проверка правки",
        "ingredients": [{"name": "картофель", "qty": "2 шт"}],
        "steps": [{"n": 1, "text": "порезать и сварить", "at": ""}],
    }}, init)
    print("3. правка с подписью:", status, data.get("error") or "ok")

    saved = json.loads((Path.home() / ".hermes/cache/recipe-bot/store" / f"{rid}.json").read_text())
    print("   в файле:", saved["title"], "·", [i["name"] for i in saved["ingredients"]],
          "· edited:", saved.get("edited"))

    status, data = post("/api/delete", {"id": rid}, init)
    print("4. удаление с подписью:", status, data.get("deleted"), data.get("title"))

    status, data = post("/api/delete", {"id": rid}, init)
    print("5. повторное удаление:", status, data.get("error"))


if __name__ == "__main__":
    main()
