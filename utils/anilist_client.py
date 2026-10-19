"""Async client for interacting with the AniList GraphQL API."""

from __future__ import annotations

import aiohttp
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_GRAPHQL_ENDPOINT = "https://graphql.anilist.co"

_SEARCH_QUERY = """
query ($search: String, $type: MediaType, $page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    media(search: $search, type: $type, sort: [POPULARITY_DESC]) {
      id
      idMal
      title {
        romaji
        english
        native
      }
      format
      status
      episodes
      chapters
      volumes
      duration
      averageScore
      coverImage {
        extraLarge
        large
        medium
        color
      }
      siteUrl
      isAdult
      startDate { year month day }
    }
  }
}
"""

_MEDIA_QUERY = """
query ($id: Int!, $type: MediaType) {
  Media(id: $id, type: $type) {
    id
    idMal
    title {
      romaji
      english
      native
    }
    format
    status
    episodes
    chapters
    volumes
    duration
    averageScore
    coverImage {
      extraLarge
      large
      medium
      color
    }
    siteUrl
    isAdult
    startDate { year month day }
  }
}
"""


class AniListClient:
    """Minimal async client for AniList GraphQL endpoints."""

    def __init__(self, session: Optional[aiohttp.ClientSession] = None) -> None:
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "AniListClient":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=30)
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            self._session = aiohttp.ClientSession(timeout=timeout, headers=headers)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("AniListClient must be used as an async context manager")
        return self._session

    async def _post(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        session = await self._ensure_session()

        try:
            async with session.post(_GRAPHQL_ENDPOINT, json={"query": query, "variables": variables}) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error("AniList API error: %s - %s", resp.status, text)
                    raise aiohttp.ClientResponseError(
                        request_info=resp.request_info,
                        history=resp.history,
                        status=resp.status,
                        message=text,
                    )

                data = await resp.json()
                return data.get("data", {})
        except aiohttp.ClientError as exc:
            logger.error("Network error when contacting AniList: %s", exc)
            raise

    async def search_media(self, search: str, media_type: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search AniList media by title."""
        search = (search or "").strip()
        if not search:
            return []

        variables = {
            "search": search,
            "type": media_type.upper(),
            "page": 1,
            "perPage": max(1, min(limit, 50)),
        }

        try:
            payload = await self._post(_SEARCH_QUERY, variables)
        except Exception:
            return []

        media = payload.get("Page", {}).get("media", [])
        return media or []

    async def get_media(self, media_id: int, media_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch media details by AniList ID."""
        variables: Dict[str, Any] = {"id": media_id}
        if media_type:
            variables["type"] = media_type.upper()

        try:
            payload = await self._post(_MEDIA_QUERY, variables)
        except Exception:
            return None

        media = payload.get("Media")
        return media
