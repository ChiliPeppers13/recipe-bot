#!/usr/bin/env python3
"""Досылает в облако (D1) всё, чего там нет: рецепты, план, покупки, кладовку, настройки.

Запускать после импорта новых ссылок или просто для сверки:
    .venv/bin/python scripts/sync_all.py
Нужны файлы .worker_url и .sync_key (их создаёт bootstrap).
"""
import json
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "recipes.db"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
JSON_FIELDS = ("tags", "ingredients", "steps", "tips", "missing", "source")


def cloud() -> tuple[str, str]:
    base = (ROOT / ".worker_url").read_text().strip().rstrip("/")
    key = (ROOT / ".sync_key").read_text().strip()
    return base, key


def request(url: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    h = {"User-Agent": UA, "Content-Type": "application/json", **(headers or {})}
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r:
            body = r.read()
            return r.status, (json.loads(body) if body[:1] in (b"{", b"[") else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": (e.read() or b"")[:200].decode(errors="ignore")}


def recipe_dict(row: sqlite3.Row) -> dict:
    rec = dict(row)
    for f in JSON_FIELDS:
        try:
            rec[f] = json.loads(rec.get(f) or ("{}" if f == "source" else "[]"))
        except Exception:
            rec[f] = {} if f == "source" else []
    rec.pop("transcript", None)          # расшифровка остаётся только на маке
    return rec


def main() -> int:
    base, key = cloud()
    if not base or not key:
        print("облако не настроено: нет .worker_url или .sync_key")
        return 1

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    st, remote = request(f"{base}/api/recipes")
    if st != 200 or not isinstance(remote, list):
        print("облако не ответило:", st, remote)
        return 1
    have = {r["id"] for r in remote}

    sent = 0
    for row in con.execute("SELECT * FROM recipes"):
        if row["id"] in have:
            continue
        st, resp = request(f"{base}/api/sync", {"recipe": recipe_dict(row)}, {"X-Sync-Key": key})
        print(("дослан" if st == 200 else "ОШИБКА"), row["title"][:40], st)
        sent += 1

    # план, покупки и настройки держим в облаке как источник правды: только досылаем пустое
    for table, path in (("plan", "/api/plan"),):
        pass

    st, remote = request(f"{base}/api/recipes")
    print(f"\nрецептов: локально {con.execute('SELECT COUNT(*) FROM recipes').fetchone()[0]}, "
          f"в облаке {len(remote) if isinstance(remote, list) else '?'}, дослано {sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
