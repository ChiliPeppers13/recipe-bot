"""Одноразовая миграция: JSON-файлы из кэша → SQLite.

Запуск: .venv/bin/python scripts/migrate_to_db.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))

import db  # noqa: E402

STORE = Path.home() / ".hermes" / "cache" / "recipe-bot" / "store"

CATEGORY_RULES = [
    ("Суп", ("суп", "борщ", "бульон", "похлёб", "солянк", "гаспачо")),
    ("Салат", ("салат", "винегрет", "цезарь")),
    ("Завтрак", ("завтрак", "омлет", "блин", "сырник", "каша", "тост", "яичниц", "оладь")),
    ("Десерт", ("десерт", "торт", "пирог", "печень", "кекс", "мусс", "тирамису", "шоколад", "морожен")),
    ("Закуска", ("закуск", "спрэд", "паштет", "хумус", "соус", "брускетт")),
    ("Напиток", ("напит", "смузи", "компот", "лимонад", "коктейл", "кофе", "чай")),
    ("Основное", ("паста", "карбонар", "котлет", "плов", "жаркое", "рагу", "запекан",
                  "кури", "мясо", "рыб", "ролл", "пицц", "лапш", "рис")),
]


def guess_category(title: str, ingredients: list[dict]) -> str:
    text = (title + " " + " ".join(i.get("name", "") for i in ingredients)).lower()
    for cat, words in CATEGORY_RULES:
        if any(w in text for w in words):
            return cat
    return "Основное"


def main() -> None:
    db.init()
    files = sorted(STORE.glob("*.json"))
    if not files:
        print("нечего мигрировать: файлов нет")
        return

    added = skipped = 0
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ⚠️  {f.name}: {e}")
            continue

        rec["id"] = f.stem
        rec["category"] = rec.get("category") or guess_category(
            rec.get("title", ""), rec.get("ingredients") or []
        )
        if not rec.get("tags"):
            tags = []
            t = (rec.get("title") or "").lower()
            for word in ("паста", "суп", "соус", "закуска", "десерт"):
                if word in t:
                    tags.append(word)
            rec["tags"] = tags

        dup = db.find_duplicate(rec)
        if dup and dup["id"] != rec["id"]:
            print(f"  ⏭  дубликат: {rec.get('title')} ≈ {dup['title']}")
            skipped += 1
            continue

        db.save_recipe(rec, rid=f.stem)
        added += 1
        print(f"  + {rec.get('category'):10} {rec.get('title')}")

    print(f"\nперенесено: {added} · пропущено дубликатов: {skipped}")
    print("в базе:", db.stats())


if __name__ == "__main__":
    main()
