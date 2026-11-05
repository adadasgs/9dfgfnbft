import aiosqlite
from datetime import datetime, timezone

DB_PATH = "radar.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            profile_url TEXT,
            gifts_count INTEGER NOT NULL DEFAULT 0,
            gift_summary TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'unpriced',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            chat_id INTEGER,
            message_id INTEGER,
            reserved_by INTEGER,
            reserved_username TEXT,
            reserved_at TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            result TEXT,
            scanned_at TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS forum_topics (
            category TEXT PRIMARY KEY,
            thread_id INTEGER NOT NULL
        )""")
        await db.commit()

async def get_profile(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def save_profile(data):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""INSERT INTO profiles
            (user_id, username, display_name, profile_url, gifts_count, gift_summary, category, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username, display_name=excluded.display_name,
              profile_url=excluded.profile_url, gifts_count=excluded.gifts_count,
              gift_summary=excluded.gift_summary, category=excluded.category, last_seen=excluded.last_seen""",
            (data['user_id'], data.get('username'), data.get('display_name'), data.get('profile_url'),
             data['gifts_count'], data['gift_summary'], data.get('category','unpriced'), now, now))
        await db.commit()

async def attach_message(user_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE profiles SET chat_id=?, message_id=? WHERE user_id=?", (chat_id, message_id, user_id))
        await db.commit()

async def reserve_profile(user_id, by_id, by_username):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("UPDATE profiles SET reserved_by=?, reserved_username=?, reserved_at=? WHERE user_id=? AND reserved_by IS NULL", (by_id, by_username, now, user_id))
        await db.commit()
        return cur.rowcount == 1

async def release_profile(user_id, by_id, is_admin=False):
    async with aiosqlite.connect(DB_PATH) as db:
        if is_admin:
            cur = await db.execute("UPDATE profiles SET reserved_by=NULL, reserved_username=NULL, reserved_at=NULL WHERE user_id=?", (user_id,))
        else:
            cur = await db.execute("UPDATE profiles SET reserved_by=NULL, reserved_username=NULL, reserved_at=NULL WHERE user_id=? AND reserved_by=?", (user_id, by_id))
        await db.commit()
        return cur.rowcount == 1

async def log_scan(user_id, username, result):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO scan_log(user_id, username, result, scanned_at) VALUES(?,?,?,?)",
                         (user_id, username, result, datetime.now(timezone.utc).isoformat()))
        await db.commit()

async def get_log(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM scan_log WHERE user_id=? ORDER BY id DESC LIMIT 15", (user_id,))
        return [dict(x) for x in await cur.fetchall()]


async def get_forum_topics():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT category, thread_id FROM forum_topics")
        return {row[0]: row[1] for row in await cur.fetchall()}

async def save_forum_topic(category, thread_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO forum_topics(category, thread_id) VALUES(?, ?) "
            "ON CONFLICT(category) DO UPDATE SET thread_id=excluded.thread_id",
            (category, thread_id)
        )
        await db.commit()
