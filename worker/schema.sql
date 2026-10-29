-- схема совпадает с локальной SQLite (app/db.py), чтобы данные переносились один в один
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
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    qty     REAL,
    unit    TEXT DEFAULT '',
    cat     TEXT DEFAULT 'прочее',
    done    INTEGER DEFAULT 0,
    created REAL
);

CREATE TABLE IF NOT EXISTS pantry (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    have    INTEGER DEFAULT 0,
    cat     TEXT DEFAULT 'прочее',
    created REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

INSERT OR IGNORE INTO settings(key, value) VALUES
  ('lang', 'ru'), ('theme', 'dark'), ('default_servings', '2'), ('max_time', ''),
  ('exclude', '[]'), ('allergens', '[]'), ('preferred_categories', '[]'), ('ai_suggestions', '0');
