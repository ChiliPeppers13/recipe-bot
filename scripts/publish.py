"""Сборка статичной копии сайта из базы: docs/ → GitHub Pages.

Рецепты хранятся в SQLite (data/recipes.db), а на сайт выгружается снимок —
поэтому читать книгу можно и с выключенным маком. Правки, план и покупки
идут через API на маке.

    python scripts/publish.py            # собрать + закоммитить + запушить
    python scripts/publish.py --no-push  # только собрать
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))

import db  # noqa: E402

WEBAPP = ROOT / "app" / "webapp" / "app.html"
DOCS = ROOT / "docs"
RECIPES = DOCS / "recipes"


def build() -> int:
    """Пересобрать docs/. Возвращает число рецептов."""
    db.init()
    rows = db.export_rows()

    DOCS.mkdir(exist_ok=True)
    RECIPES.mkdir(parents=True, exist_ok=True)

    html = WEBAPP.read_text(encoding="utf-8")
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    (DOCS / "404.html").write_text(html, encoding="utf-8")      # прямые ссылки /r/<id>

    for old in RECIPES.glob("*.json"):
        old.unlink()

    index = []
    for r in rows:
        public = dict(r)
        public.pop("transcript", None)          # расшифровка остаётся только в базе
        (RECIPES / f"{r['id']}.json").write_text(
            json.dumps(public, ensure_ascii=False), encoding="utf-8"
        )
        index.append({
            "id": r["id"],
            "title": r["title"],
            "emoji": r.get("emoji") or "🍽",
            "category": r.get("category") or "",
            "time_min": r.get("time_min"),
            "steps": len(r.get("steps") or []),
            "favorite": bool(r.get("favorite")),
            "image": r.get("image") or "",
            "platform": (r.get("source") or {}).get("platform", ""),
            "added": int(r.get("created") or 0),
        })

    (RECIPES / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    if index:
        (RECIPES / "latest.json").write_text(
            json.dumps(index[0], ensure_ascii=False), encoding="utf-8"
        )

    # адрес API: если в окружении пусто — сохраняем тот, что уже был (ручной запуск
    # publish.py не должен обнулять рабочую ссылку на туннель)
    api_file = DOCS / "api.json"
    api_base = os.getenv("API_BASE", "").rstrip("/")
    if not api_base and api_file.exists():
        try:
            api_base = (json.loads(api_file.read_text(encoding="utf-8")).get("api") or "").rstrip("/")
        except Exception:
            api_base = ""
    api_file.write_text(json.dumps({"api": api_base}, ensure_ascii=False), encoding="utf-8")
    return len(index)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def publish(message: str) -> bool:
    git("add", "docs")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return False
    git("-c", "user.email=local", "-c", "user.name=local", "commit", "-m", message)
    if not git("remote").stdout.strip():
        print("⚠️  нет git remote — коммит локальный")
        return False
    push = git("push", "origin", "HEAD")
    if push.returncode != 0:
        print("❌ пуш не прошёл:", (push.stderr or push.stdout)[-300:])
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("-m", "--message", default="publish site")
    args = ap.parse_args()

    n = build()
    print(f"собрано: docs/ · рецептов: {n}")
    if not args.no_push:
        print("✅ запушено" if publish(args.message) else "без изменений")


if __name__ == "__main__":
    main()
