// API мини-аппа на Cloudflare Workers + D1 (работает без мака)
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, X-TG-Init-Data, X-Sync-Key",
  "Access-Control-Max-Age": "86400",
};
const out = (data, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json; charset=utf-8", ...CORS } });
const enc = new TextEncoder();
const hex = (b) => [...b].map((x) => x.toString(16).padStart(2, "0")).join("");

async function hmac(key, msg) {
  const k = await crypto.subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", k, msg));
}

async function whoami(initData, token) {
  if (!initData || !token) return null;
  const p = new URLSearchParams(initData);
  const hash = p.get("hash");
  if (!hash) return null;
  p.delete("hash");
  const dcs = [...p.entries()].sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)).map(([k, v]) => k + "=" + v).join("\n");
  const secret = await hmac(enc.encode("WebAppData"), enc.encode(token));
  if (hex(await hmac(secret, enc.encode(dcs))) !== hash) return null;
  const ts = Number(p.get("auth_date") || 0);
  if (!ts || Date.now() / 1000 - ts > 172800) return null;
  try { return JSON.parse(p.get("user") || "null"); } catch { return null; }
}

const JF = ["tags", "ingredients", "steps", "tips", "missing", "source"];
const DS = { lang: "ru", theme: "dark", default_servings: "2", max_time: "", exclude: "[]", allergens: "[]", preferred_categories: "[]", ai_suggestions: "0" };

function row(r) {
  if (!r) return null;
  const o = { ...r };
  for (const f of JF) { try { o[f] = JSON.parse(r[f] ?? (f === "source" ? "{}" : "[]")); } catch { o[f] = f === "source" ? {} : []; } }
  o.favorite = !!r.favorite;
  return o;
}

const list = (v) => { try { const a = JSON.parse(v ?? "[]"); return Array.isArray(a) ? a.map((x) => String(x).trim().toLowerCase()).filter(Boolean) : []; } catch { return []; } };

async function settings(db) {
  const { results } = await db.prepare("SELECT key, value FROM settings").all();
  const o = { ...DS };
  for (const r of results) o[r.key] = r.value;
  return o;
}

async function banned(db) { const s = await settings(db); return [...list(s.exclude), ...list(s.allergens)]; }

const hit = (rec, ban) => ban.length > 0 && (rec.ingredients || []).some((i) => ban.some((b) => (i.name || "").toLowerCase().includes(b)));

async function all(db) {
  const { results } = await db.prepare("SELECT * FROM recipes ORDER BY created DESC").all();
  return results.map(row);
}

