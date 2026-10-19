"""Async client for interacting with the IGDB API."""

from __future__ import annotations

import aiohttp
import logging
from typing import Any, Dict, List, Optional
import os
import time
import json
from pathlib import Path

logger = logging.getLogger(__name__)

_IGDB_API_ENDPOINT = "https://api.igdb.com/v4"
_TWITCH_TOKEN_ENDPOINT = "https://id.twitch.tv/oauth2/token"
_TOKEN_CACHE_FILE = Path("data/igdb_token_cache.json")


def _escape_apicalypse(query: str) -> str:
    """Make a user query safe inside an APICalypse double-quoted string.

    A raw quote/backslash would end the string early, so the query must stay
    one string literal. Escape backslashes first, then quotes; drop control
    chars (newlines split APICalypse statements) and cap length.
    """
    query = query[:200]
    query = "".join(ch for ch in query if ch >= " ")
    return query.replace("\\", "\\\\").replace('"', '\\"')


class IGDBClient:
    """Minimal async client for IGDB API endpoints with automatic token regeneration."""

    def __init__(
        self, 
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        access_token: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None
    ) -> None:
        self._session = session
        self._owns_session = session is None
        # Get credentials from environment or parameters
        self._client_id = client_id or os.getenv("IGDB_CLIENT_ID", "")
        self._client_secret = client_secret or os.getenv("IGDB_CLIENT_SECRET", "")
        self._access_token = access_token or os.getenv("IGDB_ACCESS_TOKEN", "")
        self._token_expires_at: Optional[float] = None
        self._token_refresh_attempted = False
        
        # Load cached token info if available
        self._load_token_cache()

    def _load_token_cache(self) -> None:
        """Load cached token expiration time from disk."""
        try:
            if _TOKEN_CACHE_FILE.exists():
                with open(_TOKEN_CACHE_FILE, 'r') as f:
                    cache = json.load(f)
                    self._token_expires_at = cache.get("expires_at")
                    cached_token = cache.get("access_token")
                    # Use cached token if no token was provided
                    if cached_token and not self._access_token:
                        self._access_token = cached_token
                        logger.info("Loaded IGDB access token from cache")
        except Exception as e:
            logger.warning(f"Failed to load IGDB token cache: {e}")
    
    def _save_token_cache(self, access_token: str, expires_in: int) -> None:
        """Save token and expiration time to disk."""
        try:
            _TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            cache = {
                "access_token": access_token,
                "expires_at": time.time() + expires_in,
                "generated_at": time.time()
            }
            with open(_TOKEN_CACHE_FILE, 'w') as f:
                json.dump(cache, f, indent=2)
            self._token_expires_at = cache["expires_at"]
            logger.info(f"IGDB token cached, expires in {expires_in / 86400:.1f} days")
        except Exception as e:
            logger.warning(f"Failed to save IGDB token cache: {e}")
    
    def _is_token_expired(self) -> bool:
        """Check if the current token is expired or will expire soon."""
        if not self._token_expires_at:
            return False  # Unknown expiration, assume valid
        
        # Consider token expired if it expires within 1 hour (3600 seconds)
        return time.time() >= (self._token_expires_at - 3600)
    
    async def _regenerate_token(self) -> bool:
        """Regenerate access token using client credentials."""
        if not self._client_id or not self._client_secret:
            logger.error("Cannot regenerate IGDB token: missing client_id or client_secret")
            return False
        
        if self._token_refresh_attempted:
            logger.warning("Token refresh already attempted, skipping to avoid loop")
            return False
        
        self._token_refresh_attempted = True
        
        try:
            logger.info("Regenerating IGDB access token...")
            # Credentials go in the form body so they never appear in a URL or error text.
            form = {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(_TWITCH_TOKEN_ENDPOINT, data=form) as response:
                    response.raise_for_status()
                    data = await response.json()
                    
                    new_token = data.get("access_token")
                    expires_in = data.get("expires_in", 5184000)  # Default ~60 days
                    
                    if not new_token:
                        logger.error("Failed to get new token from Twitch OAuth")
                        return False
                    
                    # Update token
                    self._access_token = new_token
                    self._save_token_cache(new_token, expires_in)
                    
                    # Update session headers if session exists
                    if self._session:
                        self._session.headers.update({
                            "Authorization": f"Bearer {new_token}"
                        })
                    
                    logger.info(f"Successfully regenerated IGDB token (expires in {expires_in / 86400:.1f} days)")
                    return True
                    
        except Exception as e:
            logger.error(f"Failed to regenerate IGDB token: {e}")
            return False

    async def __aenter__(self) -> "IGDBClient":
        # Check if token needs regeneration before creating session
        if self._is_token_expired() and self._client_secret:
            await self._regenerate_token()
        
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=30)
            headers = {
                "Client-ID": self._client_id,
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json"
            }
            self._session = aiohttp.ClientSession(timeout=timeout, headers=headers)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None
        # Reset refresh flag for next use
        self._token_refresh_attempted = False

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("IGDBClient must be used as an async context manager")
        return self._session

    async def _post(self, endpoint: str, body: str) -> List[Dict[str, Any]]:
        session = await self._ensure_session()
        url = f"{_IGDB_API_ENDPOINT}/{endpoint}"
        
        try:
            async with session.post(url, data=body) as response:
                # Check for authentication errors
                if response.status == 401 and self._client_secret and not self._token_refresh_attempted:
                    logger.warning("IGDB token appears invalid, attempting regeneration...")
                    if await self._regenerate_token():
                        # Retry the request with new token
                        async with session.post(url, data=body) as retry_response:
                            retry_response.raise_for_status()
                            return await retry_response.json()
                
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            logger.error(f"IGDB API request failed: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error in IGDB API request: {e}")
            return []

    async def search_games(
        self, 
        query: str, 
        limit: int = 10,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Search for games by name.
        
        Returns a list of game objects with fields:
        - id: Game ID
        - name: Game name
        - cover: Cover art information (if available)
        - first_release_date: Unix timestamp of first release
        - total_rating: Aggregated rating
        """
        if not query or not self._client_id or not self._access_token:
            return []

        # Escape before interpolating into APICalypse double-quoted strings.
        safe_query = _escape_apicalypse(query)

        # Check if query contains Japanese characters
        has_japanese = any('\u3040' <= char <= '\u309F' or  # Hiragana
                          '\u30A0' <= char <= '\u30FF' or  # Katakana
                          '\u4E00' <= char <= '\u9FFF'     # Kanji
                          for char in query)
        
        try:
            # If searching in Japanese, search localizations first
            if has_japanese:
                # For Japanese queries, search in game_localizations
                body = f"""
fields game, name, region;
where name ~ *"{safe_query}"* & region = 3;
limit {limit * 5};
"""
                localization_results = await self._post("game_localizations", body.strip())
                if not localization_results:
                    return []
                
                # Sort results to prioritize exact matches and better relevance
                # Calculate match scores
                scored_results = []
                for loc in localization_results:
                    if not loc.get("game") or not loc.get("name"):
                        continue
                    
                    name = loc.get("name", "")
                    score = 0
                    
                    # Exact match gets highest priority
                    if name == query:
                        score = 1000
                    # Starts with query
                    elif name.startswith(query):
                        score = 500
                    # Contains query
                    elif query in name:
                        score = 100
                    # Otherwise it's a fuzzy match
                    else:
                        score = 10
                    
                    # Prefer shorter names (more specific)
                    score -= len(name) * 0.1
                    
                    scored_results.append((score, loc))
                
                # Sort by score descending
                scored_results.sort(key=lambda x: x[0], reverse=True)
                
                # Get unique game IDs in order of relevance
                seen_ids = set()
                game_ids = []
                for score, loc in scored_results:
                    game_id = loc.get("game")
                    if game_id and game_id not in seen_ids:
                        seen_ids.add(game_id)
                        game_ids.append(game_id)
                        if len(game_ids) >= limit:
                            break
                
                if not game_ids:
                    return []
                
                # Fetch the actual games
                game_ids_str = ",".join(str(gid) for gid in game_ids)
                games_body = f"""
fields name, cover.image_id, first_release_date, total_rating,
       alternative_names.name, alternative_names.comment,
       platforms.abbreviation,
       total_rating_count;
where id = ({game_ids_str});
limit {limit};
"""
                results = await self._post("games", games_body.strip())
                
                # Re-order results to match the game_ids order (by relevance)
                if results:
                    results_map = {game["id"]: game for game in results}
                    results = [results_map[gid] for gid in game_ids if gid in results_map]
                
            else:
                # For English queries, use standard search
                body = f"""
search "{safe_query}";
fields name, cover.image_id, first_release_date, total_rating, 
       alternative_names.name, alternative_names.comment,
       platforms.abbreviation,
       total_rating_count;
limit {limit};
offset {offset};
"""
                results = await self._post("games", body.strip())
            
            if not results:
                return []
            
            # Fetch localizations for all returned games so Japanese titles are available
            game_ids = [game.get("id") for game in results if game.get("id")]
            if game_ids:
                game_ids_str = ",".join(str(gid) for gid in game_ids)
                loc_body = f"""
fields game, name, region;
where game = ({game_ids_str}) & region = 3;
limit 500;
"""
                localizations = await self._post("game_localizations", loc_body.strip())
                
                # Map localizations to games
                loc_map = {}
                for loc in localizations or []:
                    game_id = loc.get("game")
                    if game_id and loc.get("name"):
                        if game_id not in loc_map:
                            loc_map[game_id] = []
                        loc_map[game_id].append(loc)
                
                # Add localizations to game results
                for game in results:
                    game_id = game.get("id")
                    if game_id and game_id in loc_map:
                        game["game_localizations"] = loc_map[game_id]
            
            return results
            
        except Exception as e:
            logger.error(f"Error searching games: {e}")
            return []

    async def get_game(self, game_id: int) -> Optional[Dict[str, Any]]:
        """
        Get detailed information about a specific game.
        
        Returns a game object with detailed fields.
        """
        if not self._client_id or not self._access_token:
            return None
        
        body = f"""
fields name, cover.image_id, first_release_date, total_rating, 
       summary, storyline, url, total_rating_count,
       alternative_names.name, alternative_names.comment,
       game_localizations.name, game_localizations.region,
       platforms.abbreviation;
where id = {game_id};
"""
        
        try:
            results = await self._post("games", body.strip())
            return results[0] if results else None
        except Exception as e:
            logger.error(f"Error getting game {game_id}: {e}")
            return None

    @staticmethod
    def get_cover_url(image_id: str, size: str = "cover_big") -> str:
        """
        Generate IGDB cover image URL.
        
        Sizes: cover_small, screenshot_med, cover_big, logo_med, 
               screenshot_big, screenshot_huge, thumb, micro, 720p, 1080p
        """
        return f"https://images.igdb.com/igdb/image/upload/t_{size}/{image_id}.jpg"

    @staticmethod
    def format_game_result(game: Dict[str, Any]) -> Dict[str, Any]:
        """Format a game result into a standardized structure."""
        # Try to find Japanese title - check game_localizations first
        japanese_title = None
        
        # Check game_localizations field (this is where IGDB stores official localized titles)
        # Region 3 is Japan in IGDB
        if "game_localizations" in game and game["game_localizations"]:
            for loc in game["game_localizations"]:
                if isinstance(loc, dict):
                    region = loc.get("region")
                    name = loc.get("name", "")
                    # Region 3 is Japan in IGDB
                    if region == 3:
                        japanese_title = name
                        break
        
        # Fallback to alternative_names if no localization found
        if not japanese_title and "alternative_names" in game and game["alternative_names"]:
            for alt in game["alternative_names"]:
                if isinstance(alt, dict):
                    comment = alt.get("comment", "").lower()
                    name = alt.get("name", "")
                    # Look for Japanese indicators in the comment field
                    if any(keyword in comment for keyword in ["japanese", "japan", "jp", "日本", "localized"]):
                        # Check if the name contains Japanese characters to confirm
                        has_japanese = any('\u3040' <= char <= '\u309F' or  # Hiragana
                                         '\u30A0' <= char <= '\u30FF' or  # Katakana
                                         '\u4E00' <= char <= '\u9FFF'     # Kanji
                                         for char in name)
                        if has_japanese:
                            japanese_title = name
                            break
        
        original_name = game.get("name", "Unknown Game")
        # Use Japanese title if available, otherwise use original
        display_name = japanese_title or original_name
        
        # Extract platform abbreviations
        platforms = []
        if "platforms" in game and game["platforms"]:
            for platform in game["platforms"]:
                if isinstance(platform, dict) and platform.get("abbreviation"):
                    platforms.append(platform["abbreviation"])
        
        result = {
            "id": game.get("id"),
            "name": display_name,  # Prioritized Japanese title
            "original_name": original_name,  # Keep original English for reference
            "cover_url": None,
            "url": game.get("url"),
            "rating": game.get("total_rating"),
            "rating_count": game.get("total_rating_count"),
            "release_date": game.get("first_release_date"),
            "platforms": platforms,  # List of platform abbreviations
            "alternative_names": [],
            "japanese_title": japanese_title
        }
        
        # Extract cover URL if available
        if "cover" in game and game["cover"]:
            if isinstance(game["cover"], dict) and "image_id" in game["cover"]:
                result["cover_url"] = IGDBClient.get_cover_url(game["cover"]["image_id"])
        
        # Extract alternative names with their comments
        if "alternative_names" in game and game["alternative_names"]:
            result["alternative_names"] = [
                {
                    "name": alt.get("name"),
                    "comment": alt.get("comment", "")
                }
                for alt in game["alternative_names"] 
                if isinstance(alt, dict) and "name" in alt
            ]
        
        return result
