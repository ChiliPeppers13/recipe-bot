"""Телеграм-бот: присылаешь ссылку → получаешь рецепт в мини-аппе.

Запуск:
    export TELEGRAM_BOT_TOKEN=...        # токен от @BotFather
    export WEBAPP_URL=https://...        # публичный https мини-аппа (туннель)
    .venv/bin/python app/bot.py

Если WEBAPP_URL не задан — бот пришлёт рецепт обычным текстом
(мини-апп в Телеграме требует именно https, локальный http не откроется).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    TelegramObject,
    WebAppInfo,
)

API = os.getenv("RECIPE_API", "http://127.0.0.1:8891")     # локальный сервер пайплайна
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")

LINK_RE = re.compile(
    r"https?://(?:www\.)?"
    r"(?:instagram\.com/(?:reel|p|tv)/[\w-]+|"
    r"tiktok\.com/@[\w.\-]+/video/\d+|"
    r"vm\.tiktok\.com/[\w]+|"
    r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w\-]+)"
    r"[^\s]*"
)

dp = Dispatcher()

# чего ждём от пользователя: {chat_id: {"action": "edit", "rid": "…"}}
PENDING: dict[int, dict] = {}
BOT_USERNAME = os.getenv("BOT_USERNAME", "gabago0l_bot")

# --- доступ: по умолчанию бот открыт; после /private — только белый список ---
ACCESS_FILE = Path.home() / ".hermes" / "cache" / "recipe-bot" / "allowed.json"
ADMIN_ID = int(os.getenv("ADMIN_ID", "8919148247"))


def load_access() -> tuple[set[int], bool]:
    """(список допущенных, включён ли приватный режим)."""
    if ACCESS_FILE.exists():
        try:
            d = json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
            return set(int(x) for x in d.get("allowed", [])), bool(d.get("private", True))
        except Exception as e:                      # битый файл не должен молча открывать бота
            print(f"⚠️  не прочитал {ACCESS_FILE}: {e}", flush=True)
            return {ADMIN_ID}, True
    return {ADMIN_ID}, False


def save_access(ids: set[int], private: bool) -> None:
    ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCESS_FILE.write_text(
        json.dumps({"allowed": sorted(ids), "private": private}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


class AccessGate(BaseMiddleware):
    """Не пускает посторонних, если включён приватный режим."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        allowed, private = load_access()
        if not private or user is None or user.id in allowed or user.id == ADMIN_ID:
            return await handler(event, data)

        # /id пускаем всегда: иначе владелец не узнает свой id, если ошибся в настройках
        if isinstance(event, Message) and (event.text or "").startswith("/id"):
            return await handler(event, data)

        # человеку полезно узнать свой id — владелец сможет его добавить
        text = f"🔒 Бот приватный.\n\nТвой id: <code>{user.id}</code> — пришли его владельцу."
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer("🔒 Бот приватный", show_alert=True)
        return None


def load_env() -> None:
    """Локальный .env проекта главнее: иначе бот подхватит чужой TELEGRAM_BOT_TOKEN из окружения."""
    project = Path(__file__).parent.parent / ".env"
    if project.exists():
        for line in project.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if v:
                    os.environ[k.strip()] = v          # жёстко перекрываем

    shared = Path.home() / ".hermes" / ".env"
    if shared.exists():
        for line in shared.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dp.message(CommandStart())
async def start(msg: Message):
    url = _demo_url()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📖 Книга рецептов", web_app=WebAppInfo(url=f"{url}/")),
    ]]) if url else None
    await msg.answer(
        "👋 Пришли ссылку на видео с рецептом — из <b>Instagram</b>, "
        "<b>TikTok</b> или <b>YouTube</b>.\n\n"
        "Я расшифрую звук и соберу рецепт: ингредиенты, шаги по порядку "
        "и переход к нужному моменту видео.\n\n"
        "Все рецепты копятся в книге — она на кнопке ниже или в меню слева от поля ввода.",
        reply_markup=kb,
    )


@dp.message(Command("recipes"))
async def recipes_cmd(msg: Message):
    url = _demo_url()
    if not url:
        await msg.answer("Книга рецептов появится, когда настроен публичный адрес мини-аппа.")
        return
    await msg.answer(
        "📖 Твоя книга рецептов:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть", web_app=WebAppInfo(url=f"{url}/")),
        ]]),
    )


@dp.message(Command("help"))
async def help_cmd(msg: Message):
    await msg.answer(
        "Как пользоваться:\n"
        "1. Скопируй ссылку на видео с готовкой (Reels / TikTok / Shorts / обычное видео).\n"
        "2. Пришли её сюда.\n"
        "3. Через 1–3 минуты придёт карточка рецепта.\n\n"
        "⚠️ Закрытые аккаунты Instagram бот не прочитает — видео должно быть публичным."
    )


