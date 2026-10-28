"""Публикация мини-аппа и рецептов на GitHub Pages.

    python scripts/publish.py            # собрать docs/ + закоммитить + запушить

Что делает:
1. копирует app/webapp/index.html  → docs/index.html и docs/404.html
   (404.html нужен, чтобы прямые ссылки /r/<id> открывались — GitHub Pages
   не умеет SPA-роутинг, поэтому неизвестный путь отдаёт 404.html, а он у нас тот же экран)
2. перекладывает все рецепты из локального хранилища в docs/recipes/<id>.json
3. обновляет docs/recipes/latest.json
4. коммитит и пушит, если есть remote

Хранилище: ~/.hermes/cache/recipe-bot/store/
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
WEBAPP = ROOT / "app" / "webapp" / "index.html"
DOCS = ROOT / "docs"
RECIPES = DOCS / "recipes"
STORE = Path.home() / ".hermes" / "cache" / "recipe-bot" / "store"


def build() -> int:
    """Пересобрать статичный сайт. Возвращает число рецептов."""
    RECIPES.mkdir(parents=True, exist_ok=True)

    html = WEBAPP.read_text(encoding="utf-8")
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    (DOCS / "404.html").write_text(html, encoding="utf-8")   # прямые ссылки /r/<id>

    # чистим старые файлы рецептов, чтобы не копить мусор
    for old in RECIPES.glob("*.json"):
        old.unlink()

    files = sorted(STORE.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for src in files:
        shutil.copy2(src, RECIPES / src.name)

    if files:
        shutil.copy2(files[-1], RECIPES / "latest.json")

    return len(files)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def publish(message: str) -> bool:
    git("add", "docs")
    staged = git("diff", "--cached", "--quiet")
    if staged.returncode == 0:
        print("нечего публиковать — изменений нет")
        return False

    git("commit", "-m", message, check=False)
    remote = git("remote").stdout.strip()
    if not remote:
        print("⚠️  нет git remote — коммит сделан локально, пуш пропущен")
        return False

    push = git("push", "origin", "HEAD", check=False)
    if push.returncode != 0:
        print("❌ пуш не прошёл:\n" + (push.stderr or push.stdout)[-400:])
        return False

    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    print(f"✅ запушено в origin/{branch}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="только собрать docs/, без git")
    ap.add_argument("-m", "--message", default="recipe: publish site")
    args = ap.parse_args()

    count = build()
    print(f"собрано: docs/ · рецептов: {count}")
    if args.no_push:
        return
    publish(args.message)


if __name__ == "__main__":
    main()
