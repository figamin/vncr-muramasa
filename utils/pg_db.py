"""Read-only async PostgreSQL client for the VNCR multi-database architecture.

Connects to the five PostgreSQL databases powering the VNCR site:
- ``vndb_stats``: enriched VN metadata, tags, producers, cover blacklist, external ratings
- ``vndb``: raw VNDB dump (used for length_votes / c_lengthnum and localized titles)
- ``jiten``: jiten.moe difficulty, character/kanji counts, and deck cache
- ``egs``: EroGamescape gamelist, brands, furigana, and non-spoiler POVs
- ``bangumi``: Bangumi subjects, scores, ranks, score distributions, and tags

Every query degrades gracefully (returning ``None`` / ``[]`` / ``{}``) if
``asyncpg`` is not installed, PostgreSQL is disabled via ``PG_ENABLED=0``, or
any database host is unreachable, so callers can fall back to HTTP APIs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import weakref
from typing import Any, Dict, List, Optional

try:
    import asyncpg  # type: ignore
except ImportError:  # pragma: no cover - optional runtime dependency
    asyncpg = None

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

from utils.helpers import (
    bangumi_subject_url,
    egs_game_url,
    extract_bangumi_id,
    extract_egs_id,
    extract_vndb_id,
    jiten_deck_url,
)

logger = logging.getLogger(__name__)

_VNDB_ID_RE = re.compile(r"^v?(\d+)$", re.IGNORECASE)
_HIRAGANA_TO_KATAKANA = str.maketrans(
    {chr(code): chr(code + 0x60) for code in range(0x3041, 0x3097)}
)
_CJK_OR_KANA_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")

_DB_ENV_MAP = {
    "vndb": ("PG_DB_VNDB", "vndb"),
    "egs": ("PG_DB_EGS", "egs"),
    "bangumi": ("PG_DB_BANGUMI", "bangumi"),
    "jiten": ("PG_DB_JITEN", "jiten"),
    "vndb_stats": ("PG_DB_VNDB_STATS", "vndb_stats"),
}


def _to_katakana(text: str) -> str:
    """Convert hiragana characters in *text* to katakana for EGS furigana search."""
    return text.translate(_HIRAGANA_TO_KATAKANA)


def _escape_like(text: str) -> str:
    """Escape LIKE/ILIKE wildcard characters."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_jsonb(value: Any) -> Any:
    """Decode jsonb fields whether asyncpg returns str or already-parsed Python objects."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def _synthesize_titles(
    title: Optional[str],
    title_jp: Optional[str],
    title_romaji: Optional[str],
) -> List[Dict[str, Optional[str]]]:
    """Build a VNDB-API-compatible ``titles`` list from vndb_stats columns."""
    titles: List[Dict[str, Optional[str]]] = []
    if title:
        titles.append({"lang": "en", "title": title, "latin": None})
    if title_jp or title_romaji:
        titles.append(
            {
                "lang": "ja",
                "title": title_jp or title_romaji,
                "latin": title_romaji,
            }
        )
    return titles


def _format_external_ratings(row: Any) -> Optional[Dict[str, Any]]:
    """Extract normalized EGS + Bangumi + consensus payload from a joined row."""
    if row is None:
        return None
    egs_id = row.get("primary_egs_id")
    bangumi_id = row.get("primary_bangumi_id")
    consensus = row.get("consensus_score")
    if egs_id is None and bangumi_id is None and consensus is None:
        return None
    return {
        "egs_id": int(egs_id) if egs_id is not None else None,
        "egs_url": egs_game_url(int(egs_id)) if egs_id is not None else None,
        "egs_median": row.get("egs_median"),
        "egs_average": row.get("egs_average"),
        "egs_stdev": row.get("egs_stdev"),
        "egs_count": row.get("egs_count"),
        "egs_playtime_median": row.get("egs_playtime_median"),
        "egs_fun_time_median": row.get("egs_fun_time_median"),
        "egs_genre": row.get("egs_genre"),
        "egs_has_trial": row.get("egs_has_trial"),
        "bangumi_id": int(bangumi_id) if bangumi_id is not None else None,
        "bangumi_url": bangumi_subject_url(int(bangumi_id)) if bangumi_id is not None else None,
        "bangumi_score": float(row["bangumi_score"]) if row.get("bangumi_score") is not None else None,
        "bangumi_rank": row.get("bangumi_rank"),
        "bangumi_votes": row.get("bangumi_votes"),
        "bangumi_name_cn": row.get("bangumi_name_cn") or None,
        "consensus_score": float(consensus) if consensus is not None else None,
    }


def _format_jiten_row(row: Any, cover_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Normalize a ``vn_difficulty`` row into a Jiten summary dict."""
    if not row:
        return None
    deck_id = row.get("jiten_deck_id")
    if deck_id is None:
        return None
    return {
        "deck_id": int(deck_id),
        "url": jiten_deck_url(int(deck_id)),
        "difficulty": row.get("difficulty"),
        "difficulty_raw": float(row["difficulty_raw"]) if row.get("difficulty_raw") is not None else 0.0,
        "character_count": int(row.get("character_count") or 0),
        "word_count": int(row.get("word_count") or 0),
        "unique_word_count": int(row.get("unique_word_count") or 0),
        "unique_kanji_count": int(row.get("unique_kanji_count") or 0),
        "sentence_count": int(row.get("sentence_count") or 0),
        "average_sentence_length": float(row["average_sentence_length"])
        if row.get("average_sentence_length") is not None
        else 0.0,
        "dialogue_percentage": float(row["dialogue_percentage"])
        if row.get("dialogue_percentage") is not None
        else 0.0,
        "cover_url": cover_url,
    }


