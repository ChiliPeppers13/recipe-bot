"""Постоянная база: SQLite в data/recipes.db.

Почему не JSON-файлы: кэш чистится, файлы теряют историю, а фильтры и план
требуют нормальных запросов. SQLite бесплатен, один файл, легко бэкапится.

Таблицы:
  recipes  — сами рецепты (JSON-поля для списков, отдельные колонки для поиска)
  plan     — недельный план: дата + приём пищи → рецепт
  shopping — список покупок (одна строка = одна позиция)
  settings — личные настройки ключ-значение
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "recipes.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    subtitle    TEXT DEFAULT '',
    emoji       TEXT DEFAULT '🍽',
    category    TEXT DEFAULT '',
    tags        TEXT DEFAULT '[]',
    time_min    INTEGER,
    servings    INTEGER,
    difficulty  TEXT DEFAULT '',
    ingredients TEXT DEFAULT '[]',
    steps       TEXT DEFAULT '[]',
    tips        TEXT DEFAULT '[]',
    missing     TEXT DEFAULT '[]',
    source_url  TEXT DEFAULT '',
    source      TEXT DEFAULT '{}',
    transcript  TEXT DEFAULT '',
    image       TEXT DEFAULT '',
    favorite    INTEGER DEFAULT 0,
    created     REAL,
    updated     REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS recipes_src ON recipes(source_url) WHERE source_url != '';
CREATE INDEX IF NOT EXISTS recipes_created ON recipes(created DESC);

CREATE TABLE IF NOT EXISTS plan (
    date      TEXT NOT NULL,
    meal      TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    PRIMARY KEY (date, meal)
);

CREATE TABLE IF NOT EXISTS shopping (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    qty      REAL,
    unit     TEXT DEFAULT '',
    cat      TEXT DEFAULT 'прочее',
    done     INTEGER DEFAULT 0,
    created  REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

JSON_FIELDS = ("tags", "ingredients", "steps", "tips", "missing", "source")

DEFAULT_SETTINGS = {
    "lang": "ru",
    "default_servings": "2",
    "max_time": "",              # пусто = без ограничения
    "exclude": "[]",             # исключаемые ингредиенты
    "allergens": "[]",           # аллергены
    "preferred_categories": "[]",
    "ai_suggestions": "0",       # 1 = разрешено генерировать новые рецепты ИИ
}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


# --- рецепты ---------------------------------------------------------------

def _row_to_recipe(row: sqlite3.Row) -> dict:
    r = dict(row)
    for f in JSON_FIELDS:
        try:
            r[f] = json.loads(r.get(f) or ("{}" if f == "source" else "[]"))
        except Exception:
            r[f] = {} if f == "source" else []
    r["favorite"] = bool(r.get("favorite"))
    return r


def normalize_url(url: str) -> str:
    """Канонический ключ источника: youtube:ID, instagram:CODE, tiktok:ID, иначе host+path.

    Нельзя просто отрезать query: у YouTube идентификатор видео живёт именно там
    (?v=ID), и все ролики склеились бы в один «дубликат».
    """
    if not url:
        return ""
    import re
    u = url.strip()

    m = re.search(r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/|embed/))([\w-]{6,})", u)
    if m:
        return f"youtube:{m.group(1)}"

    m = re.search(r"instagram\.com/(?:reel|p|tv)/([\w-]+)", u)
    if m:
        return f"instagram:{m.group(1)}"

    m = re.search(r"tiktok\.com/.*?/video/(\d+)", u)
    if m:
        return f"tiktok:{m.group(1)}"

    m = re.search(r"(?:vm|vt)\.tiktok\.com/([\w]+)", u)
    if m:
        return f"tiktok:{m.group(1)}"

    base = u.split("?")[0].split("#")[0].rstrip("/")
    base = re.sub(r"^https?://(www\.|m\.)?", "", base)
    return base.lower()


def find_duplicate(recipe: dict) -> dict | None:
    """Защита от повторного добавления того же видео/страницы."""
    url = normalize_url((recipe.get("source") or {}).get("url") or "")
    with connect() as con:
        if url:
            row = con.execute("SELECT * FROM recipes WHERE source_url = ?", (url,)).fetchone()
            if row:
                return _row_to_recipe(row)
        title = (recipe.get("title") or "").strip().lower()
        if len(title) > 3:
            row = con.execute(
                "SELECT * FROM recipes WHERE lower(title) = ?", (title,)
            ).fetchone()
            if row:
                return _row_to_recipe(row)
    return None


def save_recipe(recipe: dict, rid: str | None = None) -> str:
    """Вставка или обновление. Возвращает id."""
    rid = rid or recipe.get("id") or uuid.uuid4().hex[:10]
    now = time.time()
    src = recipe.get("source") or {}
    values = {
        "id": rid,
        "title": (recipe.get("title") or "Без названия").strip(),
        "subtitle": recipe.get("subtitle") or "",
        "emoji": recipe.get("emoji") or "🍽",
        "category": recipe.get("category") or "",
        "tags": json.dumps(recipe.get("tags") or [], ensure_ascii=False),
        "time_min": recipe.get("time_min") or None,
        "servings": recipe.get("servings") or None,
        "difficulty": recipe.get("difficulty") or "",
        "ingredients": json.dumps(recipe.get("ingredients") or [], ensure_ascii=False),
        "steps": json.dumps(recipe.get("steps") or [], ensure_ascii=False),
        "tips": json.dumps(recipe.get("tips") or [], ensure_ascii=False),
        "missing": json.dumps(recipe.get("missing") or [], ensure_ascii=False),
        "source_url": normalize_url(src.get("url") or ""),
        "source": json.dumps(src, ensure_ascii=False),
        "transcript": (recipe.get("raw_transcript") or recipe.get("transcript") or "")[:20000],
        "image": recipe.get("image") or src.get("thumbnail") or "",
        "favorite": 1 if recipe.get("favorite") else 0,
        "created": recipe.get("created") or now,
        "updated": now,
    }
    with connect() as con:
        exists = con.execute("SELECT created, favorite FROM recipes WHERE id = ?", (rid,)).fetchone()
        if exists:                       # не теряем дату и избранное при правке
            values["created"] = exists["created"]
            values["favorite"] = max(values["favorite"], exists["favorite"])
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        con.execute(f"INSERT OR REPLACE INTO recipes ({cols}) VALUES ({marks})", tuple(values.values()))
    return rid


def get_recipe(rid: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM recipes WHERE id = ?", (rid,)).fetchone()
    return _row_to_recipe(row) if row else None


def delete_recipe(rid: str) -> str | None:
    with connect() as con:
        row = con.execute("SELECT title FROM recipes WHERE id = ?", (rid,)).fetchone()
        if not row:
            return None
        con.execute("DELETE FROM recipes WHERE id = ?", (rid,))
        con.execute("DELETE FROM plan WHERE recipe_id = ?", (rid,))
    return row["title"]


def toggle_favorite(rid: str) -> bool:
    with connect() as con:
        row = con.execute("SELECT favorite FROM recipes WHERE id = ?", (rid,)).fetchone()
        if not row:
            return False
        new = 0 if row["favorite"] else 1
        con.execute("UPDATE recipes SET favorite = ? WHERE id = ?", (new, rid))
    return bool(new)


def list_recipes(q: str = "", category: str = "", tag: str = "", max_time: int = 0,
                 favorite_only: bool = False, ingredient: str = "") -> list[dict]:
    """Лёгкий список для каталога + полноценные данные для карточек."""
    sql = "SELECT * FROM recipes WHERE 1=1"
    args: list = []
    if q:
        sql += " AND (lower(title) LIKE ? OR lower(ingredients) LIKE ? OR lower(tags) LIKE ?)"
        like = f"%{q.lower()}%"
        args += [like, like, like]
    if category:
        sql += " AND category = ?"
        args.append(category)
    if tag:
        sql += " AND lower(tags) LIKE ?"
        args.append(f"%{tag.lower()}%")
    if max_time:
        sql += " AND time_min IS NOT NULL AND time_min <= ?"
        args.append(max_time)
    if favorite_only:
        sql += " AND favorite = 1"
    if ingredient:
        sql += " AND lower(ingredients) LIKE ?"
        args.append(f"%{ingredient.lower()}%")
    sql += " ORDER BY created DESC"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [_row_to_recipe(r) for r in rows]


def all_categories() -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT category, COUNT(*) n FROM recipes WHERE category != '' "
            "GROUP BY category ORDER BY n DESC"
        ).fetchall()
    return [{"name": r["category"], "count": r["n"]} for r in rows]


def all_tags() -> list[str]:
    tags: dict[str, int] = {}
    with connect() as con:
        for row in con.execute("SELECT tags FROM recipes"):
            try:
                for t in json.loads(row["tags"] or "[]"):
                    tags[t] = tags.get(t, 0) + 1
            except Exception:
                continue
    return [t for t, _ in sorted(tags.items(), key=lambda kv: -kv[1])]


# --- план питания ----------------------------------------------------------

def get_plan(start: str, end: str) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT p.date, p.meal, p.recipe_id, r.title, r.emoji, r.time_min, r.image "
            "FROM plan p LEFT JOIN recipes r ON r.id = p.recipe_id "
            "WHERE p.date BETWEEN ? AND ? ORDER BY p.date",
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


def set_plan(date: str, meal: str, recipe_id: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO plan(date, meal, recipe_id) VALUES (?, ?, ?)",
            (date, meal, recipe_id),
        )


def clear_plan(date: str, meal: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM plan WHERE date = ? AND meal = ?", (date, meal))


def move_plan(date: str, meal: str, new_date: str, new_meal: str) -> None:
    with connect() as con:
        row = con.execute("SELECT recipe_id FROM plan WHERE date = ? AND meal = ?",
                          (date, meal)).fetchone()
        if not row:
            return
        con.execute("DELETE FROM plan WHERE date = ? AND meal = ?", (date, meal))
        con.execute("INSERT OR REPLACE INTO plan(date, meal, recipe_id) VALUES (?, ?, ?)",
                    (new_date, new_meal, row["recipe_id"]))


# --- список покупок --------------------------------------------------------

UNIT_ALIASES = {
    "г": "г", "гр": "г", "грамм": "г", "граммов": "г", "g": "г",
    "кг": "кг", "kg": "кг",
    "мл": "мл", "ml": "мл",
    "л": "л", "l": "л",
    "шт": "шт", "штук": "шт", "штуки": "шт", "pcs": "шт",
    "ст. л.": "ст. л.", "ст.л.": "ст. л.", "ст л": "ст. л.", "столовая ложка": "ст. л.",
    "ч. л.": "ч. л.", "ч.л.": "ч. л.", "ч л": "ч. л.", "чайная ложка": "ч. л.",
    "стакан": "стакан", "стакана": "стакан",
    "зубчик": "зубчик", "зубчика": "зубчик", "зубчиков": "зубчик",
    "щепотка": "щепотка", "щепотки": "щепотка",
}

CATEGORY_WORDS = {
    "Овощи и фрукты": ("помидор", "лук", "чеснок", "морков", "картоф", "перец", "огурец",
                       "капуст", "кабач", "баклаж", "зелен", "петрушк", "укроп", "базилик",
                       "лимон", "яблок", "банан", "ягод", "авокадо", "шпинат", "салат"),
    "Мясо и рыба": ("мясо", "кури", "говяд", "свинин", "фарш", "бекон", "грудинк", "колбас",
                    "рыб", "лосос", "тунец", "кревет", "индейк", "язык"),
    "Молочное и яйца": ("молок", "сливк", "сыр", "пармезан", "масло сливоч", "творог", "сметан",
                        "йогурт", "кефир", "яйц", "яйко"),
    "Бакалея": ("мук", "сахар", "соль", "рис", "паст", "спагет", "макарон", "гречк", "круп",
                "овсян", "хлеб", "дрожж", "сода", "разрыхл", "крахмал", "какао"),
    "Специи и соусы": ("перец чёрн", "паприк", "куркум", "кориандр", "орегано", "тимьян",
                       "лавров", "соев", "соус", "кетчуп", "майонез", "горчиц", "уксус", "мёд"),
}


def parse_qty(text: str) -> tuple[float | None, str]:
    """«200 г» → (200, 'г'); «по вкусу» → (None, '')."""
    import re
    t = (text or "").strip().lower().replace(",", ".")
    if not t:
        return None, ""
    m = re.match(r"^([\d.]+)\s*(.*)$", t)
    if not m:
        return None, ""
    try:
        num = float(m.group(1))
    except ValueError:
        return None, ""
    unit = m.group(2).strip().strip(".")
    return num, UNIT_ALIASES.get(unit, unit)


def guess_category(name: str) -> str:
    low = (name or "").lower()
    for cat, words in CATEGORY_WORDS.items():
        if any(w in low for w in words):
            return cat
    return "Прочее"


def _merge_same(items: list[dict]) -> list[dict]:
    """Складываем только совпадающие название+единица. Разные единицы не трогаем."""
    merged: dict[tuple[str, str], dict] = {}
    for it in items:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        qty, unit = parse_qty(it.get("qty") or it.get("est") or "")
        key = (name.lower(), unit)
        if key in merged:
            cur = merged[key]
            if qty is not None and cur["qty"] is not None:
                cur["qty"] += qty
            cur["sources"].add(it.get("source_id") or "")
        else:
            merged[key] = {
                "name": name, "qty": qty, "unit": unit,
                "cat": guess_category(name),
                "sources": {it.get("source_id") or ""},
            }
    return list(merged.values())


def build_shopping(recipe_ids: list[str], replace: bool = False) -> int:
    """Собираем покупки из рецептов. Возвращает число добавленных позиций."""
    items: list[dict] = []
    for rid in recipe_ids:
        rec = get_recipe(rid)
        if not rec:
            continue
        for ing in rec.get("ingredients") or []:
            items.append({
                "name": ing.get("name"),
                "qty": ing.get("qty"),
                "est": ing.get("est"),
                "source_id": rid,
            })
    merged = _merge_same(items)

    with connect() as con:
        if replace:
            con.execute("DELETE FROM shopping")
        added = 0
        for m in merged:
            raw = f"{m['qty']:g} {m['unit']}".strip() if m["qty"] is not None else ""
            con.execute(
                "INSERT INTO shopping(name, qty, unit, cat, done, created) VALUES (?, ?, ?, ?, 0, ?)",
                (m["name"], m["qty"], m["unit"], m["cat"], time.time()),
            )
            added += 1
    return added


def get_shopping() -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM shopping ORDER BY cat, id").fetchall()
    return [dict(r) for r in rows]


def shopping_add(name: str, qty: float | None = None, unit: str = "") -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO shopping(name, qty, unit, cat, done, created) VALUES (?, ?, ?, ?, 0, ?)",
            (name.strip(), qty, unit, guess_category(name), time.time()),
        )


def shopping_toggle(sid: int) -> None:
    with connect() as con:
        con.execute("UPDATE shopping SET done = 1 - done WHERE id = ?", (sid,))


def shopping_delete(sid: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM shopping WHERE id = ?", (sid,))


def shopping_clear(done_only: bool = False) -> None:
    with connect() as con:
        con.execute("DELETE FROM shopping WHERE done = 1" if done_only else "DELETE FROM shopping")


# --- настройки -------------------------------------------------------------

def get_settings() -> dict:
    with connect() as con:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULT_SETTINGS)
    for r in rows:
        out[r["key"]] = r["value"]
    return out


def set_settings(patch: dict) -> dict:
    with connect() as con:
        for k, v in patch.items():
            if k in DEFAULT_SETTINGS:
                con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                            (k, str(v)))
    return get_settings()


# --- статистика / экспорт --------------------------------------------------

def stats() -> dict:
    with connect() as con:
        total = con.execute("SELECT COUNT(*) c FROM recipes").fetchone()["c"]
        fav = con.execute("SELECT COUNT(*) c FROM recipes WHERE favorite = 1").fetchone()["c"]
    return {"recipes": total, "favorites": fav, "db": str(DB_PATH),
            "size_kb": round(DB_PATH.stat().st_size / 1024, 1) if DB_PATH.exists() else 0}


def export_rows() -> list[dict]:
    """Все рецепты для публикации статичной копии на GitHub Pages."""
    with connect() as con:
        rows = con.execute("SELECT * FROM recipes ORDER BY created DESC").fetchall()
    return [_row_to_recipe(r) for r in rows]


if __name__ == "__main__":
    init()
    print("база готова:", DB_PATH)
    print(stats())
