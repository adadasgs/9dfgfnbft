import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from dotenv import load_dotenv
import database as db
from scanner import ProfileScanner

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("profile-radar")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DESTINATION_CHAT_ID = int(os.getenv("DESTINATION_CHAT_ID", "0"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "900"))

# Same five GRAM bands as the Portals project. A profile is not assigned a band
# until a reliable market valuation source is connected; never invent a price.
CATEGORIES = [
    ("under5", "До 5 GRAM"),
    ("5_15", "5–15 GRAM"),
    ("15_27_5", "15–27.5 GRAM"),
    ("27_5_60", "27.5–60 GRAM"),
    ("over60", "60+ GRAM"),
]

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scanner = None


def profile_keyboard(user_id: int, username: str | None):
    contact_url = f"https://t.me/{username}" if username else None
    rows = [[InlineKeyboardButton(text="📌 Занять", callback_data=f"reserve:{user_id}"),
             InlineKeyboardButton(text="📋 Открыть лог", callback_data=f"log:{user_id}")]]
    if contact_url:
        rows.append([InlineKeyboardButton(text="✉️ Написать продавцу", url=contact_url)])
    else:
        rows.append([InlineKeyboardButton(text="✉️ Написать продавцу", callback_data=f"no_contact:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gift_name(gift):
    return getattr(gift, "title", None) or getattr(gift, "slug", None) or "Collectible Gift"


async def publish_profile(user, gifts):
    username = getattr(user, "username", None)
    display_name = " ".join(x for x in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if x) or "Telegram user"
    profile_url = f"https://t.me/{username}" if username else None
    summary = "\n".join(f"• {gift_name(g)}" for g in gifts[:20])
    await db.save_profile({
        "user_id": user.id, "username": username, "display_name": display_name,
        "profile_url": profile_url, "gifts_count": len(gifts),
        "gift_summary": summary, "category": "unpriced"
    })
    existing = await db.get_profile(user.id)
    # Publish only once; updates are reflected in the log/database, avoiding duplicate group posts.
    if existing and existing.get("message_id"):
        return
    text = (
        "🎁 <b>Найдены NFT-подарки в профиле</b>\n\n"
        f"👤 Профиль: <a href=\"{profile_url}\">{display_name}</a>\n" if profile_url else
        "🎁 <b>Найдены NFT-подарки в профиле</b>\n\n" + f"👤 {display_name}\n"
    )
    text += f"🧩 NFT-подарков: <b>{len(gifts)}</b>\n\n<b>Подарки:</b>\n{summary}\n\n"
    text += "💎 Оценка цены: пока не рассчитана"
    sent = await bot.send_message(DESTINATION_CHAT_ID, text, reply_markup=profile_keyboard(user.id, username), disable_web_page_preview=True)
    await db.attach_message(user.id, DESTINATION_CHAT_ID, sent.message_id)


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "<b>NFT Profile Radar</b>\n\n"
        "Бот публикует найденные NFT-подарки в профилях Telegram.\n"
        "Сканирование работает по известным пользователям или участникам указанной исходной группы."
    )


@dp.message(Command("scan"))
async def scan_command(message: Message):
    if scanner is None:
        await message.answer("Сканер ещё не подключён. Проверьте настройки Render.")
        return
    await message.answer("🔎 Запускаю проверку доступных профилей…")
    count = 0
    async for user, gifts in scanner.scan_once():
        await publish_profile(user, gifts)
        count += 1
    await message.answer(f"Готово. Найдено профилей с NFT-подарками: {count}.")


@dp.callback_query(F.data.startswith("reserve:"))
async def reserve_callback(call: CallbackQuery):
    user_id = int(call.data.split(":", 1)[1])
    who = call.from_user.username or str(call.from_user.id)
    ok = await db.reserve_profile(user_id, call.from_user.id, who)
    if not ok:
        profile = await db.get_profile(user_id)
        await call.answer("Уже занято" + (f": @{profile['reserved_username']}" if profile and profile.get('reserved_username') else ""), show_alert=True)
        return
    profile = await db.get_profile(user_id)
    text = (call.message.text or "") + f"\n\n🔒 Занял: @{who if call.from_user.username else who}"
    await call.message.edit_text(text, reply_markup=profile_keyboard(user_id, profile.get("username")), disable_web_page_preview=True)
    await call.answer("Профиль закреплён за вами")


@dp.callback_query(F.data.startswith("log:"))
async def log_callback(call: CallbackQuery):
    user_id = int(call.data.split(":", 1)[1])
    profile = await db.get_profile(user_id)
    entries = await db.get_log(user_id)
    if not profile:
        await call.answer("Запись не найдена", show_alert=True)
        return
    reserved = profile.get("reserved_username")
    text = (
        f"📋 <b>Лог профиля</b>\n\n"
        f"ID: <code>{user_id}</code>\n"
        f"Username: @{profile.get('username') or 'нет'}\n"
        f"NFT-подарков: {profile.get('gifts_count', 0)}\n"
        f"Занято: {'да, @' + reserved if reserved else 'нет'}\n\n"
        f"<b>Подарки:</b>\n{profile.get('gift_summary') or 'нет данных'}\n\n"
        f"Записей сканирования: {len(entries)}"
    )
    await call.message.answer(text)
    await call.answer()


@dp.callback_query(F.data.startswith("no_contact:"))
async def no_contact_callback(call: CallbackQuery):
    await call.answer("У пользователя нет публичного username. Открыть личный чат по ID может быть невозможно.", show_alert=True)


async def scanner_loop():
    global scanner
    scanner = ProfileScanner()
    await scanner.start()
    log.info("Profile scanner connected")
    while True:
        try:
            async for user, gifts in scanner.scan_once():
                await publish_profile(user, gifts)
        except Exception:
            log.exception("Scanner cycle failed")
        await asyncio.sleep(SCAN_INTERVAL)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    if not DESTINATION_CHAT_ID:
        raise RuntimeError("DESTINATION_CHAT_ID is missing")
    await db.init_db()
    task = asyncio.create_task(scanner_loop())
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await bot.session.close()
        if scanner:
            await scanner.stop()

if __name__ == "__main__":
    asyncio.run(main())
