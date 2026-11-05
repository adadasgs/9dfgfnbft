import asyncio
import logging
import os
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
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

CATEGORIES = [
    ("under5", "До 5 TON"),
    ("5_15", "5–15 TON"),
    ("15_27_5", "15–27.5 TON"),
    ("27_5_60", "27.5–60 TON"),
    ("over60", "60+ TON"),
]

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scanner = None
scanner_error = "Сканер ещё не запускался"


async def ensure_forum_topics():
    saved = await db.get_forum_topics()
    for key, title in CATEGORIES:
        if key not in saved:
            topic = await bot.create_forum_topic(chat_id=DESTINATION_CHAT_ID, name=title)
            await db.save_forum_topic(key, topic.message_thread_id)
        else:
            try:
                await bot.edit_forum_topic(
                    chat_id=DESTINATION_CHAT_ID,
                    message_thread_id=saved[key],
                    name=title,
                )
            except Exception:
                log.exception("Could not rename topic %s", title)


def profile_keyboard(user_id, username):
    rows = [[
        InlineKeyboardButton(text="📌 Занять", callback_data=f"reserve:{user_id}"),
        InlineKeyboardButton(text="📋 Открыть лог", callback_data=f"log:{user_id}"),
    ]]
    if username:
        rows.append([InlineKeyboardButton(text="✉️ Написать продавцу", url=f"https://t.me/{username}")])
    else:
        rows.append([InlineKeyboardButton(text="✉️ Написать продавцу", callback_data=f"no_contact:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def publish_profile(user, gifts):
    user_id = int(user.id)
    username = getattr(user, "username", None)
    wallet = getattr(user, "wallet_address", None)
    price = getattr(user, "price_ton", None)
    category = getattr(user, "category", "unpriced")
    name = " ".join(x for x in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if x)
    name = name or "TON wallet"

    gift_lines = []
    for gift in gifts[:20]:
        title = escape(getattr(gift, "title", "TON NFT"))
        address = getattr(gift, "address", None)
        url = getattr(gift, "url", None)
        gift_lines.append(f'• <a href="{escape(url)}">{title}</a>' if url else f"• {title}")
        if address:
            gift_lines.append(f"  <code>{escape(address)}</code>")
    summary = "\n".join(gift_lines)

    await db.save_profile({
        "user_id": user_id,
        "username": username,
        "display_name": name,
        "profile_url": f"https://t.me/{username}" if username else None,
        "wallet_address": wallet,
        "nft_address": getattr(user, "nft_address", None),
        "listing_url": getattr(user, "listing_url", None),
        "price": price,
        "price_currency": "TON" if price is not None else None,
        "gifts_count": len(gifts),
        "gift_summary": summary,
        "category": category,
    })
    old = await db.get_profile(user_id)
    if old and old.get("message_id"):
        await db.log_scan(user_id, username, "Повторная проверка NFT-листинга")
        return False

    if wallet:
        seller = f"👛 Кошелёк владельца: <code>{escape(wallet)}</code>"
        identity = "🔗 Telegram username: не найден в публичных данных\n🆔 Telegram ID: недоступен из TON-адреса"
        heading = "💎 <b>Найден NFT-листинг в сети TON</b>"
    else:
        seller = f"👤 Продавец: {escape(name)}"
        identity = f"🔗 Username: @{escape(username) if username else 'не указан'}\n🆔 Telegram ID: <code>{user_id}</code>"
        heading = "🎁 <b>Найдены NFT-подарки</b>"

    price_text = f"💎 Цена: <b>{price:.4f} TON</b>" if price is not None else "💎 Цена: неизвестна"
    text = (
        f"{heading}\n\n{seller}\n{identity}\n"
        f"🧩 NFT: <b>{len(gifts)}</b>\n{price_text}\n\n"
        f"<b>Объект:</b>\n{summary}\n\n"
        "ℹ️ Адрес кошелька не подтверждает личность владельца в Telegram."
    )
    topics = await db.get_forum_topics()
    kwargs = {
        "chat_id": DESTINATION_CHAT_ID,
        "text": text,
        "reply_markup": profile_keyboard(user_id, username),
        "disable_web_page_preview": True,
    }
    if topics.get(category):
        kwargs["message_thread_id"] = topics[category]
    sent = await bot.send_message(**kwargs)
    await db.attach_message(user_id, DESTINATION_CHAT_ID, sent.message_id)
    await db.log_scan(user_id, username, f"Опубликован NFT; цена {price} TON")
    return True


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "<b>NFT Profile Radar</b>\n\n"
        "Сканирует публичные NFT-листинги TON и показывает адрес кошелька владельца. "
        "Telegram username и ID указываются только при наличии достоверных данных."
    )


@dp.message(Command("scan"))
async def scan_command(message: Message):
    if scanner is None:
        await message.answer("❌ Сканер не подключён.\n" + escape(scanner_error))
        return
    status = await message.answer("🔎 Проверяю публичные NFT-листинги TON…")
    try:
        count = 0
        published = 0
        duplicates = 0
        async for user, gifts in scanner.scan_once():
            count += 1
            was_published = await publish_profile(user, gifts)
            if was_published:
                published += 1
            else:
                duplicates += 1
        stats = getattr(scanner, "last_stats", {})
        details = (
            f"Получено NFT: {stats.get('items', 0)}\n"
            f"Обнаружено контрактов продажи: {stats.get('on_sale', 0)}\n"
            f"Проверено объявлений: {stats.get('sales_checked', 0)}\n"
            f"Подходящих с ценой и владельцем: {count}\n"
            f"Новых карточек в группе: {published}\n"
            f"Уже опубликовано ранее: {duplicates}"
        )
        await status.edit_text("✅ <b>Проверка завершена</b>\n\n" + details)
    except Exception as exc:
        log.exception("Manual /scan failed")
        await status.edit_text("❌ <b>Ошибка сканирования</b>\\n<code>" + escape(str(exc))[:1200] + "</code>\\n\\nПодробности смотри в Render → Logs.")


@dp.message(Command("topics"))
async def topics_command(message: Message):
    try:
        await ensure_forum_topics()
        saved = await db.get_forum_topics()
        lines = [f"• {title}: {saved.get(key, 'нет')}" for key, title in CATEGORIES]
        await message.answer("Темы форума готовы.\n\n" + "\n".join(lines))
    except Exception as exc:
        log.exception("Topic setup failed")
        await message.answer("Ошибка создания тем: " + escape(str(exc)))


@dp.message(Command("status"))
async def status_command(message: Message):
    if scanner is not None:
        await message.answer(f"🟢 TON-сканер подключён. Интервал: {SCAN_INTERVAL} сек.")
    else:
        await message.answer("🔴 Сканер не подключён.\n" + escape(scanner_error))


@dp.callback_query(F.data.startswith("reserve:"))
async def reserve_callback(call: CallbackQuery):
    user_id = int(call.data.split(":", 1)[1])
    who = call.from_user.username or str(call.from_user.id)
    if not await db.reserve_profile(user_id, call.from_user.id, who):
        profile = await db.get_profile(user_id)
        reserved = profile.get("reserved_username") if profile else None
        await call.answer("Уже занято" + (f": {escape(reserved)}" if reserved else ""), show_alert=True)
        return
    profile = await db.get_profile(user_id)
    await db.log_scan(user_id, profile.get("username") if profile else None, f"Занято: {who}")
    await call.message.edit_text(
        (call.message.text or "") + f"\n\n🔒 <b>Занял:</b> {escape(who)}",
        reply_markup=profile_keyboard(user_id, profile.get("username") if profile else None),
        disable_web_page_preview=True,
    )
    await call.answer("Листинг закреплён за вами")


@dp.callback_query(F.data.startswith("log:"))
async def log_callback(call: CallbackQuery):
    user_id = int(call.data.split(":", 1)[1])
    profile = await db.get_profile(user_id)
    entries = await db.get_log(user_id)
    if not profile:
        await call.answer("Запись не найдена", show_alert=True)
        return
    events = "\n".join(
        f"• {escape(str(x.get('scanned_at', '')))} — {escape(x.get('result') or '')}"
        for x in entries[:10]
    ) or "Пока нет событий"
    wallet = f"Кошелёк: <code>{escape(profile.get('wallet_address') or '')}</code>\n" if profile.get("wallet_address") else ""
    answer = (
        "📋 <b>Лог NFT</b>\n\n" + wallet
        + f"Username: @{escape(profile.get('username') or 'нет')}\n"
        + f"NFT: {profile.get('gifts_count', 0)}\n"
        + f"Цена: {profile.get('price') or 'неизвестна'} {escape(profile.get('price_currency') or '')}\n"
        + f"Занято: {escape(profile.get('reserved_username') or 'нет')}\n\n"
        + f"<b>Объект:</b>\n{escape(profile.get('gift_summary') or 'нет данных')}\n\n"
        + f"<b>События:</b>\n{events}"
    )
    await call.message.answer(answer)
    await call.answer()


@dp.callback_query(F.data.startswith("no_contact:"))
async def no_contact_callback(call: CallbackQuery):
    await call.answer(
        "Публичный Telegram-контакт не найден. Адрес кошелька не позволяет написать владельцу в Telegram.",
        show_alert=True,
    )


async def scanner_loop():
    global scanner, scanner_error
    while True:
        try:
            if scanner is None:
                scanner = ProfileScanner()
                await scanner.start()
                scanner_error = ""
                log.info("Public TON NFT scanner connected")
            async for user, gifts in scanner.scan_once():
                await publish_profile(user, gifts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            scanner_error = str(exc)
            log.exception("TON scanner failed")
            if scanner is not None:
                try:
                    await scanner.stop()
                except Exception:
                    log.exception("Scanner stop failed")
                scanner = None
        await asyncio.sleep(SCAN_INTERVAL)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    if not DESTINATION_CHAT_ID:
        raise RuntimeError("DESTINATION_CHAT_ID is missing")
    await db.init_db()
    try:
        await ensure_forum_topics()
    except Exception:
        log.exception("Forum topic setup failed")
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
