"""Utility client for VNDB's JSON API."""

import aiohttp
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

class VNDBClient:
    """
    Async client for interacting with the VNDB API
    Documentation: https://vndb.org/d11
    """
    
    def __init__(self, client_name: str = "Muramasa", client_version: str = "1.0"):
        self.base_url = "https://api.vndb.org/kana"
        self.client_name = client_name
        self.client_version = client_version
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def __aenter__(self):
        """Async context manager entry"""
        self.session = aiohttp.ClientSession(
            headers={
                "User-Agent": f"{self.client_name}/{self.client_version}",
                "Content-Type": "application/json"
            },
            timeout=aiohttp.ClientTimeout(total=30)
        )
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.session:
            await self.session.close()
    
    async def _make_request(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Make a request to the VNDB API"""
        if not self.session:
            raise RuntimeError("VNDBClient must be used as an async context manager")
            
        url = f"{self.base_url}/{endpoint}"
        
        try:
            async with self.session.post(url, json=data) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"VNDB API error: {response.status} - {await response.text()}")
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status
                    )
        except aiohttp.ClientError as e:
            logger.error(f"Network error when contacting VNDB: {e}")
            raise
    
    async def get_vn_by_id(self, vn_id: str) -> Optional[Dict[str, Any]]:
        """
        Get visual novel by ID
        
        Args:
            vn_id: VNDB visual novel ID (e.g., "v17")
            
        Returns:
            Dict containing VN data or None if not found
        """
        try:
            data = {
                "filters": ["id", "=", vn_id],
                "fields": (
                    "id,title,description,length,rating,votecount,released,"
                    "image.url,image.sexual,"
                    "tags.name,tags.rating,tags.spoiler,tags.category,"
                    "titles.title,titles.lang,titles.latin"
                )
            }
            
            response = await self._make_request("vn", data)
            
            if response.get("results"):
                return response["results"][0]
            return None
            
        except Exception as e:
            logger.error(f"Error fetching VN {vn_id}: {e}")
            return None
    
    async def get_vn(self, vn_id: str) -> Optional[Dict[str, Any]]:
        """Alias of :meth:`get_vn_by_id` for backwards compatibility."""
        return await self.get_vn_by_id(vn_id)

    async def search_vns(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search VNDB for visual novels with richer metadata for autocomplete."""
        query = (query or "").strip()
        if not query:
            return []

        limit = max(1, min(limit, 25))

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
            "results": limit
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
        """Search VNDB tags (/kana/tag) for the console's theme tag picker.

        Drops tags VNDB marks unsearchable. Ordinary searches surface grouping
        nodes like "Theme" and "Story", and the `tag` VN filter matches nothing
        at all for those, so a theme built on one would reject every VN.
        """
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, min(limit, 25))
        payload = {
            "filters": ["search", "=", query],
            "fields": "id,name,category,searchable",
            "results": limit,
        }
        response = await self._make_request("tag", payload)
        return [t for t in response.get("results", []) if t.get("searchable")]

    async def search_producers(self, query: str, limit: int = 10):
        """Search VNDB producers (/kana/producer) for the developer picker."""
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, min(limit, 25))
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
        try:
            # Default filters for quality VNs
            default_filters = ["and", 
                ["rating", ">=", 6.0],
                ["votecount", ">=", 50],
                ["length", "!=", None]
            ]
            
            data = {
                "filters": filters or default_filters,
                "fields": "id,title",
                "sort": "random",
                "results": 1
            }
            
            response = await self._make_request("vn", data)
            
            if response.get("results"):
                return response["results"][0]
            return None
            
        except Exception as e:
            logger.error(f"Error fetching random VN: {e}")
            return None
