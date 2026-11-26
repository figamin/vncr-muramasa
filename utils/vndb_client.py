"""Utility client for VNDB metadata (PostgreSQL multi-DB first, HTTP Kana API fallback)."""

import aiohttp
import logging
from typing import Any, Dict, List, Optional

from utils.pg_db import get_pg_db

logger = logging.getLogger(__name__)


class VNDBClient:
    """
    Async client for interacting with VNDB data.

    Queries the local VNCR PostgreSQL databases first (``vndb_stats``, ``vndb``,
    ``jiten``, ``egs``, ``bangumi``) and falls back to the public VNDB Kana API
    whenever PostgreSQL is disabled, unreachable, or missing an entry.
    Documentation: https://vndb.org/d11
    """

    def __init__(self, client_name: str = "Muramasa", client_version: str = "1.0"):
        self.base_url = "https://api.vndb.org/kana"
        self.client_name = client_name
        self.client_version = client_version
        self.session: Optional[aiohttp.ClientSession] = None
        self._entered = False

    async def __aenter__(self):
        """Async context manager entry (HTTP session is created lazily on fallback)."""
        self._entered = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        self._entered = False
        if self.session:
            await self.session.close()
            self.session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if not self._entered and self.session is None:
            raise RuntimeError("VNDBClient must be used as an async context manager")
        if self.session is None:
            self.session = aiohttp.ClientSession(
                headers={
                    "User-Agent": f"{self.client_name}/{self.client_version}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self.session

    async def _make_request(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Make a request to the VNDB API"""
        session = self._ensure_session()
        url = f"{self.base_url}/{endpoint}"

        try:
            async with session.post(url, json=data) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"VNDB API error: {response.status} - {await response.text()}")
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                    )
        except aiohttp.ClientError as e:
            logger.error(f"Network error when contacting VNDB: {e}")
            raise

    async def get_vn_by_id(self, vn_id: str) -> Optional[Dict[str, Any]]:
        """
        Get visual novel by ID (accepts VNDB, EGS, or Bangumi IDs/URLs when PG is active).

        Args:
            vn_id: VNDB visual novel ID (e.g., "v17")

        Returns:
            Dict containing VN data or None if not found
        """
        pg = get_pg_db()
        if pg.is_configured:
            try:
                resolved = await pg.resolve_vndb_id(vn_id) or vn_id
                pg_vn = await pg.get_vn_by_id(resolved)
                if pg_vn is not None:
                    return pg_vn
                vn_id = resolved
            except Exception as exc:
                logger.warning("PG get_vn_by_id failed for %s: %s", vn_id, exc)

        try:
            data = {
                "filters": ["id", "=", vn_id],
                "fields": (
                    "id,title,description,length,length_minutes,length_votes,"
                    "rating,votecount,released,"
                    "image.url,image.sexual,"
                    "tags.name,tags.rating,tags.spoiler,tags.category,"
                    "titles.title,titles.lang,titles.latin"
                ),
            }

            response = await self._make_request("vn", data)

            if response.get("results"):
                return response["results"][0]
            return None

        except Exception as e:
            logger.error(f"Error fetching VN {vn_id}: {e}")
            return None

    async def get_vn_full(self, vn_id: str) -> Optional[Dict[str, Any]]:
        """Fetch VN data including full EroGamescape and Bangumi detail payloads."""
        pg = get_pg_db()
        if pg.is_configured:
            try:
                resolved = await pg.resolve_vndb_id(vn_id) or vn_id
                pg_vn = await pg.get_vn_by_id(
                    resolved,
                    include_tags=True,
                    include_egs_details=True,
                    include_bangumi_details=True,
                )
                if pg_vn is not None:
                    return pg_vn
                vn_id = resolved
            except Exception as exc:
                logger.warning("PG get_vn_full failed for %s: %s", vn_id, exc)

        return await self.get_vn_by_id(vn_id)

    async def get_vns_by_ids(self, vn_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Batch-fetch VN metadata for multiple VNDB IDs (PG batch first, HTTP fallback)."""
        if not vn_ids:
            return {}

        pg = get_pg_db()
        results: Dict[str, Dict[str, Any]] = {}
        remaining = list(dict.fromkeys(v for v in vn_ids if v))

        if pg.is_configured:
            try:
                results = await pg.get_vns_by_ids(remaining)
                remaining = [vid for vid in remaining if vid not in results]
                if not remaining:
                    return results
            except Exception as exc:
                logger.warning("PG get_vns_by_ids failed: %s", exc)

        # HTTP fallback in batches of 10 (VNDB Kana API limit)
        batch_size = 10
        for i in range(0, len(remaining), batch_size):
            batch = remaining[i : i + batch_size]
            try:
                payload = {
                    "filters": ["id", "=", batch if len(batch) > 1 else batch[0]],
                    "fields": (
                        "id,title,length,length_minutes,rating,votecount,released,"
                        "image.url,image.sexual,titles.title,titles.lang,titles.latin"
                    ),
                    "results": len(batch),
                }
                response = await self._make_request("vn", payload)
                for vn in response.get("results", []):
                    vid = vn.get("id")
                    if vid:
                        results[vid] = vn
            except Exception as exc:
                logger.warning("HTTP batch VN lookup failed for %s: %s", batch, exc)

        return results

    async def get_vn(self, vn_id: str) -> Optional[Dict[str, Any]]:
        """Alias of :meth:`get_vn_by_id` for backwards compatibility."""
        return await self.get_vn_by_id(vn_id)

    async def search_vns(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search VNDB (and EGS/Bangumi titles via PG) for visual novels."""
        query = (query or "").strip()
        if not query:
            return []

        limit = max(1, min(limit, 25))

        pg = get_pg_db()
        if pg.is_configured:
            try:
                pg_results = await pg.search_vns(query, limit=limit)
                if pg_results:
                    return pg_results
            except Exception as exc:
                logger.warning("PG search_vns failed for '%s': %s", query, exc)

        payload = {
            "filters": ["search", "=", query],
            "fields": (
                "id,title,released,rating,"
                "image.url,image.sexual,"
                "titles.title,titles.lang,titles.latin"
            ),
            "sort": "popularity",
            "reverse": True,
            "page": 1,
            "results": limit,
        }

        try:
            response = await self._make_request("vn", payload)
            return response.get("results", [])
        except Exception as exc:  # pragma: no cover - network failures
            logger.error("Error searching VNDB for '%s': %s", query, exc)
            return []

    async def search_vn(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Compatibility wrapper around :meth:`search_vns`."""
        return await self.search_vns(query, limit=limit)

    async def search_tags(self, query: str, limit: int = 10):
        """Search VNDB tags for the console's theme tag picker.

        Drops tags VNDB marks unsearchable. Ordinary searches surface grouping
        nodes like "Theme" and "Story", and the `tag` VN filter matches nothing
        at all for those, so a theme built on one would reject every VN.
        """
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, min(limit, 25))

        pg = get_pg_db()
        if pg.is_configured:
            try:
                pg_tags = await pg.search_tags(query, limit=limit)
                if pg_tags:
                    return pg_tags
            except Exception as exc:
                logger.warning("PG search_tags failed for '%s': %s", query, exc)

        payload = {
            "filters": ["search", "=", query],
            "fields": "id,name,category,searchable",
            "results": limit,
        }
        response = await self._make_request("tag", payload)
        return [t for t in response.get("results", []) if t.get("searchable")]

    async def search_producers(self, query: str, limit: int = 10):
        """Search VNDB producers for the developer picker."""
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, min(limit, 25))

        pg = get_pg_db()
        if pg.is_configured:
            try:
                pg_producers = await pg.search_producers(query, limit=limit)
                if pg_producers:
                    return pg_producers
            except Exception as exc:
                logger.warning("PG search_producers failed for '%s': %s", query, exc)

        payload = {
            "filters": ["search", "=", query],
            "fields": "id,name,original",
            "results": limit,
        }
        response = await self._make_request("producer", payload)
        return response.get("results", [])

    async def get_random_vn(self, filters: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        """
        Get a random visual novel

        Args:
            filters: Optional filters to apply

        Returns:
            Random VN dictionary or None
        """
        if filters is None:
            pg = get_pg_db()
            if pg.is_configured:
                try:
                    pg_vn = await pg.get_random_vn()
                    if pg_vn is not None:
                        return pg_vn
                except Exception as exc:
                    logger.warning("PG get_random_vn failed: %s", exc)

        try:
            # Default filters for quality VNs
            default_filters = [
                "and",
                ["rating", ">=", 6.0],
                ["votecount", ">=", 50],
                ["length", "!=", None],
            ]

            data = {
                "filters": filters or default_filters,
                "fields": "id,title",
                "sort": "random",
                "results": 1,
            }

            response = await self._make_request("vn", data)

            if response.get("results"):
                return response["results"][0]
            return None

        except Exception as e:
            logger.error(f"Error fetching random VN: {e}")
            return None
