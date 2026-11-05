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
    ("under5", "До 5 GRAM"),
    ("5_15", "5–15 GRAM"),
    ("15_27_5", "15–27.5 GRAM"),
    ("27_5_60", "27.5–60 GRAM"),
    ("over60", "60+ GRAM"),
]

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scanner = None
scanner_error = "Сканер ещё не запускался"


async def ensure_forum_topics(create_missing=False):
    # Telegram Bot API cannot list forum topics. Reuse IDs from Render env or SQLite.
    # Never create topics during startup; only an explicit /topics command may do so.
    saved = await db.get_forum_topics()
    env_ids = {
        "under5": os.getenv("TOPIC_UNDER5_ID"),
        "5_15": os.getenv("TOPIC_5_15_ID"),
        "15_27_5": os.getenv("TOPIC_15_27_5_ID"),
        "27_5_60": os.getenv("TOPIC_27_5_60_ID"),
        "over60": os.getenv("TOPIC_OVER60_ID"),
    }
    resolved = {}
    for key, title in CATEGORIES:
        raw_id = env_ids.get(key)
        thread_id = int(raw_id) if raw_id and raw_id.isdigit() else saved.get(key)
        if thread_id:
            try:
                await bot.edit_forum_topic(chat_id=DESTINATION_CHAT_ID, message_thread_id=thread_id, name=title)
                resolved[key] = thread_id
                if saved.get(key) != thread_id:
                    await db.save_forum_topic(key, thread_id)
                continue
            except Exception:
                log.exception("Saved forum topic %s (%s) is unavailable", title, thread_id)
                saved.pop(key, None)
        if create_missing:
            topic = await bot.create_forum_topic(chat_id=DESTINATION_CHAT_ID, name=title)
            thread_id = topic.message_thread_id
            await db.save_forum_topic(key, thread_id)
            resolved[key] = thread_id
            log.info("Created forum topic %s: %s", key, thread_id)
    return resolved

def profile_keyboard(user_id, username, listing_url=None):
    open_button = (
        InlineKeyboardButton(text="🔗 Открыть лот", url=listing_url)
        if listing_url and listing_url.startswith(("https://t.me/", "https://"))
        else InlineKeyboardButton(text="🔗 Открыть лот", callback_data=f"open_missing:{user_id}")
    )
    seller_button = (
        InlineKeyboardButton(text="✉️ Написать продавцу", url=f"https://t.me/{username}")
        if username
        else InlineKeyboardButton(text="✉️ Написать продавцу", callback_data=f"no_contact:{user_id}")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [open_button, InlineKeyboardButton(text="📌 Занять", callback_data=f"reserve:{user_id}")],
        [InlineKeyboardButton(text="📋 Лог", callback_data=f"log:{user_id}"), seller_button],
    ])


async def publish_profile(user, gifts):
    user_id = int(user.id)
    username = getattr(user, "username", None)
    price = getattr(user, "price_ton", None)  # legacy field; scanner stores GRAM here
    telegram_id = getattr(user, "telegram_id", None)
    source = getattr(user, "source", None)
    # Distribute every detected gift listing across the five forum topics.
    # The listing key is stable, so retries go to the same topic.
    category = CATEGORIES[abs(user_id) % len(CATEGORIES)][0]
    name = " ".join(
        x for x in [
            getattr(user, "first_name", None),
            getattr(user, "last_name", None),
        ] if x
    ) or (f"Продавец @{username}" if username else "Продавец")

    gift_lines = []
    for gift in gifts[:20]:
        title = escape(getattr(gift, "title", "Telegram gift"))
        url = getattr(gift, "url", None)
        gift_lines.append(f'<a href="{escape(url)}">{title}</a>' if url else title)
    summary = "\n".join(gift_lines)

    await db.save_profile({
        "user_id": user_id,
        "username": username,
        "display_name": name,
        "profile_url": f"https://t.me/{username}" if username else None,
        "wallet_address": None,
        "nft_address": None,
        "listing_url": getattr(user, "listing_url", None),
        "price": price,
        "price_currency": "GRAM" if price is not None else None,
        "gifts_count": len(gifts),
        "gift_summary": summary,
        "category": category,
    })
    old = await db.get_profile(user_id)
    if old and old.get("message_id"):
        await db.log_scan(user_id, username, "Повторная проверка публичного объявления")
        return False

    identity = (
        (f'🔗 Telegram: <a href="https://t.me/{escape(username)}">@{escape(username)}</a>\n' if username else "🔗 Telegram: username не указан в объявлении\n")
        + (f"🆔 Telegram ID: <code>{telegram_id}</code>" if telegram_id else "🆔 Telegram ID: источник не предоставил")
    )
    price_text = f"💎 Цена: <b>{price:g} GRAM</b>" if price is not None else "💎 Цена: неизвестна"
    source_note = (
        f"Источник: <a href=\"https://t.me/{escape(source.split('/')[-1])}\">{escape(source)}</a>. "
        "Username указан в публичном объявлении."
        if source else "Username указан в публичном объявлении."
    )
    text = (
        "🎁 <b>Найдено публичное объявление о Telegram-подарке</b>\n\n"
        f"👤 Продавец: {escape(name)}\n{identity}\n"
        f"🧩 Объявлений: <b>{len(gifts)}</b>\n{price_text}\n\n"
        f"<b>Публичное объявление:</b>\n{summary}\n\n{source_note}"
    )
    topics = await db.get_forum_topics()
    kwargs = {
        "chat_id": DESTINATION_CHAT_ID,
        "text": text,
        "reply_markup": profile_keyboard(user_id, username, getattr(user, "listing_url", None)),
        "disable_web_page_preview": True,
    }
    if topics.get(category):
        kwargs["message_thread_id"] = topics[category]
    sent = await bot.send_message(**kwargs)
    await db.attach_message(user_id, DESTINATION_CHAT_ID, sent.message_id)
    await db.log_scan(user_id, username, f"Опубликовано объявление; цена {price} GRAM")
    return True


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "<b>NFT Profile Radar</b>\n\n"
        "Проверяет публичные Telegram-каналы и группы с объявлениями о подарках. "
        "Публикует только подтверждённый в объявлении @username и цену в GRAM. "
        "Для дополнительной ручной проверки доступны ChatCrawler и Gift Explore. "
        "Кошельки и TON-адреса не используются."
    )