def _is_admin(msg: Message) -> bool:
    return bool(msg.from_user) and msg.from_user.id == ADMIN_ID


@dp.message(Command("id"))
async def id_cmd(msg: Message):
    await msg.answer(f"Твой id: <code>{msg.from_user.id}</code>")


@dp.message(Command("private"))
async def private_cmd(msg: Message):
    if not _is_admin(msg):
        return
    ids, _ = load_access()
    ids.add(ADMIN_ID)
    save_access(ids, True)
    await msg.answer("🔒 Приватный режим включён. Доступ у: "
                     + ", ".join(f"<code>{i}</code>" for i in sorted(ids)))


@dp.message(Command("public"))
async def public_cmd(msg: Message):
    if not _is_admin(msg):
        return
    ids, _ = load_access()
    save_access(ids, False)
    await msg.answer("🔓 Бот снова открыт для всех.")


@dp.message(Command("allow"))
async def allow_cmd(msg: Message, command: CommandObject):
    if not _is_admin(msg):
        return
    ids, private = load_access()
    extra = {int(x) for x in (command.args or "").replace(",", " ").split() if x.strip().lstrip("-").isdigit()}
    if not extra:
        await msg.answer("Кого пустить? Например: <code>/allow 123456789</code>")
        return
    ids.update(extra)
    save_access(ids, private)
    await msg.answer("Готово. Доступ у: " + ", ".join(f"<code>{i}</code>" for i in sorted(ids)))


@dp.message(Command("deny"))
async def deny_cmd(msg: Message, command: CommandObject):
    if not _is_admin(msg):
        return
    ids, private = load_access()
    extra = {int(x) for x in (command.args or "").replace(",", " ").split() if x.strip().lstrip("-").isdigit()}
    if not extra:
        await msg.answer("Кого убрать? Например: <code>/deny 123456789</code>")
        return
    ids -= extra
    save_access(ids, private)
    await msg.answer("Убрано. Доступ у: " + ", ".join(f"<code>{i}</code>" for i in sorted(ids)) or "пусто")


@dp.message(Command("access"))
async def access_cmd(msg: Message):
    if not _is_admin(msg):
        return
    ids, private = load_access()
    state = "🔒 приватный" if private else "🔓 открыт для всех"
    await msg.answer(f"Доступ: {state}\nСписок: "
                     + (", ".join(f"<code>{i}</code>" for i in sorted(ids)) or "пусто"))


@dp.message(F.text, ~F.text.startswith("/"))
async def on_text(msg: Message):
    text = (msg.text or "").strip()

    pending = PENDING.pop(msg.chat.id, None)
    if pending and pending.get("action") == "edit":
        await _apply_edit(msg, pending["rid"], text)
        return

    link = LINK_RE.search(text)
    bare = re.search(r"https?://\S+", text)

    if link:
        await _process(msg, "/api/recipe", {"url": link.group(0)},
                       "⏳ Слушаю видео и разбираю рецепт…")
    elif bare:
        await _process(msg, "/api/page", {"url": bare.group(0)},
                       "⏳ Читаю страницу и собираю рецепт…")
    elif len(text) >= 80:
        await _process(msg, "/api/text", {"text": text}, "⏳ Разбираю текст в рецепт…")
    else:
        await msg.answer(
            "Пришли одно из трёх:\n"
            "• ссылку на видео (Instagram, TikTok, YouTube)\n"
            "• ссылку на страницу с рецептом\n"
            "• сам текст рецепта — прямо сообщением"
        )


def _kb_recipe(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Открыть рецепт",
                              web_app=WebAppInfo(url=f"{WEBAPP_URL}/#r/{rid}"))],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit:{rid}"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{rid}")],
    ])


async def _process(msg: Message, endpoint: str, payload: dict, status_text: str):
    status = await msg.answer(status_text)

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{API}{endpoint}", json=payload,
                              timeout=aiohttp.ClientTimeout(total=900)) as r:
                data = await r.json()
    except Exception as e:
        await status.edit_text(f"😔 Не получилось: <code>{str(e)[:200]}</code>")
        return

    if "error" in data:
        await status.edit_text(f"😔 {data['error'][:400]}")
        return

    rid = data["id"]
    head = f"<b>{data.get('title') or 'Рецепт'} — готово!</b>"
    if not data.get("live", True):
        head += "\n<i>GitHub Pages ещё собирает страницу — открой через полминуты.</i>"

    if WEBAPP_URL:
        await status.edit_text(head, reply_markup=_kb_recipe(rid))
    else:
        await status.edit_text(await _text_recipe(rid))


