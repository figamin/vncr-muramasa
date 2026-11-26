"""
Async client for the jiten.moe API (Japanese reading stats for visual novels).

Requests to api.jiten.moe fail intermittently, so this client retries with
backoff on network errors and 5xx responses. In-process TTL caches keep
deck-id mappings (which essentially never change) longer than full deck info.

API contract reference:
    GET /api/media-deck/by-link-id/2/{vndb_id}   -> JSON array of deck IDs
    GET /api/media-deck/{deck_id}/detail         -> { data: {...} } or {...}

linkType=2 is VNDB. mediaType=7 is VN.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import aiohttp

from utils.pg_db import get_pg_db

logger = logging.getLogger(__name__)


@dataclass
class JitenInfo:
    """Subset of jiten media-deck data the bot needs for stats and banners."""
    deck_id: int
    character_count: int
    difficulty_raw: float
    cover_url: Optional[str] = None
    unique_kanji_count: int = 0
    dialogue_percentage: float = 0.0
    difficulty: Optional[int] = None
    word_count: int = 0
    unique_word_count: int = 0
    sentence_count: int = 0
    average_sentence_length: float = 0.0


class JitenClient:
    """Async context-manager client for jiten.moe (PostgreSQL first, HTTP API fallback)."""

    BASE_URL = "https://api.jiten.moe/api"

    DECK_ID_TTL_SECONDS = 24 * 60 * 60   # 24h
    NULL_DECK_ID_TTL_SECONDS = 60 * 60   # 1h: recheck "not on jiten" sooner
    DECK_INFO_TTL_SECONDS = 6 * 60 * 60  # 6h
    CACHE_MAX_ENTRIES = 5000

    RETRY_BACKOFF_SECONDS = (1, 3, 5)
    REQUEST_TIMEOUT_SECONDS = 30

    # Class-level caches so multiple JitenClient context entries share state
    # within the bot process (a fresh client every command would otherwise
    # defeat caching).
    _deck_id_cache: Dict[str, "tuple[Optional[int], float]"] = {}
    _deck_info_cache: Dict[int, "tuple[Optional[JitenInfo], float]"] = {}

    def __init__(self, client_name: str = "VNCRBot", client_version: str = "1.0"):
        self.client_name = client_name
        self.client_version = client_version
        self.session: Optional[aiohttp.ClientSession] = None
        self._entered = False

    async def __aenter__(self) -> "JitenClient":
        self._entered = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._entered = False
        if self.session:
            await self.session.close()
            self.session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if not self._entered and self.session is None:
            raise RuntimeError("JitenClient must be used as an async context manager")
        if self.session is None:
            self.session = aiohttp.ClientSession(
                headers={
                    "User-Agent": f"{self.client_name}/{self.client_version}",
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT_SECONDS),
            )
        return self.session

    async def _get_with_retry(self, url: str) -> Optional[aiohttp.ClientResponse]:
        """
        GET ``url`` with retries on network errors and 5xx responses.
        Returns the response on 2xx, None on 4xx (after first try), raises on
        sustained failure after all retries.
        """
        session = self._ensure_session()

        last_exc: Optional[BaseException] = None
        for attempt, backoff in enumerate((0,) + self.RETRY_BACKOFF_SECONDS):
            if backoff:
                await asyncio.sleep(backoff)
            try:
                resp = await session.get(url)
                if resp.status >= 500:
                    logger.warning(
                        "jiten %s -> %d (attempt %d/%d)",
                        url, resp.status, attempt + 1, len(self.RETRY_BACKOFF_SECONDS) + 1,
                    )
                    resp.release()
                    continue
                return resp
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                logger.warning(
                    "jiten %s network error (attempt %d/%d): %s",
                    url, attempt + 1, len(self.RETRY_BACKOFF_SECONDS) + 1, exc,
                )
                continue

        if last_exc:
            logger.error("jiten %s exhausted retries: %s", url, last_exc)
        else:
            logger.error("jiten %s exhausted retries with persistent 5xx", url)
        return None

    @classmethod
    def _evict(cls, cache: dict, ttl_for_value):
        """Evict expired entries; if still over cap, drop oldest by timestamp.

        Bounded so the cache can never grow unboundedly even when every
        entry is fresh; without this fallback, sustained traffic on
        unique IDs would let the cache grow forever.
        """
        if len(cache) <= cls.CACHE_MAX_ENTRIES:
            return
        now = time.time()
        expired = [k for k, (val, ts) in cache.items() if now - ts > ttl_for_value(val)]
        for k in expired:
            del cache[k]
        if len(cache) > cls.CACHE_MAX_ENTRIES:
            sorted_items = sorted(cache.items(), key=lambda kv: kv[1][1])
            excess = len(cache) - cls.CACHE_MAX_ENTRIES
            for k, _ in sorted_items[:excess]:
                del cache[k]

    @classmethod
    def _evict_expired(cls):
        """Eviction pass for both caches when they grow too large."""
        cls._evict(
            cls._deck_id_cache,
            lambda deck_id: cls.DECK_ID_TTL_SECONDS if deck_id is not None else cls.NULL_DECK_ID_TTL_SECONDS,
        )
        cls._evict(
            cls._deck_info_cache,
            lambda _info: cls.DECK_INFO_TTL_SECONDS,
        )

    async def resolve_deck_id(self, vndb_id: str) -> Optional[int]:
        """
        Map a VNDB id (e.g. "v17") to its jiten media-deck id.
        Returns ``None`` if the VN isn't on jiten.
        """
        cached = self._deck_id_cache.get(vndb_id)
        if cached is not None:
            deck_id, ts = cached
            ttl = self.DECK_ID_TTL_SECONDS if deck_id is not None else self.NULL_DECK_ID_TTL_SECONDS
            if time.time() - ts < ttl:
                return deck_id

        pg = get_pg_db()
        if pg.is_configured:
            try:
                pg_row = await pg.get_jiten_by_vndb_id(vndb_id)
                if pg_row is not None and pg_row.get("deck_id") is not None:
                    deck_id = int(pg_row["deck_id"])
                    self._deck_id_cache[vndb_id] = (deck_id, time.time())
                    self._evict_expired()
                    return deck_id
            except Exception as exc:
                logger.warning("PG jiten resolve failed for %s: %s", vndb_id, exc)

        url = f"{self.BASE_URL}/media-deck/by-link-id/2/{vndb_id}"
        resp = await self._get_with_retry(url)
        if resp is None:
            return None

        try:
            if resp.status == 404 or 400 <= resp.status < 500:
                deck_id: Optional[int] = None
            elif resp.status == 200:
                ids = await resp.json()
                deck_id = ids[0] if isinstance(ids, list) and ids else None
            else:
                logger.warning("jiten resolve %s unexpected status %d", vndb_id, resp.status)
                return None
        finally:
            resp.release()

        self._deck_id_cache[vndb_id] = (deck_id, time.time())
        self._evict_expired()
        return deck_id

    async def get_deck_detail(self, deck_id: int) -> Optional[Dict[str, Any]]:
        """Raw deck-detail payload, with the ``{ data: ... }`` wrapper unwrapped."""
        url = f"{self.BASE_URL}/media-deck/{deck_id}/detail"
        resp = await self._get_with_retry(url)
        if resp is None:
            return None
        try:
            if resp.status != 200:
                return None
            payload = await resp.json()
        finally:
            resp.release()

        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
        return payload if isinstance(payload, dict) else None

    async def get_by_vndb_id(self, vndb_id: str, *, need_cover: bool = False) -> Optional[JitenInfo]:
        """
        Convenience: VNDB id -> :class:`JitenInfo` (or ``None``).

        Checks the PostgreSQL ``jiten`` database first; falls back to the
        jiten.moe HTTP API when PG is disabled/missing the entry or when
        ``need_cover=True`` and PG has no cached ``cover_url``.
        """
        pg = get_pg_db()
        if pg.is_configured:
            try:
                pg_row = await pg.get_jiten_by_vndb_id(vndb_id)
                if pg_row is not None and pg_row.get("deck_id") is not None:
                    deck_id = int(pg_row["deck_id"])
                    cover_url = pg_row.get("cover_url")
                    if need_cover and not cover_url:
                        try:
                            detail = await self.get_deck_detail(deck_id)
                            if detail:
                                main_deck = detail.get("mainDeck") if isinstance(detail.get("mainDeck"), dict) else detail
                                cover_url = main_deck.get("coverName") or None
                        except Exception as exc:
                            logger.debug("Optional jiten cover fetch failed for %s: %s", vndb_id, exc)
                    info = JitenInfo(
                        deck_id=deck_id,
                        character_count=int(pg_row.get("character_count") or 0),
                        difficulty_raw=float(pg_row.get("difficulty_raw") or 0.0),
                        cover_url=cover_url,
                        unique_kanji_count=int(pg_row.get("unique_kanji_count") or 0),
                        dialogue_percentage=float(pg_row.get("dialogue_percentage") or 0.0),
                        difficulty=pg_row.get("difficulty"),
                        word_count=int(pg_row.get("word_count") or 0),
                        unique_word_count=int(pg_row.get("unique_word_count") or 0),
                        sentence_count=int(pg_row.get("sentence_count") or 0),
                        average_sentence_length=float(pg_row.get("average_sentence_length") or 0.0),
                    )
                    self._deck_id_cache[vndb_id] = (deck_id, time.time())
                    self._deck_info_cache[deck_id] = (info, time.time())
                    self._evict_expired()
                    return info
            except Exception as exc:
                logger.warning("PG jiten lookup failed for %s: %s", vndb_id, exc)

        deck_id = await self.resolve_deck_id(vndb_id)
        if deck_id is None:
            return None

        cached = self._deck_info_cache.get(deck_id)
        if cached is not None:
            info, ts = cached
            if time.time() - ts < self.DECK_INFO_TTL_SECONDS:
                return info

        detail = await self.get_deck_detail(deck_id)
        if detail is None:
            return None

        # The /detail payload wraps fields under {parentDeck, mainDeck, subDecks}.
        # mainDeck holds the per-VN counts; fall back to top-level for safety
        # in case the schema changes.
        main_deck = detail.get("mainDeck") if isinstance(detail.get("mainDeck"), dict) else detail

        try:
            character_count = int(main_deck.get("characterCount", 0) or 0)
        except (TypeError, ValueError):
            character_count = 0
        try:
            difficulty_raw = float(main_deck.get("difficultyRaw", 0.0) or 0.0)
        except (TypeError, ValueError):
            difficulty_raw = 0.0
        try:
            unique_kanji_count = int(main_deck.get("uniqueKanjiCount", 0) or 0)
        except (TypeError, ValueError):
            unique_kanji_count = 0
        try:
            dialogue_percentage = float(main_deck.get("dialoguePercentage", 0.0) or 0.0)
        except (TypeError, ValueError):
            dialogue_percentage = 0.0
        cover_url = main_deck.get("coverName") or None

        info = JitenInfo(
            deck_id=deck_id,
            character_count=character_count,
            difficulty_raw=difficulty_raw,
            cover_url=cover_url,
            unique_kanji_count=unique_kanji_count,
            dialogue_percentage=dialogue_percentage,
        )
        self._deck_info_cache[deck_id] = (info, time.time())
        self._evict_expired()
        return info
