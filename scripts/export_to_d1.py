#!/usr/bin/env python3
"""Выгружает локальную SQLite в worker/seed.sql — чтобы залить базу в D1 один в один."""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "recipes.db"
OUT = ROOT / "worker" / "seed.sql"


def q(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def lean(row: dict) -> dict:
    """В облако не тащим расшифровку и длинное описание: они нужны только локально."""
    row = dict(row)
    row["transcript"] = ""
    try:
        src = json.loads(row.get("source") or "{}")
        src.pop("description", None)
        row["source"] = json.dumps(src, ensure_ascii=False)
    except Exception:
        pass
    return row


def main() -> int:
    if not DB.exists():
        print("нет базы:", DB)
        return 1

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    # D1 не принимает BEGIN/COMMIT и PRAGMA в файле — только отдельные запросы
    lines = ["-- сгенерировано scripts/export_to_d1.py"]

    for table in ("recipes", "plan", "shopping", "pantry", "settings"):
        rows = con.execute(f"SELECT * FROM {table}").fetchall()
        for row in rows:
            if table == "recipes":
                row = lean(dict(row))
            cols = ", ".join(row.keys())
            vals = ", ".join(q(row[c]) for c in row.keys())
            lines.append(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({vals});")
        print(f"{table}: {len(rows)}")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("готово:", OUT, f"({OUT.stat().st_size / 1024:.1f} КБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
