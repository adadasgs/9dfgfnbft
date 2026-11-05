import asyncio
import hashlib
import html
import logging
import os
import re
from html.parser import HTMLParser
from types import SimpleNamespace

import aiohttp

log = logging.getLogger("profile-radar")

# Website-only sources. Telegram channel scraping is intentionally disabled.
WEBSITE_SOURCES = (
    ("GiftChecker", "https://giftcheckertg.com/"),
)
SCAN_LIMIT = max(5, min(int(os.getenv("SCAN_LIMIT", "50")), 100))
REQUEST_INTERVAL = 1.1

GIFT_WORDS = re.compile(
    r"(gift|gifts|for sale|selling|sell|подарок|подарки|продам|продаю|"
    r"nft|коллекционн|collectible|plush pepe|durov|dog|signet|"
    r"lol pop|jack in the box|чашка|шапка|медвед|игрушк|礼物|礼品|"
    r"出售|出|卖|收藏品|礼物出售)",
    re.I,
)
SELLER_CUE = re.compile(
    r"(продавец|продавца|пишите|писать|контакт|связь|seller|contact|"
    r"\bdm\b|по вопросам|купить у|обращаться|для покупки|сделка|"
    r"предложение|продам|продаю|продаётся|продается|message me|"
    r"dm me|contact me|seller's|wechat|联系|卖家|私聊|联系我|找我)",
    re.I,
)
SUPPORTED_LANGUAGE_RE = re.compile(r"[А-Яа-яЁё]{2,}|[A-Za-z]{3,}|[\u3400-\u9fff]{1,}")
HIGH_RATING_RE = re.compile(
    r"(?:rating|seller rating|рейтинг|信誉|信用分)\s*[:：=]?\s*(100|9[0-9]|8[0-9])"
    r"(?:\s*/\s*100|\s*%)?",
    re.I,
)
PRICE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:gram|grams|грам(?:м|ма|мов)?|гр\.?)(?!\w)",
    re.I,
)
USERNAME_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z][A-Za-z0-9_]{4,31})")
TME_RE = re.compile(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{4,31})(?:[/?#][^\s<]*)?", re.I)
RESERVED_HANDLES = {
    "share", "proxy", "s", "addstickers", "addemoji", "joinchat",
    "gift_satellite_bot", "ogmobot", "giftexplorebot",
}


class MessageParser(HTMLParser):
    """Extracts only Telegram message text and anchors, respecting nested tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.target_depth = None
        self.parts = []
        self.anchors = []
        self._anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "div" and "tgme_widget_message_text" in classes:
            self.target_depth = len(self.stack)
        if self.target_depth is not None:
            if tag == "a":
                self._anchor = {"href": attrs.get("href", ""), "text": []}
            elif tag == "br":
                self.parts.append(" ")
        self.stack.append(tag)

    def handle_data(self, data):
        if self.target_depth is not None:
            self.parts.append(data)
            if self._anchor is not None:
                self._anchor["text"].append(data)

    def handle_endtag(self, tag):
        if self.target_depth is not None and self._anchor is not None and tag == "a":
            self.anchors.append({
                "href": self._anchor["href"],
                "text": "".join(self._anchor["text"]).strip(),
            })
            self._anchor = None
        if self.target_depth is not None and tag == "div" and len(self.stack) - 1 == self.target_depth:
            self.target_depth = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == tag:
                del self.stack[i:]
                break


class ProfileScanner:
    """Scans public Telegram gift ads and emits only explicitly advertised contacts."""

    def __init__(self):
        self.session = None
        self.mtproto = None
        self.mtproto_error = ""
        self._last_request = 0.0
        self.last_stats = {}

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=20, connect=8)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; PublicGiftRadar/2.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        api_id = os.getenv("API_ID", "").strip()
        api_hash = os.getenv("API_HASH", "").strip()
        session_string = os.getenv("SESSION_STRING", "").strip()
        if api_id and api_hash and session_string:
            try:
                from telethon import TelegramClient
                from telethon.sessions import StringSession
                self.mtproto = TelegramClient(StringSession(session_string), int(api_id), api_hash)
                await self.mtproto.connect()
                if not await self.mtproto.is_user_authorized():
                    raise RuntimeError("SESSION_STRING не авторизован")
                log.info("Telegram resale profile parser connected")
            except Exception as exc:
                self.mtproto_error = str(exc)
                log.warning("Telegram resale parser unavailable: %s", exc)
                if self.mtproto:
                    try:
                        await self.mtproto.disconnect()
                    except Exception:
                        pass
                self.mtproto = None
        else:
            self.mtproto_error = "Нужны API_ID, API_HASH и SESSION_STRING для сканирования профилей"

    async def stop(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
        if self.mtproto:
            await self.mtproto.disconnect()
        self.mtproto = None

    async def _get(self, url):
        if self.session is None:
            raise RuntimeError("Public Telegram scanner is not started")
        delay = REQUEST_INTERVAL - (asyncio.get_running_loop().time() - self._last_request)
        if delay > 0:
            await asyncio.sleep(delay)
        self._last_request = asyncio.get_running_loop().time()
        async with self.session.get(url) as response:
            if response.status in (403, 404, 429):
                log.warning("Public preview unavailable: %s HTTP %s", url, response.status)
                return ""
            response.raise_for_status()
            return await response.text()

    @staticmethod
    def _blocks(page):
        starts = [
            m.start()
            for m in re.finditer(
                r'<div[^>]+class="[^"]*tgme_widget_message_wrap',
                page,
            )
        ]
        for i, start in enumerate(starts):
            block = page[starts[i]:starts[i + 1] if i + 1 < len(starts) else len(page)]
            post = re.search(r'data-post="([A-Za-z0-9_]+)/([0-9]+)"', block)
            if not post:
                continue
            parser = MessageParser()
            parser.feed(block)
            text = re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()
            if text:
                yield post.group(1), post.group(2), text, parser.anchors

    @staticmethod
    def _username(text, anchors, channel):
        # Require an explicit seller/contact cue in Russian, English or Chinese.
        contact_pattern = re.compile(
            r"(?:продавец|контакт|пишите|писать|связь|seller|contact|"
            r"message me|dm me|contact me|\\bdm\\b|по вопросам|обращаться|"
            r"для покупки|вопросы|联系我|卖家|联系|私聊|找我)"
            r"\s*(?:мне\s*)?(?:[:：=—-]\s*)?"
            r"(?:в\s+(?:личку|лс|личные сообщения)\s*)?"
            r"(?:me\s*)?@([A-Za-z][A-Za-z0-9_]{4,31})",
            re.I,
        )
        candidates = set(contact_pattern.findall(text))
        for anchor in anchors:
            label = anchor.get("text", "")
            href = anchor.get("href", "")
            if re.search(
                r"(написать продавцу|связаться с продавцом|контакт продавца|"
                r"seller contact|contact seller|message seller|联系卖家|卖家联系)",
                label,
                re.I,
            ):
                candidates.update(TME_RE.findall(href))
        candidates = {
            name for name in candidates
            if name.lower() != channel.lower()
            and name.lower() not in RESERVED_HANDLES
            and not name.lower().endswith("_bot")
        }
        return next(iter(candidates)) if len(candidates) == 1 else None

    @staticmethod
    def _has_supported_language(text):
        # Russian, English and Chinese are accepted; other scripts are not.
        return bool(SUPPORTED_LANGUAGE_RE.search(text))

    @staticmethod
    def _has_high_rating(text):
        match = HIGH_RATING_RE.search(text)
        return bool(match and int(match.group(1)) >= 80)

    @staticmethod
    def _price(text):
        match = PRICE_RE.search(text)
        if not match:
            return None
        try:
            price = float(match.group(1).replace(",", "."))
            return price if price > 0 else None
        except ValueError:
            return None

    @staticmethod
    def _category(price):
        if price < 5:
            return "under5"
        if price < 15:
            return "5_15"
        if price < 27.5:
            return "15_27_5"
        if price < 60:
            return "27_5_60"
        return "over60"

    @staticmethod
    def _internal_id(channel, post_id):
        # Local listing key only; never shown as a Telegram account ID.
        digest = hashlib.sha256(f"{channel}/{post_id}".encode()).hexdigest()[:15]
        return -(int(digest, 16) or 1)


    @staticmethod
    def _stars_price(value):
        if not value:
            return None
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is None:
            return None
        amount = getattr(value, "amount", None)
        nanos = getattr(value, "nanos", 0) or 0
        if amount is None:
            return None
        try:
            return float(amount) + float(nanos) / 1_000_000_000
        except (TypeError, ValueError):
            return None

    async def scan_mtproto_once(self):
        """Read Telegram's public Stars resale listings and resolve only user sellers."""
        if self.mtproto is None:
            return
        from telethon.tl import functions

        try:
            catalog = await self.mtproto(functions.payments.GetStarGiftsRequest(hash=0))
            for gift_type in getattr(catalog, "gifts", []):
                if not getattr(gift_type, "availability_resale", 0):
                    continue
                offset = ""
                for _page in range(3):
                    result = await self.mtproto(
                        functions.payments.GetResaleStarGiftsRequest(
                            stars_only=True,
                            gift_id=gift_type.id,
                            offset=offset,
                            limit=100,
                        )
                    )
                    users = {
                        item.id: item
                        for item in getattr(result, "users", [])
                        if getattr(item, "id", None) is not None
                    }
                    for listing in getattr(result, "gifts", []):
                        owner_peer = getattr(listing, "owner_id", None)
                        owner_id = getattr(owner_peer, "user_id", None)
                        if not owner_id:
                            continue  # Ignore channels and wallet-only owners.
                        seller = users.get(owner_id)
                        if seller is None:
                            try:
                                seller = await self.mtproto.get_entity(owner_peer)
                            except Exception:
                                continue
                        username = getattr(seller, "username", None)
                        if not username or getattr(seller, "bot", False):
                            continue
                        slug = getattr(listing, "slug", None)
                        if not slug:
                            continue
                        listing_url = f"https://t.me/nft/{slug}"
                        price = self._stars_price(getattr(listing, "resell_amount", None))
                        user = SimpleNamespace(
                            id=self._internal_id("telegram-stars-resale", slug),
                            username=username,
                            first_name=getattr(seller, "first_name", None),
                            last_name=getattr(seller, "last_name", None),
                            telegram_id=None,
                            price_ton=price,
                            price_currency="Stars",
                            source=None,
                            listing_url=listing_url,
                        )
                        gift_title = getattr(listing, "title", None) or getattr(gift_type, "title", None) or "Telegram Gift"
                        gift_num = getattr(listing, "num", None)
                        label = f"{gift_title} #{gift_num}" if gift_num is not None else gift_title
                        gift = SimpleNamespace(title=label, url=listing_url)
                        yield user, [gift]
                    offset = getattr(result, "next_offset", None) or ""
                    if not offset:
                        break
                    await asyncio.sleep(0.4)
                await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Telegram Stars resale profile scan failed: %s", exc)

    async def scan_once(self):
        """Read website pages only; never scan Telegram channels.

        A seller is accepted only when a listing page explicitly labels the
        username as its seller/contact. General @mentions and site support
        accounts are ignored.
        """
        self.last_stats = {
            "websites_checked": 0,
            "websites_unavailable": 0,
            "listing_pages_with_seller": 0,
            "profiles_with_verified_username": 0,
        }
        seen = set()
        seller_cue = re.compile(
            r"(?:seller|seller username|seller contact|contact seller|"
            r"продавец|контакт продавца|написать продавцу)\s*[:：=—-]?\s*"
            r"(?:<[^>]+>\s*)?(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{4,31})"
            r"|(?:seller|seller username|seller contact|contact seller|"
            r"продавец|контакт продавца|написать продавцу)\s*[:：=—-]?\s*"
            r"@([A-Za-z][A-Za-z0-9_]{4,31})",
            re.I,
        )
        gift_hint = re.compile(
            r"(gift|gifts|telegram gift|подарок|подарки|collectible|nft)",
            re.I,
        )
        # Primary new method: Telegram's own public resale feed, Stars only (no TON).
        async for user, gifts in self.scan_mtproto_once():
            yield user, gifts

        for source_name, base_url in WEBSITE_SOURCES:
            self.last_stats["websites_checked"] += 1
            try:
                page = await self._get(base_url)
                if not page:
                    self.last_stats["websites_unavailable"] += 1
                    continue
                # Parse only server-rendered listing fragments. If the site
                # requires a browser session/JS API, do not invent records.
                for match in seller_cue.finditer(page):
                    username = next((x for x in match.groups() if x), None)
                    if not username or username.lower().endswith("_bot"):
                        continue
                    left = page[max(0, match.start()-1800):match.start()]
                    right = page[match.end():match.end()+900]
                    context = html.unescape(re.sub(r"<[^>]+>", " ", left + " " + right))
                    context = re.sub(r"\s+", " ", context).strip()
                    if not gift_hint.search(context):
                        continue
                    listing_match = re.search(
                        r"""href=["']([^"']+)["']""",
                        page[max(0, match.start()-1200):match.start()],
                        re.I,
                    )
                    listing_url = base_url
                    if listing_match:
                        from urllib.parse import urljoin
                        listing_url = urljoin(base_url, html.unescape(listing_match.group(1)))
                    key = (source_name.lower(), username.lower(), listing_url)
                    if key in seen:
                        continue
                    seen.add(key)
                    internal_id = self._internal_id(source_name, listing_url)
                    user = SimpleNamespace(
                        id=internal_id,
                        username=username,
                        first_name=None,
                        last_name=None,
                        telegram_id=None,
                        price_ton=self._price(context),
                        source=None,
                        listing_url=listing_url,
                    )
                    gift = SimpleNamespace(title=context[:180] or "Telegram gift listing", url=listing_url)
                    self.last_stats["listing_pages_with_seller"] += 1
                    self.last_stats["profiles_with_verified_username"] += 1
                    yield user, [gift]
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_stats["websites_unavailable"] += 1
                log.warning("Website scan failed (%s): %s", source_name, exc)
