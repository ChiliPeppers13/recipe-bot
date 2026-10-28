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
import os
import re
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
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


@dp.message(F.text.regexp(LINK_RE))
async def on_link(msg: Message):
    url = LINK_RE.search(msg.text).group(0)
    status = await msg.answer("⏳ Слушаю видео и разбираю рецепт…")

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{API}/api/recipe", json={"url": url},
                              timeout=aiohttp.ClientTimeout(total=900)) as r:
                data = await r.json()
    except Exception as e:
        await status.edit_text(f"😔 Не получилось: <code>{str(e)[:200]}</code>")
        return

    if "error" in data:
        await status.edit_text(
            "😔 Не смог разобрать это видео.\n\n"
            f"<code>{data['error'][:300]}</code>\n\n"
            "Частые причины: закрытый аккаунт, нет звука или видео удалено."
        )
        return

    rid = data["id"]
    live = data.get("live", True)
    head = f"<b>{data.get('title') or 'Рецепт'} — готово!</b>"
    if not live:
        head += "\n<i>GitHub Pages ещё собирает страницу — открой через полминуты.</i>"

    if WEBAPP_URL:
        await status.edit_text(
            head,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📖 Открыть рецепт",
                                     web_app=WebAppInfo(url=f"{WEBAPP_URL}/#r/{rid}")),
            ]]),
        )
    else:
        await status.edit_text(await _text_recipe(rid))


@dp.message()
async def fallback(msg: Message):
    await msg.answer("Пришли ссылку на видео с рецептом (Instagram, TikTok или YouTube).")


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
