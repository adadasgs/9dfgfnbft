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
DEFAULT_CHANNELS = "podarkitgnft,GiftTradeee,brankasgift,giftwhalefeed,GiftNotification,Gift_Alerts,GiftChanges,GiftChangesUpdates,TGcollectors,giftinsider,Gifted,GiftsMage,GiftChangesIDs,GiftChangesModels,GiftChangesAuctions,GiftNFTs,GIFT_PODARKI"
CHANNELS = [x.strip().lstrip("@") for x in os.getenv("SCAN_CHANNELS", DEFAULT_CHANNELS).split(",") if x.strip()]
SCAN_LIMIT = max(5, min(int(os.getenv("SCAN_LIMIT", "30")), 100))
REQUEST_INTERVAL = 1.1
GIFT_WORDS = re.compile(r"(gift|подарок|подарки|nft|коллекционн|collectible|plush pepe|durov|dog|signet|lol pop|jack in the box|чашка|шапка|медвед|игрушк)", re.I)
SELLER_CUE = re.compile(r"(продавец|продавца|пишите|писать|контакт|связь|seller|contact|dm|telegram|телеграм|по вопросам|купить у|обращаться)", re.I)
PRICE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:gram|grams|грам(?:м|ма|мов)?|гр\.?)(?!\w)", re.I)
USERNAME_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z][A-Za-z0-9_]{4,31})")
TME_RE = re.compile(r"https?://t\.me/([A-Za-z][A-Za-z0-9_]{4,31})(?:\?[^\s<]*)?", re.I)


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.in_text = False

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "").split()
        if "tgme_widget_message_text" in classes:
            self.in_text = True
        elif self.in_text and tag == "br":
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag == "div" and self.in_text:
            self.in_text = False

    def handle_data(self, data):
        if self.in_text:
            self.parts.append(data)


class ProfileScanner:
    """Scans public Telegram post previews; only explicit seller contacts are emitted."""

    def __init__(self):
        self.session = None
        self._last_request = 0.0
        self.last_stats = {}

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=20, connect=8)
        self.session = aiohttp.ClientSession(timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (compatible; PublicGiftRadar/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        })

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
        starts = [m.start() for m in re.finditer(r'<div[^>]+class="[^"]*tgme_widget_message_wrap', page)]
        for i, start in enumerate(starts):
            block = page[start:starts[i + 1] if i + 1 < len(starts) else len(page)]
            post = re.search(r'data-post="([A-Za-z0-9_]+)/([0-9]+)"', block)
            body = re.search(r'<div[^>]+class="[^"]*tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
            if post and body:
                parser = TextExtractor()
                parser.feed(body.group(1))
                text = re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()
                yield post.group(1), post.group(2), text, body.group(1)

    @staticmethod
    def _username(text, fragment, channel):
        if not SELLER_CUE.search(text):
            return None
        names = set(USERNAME_RE.findall(text)) | set(TME_RE.findall(fragment))
        names = {n for n in names if n.lower() != channel.lower() and n.lower() not in {"share", "proxy"}}
        return next(iter(names)) if len(names) == 1 else None

    @staticmethod
    def _price(text):
        m = PRICE_RE.search(text)
        if not m:
            return None
        try:
            p = float(m.group(1).replace(",", "."))
            return p if p > 0 else None
        except ValueError:
            return None

    @staticmethod
    def _category(price):
        if price < 5: return "under5"
        if price < 15: return "5_15"
        if price < 27.5: return "15_27_5"
        if price < 60: return "27_5_60"
        return "over60"

    @staticmethod
    def _internal_id(channel, post_id):
        # Local database key, never a Telegram account ID.
        digest = hashlib.sha256(f"{channel}/{post_id}".encode()).hexdigest()[:15]
        return -(int(digest, 16) or 1)

    async def scan_once(self):
        self.last_stats = {"channels_checked": 0, "posts_checked": 0, "gift_posts": 0, "with_username": 0}
        for channel in CHANNELS:
            try:
                page = await self._get(f"https://t.me/s/{channel}")
                self.last_stats["channels_checked"] += 1
                for post_channel, post_id, text, fragment in self._blocks(page):
                    self.last_stats["posts_checked"] += 1
                    if not GIFT_WORDS.search(text):
                        continue
                    self.last_stats["gift_posts"] += 1
                    username = self._username(text, fragment, post_channel)
                    price = self._price(text)
                    if not username or price is None:
                        continue
                    self.last_stats["with_username"] += 1
                    url = f"https://t.me/{post_channel}/{post_id}"
                    gift = SimpleNamespace(title=text[:160] or "Telegram gift", address=None, url=url)
                    profile = SimpleNamespace(
                        id=self._internal_id(post_channel, post_id),
                        username=username,
                        first_name=f"Продавец @{username}",
                        last_name="",
                        wallet_address=None,
                        nft_address=None,
                        listing_url=url,
                        price_ton=price,
                        category=self._category(price),
                        telegram_id=None,
                        source=f"t.me/{post_channel}",
                    )
                    yield profile, [gift]
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not scan public channel @%s", channel)
        log.info("Public Telegram scan stats: %s", self.last_stats)
