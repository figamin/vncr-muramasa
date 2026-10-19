"""
Media data integration for immersion tracking
Handles integration with VNDB, anime databases, and video platforms
"""

import aiohttp
import asyncio
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse, parse_qs
from utils.vndb_client import VNDBClient
from utils.anilist_client import AniListClient
from utils.igdb_client import IGDBClient


def normalize_anilist_titles(media: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Extract consistent title fields from an AniList payload."""
    titles_payload = media.get("title") or {}

    romaji = titles_payload.get("romaji") or None
    english = titles_payload.get("english") or None
    native_title = titles_payload.get("native") or None

    # Prioritize native (Japanese) title for Japanese learners
    primary: Optional[str] = native_title or romaji or english

    return {
        "romaji": romaji,
        "english": english,
        "native": native_title,
        "primary": primary,
    }


def format_anilist_date(date_payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """Format AniList startDate payload into a concise string."""
    if not isinstance(date_payload, dict):
        return None

    year = date_payload.get("year")
    month = date_payload.get("month")
    day = date_payload.get("day")

    if not year:
        return None

    if month and day:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    if month:
        return f"{int(year):04d}-{int(month):02d}"
    return str(int(year))


def normalize_vn_titles(vn: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Extract primary, English, Japanese, and Romaji titles from VNDB payload."""
    titles = {
        "primary": vn.get("title"),
        "en": None,
        "ja": None,
        "romaji": None,
    }

    for entry in (vn.get("titles") or []):
        lang = entry.get("lang")
        main_title = entry.get("title")
        latin = entry.get("latin")

        if lang == "en" and main_title:
            titles["en"] = main_title
        elif lang == "ja" and main_title:
            titles["ja"] = main_title
            if latin:
                titles["romaji"] = latin
        elif lang in ("x-jat", "romaji") and (latin or main_title):
            if not titles["romaji"]:
                titles["romaji"] = latin or main_title

    if not titles["primary"]:
        titles["primary"] = titles["en"] or titles["ja"] or titles["romaji"]

    return titles

from .constants import NSFW_SEXUAL_THRESHOLD

logger = logging.getLogger(__name__)

try:
    import yt_dlp  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    yt_dlp = None

class VideoInfo:
    """Video information container"""
    
    def __init__(self, title: str, duration: int, thumbnail: str = "", 
                 channel_name: str = "", channel_id: str = "", 
                 video_id: str = "", platform: str = ""):
        self.title = title
        self.duration = duration  # in seconds
        self.thumbnail = thumbnail
        self.channel_name = channel_name
        self.channel_id = channel_id
        self.video_id = video_id
        self.platform = platform

class AnimeInfo:
    """Anime information container"""
    
    def __init__(self, title: str, episode_count: int = 0, 
                 thumbnail: str = "", anidb_id: str = "", 
                 sources: List[str] = None):
        self.title = title
        self.episode_count = episode_count
        self.thumbnail = thumbnail
        self.anidb_id = anidb_id
        self.sources = sources or []
def _normalize_sexual_value(value: Any) -> Optional[float]:
    """Convert VNDB image sexual flag to a comparable float scale.
    
    VNDB returns fractional values between 0 and 2, so the decimal
    precision is kept for the explicit-content threshold comparison.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        lowered = value.lower()
        mapping = {
            "safe": 0.0,
            "suggestive": 1.0,
            "questionable": 1.0,
            "explicit": 2.0,
            "nsfw": 2.0,
        }
        if lowered in mapping:
            return mapping[lowered]

        try:
            return float(lowered)
        except ValueError:
            return None

    return None


def is_image_explicit(sexual_value: Union[float, int, str, Dict[str, Any], None]) -> bool:
    """Return True if VNDB image metadata marks the cover as explicit.
    
    VNDB marks images as 'Explicit' below 2.0, not only at it.
    Their scale is: 0 = Safe, 1 = Suggestive, 2 = Explicit.
    In practice, images at or above NSFW_SEXUAL_THRESHOLD are shown with explicit warnings.
    
    Args:
        sexual_value: Can be a float/int (0-2), string ("safe"/"explicit"),
                     dict with 'sexual' key, or None
    """
    # Support both direct values and dict payload for backwards compatibility
    if isinstance(sexual_value, dict):
        sexual_value = sexual_value.get("sexual")
    
    normalized = _normalize_sexual_value(sexual_value)
    return normalized is not None and normalized >= NSFW_SEXUAL_THRESHOLD


def extract_sexual_level(image_payload: Dict[str, Any]) -> Optional[float]:
    """Extract and normalize sexual rating from VNDB image payload.
    
    Returns float between 0-2, or None if not available.
    """
    return _normalize_sexual_value(image_payload.get("sexual"))


class MediaSearcher:
    """Main media search and metadata service"""

    def __init__(
        self,
        vndb_client: Optional[Union[VNDBClient, Callable[[], VNDBClient]]] = None,
        anilist_client: Optional[Union[AniListClient, Callable[[], AniListClient]]] = None,
        igdb_client: Optional[Union[IGDBClient, Callable[[], IGDBClient]]] = None,
    ):
        if callable(vndb_client):
            self._vndb_client_factory: Optional[Callable[[], VNDBClient]] = vndb_client
            self._vndb_client_template: Optional[VNDBClient] = None
        else:
            self._vndb_client_factory = None
            self._vndb_client_template = vndb_client

        if callable(anilist_client):
            self._anilist_client_factory: Optional[Callable[[], AniListClient]] = anilist_client
            self._anilist_client_template: Optional[AniListClient] = None
        else:
            self._anilist_client_factory = None
            self._anilist_client_template = anilist_client if isinstance(anilist_client, AniListClient) else None
        
        if callable(igdb_client):
            self._igdb_client_factory: Optional[Callable[[], IGDBClient]] = igdb_client
            self._igdb_client_template: Optional[IGDBClient] = None
        else:
            self._igdb_client_factory = None
            self._igdb_client_template = igdb_client if isinstance(igdb_client, IGDBClient) else None
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    def _create_vndb_client(self) -> VNDBClient:
        if self._vndb_client_factory:
            client = self._vndb_client_factory()
            if isinstance(client, VNDBClient):
                return client
            raise TypeError("VNDB client factory must return a VNDBClient instance")

        if self._vndb_client_template:
            template = self._vndb_client_template
            return VNDBClient(client_name=template.client_name, client_version=template.client_version)

        return VNDBClient()

    def _create_anilist_client(self) -> AniListClient:
        if self._anilist_client_factory:
            client = self._anilist_client_factory()
            if isinstance(client, AniListClient):
                return client
            raise TypeError("AniList client factory must return an AniListClient instance")

        if self._anilist_client_template:
            template = self._anilist_client_template
            if isinstance(template, AniListClient):
                # Reuse provided instance when possible
                return template

        return AniListClient()
    
    def _create_igdb_client(self) -> IGDBClient:
        if self._igdb_client_factory:
            client = self._igdb_client_factory()
            if isinstance(client, IGDBClient):
                return client
            raise TypeError("IGDB client factory must return an IGDBClient instance")

        if self._igdb_client_template:
            template = self._igdb_client_template
            if isinstance(template, IGDBClient):
                # Reuse provided instance when possible
                return template

        return IGDBClient()

    async def search_visual_novel(self, query: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Search VNDB for visual novels and normalize the response."""
        try:
            client = self._create_vndb_client()
            async with client as api:
                raw_results = await api.search_vns(query, limit=limit)

            results = []
            for vn in raw_results:
                title_data = normalize_vn_titles(vn)
                image = (vn.get("image") or {})
                sexual_level_value = _normalize_sexual_value(image.get("sexual"))
                image_is_explicit = is_image_explicit(image)
                
                # Prioritize Japanese title for display (for Japanese learners)
                display_title = title_data.get("ja") or title_data.get("primary") or vn.get("title")
                
                results.append({
                    "id": vn.get("id"),
                    "titles": title_data,
                    "display": display_title,
                    "image": image.get("url"),
                    "image_is_nsfw": image_is_explicit,
                    "rating": vn.get("rating"),
                    "released": vn.get("released"),
                    "image_sexual": sexual_level_value,
                })

            return results

        except Exception as exc:  # pragma: no cover - network failure path
            logger.error("Error searching visual novels: %s", exc)
            return []
    
    def _normalize_anilist_media(
        self,
        media: Optional[Dict[str, Any]],
        media_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(media, dict):
            return None

        titles = normalize_anilist_titles(media)

        # Prioritize native (Japanese) titles for Japanese learners
        display_field: Optional[str] = None
        if titles.get("native"):
            display_field = "native"
        elif titles.get("romaji"):
            display_field = "romaji"
        elif titles.get("english"):
            display_field = "english"

        display_title = titles.get(display_field or "primary")

        cover = media.get("coverImage") or {}
        cover_url = cover.get("extraLarge") or cover.get("large") or cover.get("medium")

        is_adult = bool(media.get("isAdult"))

        normalized: Dict[str, Any] = {
            "id": media.get("id"),
            "mal_id": media.get("idMal"),
            "titles": titles,
            "display": display_title,
            "display_field": display_field,
            "media_type": media_type,
            "format": media.get("format"),
            "status": media.get("status"),
            "episodes": media.get("episodes"),
            "chapters": media.get("chapters"),
            "volumes": media.get("volumes"),
            "episode_duration": media.get("duration"),
            "average_score": media.get("averageScore"),
            "site_url": media.get("siteUrl"),
            "cover_url": cover_url,
            "cover_color": cover.get("color"),
            "is_adult": is_adult,
            "cover_is_nsfw": is_adult,
            "start_date": format_anilist_date(media.get("startDate")),
        }

        return normalized

    async def _search_anilist_media(self, query: str, media_type: str, limit: int = 25) -> List[Dict[str, Any]]:
        search_term = (query or "").strip()
        if not search_term:
            return []

        try:
            client = self._create_anilist_client()
            async with client as api:
                raw_results = await api.search_media(search_term, media_type, limit=limit)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.error("AniList %s search failed for '%s': %s", media_type, search_term, exc)
            return []

        results: List[Dict[str, Any]] = []
        for item in raw_results or []:
            normalized = self._normalize_anilist_media(item, media_type)
            if not normalized:
                continue

            # Skip explicit adult entries from autocomplete but keep metadata accessible elsewhere
            if normalized.get("is_adult"):
                continue

            if not normalized.get("display"):
                continue

            results.append(normalized)

        return results

    async def search_anime(self, query: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Search AniList for anime titles."""
        return await self._search_anilist_media(query, "ANIME", limit=limit)

    async def search_manga(self, query: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Search AniList for manga titles."""
        return await self._search_anilist_media(query, "MANGA", limit=limit)
    
    async def search_games(self, query: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Search IGDB for game titles."""
        search_term = (query or "").strip()
        if not search_term:
            return []
        
        try:
            client = self._create_igdb_client()
            async with client as api:
                raw_results = await api.search_games(search_term, limit=limit)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.error("IGDB game search failed for '%s': %s", search_term, exc)
            return []
        
        results: List[Dict[str, Any]] = []
        for game in raw_results or []:
            formatted = IGDBClient.format_game_result(game)
            if not formatted or not formatted.get("id"):
                continue
            
            # Build display label with release year if available
            display_parts = [formatted["name"]]
            if formatted.get("release_date"):
                try:
                    from datetime import datetime
                    year = datetime.fromtimestamp(formatted["release_date"]).year
                    display_parts.append(str(year))
                except:
                    pass
            
            formatted["display"] = " — ".join(display_parts)
            results.append(formatted)
        
        return results
    
    async def get_game(self, game_id: Union[int, str]) -> Optional[Dict[str, Any]]:
        """Get detailed game information from IGDB."""
        try:
            numeric_id = int(game_id)
        except (TypeError, ValueError):
            return None
        
        try:
            client = self._create_igdb_client()
            async with client as api:
                game = await api.get_game(numeric_id)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.error("IGDB game lookup failed for %s: %s", game_id, exc)
            return None
        
        if not game:
            return None
        
        return IGDBClient.format_game_result(game)

    async def get_anilist_media(self, media_id: Union[int, str], media_type: str) -> Optional[Dict[str, Any]]:
        try:
            numeric_id = int(media_id)
        except (TypeError, ValueError):
            return None

        try:
            client = self._create_anilist_client()
            async with client as api:
                payload = await api.get_media(numeric_id, media_type)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.error("AniList %s lookup failed for %s: %s", media_type, media_id, exc)
            return None

        if not payload:
            return None

        return self._normalize_anilist_media(payload, media_type)
    
    async def get_video_info(self, url: str) -> Optional[VideoInfo]:
        """Get video information from URL"""
        try:
            # Normalize URL - add https:// if no protocol is present
            normalized_url = url.strip()
            if not normalized_url.startswith(('http://', 'https://')):
                normalized_url = f"https://{normalized_url}"
            
            parsed_url = urlparse(normalized_url)
            
            if 'youtube.com' in parsed_url.netloc or 'youtu.be' in parsed_url.netloc:
                return await self._get_youtube_info(normalized_url)
            else:
                logger.warning(f"Unsupported video platform: {parsed_url.netloc}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting video info: {e}")
            return None
    
    async def _get_youtube_info(self, url: str) -> Optional[VideoInfo]:
        """Get YouTube video information"""
        try:
            return await _fetch_youtube_info(url)
            
        except Exception as e:
            logger.error(f"Error getting YouTube info: {e}")
            return None
    
    def _extract_youtube_id(self, url: str) -> Optional[str]:
        """Extract YouTube video ID from URL"""
        try:
            # Normalize URL - add https:// if no protocol is present
            normalized_url = url.strip()
            if not normalized_url.startswith(('http://', 'https://')):
                normalized_url = f"https://{normalized_url}"
            
            parsed = urlparse(normalized_url)
            
            if 'youtu.be' in parsed.netloc:
                return parsed.path[1:]  # Remove leading slash
            elif 'youtube.com' in parsed.netloc:
                if 'watch' in parsed.path:
                    query_params = parse_qs(parsed.query)
                    return query_params.get('v', [None])[0]
                elif 'embed' in parsed.path:
                    return parsed.path.split('/embed/')[-1]
            
            return None
            
        except Exception as e:
            logger.error(f"Error extracting YouTube ID: {e}")
            return None
    
    async def get_vndb_info(self, vndb_id: str) -> Optional[Dict[str, Any]]:
        """Fetch detailed VN data for logging or embeds."""
        try:
            client = self._create_vndb_client()
            async with client as api:
                return await api.get_vn_by_id(vndb_id)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.error("Error getting VNDB info: %s", exc)
            return None
    
    def create_autocomplete_value(self, item_id: Union[str, int], field: str, source: Optional[str] = None) -> str:
        """Create autocomplete value string with optional media source metadata."""
        id_part = str(item_id)
        if source:
            id_part = f"{source}|{id_part}"
        return f"${{{id_part}:{field}}}"

    def parse_autocomplete_value(self, value: str) -> Optional[Tuple[str, str, Optional[str]]]:
        """Parse autocomplete value into (item_id, field, source)."""
        if not value or not value.startswith("${") or not value.endswith("}"):
            return None

        try:
            content = value[2:-1]
            id_and_field = content.rsplit(':', 1)
            if len(id_and_field) != 2:
                return None

            id_part, field = id_and_field
            source: Optional[str] = None
            item_id = id_part

            if '|' in id_part:
                source, item_id = id_part.split('|', 1)

            return item_id, field, source

        except Exception as exc:  # pragma: no cover - defensive against malformed tokens
            logger.error("Error parsing autocomplete value: %s", exc)
            return None

class YouTubeAPI:
    """YouTube Data API integration (requires API key)"""
    
    def __init__(self, api_key: str = None):
        self.api_key = api_key
        self.base_url = "https://www.googleapis.com/youtube/v3"
    
    async def get_video_info(self, video_id: str) -> Optional[VideoInfo]:
        """Get video info from YouTube Data API"""
        if not self.api_key:
            logger.warning("YouTube API key not configured")
            return None
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/videos"
                params = {
                    'id': video_id,
                    'part': 'snippet,contentDetails',
                    'key': self.api_key
                }
                
                async with session.get(url, params=params) as response:
                    if response.status != 200:
                        logger.error(f"YouTube API error: {response.status}")
                        return None
                    
                    data = await response.json()
                    items = data.get('items', [])
                    
                    if not items:
                        logger.warning(f"No YouTube video found for ID: {video_id}")
                        return None
                    
                    item = items[0]
                    snippet = item.get('snippet', {})
                    content_details = item.get('contentDetails', {})
                    
                    # Parse duration from ISO 8601 format
                    duration_str = content_details.get('duration', 'PT0S')
                    duration_seconds = self._parse_iso8601_duration(duration_str)
                    
                    return VideoInfo(
                        title=snippet.get('title', ''),
                        duration=duration_seconds,
                        thumbnail=snippet.get('thumbnails', {}).get('maxresdefault', {}).get('url', ''),
                        channel_name=snippet.get('channelTitle', ''),
                        channel_id=snippet.get('channelId', ''),
                        video_id=video_id,
                        platform="youtube"
                    )
                    
        except Exception as e:
            logger.error(f"Error getting YouTube video info: {e}")
            return None
    
    def _parse_iso8601_duration(self, duration_str: str) -> int:
        """Parse ISO 8601 duration (PT1H2M3S) to seconds"""
        try:
            import re
            
            # Remove PT prefix
            duration_str = duration_str[2:] if duration_str.startswith('PT') else duration_str
            
            # Extract hours, minutes, seconds
            hours = 0
            minutes = 0
            seconds = 0
            
            # Hours
            h_match = re.search(r'(\d+)H', duration_str)
            if h_match:
                hours = int(h_match.group(1))
            
            # Minutes
            m_match = re.search(r'(\d+)M', duration_str)
            if m_match:
                minutes = int(m_match.group(1))
            
            # Seconds
            s_match = re.search(r'(\d+)S', duration_str)
            if s_match:
                seconds = int(s_match.group(1))
            
            return hours * 3600 + minutes * 60 + seconds
            
        except Exception as e:
            logger.error(f"Error parsing duration: {e}")
            return 0

async def _fetch_youtube_info(url: str) -> Optional[VideoInfo]:
    """Fetch enriched YouTube metadata using available integrations."""
    video_id = _extract_youtube_id(url)
    if not video_id:
        logger.warning("Unable to extract YouTube ID from URL: %s", url)
        return None

    # Try the configured YouTube Data API first if available
    if youtube_api:
        try:
            api_info = await youtube_api.get_video_info(video_id)
        except Exception as api_error:  # pragma: no cover - network call
            logger.error("YouTube API lookup failed for %s: %s", video_id, api_error)
            api_info = None

        if api_info:
            return api_info

    # Fall back to yt-dlp if available (no API key required)
    ytdlp_info = await _get_youtube_info_via_ytdlp(url, video_id)
    if ytdlp_info:
        return ytdlp_info

    # Try web scraping fallback when yt-dlp fails
    web_scrape_info = await _get_youtube_info_via_scraping(url, video_id)
    if web_scrape_info:
        return web_scrape_info

    # Final fallback to basic placeholder to avoid breaking the flow
    logger.warning("Falling back to placeholder metadata for video %s", video_id)
    return VideoInfo(
        title=f"YouTube Video {video_id}",
        duration=1440,
        thumbnail=f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
        channel_name="YouTube",
        channel_id="",
        video_id=video_id,
        platform="youtube"
    )


async def _get_youtube_info_via_ytdlp(url: str, video_id: str) -> Optional[VideoInfo]:
    """Use yt-dlp to pull YouTube metadata without requiring API access."""
    if yt_dlp is None:
        return None

    def _extract() -> Optional[Dict[str, Any]]:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            # Try to bypass YouTube bot detection
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "web"],
                    "player_skip": ["configs"],
                }
            },
            # Add headers to look more like a real browser
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info

    try:
        info_dict = await asyncio.to_thread(_extract)
    except Exception as e:  # pragma: no cover - best effort fallback
        logger.warning("yt-dlp metadata extraction failed for %s: %s", video_id, e)
        return None

    if not info_dict:
        return None

    duration = info_dict.get("duration")
    duration_seconds = int(duration) if isinstance(duration, (int, float)) else 0

    thumbnail = info_dict.get("thumbnail")
    if not thumbnail:
        thumbnails = info_dict.get("thumbnails") or []
        if isinstance(thumbnails, list) and thumbnails:
            # Choose the highest resolution thumbnail available
            best_thumb = max(thumbnails, key=lambda item: item.get("height", 0))
            thumbnail = best_thumb.get("url")

    return VideoInfo(
        title=info_dict.get("title") or f"YouTube Video {video_id}",
        duration=duration_seconds if duration_seconds > 0 else 0,
        thumbnail=thumbnail or f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
        channel_name=info_dict.get("channel") or info_dict.get("uploader") or "YouTube",
        channel_id=info_dict.get("channel_id") or info_dict.get("uploader_id") or "",
        video_id=info_dict.get("id") or video_id,
        platform="youtube"
    )


async def _get_youtube_info_via_scraping(url: str, video_id: str) -> Optional[VideoInfo]:
    """Fallback web scraping method to get basic YouTube metadata."""
    try:
        import aiohttp
        import re
        import json
        from html import unescape
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        # Scrape the canonical watch URL built from the validated video_id, so a
        # youtu.be/consent redirect isn't needed. Redirects stay disabled so the
        # request only reaches the host already allowlisted by get_video_info.
        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(canonical_url, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False) as response:
                if response.status != 200:
                    return None
                    
                html = await response.text()
                
                # Extract title from page title or og:title meta tag
                title = None
                title_match = re.search(r'<title>([^<]+)</title>', html)
                if title_match:
                    title = unescape(title_match.group(1))
                    # Remove " - YouTube" suffix if present
                    title = re.sub(r'\s*-\s*YouTube\s*$', '', title)
                
                if not title:
                    # Try og:title meta tag
                    og_title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                    if og_title_match:
                        title = unescape(og_title_match.group(1))
                
                # Extract channel name from various possible locations
                channel_name = None
                
                # Try to find channel name in JSON-LD structured data
                json_ld_matches = re.findall(r'<script type="application/ld\+json">([^<]+)</script>', html)
                for json_ld_text in json_ld_matches:
                    try:
                        json_data = json.loads(json_ld_text)
                        if isinstance(json_data, dict):
                            author = json_data.get('author')
                            if isinstance(author, dict):
                                channel_name = author.get('name')
                                if channel_name:
                                    break
                    except:
                        continue
                
                # Try to find channel name in meta tags
                if not channel_name:
                    channel_match = re.search(r'<meta name="author" content="([^"]+)"', html)
                    if channel_match:
                        channel_name = unescape(channel_match.group(1))
                
                # Try to extract duration from structured data
                duration_seconds = 0
                for json_ld_text in json_ld_matches:
                    try:
                        json_data = json.loads(json_ld_text)
                        if isinstance(json_data, dict):
                            duration_str = json_data.get('duration')
                            if duration_str:
                                # Parse ISO 8601 duration format (PT1H2M30S)
                                duration_match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
                                if duration_match:
                                    hours = int(duration_match.group(1) or 0)
                                    minutes = int(duration_match.group(2) or 0)
                                    seconds = int(duration_match.group(3) or 0)
                                    duration_seconds = hours * 3600 + minutes * 60 + seconds
                                    break
                    except:
                        continue
                
                if title and channel_name:
                    return VideoInfo(
                        title=title,
                        duration=duration_seconds,
                        thumbnail=f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
                        channel_name=channel_name,
                        channel_id="",
                        video_id=video_id,
                        platform="youtube"
                    )
                    
    except Exception as e:
        logger.warning(f"Web scraping fallback failed for {video_id}: {e}")
    
    return None


# Global instance for YouTube API (will be initialized with API key if available)
youtube_api: Optional[YouTubeAPI] = None

def enable_youtube_api(api_key: str):
    """Enable YouTube API with the provided API key"""
    global youtube_api
    youtube_api = YouTubeAPI(api_key)
    logger.info("YouTube API enabled")

# Exact host allowlist for outbound video fetches. /log_video takes a free-text
# URL from any member, so the host must be validated before any server-side fetch.
_YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be",
})


def _is_allowed_youtube_url(parsed) -> bool:
    """Validate against an exact hostname allowlist (not a netloc substring) and
    reject embedded userinfo, since text before an '@' is not the host that
    receives the request."""
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.hostname or "").lower() in _YOUTUBE_HOSTS


async def get_video_info(url: str) -> Optional[VideoInfo]:
    """Get video info, using available integrations for richer metadata."""
    try:
        # Normalize URL - add https:// if no protocol is present
        normalized_url = url.strip()
        if not normalized_url.startswith(('http://', 'https://')):
            normalized_url = f"https://{normalized_url}"

        parsed_url = urlparse(normalized_url)

        if _is_allowed_youtube_url(parsed_url):
            return await _fetch_youtube_info(normalized_url)

        logger.warning("Unsupported or disallowed video URL host: %s", parsed_url.netloc)
        return None

    except Exception as e:
        logger.error(f"Error getting video info: {e}")
        return None

def _extract_youtube_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from URL"""
    try:
        # Normalize URL - add https:// if no protocol is present
        normalized_url = url.strip()
        if not normalized_url.startswith(('http://', 'https://')):
            normalized_url = f"https://{normalized_url}"
        
        parsed = urlparse(normalized_url)

        host = (parsed.hostname or "").lower()
        if host in ("youtu.be", "www.youtu.be"):
            return parsed.path[1:]  # Remove leading slash
        elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
            if 'watch' in parsed.path:
                query_params = parse_qs(parsed.query)
                return query_params.get('v', [None])[0]
            elif 'embed' in parsed.path:
                return parsed.path.split('/embed/')[-1]

        return None
        
    except Exception as e:
        logger.error(f"Error extracting YouTube ID: {e}")
        return None