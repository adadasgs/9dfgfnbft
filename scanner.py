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

# Public Telegram gift-sale communities and announcement channels.
# Only public posts are read; no private groups, user sessions, wallets or TON APIs.
DEFAULT_CHANNELS = (
    "trade_gifts_chat,giftsatellite,podarkirar_novostya_prodatcy,NFTGiftNewz,"
    "GiftChangesUpdates,GiftChanges,Gift_Alerts,GiftNotification,GiftNFTs,"
    "GiftTradeee,podarkitgnft,GIFT_PODARKI,Gifted,TGcollectors,giftinsider,"
    "GiftsMage,GiftWhaleFeed,GiftChangesModels,GiftChangesAuctions,"
    "GiftChangesIDs,gift_newstg,giftroad,indigoGif,brankasgift"
)
CHANNELS = [
    x.strip().lstrip("@")
    for x in os.getenv("SCAN_CHANNELS", DEFAULT_CHANNELS).split(",")
    if x.strip()
]
SCAN_LIMIT = max(5, min(int(os.getenv("SCAN_LIMIT", "50")), 100))
REQUEST_INTERVAL = 1.1

GIFT_WORDS = re.compile(
    r"(gift|подарок|подарки|nft|коллекционн|collectible|plush pepe|"
    r"durov|dog|signet|lol pop|jack in the box|чашка|шапка|медвед|игрушк)",
    re.I,
)
SELLER_CUE = re.compile(
    r"(продавец|продавца|пишите|писать|контакт|связь|seller|contact|"
    r"\bdm\b|по вопросам|купить у|обращаться|для покупки|сделка|"
    r"предложение|продам|продаю|продаётся|продается)",
    re.I,
)
RUSSIAN_TEXT_RE = re.compile(
    r"[А-Яа-яЁё]{3,}|"
    r"(продам|продаю|продаётся|продается|куплю|покупка|продавец|"
    r"подарок|подарки|обмен|цена|рублей|руб\.?|связь|пишите)",
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

    async def stop(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

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
        if not SELLER_CUE.search(text):
            return None

        candidates = set()
        for cue in SELLER_CUE.finditer(text):
            # A username must be close to an explicit seller/contact cue.
            window = text[max(0, cue.start() - 110):min(len(text), cue.end() + 150)]
            candidates.update(USERNAME_RE.findall(window))
            candidates.update(TME_RE.findall(window))

        # Also accept an explicit @handle hyperlink when the surrounding post
        # clearly contains seller/contact wording.
        for anchor in anchors:
            label = anchor.get("text", "")
            href = anchor.get("href", "")
            # Only treat a link as a seller contact when its visible label is
            # an explicit contact action; ordinary gift/profile links are ignored.
            if re.search(r"(написать продавцу|связаться|контакт продавца|seller contact)", label, re.I):
                candidates.update(TME_RE.findall(href))

        candidates = {
            name for name in candidates
            if name.lower() != channel.lower()
            and name.lower() not in RESERVED_HANDLES
            and not name.lower().endswith("_bot")
        }
        return next(iter(candidates)) if len(candidates) == 1 else None

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

    async def scan_once(self):
        self.last_stats = {
            "channels_total": len(CHANNELS),
            "channels_checked": 0,
            "channels_unavailable": 0,
            "posts_checked": 0,
            "gift_posts": 0,
            "seller_contacts": 0,
            "ignored_without_seller": 0,
            "ignored_non_russian": 0,
            "with_gram_price": 0,
            "with_username_and_price": 0,
        }
        for channel in CHANNELS:
            try:
                page = await self._get(f"https://t.me/s/{channel}")
                if not page:
                    self.last_stats["channels_unavailable"] += 1
                    continue
                self.last_stats["channels_checked"] += 1
                for post_channel, post_id, text, anchors in self._blocks(page):
                    self.last_stats["posts_checked"] += 1
                    if not GIFT_WORDS.search(text):
                        continue
                    self.last_stats["gift_posts"] += 1
                    # Russian-first mode: ignore listings without Russian-language text.
                    if not RUSSIAN_TEXT_RE.search(text):
                        self.last_stats["ignored_non_russian"] += 1
                        continue
                    username = self._username(text, anchors, post_channel)
                    if username:
                        self.last_stats["seller_contacts"] += 1
                    else:
                        # Channel announcements without an identifiable seller are not seller profiles.
                        self.last_stats["ignored_without_seller"] += 1
                        continue
                    price = self._price(text)
                    if price is not None:
                        self.last_stats["with_gram_price"] += 1
                        if username:
                            self.last_stats["with_username_and_price"] += 1

                    post_url = f"https://t.me/{post_channel}/{post_id}"
                    gift = SimpleNamespace(
                        title=text[:500] or "Telegram gift",
                        address=None,
                        url=post_url,
                    )
                    profile = SimpleNamespace(
                        id=self._internal_id(post_channel, post_id),
                        username=username,
                        first_name=f"Продавец @{username}",
                        last_name="",
                        listing_url=post_url,
                        price_ton=price,  # legacy DB field; value is GRAM only
                        category=self._category(price) if price is not None else "unpriced",
                        telegram_id=None,
                        source=f"t.me/{post_channel}",
                    )
                    yield profile, [gift]
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_stats["channels_unavailable"] += 1
                log.exception("Could not scan public channel @%s", channel)
        log.info("Public Telegram scan stats: %s", self.last_stats)