class VNCRMultiDB:
    """Async multi-database PostgreSQL reader for VNCR's five databases."""

    COOLDOWN_SECONDS = 30.0

    def __init__(self) -> None:
        # Pools and creation locks are keyed per running event loop because
        # web_console.py and sub-bots can execute coroutines on separate loops.
        self._pools: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[str, Any]]" = (
            weakref.WeakKeyDictionary()
        )
        self._locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
            weakref.WeakKeyDictionary()
        )
        self._backoff_until: Dict[str, float] = {}
        self._dotenv_loaded = False

    def _ensure_env_loaded(self) -> None:
        if not self._dotenv_loaded and not os.getenv("PG_HOST") and load_dotenv is not None:
            load_dotenv()
            self._dotenv_loaded = True

    @property
    def is_configured(self) -> bool:
        """True when asyncpg is available and PG_ENABLED + host/user are configured."""
        if asyncpg is None:
            return False
        self._ensure_env_loaded()
        enabled = os.getenv("PG_ENABLED", "1").strip().lower()
        if enabled in ("0", "false", "no", "off"):
            return False
        return bool(os.getenv("PG_HOST") and os.getenv("PG_USER"))

    def _db_name(self, db_key: str) -> str:
        env_key, default_name = _DB_ENV_MAP[db_key]
        return os.getenv(env_key, default_name)

    def _get_loop_lock(self, loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
        lock = self._locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[loop] = lock
        return lock

    async def _get_pool(self, db_key: str) -> Optional[Any]:
        """Get or create a per-loop asyncpg pool for *db_key*, respecting backoff."""
        if not self.is_configured or db_key not in _DB_ENV_MAP:
            return None

        now = time.monotonic()
        if now < self._backoff_until.get(db_key, 0.0):
            return None

        loop = asyncio.get_running_loop()
        loop_pools = self._pools.get(loop)
        if loop_pools is not None and db_key in loop_pools:
            return loop_pools[db_key]

        lock = self._get_loop_lock(loop)
        async with lock:
            loop_pools = self._pools.setdefault(loop, {})
            if db_key in loop_pools:
                return loop_pools[db_key]

            host = os.getenv("PG_HOST", "").strip()
            port = int(os.getenv("PG_PORT", "5432") or 5432)
            user = os.getenv("PG_USER", "").strip()
            password = os.getenv("PG_PASSWORD", "")
            database = self._db_name(db_key)

            try:
                pool = await asyncpg.create_pool(
                    host=host,
                    port=port,
                    user=user,
                    password=password,
                    database=database,
                    min_size=0,
                    max_size=5,
                    timeout=5.0,
                    command_timeout=10.0,
                )
                loop_pools[db_key] = pool
                return pool
            except Exception as exc:
                self._backoff_until[db_key] = time.monotonic() + self.COOLDOWN_SECONDS
                logger.warning(
                    "PostgreSQL connection failed for db=%s (%s:%s/%s): %s",
                    db_key,
                    host,
                    port,
                    database,
                    exc,
                )
                return None

    async def _fetch(self, db_key: str, query: str, *args: Any) -> Optional[List[Any]]:
        pool = await self._get_pool(db_key)
        if pool is None:
            return None
        try:
            async with pool.acquire(timeout=5.0) as conn:
                return await conn.fetch(query, *args)
        except Exception as exc:
            self._backoff_until[db_key] = time.monotonic() + self.COOLDOWN_SECONDS
            logger.warning("PostgreSQL query failed on %s: %s", db_key, exc)
            return None

    async def _fetchrow(self, db_key: str, query: str, *args: Any) -> Optional[Any]:
        pool = await self._get_pool(db_key)
        if pool is None:
            return None
        try:
            async with pool.acquire(timeout=5.0) as conn:
                return await conn.fetchrow(query, *args)
        except Exception as exc:
            self._backoff_until[db_key] = time.monotonic() + self.COOLDOWN_SECONDS
            logger.warning("PostgreSQL fetchrow failed on %s: %s", db_key, exc)
            return None

    async def close(self) -> None:
        """Close all pools created on the current running event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop_pools = self._pools.pop(loop, None)
        if not loop_pools:
            return
        for db_key, pool in list(loop_pools.items()):
            try:
                await pool.close()
            except Exception as exc:
                logger.debug("Error closing PG pool %s: %s", db_key, exc)

    # ------------------------------------------------------------------
    # ID normalization & cross-site resolution
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_vndb_id(value: Optional[str]) -> Optional[str]:
        """Normalize ``2016`` / ``v2016`` / ``V2016`` to ``v2016``."""
        if not value:
            return None
        text = str(value).strip()
        match = _VNDB_ID_RE.match(text)
        if match:
            return f"v{int(match.group(1))}"
        return extract_vndb_id(text)

    async def resolve_vndb_id(self, raw: str) -> Optional[str]:
        """Resolve a VNDB, EroGamescape, or Bangumi ID/URL to a VNDB ``vXXX`` ID."""
        if not raw:
            return None
        text = str(raw).strip()
        if not text:
            return None

        # 1. Explicit EGS URL or egs:ID (check before generic v\d+ / digits)
        egs_id = extract_egs_id(text)
        if egs_id is not None:
            row = await self._fetchrow(
                "vndb_stats",
                "SELECT vn_id FROM vn_external_ratings WHERE primary_egs_id = $1 LIMIT 1",
                egs_id,
            )
            return row["vn_id"] if row else None

        # 2. Explicit Bangumi URL or bgm:ID / bangumi:ID
        bgm_id = extract_bangumi_id(text)
        if bgm_id is not None:
            row = await self._fetchrow(
                "vndb_stats",
                "SELECT vn_id FROM vn_external_ratings WHERE primary_bangumi_id = $1 LIMIT 1",
                bgm_id,
            )
            return row["vn_id"] if row else None

        # 3. VNDB URL / vXXX / bare numeric VNDB ID
        if "vndb.org" in text.lower() or _VNDB_ID_RE.match(text):
            return self.normalize_vndb_id(text)

        return extract_vndb_id(text)

    # ------------------------------------------------------------------
    # Core VN queries
    # ------------------------------------------------------------------

    async def get_vn_by_id(
        self,
        vndb_id: str,
        *,
        include_tags: bool = True,
        include_egs_details: bool = False,
        include_bangumi_details: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Fetch VNDB-API-compatible VN metadata enriched with EGS, Bangumi, and Jiten."""
        vn_id = self.normalize_vndb_id(vndb_id)
        if not vn_id or not self.is_configured:
            return None

        main_row = await self._fetchrow(
            "vndb_stats",
            """
            SELECT
                vn.id, vn.title, vn.title_romaji, vn.title_jp, vn.aliases,
                vn.description, vn.image_url, vn.image_sexual,
                vn.length, vn.length_minutes, vn.released,
                vn.rating, vn.average_rating, vn.votecount, vn.popularity,
                vn.developers, vn.olang, vn.devstatus,
                ext.primary_egs_id, ext.egs_median, ext.egs_average, ext.egs_stdev,
                ext.egs_count, ext.egs_playtime_median, ext.egs_fun_time_median,
                ext.egs_genre, ext.egs_has_trial,
                ext.primary_bangumi_id, ext.bangumi_score, ext.bangumi_rank,
                ext.bangumi_votes, ext.bangumi_name_cn, ext.consensus_score,
                (cb.vn_id IS NOT NULL) AS cover_blacklisted
            FROM visual_novels vn
            LEFT JOIN vn_external_ratings ext ON ext.vn_id = vn.id
            LEFT JOIN cover_blacklist cb ON cb.vn_id = vn.id
            WHERE vn.id = $1
            """,
            vn_id,
        )
        if not main_row:
            return None

        row_dict = dict(main_row)
        dev_ids = list(row_dict.get("developers") or [])

        # Fetch supplementary details concurrently across vndb, jiten, and vndb_stats
        tasks: Dict[str, Any] = {
            "vndb_extra": self._fetchrow(
                "vndb",
                "SELECT c_length, c_lengthnum FROM vn WHERE id = $1",
                vn_id,
            ),
            "vndb_titles": self._fetch(
                "vndb",
                "SELECT lang::text AS lang, official, title, latin FROM vn_titles WHERE id = $1",
                vn_id,
            ),
            "jiten": self.get_jiten_by_vndb_id(vn_id),
        }
        if include_tags:
            tasks["tags"] = self._fetch(
                "vndb_stats",
                """
                SELECT t.id, t.name, t.category, vt.score, vt.spoiler_level
                FROM vn_tags vt
                JOIN tags t ON t.id = vt.tag_id
                WHERE vt.vn_id = $1 AND COALESCE(vt.lie, false) = false
                ORDER BY vt.score DESC NULLS LAST, t.vn_count DESC NULLS LAST
                LIMIT 50
                """,
                vn_id,
            )
        if dev_ids:
            tasks["producers"] = self._fetch(
                "vndb_stats",
                "SELECT id, name, original FROM producers WHERE id = ANY($1::text[])",
                dev_ids,
            )
        if include_egs_details and row_dict.get("primary_egs_id") is not None:
            tasks["egs_details"] = self.get_egs_details(int(row_dict["primary_egs_id"]))
        if include_bangumi_details and row_dict.get("primary_bangumi_id") is not None:
            tasks["bangumi_details"] = self.get_bangumi_details(int(row_dict["primary_bangumi_id"]))

        keys = list(tasks.keys())
        resolved_values = await asyncio.gather(*tasks.values(), return_exceptions=True)
        extra: Dict[str, Any] = {}
        for key, val in zip(keys, resolved_values):
            extra[key] = None if isinstance(val, Exception) else val

        vndb_extra = extra.get("vndb_extra")
        length_minutes = row_dict.get("length_minutes")
        if length_minutes is None and vndb_extra is not None:
            length_minutes = vndb_extra.get("c_length")
        length_votes = vndb_extra.get("c_lengthnum") if vndb_extra is not None else None

        title_rows = extra.get("vndb_titles")
        if title_rows:
            titles = [
                {
                    "lang": r["lang"],
                    "title": r["title"],
                    "latin": r["latin"] or None,
                    "official": r["official"],
                }
                for r in title_rows
            ]
        else:
            titles = _synthesize_titles(
                row_dict.get("title"),
                row_dict.get("title_jp"),
                row_dict.get("title_romaji"),
            )

        tags_list: List[Dict[str, Any]] = []
        for t in extra.get("tags") or []:
            tags_list.append(
                {
                    "id": f"g{t['id']}",
                    "name": t["name"],
                    "rating": float(t["score"]) if t["score"] is not None else 0.0,
                    "spoiler": int(t["spoiler_level"] or 0),
                    "category": t["category"],
                }
            )

        producers_by_id = {p["id"]: p for p in (extra.get("producers") or [])}
        developers = [
            {
                "id": pid,
                "name": producers_by_id[pid]["name"],
                "original": producers_by_id[pid]["original"],
            }
            for pid in dev_ids
            if pid in producers_by_id
        ]

        rating_10 = row_dict.get("rating")
        rating_100 = round(float(rating_10) * 10.0, 2) if rating_10 is not None else None
        released = row_dict.get("released")
        released_str = released.isoformat() if hasattr(released, "isoformat") else released
        image_url = row_dict.get("image_url")
        image_sexual = row_dict.get("image_sexual")
        image_dict = (
            {"url": image_url, "sexual": float(image_sexual) if image_sexual is not None else 0.0}
            if image_url
            else None
        )

        result: Dict[str, Any] = {
            "id": row_dict["id"],
            "title": row_dict["title"],
            "title_jp": row_dict.get("title_jp"),
            "title_romaji": row_dict.get("title_romaji"),
            "description": row_dict.get("description") or "",
            "length": row_dict.get("length"),
            "length_minutes": length_minutes,
            "length_votes": length_votes,
            "rating": rating_100,
            "votecount": row_dict.get("votecount"),
            "released": released_str,
            "image": image_dict,
            "cover_blacklisted": bool(row_dict.get("cover_blacklisted")),
            "titles": titles,
            "tags": tags_list,
            "developers": developers,
            "external_ratings": _format_external_ratings(row_dict),
            "jiten": extra.get("jiten"),
        }
        if include_egs_details:
            result["egs_details"] = extra.get("egs_details")
        if include_bangumi_details:
            result["bangumi_details"] = extra.get("bangumi_details")

        return result

    async def get_vns_by_ids(self, vndb_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Batch-fetch normalized VN dicts for a list of VNDB IDs."""
        if not vndb_ids or not self.is_configured:
            return {}
        normalized = []
        seen = set()
        for raw_id in vndb_ids:
            vid = self.normalize_vndb_id(raw_id)
            if vid and vid not in seen:
                seen.add(vid)
                normalized.append(vid)
        if not normalized:
            return {}

        rows, title_rows = await asyncio.gather(
            self._fetch(
                "vndb_stats",
                """
                SELECT
                    vn.id, vn.title, vn.title_romaji, vn.title_jp,
                    vn.image_url, vn.image_sexual, vn.length, vn.length_minutes,
                    vn.released, vn.rating, vn.votecount,
                    ext.primary_egs_id, ext.egs_median, ext.egs_average, ext.egs_stdev,
                    ext.egs_count, ext.egs_playtime_median, ext.egs_fun_time_median,
                    ext.egs_genre, ext.egs_has_trial,
                    ext.primary_bangumi_id, ext.bangumi_score, ext.bangumi_rank,
                    ext.bangumi_votes, ext.bangumi_name_cn, ext.consensus_score,
                    diff.jiten_deck_id, diff.difficulty, diff.difficulty_raw,
                    diff.character_count, diff.word_count, diff.unique_word_count,
                    diff.unique_kanji_count, diff.sentence_count,
                    diff.average_sentence_length, diff.dialogue_percentage,
                    (cb.vn_id IS NOT NULL) AS cover_blacklisted
                FROM visual_novels vn
                LEFT JOIN vn_external_ratings ext ON ext.vn_id = vn.id
                LEFT JOIN vn_difficulty diff ON diff.vn_id = vn.id
                LEFT JOIN cover_blacklist cb ON cb.vn_id = vn.id
                WHERE vn.id = ANY($1::text[])
                """,
                normalized,
            ),
            self._fetch(
                "vndb",
                "SELECT id, lang::text AS lang, official, title, latin FROM vn_titles WHERE id = ANY($1::text[])",
                normalized,
            ),
        )
        if rows is None:
            return {}

        titles_map: Dict[str, List[Dict[str, Optional[str]]]] = {}
        for tr in title_rows or []:
            titles_map.setdefault(tr["id"], []).append(
                {
                    "lang": tr["lang"],
                    "title": tr["title"],
                    "latin": tr["latin"] or None,
                    "official": tr["official"],
                }
            )

        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            rd = dict(r)
            vid = rd["id"]
            rating_10 = rd.get("rating")
            released = rd.get("released")
            image_url = rd.get("image_url")
            image_sexual = rd.get("image_sexual")
            out[vid] = {
                "id": vid,
                "title": rd["title"],
                "title_jp": rd.get("title_jp"),
                "title_romaji": rd.get("title_romaji"),
                "length": rd.get("length"),
                "length_minutes": rd.get("length_minutes"),
                "rating": round(float(rating_10) * 10.0, 2) if rating_10 is not None else None,
                "votecount": rd.get("votecount"),
                "released": released.isoformat() if hasattr(released, "isoformat") else released,
                "image": (
                    {"url": image_url, "sexual": float(image_sexual) if image_sexual is not None else 0.0}
                    if image_url
                    else None
                ),
                "cover_blacklisted": bool(rd.get("cover_blacklisted")),
                "titles": titles_map.get(vid)
                or _synthesize_titles(rd.get("title"), rd.get("title_jp"), rd.get("title_romaji")),
                "external_ratings": _format_external_ratings(rd),
                "jiten": _format_jiten_row(rd),
            }
        return out

    async def search_vns(self, query: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Search visual novels across VNDB, EGS (incl. furigana), and Bangumi CN titles."""
        clean = (query or "").strip()
        if not clean or not self.is_configured:
            return []

        limit = max(1, min(limit, 25))

        # Direct ID / URL lookup
        resolved_id = await self.resolve_vndb_id(clean)
        if resolved_id:
            vn = await self.get_vn_by_id(resolved_id, include_tags=False)
            if vn:
                return [vn]

        q_lower = clean.lower()
        like_escaped = _escape_like(clean)
        prefix_pat = f"{like_escaped}%"
        sub_pat = f"%{like_escaped}%"
        use_trgm = len(clean) >= 3

        rows = await self._fetch(
            "vndb_stats",
            f"""
            SELECT
                vn.id, vn.title, vn.title_romaji, vn.title_jp,
                vn.image_url, vn.image_sexual, vn.released, vn.rating, vn.votecount,
                ext.primary_egs_id, ext.egs_median, ext.egs_average, ext.egs_stdev,
                ext.egs_count, ext.egs_playtime_median, ext.egs_fun_time_median,
                ext.egs_genre, ext.egs_has_trial,
                ext.primary_bangumi_id, ext.bangumi_score, ext.bangumi_rank,
                ext.bangumi_votes, ext.bangumi_name_cn, ext.consensus_score,
                (cb.vn_id IS NOT NULL) AS cover_blacklisted,
                CASE
                    WHEN lower(vn.title) = $1
                      OR lower(COALESCE(vn.title_jp, '')) = $1
                      OR lower(COALESCE(vn.title_romaji, '')) = $1
                      OR lower(COALESCE(ext.bangumi_name_cn, '')) = $1
                    THEN 0
                    WHEN vn.title ILIKE $2
                      OR vn.title_jp ILIKE $2
                      OR vn.title_romaji ILIKE $2
                      OR ext.bangumi_name_cn ILIKE $2
                    THEN 1
                    WHEN vn.title ILIKE $3
                      OR vn.title_jp ILIKE $3
                      OR vn.title_romaji ILIKE $3
                      OR array_to_string(vn.aliases, ' ') ILIKE $3
                      OR ext.bangumi_name_cn ILIKE $3
                    THEN 2
                    ELSE 3
                END AS match_tier
            FROM visual_novels vn
            LEFT JOIN vn_external_ratings ext ON ext.vn_id = vn.id
            LEFT JOIN cover_blacklist cb ON cb.vn_id = vn.id
            WHERE vn.title ILIKE $3
               OR vn.title_jp ILIKE $3
               OR vn.title_romaji ILIKE $3
               OR array_to_string(vn.aliases, ' ') ILIKE $3
               OR ext.bangumi_name_cn ILIKE $3
               {"OR similarity(vn.title, $4) > 0.35 OR similarity(COALESCE(vn.title_jp, ''), $4) > 0.35" if use_trgm else ""}
            ORDER BY match_tier ASC, COALESCE(vn.votecount, 0) DESC, vn.rating DESC NULLS LAST
            LIMIT $5
            """,
            *([q_lower, prefix_pat, sub_pat, clean, limit] if use_trgm else [q_lower, prefix_pat, sub_pat, limit]),
        )
        if rows is None:
            return []

        results_by_id: Dict[str, Dict[str, Any]] = {}
        ordered_ids: List[str] = []

        def _row_to_search_dict(rd: Dict[str, Any]) -> Dict[str, Any]:
            rating_10 = rd.get("rating")
            released = rd.get("released")
            image_url = rd.get("image_url")
            image_sexual = rd.get("image_sexual")
            return {
                "id": rd["id"],
                "title": rd["title"],
                "released": released.isoformat() if hasattr(released, "isoformat") else released,
                "rating": round(float(rating_10) * 10.0, 2) if rating_10 is not None else None,
                "image": (
                    {"url": image_url, "sexual": float(image_sexual) if image_sexual is not None else 0.0}
                    if image_url
                    else None
                ),
                "cover_blacklisted": bool(rd.get("cover_blacklisted")),
                "titles": _synthesize_titles(
                    rd.get("title"),
                    rd.get("title_jp"),
                    rd.get("title_romaji"),
                ),
                "external_ratings": _format_external_ratings(rd),
            }

        for r in rows:
            rd = dict(r)
            vid = rd["id"]
            results_by_id[vid] = _row_to_search_dict(rd)
            ordered_ids.append(vid)

        # If we have room left and the query looks Japanese or returned few hits,
        # also match EGS gamename / katakana furigana and map back via primary_egs_id.
        if len(ordered_ids) < limit and (_CJK_OR_KANA_RE.search(clean) or len(ordered_ids) < 5):
            kata_sub = f"%{_escape_like(_to_katakana(clean))}%"
            egs_rows = await self._fetch(
                "egs",
                """
                SELECT id
                FROM gamelist
                WHERE gamename ILIKE $1 OR furigana ILIKE $2
                ORDER BY COALESCE(count2, 0) DESC, median DESC NULLS LAST
                LIMIT $3
                """,
                sub_pat,
                kata_sub,
                limit,
            )
            if egs_rows:
                egs_ids = [int(er["id"]) for er in egs_rows]
                mapped_rows = await self._fetch(
                    "vndb_stats",
                    """
                    SELECT
                        vn.id, vn.title, vn.title_romaji, vn.title_jp,
                        vn.image_url, vn.image_sexual, vn.released, vn.rating, vn.votecount,
                        ext.primary_egs_id, ext.egs_median, ext.egs_average, ext.egs_stdev,
                        ext.egs_count, ext.egs_playtime_median, ext.egs_fun_time_median,
                        ext.egs_genre, ext.egs_has_trial,
                        ext.primary_bangumi_id, ext.bangumi_score, ext.bangumi_rank,
                        ext.bangumi_votes, ext.bangumi_name_cn, ext.consensus_score,
                        (cb.vn_id IS NOT NULL) AS cover_blacklisted
                    FROM vn_external_ratings ext
                    JOIN visual_novels vn ON vn.id = ext.vn_id
                    LEFT JOIN cover_blacklist cb ON cb.vn_id = vn.id
                    WHERE ext.primary_egs_id = ANY($1::int[])
                    ORDER BY COALESCE(vn.votecount, 0) DESC
                    LIMIT $2
                    """,
                    egs_ids,
                    limit,
                )
                for mr in mapped_rows or []:
                    rd = dict(mr)
                    vid = rd["id"]
                    if vid not in results_by_id:
                        results_by_id[vid] = _row_to_search_dict(rd)
                        ordered_ids.append(vid)
                        if len(ordered_ids) >= limit:
                            break

        return [results_by_id[vid] for vid in ordered_ids[:limit]]

    async def search_tags(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search searchable VNDB tags from ``vndb_stats.tags``."""
        clean = (query or "").strip()
        if not clean or not self.is_configured:
            return []
        limit = max(1, min(limit, 25))
        like_escaped = _escape_like(clean)
        rows = await self._fetch(
            "vndb_stats",
            """
            SELECT id, name, category, searchable
            FROM tags
            WHERE COALESCE(searchable, true) = true
              AND (name ILIKE $2 OR array_to_string(aliases, ' ') ILIKE $2)
            ORDER BY
                CASE
                    WHEN lower(name) = $1 THEN 0
                    WHEN name ILIKE $3 THEN 1
                    ELSE 2
                END,
                COALESCE(vn_count, 0) DESC
            LIMIT $4
            """,
            clean.lower(),
            f"%{like_escaped}%",
            f"{like_escaped}%",
            limit,
        )
        if rows is None:
            return []
        return [
            {
                "id": f"g{r['id']}",
                "name": r["name"],
                "category": r["category"],
                "searchable": bool(r["searchable"]),
            }
            for r in rows
        ]

    async def search_producers(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search VNDB producers from ``vndb_stats.producers``."""
        clean = (query or "").strip()
        if not clean or not self.is_configured:
            return []
        limit = max(1, min(limit, 25))
        like_escaped = _escape_like(clean)
        rows = await self._fetch(
            "vndb_stats",
            """
            SELECT id, name, original
            FROM producers
            WHERE name ILIKE $2 OR COALESCE(original, '') ILIKE $2
            ORDER BY
                CASE
                    WHEN lower(name) = $1 OR lower(COALESCE(original, '')) = $1 THEN 0
                    WHEN name ILIKE $3 OR COALESCE(original, '') ILIKE $3 THEN 1
                    ELSE 2
                END,
                COALESCE(vn_count, 0) DESC
            LIMIT $4
            """,
            clean.lower(),
            f"%{like_escaped}%",
            f"{like_escaped}%",
            limit,
        )
        if rows is None:
            return []
        return [{"id": r["id"], "name": r["name"], "original": r["original"]} for r in rows]

    async def get_random_vn(self) -> Optional[Dict[str, Any]]:
        """Return a random well-rated VN from ``vndb_stats.visual_novels``."""
        if not self.is_configured:
            return None
        row = await self._fetchrow(
            "vndb_stats",
            """
            SELECT id, title
            FROM visual_novels
            WHERE rating >= 6.0 AND votecount >= 50 AND length IS NOT NULL
            ORDER BY RANDOM()
            LIMIT 1
            """,
        )
        return {"id": row["id"], "title": row["title"]} if row else None

    # ------------------------------------------------------------------
    # Jiten, EroGamescape, and Bangumi specific queries
    # ------------------------------------------------------------------

    async def get_jiten_by_vndb_id(self, vndb_id: str) -> Optional[Dict[str, Any]]:
        """Fetch Jiten reading metrics for *vndb_id* from ``jiten`` (fallback ``vndb_stats``)."""
        vn_id = self.normalize_vndb_id(vndb_id)
        if not vn_id or not self.is_configured:
            return None

        row = await self._fetchrow(
            "jiten",
            """
            SELECT
                d.vn_id, d.jiten_deck_id, d.difficulty, d.difficulty_raw,
                d.character_count, d.word_count, d.unique_word_count,
                d.unique_kanji_count, d.sentence_count,
                d.average_sentence_length, d.dialogue_percentage,
                c.detail_json
            FROM vn_difficulty d
            LEFT JOIN jiten_deck_cache c ON c.vn_id = d.vn_id
            WHERE d.vn_id = $1
            """,
            vn_id,
        )
        cover_url: Optional[str] = None
        if row:
            detail_json = _parse_jsonb(row.get("detail_json"))
            if isinstance(detail_json, dict):
                main_deck = detail_json.get("mainDeck")
                if isinstance(main_deck, dict):
                    cover_url = main_deck.get("coverName") or None
                if not cover_url:
                    cover_url = detail_json.get("coverName") or None
            return _format_jiten_row(dict(row), cover_url=cover_url)

        # Fallback to vndb_stats mirror if jiten DB wasn't reachable or didn't have it
        fallback_row = await self._fetchrow(
            "vndb_stats",
            """
            SELECT
                vn_id, jiten_deck_id, difficulty, difficulty_raw,
                character_count, word_count, unique_word_count,
                unique_kanji_count, sentence_count,
                average_sentence_length, dialogue_percentage
            FROM vn_difficulty
            WHERE vn_id = $1
            """,
            vn_id,
        )
        return _format_jiten_row(dict(fallback_row)) if fallback_row else None

    async def get_jiten_by_vndb_ids(self, vndb_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Batch-fetch Jiten metrics for multiple VNDB IDs."""
        if not vndb_ids or not self.is_configured:
            return {}
        normalized = [vid for vid in (self.normalize_vndb_id(v) for v in vndb_ids) if vid]
        if not normalized:
            return {}
        rows = await self._fetch(
            "jiten",
            """
            SELECT
                vn_id, jiten_deck_id, difficulty, difficulty_raw,
                character_count, word_count, unique_word_count,
                unique_kanji_count, sentence_count,
                average_sentence_length, dialogue_percentage
            FROM vn_difficulty
            WHERE vn_id = ANY($1::text[])
            """,
            normalized,
        )
        if rows is None:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            formatted = _format_jiten_row(dict(r))
            if formatted:
                out[r["vn_id"]] = formatted
        return out

    async def get_egs_details(self, egs_id: int) -> Optional[Dict[str, Any]]:
        """Fetch EroGamescape game metadata, brand, and top non-spoiler POVs."""
        if not self.is_configured:
            return None

        game_row, pov_rows = await asyncio.gather(
            self._fetchrow(
                "egs",
                """
                SELECT
                    g.id, g.gamename, g.furigana, g.sellday,
                    g.median, g.average2, g.count2, g.stdev,
                    g.total_play_time_median, g.time_before_understanding_fun_median,
                    g.genre, g.erogame, g.dmm, g.dlsite_id, g.trial_url,
                    b.id AS brand_id, b.brandname, b.makername
                FROM gamelist g
                LEFT JOIN brandlist b ON b.id = g.brandname
                WHERE g.id = $1
                """,
                int(egs_id),
            ),
            self._fetch(
                "egs",
                """
                SELECT
                    p.id, p.title,
                    SUM(
                        CASE pt.rank
                            WHEN 'A' THEN pt.count * 3
                            WHEN 'B' THEN pt.count * 2
                            ELSE pt.count
                        END
                    ) AS weighted_score,
                    SUM(pt.count) AS total_votes
                FROM povgroups_toukei pt
                JOIN povlist p ON p.id = pt.pov
                WHERE pt.game = $1 AND COALESCE(p.netabare, false) = false
                GROUP BY p.id, p.title
                ORDER BY weighted_score DESC
                LIMIT 6
                """,
                int(egs_id),
            ),
        )
        if not game_row:
            return None

        gd = dict(game_row)
        sellday = gd.get("sellday")
        return {
            "id": int(gd["id"]),
            "url": egs_game_url(int(gd["id"])),
            "gamename": gd.get("gamename"),
            "furigana": gd.get("furigana"),
            "sellday": sellday.isoformat() if hasattr(sellday, "isoformat") else sellday,
            "brand_id": gd.get("brand_id"),
            "brandname": gd.get("brandname"),
            "makername": gd.get("makername"),
            "median": gd.get("median"),
            "average": gd.get("average2"),
            "count": gd.get("count2"),
            "stdev": gd.get("stdev"),
            "total_play_time_median": gd.get("total_play_time_median"),
            "time_before_fun_median": gd.get("time_before_understanding_fun_median"),
            "genre": gd.get("genre"),
            "erogame": gd.get("erogame"),
            "dmm": gd.get("dmm"),
            "dlsite_id": gd.get("dlsite_id"),
            "trial_url": gd.get("trial_url"),
            "povs": [
                {
                    "id": p["id"],
                    "title": p["title"],
                    "score": int(p["weighted_score"] or 0),
                    "votes": int(p["total_votes"] or 0),
                }
                for p in (pov_rows or [])
            ],
        }

    async def get_bangumi_details(self, bangumi_id: int) -> Optional[Dict[str, Any]]:
        """Fetch Bangumi subject metadata, score breakdown, and top user tags."""
        if not self.is_configured:
            return None

        row = await self._fetchrow(
            "bangumi",
            """
            SELECT id, type, name, name_cn, date, score, rank, nsfw,
                   score_details, tags, favorite, summary
            FROM subject
            WHERE id = $1
            """,
            int(bangumi_id),
        )
        if not row:
            return None

        rd = dict(row)
        score_details = _parse_jsonb(rd.get("score_details")) or {}
        votes = 0
        if isinstance(score_details, dict):
            for v in score_details.values():
                try:
                    votes += int(v)
                except (TypeError, ValueError):
                    continue

        raw_tags = _parse_jsonb(rd.get("tags")) or []
        top_tags: List[Dict[str, Any]] = []
        if isinstance(raw_tags, list):
            for item in raw_tags[:6]:
                if isinstance(item, dict) and item.get("name"):
                    top_tags.append(
                        {"name": str(item["name"]), "count": int(item.get("count") or 0)}
                    )

        favorite = _parse_jsonb(rd.get("favorite")) or {}
        return {
            "id": int(rd["id"]),
            "url": bangumi_subject_url(int(rd["id"])),
            "type": rd.get("type"),
            "name": rd.get("name"),
            "name_cn": rd.get("name_cn") or None,
            "date": rd.get("date"),
            "score": float(rd["score"]) if rd.get("score") is not None else None,
            "rank": rd.get("rank"),
            "votes": votes,
            "nsfw": bool(rd.get("nsfw")),
            "tags": top_tags,
            "favorite": favorite if isinstance(favorite, dict) else {},
            "summary": rd.get("summary") or "",
        }


_pg_db_instance: Optional[VNCRMultiDB] = None


def get_pg_db() -> VNCRMultiDB:
    """Return the shared :class:`VNCRMultiDB` instance."""
    global _pg_db_instance
    if _pg_db_instance is None:
        _pg_db_instance = VNCRMultiDB()
    return _pg_db_instance
