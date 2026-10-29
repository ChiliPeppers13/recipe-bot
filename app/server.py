"""Локальный сервер: отдаёт мини-апп и рецепты.

    python server.py                 # http://127.0.0.1:8787
    python server.py --port 9000

Ручки:
    GET  /                      — мини-апп (последний рецепт)
    GET  /r/<id>                — мини-апп с конкретным рецептом
    GET  /r/<id>/recipe.json    — данные рецепта
    GET  /api/latest            — id последнего рецепта
    POST /api/recipe  {"url": "…"}  — обработать ссылку, вернуть {id}

Для Телеграма сервер должен быть доступен по HTTPS (см. README).
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
WEBAPP = ROOT / "webapp" / "index.html"
STORE = Path.home() / ".hermes" / "cache" / "recipe-bot" / "store"

# куда публикуется сайт — нужен, чтобы дождаться сборки Pages перед ответом боту
WEBAPP_BASE = os.getenv("WEBAPP_URL", "https://chilipeppers13.github.io/recipe-bot").rstrip("/")

# доступ и подпись Telegram: мини-апп присылает initData, сервер проверяет её токеном бота
ACCESS_FILE = Path.home() / ".hermes" / "cache" / "recipe-bot" / "allowed.json"


def _bot_token() -> str:
    for line in (ROOT.parent / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


BOT_TOKEN = _bot_token()


def verify_init_data(init_data: str, max_age: int = 86400) -> dict | None:
    """Проверяем подпись initData (алгоритм Telegram WebApp) и свежесть."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", "")
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
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

sys.path.insert(0, str(ROOT))
from pipeline import build, edit_recipe, from_page, from_source_text  # noqa: E402

STORE.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()


