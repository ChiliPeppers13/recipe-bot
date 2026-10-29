"""Резервная копия базы: backups/recipes-ГГГГММДД-ЧЧММ.db (оставляем 14 последних).

    .venv/bin/python scripts/backup.py

Восстановление: остановить бота, положить файл на место data/recipes.db, запустить снова.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))

import db  # noqa: E402

BACKUPS = ROOT / "backups"
KEEP = 14


def main() -> None:
    if not db.DB_PATH.exists():
        raise SystemExit(f"база не найдена: {db.DB_PATH}")
    BACKUPS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    target = BACKUPS / f"recipes-{stamp}.db"

    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)          # корректная копия даже при активной записи
    src.close()
    dst.close()

    old = sorted(BACKUPS.glob("recipes-*.db"))
    for f in old[:-KEEP]:
        f.unlink()

    print(f"✅ копия: {target} ({target.stat().st_size // 1024} КБ)")
    print(f"всего копий: {len(list(BACKUPS.glob('recipes-*.db')))}")


if __name__ == "__main__":
    main()
