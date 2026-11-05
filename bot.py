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

# Price categories are deliberately not assigned until a trustworthy valuation exists.
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


async def ensure_forum_topics():
    saved = await db.get_forum_topics()
    created = []
    for key, title in CATEGORIES:
        if key not in saved:
            topic = await bot.create_forum_topic(chat_id=DESTINATION_CHAT_ID, name=title)
            await db.save_forum_topic(key, topic.message_thread_id)
            created.append(title)
            log.info("Created forum topic %s (thread_id=%s)", title, topic.message_thread_id)
    return created


def profile_keyboard(user_id: int, username: str | None):
    rows = [[
        InlineKeyboardButton(text="📌 Занять", callback_data=f"reserve:{user_id}"),
        InlineKeyboardButton(text="📋 Открыть лог", callback_data=f"log:{user_id}"),
    ]]
    if username:
        rows.append([InlineKeyboardButton(text="✉️ Написать продавцу", url=f"https://t.me/{username}")])
    else:
        rows.append([InlineKeyboardButton(text="✉️ Написать продавцу", callback_data=f"no_contact:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gift_name(gift):
    return getattr(gift, "title", None) or getattr(gift, "slug", None) or "Collectible Gift"


async def publish_profile(user, gifts):
    username = getattr(user, "username", None)
    user_id = int(user.id)
    display_name = " ".join(
        x for x in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if x
    ) or "Telegram user"
    profile_url = f"https://t.me/{username}" if username else None
    summary = "\n".join(f"• {escape(gift_name(g))}" for g in gifts[:20])

    await db.save_profile({
        "user_id": user_id,
        "username": username,
        "display_name": display_name,
        "profile_url": profile_url,
        "gifts_count": len(gifts),
        "gift_summary": summary,
        "category": "unpriced",
    })
    existing = await db.get_profile(user_id)
    if existing and existing.get("message_id"):
        await db.log_scan(user_id, username, f"Повторная проверка: NFT-подарков {len(gifts)}")
        return

    safe_name = escape(display_name)
    username_line = f"🔗 Username: @{escape(username)}" if username else "🔗 Username: не указан"
    profile_line = f'👤 Продавец: <a href="{profile_url}">{safe_name}</a>' if profile_url else f"👤 Продавец: {safe_name}"
    card_text = (
        "🎁 <b>Найдены NFT-подарки в профиле</b>\n\n"
        f"{profile_line}\n"
        f"{username_line}\n"
        f"🆔 Telegram ID: <code>{user_id}</code>\n"
        f"🧩 NFT-подарков: <b>{len(gifts)}</b>\n\n"
        f"<b>Подарки:</b>\n{summary}\n\n"
        "💎 Оценка цены: пока не рассчитана"
    )
    sent = await bot.send_message(
        DESTINATION_CHAT_ID,
        card_text,
        reply_markup=profile_keyboard(user_id, username),
        disable_web_page_preview=True,
    )
    await db.attach_message(user_id, DESTINATION_CHAT_ID, sent.message_id)
    await db.log_scan(user_id, username, f"Опубликовано; NFT-подарков: {len(gifts)}")


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "<b>NFT Profile Radar</b>\n\n"
        "Бот публикует найденные NFT-подарки в профилях Telegram.\n"
        "Сканирование возможно только по доступным источникам пользователей, а не по всем аккаунтам Telegram."
    )


@dp.message(Command("scan"))
async def scan_command(message: Message):
    if scanner is None:
        await message.answer("❌ Сканер не подключён.\n\nПричина: " + escape(scanner_error))
        return
    await message.answer("🔎 Запускаю проверку доступных профилей…")
    count = 0
    async for user, gifts in scanner.scan_once():
        await publish_profile(user, gifts)
        count += 1
    await message.answer(f"Готово. Найдено профилей с NFT-подарками: {count}.")


@dp.message(Command("topics"))
async def topics_command(message: Message):
    try:
        created = await ensure_forum_topics()
        saved = await db.get_forum_topics()
        lines = [f"• {title}: {saved.get(key, 'нет')}" for key, title in CATEGORIES]
        status = "Созданы темы: " + ", ".join(created) if created else "Все пять тем уже созданы."
        await message.answer(status + "\n\n" + "\n".join(lines))
    except Exception as exc:
        log.exception("Forum topic setup failed")
        await message.answer(
            "Не удалось создать темы. Проверьте права администратора и право «Управление темами».\n"
            "Причина: " + escape(str(exc))
        )


@dp.message(Command("status"))
async def status_command(message: Message):
    if scanner is not None:
        await message.answer(f"🟢 Сканер подключён.\nИнтервал: {SCAN_INTERVAL} сек.")
    else:
        await message.answer("🔴 Сканер не подключён.\nПричина: " + escape(scanner_error))


@dp.callback_query(F.data.startswith("reserve:"))
async def reserve_callback(call: CallbackQuery):
    user_id = int(call.data.split(":", 1)[1])
    who = call.from_user.username or str(call.from_user.id)
    ok = await db.reserve_profile(user_id, call.from_user.id, who)
    if not ok:
        profile = await db.get_profile(user_id)
        reserved = profile.get("reserved_username") if profile else None
        suffix = f": @{escape(reserved)}" if reserved and reserved.isalnum() else (f": {escape(reserved)}" if reserved else "")
        await call.answer("Уже занято" + suffix, show_alert=True)
        return

    profile = await db.get_profile(user_id)
    await db.log_scan(
        user_id,
        profile.get("username") if profile else None,
        f"Занято пользователем {who}; Telegram ID {call.from_user.id}",
    )
    safe_who = escape("@" + who if call.from_user.username else who)
    original = call.message.text or ""
    await call.message.edit_text(
        original + f"\n\n🔒 <b>Занял:</b> {safe_who}",
        reply_markup=profile_keyboard(user_id, profile.get("username") if profile else None),
        disable_web_page_preview=True,
    )
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
    reserved_text = f"да, {escape(reserved)}" if reserved else "нет"
    log_lines = [
        f"• {escape(str(x.get('scanned_at', '')))} — {escape(x.get('result') or 'Событие')}"
        for x in entries[:10]
    ]
    text = (
        f"📋 <b>Лог профиля</b>\n\n"
        f"ID: <code>{user_id}</code>\n"
        f"Username: @{escape(profile.get('username') or 'нет')}\n"
        f"NFT-подарков: {profile.get('gifts_count', 0)}\n"
        f"Занято: {reserved_text}\n\n"
        f"<b>Подарки:</b>\n{escape(profile.get('gift_summary') or 'нет данных')}\n\n"
        f"<b>Последние события:</b>\n" + ("\n".join(log_lines) if log_lines else "Пока нет событий")
    )
    await call.message.answer(text)
    await call.answer()


@dp.callback_query(F.data.startswith("no_contact:"))
async def no_contact_callback(call: CallbackQuery):
    await call.answer(
        "У пользователя нет публичного username. Открыть личный чат по ID может быть невозможно.",
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
                log.info("Profile scanner connected")
            async for user, gifts in scanner.scan_once():
                await publish_profile(user, gifts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            scanner_error = str(exc)
            log.exception("Scanner connection/scan failed")
            if scanner is not None:
                try:
                    await scanner.stop()
                except Exception:
                    log.exception("Scanner disconnect failed")
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
        log.exception("Forum topic creation failed; check forum mode and bot permissions")
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
