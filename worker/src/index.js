// API мини-аппа на Cloudflare Workers + D1: работает без мака.

// ── общие ответы ───────────────────────────────────────────────────────────
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, X-TG-Init-Data, X-Sync-Key",
  "Access-Control-Max-Age": "86400",
};
const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS },
  });

// ── подпись Telegram initData ───────────────────────────────────────────────
const enc = new TextEncoder();

async function hmacRaw(keyBytes, msgBytes) {
  const key = await crypto.subtle.importKey(
    "raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, msgBytes));
}

const toHex = (bytes) => [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");

async function verifyInitData(initData, botToken) {
  if (!initData || !botToken) return null;
  const params = new URLSearchParams(initData);
  const hash = params.get("hash");
  if (!hash) return null;
  params.delete("hash");

  const dcString = [...params.entries()]
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");

  const secret = await hmacRaw(enc.encode("WebAppData"), enc.encode(botToken));
  const sig = toHex(await hmacRaw(secret, enc.encode(dcString)));
  if (sig !== hash) return null;

  const authDate = Number(params.get("auth_date") || 0);
  if (!authDate || Date.now() / 1000 - authDate > 172800) return null;
  try {
    return JSON.parse(params.get("user") || "null");
  } catch {
    return null;
  }
}

// ── рецепты ─────────────────────────────────────────────────────────────────
const JSON_FIELDS = ["tags", "ingredients", "steps", "tips", "missing", "source"];

function parseRow(row) {
  if (!row) return null;
  const out = { ...row };
  for (const f of JSON_FIELDS) {
    try {
      out[f] = JSON.parse(row[f] ?? (f === "source" ? "{}" : "[]"));
    } catch {
      out[f] = f === "source" ? {} : [];
    }
  }
  out.favorite = !!row.favorite;
  return out;
}

const DEFAULT_SETTINGS = {
  lang: "ru", theme: "dark", default_servings: "2", max_time: "",
  exclude: "[]", allergens: "[]", preferred_categories: "[]", ai_suggestions: "0",
};

async function getSettings(db) {
  const { results } = await db.prepare("SELECT key, value FROM settings").all();
  const out = { ...DEFAULT_SETTINGS };
  for (const r of results) out[r.key] = r.value;
  return out;
}

function listOf(value) {
  try {
    const v = JSON.parse(value ?? "[]");
    return Array.isArray(v) ? v.map((x) => String(x).trim().toLowerCase()).filter(Boolean) : [];
  } catch {
    return [];
  }
}

async function bannedTerms(db) {
  const s = await getSettings(db);
  return [...listOf(s.exclude), ...listOf(s.allergens)];
}

const isBanned = (rec, banned) => {
  if (!banned.length) return false;
  const text = (rec.ingredients || []).map((i) => (i.name || "").toLowerCase()).join(" ");
  return banned.some((b) => text.includes(b));
};

async function allRecipes(db) {
  const { results } = await db.prepare("SELECT * FROM recipes ORDER BY created DESC").all();
  return results.map(parseRow);
}

async function getRecipe(db, id) {
  const row = await db.prepare("SELECT * FROM recipes WHERE id = ?").bind(id).first();
  return parseRow(row);
}

const publicRecipe = (rec) => {
  if (!rec) return null;
  const { transcript, ...rest } = rec;
  return rest;
};

// ── категории продуктов для списка покупок ─────────────────────────────────
const CATEGORY_WORDS = {
  "Овощи и фрукты": ["помидор", "лук", "чеснок", "морков", "картоф", "перец", "огурец", "капуст",
    "кабач", "баклаж", "зелен", "петрушк", "укроп", "базилик", "лимон", "яблок", "банан",
    "ягод", "авокадо", "шпинат", "салат"],
  "Мясо и рыба": ["мясо", "кури", "говяд", "свинин", "фарш", "бекон", "грудинк", "колбас", "рыб",
    "лосос", "тунец", "кревет", "индейк", "язык"],
  "Молочное и яйца": ["молок", "сливк", "сыр", "пармезан", "масло сливоч", "творог", "сметан",
    "йогурт", "кефир", "яйц", "яйко"],
  "Бакалея": ["мук", "сахар", "соль", "рис", "паст", "спагет", "макарон", "гречк", "круп", "овсян",
    "хлеб", "дрожж", "сода", "разрыхл", "крахмал", "какао"],
  "Специи и соусы": ["перец чёрн", "паприк", "куркум", "кориандр", "орегано", "тимьян", "лавров",
    "соев", "соус", "кетчуп", "майонез", "горчиц", "уксус", "мёд"],
};

const guessCategory = (name) => {
  const low = (name || "").toLowerCase();
  for (const [cat, words] of Object.entries(CATEGORY_WORDS)) {
    if (words.some((w) => low.includes(w))) return cat;
  }
  return "Прочее";
};

const parseQty = (text) => {
  const t = String(text ?? "").trim().toLowerCase().replace(",", ".");
  const m = t.match(/^([\d.]+)\s*(.*)$/);
  if (!m) return [null, ""];
  const n = Number(m[1]);
  return Number.isFinite(n) ? [n, m[2].trim()] : [null, ""];
};

// ── роутер ──────────────────────────────────────────────────────────────────
export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const q = (k, d = "") => url.searchParams.get(k) ?? d;
    const db = env.DB;

    try {
      // ---------- чтение ----------
      if (request.method === "GET") {
        if (path === "/api/recipes") {
          const banned = await bannedTerms(db);
          const fav = q("favorite") === "1";
          const cat = q("category");
          const maxTime = Number(q("max_time", "0")) || 0;
          const text = q("q").trim().toLowerCase();
          const ingredient = q("ingredient").trim().toLowerCase();

          let rows = (await allRecipes(db)).filter((r) => !isBanned(r, banned));
          if (fav) rows = rows.filter((r) => r.favorite);
          if (cat) rows = rows.filter((r) => r.category === cat);
          if (maxTime) rows = rows.filter((r) => r.time_min && r.time_min <= maxTime);
          if (text) {
            rows = rows.filter((r) =>
              (r.title || "").toLowerCase().includes(text) ||
              (r.subtitle || "").toLowerCase().includes(text) ||
              (r.ingredients || []).some((i) => (i.name || "").toLowerCase().includes(text)));
          }
          if (ingredient) {
            rows = rows.filter((r) =>
              (r.ingredients || []).some((i) => (i.name || "").toLowerCase().includes(ingredient)));
          }

          return json(rows.map((r) => ({
            id: r.id, title: r.title, subtitle: r.subtitle, emoji: r.emoji,
            category: r.category, time_min: r.time_min, servings: r.servings,
            steps: (r.steps || []).length, favorite: r.favorite, image: r.image || "",
            ingredients: (r.ingredients || []).map((i) => i.name),
            added: Math.floor(r.created || 0),
          })));
        }

        if (path === "/api/recipe") {
          const rec = await getRecipe(db, q("id"));
          return rec ? json(publicRecipe(rec)) : json({ error: "не найдено" }, 404);
        }

        if (path === "/api/meta") {
          const rows = await allRecipes(db);
          const cats = {};
          const tags = {};
          for (const r of rows) {
            if (r.category) cats[r.category] = (cats[r.category] || 0) + 1;
            for (const t of r.tags || []) tags[t] = (tags[t] || 0) + 1;
          }
          return json({
            categories: Object.entries(cats).map(([name, count]) => ({ name, count }))
              .sort((a, b) => b.count - a.count),
            tags: Object.entries(tags).sort((a, b) => b[1] - a[1]).map(([t]) => t).slice(0, 30),
            stats: { recipes: rows.length, favorites: rows.filter((r) => r.favorite).length, db: "D1", size_kb: 0 },
            settings: await getSettings(db),
          });
        }

        if (path === "/api/settings") return json(await getSettings(db));

        if (path === "/api/plan") {
          const today = new Date();
          const monday = new Date(today);
          monday.setDate(today.getDate() - ((today.getDay() + 6) % 7));
          const iso = (d) => d.toISOString().slice(0, 10);
          const start = q("start", iso(monday));
          const endDate = new Date(start);
          endDate.setDate(endDate.getDate() + 6);
          const end = q("end", iso(endDate));
          const { results } = await db.prepare(
            `SELECT p.date, p.meal, p.recipe_id, r.title, r.emoji, r.time_min, r.image
             FROM plan p LEFT JOIN recipes r ON r.id = p.recipe_id
             WHERE p.date BETWEEN ? AND ? ORDER BY p.date`
          ).bind(start, end).all();
          return json({ start, end, items: results });
        }

        if (path === "/api/shopping") {
          const { results } = await db.prepare("SELECT * FROM shopping ORDER BY cat, id").all();
          return json(results);
        }

        if (path === "/api/pantry") {
          const { results } = await db.prepare(
            "SELECT * FROM pantry ORDER BY have DESC, cat, name").all();
          return json(results);
        }

        if (path === "/api/suggest") {
          let have = q("have").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
          if (!have.length) {
            const { results } = await db.prepare("SELECT name FROM pantry WHERE have = 1").all();
            have = results.map((r) => r.name.toLowerCase());
          }
          if (!have.length) return json([]);
          const banned = await bannedTerms(db);
          const out = [];
          for (const rec of await allRecipes(db)) {
            if (isBanned(rec, banned)) continue;
            const ings = rec.ingredients || [];
            if (!ings.length) continue;
            const matched = [], missing = [];
            for (const ing of ings) {
              const name = (ing.name || "").toLowerCase();
              if (have.some((h) => name.includes(h) || h.includes(name))) matched.push(ing.name);
              else missing.push(ing.name);
            }
            if (!matched.length) continue;
            out.push({
              id: rec.id, title: rec.title, emoji: rec.emoji, category: rec.category,
              image: rec.image || "", matched, missing,
              have: matched.length, total: ings.length,
            });
          }
          out.sort((a, b) => b.have / b.total - a.have / a.total);
          return json(out.slice(0, 20));
        }

        if (path === "/api/latest") {
          const row = await db.prepare("SELECT id FROM recipes ORDER BY created DESC LIMIT 1").first();
          return json({ id: row ? row.id : null });
        }

        if (path === "/api/health") return json({ ok: true, store: "d1" });
        return json({ error: "нет такого маршрута" }, 404);
      }

      // ---------- запись ----------
      if (request.method !== "POST") return json({ error: "метод не поддержан" }, 405);

      const payload = await request.json().catch(() => ({}));

      // импорт видео/страниц живёт на маке: там yt-dlp и whisper
      if (path === "/api/import" || path === "/api/page" || path === "/api/text") {
        return json({ error: "импорт работает только когда включён мак" }, 503);
      }

      // синхронизация из локального пайплайна (доверенный ключ, не initData)
      if (path === "/api/sync") {
        if (!env.SYNC_KEY || request.headers.get("X-Sync-Key") !== env.SYNC_KEY) {
          return json({ error: "нет доступа" }, 403);
        }
        if (payload.delete) {
          await db.prepare("DELETE FROM recipes WHERE id = ?").bind(payload.delete).run();
          return json({ ok: true, deleted: payload.delete });
        }
        const rec = payload.recipe || {};
        const id = rec.id || crypto.randomUUID().replace(/-/g, "").slice(0, 10);
        const now = Date.now() / 1000;
        const cur = await db.prepare("SELECT created FROM recipes WHERE id = ?").bind(id).first();
        await db.prepare(
          `INSERT INTO recipes (id, title, subtitle, emoji, category, tags, time_min, servings,
             difficulty, ingredients, steps, tips, missing, source_url, source, transcript,
             image, favorite, created, updated)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title, subtitle=excluded.subtitle, emoji=excluded.emoji,
             category=excluded.category, tags=excluded.tags, time_min=excluded.time_min,
             servings=excluded.servings, difficulty=excluded.difficulty,
             ingredients=excluded.ingredients, steps=excluded.steps, tips=excluded.tips,
             missing=excluded.missing, source_url=excluded.source_url, source=excluded.source,
             transcript=excluded.transcript, image=excluded.image, updated=excluded.updated`
        ).bind(
          id, rec.title || "Без названия", rec.subtitle || "", rec.emoji || "🍽",
          rec.category || "", JSON.stringify(rec.tags || []), rec.time_min || null,
          rec.servings || null, rec.difficulty || "", JSON.stringify(rec.ingredients || []),
          JSON.stringify(rec.steps || []), JSON.stringify(rec.tips || []),
          JSON.stringify(rec.missing || []), rec.source_url || "",
          JSON.stringify(rec.source || {}), rec.transcript || "", rec.image || "",
          rec.favorite ? 1 : 0, cur ? cur.created : now, now
        ).run();
        return json({ ok: true, id });
      }

      // всё ниже — только для своих из мини-аппа
      const user = await verifyInitData(request.headers.get("X-TG-Init-Data") || "", env.BOT_TOKEN);
      const allowed = (env.ALLOWED || "").split(",").map((s) => s.trim()).filter(Boolean);
      if (!user || !allowed.includes(String(user.id))) {
        return json({ error: "нет доступа" }, 403);
      }

      if (path === "/api/save") {
        const id = String(payload.id || "");
        const existing = await getRecipe(db, id);
        if (!existing) return json({ error: "рецепт не найден" }, 404);
        const rec = payload.recipe || {};
        if (!Array.isArray(rec.ingredients) || !Array.isArray(rec.steps)) {
          return json({ error: "нужны ингредиенты и шаги" }, 400);
        }
        const merged = { ...existing, ...rec };
        await db.prepare(
          `UPDATE recipes SET title=?, subtitle=?, emoji=?, category=?, tags=?, time_min=?,
             servings=?, difficulty=?, ingredients=?, steps=?, tips=?, image=?, updated=?
           WHERE id=?`
        ).bind(
          merged.title || existing.title, merged.subtitle || "", merged.emoji || "🍽",
          merged.category || "", JSON.stringify(merged.tags || []), merged.time_min || null,
          merged.servings || null, merged.difficulty || "",
          JSON.stringify(merged.ingredients || []), JSON.stringify(merged.steps || []),
          JSON.stringify(merged.tips || []), merged.image || "", Date.now() / 1000, id
        ).run();
        return json({ ok: true, id });
      }

      if (path === "/api/delete") {
        const id = String(payload.id || "");
        const row = await db.prepare("SELECT title FROM recipes WHERE id = ?").bind(id).first();
        if (!row) return json({ error: "рецепт не найден" }, 404);
        await db.prepare("DELETE FROM recipes WHERE id = ?").bind(id).run();
        await db.prepare("DELETE FROM plan WHERE recipe_id = ?").bind(id).run();
        return json({ deleted: id, title: row.title });
      }

      if (path === "/api/favorite") {
        const id = String(payload.id || "");
        const row = await db.prepare("SELECT favorite FROM recipes WHERE id = ?").bind(id).first();
        if (!row) return json({ favorite: false });
        const next = row.favorite ? 0 : 1;
        await db.prepare("UPDATE recipes SET favorite = ? WHERE id = ?").bind(next, id).run();
        return json({ favorite: !!next });
      }

      if (path === "/api/plan") {
        const { action, date, meal, recipe_id, new_date, new_meal } = payload;
        if (action === "set") {
          await db.prepare("INSERT OR REPLACE INTO plan(date, meal, recipe_id) VALUES (?,?,?)")
            .bind(date, meal, recipe_id).run();
        } else if (action === "clear") {
          await db.prepare("DELETE FROM plan WHERE date = ? AND meal = ?").bind(date, meal).run();
        } else if (action === "move") {
          const row = await db.prepare("SELECT recipe_id FROM plan WHERE date=? AND meal=?")
            .bind(date, meal).first();
          if (row) {
            await db.prepare("DELETE FROM plan WHERE date = ? AND meal = ?").bind(date, meal).run();
            await db.prepare("INSERT OR REPLACE INTO plan(date, meal, recipe_id) VALUES (?,?,?)")
              .bind(new_date, new_meal, row.recipe_id).run();
          }
        } else {
          return json({ error: "неизвестное действие" }, 400);
        }
        return json({ ok: true });
      }

      if (path === "/api/shopping") {
        const { action } = payload;
        if (action === "build") {
          const ids = payload.recipes || [];
          const merged = new Map();
          for (const rid of ids) {
            const rec = await getRecipe(db, rid);
            if (!rec) continue;
            for (const ing of rec.ingredients || []) {
              const name = String(ing.name || "").trim();
              if (!name) continue;
              const [qty, unit] = parseQty(ing.qty || ing.est || "");
              const key = `${name.toLowerCase()}|${unit}`;
              const cur = merged.get(key);
              if (cur) {
                if (qty !== null && cur.qty !== null) cur.qty += qty;
              } else {
                merged.set(key, { name, qty, unit, cat: guessCategory(name) });
              }
            }
          }
          if (payload.replace) await db.prepare("DELETE FROM shopping WHERE done = 0").run();
          let added = 0;
          for (const it of merged.values()) {
            await db.prepare(
              "INSERT INTO shopping(name, qty, unit, cat, done, created) VALUES (?,?,?,?,0,?)"
            ).bind(it.name, it.qty, it.unit, it.cat, Date.now() / 1000).run();
            added++;
          }
          const { results } = await db.prepare("SELECT * FROM shopping ORDER BY cat, id").all();
          return json({ added, items: results });
        }
        if (action === "add") {
          const name = String(payload.name || "").trim();
          if (!name) return json({ error: "нужно название" }, 400);
          await db.prepare(
            "INSERT INTO shopping(name, qty, unit, cat, done, created) VALUES (?,?,?,?,0,?)"
          ).bind(name, payload.qty ?? null, payload.unit || "", guessCategory(name), Date.now() / 1000).run();
        } else if (action === "toggle") {
          await db.prepare("UPDATE shopping SET done = 1 - done WHERE id = ?").bind(payload.id).run();
        } else if (action === "delete") {
          await db.prepare("DELETE FROM shopping WHERE id = ?").bind(payload.id).run();
        } else if (action === "clear") {
          if (payload.done_only) await db.prepare("DELETE FROM shopping WHERE done = 1").run();
          else await db.prepare("DELETE FROM shopping").run();
        } else {
          return json({ error: "неизвестное действие" }, 400);
        }
        return json({ ok: true });
      }

      if (path === "/api/pantry") {
        const { action } = payload;
        if (action === "add") {
          const name = String(payload.name || "").trim();
          if (!name) return json({ error: "нужно название" }, 400);
          await db.prepare(
            "INSERT OR IGNORE INTO pantry(name, have, cat, created) VALUES (?,1,?,?)"
          ).bind(name, guessCategory(name), Date.now() / 1000).run();
        } else if (action === "toggle") {
          await db.prepare("UPDATE pantry SET have = 1 - have WHERE id = ?").bind(payload.id).run();
        } else if (action === "delete") {
          await db.prepare("DELETE FROM pantry WHERE id = ?").bind(payload.id).run();
        } else if (action === "clear_have") {
          await db.prepare("UPDATE pantry SET have = 0").run();
        } else {
          return json({ error: "неизвестное действие" }, 400);
        }
        const { results } = await db.prepare("SELECT * FROM pantry ORDER BY have DESC, cat, name").all();
        return json({ ok: true, items: results });
      }

      if (path === "/api/settings") {
        const patch = payload.patch || {};
        for (const [k, v] of Object.entries(patch)) {
          if (!(k in DEFAULT_SETTINGS)) continue;
          await db.prepare("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)")
            .bind(k, String(v)).run();
        }
        return json(await getSettings(db));
      }

      return json({ error: "нет такого маршрута" }, 404);
    } catch (err) {
      return json({ error: String(err.message || err).slice(0, 300) }, 500);
    }
  },
};
