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
import json
import sys
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
WEBAPP = ROOT / "webapp" / "index.html"
STORE = Path.home() / ".hermes" / "cache" / "recipe-bot" / "store"

sys.path.insert(0, str(ROOT))
from pipeline import build  # noqa: E402

STORE.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()


def latest_id() -> str | None:
    files = sorted(STORE.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem if files else None


def find_recipe(rid: str) -> dict | None:
    path = STORE / f"{rid}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


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
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

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

    def do_POST(self):
        if self.path.split("?")[0] != "/api/recipe":
            return self._json({"error": "not found"}, 404)

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            url = (payload.get("url") or "").strip()
            if not url.startswith("http"):
                return self._json({"error": "нужна ссылка http(s)"}, 400)

            with _lock:                     # whisper не любит параллельных прогонов
                recipe = build(url)

            rid = uuid.uuid4().hex[:10]
            (STORE / f"{rid}.json").write_text(
                json.dumps(recipe, ensure_ascii=False), encoding="utf-8"
            )
            return self._json({"id": rid, "title": recipe.get("title")})

        except Exception as e:                      # показываем причину, а не 500 без текста
            traceback.print_exc()
            return self._json({"error": str(e)[:500]}, 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
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
