import asyncio
import hashlib
import logging
import os
from types import SimpleNamespace

import aiohttp

log = logging.getLogger("profile-radar")

API_BASE = os.getenv("TONCENTER_API_BASE", "https://toncenter.com/api/v3").rstrip("/")
SCAN_LIMIT = max(10, min(int(os.getenv("SCAN_LIMIT", "100")), 500))
REQUEST_INTERVAL = 1.15  # Anonymous TON Center requests are limited; stay below 1 RPS.


class ProfileScanner:
    """Scans publicly indexed TON NFT listings; no Telegram user session is required."""

    def __init__(self):
        self.session = None
        self._last_request = 0.0
        self.api_key = os.getenv("TONCENTER_API_KEY", "").strip()
        self.last_stats = {}

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=25, connect=10)
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        self.session = aiohttp.ClientSession(timeout=timeout, headers=headers)

    async def stop(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def _get(self, path, params=None):
        if self.session is None:
            raise RuntimeError("TON scanner is not started")
        wait = REQUEST_INTERVAL - (asyncio.get_running_loop().time() - self._last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        url = f"{API_BASE}{path}"
        for attempt in range(4):
            self._last_request = asyncio.get_running_loop().time()
            async with self.session.get(url, params=params or {}) as response:
                if response.status == 429:
                    delay = min(2 ** (attempt + 1), 15)
                    log.warning("TON Center rate limit; retrying in %s seconds", delay)
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                payload = await response.json(content_type=None)
                if isinstance(payload, dict) and payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                return payload
        raise RuntimeError("TON Center rate limit: retry limit reached")

    @staticmethod
    def _address(value):
        if isinstance(value, dict):
            return value.get("address") or value.get("user_friendly")
        return value if isinstance(value, str) else None

    @staticmethod
    def _metadata(item, payload):
        address = item.get("address")
        metadata = (payload.get("metadata") or {}).get(address) or {}
        token_info = metadata.get("token_info") or []
        token = token_info[0] if token_info else {}
        content = item.get("content") or {}
        collection = item.get("collection") or {}
        name = (
            token.get("name")
            or content.get("name")
            or collection.get("name")
            or "TON NFT"
        )
        collection_name = collection.get("name") or token.get("symbol") or ""
        return str(name), str(collection_name)

    @staticmethod
    def _price_ton(sale):
        # TON Center's full_price is expressed in nanotons (1 TON = 1e9 nanotons).
        raw = sale.get("full_price")
        details = sale.get("details") or {}
        if raw is None and isinstance(details, dict):
            raw = details.get("full_price") or details.get("price") or details.get("amount")
        if isinstance(raw, dict):
            raw = raw.get("amount") or raw.get("value")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return value / 1_000_000_000

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
    def _internal_id(address):
        # Internal database key only. It is NOT a Telegram user ID.
        number = int(hashlib.sha256(address.encode("utf-8")).hexdigest()[:15], 16)
        return -(number or 1)

    async def scan_once(self):
        payload = await self._get(
            "/nft/items",
            {
                "limit": SCAN_LIMIT,
                "offset": 0,
                "sort_by_last_transaction_lt": "true",
            },
        )
        items = payload.get("nft_items") or []
        self.last_stats = {"items": len(items), "on_sale": 0, "sales_checked": 0}
        log.info("TON index returned %s NFT items", len(items))

        for item in items:
            if not item.get("on_sale"):
                continue
            self.last_stats["on_sale"] += 1
            sale_address = self._address(item.get("sale_contract_address") or item.get("auction_contract_address"))
            nft_address = self._address(item.get("address"))
            if not sale_address or not nft_address:
                continue

            try:
                sale_payload = await self._get("/nft/sales", {"address": sale_address})
                self.last_stats["sales_checked"] += 1
                sales = sale_payload.get("nft_sales") or []
                if not sales:
                    continue
                sale = sales[0]
                price = self._price_ton(sale)
                if price is None:
                    log.info("Skipping listing with unknown price: %s", nft_address)
                    continue

                nft_item = sale.get("nft_item") or item
                owner = (
                    self._address(sale.get("nft_owner_address"))
                    or self._address(nft_item.get("owner_address"))
                    or self._address(nft_item.get("real_owner"))
                    or self._address(item.get("owner_address"))
                    or self._address(item.get("real_owner"))
                )
                if not owner:
                    continue

                title, collection_name = self._metadata(item, payload)
                gift = SimpleNamespace(
                    title=title,
                    address=nft_address,
                    url=f"https://tonviewer.com/{nft_address}",
                    collection_name=collection_name,
                )
                profile = SimpleNamespace(
                    id=self._internal_id(nft_address),
                    username=None,
                    first_name="Владелец TON-кошелька",
                    last_name="",
                    wallet_address=owner,
                    nft_address=nft_address,
                    listing_url=f"https://tonviewer.com/{nft_address}",
                    price_ton=price,
                    category=self._category(price),
                )
                yield profile, [gift]
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not process TON NFT listing %s", nft_address)
