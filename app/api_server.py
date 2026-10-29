"""API кулинарной книги: всё поверх SQLite.

Сервер делает две вещи:
  1. принимает импорт ссылок от бота и API мини-аппа;
  2. после каждого изменения пересобирает статичную копию сайта (docs/) и пушит.

Доступ к правкам — только с подписью Telegram WebApp (initData) и только
для id из белого списка. Чтение — свободное (это ваш личный сайт-снимок).

    python api_server.py --port 8891
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
WEBAPP = ROOT / "webapp" / "app.html"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "scripts"))

import db  # noqa: E402
from pipeline import build as build_from_video  # noqa: E402
from pipeline import edit_recipe, from_page, from_source_text  # noqa: E402

WEBAPP_BASE = os.getenv("WEBAPP_URL", "https://chilipeppers13.github.io/recipe-bot").rstrip("/")
ACCESS_FILE = Path.home() / ".hermes" / "cache" / "recipe-bot" / "allowed.json"

_lock = threading.Lock()


def _bot_token() -> str:
    env = ROOT.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


BOT_TOKEN = _bot_token()


def verify_init_data(init_data: str, max_age: int = 86400) -> dict | None:
    """Подпись initData: HMAC-SHA256 по отсортированным полям, ключ WebAppData+токен."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return None


def allowed_users() -> tuple[set[int], bool]:
    if ACCESS_FILE.exists():
        try:
            d = json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
            return {int(x) for x in d.get("allowed", [])}, bool(d.get("private", False))
        except Exception as e:
            print(f"⚠️  не прочитал {ACCESS_FILE}: {e}", flush=True)
    return set(), False


WORKER_URL_FILE = ROOT / ".worker_url"
SYNC_KEY_FILE = ROOT / ".sync_key"