def latest_id() -> str | None:
    files = sorted(STORE.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem if files else None


def find_recipe(rid: str) -> dict | None:
    path = STORE / f"{rid}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def wait_until_live(rid: str, timeout: float = 100.0) -> bool:
    """GitHub Pages собирает сайт не мгновенно: без ожидания кнопка ведёт на 404."""
    url = f"{WEBAPP_BASE}/recipes/{rid}.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                if r.status == 200:
                    print(f"  страница готова: {url}", flush=True)
                    return True
        except Exception:
            pass
        time.sleep(3)
    print(f"  ⚠️  страница не появилась за {int(timeout)} c: {url}", flush=True)
    return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):          # тише в консоли
        if "/api/" in self.path:
            print(f"  API {self.path} → {args[1]}", flush=True)

    # --- helpers ---
    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # мини-апп живёт на github.io и стучится сюда кросс-доменно
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
        self._send(200, WEBAPP.read_bytes(), "text/html; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            return self._html()

        if path == "/api/latest":
            rid = latest_id()
            return self._json({"id": rid} if rid else {"id": None}, 200 if rid else 404)

        # единый путь данных для аппа: /recipes/<id>.json (тот же, что на GitHub Pages)
        if path.startswith("/recipes/"):
            name = path[len("/recipes/"):]
            if name == "index.json":                     # список для «книги рецептов»
                items = []
                for f in sorted(STORE.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                    try:
                        r = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    s = r.get("source") or {}
                    items.append({
                        "id": f.stem,
                        "title": r.get("title") or "Без названия",
                        "emoji": r.get("emoji") or "🍽",
                        "time_min": r.get("time_min"),
                        "steps": len(r.get("steps") or []),
                        "platform": s.get("platform") or "",
                        "thumb": s.get("thumbnail") or "",
                        "added": int(f.stat().st_mtime),
                    })
                return self._json(items)
            rid = latest_id() if name == "latest.json" else name.rsplit(".json", 1)[0]
            recipe = find_recipe(rid) if rid else None
            return self._json(recipe) if recipe else self._json({"error": "not found"}, 404)

        if path.startswith("/r/"):
            rest = path[3:].strip("/")
            if rest.endswith("recipe.json"):
                rid = rest.split("/")[0]
                recipe = find_recipe(rid)
                return self._json(recipe) if recipe else self._json({"error": "not found"}, 404)
            return self._html()                                   # тот же мини-апп

        if path.startswith("/recipe.json"):                       # /recipe.json?id=…
            rid = latest_id()
            if "id=" in self.path:
                rid = self.path.split("id=")[1].split("&")[0]
            recipe = find_recipe(rid) if rid else None
            return self._json(recipe) if recipe else self._json({"error": "no recipes yet"}, 404)

        self._json({"error": "not found"}, 404)

    # --- общее: сохранить рецепт и выложить на GitHub Pages ---
    def _save(self, recipe: dict, rid: str | None = None, wait: bool = True) -> dict:
        rid = rid or uuid.uuid4().hex[:10]
        (STORE / f"{rid}.json").write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")
        live = False
        try:
            self._publish_docs(f"recipe: {recipe.get('title', rid)}")
            if wait:
                live = wait_until_live(rid)      # без этого кнопка в боте ведёт на 404
        except Exception as e:
            print(f"  ⚠️  публикация не удалась: {e}", flush=True)
        return {"id": rid, "title": recipe.get("title"), "live": live}

    def _publish_docs(self, message: str) -> None:
        sys.path.insert(0, str(ROOT.parent / "scripts"))
        import publish  # noqa: PLC0415
        publish.build()
        publish.publish(message)

    def _recipe_by_id(self, rid: str) -> dict | None:
        return find_recipe(rid)

    def _who(self) -> dict | None:
        """Кто стучится: проверяем подпись initData от Telegram и белый список."""
        user = verify_init_data(self.headers.get("X-TG-Init-Data") or "")
        if not user:
            return None
        ids, private = allowed_users()
        if private and int(user.get("id", 0)) not in ids:
            return None
        return user

    def do_POST(self):
        route = self.path.split("?")[0]
        allowed = ("/api/recipe", "/api/text", "/api/page", "/api/edit", "/api/delete", "/api/save")
        if route not in allowed:
            return self._json({"error": "not found"}, 404)

        # тело читаем ДО любых отказов: иначе остаток висит в соединении
        # (туннель переиспользует keep-alive, и следующий запрос ломается)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"error": "плохой JSON"}, 400)

        who = self._who()
        needs_auth = route in ("/api/save", "/api/delete", "/api/edit")
        if needs_auth and not who:
            return self._json({"error": "нет доступа: открой экран из Телеграма"}, 403)

        try:
            if route == "/api/recipe":
                url = (payload.get("url") or "").strip()
                if not url.startswith("http"):
                    return self._json({"error": "нужна ссылка http(s)"}, 400)
                with _lock:                     # whisper не любит параллельных прогонов
                    recipe = build(url)
                return self._json(self._save(recipe))

            if route == "/api/page":                 # обычный сайт с рецептом
                url = (payload.get("url") or "").strip()
                if not url.startswith("http"):
                    return self._json({"error": "нужна ссылка http(s)"}, 400)
                with _lock:
                    recipe = from_page(url)
                return self._json(self._save(recipe))

            if route == "/api/text":                 # текст рецепта сообщением
                text = (payload.get("text") or "").strip()
                with _lock:
                    recipe = from_source_text(
                        text, title=(payload.get("title") or "").strip(),
                        origin="текст от пользователя",
                    )
                return self._json(self._save(recipe))

            if route == "/api/edit":                 # правка готового рецепта
                rid = (payload.get("id") or "").strip()
                instruction = (payload.get("instruction") or "").strip()
                if not rid or len(instruction) < 3:
                    return self._json({"error": "нужен id и текст правки"}, 400)
                recipe = self._recipe_by_id(rid)
                if not recipe:
                    return self._json({"error": "рецепт не найден"}, 404)
                with _lock:
                    updated = edit_recipe(recipe, instruction)
                return self._json(self._save(updated, rid=rid, wait=False))

            if route == "/api/save":                 # правка прямо из мини-аппа
                rid = (payload.get("id") or "").strip()
                rec = payload.get("recipe") or {}
                existing = self._recipe_by_id(rid) if rid else None
                if not existing:
                    return self._json({"error": "рецепт не найден"}, 404)
                if not isinstance(rec.get("ingredients"), list) or not isinstance(rec.get("steps"), list):
                    return self._json({"error": "в рецепте нужны ингредиенты и шаги"}, 400)

                merged = {**existing, **rec}
                for i, s in enumerate(merged["steps"], 1):
                    s["n"] = i
                merged["edited"] = True
                merged["edited_by"] = who.get("id") if who else None
                return self._json(self._save(merged, rid=rid, wait=False))

            if route == "/api/delete":
                rid = (payload.get("id") or "").strip()
                path_file = STORE / f"{rid}.json"
                if not rid or not path_file.exists():
                    return self._json({"error": "рецепт не найден"}, 404)
                title = json.loads(path_file.read_text(encoding="utf-8")).get("title", rid)
                path_file.unlink()
                self._publish_docs(f"delete: {title}")
                return self._json({"deleted": rid, "title": title})

        except Exception as e:                      # показываем причину, а не 500 без текста
            traceback.print_exc()
            return self._json({"error": str(e)[:500]}, 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8891)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"мини-апп: http://{args.host}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен", flush=True)


if __name__ == "__main__":
    main()