@dp.message(Command("scan"))
async def scan_command(message: Message):
    if scanner is None:
        await message.answer("❌ Сканер не подключён.\n" + escape(scanner_error))
        return
    status = await message.answer("🔎 Проверяю публичные Telegram-объявления о подарках…")
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
            f"Каналов в списке: {stats.get('channels_total', 0)}\n"
            f"Проверено каналов: {stats.get('channels_checked', 0)}\n"
            f"Недоступно каналов: {stats.get('channels_unavailable', 0)}\n"
            f"Проверено постов: {stats.get('posts_checked', 0)}\n"
            f"Постов о подарках: {stats.get('gift_posts', 0)}\n"
            f"Постов с контактом продавца: {stats.get('seller_contacts', 0)}\n"
            f"Контактов с ценой в GRAM: {stats.get('with_username_and_price', 0)}\n"
            f"Подходящих объявлений: {count}\n"
            f"Новых карточек в группе: {published}\n"
            f"Уже опубликовано ранее: {duplicates}"
        )
        await status.edit_text("✅ <b>Проверка завершена</b>\n\n" + details)
    except Exception as exc:
        log.exception("Manual /scan failed")
        await status.edit_text(
            "❌ <b>Ошибка сканирования</b>\n<code>"
            + escape(str(exc))[:1200]
            + "</code>\n\nПодробности смотри в Render → Logs."
        )


@dp.message(Command("topics"))
async def topics_command(message: Message):
    try:
        await ensure_forum_topics(create_missing=True)
        saved = await db.get_forum_topics()
        lines = [f"• {title}: {saved.get(key, 'нет')}" for key, title in CATEGORIES]
        await message.answer("Темы форума готовы.\n\n" + "\n".join(lines))
    except Exception as exc:
        log.exception("Topic setup failed")
        await message.answer("Ошибка создания тем: " + escape(str(exc)))


@dp.message(Command("sources"))
async def sources_command(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 ChatCrawler — публичный поиск", url="https://app.chatcrawler.app/")],
        [InlineKeyboardButton(text="🎁 Gift Explore — поиск подарков", url="https://t.me/giftexplorebot")],
    ])
    await message.answer(
        "<b>Источники дополнительной проверки</b>\n\n"
        "• ChatCrawler — бесплатный поиск по открытым Telegram-постам и профилям авторов.\n"
        "• Gift Explore — поиск подарков, история цен и поиск владельцев; бесплатные запросы ограничены.\n\n"
        "Сервисы открываются отдельно: подтверждённого публичного API для автоматического "
        "получения их результатов не найдено. Бот не будет выдавать имя канала или кошелёк за username продавца.",
        reply_markup=keyboard,
    )


@dp.message(Command("status"))
async def status_command(message: Message):
    if scanner is not None:
        await message.answer(
            f"🟢 Сканер публичных Telegram-объявлений подключён. Интервал: {SCAN_INTERVAL} сек."
        )
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
        reply_markup=profile_keyboard(\n            user_id,\n            profile.get("username") if profile else None,\n            profile.get("listing_url") if profile else None,\n        ),
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
    answer = (
        "📋 <b>Лог объявления</b>\n\n"
        + f"Username: @{escape(profile.get('username') or 'нет')}\n"
        + f"Подарков в объявлении: {profile.get('gifts_count', 0)}\n"
        + f"Цена: {profile.get('price') or 'неизвестна'} {escape(profile.get('price_currency') or '')}\n"
        + f"Занято: {escape(profile.get('reserved_username') or 'нет')}\n\n"
        + f"<b>Объявление:</b>\n{escape(profile.get('gift_summary') or 'нет данных')}\n\n"
        + f"<b>События:</b>\n{events}"
    )
    await call.message.answer(answer)
    await call.answer()


@dp.callback_query(F.data.startswith("open_missing:"))
async def open_missing_callback(call: CallbackQuery):
    await call.answer("Ссылка на исходный лот отсутствует в источнике.", show_alert=True)


@dp.callback_query(F.data.startswith("no_contact:"))
async def no_contact_callback(call: CallbackQuery):
    await call.answer(
        "Публичный Telegram username продавца не найден в объявлении.",
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
                log.info("Public Telegram gift-ad scanner connected")
            async for user, gifts in scanner.scan_once():
                await publish_profile(user, gifts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            scanner_error = str(exc)
            log.exception("Public Telegram scanner failed")
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
        await ensure_forum_topics(create_missing=False)
    except Exception:
        log.exception("Forum topic restore failed")
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