async def _apply_edit(msg: Message, rid: str, instruction: str):
    status = await msg.answer("✏️ Правлю рецепт…")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{API}/api/edit", json={"id": rid, "instruction": instruction},
                              timeout=aiohttp.ClientTimeout(total=400)) as r:
                data = await r.json()
    except Exception as e:
        await status.edit_text(f"😔 Не получилось: <code>{str(e)[:200]}</code>")
        return

    if "error" in data:
        await status.edit_text(f"😔 {data['error'][:300]}")
        return

    await status.edit_text(
        f"<b>{data.get('title')} — обновлено</b>\n<i>Открой рецепт, чтобы увидеть правки.</i>",
        reply_markup=_kb_recipe(rid) if WEBAPP_URL else None,
    )


@dp.callback_query(F.data.startswith("edit:"))
async def cb_edit(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    PENDING[cq.message.chat.id] = {"action": "edit", "rid": rid}
    await cq.message.answer(
        "✏️ Что изменить? Напиши одним сообщением, например:\n"
        "«убери сливки, добавь чеснок» или «переименуй в „Паста с беконом“»"
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("del:"))
async def cb_del(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    await cq.message.answer(
        "🗑 Удалить рецепт? Он исчезнет из книги, файл уйдёт из репозитория.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Да, удалить", callback_data=f"delok:{rid}"),
            InlineKeyboardButton(text="Отмена", callback_data="cancel"),
        ]]),
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("delok:"))
async def cb_delok(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{API}/api/delete", json={"id": rid}, timeout=aiohttp.ClientTimeout(total=120)) as r:
                data = await r.json()
    except Exception as e:
        await cq.message.edit_text(f"😔 Не получилось: <code>{str(e)[:200]}</code>")
        await cq.answer()
        return

    if "error" in data:
        await cq.message.edit_text(f"😔 {data['error'][:300]}")
    else:
        await cq.message.edit_text(f"🗑 Удалено: «{data.get('title')}»")
    await cq.answer()


@dp.callback_query(F.data == "cancel")
async def cb_cancel(cq: CallbackQuery):
    await cq.message.edit_text("Отменено.")
    await cq.answer()


@dp.message(CommandStart(deep_link=True))
async def start_payload(msg: Message, command: CommandObject):
    payload = command.args or ""
    if payload.startswith("edit_"):
        PENDING[msg.chat.id] = {"action": "edit", "rid": payload[5:]}
        await msg.answer("✏️ Что изменить в этом рецепте? Напиши одним сообщением.")
    elif payload.startswith("del_"):
        await msg.answer(
            "🗑 Удалить этот рецепт?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Да, удалить", callback_data=f"delok:{payload[4:]}"),
                InlineKeyboardButton(text="Отмена", callback_data="cancel"),
            ]]),
        )
    else:
        await start(msg)


@dp.message()
async def fallback(msg: Message):
    await msg.answer("Пришли ссылку на видео, ссылку на страницу с рецептом или текст рецепта.")


async def _text_recipe(rid: str) -> str:
    """Запасной формат: рецепт сообщением, без мини-аппа."""
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{API}/r/{rid}/recipe.json") as r:
            rec = await r.json()

    ings = "\n".join(
        f"• {i['name']} — {i.get('qty') or 'по вкусу'}"
        + (f" <i>{i['est']}</i>" if i.get("est") and (i.get("qty") == "по вкусу") else "")
        for i in rec.get("ingredients", [])
    )
    steps = "\n".join(
        f"{s['n']}. {s['text']}" + (f" <i>({s['at']})</i>" if s.get("at") else "")
        for s in rec.get("steps", [])
    )
    head = f"{rec.get('emoji','')} <b>{rec.get('title')}</b>"
    meta = f"⏱ {rec.get('time_min','—')} мин · 🍽 {rec.get('servings','—')} порц."
    out = f"{head}\n<i>{rec.get('subtitle','')}</i>\n\n{meta}\n\n<b>Ингредиенты</b>\n{ings}\n\n<b>Шаги</b>\n{steps}"
    if rec.get("source", {}).get("url"):
        out += f"\n\n<a href='{rec['source']['url']}'>▶ Оригинал видео</a>"
    return out[:4000]


def _demo_url() -> str:
    return f"{WEBAPP_URL}" if WEBAPP_URL else ""


async def main():
    load_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("нет TELEGRAM_BOT_TOKEN — создай бота у @BotFather и положи токен в .env")
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)

    # белый список: если файла нет — создаём и закрываем бота на админа
    if not ACCESS_FILE.exists():
        save_access({ADMIN_ID}, True)
    ids, private = load_access()
    print(f"доступ: {'приватный' if private else 'открытый'} · {sorted(ids)}", flush=True)

    dp.message.middleware(AccessGate())
    dp.callback_query.middleware(AccessGate())

    # постоянная кнопка «Рецепты» слева от поля ввода — мини-апп в один тап
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Рецепты", web_app=WebAppInfo(url=f"{WEBAPP_URL}/"),
                )
            )
            print(f"кнопка меню → {WEBAPP_URL}/", flush=True)
        except Exception as e:
            print(f"⚠️  не удалось поставить кнопку меню: {e}", flush=True)

    print("бот запущен", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
