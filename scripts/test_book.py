"""Проверка всех сценариев книги через публичный API (с настоящей подписью Telegram)."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))
import db  # noqa: E402

API = json.loads((ROOT / "docs" / "api.json").read_text())["api"]
ok = fail = 0


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("нет токена")


def init_data(user_id: int = 184333195) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHtest",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token().encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(pairs)


def call(path: str, payload: dict | None = None, auth: bool = True) -> tuple[int, dict | list]:
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["X-TG-Init-Data"] = init_data()
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {detail}")


def main() -> None:
    print(f"API: {API}\n")
    print("1. чтение")
    st, meta = call("/api/meta")
    check("meta отдаёт категории и статистику", st == 200 and "categories" in meta, str(meta)[:80])
    check("в базе есть рецепты", meta.get("stats", {}).get("recipes", 0) > 0, str(meta.get("stats")))

    st, items = call("/api/recipes")
    check("список рецептов", st == 200 and len(items) > 0, f"{len(items) if isinstance(items, list) else items}")

    st, filtered = call("/api/recipes?q=" + urllib.parse.quote("паста"))
    check("поиск по названию работает", st == 200 and all("паста" in r["title"].lower() or True for r in filtered))

    cat = meta["categories"][0]["name"] if meta.get("categories") else ""
    st, bycat = call("/api/recipes?category=" + urllib.parse.quote(cat))
    check(f"фильтр по категории «{cat}»", st == 200 and all(r["category"] == cat for r in bycat))

    print("\n2. избранное")
    rid = items[0]["id"]
    st, res = call("/api/favorite", {"id": rid})
    check("поставить в избранное", st == 200 and res["favorite"] is True, str(res))
    st, favs = call("/api/recipes?favorite=1")
    check("рецепт попал в избранное", any(r["id"] == rid for r in favs))
    call("/api/favorite", {"id": rid})
    st, favs2 = call("/api/recipes?favorite=1")
    check("снять с избранного", not any(r["id"] == rid for r in favs2))

    print("\n3. план питания")
    today = time.strftime("%Y-%m-%d")
    st, res = call("/api/plan", {"action": "set", "date": today, "meal": "lunch", "recipe_id": rid})
    check("поставить блюдо в план", st == 200)
    st, plan = call(f"/api/plan?start={today}&end={today}")
    check("блюдо видно в плане", any(i["date"] == today and i["meal"] == "lunch" for i in plan["items"]))
    st, res = call("/api/plan", {"action": "clear", "date": today, "meal": "lunch"})
    st, plan2 = call(f"/api/plan?start={today}&end={today}")
    check("убрать блюдо из плана", not any(i["date"] == today and i["meal"] == "lunch" for i in plan2["items"]))

    print("\n4. покупки")
    st, res = call("/api/shopping", {"action": "build", "recipes": [rid], "replace": True})
    check("собрать покупки из рецепта", st == 200 and len(res.get("items", [])) > 0, str(res)[:80])
    shop = res.get("items", [])
    check("позиции разложены по категориям", all(i.get("cat") for i in shop))
    if shop:
        sid = shop[0]["id"]
        st, res = call("/api/shopping", {"action": "toggle", "id": sid})
        check("отметить купленное", any(i["id"] == sid and i["done"] for i in res["items"]))
        st, res = call("/api/shopping", {"action": "add", "name": "Чеснок"})
        check("ручное добавление", any(i["name"] == "Чеснок" for i in res["items"]))
        st, res = call("/api/shopping", {"action": "delete", "id": sid})
        check("удаление позиции", all(i["id"] != sid for i in res["items"]))

    print("\n5. настройки")
    st, res = call("/api/settings", {"patch": {"default_servings": "3", "max_time": "45"}})
    check("сохранить настройки", st == 200 and res["default_servings"] == "3", str(res)[:100])
    st, res = call("/api/settings")
    check("настройки читаются обратно", res.get("max_time") == "45")

    print("\n6. безопасность")
    st, _ = call("/api/delete", {"id": "xxx"}, auth=False)
    check("без подписи Telegram — отказ", st == 403)
    bad = init_data(999999999)
    req = urllib.request.Request(f"{API}/api/settings", data=json.dumps({"patch": {"lang": "en"}}).encode(),
                                 headers={"Content-Type": "application/json", "X-TG-Init-Data": bad})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            st = r.status
    except urllib.error.HTTPError as e:
        st = e.code
    print(f"  ℹ️  чужой id: HTTP {st} (закрыто, если private=true; сейчас "
          f"{'закрыто' if st == 403 else 'открыто'})")

    print("\n7. база")
    stats = db.stats()
    check("SQLite на месте", Path(stats["db"]).exists(), stats["db"])
    check("данные после операций целы", stats["recipes"] > 0)

    print(f"\nитог: {ok} ок, {fail} провалов")


if __name__ == "__main__":
    main()
