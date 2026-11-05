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
    "GiftChangesIDs,gift_newstg,giftroad,indigoGif,brankasgift,"
    "project_crystal,nexikj,DosikGIFTS,agentdurovaton,getSendGifts,reystqq,"
    "chaoblog,gifttradeservise,projectx_nft,cryptoblog,hikoded,whisper_in_the_night,"
    "cryptaad,benzin,CreepyGifts,alishka_gifts,snyalcepik,semenchuk,groza,"
    "loxssy52,ohuenko,PodarokDurova,bape,vices,borov_club,lalyounft,bafet,"
    "DuckLodge,nonstopiiiikk,durovgoyda,mr_tony,digest,roxtalk,zorgannft,"
    "dadyacrypto,nft13onton,directorpodarkov,"
    "GiftTradeee,TonnelGift,mrktnotification,giftsmarket_starsgram,"
    "GiftPawn,nft_gifts_chat,vseznayka_otc"
)
CHANNELS = [
    x.strip().lstrip("@")
    for x in os.getenv("SCAN_CHANNELS", DEFAULT_CHANNELS).split(",")
    if x.strip()
]
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

    async def _scan_chatcrawler(self):
        """Read public Telegram-market listings and resolve only the linked seller profile username."""
        page = await self._get("https://chatcrawler.app/c/gifts/listings")
        if not page:
            self.last_stats["chatcrawler_unavailable"] = 1
            return

        self.last_stats["chatcrawler_listings_seen"] = 0
        self.last_stats["chatcrawler_profiles_checked"] = 0
        self.last_stats["chatcrawler_with_username"] = 0
        self.last_stats["chatcrawler_without_username"] = 0

        # A seller link points to a ChatCrawler person page. The preceding lot link
        # belongs to that listing card; no arbitrary @mentions are treated as sellers.
        seller_links = list(re.finditer(
            r'<a\b[^>]*href=["\'](?:https://chatcrawler\.app)?(/c/gifts/people/([0-9]+))[^"\']*["\'][^>]*>(.*?)</a>',
            page, re.I | re.S
        ))
        username_cache = {}
        previous_end = 0
        emitted = 0

        for seller_match in seller_links:
            segment = page[previous_end:seller_match.start()]
            previous_end = seller_match.end()
            lot_links = list(re.finditer(
                r'<a\b[^>]*href=["\'](?:https://chatcrawler\.app)?(/c/nft/[^"\']+)["\'][^>]*>(.*?)</a>',
                segment, re.I | re.S
            ))
            if not lot_links:
                continue
            lot_match = lot_links[-1]
            lot_path = html.unescape(lot_match.group(1))
            lot_title = re.sub(r"<[^>]+>", " ", lot_match.group(2))
            lot_title = re.sub(r"\s+", " ", html.unescape(lot_title)).strip()
            if not lot_title:
                lot_title = lot_path.rsplit("/", 1)[-1].replace("-", " ")

            self.last_stats["chatcrawler_listings_seen"] += 1
            seller_id = seller_match.group(2)
            if seller_id not in username_cache:
                try:
                    profile_page = await self._get(
                        f"https://chatcrawler.app/c/gifts/people/{seller_id}"
                    )
                    profile_text = re.sub(r"<[^>]+>", " ", profile_page)
                    profile_text = re.sub(r"\s+", " ", html.unescape(profile_text))
                    # Only accept the username explicitly shown in the profile's Username field.
                    found = re.search(
                        r"\bUsername\s*@([A-Za-z][A-Za-z0-9_]{4,31})\b",
                        profile_text, re.I
                    )
                    username_cache[seller_id] = found.group(1) if found else None
                    self.last_stats["chatcrawler_profiles_checked"] += 1
                except Exception:
                    log.exception("ChatCrawler seller profile lookup failed: %s", seller_id)
                    username_cache[seller_id] = None

            username = username_cache[seller_id]
            if not username:
                self.last_stats["chatcrawler_without_username"] += 1
                continue

            self.last_stats["chatcrawler_with_username"] += 1
            emitted += 1
            listing_url = "https://chatcrawler.app" + lot_path
            digest = hashlib.sha256(
                f"chatcrawler/{seller_id}/{lot_path}".encode()
            ).hexdigest()[:15]
            profile = SimpleNamespace(
                id=-(int(digest, 16) or 1),
                username=username,
                first_name=f"Продавец @{username}",
                last_name="",
                listing_url=listing_url,
                price_ton=None,
                category="unpriced",
                telegram_id=int(seller_id) if seller_id.isdigit() else None,
                source="ChatCrawler · публичный маркет",
            )
            gift = SimpleNamespace(title=lot_title, address=None, url=listing_url)
            yield profile, [gift]
            if emitted >= 30:
                break

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
            "ignored_high_rating": 0,
            "with_gram_price": 0,
            "with_username_and_price": 0,
            "chatcrawler_unavailable": 0,
        }
        try:
            async for profile, gifts in self._scan_chatcrawler():
                yield profile, gifts
        except asyncio.CancelledError:
            raise
        except Exception:
            self.last_stats["chatcrawler_unavailable"] = 1
            log.exception("ChatCrawler listings scan failed")
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
                    # Accept Russian, English and Chinese gift listings.
                    if not self._has_supported_language(text):
                        self.last_stats["ignored_non_russian"] += 1
                        continue
                    # Exclude only explicitly stated ratings of 80–100; never guess ratings.
                    if self._has_high_rating(text):
                        self.last_stats["ignored_high_rating"] += 1
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
                        title="Публичное объявление о Telegram-подарке",
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
