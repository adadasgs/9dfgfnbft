import asyncio
import logging
import os
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

log = logging.getLogger("profile-radar")

class ProfileScanner:
    def __init__(self):
        api_id = int(os.getenv("API_ID", "0"))
        api_hash = os.getenv("API_HASH", "")
        session = os.getenv("SESSION_STRING", "")
        if not api_id or not api_hash or not session:
            raise RuntimeError("Set API_ID, API_HASH and SESSION_STRING in Render environment")
        self.client = TelegramClient(StringSession(session), api_id, api_hash)

    async def start(self):
        await self.client.start()

    async def stop(self):
        await self.client.disconnect()

    async def candidate_users(self):
        seen = set()
        source = os.getenv("SCAN_SOURCE_CHAT", "").strip()
        if source:
            try:
                async for user in self.client.iter_participants(source):
                    if getattr(user, "bot", False) or getattr(user, "deleted", False):
                        continue
                    if user.id not in seen:
                        seen.add(user.id)
                        yield user
            except Exception:
                log.exception("Cannot enumerate members of SCAN_SOURCE_CHAT")
        raw = os.getenv("SCAN_USERS", "")
        for item in [x.strip() for x in raw.split(",") if x.strip()]:
            try:
                user = await self.client.get_entity(item)
                if user.id not in seen and not getattr(user, "bot", False):
                    seen.add(user.id)
                    yield user
            except Exception:
                log.warning("Cannot resolve scan target: %s", item)

    async def get_profile_gifts(self, user):
        """Fetch gifts explicitly displayed/owned on a known Telegram peer profile."""
        peer = await self.client.get_input_entity(user)
        offset = ""
        found = []
        while True:
            result = await self.client(functions.payments.GetSavedStarGiftsRequest(
                peer=peer, offset=offset, limit=100
            ))
            for gift in getattr(result, "gifts", []):
                item = getattr(gift, "gift", None)
                # Only collectible NFT gifts, not ordinary unlimited Star Gifts.
                if isinstance(item, types.StarGiftUnique):
                    found.append(item)
            next_offset = getattr(result, "next_offset", None)
            if not next_offset:
                break
            offset = next_offset
            await asyncio.sleep(0.25)
        return found

    async def scan_once(self):
        async for user in self.candidate_users():
            try:
                gifts = await self.get_profile_gifts(user)
                if gifts:
                    yield user, gifts
            except Exception as exc:
                log.warning("Gift scan failed for user_id=%s: %s", user.id, exc)
            await asyncio.sleep(1.0)