def sync_to_worker(recipe: dict | None = None, delete_id: str | None = None) -> None:
    """Отправляем рецепт в облако (D1). Мак становится необязательным для чтения и правок."""
    try:
        base = WORKER_URL_FILE.read_text().strip().rstrip("/")
        key = SYNC_KEY_FILE.read_text().strip()
    except Exception:
        return                                   # облако ещё не настроено — молча живём локально
    if not base:
        return
    body = json.dumps({"delete": delete_id} if delete_id else {"recipe": recipe}).encode()
    req = urllib.request.Request(f"{base}/api/sync", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Sync-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            r.read()
    except Exception as e:
        print(f"sync: не отправлено ({str(e)[:120]})", flush=True)


def publish_now(message: str, wait_for: str = "") -> bool:
    """Пересобрать сайт и запушить. Если ждём — дождаться появления файла на Pages."""
    try:
        import publish as pub  # scripts/publish.py
        pub.build()
        pushed = pub.publish(message)
        if pushed and wait_for:
            url = f"{WEBAPP_BASE}/recipes/{wait_for}.json"
            deadline = time.time() + 100
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(url, timeout=8) as r:
                        if r.status == 200:
                            return True
                except Exception:
                    pass
                time.sleep(3)
        return pushed
    except Exception as e:
        print(f"⚠️  публикация не удалась: {e}", flush=True)
        return False


def public_recipe(rec: dict) -> dict:
    out = dict(rec)
    out.pop("transcript", None)
    return out


def suggest_by_ingredients(have: list[str], limit: int = 20, exclude: list[str] | None = None) -> list[dict]:
    """Подбор из СВОЕЙ книги: сколько ингредиентов уже есть под рукой."""
    have_low = [h.strip().lower() for h in have if h.strip()]
    if not have_low:
        return []
    out = []
    banned = [b.lower() for b in (exclude or [])]
    for rec in db.list_recipes():
        if db._is_banned(rec, banned):
            continue
        ings = [(i.get("name") or "").lower() for i in rec.get("ingredients") or []]
        if not ings:
            continue
        matched, missing = [], []
        for name, ing in zip(ings, rec.get("ingredients") or []):
            if any(h in name or name in h for h in have_low if h):
                matched.append(ing.get("name"))
            else:
                missing.append(ing.get("name"))
        if matched:
            out.append({
                "id": rec["id"], "title": rec["title"], "emoji": rec.get("emoji"),
                "category": rec.get("category"), "time_min": rec.get("time_min"),
                "image": rec.get("image") or "",
                "matched": matched,
                "missing": missing[:6],
                "score": round(len(matched) / max(len(ings), 1), 2),
            })
    out.sort(key=lambda x: (-x["score"], len(x["missing"])))
    return out[:limit]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/api/" in self.path:
            print(f"  API {self.path.split('?')[0]} → {args[1]}", flush=True)

    # --- служебное ---
    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-TG-Init-Data")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b"")

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _html(self):
        index = [{
            "id": r["id"], "title": r["title"], "emoji": r.get("emoji"),
            "category": r.get("category"), "time_min": r.get("time_min"),
            "steps": len(r.get("steps") or []), "favorite": bool(r.get("favorite")),
            "image": r.get("image") or "", "added": int(r.get("created") or 0),
        } for r in db.export_rows()]
        seed = ('<script>window.__API__=""' + ';window.__INDEX__='
                + json.dumps(index, ensure_ascii=False) + ';</script>')
        html = WEBAPP.read_text(encoding="utf-8").replace(
            '<div id="nav"></div>', '<div id="nav"></div>\n' + seed)
        self._send(200, html.encode(), "text/html; charset=utf-8")

    def _who(self) -> dict | None:
        user = verify_init_data(self.headers.get("X-TG-Init-Data") or "")
        if not user:
            return None
        ids, private = allowed_users()
        if private and int(user.get("id", 0)) not in ids:
            return None
        return user

    # --- чтение ---
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)

        def one(key: str, default: str = "") -> str:
            return (qs.get(key) or [default])[0]

        if path in ("/", "/index.html"):
            return self._html()

        if path.startswith("/recipes/"):
            name = path[len("/recipes/"):]
            if name == "index.json":
                return self._json([{
                    "id": r["id"], "title": r["title"], "emoji": r.get("emoji"),
                    "category": r.get("category"), "time_min": r.get("time_min"),
                    "steps": len(r.get("steps") or []), "favorite": bool(r.get("favorite")),
                    "image": r.get("image") or "", "added": int(r.get("created") or 0),
                } for r in db.export_rows()])
            rid = name.split(".")[0]
            rec = db.get_recipe(rid)
            return self._json(public_recipe(rec)) if rec else self._json({"error": "не найдено"}, 404)

        if path == "/api/recipes":
            rows = db.list_recipes(
                q=one("q"), category=one("category"), tag=one("tag"),
                max_time=int(one("max_time", "0") or 0),
                favorite_only=one("favorite") == "1",
                ingredient=one("ingredient"),
                exclude=db.banned_terms(),     # «исключить ингредиенты» и аллергены реально работают
            )
            return self._json([{
                "id": r["id"], "title": r["title"], "subtitle": r.get("subtitle"),
                "emoji": r.get("emoji"), "category": r.get("category"),
                "time_min": r.get("time_min"), "servings": r.get("servings"),
                "steps": len(r.get("steps") or []), "favorite": bool(r.get("favorite")),
                "image": r.get("image") or "",
                "ingredients": [i.get("name") for i in (r.get("ingredients") or [])],
                "added": int(r.get("created") or 0),
            } for r in rows])

        if path == "/api/recipe":
            rec = db.get_recipe(one("id"))
            return self._json(public_recipe(rec)) if rec else self._json({"error": "не найдено"}, 404)

        if path == "/api/meta":
            return self._json({
                "categories": db.all_categories(),
                "tags": db.all_tags()[:30],
                "stats": db.stats(),
                "settings": db.get_settings(),
            })

        if path == "/api/settings":
            return self._json(db.get_settings())

        if path == "/api/plan":
            today = date.today()
            start = one("start", (today - timedelta(days=today.weekday())).isoformat())
            end = one("end", (date.fromisoformat(start) + timedelta(days=6)).isoformat())
            return self._json({"start": start, "end": end, "items": db.get_plan(start, end)})

        if path == "/api/shopping":
            return self._json(db.get_shopping())

        if path == "/api/suggest":
            have = [h for h in one("have").split(",") if h.strip()]
            if not have:
                have = db.pantry_have()          # продукты из кладовки
            return self._json(suggest_by_ingredients(have, exclude=db.banned_terms()))

        if path == "/api/pantry":
            return self._json(db.pantry_all())

        if path == "/api/latest":
            rows = db.export_rows()
            return self._json({"id": rows[0]["id"]} if rows else {"id": None})

        self._json({"error": "не найдено"}, 404)

    # --- изменения ---
    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        allowed = (
            "/api/import", "/api/text", "/api/page",      # бот: импорт без подписи
            "/api/save", "/api/delete", "/api/favorite",  # мини-апп
            "/api/plan", "/api/shopping", "/api/settings", "/api/edit", "/api/pantry",
        )
        if route not in allowed:
            return self._json({"error": "не найдено"}, 404)

        # тело читаем ДО отказов: иначе остаток ломает следующее keep-alive соединение
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"error": "плохой JSON"}, 400)

        who = self._who() if route not in ("/api/import", "/api/text", "/api/page") else None
        if route in ("/api/save", "/api/delete", "/api/favorite", "/api/plan",
                     "/api/shopping", "/api/settings", "/api/pantry") and not who:
            return self._json({"error": "нет доступа: открой экран из Телеграма"}, 403)

        try:
            # ---------- импорт ----------
            if route == "/api/import":
                url = (payload.get("url") or "").strip()
                if not url.startswith("http"):
                    return self._json({"error": "нужна ссылка http(s)"}, 400)
                with _lock:
                    recipe = build_from_video(url)
                return self._json(self._store_import(recipe))

            if route == "/api/page":
                url = (payload.get("url") or "").strip()
                if not url.startswith("http"):
                    return self._json({"error": "нужна ссылка http(s)"}, 400)
                with _lock:
                    recipe = from_page(url)
                return self._json(self._store_import(recipe))

            if route == "/api/text":
                with _lock:
                    recipe = from_source_text(
                        (payload.get("text") or ""), title=(payload.get("title") or ""),
                        origin="текст от пользователя",
                    )
                return self._json(self._store_import(recipe))

            # ---------- правка ----------
            if route == "/api/save":
                rid = (payload.get("id") or "").strip()
                rec = payload.get("recipe") or {}
                existing = db.get_recipe(rid)
                if not existing:
                    return self._json({"error": "рецепт не найден"}, 404)
                if not isinstance(rec.get("ingredients"), list) or not isinstance(rec.get("steps"), list):
                    return self._json({"error": "нужны ингредиенты и шаги"}, 400)
                merged = {**existing, **rec}
                for i, s in enumerate(merged["steps"], 1):
                    s["n"] = i
                db.save_recipe(merged, rid=rid)
                sync_to_worker(recipe=db.get_recipe(rid))
                publish_now(f"edit: {merged.get('title')}")
                return self._json({"id": rid, "ok": True})

            if route == "/api/edit":
                rid = (payload.get("id") or "").strip()
                instruction = (payload.get("instruction") or "").strip()
                existing = db.get_recipe(rid)
                if not existing or not instruction:
                    return self._json({"error": "нужен id и текст правки"}, 400)
                with _lock:
                    updated = edit_recipe(existing, instruction)
                db.save_recipe(updated, rid=rid)
                publish_now(f"edit: {updated.get('title')}")
                return self._json({"id": rid, "title": updated.get("title"), "ok": True})

            if route == "/api/delete":
                title = db.delete_recipe((payload.get("id") or "").strip())
                if title is not None:
                    sync_to_worker(delete_id=payload["id"])
                if title is None:
                    return self._json({"error": "рецепт не найден"}, 404)
                publish_now(f"delete: {title}")
                return self._json({"deleted": payload["id"], "title": title})

            if route == "/api/favorite":
                state = db.toggle_favorite((payload.get("id") or "").strip())
                publish_now("favorite")
                return self._json({"favorite": state})

            # ---------- план ----------
            if route == "/api/plan":
                action = payload.get("action")
                if action == "set":
                    db.set_plan(payload["date"], payload["meal"], payload["recipe_id"])
                elif action == "clear":
                    db.clear_plan(payload["date"], payload["meal"])
                elif action == "move":
                    db.move_plan(payload["date"], payload["meal"],
                                 payload["new_date"], payload["new_meal"])
                else:
                    return self._json({"error": "неизвестное действие"}, 400)
                return self._json({"ok": True})

            # ---------- покупки ----------
            if route == "/api/shopping":
                action = payload.get("action")
                if action == "build":
                    ids = payload.get("recipes") or []
                    n = db.build_shopping(ids, replace=bool(payload.get("replace")))
                    return self._json({"added": n, "items": db.get_shopping()})
                if action == "add":
                    db.shopping_add(payload.get("name", ""), payload.get("qty"), payload.get("unit", ""))
                elif action == "toggle":
                    db.shopping_toggle(int(payload["id"]))
                elif action == "delete":
                    db.shopping_delete(int(payload["id"]))
                elif action == "clear":
                    db.shopping_clear(bool(payload.get("done_only")))
                else:
                    return self._json({"error": "неизвестное действие"}, 400)
                return self._json({"ok": True, "items": db.get_shopping()})

            # ---------- кладовка ----------
            if route == "/api/pantry":
                action = payload.get("action")
                if action == "add":
                    db.pantry_add(payload.get("name", ""))
                elif action == "toggle":
                    db.pantry_toggle(int(payload["id"]))
                elif action == "delete":
                    db.pantry_delete(int(payload["id"]))
                elif action == "clear_have":
                    for row in db.pantry_all():
                        if row["have"]:
                            db.pantry_toggle(row["id"])
                else:
                    return self._json({"error": "неизвестное действие"}, 400)
                return self._json({"ok": True, "items": db.pantry_all()})

            # ---------- настройки ----------
            if route == "/api/settings":
                return self._json(db.set_settings(payload.get("patch") or {}))

        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)[:400]}, 500)

    # --- импорт: дубликаты и сохранение ---
    def _store_import(self, recipe: dict) -> dict:
        dup = db.find_duplicate(recipe)
        if dup:
            return {"duplicate": True, "id": dup["id"], "title": dup["title"]}
        rid = db.save_recipe(recipe)
        saved = db.get_recipe(rid)
        if saved:
            saved["transcript"] = recipe.get("transcript") or ""     # в облако уходит и расшифровка
            sync_to_worker(recipe=saved)
        live = publish_now(f"recipe: {recipe.get('title')}", wait_for=rid)
        return {"id": rid, "title": recipe.get("title"), "live": live, "duplicate": False}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8891)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    db.init()
    seeded = db.pantry_seed_defaults()
    if seeded:
        print(f"кладовка: добавлен базовый набор ({seeded} продуктов)", flush=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"API: http://{args.host}:{args.port} · база: {db.DB_PATH}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен", flush=True)


if __name__ == "__main__":
    main()