const CATS = {
  "Овощи и фрукты": ["помидор", "лук", "чеснок", "морков", "картоф", "перец", "огурец", "капуст", "кабач", "баклаж", "зелен", "петрушк", "укроп", "базилик", "лимон", "яблок", "банан", "ягод", "авокадо", "шпинат", "салат"],
  "Мясо и рыба": ["мясо", "кури", "говяд", "свинин", "фарш", "бекон", "грудинк", "колбас", "рыб", "лосос", "тунец", "кревет", "индейк", "язык"],
  "Молочное и яйца": ["молок", "сливк", "сыр", "пармезан", "масло сливоч", "творог", "сметан", "йогурт", "кефир", "яйц", "яйко"],
  "Бакалея": ["мук", "сахар", "соль", "рис", "паст", "спагет", "макарон", "гречк", "круп", "овсян", "хлеб", "дрожж", "сода", "разрыхл", "крахмал", "какао"],
  "Специи и соусы": ["перец чёрн", "паприк", "куркум", "кориандр", "орегано", "тимьян", "лавров", "соев", "соус", "кетчуп", "майонез", "горчиц", "уксус", "мёд"],
};
const cat = (n) => { const l = (n || "").toLowerCase(); for (const [c, ws] of Object.entries(CATS)) if (ws.some((w) => l.includes(w))) return c; return "Прочее"; };
const qty = (t) => { const m = String(t ?? "").trim().toLowerCase().replace(",", ".").match(/^([\d.]+)\s*(.*)$/); const n = m ? Number(m[1]) : NaN; return Number.isFinite(n) ? [n, m[2].trim()] : [null, ""]; };

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    const u = new URL(req.url);
    const path = u.pathname.replace(/\/+$/, "") || "/";
    const g = (k, d = "") => u.searchParams.get(k) ?? d;
    const db = env.DB;
    try {
      if (req.method === "GET") {
        if (path === "/api/health") return out({ ok: true, store: "d1" });

        if (path === "/api/recipes") {
          const ban = await banned(db);
          let rs = (await all(db)).filter((r) => !hit(r, ban));
          if (g("favorite") === "1") rs = rs.filter((r) => r.favorite);
          const c = g("category"); if (c) rs = rs.filter((r) => r.category === c);
          const mt = Number(g("max_time", "0")) || 0; if (mt) rs = rs.filter((r) => r.time_min && r.time_min <= mt);
          const t = g("q").trim().toLowerCase();
          if (t) rs = rs.filter((r) => (r.title || "").toLowerCase().includes(t) || (r.subtitle || "").toLowerCase().includes(t) || (r.ingredients || []).some((i) => (i.name || "").toLowerCase().includes(t)));
          const ing = g("ingredient").trim().toLowerCase();
          if (ing) rs = rs.filter((r) => (r.ingredients || []).some((i) => (i.name || "").toLowerCase().includes(ing)));
          return out(rs.map((r) => ({
            id: r.id, title: r.title, subtitle: r.subtitle, emoji: r.emoji, category: r.category,
            time_min: r.time_min, servings: r.servings, steps: (r.steps || []).length,
            favorite: r.favorite, image: r.image || "", ingredients: (r.ingredients || []).map((i) => i.name),
            added: Math.floor(r.created || 0),
          })));
        }

        if (path === "/api/recipe") {
          const r = row(await db.prepare("SELECT * FROM recipes WHERE id = ?").bind(g("id")).first());
          if (!r) return out({ error: "не найдено" }, 404);
          delete r.transcript;
          return out(r);
        }

        if (path === "/api/meta") {
          const rs = await all(db);
          const c = {}, tg = {};
          for (const r of rs) { if (r.category) c[r.category] = (c[r.category] || 0) + 1; for (const x of r.tags || []) tg[x] = (tg[x] || 0) + 1; }
          return out({
            categories: Object.entries(c).map(([name, count]) => ({ name, count })).sort((a, b) => b.count - a.count),
            tags: Object.entries(tg).sort((a, b) => b[1] - a[1]).map(([x]) => x).slice(0, 30),
            stats: { recipes: rs.length, favorites: rs.filter((r) => r.favorite).length, db: "D1", size_kb: 0 },
            settings: await settings(db),
          });
        }

        if (path === "/api/settings") return out(await settings(db));

        if (path === "/api/plan") {
          const today = new Date();
          const mon = new Date(today); mon.setDate(today.getDate() - ((today.getDay() + 6) % 7));
          const iso = (d) => d.toISOString().slice(0, 10);
          const start = g("start", iso(mon));
          const e = new Date(start); e.setDate(e.getDate() + 6);
          const end = g("end", iso(e));
          const { results } = await db.prepare(
            "SELECT p.date, p.meal, p.recipe_id, r.title, r.emoji, r.time_min, r.image FROM plan p LEFT JOIN recipes r ON r.id = p.recipe_id WHERE p.date BETWEEN ? AND ? ORDER BY p.date"
          ).bind(start, end).all();
          return out({ start, end, items: results });
        }

        if (path === "/api/shopping") return out((await db.prepare("SELECT * FROM shopping ORDER BY cat, id").all()).results);

        if (path === "/api/pantry") return out((await db.prepare("SELECT * FROM pantry ORDER BY have DESC, cat, name").all()).results);

        if (path === "/api/suggest") {
          let have = g("have").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
          if (!have.length) have = (await db.prepare("SELECT name FROM pantry WHERE have = 1").all()).results.map((r) => r.name.toLowerCase());
          if (!have.length) return out([]);
          const ban = await banned(db);
          const res = [];
          for (const r of await all(db)) {
            if (hit(r, ban)) continue;
            const ings = r.ingredients || [];
            if (!ings.length) continue;
            const m = [], miss = [];
            for (const i of ings) {
              const n = (i.name || "").toLowerCase();
              if (have.some((h) => n.includes(h) || h.includes(n))) m.push(i.name); else miss.push(i.name);
            }
            if (!m.length) continue;
            res.push({ id: r.id, title: r.title, emoji: r.emoji, category: r.category, image: r.image || "", matched: m, missing: miss, have: m.length, total: ings.length });
          }
          res.sort((a, b) => b.have / b.total - a.have / a.total);
          return out(res.slice(0, 20));
        }

        if (path === "/api/latest") {
          const r = await db.prepare("SELECT id FROM recipes ORDER BY created DESC LIMIT 1").first();
          return out({ id: r ? r.id : null });
        }
        return out({ error: "нет такого маршрута" }, 404);
      }

      if (req.method !== "POST") return out({ error: "метод не поддержан" }, 405);
      const body = await req.json().catch(() => ({}));

      if (path === "/api/import" || path === "/api/page" || path === "/api/text")
        return out({ error: "импорт работает только когда включён мак" }, 503);

      if (path === "/api/sync" || path === "/api/seed") {
        if (!env.SYNC_KEY || req.headers.get("X-Sync-Key") !== env.SYNC_KEY) return out({ error: "нет доступа" }, 403);

        if (path === "/api/seed") {
          const text = await (await fetch(body.url)).text();
          const stmts = text.split(";\n").map((s) => s.trim()).filter((s) => s && !s.startsWith("--"));
          let ok = 0;
          const bad = [];
          for (let i = 0; i < stmts.length; i += 40) {
            const chunk = stmts.slice(i, i + 40);
            try { await db.batch(chunk.map((s) => db.prepare(s))); ok += chunk.length; }
            catch (e) { bad.push(String(e.message || e).slice(0, 120)); }
          }
          const n = (await db.prepare("SELECT COUNT(*) c FROM recipes").first()).c;
          return out({ statements: stmts.length, applied: ok, errors: bad, recipes: n });
        }

        if (body.delete) { await db.prepare("DELETE FROM recipes WHERE id = ?").bind(body.delete).run(); return out({ ok: true }); }
        const r = body.recipe || {};
        const id = r.id || crypto.randomUUID().replace(/-/g, "").slice(0, 10);
        const now = Date.now() / 1000;
        const cur = await db.prepare("SELECT created FROM recipes WHERE id = ?").bind(id).first();
        await db.prepare(
          "INSERT INTO recipes (id,title,subtitle,emoji,category,tags,time_min,servings,difficulty,ingredients,steps,tips,missing,source_url,source,transcript,image,favorite,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) " +
          "ON CONFLICT(id) DO UPDATE SET title=excluded.title, subtitle=excluded.subtitle, emoji=excluded.emoji, category=excluded.category, tags=excluded.tags, time_min=excluded.time_min, servings=excluded.servings, difficulty=excluded.difficulty, ingredients=excluded.ingredients, steps=excluded.steps, tips=excluded.tips, missing=excluded.missing, source_url=excluded.source_url, source=excluded.source, image=excluded.image, updated=excluded.updated"
        ).bind(id, r.title || "Без названия", r.subtitle || "", r.emoji || "🍽", r.category || "", JSON.stringify(r.tags || []),
          r.time_min || null, r.servings || null, r.difficulty || "", JSON.stringify(r.ingredients || []), JSON.stringify(r.steps || []),
          JSON.stringify(r.tips || []), JSON.stringify(r.missing || []), r.source_url || "", JSON.stringify(r.source || {}),
          r.transcript || "", r.image || "", r.favorite ? 1 : 0, cur ? cur.created : now, now).run();
        return out({ ok: true, id });
      }

      const user = await whoami(req.headers.get("X-TG-Init-Data") || "", env.BOT_TOKEN);
      const allow = (env.ALLOWED || "").split(",").map((s) => s.trim()).filter(Boolean);
      if (!user || !allow.includes(String(user.id))) return out({ error: "нет доступа" }, 403);

      if (path === "/api/save") {
        const id = String(body.id || "");
        const cur = row(await db.prepare("SELECT * FROM recipes WHERE id = ?").bind(id).first());
        if (!cur) return out({ error: "рецепт не найден" }, 404);
        const r = body.recipe || {};
        if (!Array.isArray(r.ingredients) || !Array.isArray(r.steps)) return out({ error: "нужны ингредиенты и шаги" }, 400);
        const m = { ...cur, ...r };
        await db.prepare("UPDATE recipes SET title=?,subtitle=?,emoji=?,category=?,tags=?,time_min=?,servings=?,difficulty=?,ingredients=?,steps=?,tips=?,image=?,updated=? WHERE id=?")
          .bind(m.title || cur.title, m.subtitle || "", m.emoji || "🍽", m.category || "", JSON.stringify(m.tags || []), m.time_min || null,
            m.servings || null, m.difficulty || "", JSON.stringify(m.ingredients || []), JSON.stringify(m.steps || []),
            JSON.stringify(m.tips || []), m.image || "", Date.now() / 1000, id).run();
        return out({ ok: true, id });
      }

      if (path === "/api/delete") {
        const id = String(body.id || "");
        const r = await db.prepare("SELECT title FROM recipes WHERE id = ?").bind(id).first();
        if (!r) return out({ error: "рецепт не найден" }, 404);
        await db.batch([db.prepare("DELETE FROM recipes WHERE id = ?").bind(id), db.prepare("DELETE FROM plan WHERE recipe_id = ?").bind(id)]);
        return out({ deleted: id, title: r.title });
      }

      if (path === "/api/favorite") {
        const id = String(body.id || "");
        const r = await db.prepare("SELECT favorite FROM recipes WHERE id = ?").bind(id).first();
        if (!r) return out({ favorite: false });
        const next = r.favorite ? 0 : 1;
        await db.prepare("UPDATE recipes SET favorite = ? WHERE id = ?").bind(next, id).run();
        return out({ favorite: !!next });
      }

      if (path === "/api/plan") {
        const { action, date, meal, recipe_id, new_date, new_meal } = body;
        if (action === "set") await db.prepare("INSERT OR REPLACE INTO plan(date,meal,recipe_id) VALUES (?,?,?)").bind(date, meal, recipe_id).run();
        else if (action === "clear") await db.prepare("DELETE FROM plan WHERE date = ? AND meal = ?").bind(date, meal).run();
        else if (action === "move") {
          const r = await db.prepare("SELECT recipe_id FROM plan WHERE date=? AND meal=?").bind(date, meal).first();
          if (r) {
            await db.prepare("DELETE FROM plan WHERE date = ? AND meal = ?").bind(date, meal).run();
            await db.prepare("INSERT OR REPLACE INTO plan(date,meal,recipe_id) VALUES (?,?,?)").bind(new_date, new_meal, r.recipe_id).run();
          }
        } else return out({ error: "неизвестное действие" }, 400);
        return out({ ok: true });
      }

      if (path === "/api/shopping") {
        const a = body.action;
        if (a === "build") {
          const merged = new Map();
          for (const rid of body.recipes || []) {
            const r = row(await db.prepare("SELECT * FROM recipes WHERE id = ?").bind(rid).first());
            if (!r) continue;
            for (const i of r.ingredients || []) {
              const name = String(i.name || "").trim();
              if (!name) continue;
              const [n, unit] = qty(i.qty || i.est || "");
              const k = name.toLowerCase() + "|" + unit;
              const cur = merged.get(k);
              if (cur) { if (n !== null && cur.qty !== null) cur.qty += n; }
              else merged.set(k, { name, qty: n, unit, cat: cat(name) });
            }
          }
          if (body.replace) await db.prepare("DELETE FROM shopping WHERE done = 0").run();
          const items = [...merged.values()];
          if (items.length) {
            await db.batch(items.map((i) => db.prepare("INSERT INTO shopping(name,qty,unit,cat,done,created) VALUES (?,?,?,?,0,?)").bind(i.name, i.qty, i.unit, i.cat, Date.now() / 1000)));
          }
          return out({ added: items.length, items: (await db.prepare("SELECT * FROM shopping ORDER BY cat, id").all()).results });
        }
        if (a === "add") {
          const name = String(body.name || "").trim();
          if (!name) return out({ error: "нужно название" }, 400);
          await db.prepare("INSERT INTO shopping(name,qty,unit,cat,done,created) VALUES (?,?,?,?,0,?)").bind(name, body.qty ?? null, body.unit || "", cat(name), Date.now() / 1000).run();
        } else if (a === "toggle") await db.prepare("UPDATE shopping SET done = 1 - done WHERE id = ?").bind(body.id).run();
        else if (a === "delete") await db.prepare("DELETE FROM shopping WHERE id = ?").bind(body.id).run();
        else if (a === "clear") await db.prepare(body.done_only ? "DELETE FROM shopping WHERE done = 1" : "DELETE FROM shopping").run();
        else return out({ error: "неизвестное действие" }, 400);
        return out({ ok: true });
      }

      if (path === "/api/pantry") {
        const a = body.action;
        if (a === "add") {
          const name = String(body.name || "").trim();
          if (!name) return out({ error: "нужно название" }, 400);
          await db.prepare("INSERT OR IGNORE INTO pantry(name,have,cat,created) VALUES (?,1,?,?)").bind(name, cat(name), Date.now() / 1000).run();
        } else if (a === "toggle") await db.prepare("UPDATE pantry SET have = 1 - have WHERE id = ?").bind(body.id).run();
        else if (a === "delete") await db.prepare("DELETE FROM pantry WHERE id = ?").bind(body.id).run();
        else if (a === "clear_have") await db.prepare("UPDATE pantry SET have = 0").run();
        else return out({ error: "неизвестное действие" }, 400);
        return out({ ok: true, items: (await db.prepare("SELECT * FROM pantry ORDER BY have DESC, cat, name").all()).results });
      }

      if (path === "/api/settings") {
        for (const [k, v] of Object.entries(body.patch || {})) {
          if (!(k in DS)) continue;
          await db.prepare("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)").bind(k, String(v)).run();
        }
        return out(await settings(db));
      }

      return out({ error: "нет такого маршрута" }, 404);
    } catch (e) {
      return out({ error: String(e.message || e).slice(0, 300) }, 500);
    }
  },
};
