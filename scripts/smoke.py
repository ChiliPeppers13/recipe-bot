"""Быстрая проверка без Телеграма: сервер отдаёт рецепт, бот импортируется."""
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
API = "http://127.0.0.1:8787"


def get(path: str):
    with urllib.request.urlopen(API + path, timeout=10) as r:
        return json.loads(r.read())


def main():
    import aiogram
    print("aiogram:", aiogram.__version__)

    latest = get("/api/latest")
    print("последний рецепт:", latest)
    rid = latest["id"]

    rec = get(f"/r/{rid}/recipe.json")
    print("полей в рецепте:", sorted(rec.keys()))

    import bot  # noqa: E402  — проверяем, что модуль собирается
    text = asyncio.run(bot._text_recipe(rid))
    print("\n--- текстовая версия для Телеграма ---")
    print(text[:900])
    print(f"...(всего {len(text)} символов)")


if __name__ == "__main__":
    main()
