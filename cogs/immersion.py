"""
Immersion tracking cog for VNCRbot
Implements time-focused immersion tracking similar to botsu bot
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union
import asyncio
import io
import aiohttp

from utils.database import Database
from utils.immersion_models import (
    Activity,
    ActivityType,
    MediaType,
    Goal,
    LeaderboardEntry,
    parse_duration_complex,
)
from utils.immersion_repository import ActivityRepository, GoalRepository, UserRepository
from utils.immersion_services import UserTimeService, GoalService, GuildRepository
from utils.immersion_media import (
    MediaSearcher,
    VideoInfo,
    extract_sexual_level,
    get_video_info,
    is_image_explicit,
    normalize_vn_titles,
)
from utils.class_progress_bar import render_class_progress_bar
from utils.profile_card import ProfileCardGenerator
from utils.helpers import extract_vndb_id
from utils.jiten_client import JitenClient

logger = logging.getLogger(__name__)


async def safe_defer(interaction: discord.Interaction, ephemeral: bool = False) -> bool:
    """
    Safely defer an interaction, handling timeout gracefully.
    Returns True if defer succeeded, False if interaction expired.
    """
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except discord.errors.NotFound:
        logger.warning(f"Interaction expired before defer for user {interaction.user.id}")
        return False


async def safe_followup(interaction: discord.Interaction, content: str = None,
                        embed: discord.Embed = None, ephemeral: bool = False, **kwargs) -> bool:
    """
    Safely send a followup message, handling expired interactions.
    Returns True if send succeeded, False otherwise.
    """
    try:
        await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral, **kwargs)
        return True
    except discord.errors.NotFound:
        logger.warning(f"Could not send followup - interaction/webhook expired")
        return False


MEDIA_ACTIVITY_COMPATIBILITY = {
    MediaType.BOOK.value: ActivityType.READING.value,
    MediaType.MANGA.value: ActivityType.READING.value,
    MediaType.VISUAL_NOVEL.value: ActivityType.READING.value,
    MediaType.ANIME.value: ActivityType.LISTENING.value,
    MediaType.VIDEO.value: ActivityType.LISTENING.value,
}

LEADERBOARD_GUILD_FILTERS: Dict[str, str] = {
    "1331707580757704795": "VNCR (Visual Novel Club Resurrection)",
    "1415732490047324192": "LJTA (Learn Japanese Through Anime)",
}

EXTRA_GUILD_FILTERS_ENV_KEY = "VNCRBOT_EXTRA_GUILD_FILTERS"


def _extend_leaderboard_filters_from_env() -> None:
    raw = os.getenv(EXTRA_GUILD_FILTERS_ENV_KEY)
    if not raw:
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in %s; ignoring extra guild filters", EXTRA_GUILD_FILTERS_ENV_KEY)
        return

    if not isinstance(data, dict):
        logger.warning("Expected dict for %s payload; ignoring", EXTRA_GUILD_FILTERS_ENV_KEY)
        return

    for guild_id, label in data.items():
        guild_id_str = str(guild_id)
        label_str = str(label)
        if guild_id_str not in LEADERBOARD_GUILD_FILTERS:
            LEADERBOARD_GUILD_FILTERS[guild_id_str] = label_str


_extend_leaderboard_filters_from_env()


def _build_leaderboard_guild_choices() -> List[app_commands.Choice[str]]:
    """Build guild filter choices. Note: This is called at module load time,
    so it includes all configured guilds. The command will filter at runtime
    to only guilds the bot is actually in."""
    return [
        app_commands.Choice(name=label, value=guild_id)
        for guild_id, label in LEADERBOARD_GUILD_FILTERS.items()
    ]

# Date format help text for command descriptions
DATE_FORMAT_HELP = "Date of activity, e.g. 'yesterday', '-2', '6/15'. Defaults to today. Limited to last 7 days."

DEFAULT_CLASS_ROLE_DEFINITIONS: List[Dict[str, Any]] = [
    {"name": "魑魅魍魎", "min_hours": 0},
    {"name": "鬼", "min_hours": 1 / 3600},  # First log triggers upgrade from base role
    {"name": "餓鬼", "min_hours": 10},
    {"name": "邪鬼", "min_hours": 25},
    {"name": "幽鬼", "min_hours": 50},
    {"name": "霊鬼", "min_hours": 100},
    {"name": "妖鬼", "min_hours": 200},
    {"name": "怪鬼", "min_hours": 400},
    {"name": "冥鬼", "min_hours": 700},
    {"name": "吸血鬼", "min_hours": 1000},
    {"name": "魔鬼", "min_hours": 1500},
    {"name": "神鬼", "min_hours": 2000},
]

ANIME_CLASS_ROLE_DEFINITIONS: List[Dict[str, Any]] = [
    {"name": "人間", "min_hours": 0},       # ordinary human
    {"name": "才人", "min_hours": 1},       # talented person, natural first "upgrade"
    {"name": "名人", "min_hours": 10},      # master/expert, skill polished
    {"name": "賢人", "min_hours": 25},      # wise person, intellect, insight
    {"name": "貴人", "min_hours": 50},      # noble person, elevated social/spiritual rank
    {"name": "偉人", "min_hours": 100},     # great person, historically or morally great
    {"name": "傑人", "min_hours": 200},     # outstanding person, surpassing others
    {"name": "達人", "min_hours": 400},     # brilliant hero-like individual
    {"name": "聖人", "min_hours": 700},     # saint, moral and spiritual perfection
    {"name": "超人", "min_hours": 1000},    # superhuman, transcends normal limits
    {"name": "天人", "min_hours": 1500},    # celestial being, divine class
    {"name": "神人", "min_hours": 2000}     # godly being, ultimate transcendence
]

DEFAULT_SPECIAL_RANK_ROLES: List[Dict[str, Any]] = [
    {"name": "天上天下唯我独尊", "scope": "all_time", "position": 1, "limit": 1},
    {"name": "魔王", "scope": "prev_month", "position": 1, "limit": 1},
    {"name": "四天王", "scope": "prev_month", "position": 2, "limit": 4},
]


def _format_activity_types(activity: Activity) -> Optional[str]:
    """Return display string for activity/media type details."""
    details: List[str] = []

    primary = getattr(activity, "primary_type", None)
    if primary:
        details.append(primary.replace('_', ' ').title())

    media = getattr(activity, "media_type", None)
    if media:
        details.append(media.replace('_', ' ').title())

    if not details:
        return None

    return " • ".join(details)


def _format_duration_value(seconds: int) -> str:
    """Convert raw seconds into the embed-friendly display string."""
    if seconds is None or seconds <= 0:
        return "Not logged"

    if seconds < 60:
        return f"{seconds}s"

    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        if remaining_seconds == 0:
            return f"{minutes}m"
        return f"{minutes}m {remaining_seconds}s"

    hours, remaining_minutes = divmod(minutes, 60)
    if remaining_minutes == 0:
        return f"{hours}h"
    return f"{hours}h {remaining_minutes}m"

def _format_hours_display(hours: float) -> str:
    """Format hours as 'Xh Ym' for display (e.g., 10.8 -> '10h 48m')."""
    total_seconds = int(hours * 3600)
    return _format_duration_value(total_seconds)


def _render_log_progress_bar(progress_ratio: float, length: int = 12) -> str:
    """Text tier-progress bar, used when the rendered image is unavailable.
    Eighth-block resolution so a short session still visibly moves the bar."""
    progress_ratio = min(max(progress_ratio, 0.0), 1.0)
    partials = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")
    total_eighths = round(progress_ratio * length * 8)
    if progress_ratio > 0 and total_eighths == 0:
        total_eighths = 1
    cells = []
    for i in range(length):
        fill = min(max(total_eighths - i * 8, 0), 8)
        if fill == 0:
            cells.append("░")
        elif fill < 8:
            cells.append(partials[fill])
        else:
            cells.append("█")
    return "".join(cells)


def _format_gained_pct(gained_ratio: float) -> str:
    """Session gain as a percent string; tiny-but-real gains show '<0.1' not '0.0'."""
    pct = gained_ratio * 100
    if 0 < pct < 0.05:
        return "<0.1"
    return f"{pct:.1f}"

def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_timespan(delta: timedelta) -> str:
    total_seconds = int(abs(delta.total_seconds()))
    if total_seconds <= 0:
        return "0s"

    periods = [
        ("y", 60 * 60 * 24 * 365),
        ("mo", 60 * 60 * 24 * 30),
        ("d", 60 * 60 * 24),
        ("h", 60 * 60),
        ("m", 60),
    ]

    parts: List[str] = []
    for suffix, length in periods:
        if total_seconds < length:
            continue
        value, total_seconds = divmod(total_seconds, length)
        if value:
            parts.append(f"{value}{suffix}")
        if len(parts) >= 2:
            break

    if not parts:
        parts.append(f"{total_seconds}s")

    return " ".join(parts)


def _format_relative_time(target: Optional[datetime], now: datetime) -> str:
    normalized = _ensure_utc(target)
    if not normalized:
        return "never"

    normalized_now = _ensure_utc(now) or datetime.now(timezone.utc)
    delta = normalized_now - normalized
    suffix = "ago"
    if delta.total_seconds() < 0:
        delta = -delta
        suffix = "from now"

    return f"{_format_timespan(delta)} {suffix}"


def _clean_activity_name(name: str) -> str:
    """Clean up activity names that may have autocomplete display format embedded.
    
    Format examples to clean:
    - "Title — YYYY-MM-DD • rating/10" -> "Title"
    - "Title — Type • Status • Score%" -> "Title"
    """
    if not name:
        return name
    
    # Check if this looks like an autocomplete display value
    if " — " in name and ("•" in name or "/" in name):
        # Extract the title part before the " — " separator
        title_part = name.split(" — ")[0].strip()
        if title_part:
            return title_part
    
    return name

def _format_history_entry(activity: Activity) -> str:
    """Build a readable history block for an activity."""
    raw_title = activity.name if activity.name else "(Untitled)"
    # Clean up the title in case it has autocomplete format embedded
    title = _clean_activity_name(raw_title)
    # Convert to Unix timestamp for Discord's timezone-aware formatting
    unix_timestamp = int(activity.date.timestamp())
    # Discord timestamp format: <t:timestamp:f> shows "Month Day, Year Hour:Minute AM/PM" in user's timezone
    timestamp_display = f"<t:{unix_timestamp}:f>"
    lines = [
        f"**{title}**",
        timestamp_display,
        f"🕒 Duration: {_format_duration_value(activity.duration)}"
    ]

    activity_types = _format_activity_types(activity)
    if activity_types:
        lines.append(f"📚 Type: {activity_types}")

    characters = activity.get_meta('characters')
    if isinstance(characters, (int, float)) and characters > 0:
        lines.append(f"🔡 Characters: {int(characters):,}")

    pages = activity.get_meta('pages')
    if isinstance(pages, (int, float)) and pages > 0:
        # Format pages with up to 2 decimal places, removing trailing zeros
        formatted_pages = f"{pages:,.2f}".rstrip('0').rstrip('.')
        lines.append(f"📄 Pages: {formatted_pages}")

    if activity.media_type == MediaType.ANIME.value:
        episodes_logged = activity.get_meta('episodes')
        if isinstance(episodes_logged, (int, float)) and episodes_logged > 0:
            lines.append(f"📺 Episodes: {int(episodes_logged)}")

    # Add comment indicator if present
    if activity.comment:
        lines.append(f"💭 Use `/log_details activity_id:{activity.id}` to view comment")

    return "\n".join(lines)


class UndoLogView(discord.ui.View):
    """View with an Undo button to delete a recently logged activity"""
    
    def __init__(self, activity_id: int, user_id: int, cog: 'ImmersionCog'):
        super().__init__(timeout=300)  # 5 minute timeout
        self.activity_id = activity_id
        self.user_id = user_id
        self.cog = cog
    
    @discord.ui.button(label="Undo Log", style=discord.ButtonStyle.danger, emoji="↩️")
    async def undo_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle the undo button press"""
        # Check if the user pressing the button is the one who created the log
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ You can only undo your own logs!",
                ephemeral=True
            )
            return
        
        # Delete the activity
        success = await self.cog.activity_repo.delete_activity(self.activity_id, str(self.user_id))
        
        if success:
            # Disable the button
            button.disabled = True
            button.label = "Undone"
            button.style = discord.ButtonStyle.secondary
            await interaction.response.edit_message(view=self)
            
            # Send confirmation
            await interaction.followup.send(
                f"✅ Activity ID `{self.activity_id}` has been deleted.",
                ephemeral=True
            )
            
            # Re-sync roles in case this affects class progression
            try:
                await self.cog._sync_roles_and_notify(
                    interaction,
                    source="undo_log",
                    actor=interaction.user,
                )
            except Exception as e:
                logger.error(f"Error syncing roles after undo: {e}")
        else:
            await interaction.response.send_message(
                f"❌ Failed to delete activity ID `{self.activity_id}`. It may have already been deleted.",
                ephemeral=True
            )
    
    async def on_timeout(self):
        """Disable the button when the view times out"""
        self.undo_button.disabled = True
        self.undo_button.label = "Expired"
        self.undo_button.style = discord.ButtonStyle.secondary


class ImmersionCog(commands.Cog):
    """Immersion tracking functionality"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db = Database()
        self._display_name_cache: Dict[int, str] = {}
        self._membership_lookup_cache: Dict[Tuple[int, int], bool] = {}
        
        # Initialize repositories and services
        self.activity_repo = ActivityRepository(self.db)
        self.user_repo = UserRepository(self.db)
        self.goal_repo = GoalRepository(self.db)
        self.guild_repo = GuildRepository(self.db)
        
        # Initialize services
        self.time_service = UserTimeService(self.user_repo, self.guild_repo)
        self.goal_service = GoalService(self.goal_repo, self.time_service, self.activity_repo)
        self.media_searcher = MediaSearcher()

        self._default_class_role_definitions = self._normalize_class_role_definitions(DEFAULT_CLASS_ROLE_DEFINITIONS)
        self._default_rank_role_definitions = self._normalize_rank_role_definitions(DEFAULT_SPECIAL_RANK_ROLES)
        self.role_sync_lock = asyncio.Lock()
        self._initial_sync_task: Optional[asyncio.Task] = None
    
    async def cog_load(self):
        """Initialize the cog"""
        await self.db.initialize()
        logger.info("Immersion tracking cog loaded")

        if not self.class_role_sync.is_running():
            self.class_role_sync.start()

        if not self._initial_sync_task:
            self._initial_sync_task = asyncio.create_task(self._initial_role_sync())

        # Sync guild information for cross-bot compatibility
        asyncio.create_task(self._sync_guild_info())

    async def cog_unload(self):
        if self.class_role_sync.is_running():
            self.class_role_sync.cancel()

        if self._initial_sync_task:
            self._initial_sync_task.cancel()
            self._initial_sync_task = None

        logger.info("Immersion tracking cog unloaded")

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """Reply to cooldown hits ephemerally; leave everything else to the global handler."""
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Slow down. Try again in {error.retry_after:.1f}s."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except discord.HTTPException:
                pass
            return
        # Don't re-raise: discord.py invokes the tree's global on_error (error_logging)
        # independently, so re-raising here double-dispatches and emits a spurious
        # "Task exception was never retrieved" traceback.
        return

    async def _is_user_member_of_guild(
        self,
        guild: Optional[discord.Guild],
        user_id: Union[int, str],
        cache: Optional[Dict[Tuple[int, int], bool]] = None,
        guild_id: Optional[int] = None,
    ) -> bool:
        resolved_guild_id: Optional[int] = None
        if guild is not None:
            resolved_guild_id = guild.id
        elif guild_id is not None:
            resolved_guild_id = guild_id

        if resolved_guild_id is None:
            return False

        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            return False

        cache_store = cache if cache is not None else self._membership_lookup_cache
        cache_key = (resolved_guild_id, user_id_int)
        if cache_store is not None and cache_key in cache_store:
            return cache_store[cache_key]

        member = None
        if guild is not None:
            member = guild.get_member(user_id_int)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id_int)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None

        is_member = bool(member and not member.bot)

        if not is_member:
            try:
                member_payload = await self.bot.http.get_member(resolved_guild_id, user_id_int)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member_payload = None
            else:
                user_payload = member_payload.get("user", {}) if isinstance(member_payload, dict) else {}
                is_member = not user_payload.get("bot", False)

        if cache_store is not None:
            cache_store[cache_key] = is_member

        return is_member

    async def _resolve_display_name(
        self,
        user_id: Union[int, str],
        primary_guild: Optional[discord.Guild] = None,
    ) -> str:
        """Return the best available display name for the given user ID across shared guilds."""
        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            return f"User {user_id}"

        cached = self._display_name_cache.get(user_id_int)
        if cached:
            return cached

        candidate_guilds: List[discord.Guild] = []
        if primary_guild is not None:
            candidate_guilds.append(primary_guild)

        for guild in getattr(self.bot, "guilds", []):
            if guild not in candidate_guilds:
                candidate_guilds.append(guild)

        for guild in candidate_guilds:
            member = guild.get_member(user_id_int)
            if member:
                display_name = member.display_name
                self._display_name_cache[user_id_int] = display_name
                return display_name

        user = self.bot.get_user(user_id_int)
        if user:
            display_name = getattr(user, "display_name", getattr(user, "name", f"User {user_id_int}"))
            self._display_name_cache[user_id_int] = display_name
            return display_name

        try:
            user = await self.bot.fetch_user(user_id_int)
        except Exception:
            user = None

        if user:
            display_name = getattr(user, "display_name", getattr(user, "name", f"User {user_id_int}"))
            self._display_name_cache[user_id_int] = display_name
            return display_name

        fallback = f"User {user_id_int}"
        self._display_name_cache[user_id_int] = fallback
        return fallback
    
    async def _fetch_vn_details(self, vn_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Fetch VN details with database caching."""
        if not vn_id:
            return None

        try:
            cached = await self.db.get_cached_vndb_data(vn_id)
        except Exception as exc:
            logger.warning("Failed to load cached VNDB data for %s: %s", vn_id, exc)
            cached = None

        if cached:
            return cached

        vn_data = await self.media_searcher.get_vndb_info(vn_id)
        if vn_data:
            try:
                await self.db.cache_vndb_data(vn_id, vn_data)
            except Exception as exc:
                logger.warning("Failed to cache VNDB data for %s: %s", vn_id, exc)

        return vn_data

    @staticmethod
    def _map_vn_title_key(preferred: Optional[str]) -> Optional[str]:
        if not preferred:
            return None

        key = preferred.lower()
        if key in {"ja", "jp"}:
            return "ja"
        if key in {"en", "eng", "english"}:
            return "en"
        if key in {"romaji", "x-jat"}:
            return "romaji"
        if key == "primary":
            return "primary"

        return None

    def _select_vn_display_title(self, titles: Dict[str, Optional[str]], preferred: Optional[str]) -> Optional[str]:
        key_priority: List[str] = []

        mapped = self._map_vn_title_key(preferred)
        if mapped:
            key_priority.append(mapped)

        # Prioritize Japanese titles for Japanese learners
        key_priority.extend(["ja", "primary", "romaji", "en"])

        seen = set()
        for key in key_priority:
            if key in seen:
                continue
            seen.add(key)
            title = titles.get(key)
            if title:
                return title

        for title in titles.values():
            if title:
                return title

        return None

    async def _build_vn_context(self, raw_value: str) -> Dict[str, Any]:
        """Resolve VNDB metadata for a log entry."""
        sanitized_input = (raw_value or "").strip()

        context: Dict[str, Any] = {
            "display_name": sanitized_input or raw_value,
            "meta": {"vn_input": raw_value},
            "vndb_id": None,
            "vndb_url": None,
            "cover_url": None,
            "cover_is_nsfw": False,
            "jiten_deck_id": None,
            "rating": None,
            "length": None,
            "released": None,
            "titles": {},
        }

        vn_id = None
        preferred_key = None

        selection = self.media_searcher.parse_autocomplete_value(raw_value)
        if selection:
            vn_id, preferred_key, source = selection
            if source and not source.lower().startswith("vndb"):
                vn_id = None
                preferred_key = None
        else:
            # Check if this looks like an autocomplete display value that got replaced by Discord
            # Format: "Title — YYYY-MM-DD • rating/10 [vXXXXX]"

            # First try to extract VN ID from [vXXXXX] pattern (most reliable)
            vn_id_match = re.search(r'\[v(\d+)\]', sanitized_input)
            if vn_id_match:
                vn_id = f"v{vn_id_match.group(1)}"
                logger.info(f"Recovered VN ID from display format: {vn_id}")
            elif " — " in sanitized_input and ("•" in sanitized_input or "/" in sanitized_input):
                # Fall back to title search for legacy autocomplete values without [vXXXXX]
                # Extract the title part before the " — " separator
                title_part = sanitized_input.split(" — ")[0].strip()
                if title_part:
                    # Try to search VNDB for this exact title
                    try:
                        search_results = await self.media_searcher.search_visual_novel(title_part, limit=5)
                        if search_results:
                            # Use the first result (best match)
                            first_match = search_results[0]
                            vn_id = first_match.get("id")
                            # Try to match the title to determine preferred key
                            titles = first_match.get("titles") or {}
                            display_title = first_match.get("display")
                            for key, title_value in titles.items():
                                if title_value == display_title:
                                    if key == "en":
                                        preferred_key = "en"
                                    elif key == "ja":
                                        preferred_key = "jp"
                                    elif key == "romaji":
                                        preferred_key = "romaji"
                                    break
                            logger.info(f"Recovered VN from autocomplete display format (legacy): {title_part} -> {vn_id}")
                    except Exception as e:
                        logger.warning(f"Failed to recover VN from display format: {e}")
            
            # If still no vn_id, try extracting VNDB ID from input
            if not vn_id:
                extracted = extract_vndb_id(sanitized_input)
                if extracted:
                    vn_id = extracted.lower()

        if vn_id and not vn_id.startswith("v"):
            vn_id = f"v{vn_id}"

        if vn_id:
            vn_data = await self._fetch_vn_details(vn_id)
            if vn_data:
                titles = normalize_vn_titles(vn_data)
                display_title = self._select_vn_display_title(titles, preferred_key)

                if display_title:
                    context["display_name"] = display_title

                image_info = vn_data.get("image") or {}
                image_url = image_info.get("url")
                sexual_level = extract_sexual_level(image_info)
                cover_is_explicit = is_image_explicit(image_info)

                context.update({
                    "vndb_id": vn_id,
                    "vndb_url": f"https://vndb.org/{vn_id}",
                    "rating": vn_data.get("rating"),
                    "length": vn_data.get("length"),
                    "released": vn_data.get("released"),
                    "titles": titles,
                })

                if image_url:
                    context["cover_url"] = image_url  # Always set the URL
                    context["cover_is_nsfw"] = cover_is_explicit

                # Prefer the jiten.moe cover whenever the VN is on jiten: it is
                # cropped and always SFW, so it sidesteps the NSFW gate entirely
                # (VNs not on jiten keep the VNDB cover and its explicit-cover check).
                try:
                    async with JitenClient() as jiten:
                        jinfo = await jiten.get_by_vndb_id(vn_id)
                    if jinfo:
                        context["jiten_deck_id"] = jinfo.deck_id
                        if jinfo.cover_url:
                            context["cover_url"] = jinfo.cover_url
                            context["cover_is_nsfw"] = False
                except Exception as e:
                    logger.warning("jiten lookup failed for %s: %s", vn_id, e)

                vn_meta = {
                    "id": vn_id,
                    "url": context["vndb_url"],
                    "titles": titles,
                    "rating": vn_data.get("rating"),
                    "length": vn_data.get("length"),
                    "released": vn_data.get("released"),
                    "image_url": image_url,
                    "image_sexual": sexual_level,
                    "preferred_field": preferred_key,
                }

                context["meta"]["vn"] = vn_meta
            else:
                context["meta"]["vn_lookup_failed"] = True

        return context

    @staticmethod
    def _extract_anilist_id(text: str, expected_type: Optional[str] = None) -> Optional[int]:
        if not text:
            return None

        pattern = re.compile(r"anilist\.co/(anime|manga)/(\d+)", re.IGNORECASE)
        match = pattern.search(text)
        if match:
            media_category = match.group(1).upper()
            if not expected_type or media_category == expected_type.upper():
                return int(match.group(2))
            return None

        pattern = re.compile(r"(?:anilist|al)\s*[:#/](\d+)", re.IGNORECASE)
        match = pattern.search(text)
        if match:
            return int(match.group(1))

        return None

    async def _build_anilist_context(self, media_type: str, raw_value: str) -> Dict[str, Any]:
        sanitized_input = (raw_value or "").strip()
        normalized_type = (media_type or "").upper()

        context: Dict[str, Any] = {
            "display_name": sanitized_input or raw_value,
            "meta": {"anilist_input": raw_value},
            "anilist_id": None,
            "anilist_url": None,
            "cover_url": None,
            "cover_is_nsfw": False,
            "titles": {},
            "format": None,
            "status": None,
            "average_score": None,
            "total_episodes": None,
            "total_chapters": None,
            "total_volumes": None,
            "episode_duration": None,
            "start_date": None,
        }

        selection = self.media_searcher.parse_autocomplete_value(raw_value)
        media_id: Optional[str] = None
        preferred_field: Optional[str] = None

        if selection:
            candidate_id, selected_field, source = selection
            source_valid = source and source.lower().startswith("anilist")
            declared_type: Optional[str] = None
            if source and ':' in source:
                declared_type = source.split(':', 1)[1]

            if source_valid and (not declared_type or declared_type.upper() == normalized_type):
                media_id = candidate_id
                preferred_field = selected_field
            else:
                preferred_field = None
        else:
            # Check if this looks like an autocomplete display value that got replaced by Discord
            # Format examples: "Title — Type • Status • Score%" or "Title — Type • Episodes"
            if " — " in sanitized_input and ("•" in sanitized_input or normalized_type in sanitized_input.upper()):
                # Extract the title part before the " — " separator
                title_part = sanitized_input.split(" — ")[0].strip()
                if title_part:
                    # Try to search AniList for this title
                    try:
                        if normalized_type == "ANIME":
                            search_results = await self.media_searcher.search_anime(title_part, limit=5)
                        elif normalized_type == "MANGA":
                            search_results = await self.media_searcher.search_manga(title_part, limit=5)
                        else:
                            search_results = []
                        
                        if search_results:
                            # Use the first result (best match)
                            first_match = search_results[0]
                            media_id = str(first_match.get("id"))
                            # Try to match the title to determine preferred field
                            titles = first_match.get("titles") or {}
                            display_title = first_match.get("display")
                            for key, title_value in titles.items():
                                if title_value == display_title:
                                    preferred_field = key
                                    break
                            logger.info(f"Recovered {normalized_type} from autocomplete display format: {title_part} -> {media_id}")
                    except Exception as e:
                        logger.warning(f"Failed to recover {normalized_type} from display format: {e}")

        if media_id is None:
            extracted = self._extract_anilist_id(sanitized_input, normalized_type)
            if extracted is not None:
                media_id = str(extracted)

        if media_id:
            media_data = await self.media_searcher.get_anilist_media(media_id, normalized_type)
            if media_data:
                titles = media_data.get("titles") or {}
                # Prioritize native (Japanese) title for Japanese learners when no specific preference
                if preferred_field:
                    preferred_key = preferred_field
                else:
                    # Default to native → romaji → english for Japanese learners
                    preferred_key = "native" if titles.get("native") else (media_data.get("display_field") or "primary")
                preferred_key_lower = preferred_key.lower() if isinstance(preferred_key, str) else None

                display_title = (
                    titles.get(preferred_key_lower)
                    if preferred_key_lower and isinstance(titles, dict)
                    else None
                )
                if not display_title:
                    display_title = media_data.get("display") or context["display_name"]

                if display_title:
                    context["display_name"] = display_title

                cover_url = media_data.get("cover_url")
                cover_is_nsfw = bool(media_data.get("cover_is_nsfw"))

                context.update({
                    "anilist_id": media_data.get("id"),
                    "anilist_url": media_data.get("site_url"),
                    "titles": titles,
                    "format": media_data.get("format"),
                    "status": media_data.get("status"),
                    "average_score": media_data.get("average_score"),
                    "total_episodes": media_data.get("episodes"),
                    "total_chapters": media_data.get("chapters"),
                    "total_volumes": media_data.get("volumes"),
                    "episode_duration": media_data.get("episode_duration"),
                    "start_date": media_data.get("start_date"),
                    "cover_is_nsfw": cover_is_nsfw,
                })

                if cover_url and not cover_is_nsfw:
                    context["cover_url"] = cover_url

                context["meta"]["anilist"] = {
                    "id": media_data.get("id"),
                    "site_url": media_data.get("site_url"),
                    "titles": titles,
                    "format": media_data.get("format"),
                    "status": media_data.get("status"),
                    "episodes": media_data.get("episodes"),
                    "chapters": media_data.get("chapters"),
                    "volumes": media_data.get("volumes"),
                    "episode_duration": media_data.get("episode_duration"),
                    "average_score": media_data.get("average_score"),
                    "start_date": media_data.get("start_date"),
                    "preferred_field": preferred_key_lower,
                    "is_adult": media_data.get("is_adult"),
                    "cover_url": cover_url,
                    "cover_color": media_data.get("cover_color"),
                    "media_type": normalized_type,
                    "mal_id": media_data.get("mal_id"),
                }
            else:
                context["meta"]["anilist_lookup_failed"] = True

        return context

    async def _build_game_context(self, raw_value: str) -> Dict[str, Any]:
        """Resolve IGDB metadata for a game log entry."""
        sanitized_input = (raw_value or "").strip()

        context: Dict[str, Any] = {
            "display_name": sanitized_input or raw_value,
            "meta": {"game_input": raw_value},
            "igdb_id": None,
            "igdb_url": None,
            "cover_url": None,
            "rating": None,
            "rating_count": None,
            "release_date": None,
            "alternative_names": [],
        }

        game_id = None
        preferred_name = None

        # Try to parse autocomplete value (format: ${igdb:GAME|123:name})
        selection = self.media_searcher.parse_autocomplete_value(raw_value)
        if selection:
            game_id, preferred_name, source = selection
            logger.info(f"Parsed autocomplete value: game_id={game_id}, source={source}")
            if source and not source.lower().startswith("igdb"):
                game_id = None
                preferred_name = None
        else:
            # Check if this is a display format from autocomplete (e.g. "Game — Platform • Year • Rating")
            # Only search when the autocomplete display pattern is detected (has " — " separator)
            if " — " in sanitized_input:
                # Extract the title part before the first " — "
                title_part = sanitized_input.split(" — ")[0].strip()
                logger.info(f"Detected autocomplete display format, extracting title: {title_part}")
                
                # Search for the game using just the title
                try:
                    search_results = await self.media_searcher.search_games(title_part, limit=5)
                    if search_results:
                        top_result = search_results[0]
                        game_id = top_result.get("id")
                        preferred_name = top_result.get("name") or top_result.get("display")
                        logger.info(f"Recovered game from autocomplete display format: {title_part} -> game_id={game_id}")
                except Exception as e:
                    logger.warning(f"Display format game search failed: {e}")

        if game_id:
            try:
                game_data = await self.media_searcher.get_game(game_id)
                if game_data:
                    display_name = game_data.get("name") or preferred_name or sanitized_input

                    context.update({
                        "display_name": display_name,
                        "igdb_id": game_data.get("id"),
                        "igdb_url": game_data.get("url"),
                        "rating": game_data.get("rating"),
                        "rating_count": game_data.get("rating_count"),
                        "release_date": game_data.get("release_date"),
                        "alternative_names": game_data.get("alternative_names", []),
                    })

                    if game_data.get("cover_url"):
                        context["cover_url"] = game_data["cover_url"]

                    context["meta"]["igdb"] = {
                        "id": game_data.get("id"),
                        "url": game_data.get("url"),
                        "name": game_data.get("name"),
                        "rating": game_data.get("rating"),
                        "rating_count": game_data.get("rating_count"),
                        "release_date": game_data.get("release_date"),
                        "cover_url": game_data.get("cover_url"),
                        "alternative_names": game_data.get("alternative_names", []),
                    }
                else:
                    context["meta"]["igdb_lookup_failed"] = True
            except Exception as e:
                logger.error(f"Error building game context: {e}")
                context["meta"]["igdb_lookup_failed"] = True

        return context

    async def _date_autocomplete(self, interaction: discord.Interaction, current: str):
        """Shared autocomplete for the `date` parameter on every /log_* command.

        Shows Today, Yesterday, and 2–7 days ago as real calendar dates in the
        user's timezone. Values are canonical YYYY-MM-DD so the parser accepts
        them verbatim. Users may also type freeform shortcuts (yesterday, -2,
        mon, 6/15); Discord's fuzzy filter narrows the list, and anything
        unmatched is still parsed on submit.
        """
        try:
            user_id = str(interaction.user.id)
            guild_id = str(interaction.guild_id) if interaction.guild_id else None
            user_tz = await self.time_service.get_time_location(user_id, guild_id)
        except Exception as e:
            logger.warning(f"Date autocomplete timezone lookup failed: {e}")
            user_tz = timezone.utc

        today_local = datetime.now(user_tz)
        choices: List[app_commands.Choice[str]] = []
        for days_back in range(8):
            d = today_local - timedelta(days=days_back)
            date_str = d.strftime('%Y-%m-%d')
            label_date = f"{d.strftime('%b')} {d.day}"
            if days_back == 0:
                name = f"Today ({label_date})"
            elif days_back == 1:
                name = f"Yesterday ({label_date})"
            else:
                name = f"{days_back} days ago ({label_date})"
            choices.append(app_commands.Choice(name=name, value=date_str))
        return choices

    @app_commands.command(name="log_manual", description="Log your immersion time")
    @app_commands.describe(
        activity_type="Type of immersion activity",
        content="What you read/watched/listened to",
        duration="Time spent (formats: 30m, 1h30m, 1.5h, or 90 for minutes)",
        date=DATE_FORMAT_HELP,
        comment="Optional comment or note about this log entry"
    )
    @app_commands.choices(activity_type=[
        app_commands.Choice(name="Reading", value="reading"),
        app_commands.Choice(name="Listening", value="listening")
    ])
    @app_commands.autocomplete(date=_date_autocomplete)
    async def log_manual(self, interaction: discord.Interaction,
                        activity_type: str, content: str, duration: str,
                        date: str = None, comment: str = None):
        """Manual logging of immersion activities"""
        try:
            await interaction.response.defer()
            
            # Validate comment length (Discord embed field limit is 1024 characters)
            if comment and len(comment) > 1024:
                await interaction.followup.send(
                    f"❌ Comment is too long ({len(comment)} characters). "
                    f"Discord limits comments to 1024 characters. "
                    f"Please shorten your comment and try again.",
                    ephemeral=True
                )
                return
            
            user_id = str(interaction.user.id)
            guild_id = str(interaction.guild_id) if interaction.guild_id else None
            
            # Parse duration
            try:
                duration_seconds = self._parse_duration(duration)
            except ValueError as e:
                await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)
                return
            
            # Parse date if provided
            activity_date = datetime.now(timezone.utc)
            if date:
                try:
                    activity_date = await self.time_service.parse_user_datetime(user_id, guild_id, date)
                except ValueError as e:
                    await interaction.followup.send(f"Invalid date format: {str(e)}", ephemeral=True)
                    return
            
            # Validate date restrictions
            user_tz = await self.time_service.get_time_location(user_id, guild_id)
            date_error = await self._validate_log_date(activity_date, user_tz)
            if date_error:
                await interaction.followup.send(date_error, ephemeral=True)
                return
            
            # Validate daily duration limit
            if duration_seconds > 0:
                duration_error = await self._validate_daily_duration_limit(user_id, activity_date, duration_seconds)
                if duration_error:
                    await interaction.followup.send(duration_error, ephemeral=True)
                    return
            
            # Create activity
            activity = Activity(
                user_id=user_id,
                guild_id=guild_id,
                name=content,
                primary_type=activity_type,
                duration=duration_seconds,
                date=activity_date,
                comment=comment
            )
            
            # Save to database
            try:
                activity_id = await self.activity_repo.create_activity(activity)
            except ValueError as e:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return
            if not activity_id:
                await interaction.followup.send("Failed to log activity. Please try again.", ephemeral=True)
                return
            
            # Check for completed goals
            completed_goals = await self.goal_service.check_completed(activity)
            
            # Create response embed
            embed = discord.Embed(
                title="Activity Logged!",
                color=discord.Color.green(),
                timestamp=activity_date
            )
            
            embed.add_field(name="Content", value=content, inline=False)
            embed.add_field(name="Type", value=activity_type.title(), inline=True)
            embed.add_field(name="Duration", value=_format_duration_value(activity.duration), inline=True)

            # Add title totals (manual logs have no media_type)
            title_stats = await self.activity_repo.get_title_stats(
                user_id,
                content,
                media_type=None
            )
            if title_stats:
                totals_parts = [f"**{_format_duration_value(title_stats['total_duration'])}** total"]
                totals_parts.append(f"**{title_stats['total_count']}** logs")
                embed.add_field(
                    name="📚 Content Totals",
                    value=" • ".join(totals_parts),
                    inline=False
                )

            # Add comment if provided
            if comment:
                embed.add_field(name="💭 Comment", value=comment, inline=False)

            embed.set_footer(text=f"Activity ID: {activity_id}")

            # Create view with undo button
            view = UndoLogView(activity_id, interaction.user.id, self)

            await interaction.followup.send(embed=embed, view=view)
            
            # Send goal completion notifications
            if completed_goals:
                await self._notify_goal_completions(interaction, completed_goals)
            
            # Sync roles after logging (may affect class progression)
            await self._sync_roles_and_notify(
                interaction,
                source="activity_log",
                actor=interaction.user,
            )
                
        except Exception as e:
            logger.error(f"Error in log_manual command: {e}")
            await interaction.followup.send("An error occurred while logging your activity.", ephemeral=True)
    
    # Video logging subcommand
    @app_commands.command(name="log_video", description="Log a video you watched")
    @app_commands.describe(
        url="URL of the video",
        duration="Override duration (optional, formats: 30m, 1h30m, 1.5h, or 90 for minutes)",
        date=DATE_FORMAT_HELP,
        comment="Optional comment or note about this log entry"
    )
    @app_commands.autocomplete(date=_date_autocomplete)
    async def log_video(self, interaction: discord.Interaction, url: str,
                       duration: str = None, date: str = None, comment: str = None):
        """Log video watching activity"""
        if not await safe_defer(interaction):
            return  # Interaction expired; nothing to respond to

        try:
            
            # Validate comment length (Discord embed field limit is 1024 characters)
            if comment and len(comment) > 1024:
                await interaction.followup.send(
                    f"❌ Comment is too long ({len(comment)} characters). "
                    f"Discord limits comments to 1024 characters. "
                    f"Please shorten your comment and try again.",
                    ephemeral=True
                )
                return
            
            user_id = str(interaction.user.id)
            guild_id = str(interaction.guild_id) if interaction.guild_id else None
            
            # Normalize URL - add https:// if no protocol is present
            normalized_url = url.strip()
            if not normalized_url.startswith(('http://', 'https://')):
                normalized_url = f"https://{normalized_url}"
            
            # Get video info
            video_info = await get_video_info(normalized_url)
            if not video_info:
                await interaction.followup.send("Unable to get video information from URL.", ephemeral=True)
                return
            
            # Override duration if provided
            if duration:
                try:
                    video_info.duration = self._parse_duration(duration)
                except ValueError as e:
                    await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)
                    return
            
            # Parse date if provided
            activity_date = datetime.now(timezone.utc)
            if date:
                try:
                    activity_date = await self.time_service.parse_user_datetime(user_id, guild_id, date)
                except ValueError as e:
                    await interaction.followup.send(f"Invalid date format: {str(e)}", ephemeral=True)
                    return
            
            # Validate date restrictions
            user_tz = await self.time_service.get_time_location(user_id, guild_id)
            date_error = await self._validate_log_date(activity_date, user_tz)
            if date_error:
                await interaction.followup.send(date_error, ephemeral=True)
                return
            
            # Validate daily duration limit
            if video_info.duration > 0:
                duration_error = await self._validate_daily_duration_limit(user_id, activity_date, video_info.duration)
                if duration_error:
                    await interaction.followup.send(duration_error, ephemeral=True)
                    return
            
            # Create activity
            activity = Activity(
                user_id=user_id,
                guild_id=guild_id,
                name=video_info.title,
                primary_type=ActivityType.LISTENING.value,
                media_type=MediaType.VIDEO.value,
                duration=video_info.duration,
                date=activity_date,
                comment=comment
            )
            
            # Set video metadata
            activity.set_meta('url', normalized_url)
            activity.set_meta('platform', video_info.platform)
            activity.set_meta('video_id', video_info.video_id)
            activity.set_meta('channel_name', video_info.channel_name)
            activity.set_meta('thumbnail', video_info.thumbnail)
            
            # Save to database
            try:
                activity_id = await self.activity_repo.create_activity(activity)
            except ValueError as e:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return
            if not activity_id:
                await interaction.followup.send("Failed to log video activity.", ephemeral=True)
                return
            
            # Check for completed goals
            completed_goals = await self.goal_service.check_completed(activity)
            
            # Create response embed
            embed = discord.Embed(
                title="Video Activity Logged!",
                color=discord.Color.green(),
                timestamp=activity_date
            )
            
            embed.add_field(name="Title", value=video_info.title, inline=False)
            embed.add_field(name="Channel", value=video_info.channel_name, inline=True)
            embed.add_field(name="Duration", value=_format_duration_value(activity.duration), inline=True)
            
            if video_info.thumbnail:
                embed.set_image(url=video_info.thumbnail)

            # Add title totals
            title_stats = await self.activity_repo.get_title_stats(
                user_id,
                video_info.title,
                media_type="video"
            )
            if title_stats:
                totals_parts = [f"**{_format_duration_value(title_stats['total_duration'])}** total"]
                totals_parts.append(f"**{title_stats['total_count']}** logs")
                embed.add_field(
                    name="📺 Video Totals",
                    value=" • ".join(totals_parts),
                    inline=False
                )

            # Add comment if provided
            if comment:
                embed.add_field(name="💭 Comment", value=comment, inline=False)

            embed.set_footer(text=f"Activity ID: {activity_id}")

            # Create view with undo button
            view = UndoLogView(activity_id, interaction.user.id, self)

            # Add button to open video
            view.add_item(discord.ui.Button(
                label="Watch Video",
                style=discord.ButtonStyle.link,
                url=normalized_url
            ))

            await interaction.followup.send(embed=embed, view=view)
            
            # Send goal completion notifications
            if completed_goals:
                await self._notify_goal_completions(interaction, completed_goals)
            
            # Sync roles after logging (may affect class progression)
            await self._sync_roles_and_notify(
                interaction,
                source="activity_log",
                actor=interaction.user,
            )
                
        except Exception as e:
            logger.error(f"Error in log_video command: {e}")
            await safe_followup(interaction, "An error occurred while logging the video.", ephemeral=True)

    async def build_vn_log(
        self,
        *,
        user_id: str,
        guild_id: Optional[str],
        name: str,
        characters: Optional[int],
        duration_seconds: int,
        activity_date: datetime,
        comment: Optional[str] = None,
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, discord.Embed, "UndoLogView", List[Goal], Optional[discord.File]]:
        """Core VN-log routine shared by /log_vn and the kizuna webhook listener.

        Validates inputs, creates the Activity row, and builds the
        "Visual Novel Activity Logged!" embed plus an UndoLogView, plus the
        rendered class-progress bar image (None when no bar applies or the
        render failed; the embed then falls back to a text bar). Does NOT
        send any message and does NOT trigger role sync; the caller is
        responsible for both. Raises ValueError on validation failures so the
        caller can decide how to surface them.
        """
        if comment and len(comment) > 1024:
            raise ValueError(
                f"Comment is too long ({len(comment)} characters). "
                f"Discord limits comments to 1024 characters."
            )

        user_tz = await self.time_service.get_time_location(user_id, guild_id)
        date_error = await self._validate_log_date(activity_date, user_tz)
        if date_error:
            raise ValueError(date_error)

        if duration_seconds > 0:
            duration_error = await self._validate_daily_duration_limit(user_id, activity_date, duration_seconds)
            if duration_error:
                raise ValueError(duration_error)

        vn_context = await self._build_vn_context(name)
        display_name = (vn_context.get("display_name") or name or "").strip() or name

        activity = Activity(
            user_id=user_id,
            guild_id=guild_id,
            name=display_name,
            primary_type=ActivityType.READING.value,
            media_type=MediaType.VISUAL_NOVEL.value,
            duration=duration_seconds,
            date=activity_date,
            comment=comment,
        )

        reading_speed = None
        if characters is not None:
            activity.set_meta('characters', characters)
            if characters > 0 and duration_seconds > 0:
                reading_speed = characters / (duration_seconds / 3600)
                activity.set_meta('reading_speed', reading_speed)

        for meta_key, meta_value in vn_context.get("meta", {}).items():
            activity.set_meta(meta_key, meta_value)

        if extra_meta:
            for meta_key, meta_value in extra_meta.items():
                activity.set_meta(meta_key, meta_value)

        activity_id = await self.activity_repo.create_activity(activity)
        if not activity_id:
            raise ValueError("Failed to log VN activity.")

        completed_goals = await self.goal_service.check_completed(activity)

        embed = discord.Embed(
            title="Visual Novel Activity Logged!",
            color=discord.Color.green(),
            timestamp=activity_date,
        )

        embed.add_field(name="Title", value=display_name, inline=False)
        embed.add_field(name="Duration", value=_format_duration_value(activity.duration), inline=True)

        if characters is not None:
            embed.add_field(name="Characters Read", value=f"{characters:,}", inline=True)

        if reading_speed is not None:
            embed.add_field(
                name="Reading Speed",
                value=f"{reading_speed:.1f} chars/hour",
                inline=True,
            )

        if vn_context.get("cover_is_nsfw"):
            embed.add_field(
                name="Cover",
                value="🔞 Cover hidden (marked explicit on VNDB)",
                inline=False,
            )
        elif vn_context.get("cover_url"):
            embed.set_thumbnail(url=vn_context["cover_url"])

        title_stats = await self.activity_repo.get_title_stats(
            user_id,
            display_name,
            media_type="visual_novel",
            vndb_id=vn_context.get("vndb_id"),
        )
        if title_stats:
            totals_parts = [f"**{_format_duration_value(title_stats['total_duration'])}** total"]
            if title_stats.get('total_characters', 0) > 0:
                totals_parts.append(f"**{title_stats['total_characters']:,}** chars")
            totals_parts.append(f"**{title_stats['total_count']}** logs")
            embed.add_field(
                name="📚 Title Totals",
                value=" • ".join(totals_parts),
                inline=False,
            )

        # Class progress is shown whenever the log adds time. (Manual /log_vn
        # without a duration still skips this since duration_seconds == 0.)
        progress_file: Optional[discord.File] = None
        if duration_seconds > 0:
            vn_seconds = await self.activity_repo.sum_activity_duration(
                user_id, media_type=MediaType.VISUAL_NOVEL.value
            )
            vn_hours = (vn_seconds or 0) / 3600

            guild = self.bot.get_guild(int(guild_id)) if guild_id else None
            if guild:
                class_definitions = self._get_class_role_definitions_for_guild(guild)
            else:
                class_definitions = list(self._default_class_role_definitions)

            current_class = None
            next_class = None

            if class_definitions:
                current_class = class_definitions[0]["name"]
                for definition in class_definitions:
                    if vn_hours >= definition["min_hours"]:
                        current_class = definition["name"]
                    else:
                        next_class = definition
                        break

            # Needed by both the next-tier and the max-tier branches below.
            current_threshold = 0
            for definition in class_definitions:
                if definition["name"] == current_class:
                    current_threshold = definition["min_hours"]
                    break
            if current_threshold < 0.001:
                current_threshold = 0.0  # 鬼's 1-second "first log" threshold displays as 0h

            if current_class and next_class:
                next_threshold = next_class["min_hours"]
                hours_to_next = next_threshold - vn_hours

                progress_in_tier = vn_hours - current_threshold
                tier_width = next_threshold - current_threshold
                progress_ratio = min(progress_in_tier / tier_width, 1.0) if tier_width > 0 else 1.0
                progress_pct = progress_ratio * 100

                # Portion of the tier earned by this log (clamped to the tier, so a
                # session that crossed the threshold only counts its in-tier part).
                before_in_tier = max(vn_hours - duration_seconds / 3600 - current_threshold, 0.0)
                before_ratio = min(before_in_tier / tier_width, 1.0) if tier_width > 0 else 1.0
                gained_ratio = max(progress_ratio - before_ratio, 0.0)

                # Color bar image (green = prior progress, yellow = this log);
                # text bar only if the render fails.
                bar_line = ""
                try:
                    png = await asyncio.to_thread(render_class_progress_bar, progress_ratio, gained_ratio)
                    progress_file = discord.File(io.BytesIO(png), filename="class_progress.png")
                    embed.set_image(url="attachment://class_progress.png")
                except Exception as exc:
                    logger.warning(f"Class progress bar render failed: {exc}")
                    bar_line = f"{_render_log_progress_bar(progress_ratio)}\n"

                if vn_hours == 0 and current_class == "魑魅魍魎":
                    next_class_display = f"{next_class['name']} (first log)" if next_class['name'] == "鬼" else f"{next_class['name']} ({next_threshold:.0f}h)"
                    progress_value = (
                        f"**{current_class} ({current_threshold:.0f}h)** → **{next_class_display}**\n"
                        f"{bar_line}"
                        f"Log your first session to advance!\n"
                        f"**Total VN Time:** {_format_hours_display(vn_hours)}"
                    )
                else:
                    progress_value = (
                        f"**{current_class} ({current_threshold:.0f}h)** → **{next_class['name']} ({next_threshold:.0f}h)**\n"
                        f"{bar_line}"
                        f"{progress_pct:.1f}% (+{_format_gained_pct(gained_ratio)}%) • "
                        f"{_format_hours_display(progress_in_tier)} / {_format_hours_display(tier_width)} in tier • {_format_hours_display(hours_to_next)} to go\n"
                        f"**Total VN Time:** {_format_hours_display(vn_hours)}"
                    )

                embed.add_field(name="⚔️ VN Class Progress", value=progress_value, inline=False)
            elif current_class:
                progress_value = f"**{current_class} ({current_threshold:.0f}h)** ✨ MAX TIER\n**Total VN Time:** {_format_hours_display(vn_hours)}"
                embed.add_field(name="⚔️ VN Class Progress", value=progress_value, inline=False)

        if comment:
            embed.add_field(name="💭 Comment", value=comment, inline=False)

        embed.set_footer(text=f"Activity ID: {activity_id}")

        view = UndoLogView(activity_id, int(user_id), self)

        if vn_context.get("vndb_url"):
            view.add_item(discord.ui.Button(
                label="VNDB",
                style=discord.ButtonStyle.link,
                url=vn_context["vndb_url"],
            ))

        if vn_context.get("jiten_deck_id") is not None:
            view.add_item(discord.ui.Button(
                label="jiten.moe",
                style=discord.ButtonStyle.link,
                url=f"https://jiten.moe/decks/media/{vn_context['jiten_deck_id']}/detail",
            ))

        return activity_id, embed, view, completed_goals, progress_file

    @app_commands.command(name="log_vn", description="Log visual novel reading")
    @app_commands.describe(
        name="Name of the visual novel",
        characters="Number of characters read (optional)",
        duration="Reading time (optional, formats: 30m, 1h30m, 1.5h, or 90 for minutes)",
        date=DATE_FORMAT_HELP,
        comment="Optional comment or note about this log entry"
    )
    @app_commands.autocomplete(date=_date_autocomplete)
    async def log_vn(self, interaction: discord.Interaction, name: str,
                    characters: Optional[int] = None, duration: str = None,
                    date: str = None, comment: str = None):
        """Log visual novel reading activity"""
        if not await safe_defer(interaction):
            return  # Interaction expired; nothing to respond to

        try:
            user_id = str(interaction.user.id)
            guild_id = str(interaction.guild_id) if interaction.guild_id else None

            if duration:
                try:
                    duration_seconds = self._parse_duration(duration)
                except ValueError as e:
                    await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)
                    return
            else:
                duration_seconds = 0

            activity_date = datetime.now(timezone.utc)
            if date:
                try:
                    activity_date = await self.time_service.parse_user_datetime(user_id, guild_id, date)
                except ValueError as e:
                    await interaction.followup.send(f"Invalid date format: {str(e)}", ephemeral=True)
                    return

            try:
                activity_id, embed, view, completed_goals, progress_file = await self.build_vn_log(
                    user_id=user_id,
                    guild_id=guild_id,
                    name=name,
                    characters=characters,
                    duration_seconds=duration_seconds,
                    activity_date=activity_date,
                    comment=comment,
                )
            except ValueError as e:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return

            if progress_file:
                await interaction.followup.send(embed=embed, view=view, file=progress_file)
            else:
                await interaction.followup.send(embed=embed, view=view)

            if completed_goals:
                await self._notify_goal_completions(interaction, completed_goals)

            await self._sync_roles_and_notify(
                interaction,
                source="activity_log",
                actor=interaction.user,
            )

        except Exception as e:
            logger.error(f"Error in log_vn command: {e}")
            await safe_followup(interaction, "An error occurred while logging the VN.", ephemeral=True)

    @log_vn.autocomplete("name")
    async def log_vn_name_autocomplete(self, interaction: discord.Interaction, current: str):
        query = (current or "").strip()
        if len(query) < 2:
            return []

        try:
            results = await self.media_searcher.search_visual_novel(query, limit=25)
        except Exception as exc:
            logger.warning("VNDB autocomplete failed for '%s': %s", query, exc)
            return []

        choices: List[app_commands.Choice[str]] = []

        for vn in results:
            vn_id = vn.get("id")
            titles = vn.get("titles") or {}
            display_title = vn.get("display") or titles.get("primary")

            if not vn_id or not display_title:
                continue

            # Determine which title key is used for display
            field_key = "primary"
            for key, option in (("en", "en"), ("ja", "jp"), ("romaji", "romaji")):
                if titles.get(key) and titles.get(key) == display_title:
                    field_key = option
                    break

            rating = vn.get("rating")
            released = vn.get("released")
            badge_parts: List[str] = []
            if isinstance(released, str) and released:
                badge_parts.append(released)
            if isinstance(rating, (int, float)) and rating > 0:
                badge_parts.append(f"{rating / 10:.1f}/10")

            # Build label with VN ID suffix to survive Discord token replacement
            vn_id_suffix = f" [{vn_id}]"
            if badge_parts:
                choice_label = f"{display_title} — {' • '.join(badge_parts)}{vn_id_suffix}"
            else:
                choice_label = f"{display_title}{vn_id_suffix}"

            # Truncate title if needed, but preserve the VN ID suffix
            if len(choice_label) > 100:
                if badge_parts:
                    badge_str = f" — {' • '.join(badge_parts)}"
                    available_for_title = 100 - len(badge_str) - len(vn_id_suffix) - 1
                    truncated_title = display_title[:available_for_title] + "…"
                    choice_label = f"{truncated_title}{badge_str}{vn_id_suffix}"
                else:
                    truncated_title = display_title[:100 - len(vn_id_suffix) - 1] + "…"
                    choice_label = f"{truncated_title}{vn_id_suffix}"

            value = self.media_searcher.create_autocomplete_value(vn_id, field_key, source="vndb")
            choices.append(app_commands.Choice(name=choice_label, value=value))

        return choices[:25]
    
    @app_commands.command(name="log_manga", description="Log manga reading")
    @app_commands.describe(
        name="Name of the manga",
        pages="Number of pages read (optional, can be decimal like 42.5)",
        duration="Reading time (optional, formats: 30m, 1h30m, 1.5h, or 90 for minutes)",
        date=DATE_FORMAT_HELP,
        comment="Optional comment or note about this log entry"
    )
    @app_commands.autocomplete(date=_date_autocomplete)
    async def log_manga(self, interaction: discord.Interaction, name: str,
                       pages: Optional[str] = None, duration: str = None, date: str = None, comment: str = None):
        """Log manga reading activity"""
        if not await safe_defer(interaction):
            return  # Interaction expired; nothing to respond to

        try:
            
            # Validate comment length (Discord embed field limit is 1024 characters)
            if comment and len(comment) > 1024:
                await interaction.followup.send(
                    f"❌ Comment is too long ({len(comment)} characters). "
                    f"Discord limits comments to 1024 characters. "
                    f"Please shorten your comment and try again.",
                    ephemeral=True
                )
                return
            
            user_id = str(interaction.user.id)
            guild_id = str(interaction.guild_id) if interaction.guild_id else None

            manga_context = await self._build_anilist_context(MediaType.MANGA.value, name)
            display_name = manga_context.get("display_name") or name
            
            # Parse pages if provided
            pages_value = None
            if pages is not None:
                try:
                    pages_value = float(pages)
                    if pages_value <= 0:
                        await interaction.followup.send("❌ Pages must be a positive number.", ephemeral=True)
                        return
                except (ValueError, TypeError):
                    await interaction.followup.send(f"❌ Invalid pages value. Please enter a number (e.g., 42 or 42.5).", ephemeral=True)
                    return
            
            # Parse duration or default to zero when not provided
            if duration:
                try:
                    duration_seconds = self._parse_duration(duration)
                except ValueError as e:
                    await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)
                    return
            else:
                duration_seconds = 0

            reading_speed = None
            if pages_value is not None and duration_seconds > 0:
                reading_speed = pages_value / (duration_seconds / 3600)
            
            # Parse date
            if date:
                try:
                    activity_date = await self.time_service.parse_user_datetime(user_id, guild_id, date)
                except ValueError as e:
                    await interaction.followup.send(f"Invalid date format: {str(e)}", ephemeral=True)
                    return
            else:
                activity_date = datetime.now(timezone.utc)
            
            # Validate date restrictions
            user_tz = await self.time_service.get_time_location(user_id, guild_id)
            date_error = await self._validate_log_date(activity_date, user_tz)
            if date_error:
                await interaction.followup.send(date_error, ephemeral=True)
                return
            
            # Validate daily duration limit (only if duration is provided)
            if duration_seconds > 0:
                duration_error = await self._validate_daily_duration_limit(user_id, activity_date, duration_seconds)
                if duration_error:
                    await interaction.followup.send(duration_error, ephemeral=True)
                    return
            
            # Create activity
            activity = Activity(
                user_id=user_id,
                guild_id=guild_id,
                name=display_name,
                primary_type="reading",
                media_type="manga",
                duration=duration_seconds,
                date=activity_date,
                comment=comment
            )
            
            if pages_value is not None:
                activity.set_meta('pages', pages_value)
            if reading_speed is not None:
                activity.set_meta('reading_speed', reading_speed)

            if manga_context.get("meta", {}).get("anilist"):
                activity.set_meta('anilist', manga_context["meta"]["anilist"])
            if manga_context.get("anilist_id") is not None:
                activity.set_meta('anilist_id', manga_context.get("anilist_id"))
            if manga_context.get("total_chapters") is not None:
                activity.set_meta('anilist_total_chapters', manga_context.get("total_chapters"))
            if manga_context.get("total_volumes") is not None:
                activity.set_meta('anilist_total_volumes', manga_context.get("total_volumes"))

            # Save to database
            try:
                activity_id = await self.activity_repo.create_activity(activity)
            except ValueError as e:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return
            if not activity_id:
                await interaction.followup.send("Failed to log manga activity.", ephemeral=True)
                return
            
            # Check for goal completions
            completed_goals = await self.goal_service.check_completed(activity)
            
            # Create response embed
            embed = discord.Embed(
                title="Manga Activity Logged!",
                color=discord.Color.green(),
                timestamp=activity_date
            )
            
            embed.add_field(name="Title", value=display_name, inline=False)
            embed.add_field(name="Duration", value=_format_duration_value(activity.duration), inline=True)

            if pages_value is not None:
                # Format pages with up to 2 decimal places, removing trailing zeros
                formatted_pages = f"{pages_value:,.2f}".rstrip('0').rstrip('.')
                embed.add_field(name="Pages Read", value=formatted_pages, inline=True)

            if reading_speed is not None:
                embed.add_field(name="Reading Speed", value=f"{reading_speed:.1f} pages/hour", inline=True)

            if manga_context.get("cover_url"):
                embed.set_thumbnail(url=manga_context["cover_url"])

            # Add title totals
            title_stats = await self.activity_repo.get_title_stats(
                user_id,
                display_name,
                media_type="manga"
            )
            if title_stats:
                totals_parts = [f"**{_format_duration_value(title_stats['total_duration'])}** total"]
                if title_stats.get('total_pages', 0) > 0:
                    formatted_total_pages = f"{title_stats['total_pages']:,.1f}".rstrip('0').rstrip('.')
                    totals_parts.append(f"**{formatted_total_pages}** pages")
                totals_parts.append(f"**{title_stats['total_count']}** logs")
                embed.add_field(
                    name="📚 Title Totals",
                    value=" • ".join(totals_parts),
                    inline=False
                )

            # Add comment if provided
            if comment:
                embed.add_field(name="💭 Comment", value=comment, inline=False)

            embed.set_footer(text=f"Activity ID: {activity_id}")

            # Create view with undo button
            view = UndoLogView(activity_id, interaction.user.id, self)

            # Add AniList link if available
            if manga_context.get("anilist_url"):
                view.add_item(discord.ui.Button(
                    label="Open on AniList",
                    style=discord.ButtonStyle.link,
                    url=manga_context["anilist_url"],
                ))

            await interaction.followup.send(embed=embed, view=view)
            
            # Send goal completion notifications
            if completed_goals:
                await self._notify_goal_completions(interaction, completed_goals)
            
            # Sync roles after logging (may affect class progression)
            await self._sync_roles_and_notify(
                interaction,
                source="activity_log",
                actor=interaction.user,
            )
                
        except Exception as e:
            logger.error(f"Error in log_manga command: {e}")
            await safe_followup(interaction, "An error occurred while logging the manga.", ephemeral=True)

    @log_manga.autocomplete("name")
    async def log_manga_name_autocomplete(self, interaction: discord.Interaction, current: str):
        query = (current or "").strip()
        if len(query) < 2:
            return []

        try:
            results = await self.media_searcher.search_manga(query, limit=25)
        except Exception as exc:
            logger.warning("AniList manga autocomplete failed for '%s': %s", query, exc)
            return []

        choices: List[app_commands.Choice[str]] = []
        for item in results:
            media_id = item.get("id")
            display = item.get("display")
            if not media_id or not display:
                continue

            label = display
            info_bits: List[str] = []
            if item.get("format"):
                info_bits.append(item["format"].replace('_', ' ').title())
            chapters_value = item.get("chapters")
            if isinstance(chapters_value, (int, float)) and chapters_value > 0:
                info_bits.append(f"{int(chapters_value)} chapters")
            volumes_value = item.get("volumes")
            if isinstance(volumes_value, (int, float)) and volumes_value > 0:
                info_bits.append(f"{int(volumes_value)} volumes")
            if info_bits:
                label = f"{display} — {' • '.join(info_bits)}"

            if len(label) > 100:
                label = label[:97] + "…"

            field_key = item.get("display_field") or "primary"
            value = self.media_searcher.create_autocomplete_value(media_id, field_key, source="anilist:MANGA")
            choices.append(app_commands.Choice(name=label, value=value))

        return choices[:25]

    @app_commands.command(name="log_book", description="Log book reading")
    @app_commands.describe(
        name="Name of the book",
        pages="Number of pages read (optional, can be decimal like 42.5)",
        characters="Number of characters read (optional)",
        duration="Reading time (optional, formats: 30m, 1h30m, 1.5h, or 90 for minutes)",
        date=DATE_FORMAT_HELP,
        comment="Optional comment or note about this log entry"
    )
    @app_commands.autocomplete(date=_date_autocomplete)
    async def log_book(
        self,
        interaction: discord.Interaction,
        name: str,
        pages: Optional[str] = None,
        characters: Optional[int] = None,
        duration: str = None,
        date: str = None,
        comment: str = None
    ):
        """Log book reading activity"""
        if not await safe_defer(interaction):
            return  # Interaction expired; nothing to respond to

        try:
            
            # Validate comment length (Discord embed field limit is 1024 characters)
            if comment and len(comment) > 1024:
                await interaction.followup.send(
                    f"❌ Comment is too long ({len(comment)} characters). "
                    f"Discord limits comments to 1024 characters. "
                    f"Please shorten your comment and try again.",
                    ephemeral=True
                )
                return
            
            user_id = str(interaction.user.id)
            guild_id = str(interaction.guild_id) if interaction.guild_id else None
            
            # Parse pages if provided
            pages_value = None
            if pages is not None:
                try:
                    pages_value = float(pages)
                    if pages_value <= 0:
                        await interaction.followup.send("❌ Pages must be a positive number.", ephemeral=True)
                        return
                except (ValueError, TypeError):
                    await interaction.followup.send(f"❌ Invalid pages value. Please enter a number (e.g., 42 or 42.5).", ephemeral=True)
                    return
            
            # Calculate duration if not provided
            if duration:
                try:
                    duration_seconds = self._parse_duration(duration)
                except ValueError as e:
                    await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)
                    return
            else:
                duration_seconds = 0

            pages_per_hour = None
            characters_per_hour = None
            if duration_seconds > 0:
                hours_spent = duration_seconds / 3600
                if pages_value is not None:
                    pages_per_hour = pages_value / hours_spent if hours_spent > 0 else None
                if characters is not None:
                    characters_per_hour = characters / hours_spent if hours_spent > 0 else None
            
            # Parse date if provided
            activity_date = datetime.now(timezone.utc)
            if date:
                try:
                    activity_date = await self.time_service.parse_user_datetime(user_id, guild_id, date)
                except ValueError as e:
                    await interaction.followup.send(f"Invalid date format: {str(e)}", ephemeral=True)
                    return
            
            # Validate date restrictions
            user_tz = await self.time_service.get_time_location(user_id, guild_id)
            date_error = await self._validate_log_date(activity_date, user_tz)
            if date_error:
                await interaction.followup.send(date_error, ephemeral=True)
                return
            
            # Validate daily duration limit
            if duration_seconds > 0:
                duration_error = await self._validate_daily_duration_limit(user_id, activity_date, duration_seconds)
                if duration_error:
                    await interaction.followup.send(duration_error, ephemeral=True)
                    return
            
            # Create activity
            activity = Activity(
                user_id=user_id,
                guild_id=guild_id,
                name=name,
                primary_type=ActivityType.READING.value,
                media_type=MediaType.BOOK.value,
                duration=duration_seconds,
                date=activity_date,
                comment=comment
            )
            
            if pages_value is not None:
                activity.set_meta('pages', pages_value)
            if characters is not None:
                activity.set_meta('characters', characters)
            if pages_per_hour is not None:
                activity.set_meta('pages_per_hour', pages_per_hour)
            if characters_per_hour is not None:
                activity.set_meta('characters_per_hour', characters_per_hour)
            if characters_per_hour is not None:
                activity.set_meta('reading_speed', characters_per_hour)
            elif pages_per_hour is not None:
                activity.set_meta('reading_speed', pages_per_hour)
            
            # Save to database
            try:
                activity_id = await self.activity_repo.create_activity(activity)
            except ValueError as e:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return
            if not activity_id:
                await interaction.followup.send("Failed to log book activity.", ephemeral=True)
                return
            
            # Check for completed goals
            completed_goals = await self.goal_service.check_completed(activity)
            
            # Create response embed
            embed = discord.Embed(
                title="Book Activity Logged!",
                color=discord.Color.green(),
                timestamp=activity_date
            )
            
            embed.add_field(name="Title", value=name, inline=False)
            embed.add_field(name="Duration", value=_format_duration_value(activity.duration), inline=True)

            if pages_value is not None:
                # Format pages with up to 2 decimal places, removing trailing zeros
                formatted_pages = f"{pages_value:,.2f}".rstrip('0').rstrip('.')
                embed.add_field(name="Pages Read", value=formatted_pages, inline=True)
            if characters is not None:
                embed.add_field(name="Characters Read", value=f"{int(characters):,}", inline=True)

            if pages_per_hour is not None:
                embed.add_field(
                    name="Pages per Hour",
                    value=f"{pages_per_hour:.1f}",
                    inline=True
                )
            if characters_per_hour is not None:
                embed.add_field(
                    name="Characters per Hour",
                    value=f"{characters_per_hour:.1f}",
                    inline=True
                )

            # Add title totals
            title_stats = await self.activity_repo.get_title_stats(
                user_id,
                name,
                media_type="book"
            )
            if title_stats:
                totals_parts = [f"**{_format_duration_value(title_stats['total_duration'])}** total"]
                if title_stats.get('total_pages', 0) > 0:
                    formatted_total_pages = f"{title_stats['total_pages']:,.1f}".rstrip('0').rstrip('.')
                    totals_parts.append(f"**{formatted_total_pages}** pages")
                if title_stats.get('total_characters', 0) > 0:
                    totals_parts.append(f"**{title_stats['total_characters']:,}** chars")
                totals_parts.append(f"**{title_stats['total_count']}** logs")
                embed.add_field(
                    name="📚 Title Totals",
                    value=" • ".join(totals_parts),
                    inline=False
                )

            # Add comment if provided
            if comment:
                embed.add_field(name="💭 Comment", value=comment, inline=False)

            embed.set_footer(text=f"Activity ID: {activity_id}")

            # Create view with undo button
            view = UndoLogView(activity_id, interaction.user.id, self)

            await interaction.followup.send(embed=embed, view=view)
            
            # Send goal completion notifications
            if completed_goals:
                await self._notify_goal_completions(interaction, completed_goals)
            
            # Sync roles after logging (may affect class progression)
            await self._sync_roles_and_notify(
                interaction,
                source="activity_log",
                actor=interaction.user,
            )
                
        except Exception as e:
            logger.error(f"Error in log_book command: {e}")
            await safe_followup(interaction, "An error occurred while logging the book.", ephemeral=True)

    @app_commands.command(name="log_anime", description="Log anime watching")
    @app_commands.describe(
        name="Name of the anime",
        episodes="Number of episodes watched",
        episode_duration="Duration per episode in minutes (default: 24)",
        date=DATE_FORMAT_HELP,
        comment="Optional comment or note about this log entry"
    )
    @app_commands.autocomplete(date=_date_autocomplete)
    async def log_anime(self, interaction: discord.Interaction, name: str,
                       episodes: int, episode_duration: int = 24, date: str = None, comment: str = None):
        """Log anime watching activity"""
        if not await safe_defer(interaction):
            return  # Interaction expired; nothing to respond to

        try:
            
            # Validate comment length (Discord embed field limit is 1024 characters)
            if comment and len(comment) > 1024:
                await interaction.followup.send(
                    f"❌ Comment is too long ({len(comment)} characters). "
                    f"Discord limits comments to 1024 characters. "
                    f"Please shorten your comment and try again.",
                    ephemeral=True
                )
                return
            
            user_id = str(interaction.user.id)
            guild_id = str(interaction.guild_id) if interaction.guild_id else None

            anime_context = await self._build_anilist_context(MediaType.ANIME.value, name)
            display_name = anime_context.get("display_name") or name
            
            # Determine per-episode duration using AniList data when available
            official_duration = anime_context.get("episode_duration")
            per_episode_duration = episode_duration
            if per_episode_duration <= 0:
                per_episode_duration = official_duration or 24
            elif official_duration and episode_duration == 24:
                per_episode_duration = official_duration

            per_episode_duration = per_episode_duration or 0
            per_episode_duration = int(per_episode_duration)

            # Calculate total duration
            duration_seconds = int(max(episodes, 0) * max(per_episode_duration, 0) * 60)
            
            # Parse date if provided
            activity_date = datetime.now(timezone.utc)
            if date:
                try:
                    activity_date = await self.time_service.parse_user_datetime(user_id, guild_id, date)
                except ValueError as e:
                    await interaction.followup.send(f"Invalid date format: {str(e)}", ephemeral=True)
                    return
            
            # Validate date restrictions
            user_tz = await self.time_service.get_time_location(user_id, guild_id)
            date_error = await self._validate_log_date(activity_date, user_tz)
            if date_error:
                await interaction.followup.send(date_error, ephemeral=True)
                return
            
            # Validate daily duration limit
            if duration_seconds > 0:
                duration_error = await self._validate_daily_duration_limit(user_id, activity_date, duration_seconds)
                if duration_error:
                    await interaction.followup.send(duration_error, ephemeral=True)
                    return
            
            # Create activity
            activity = Activity(
                user_id=user_id,
                guild_id=guild_id,
                name=display_name,
                primary_type=ActivityType.LISTENING.value,
                media_type=MediaType.ANIME.value,
                duration=duration_seconds,
                date=activity_date,
                comment=comment
            )
            
            activity.set_meta('episodes', episodes)
            activity.set_meta('episode_duration', per_episode_duration)
            if official_duration:
                activity.set_meta('anilist_episode_duration', official_duration)

            if anime_context.get("meta", {}).get("anilist"):
                activity.set_meta('anilist', anime_context["meta"]["anilist"])
            if anime_context.get("anilist_id") is not None:
                activity.set_meta('anilist_id', anime_context.get("anilist_id"))
            if anime_context.get("total_episodes") is not None:
                activity.set_meta('anilist_total_episodes', anime_context.get("total_episodes"))
            
            # Save to database
            try:
                activity_id = await self.activity_repo.create_activity(activity)
            except ValueError as e:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return
            if not activity_id:
                await interaction.followup.send("Failed to log anime activity.", ephemeral=True)
                return
            
            # Check for completed goals
            completed_goals = await self.goal_service.check_completed(activity)
            
            # Create response embed
            embed = discord.Embed(
                title="Anime Activity Logged!",
                color=discord.Color.green(),
                timestamp=activity_date
            )
            
            embed.add_field(name="Title", value=display_name, inline=False)
            embed.add_field(name="Episodes", value=str(episodes), inline=True)
            embed.add_field(name="Duration", value=_format_duration_value(activity.duration), inline=True)

            if anime_context.get("cover_url"):
                embed.set_thumbnail(url=anime_context["cover_url"])

            # Add title totals
            title_stats = await self.activity_repo.get_title_stats(
                user_id,
                display_name,
                media_type="anime"
            )
            if title_stats:
                totals_parts = [f"**{_format_duration_value(title_stats['total_duration'])}** total"]
                if title_stats.get('total_episodes', 0) > 0:
                    totals_parts.append(f"**{title_stats['total_episodes']}** eps")
                totals_parts.append(f"**{title_stats['total_count']}** logs")
                embed.add_field(
                    name="📚 Title Totals",
                    value=" • ".join(totals_parts),
                    inline=False
                )

            # Add anime class progress information (only if duration was logged)
            progress_file: Optional[discord.File] = None
            if duration_seconds > 0:
                # Get updated anime duration after the activity was saved
                anime_seconds = await self.activity_repo.sum_activity_duration(user_id, media_type=MediaType.ANIME.value)
                anime_hours = (anime_seconds or 0) / 3600
                
                # Use anime class definitions (not VN class definitions)
                anime_class_definitions = ANIME_CLASS_ROLE_DEFINITIONS
                
                current_class = None
                next_class = None
                
                if anime_class_definitions:
                    current_class = anime_class_definitions[0]["name"]
                    for definition in anime_class_definitions:
                        if anime_hours >= definition["min_hours"]:
                            current_class = definition["name"]
                        else:
                            next_class = definition
                            break
                
                if current_class and next_class:
                    next_threshold = next_class["min_hours"]
                    hours_to_next = next_threshold - anime_hours
                    
                    # Find current class threshold for tier-relative progress
                    current_threshold = 0
                    for definition in anime_class_definitions:
                        if definition["name"] == current_class:
                            current_threshold = definition["min_hours"]
                            break
                    if current_threshold < 0.001:
                        current_threshold = 0.0  # epsilon "first log" thresholds display as 0h

                    # Calculate tier-relative progress (progress within current tier)
                    progress_in_tier = anime_hours - current_threshold
                    tier_width = next_threshold - current_threshold
                    progress_ratio = min(progress_in_tier / tier_width, 1.0) if tier_width > 0 else 1.0
                    progress_pct = progress_ratio * 100

                    # Portion of the tier earned by this log (clamped to the tier).
                    before_in_tier = max(anime_hours - duration_seconds / 3600 - current_threshold, 0.0)
                    before_ratio = min(before_in_tier / tier_width, 1.0) if tier_width > 0 else 1.0
                    gained_ratio = max(progress_ratio - before_ratio, 0.0)

                    # Color bar image (green = prior progress, yellow = this log);
                    # text bar only if the render fails.
                    bar_line = ""
                    try:
                        png = await asyncio.to_thread(render_class_progress_bar, progress_ratio, gained_ratio)
                        progress_file = discord.File(io.BytesIO(png), filename="class_progress.png")
                        embed.set_image(url="attachment://class_progress.png")
                    except Exception as exc:
                        logger.warning(f"Class progress bar render failed: {exc}")
                        bar_line = f"{_render_log_progress_bar(progress_ratio)}\n"

                    if anime_hours == 0 and current_class == "人間":
                        progress_value = (
                            f"**{current_class} ({current_threshold:.0f}h)** → **{next_class['name']} ({next_threshold:.0f}h)**\n"
                            f"{bar_line}"
                            f"Log your first session to advance!\n"
                            f"**Total Anime Time:** {_format_hours_display(anime_hours)}"
                        )
                    else:
                        # Show tier progress explicitly
                        progress_value = (
                            f"**{current_class} ({current_threshold:.0f}h)** → **{next_class['name']} ({next_threshold:.0f}h)**\n"
                            f"{bar_line}"
                            f"{progress_pct:.1f}% (+{_format_gained_pct(gained_ratio)}%) • "
                            f"{_format_hours_display(progress_in_tier)} / {_format_hours_display(tier_width)} in tier • {_format_hours_display(hours_to_next)} to go\n"
                            f"**Total Anime Time:** {_format_hours_display(anime_hours)}"
                        )
                    
                    embed.add_field(name="⚔️ Anime Class Progress", value=progress_value, inline=False)
                elif current_class:
                    # Max tier reached
                    current_threshold = 0
                    for definition in anime_class_definitions:
                        if definition["name"] == current_class:
                            current_threshold = definition["min_hours"]
                            break
                    progress_value = f"**{current_class} ({current_threshold:.0f}h)** ✨ MAX TIER\n**Total Anime Time:** {_format_hours_display(anime_hours)}"
                    embed.add_field(name="⚔️ Anime Class Progress", value=progress_value, inline=False)
            
            # Add comment if provided
            if comment:
                embed.add_field(name="💭 Comment", value=comment, inline=False)
            
            embed.set_footer(text=f"Activity ID: {activity_id}")

            # Create view with undo button
            view = UndoLogView(activity_id, interaction.user.id, self)
            
            # Add AniList link if available
            if anime_context.get("anilist_url"):
                view.add_item(discord.ui.Button(
                    label="Open on AniList",
                    style=discord.ButtonStyle.link,
                    url=anime_context["anilist_url"],
                ))

            if progress_file:
                await interaction.followup.send(embed=embed, view=view, file=progress_file)
            else:
                await interaction.followup.send(embed=embed, view=view)
            
            # Send goal completion notifications
            if completed_goals:
                await self._notify_goal_completions(interaction, completed_goals)
            
            # Sync roles after logging (may affect class progression)
            await self._sync_roles_and_notify(
                interaction,
                source="activity_log",
                actor=interaction.user,
            )
                
        except Exception as e:
            logger.error(f"Error in log_anime command: {e}")
            await safe_followup(interaction, "An error occurred while logging the anime.", ephemeral=True)

    @log_anime.autocomplete("name")
    async def log_anime_name_autocomplete(self, interaction: discord.Interaction, current: str):
        query = (current or "").strip()
        if len(query) < 2:
            return []

        try:
            results = await self.media_searcher.search_anime(query, limit=25)
        except Exception as exc:
            logger.warning("AniList anime autocomplete failed for '%s': %s", query, exc)
            return []

        choices: List[app_commands.Choice[str]] = []
        for item in results:
            media_id = item.get("id")
            display = item.get("display")
            if not media_id or not display:
                continue

            label = display
            info_bits: List[str] = []
            if item.get("format"):
                info_bits.append(item["format"].replace('_', ' ').title())
            episodes_value = item.get("episodes")
            if isinstance(episodes_value, (int, float)) and episodes_value > 0:
                info_bits.append(f"{int(episodes_value)} eps")
            if item.get("start_date"):
                info_bits.append(str(item["start_date"]))
            if info_bits:
                label = f"{display} — {' • '.join(info_bits)}"

            if len(label) > 100:
                label = label[:97] + "…"

            field_key = item.get("display_field") or "primary"
            value = self.media_searcher.create_autocomplete_value(media_id, field_key, source="anilist:ANIME")
            choices.append(app_commands.Choice(name=label, value=value))

        return choices[:25]
    
    @app_commands.command(name="log_game", description="Log game playing")
    @app_commands.describe(
        name="Name of the game",
        duration="Time spent playing (formats: 30m, 1h30m, 1.5h, or 90 for minutes)",
        activity_type="Activity type (default: reading)",
        date=DATE_FORMAT_HELP,
        comment="Optional comment or note about this log entry"
    )
    @app_commands.choices(activity_type=[
        app_commands.Choice(name="Reading", value="reading"),
        app_commands.Choice(name="Listening", value="listening")
    ])
    @app_commands.autocomplete(date=_date_autocomplete)
    async def log_game(
        self,
        interaction: discord.Interaction,
        name: str,
        duration: str,
        activity_type: str = "reading",
        date: str = None,
        comment: str = None
    ):
        """Log game playing activity"""
        try:
            await interaction.response.defer()
            
            # Validate comment length (Discord embed field limit is 1024 characters)
            if comment and len(comment) > 1024:
                await interaction.followup.send(
                    f"❌ Comment is too long ({len(comment)} characters). "
                    f"Discord limits comments to 1024 characters. "
                    f"Please shorten your comment and try again.",
                    ephemeral=True
                )
                return
            
            user_id = str(interaction.user.id)
            guild_id = str(interaction.guild_id) if interaction.guild_id else None
            
            # Build game context
            game_context = await self._build_game_context(name)
            display_name = game_context.get("display_name") or name
            
            # Parse duration
            try:
                duration_seconds = self._parse_duration(duration)
            except ValueError as e:
                await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)
                return
            
            # Parse date if provided
            activity_date = datetime.now(timezone.utc)
            if date:
                try:
                    activity_date = await self.time_service.parse_user_datetime(user_id, guild_id, date)
                except ValueError as e:
                    await interaction.followup.send(f"Invalid date format: {str(e)}", ephemeral=True)
                    return
            
            # Validate date restrictions
            user_tz = await self.time_service.get_time_location(user_id, guild_id)
            date_error = await self._validate_log_date(activity_date, user_tz)
            if date_error:
                await interaction.followup.send(date_error, ephemeral=True)
                return
            
            # Validate daily duration limit
            if duration_seconds > 0:
                duration_error = await self._validate_daily_duration_limit(user_id, activity_date, duration_seconds)
                if duration_error:
                    await interaction.followup.send(duration_error, ephemeral=True)
                    return
            
            # Create activity with specified type (defaults to reading)
            activity = Activity(
                user_id=user_id,
                guild_id=guild_id,
                name=display_name,
                primary_type=activity_type,
                media_type="game",
                duration=duration_seconds,
                date=activity_date,
                comment=comment
            )
            
            # Set game metadata
            if game_context.get("meta", {}).get("igdb"):
                activity.set_meta('igdb', game_context["meta"]["igdb"])
            if game_context.get("igdb_id") is not None:
                activity.set_meta('igdb_id', game_context.get("igdb_id"))
            if game_context.get("rating") is not None:
                activity.set_meta('rating', game_context.get("rating"))
            
            # Save to database
            try:
                activity_id = await self.activity_repo.create_activity(activity)
            except ValueError as e:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return
            if not activity_id:
                await interaction.followup.send("Failed to log game activity.", ephemeral=True)
                return
            
            # Check for completed goals
            completed_goals = await self.goal_service.check_completed(activity)
            
            # Create response embed
            embed = discord.Embed(
                title="Game Activity Logged!",
                color=discord.Color.green(),
                timestamp=activity_date
            )
            
            embed.add_field(name="Game", value=display_name, inline=False)
            embed.add_field(name="Duration", value=_format_duration_value(activity.duration), inline=True)
            embed.add_field(name="Type", value=activity_type.title(), inline=True)
            
            # Add game cover if available
            if game_context.get("cover_url"):
                embed.set_thumbnail(url=game_context["cover_url"])

            # Add title totals
            title_stats = await self.activity_repo.get_title_stats(
                user_id,
                display_name,
                media_type="game"
            )
            if title_stats:
                totals_parts = [f"**{_format_duration_value(title_stats['total_duration'])}** total"]
                totals_parts.append(f"**{title_stats['total_count']}** logs")
                embed.add_field(
                    name="🎮 Title Totals",
                    value=" • ".join(totals_parts),
                    inline=False
                )

            # Add comment if provided
            if comment:
                embed.add_field(name="💭 Comment", value=comment, inline=False)

            embed.set_footer(text=f"Activity ID: {activity_id}")

            # Create view with undo button
            view = UndoLogView(activity_id, interaction.user.id, self)

            # Add IGDB link if available
            if game_context.get("igdb_url"):
                view.add_item(discord.ui.Button(
                    label="Open on IGDB",
                    style=discord.ButtonStyle.link,
                    url=game_context["igdb_url"],
                ))
            
            await interaction.followup.send(embed=embed, view=view)
            
            # Send goal completion notifications
            if completed_goals:
                await self._notify_goal_completions(interaction, completed_goals)
            
            # Sync roles after logging (may affect class progression)
            await self._sync_roles_and_notify(
                interaction,
                source="activity_log",
                actor=interaction.user,
            )
                
        except Exception as e:
            logger.error(f"Error in log_game command: {e}")
            await interaction.followup.send("An error occurred while logging the game.", ephemeral=True)
    
    @log_game.autocomplete("name")
    async def log_game_name_autocomplete(self, interaction: discord.Interaction, current: str):
        query = (current or "").strip()
        if len(query) < 2:
            return []

        try:
            results = await self.media_searcher.search_games(query, limit=25)
        except Exception as exc:
            logger.warning("IGDB game autocomplete failed for '%s': %s", query, exc)
            return []

        choices: List[app_commands.Choice[str]] = []
        for item in results:
            game_id = item.get("id")
            display = item.get("display") or item.get("name")
            if not game_id or not display:
                continue

            label = display
            
            # Add additional info to label
            info_bits: List[str] = []
            
            # Add platforms (abbreviated)
            if item.get("platforms"):
                platforms = item["platforms"][:3]  # Limit to first 3 platforms
                if platforms:
                    info_bits.append("/".join(platforms))
            
            if item.get("release_date"):
                try:
                    from datetime import datetime
                    year = datetime.fromtimestamp(item["release_date"]).year
                    info_bits.append(str(year))
                except:
                    pass
            if item.get("rating") is not None:
                rating = item["rating"]
                info_bits.append(f"{rating:.0f}/100")
            
            if info_bits:
                label = f"{display} — {' • '.join(info_bits)}"

            if len(label) > 100:
                label = label[:97] + "…"

            value = self.media_searcher.create_autocomplete_value(game_id, "name", source="igdb:GAME")
            choices.append(app_commands.Choice(name=label, value=value))

        return choices[:25]
    
    @app_commands.command(name="log_details", description="View detailed information about a specific log entry")
    @app_commands.describe(activity_id="The ID of the activity to view details for")
    async def log_details(self, interaction: discord.Interaction, activity_id: int):
        """View detailed information about a specific activity log entry"""
        try:
            # Ephemeral so a member cannot re-broadcast another member's log by id.
            await interaction.response.defer(ephemeral=True)

            user_id = str(interaction.user.id)
            
            # Get the activity by ID
            activity = await self.activity_repo.get_activity_by_id(activity_id)
            
            if not activity:
                await interaction.followup.send(
                    f"❌ Activity with ID {activity_id} not found.",
                    ephemeral=True
                )
                return
            

            
            # Fetch API metadata if available
            api_metadata = {}
            if activity.media_type == "visual_novel":
                # Check if VN metadata is already stored in activity meta
                vn_meta = activity.get_meta('vn')
                if vn_meta and vn_meta.get('id'):
                    # Use stored VN metadata
                    # Check NSFW status using proper threshold
                    image_info = {"sexual": vn_meta.get("image_sexual", 0)}
                    api_metadata = {
                        "type": "vndb",
                        "id": vn_meta["id"],
                        "url": vn_meta.get("url", f"https://vndb.org/{vn_meta['id']}"),
                        "cover_url": vn_meta.get("image_url"),
                        "cover_is_nsfw": is_image_explicit(image_info),
                        "rating": vn_meta.get("rating"),
                        "length": vn_meta.get("length"),
                        "released": vn_meta.get("released"),
                        "titles": vn_meta.get("titles", {})
                    }
                    logger.info(f"Using stored VN metadata for {activity.name}: {api_metadata}")
                else:
                    # Fall back to fetching VN metadata
                    try:
                        logger.info(f"Attempting to fetch VN metadata for: {activity.name}")
                        vn_context = await self._build_vn_context(activity.name)
                        logger.info(f"VN context result: {vn_context}")
                        if vn_context.get("vndb_id"):
                            api_metadata = {
                                "type": "vndb",
                                "id": vn_context["vndb_id"],
                                "url": vn_context["vndb_url"],
                                "cover_url": vn_context.get("cover_url"),
                                "cover_is_nsfw": vn_context.get("cover_is_nsfw", False),
                                "rating": vn_context.get("rating"),
                                "length": vn_context.get("length"),
                                "released": vn_context.get("released"),
                                "titles": vn_context.get("titles", {})
                            }
                            logger.info(f"Created VN api_metadata: {api_metadata}")
                        else:
                            logger.warning(f"No vndb_id found in VN context for {activity.name}")
                    except Exception as e:
                        logger.warning(f"Failed to fetch VN metadata for {activity.name}: {e}")
            
            elif activity.media_type == "anime":
                # Check if anime metadata is already stored in activity meta
                anilist_meta = activity.get_meta('anilist')
                if anilist_meta and anilist_meta.get('id'):
                    # Use stored anime metadata
                    api_metadata = {
                        "type": "anime",
                        "id": anilist_meta["id"],
                        "title": anilist_meta.get("title"),
                        "titles": anilist_meta.get("titles", {}),
                        "episodes": anilist_meta.get("episodes"),
                        "score": anilist_meta.get("average_score"),
                        "status": anilist_meta.get("status"),
                        "start_date": anilist_meta.get("start_date"),
                        "cover_url": anilist_meta.get("cover_url"),
                        "site_url": anilist_meta.get("site_url")
                    }
                    logger.info(f"Using stored anime metadata for {activity.name}: {api_metadata}")
                else:
                    # Fall back to fetching anime metadata
                    try:
                        # Use the existing anime search functionality
                        search_results = await self.media_searcher.search_anime(activity.name, limit=1)
                        if search_results:
                            anime_data = search_results[0]
                            api_metadata = {
                                "type": "anime",
                                "id": anime_data.get("id"),
                                "title": anime_data.get("display"),
                                "titles": anime_data.get("titles", {}),
                                "episodes": anime_data.get("episodes"),
                                "score": anime_data.get("average_score"),
                                "status": anime_data.get("status"),
                                "start_date": anime_data.get("start_date"),
                                "cover_url": anime_data.get("cover_url"),
                                "site_url": anime_data.get("site_url")
                            }
                    except Exception as e:
                        logger.warning(f"Failed to fetch anime metadata for {activity.name}: {e}")
            
            elif activity.media_type == "manga":
                # Check if manga metadata is already stored in activity meta
                anilist_meta = activity.get_meta('anilist')
                if anilist_meta and anilist_meta.get('id'):
                    # Use stored manga metadata
                    api_metadata = {
                        "type": "manga",
                        "id": anilist_meta["id"],
                        "title": anilist_meta.get("title"),
                        "titles": anilist_meta.get("titles", {}),
                        "chapters": anilist_meta.get("chapters"),
                        "volumes": anilist_meta.get("volumes"),
                        "score": anilist_meta.get("average_score"),
                        "status": anilist_meta.get("status"),
                        "start_date": anilist_meta.get("start_date"),
                        "cover_url": anilist_meta.get("cover_url"),
                        "site_url": anilist_meta.get("site_url")
                    }
                    logger.info(f"Using stored manga metadata for {activity.name}: {api_metadata}")
                else:
                    # Fall back to fetching manga metadata
                    try:
                        search_results = await self.media_searcher.search_manga(activity.name, limit=1)
                        if search_results:
                            manga_data = search_results[0]
                            api_metadata = {
                                "type": "manga",
                                "id": manga_data.get("id"),
                                "title": manga_data.get("display"),
                                "titles": manga_data.get("titles", {}),
                                "chapters": manga_data.get("chapters"),
                                "volumes": manga_data.get("volumes"),
                                "score": manga_data.get("average_score"),
                                "status": manga_data.get("status"),
                                "start_date": manga_data.get("start_date"),
                                "cover_url": manga_data.get("cover_url"),
                                "site_url": manga_data.get("site_url")
                            }
                    except Exception as e:
                        logger.warning(f"Failed to fetch manga metadata for {activity.name}: {e}")
            
            elif activity.media_type == "game":
                # Check if game metadata is already stored in activity meta
                igdb_meta = activity.get_meta('igdb')
                if igdb_meta and igdb_meta.get('id'):
                    # Use stored game metadata
                    api_metadata = {
                        "type": "game",
                        "id": igdb_meta["id"],
                        "name": igdb_meta.get("name"),
                        "rating": igdb_meta.get("rating"),
                        "release_date": igdb_meta.get("release_date"),
                        "genres": igdb_meta.get("genres", []),
                        "platforms": igdb_meta.get("platforms", []),
                        "cover_url": igdb_meta.get("cover_url")
                    }
                    logger.info(f"Using stored game metadata for {activity.name}: {api_metadata}")
                else:
                    # Fall back to fetching game metadata
                    try:
                        search_results = await self.media_searcher.search_games(activity.name, limit=1)
                        if search_results:
                            game_data = search_results[0]
                            api_metadata = {
                                "type": "game",
                                "id": game_data.get("id"),
                                "name": game_data.get("name"),
                                "rating": game_data.get("rating"),
                                "release_date": game_data.get("release_date"),
                                "genres": game_data.get("genres", []),  # Already a list of strings
                                "platforms": game_data.get("platforms", []),  # Already a list of strings  
                                "cover_url": game_data.get("cover_url")
                            }
                    except Exception as e:
                        logger.warning(f"Failed to fetch game metadata for {activity.name}: {e}")

            # Create detailed embed
            embed = discord.Embed(
                title=f"📋 Activity Details (ID: {activity_id})",
                color=discord.Color.blue(),
                timestamp=activity.date
            )
            
            embed.add_field(name="📚 Title", value=_clean_activity_name(activity.name), inline=False)
            embed.add_field(name="👤 User", value=f"<@{activity.user_id}>", inline=True)
            embed.add_field(name="🎯 Activity Type", value=activity.primary_type.title(), inline=True)
            
            if activity.media_type:
                media_display = activity.media_type.replace('_', ' ').title()
                embed.add_field(name="📖 Media Type", value=media_display, inline=True)
            
            embed.add_field(name="⏱️ Duration", value=_format_duration_value(activity.duration), inline=True)
            
            # Add metadata information if available
            if activity.meta:
                meta_fields = []
                
                # Episodes (anime)
                if activity.get_meta('episodes'):
                    meta_fields.append(f"Episodes: {activity.get_meta('episodes')}")
                
                # Characters (reading)
                if activity.get_meta('characters'):
                    chars = activity.get_meta('characters')
                    meta_fields.append(f"Characters: {chars:,}")
                    
                    # Character reading speed
                    if activity.get_meta('characters_per_hour'):
                        speed = activity.get_meta('characters_per_hour')
                        meta_fields.append(f"Reading Speed: {speed:.1f} chars/hour")
                    elif activity.get_meta('reading_speed') and not activity.get_meta('pages'):
                        # Fallback to reading_speed if no pages (for VNs)
                        speed = activity.get_meta('reading_speed')
                        meta_fields.append(f"Reading Speed: {speed:.1f} chars/hour")
                
                # Pages (books/manga)
                if activity.get_meta('pages'):
                    pages = activity.get_meta('pages')
                    meta_fields.append(f"Pages: {pages}")
                    
                    # Page reading speed
                    if activity.get_meta('pages_per_hour'):
                        speed = activity.get_meta('pages_per_hour')
                        meta_fields.append(f"Reading Speed: {speed:.1f} pages/hour")
                    elif activity.get_meta('reading_speed'):
                        # Use reading_speed for manga or books without separate pages_per_hour
                        speed = activity.get_meta('reading_speed')
                        meta_fields.append(f"Reading Speed: {speed:.1f} pages/hour")
                
                # URL (videos)
                if activity.get_meta('url'):
                    url = activity.get_meta('url')
                    meta_fields.append(f"[Video Link]({url})")
                
                if meta_fields:
                    embed.add_field(name="📊 Details", value="\n".join(meta_fields), inline=False)
            
            # Add API metadata if available
            logger.info(f"Processing api_metadata: {api_metadata}")
            if api_metadata:
                logger.info(f"API metadata type: {api_metadata.get('type')}")
                if api_metadata["type"] == "vndb":
                    api_fields = []
                    if api_metadata.get("rating"):
                        api_fields.append(f"Rating: {api_metadata['rating']:.1f}/10")
                    if api_metadata.get("length"):
                        length_map = {
                            1: "Very Short (< 2 hours)",
                            2: "Short (2-10 hours)", 
                            3: "Medium (10-30 hours)",
                            4: "Long (30-50 hours)",
                            5: "Very Long (> 50 hours)"
                        }
                        length_text = length_map.get(api_metadata["length"], f"Length: {api_metadata['length']}")
                        api_fields.append(f"Length: {length_text}")
                    if api_metadata.get("released"):
                        api_fields.append(f"Released: {api_metadata['released']}")
                    if api_metadata.get("url"):
                        api_fields.append(f"[View on VNDB]({api_metadata['url']})")
                    
                    if api_fields:
                        embed.add_field(name="📚 VNDB Info", value="\n".join(api_fields), inline=False)
                    
                    # Add cover image if available and not NSFW
                    if api_metadata.get("cover_url") and not api_metadata.get("cover_is_nsfw"):
                        embed.set_thumbnail(url=api_metadata["cover_url"])
                    elif api_metadata.get("cover_is_nsfw"):
                        embed.add_field(name="🔞 Cover", value="Cover hidden (marked explicit on VNDB)", inline=False)
                    # Metadata present but no cover URL
                    elif api_metadata.get("id") and not api_metadata.get("cover_url"):
                        logger.debug(f"VN {api_metadata['id']} has metadata but no cover URL")
                
                elif api_metadata["type"] == "anime":
                    api_fields = []
                    if api_metadata.get("score"):
                        # AniList scores are out of 100, convert to 10 scale
                        score = api_metadata["score"] / 10
                        api_fields.append(f"Score: {score:.1f}/10")
                    if api_metadata.get("episodes"):
                        api_fields.append(f"Episodes: {api_metadata['episodes']}")
                    if api_metadata.get("start_date"):
                        api_fields.append(f"Released: {api_metadata['start_date']}")
                    if api_metadata.get("status"):
                        api_fields.append(f"Status: {api_metadata['status']}")
                    if api_metadata.get("site_url"):
                        api_fields.append(f"[View on AniList]({api_metadata['site_url']})")
                    
                    if api_fields:
                        embed.add_field(name="📺 Anime Info", value="\n".join(api_fields), inline=False)
                    
                    if api_metadata.get("cover_url"):
                        embed.set_thumbnail(url=api_metadata["cover_url"])
                
                elif api_metadata["type"] == "manga":
                    api_fields = []
                    if api_metadata.get("score"):
                        # AniList scores are out of 100, convert to 10 scale
                        score = api_metadata["score"] / 10
                        api_fields.append(f"Score: {score:.1f}/10")
                    if api_metadata.get("chapters"):
                        api_fields.append(f"Chapters: {api_metadata['chapters']}")
                    if api_metadata.get("volumes"):
                        api_fields.append(f"Volumes: {api_metadata['volumes']}")
                    if api_metadata.get("start_date"):
                        api_fields.append(f"Released: {api_metadata['start_date']}")
                    if api_metadata.get("status"):
                        api_fields.append(f"Status: {api_metadata['status']}")
                    if api_metadata.get("site_url"):
                        api_fields.append(f"[View on AniList]({api_metadata['site_url']})")
                    
                    if api_fields:
                        embed.add_field(name="📖 Manga Info", value="\n".join(api_fields), inline=False)
                    
                    if api_metadata.get("cover_url"):
                        embed.set_thumbnail(url=api_metadata["cover_url"])
                
                elif api_metadata["type"] == "game":
                    api_fields = []
                    if api_metadata.get("rating"):
                        api_fields.append(f"Rating: {api_metadata['rating']:.1f}/100")
                    if api_metadata.get("genres"):
                        genres = ", ".join(api_metadata["genres"][:3])  # Limit to 3 genres
                        api_fields.append(f"Genres: {genres}")
                    if api_metadata.get("platforms"):
                        platforms = ", ".join(api_metadata["platforms"][:3])  # Limit to 3 platforms
                        api_fields.append(f"Platforms: {platforms}")
                    if api_metadata.get("release_date"):
                        from datetime import datetime
                        try:
                            release_date = datetime.fromtimestamp(api_metadata["release_date"])
                            api_fields.append(f"Released: {release_date.year}")
                        except:
                            pass
                    
                    # Add IGDB link if available  
                    if api_metadata.get("id"):
                        igdb_url = f"https://www.igdb.com/games/{api_metadata['id']}"
                        api_fields.append(f"[View on IGDB]({igdb_url})")
                    
                    if api_fields:
                        embed.add_field(name="🎮 Game Info", value="\n".join(api_fields), inline=False)
                    
                    if api_metadata.get("cover_url"):
                        # IGDB cover URLs need https prefix
                        cover_url = api_metadata["cover_url"]
                        if cover_url.startswith("//"):
                            cover_url = "https:" + cover_url
                        embed.set_thumbnail(url=cover_url)
            
            # Add comment if present
            if activity.comment:
                embed.add_field(name="💭 Comment", value=activity.comment, inline=False)
            
            # Add timestamps
            embed.add_field(name="📅 Activity Date", value=f"<t:{int(activity.date.timestamp())}:F>", inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error in log_info command: {e}")
            await interaction.followup.send("An error occurred while retrieving activity details.", ephemeral=True)
    
    @app_commands.command(name="log_history", description="View your activity history")
    @app_commands.describe(
        user="User to view history for (defaults to yourself)",
        page="Page number to view",
        activity_type="Filter by activity type (optional)",
        media_type="Filter by media type (optional)"
    )
    @app_commands.choices(
        activity_type=[
            app_commands.Choice(name="Reading", value="reading"),
            app_commands.Choice(name="Listening", value="listening")
        ],
        media_type=[
            app_commands.Choice(name="Book", value="book"),
            app_commands.Choice(name="Manga", value="manga"),
            app_commands.Choice(name="Anime", value="anime"),
            app_commands.Choice(name="Video", value="video"),
            app_commands.Choice(name="Visual Novel", value="visual_novel"),
            app_commands.Choice(name="Game", value="game")
        ]
    )
    async def log_history(self, interaction: discord.Interaction, 
                     user: discord.User = None, page: int = 1,
                     activity_type: app_commands.Choice[str] = None,
                     media_type: app_commands.Choice[str] = None):
        """View activity history with pagination"""
        try:
            await interaction.response.defer()
            
            target_user = user or interaction.user
            user_id = str(target_user.id)
            activity_filter = activity_type.value if activity_type else None
            media_filter = media_type.value if media_type else None
            
            page_size = 6
            
            # First, get total count to check if user has any activities
            total_count = await self.activity_repo.db.get_user_activity_count(
                user_id,
                None,
                primary_type=activity_filter,
                media_type=media_filter
            )
            
            if total_count == 0:
                embed = discord.Embed(
                    title="No Logs Found",
                    description="No immersion logs found for this user.",
                    color=discord.Color.blue()
                )
                await interaction.followup.send(embed=embed)
                return
            
            # Calculate max page and adjust if requested page is too high
            max_page = (total_count + page_size - 1) // page_size
            if page > max_page:
                page = max_page
            elif page < 1:
                page = 1
                
            offset = (page - 1) * page_size
            
            # Get paginated activities
            activity_page = await self.activity_repo.page_by_user_id(
                user_id=user_id,
                guild_id=None,
                page_size=page_size,
                offset=offset,
                primary_type=activity_filter,
                media_type=media_filter
            )
            
            # Create history embed
            embed = discord.Embed(
                title="Log History",
                color=discord.Color.blue()
            )
            
            embed.set_author(
                name=target_user.display_name,
                icon_url=target_user.display_avatar.url if target_user.display_avatar else None
            )

            filters_text = []
            if activity_filter:
                filters_text.append(f"Activity: {activity_filter.title()}")
            if media_filter:
                filters_text.append(f"Media: {media_filter.replace('_', ' ').title()}")
            if filters_text:
                embed.description = " | ".join(filters_text)
            
            # Add activity fields (always show IDs)
            for activity in activity_page.activities:
                field_name = f"ID: {activity.id}"
                embed.add_field(
                    name=field_name,
                    value=_format_history_entry(activity),
                    inline=True
                )
            
            # Update footer to include helpful message
            embed.set_footer(
                text=f"Page {activity_page.page} of {activity_page.page_count} • Use /log_details to view full activity info"
            )
            
            # Create navigation view
            view = HistoryView(
                user_id=user_id,
                guild_id=None,
                current_page=activity_page.page,
                total_pages=activity_page.page_count,
                activity_repo=self.activity_repo,
                invoker_id=interaction.user.id,
                activity_type=activity_filter,
                media_type=media_filter,
                page_size=page_size
            )
            
            await interaction.followup.send(embed=embed, view=view)
            
        except Exception as e:
            logger.error(f"Error in log_history command: {e}")
            await interaction.followup.send("An error occurred while retrieving history.", ephemeral=True)
    
    @app_commands.command(name="log_leaderboard", description="View the immersion leaderboard")
    @app_commands.describe(
        timeframe="Time period for the leaderboard",
        media_type="Filter by specific media type (optional)",
        activity_type="Filter by activity type (optional)",
        page="Page number to view (default: 1)",
        guild="Restrict results to a specific server (optional)"
    )
    @app_commands.choices(timeframe=[
        app_commands.Choice(name="Today", value="today"),
        app_commands.Choice(name="This Week", value="week"),
        app_commands.Choice(name="This Month", value="month"),
        app_commands.Choice(name="This Year", value="year"),
        app_commands.Choice(name="All Time", value="all")
    ], media_type=[
        app_commands.Choice(name="Book", value="book"),
        app_commands.Choice(name="Manga", value="manga"),
        app_commands.Choice(name="Anime", value="anime"),
        app_commands.Choice(name="Visual Novel", value="visual_novel"),
        app_commands.Choice(name="Video", value="video"),
        app_commands.Choice(name="Game", value="game")
    ], activity_type=[
        app_commands.Choice(name="Reading", value="reading"),
        app_commands.Choice(name="Listening", value="listening")
    ], metric=[
        app_commands.Choice(name="Duration", value="duration"),
        app_commands.Choice(name="Characters", value="characters"),
        app_commands.Choice(name="Pages", value="pages")
    ], guild=_build_leaderboard_guild_choices())
    async def log_leaderboard(
        self,
        interaction: discord.Interaction,
        timeframe: str = "all",
        media_type: str = None,
        activity_type: str = None,
        metric: str = "duration",
        page: int = 1,
        guild: str = None,
    ):
        """Show immersion leaderboard"""
        try:
            await interaction.response.defer()
            
            guild_filter = guild or None
            guild_label = LEADERBOARD_GUILD_FILTERS.get(guild_filter) if guild_filter else None
            target_guild: Optional[discord.Guild] = None
            guild_id_int: Optional[int] = None
            membership_cache: Dict[Tuple[int, int], bool] = {}
            prefetched_members: Optional[List[LeaderboardEntry]] = None

            if guild_filter:
                try:
                    guild_id_int = int(guild_filter)
                except (TypeError, ValueError):
                    guild_id_int = None
                    await interaction.followup.send(
                        "Invalid guild ID provided.",
                        ephemeral=True,
                    )
                    return

                if guild_id_int is not None:
                    target_guild = self.bot.get_guild(guild_id_int)
                    if target_guild is None:
                        try:
                            target_guild = await self.bot.fetch_guild(guild_id_int)
                        except Exception as fetch_exc:
                            logger.debug("Failed to fetch guild %s: %s", guild_filter, fetch_exc)
                            target_guild = None
            
            # Calculate time range
            now = datetime.now(timezone.utc)
            start_date = None
            
            if timeframe == "today":
                start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            elif timeframe == "week":
                days_since_monday = now.weekday()
                start_date = (now - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
            elif timeframe == "month":
                start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            elif timeframe == "year":
                start_date = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

            expected_activity_for_media = MEDIA_ACTIVITY_COMPATIBILITY.get(media_type) if media_type else None
            incompatible_filters = bool(
                media_type
                and activity_type
                and expected_activity_for_media
                and expected_activity_for_media != activity_type
            )
            
            metric_key = (metric or "duration").lower()
            metric_labels = {
                "duration": "Duration",
                "characters": "Characters",
                "pages": "Pages"
            }
            if metric_key not in metric_labels:
                metric_key = "duration"
            metric_label = metric_labels[metric_key]

            page_size = 10

            # Guild filtering: Check membership dynamically
            # If bot is in guild: use live Discord API
            # If bot is NOT in guild: use cached data from other bot instance
            
            bot_guild_ids = [g.id for g in self.bot.guilds]
            bot_is_in_target_guild = guild_id_int in bot_guild_ids if guild_id_int else False
            
            if guild_filter:
                guild_member_ids = set()
                
                if bot_is_in_target_guild:
                    # Bot is in guild - get live member list
                    logger.info(f"Bot is in guild {guild_id_int}, using live Discord member list")
                    target_guild = self.bot.get_guild(guild_id_int)
                    if target_guild:
                        guild_member_ids = {str(m.id) for m in target_guild.members if not m.bot}
                        # Cache guild info and members for other bot instances
                        guild_icon_url = target_guild.icon.url if target_guild.icon else None
                        await self.activity_repo.db.sync_guild_info(guild_filter, target_guild.name, guild_icon_url)
                        await self.activity_repo.db.sync_guild_members(guild_filter, guild_member_ids)
                        logger.info(f"Cached {len(guild_member_ids)} members for guild {guild_filter}")
                else:
                    # Bot is NOT in guild - use cached data
                    logger.info(f"Bot is NOT in guild {guild_id_int}, using cached member list")
                    guild_member_ids = await self.activity_repo.db.get_guild_member_ids(guild_filter)
                    logger.info(f"Loaded {len(guild_member_ids)} cached members for guild {guild_filter}")
                
                if not guild_member_ids:
                    guild_name = LEADERBOARD_GUILD_FILTERS.get(guild_filter, f"Guild {guild_filter}")
                    await interaction.followup.send(
                        f"No member data available for **{guild_name}**.\n\n"
                        f"This guild may need to be queried from a bot that is a member of it first.",
                        ephemeral=True,
                    )
                    return
                
                # Fetch ALL activities (no guild_id filter)
                fetch_limit = max(page * page_size * 3, 200)
                
                candidate_entries = await self.activity_repo.get_top_members(
                    guild_id=None,  # Get ALL activities
                    limit=fetch_limit,
                    start_date=start_date,
                    end_date=now,
                    media_type=media_type,
                    activity_type=activity_type,
                    metric=metric_key,
                )
                
                logger.info(f"Fetched {len(candidate_entries)} total entries, filtering by {len(guild_member_ids)} guild members...")
                
                # Filter to only guild members
                filtered_members = [
                    entry for entry in candidate_entries
                    if entry.user_id in guild_member_ids
                ]
                
                logger.info(f"After guild filter: {len(filtered_members)} members from guild {guild_filter}")
                prefetched_members = filtered_members
                total_users = len(filtered_members)
            else:
                total_users = await self.activity_repo.get_leaderboard_user_count(
                    guild_id=None,
                    start_date=start_date,
                    end_date=now,
                    media_type=media_type,
                    activity_type=activity_type,
                    metric=metric_key,
                )
            
            if total_users == 0:
                filters = []
                if media_type:
                    filters.append(media_type.replace('_', ' ').title())
                if activity_type:
                    filters.append(activity_type.title())
                if metric_key != "duration":
                    filters.append(f"Metric: {metric_label}")
                if guild_label:
                    filters.append(f"Guild: {guild_label}")
                filter_text = f" ({' | '.join(filters)})" if filters else ""
                description = "No logs found for this time period."
                if incompatible_filters:
                    media_label = media_type.replace('_', ' ').title()
                    expected_label = expected_activity_for_media.title() if expected_activity_for_media else None
                    if expected_label:
                        description = (
                            f"{media_label} logs are tracked as {expected_label} activities, "
                            "so that filter combination will always be empty. Try switching the activity type "
                            "or clearing one of the filters."
                        )
                embed = discord.Embed(
                    title=f"Leaderboard{filter_text}",
                    description=description,
                    color=discord.Color.blue()
                )
                await interaction.followup.send(embed=embed)
                return
            
            # Calculate max page and adjust if requested page is too high
            max_page = (total_users + page_size - 1) // page_size
            if page > max_page:
                page = max_page
            elif page < 1:
                page = 1
            
            # Calculate offset for pagination
            offset = (page - 1) * page_size

            if guild_filter:
                top_members_page = prefetched_members[offset:offset + page_size] if prefetched_members else []
            else:
                total_to_fetch = page * page_size
                top_members = await self.activity_repo.get_top_members(
                    guild_id=None,
                    limit=total_to_fetch,
                    start_date=start_date,
                    end_date=now,
                    media_type=media_type,
                    activity_type=activity_type,
                    metric=metric_key,
                )
                top_members_page = top_members[offset:offset + page_size]
            
            if not top_members_page:
                await interaction.followup.send("No data available for this page.", ephemeral=True)
                return
            
            # Create leaderboard embed
            filters = []
            if activity_type:
                filters.append(activity_type.title())
            if media_type:
                filters.append(media_type.replace('_', ' ').title())
            if metric_key != "duration":
                filters.append(f"Metric: {metric_label}")
            if guild_label:
                filters.append(f"Guild: {guild_label}")
            filter_text = f" - {' | '.join(filters)}" if filters else ""
            embed = discord.Embed(
                title=f"Immersion Leaderboard - {timeframe.title()}{filter_text}",
                color=discord.Color.gold(),
                timestamp=now
            )
            
            if start_date:
                embed.set_footer(text=f"Page {page} of {max_page} • Since {start_date.strftime('%Y-%m-%d')}")
            else:
                embed.set_footer(text=f"Page {page} of {max_page}")
            
            # Add leaderboard entries
            for i, member in enumerate(top_members_page, 1):
                rank = offset + i  # Calculate actual rank on full leaderboard
                display_name = await self._resolve_display_name(member.user_id, interaction.guild)
                
                metric_display = member.metric_display(metric_key)
                log_text = f"{member.activity_count} log{'s' if member.activity_count != 1 else ''}"
                details: List[str] = []
                if metric_key != "duration" and member.total_duration > 0:
                    details.append(f"Duration: {member.total_duration_str}")
                details.append(log_text)
                detail_text = " | ".join(details)
                value = metric_display
                if detail_text:
                    value += f"\n{detail_text}"
                
                embed.add_field(
                    name=f"{rank}. {display_name}",
                    value=value,
                    inline=False
                )
            
            # Create navigation view
            primary_guild_id = None
            if target_guild is not None:
                primary_guild_id = target_guild.id
            elif guild_id_int is not None:
                primary_guild_id = guild_id_int
            if primary_guild_id is None and interaction.guild:
                primary_guild_id = interaction.guild.id

            view = LeaderboardView(
                bot=self.bot,
                activity_repo=self.activity_repo,
                current_page=page,
                total_pages=max_page,
                timeframe=timeframe,
                start_date=start_date,
                now=now,
                media_type=media_type,
                activity_type=activity_type,
                metric=metric_key,
                page_size=page_size,
                guild_id=guild_filter,
                primary_guild_id=primary_guild_id,
                prefetched_entries=prefetched_members,
            )
            
            await interaction.followup.send(embed=embed, view=view)
            
        except Exception as e:
            logger.error(f"Error in log_leaderboard command: {e}")
            await interaction.followup.send("An error occurred while generating the leaderboard.", ephemeral=True)
    
    @app_commands.command(name="manage_class_roles", description="[ADMIN] Force refresh of VN class and ranking roles")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def class_roles_sync(self, interaction: discord.Interaction):
        """Manually trigger class role synchronization."""
        try:
            await interaction.response.defer(ephemeral=True)

            if not interaction.guild:
                await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
                return

            stats = await self.sync_reward_roles_for_guild(interaction.guild, source="manual")
            await interaction.followup.send(
                "VN class roles have been synchronized. "
                f"Assigned: {stats.get('class_assigned', 0)} class roles, "
                f"Removed: {stats.get('class_removed', 0)}. "
                f"Ranking roles → assigned: {stats.get('rank_assigned', 0)}, removed: {stats.get('rank_removed', 0)}.",
                ephemeral=True
            )

        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to modify roles.", ephemeral=True)
        except Exception as exc:
            logger.error(f"Error while running class_roles_sync: {exc}")
            await interaction.followup.send("An error occurred while syncing roles.", ephemeral=True)

    @app_commands.command(name="log_undo", description="Undo your last activity")
    @app_commands.describe(activity_id="Specific activity ID to undo (optional)")
    async def log_undo(self, interaction: discord.Interaction, activity_id: int = None):
        """Undo the last logged activity"""
        try:
            await interaction.response.defer()
            
            user_id = str(interaction.user.id)
            guild_id = str(interaction.guild_id) if interaction.guild_id else None
            
            sync_after_response = False

            if activity_id:
                # Undo specific activity
                success = await self.activity_repo.delete_activity(activity_id, user_id)
                if success:
                    await self.goal_service.refresh_user_goals(user_id)
                    sync_after_response = True

                    embed = discord.Embed(
                        title="Activity Undone",
                        description=f"Activity {activity_id} has been removed.",
                        color=discord.Color.green()
                    )
                else:
                    embed = discord.Embed(
                        title="Undo Failed",
                        description="Activity not found or you don't have permission to undo it.",
                        color=discord.Color.red()
                    )
            else:
                # Undo last activity
                last_activity = await self.activity_repo.get_latest_activity(user_id, guild_id)
                if not last_activity:
                    embed = discord.Embed(
                        title="No Logs to Undo",
                        description="You don't have any recent logs to undo.",
                        color=discord.Color.blue()
                    )
                else:
                    success = await self.activity_repo.delete_activity(last_activity.id, user_id)
                    if success:
                        await self.goal_service.refresh_user_goals(user_id)
                        sync_after_response = True

                        embed = discord.Embed(
                            title="Activity Undone",
                            description=f"Removed: {last_activity.name} ({_format_duration_value(last_activity.duration)})",
                            color=discord.Color.green()
                        )
                    else:
                        embed = discord.Embed(
                            title="Undo Failed",
                            description="Failed to undo the activity.",
                            color=discord.Color.red()
                        )
            
            await interaction.followup.send(embed=embed)

            if sync_after_response:
                await self._sync_roles_and_notify(
                    interaction,
                    source="activity_undo",
                    actor=interaction.user,
                )
            
        except Exception as e:
            logger.error(f"Error in log_undo command: {e}")
            await interaction.followup.send("An error occurred while undoing the activity.", ephemeral=True)
    
    @app_commands.command(name="log_library", description="View your library of logged titles")
    @app_commands.describe(
        user="User to view library for (defaults to yourself)",
        media_type="Filter by specific media type (optional)",
        activity_type="Filter by activity type (optional)",
        sort_by="Sort order (default: recently logged)",
        page="Page number to view"
    )
    @app_commands.choices(
        media_type=[
            app_commands.Choice(name="Book", value="book"),
            app_commands.Choice(name="Manga", value="manga"),
            app_commands.Choice(name="Anime", value="anime"),
            app_commands.Choice(name="Video", value="video"),
            app_commands.Choice(name="Visual Novel", value="visual_novel"),
            app_commands.Choice(name="Game", value="game"),
            app_commands.Choice(name="Manual Log", value="manual")
        ],
        activity_type=[
            app_commands.Choice(name="Reading", value="reading"),
            app_commands.Choice(name="Listening", value="listening")
        ],
        sort_by=[
            app_commands.Choice(name="⏱️ Time (Most)", value="duration"),
            app_commands.Choice(name="📊 Count (Most Logs)", value="count"),
            app_commands.Choice(name="📅 Recently Logged", value="recent"),
            app_commands.Choice(name="🔤 Alphabetical", value="alphabetical")
        ]
    )
    async def log_library(self, interaction: discord.Interaction,
                         user: discord.User = None,
                         media_type: str = None,
                         activity_type: str = None,
                         sort_by: str = "recent",
                         page: int = 1):
        """View library of all logged titles"""
        try:
            await interaction.response.defer()
            
            target_user = user or interaction.user
            user_id = str(target_user.id)
            
            # Get library data
            library_items = await self.activity_repo.get_user_library(
                user_id=user_id,
                media_type=media_type,
                activity_type=activity_type,
                sort_by=sort_by
            )
            
            logger.info(f"Library query returned {len(library_items)} items for user {user_id}")
            
            if not library_items:
                embed = discord.Embed(
                    title="📚 Library Empty",
                    description="No titles found with the current filters.",
                    color=discord.Color.blue()
                )
                await interaction.followup.send(embed=embed)
                return
            
            # For alphabetical sort, fetch Japanese titles for ALL VNs BEFORE pagination
            # This ensures sorting happens with the actual displayed titles
            if sort_by == "alphabetical":
                vns_with_vndb_id = [
                    item for item in library_items
                    if item.get('vndb_id') and item['media_type'] == 'visual_novel'
                ]
                
                if vns_with_vndb_id:
                    # Batch VNDB API calls (up to 10 VNs per request)
                    async def fetch_japanese_titles_batch(session, vndb_ids_batch):
                        """Fetch titles for multiple VNs in a single API call"""
                        try:
                            payload = {
                                "filters": ["id", "=", vndb_ids_batch if len(vndb_ids_batch) > 1 else vndb_ids_batch[0]],
                                "fields": "id, titles.title, titles.lang",
                                "results": len(vndb_ids_batch)
                            }
                            async with session.post(
                                "https://api.vndb.org/kana/vn",
                                json=payload,
                                headers={"Content-Type": "application/json"},
                                timeout=aiohttp.ClientTimeout(total=5)
                            ) as response:
                                if response.status == 200:
                                    data = await response.json()
                                    return data.get("results", [])
                        except Exception as exc:
                            logger.warning(f"Failed to fetch batch of {len(vndb_ids_batch)} titles: {exc}")
                        return []
                    
                    # Create mapping of vndb_id to item for quick lookup
                    vndb_id_to_items = {}
                    for item in vns_with_vndb_id:
                        vndb_id = item['vndb_id']
                        if vndb_id not in vndb_id_to_items:
                            vndb_id_to_items[vndb_id] = []
                        vndb_id_to_items[vndb_id].append(item)
                    
                    # Batch VNDB IDs (10 per request - VNDB API limit)
                    batch_size = 10
                    vndb_ids = list(vndb_id_to_items.keys())
                    batches = [vndb_ids[i:i + batch_size] for i in range(0, len(vndb_ids), batch_size)]
                    
                    async with aiohttp.ClientSession() as session:
                        # Fetch all batches in parallel
                        batch_results = await asyncio.gather(
                            *[fetch_japanese_titles_batch(session, batch) for batch in batches],
                            return_exceptions=True
                        )
                        
                        # Process results and update item names
                        for result in batch_results:
                            if isinstance(result, list):
                                for vn in result:
                                    vn_id = vn.get("id")
                                    if vn_id and vn_id in vndb_id_to_items:
                                        titles = vn.get("titles", [])
                                        japanese_title = None
                                        for title_obj in titles:
                                            if title_obj.get("lang") == "ja":
                                                japanese_title = title_obj.get("title")
                                                break
                                        if japanese_title:
                                            # Update all items with this vndb_id
                                            for item in vndb_id_to_items[vn_id]:
                                                item['name'] = japanese_title
                    
                    # Re-sort with updated Japanese titles
                    import unicodedata
                    def normalize_for_sort(text: str) -> str:
                        return unicodedata.normalize('NFD', text.lower())
                    library_items.sort(key=lambda x: normalize_for_sort(x['name']))
                    logger.info(f"Re-sorted {len(library_items)} items alphabetically after fetching Japanese titles")
            
            # Pagination setup - 3 items per page for gallery view (mobile-friendly)
            items_per_page = 3
            total_pages = (len(library_items) + items_per_page - 1) // items_per_page
            
            logger.info(f"Pagination: {len(library_items)} items, {items_per_page} per page = {total_pages} total pages")
            
            # Clamp page number
            if page < 1:
                page = 1
            elif page > total_pages:
                page = total_pages
            
            start_idx = (page - 1) * items_per_page
            end_idx = start_idx + items_per_page
            page_items = library_items[start_idx:end_idx]
            
            # For VNs without cached NSFW status, check current VNDB cover and update if needed (parallel)
            vns_to_check = [
                item for item in page_items
                if (item.get('cover_url') 
                    and not item.get('cover_is_nsfw') 
                    and item.get('vndb_id')
                    and item['media_type'] == 'visual_novel')
            ]
            
            if vns_to_check:
                async def check_vn(session, item):
                    try:
                        payload = {
                            "filters": ["id", "=", item['vndb_id']],
                            "fields": "image.url, image.sexual, image.violence"
                        }
                        async with session.post(
                            "https://api.vndb.org/kana/vn",
                            json=payload,
                            headers={"Content-Type": "application/json"},
                            timeout=aiohttp.ClientTimeout(total=2)
                        ) as response:
                            if response.status == 200:
                                data = await response.json()
                                if data.get("results"):
                                    vn = data["results"][0]
                                    image_info = vn.get("image") or {}
                                    current_cover_url = image_info.get("url")
                                    
                                    # Update to current cover URL and rating
                                    if current_cover_url:
                                        item['cover_url'] = current_cover_url
                                    item['cover_is_nsfw'] = is_image_explicit(image_info.get("sexual"))
                    except Exception as exc:
                        logger.warning("Failed NSFW check for %s: %s", item['name'], exc)
                
                async with aiohttp.ClientSession() as session:
                    await asyncio.gather(*[check_vn(session, item) for item in vns_to_check])

            # Media type emojis
            media_emojis = {
                'visual_novel': '📖', 'manga': '📕', 'anime': '📺',
                'book': '📚', 'video': '🎬', 'game': '🎮'
            }
            
            # Build filter description
            filter_parts = []
            if media_type:
                filter_parts.append(f"📑 {media_type.replace('_', ' ').title()}")
            if activity_type:
                filter_parts.append(f"⚡ {activity_type.title()}")
            
            # Add sort indicator
            sort_labels = {
                "duration": "⏱️ Sorted by Time",
                "count": "📊 Sorted by Log Count", 
                "recent": "📅 Sorted by Recent",
                "alphabetical": "🔤 Sorted Alphabetically"
            }
            filter_parts.append(sort_labels.get(sort_by, ""))
            
            # Create multiple embeds - one per item (gallery style)
            embeds = []
            
            for i, item in enumerate(page_items, start=start_idx + 1):
                emoji = media_emojis.get(item['media_type'], '📄')
                duration_str = _format_duration_value(item['total_duration']) if item['total_duration'] > 0 else "0m"
                count = item['total_count']

                # Format dates
                try:
                    if item['last_logged']:
                        last_date = datetime.fromisoformat(item['last_logged']).strftime('%Y-%m-%d')
                    else:
                        last_date = "Unknown"
                except:
                    last_date = "Unknown"

                # Truncate title if too long
                display_name = item['name'][:250] if len(item['name']) > 250 else item['name']

                # Create individual embed for this item
                item_embed = discord.Embed(
                    title=f"{emoji} {display_name}",
                    color=discord.Color.purple()
                )

                # Add cover as thumbnail if available and not NSFW
                if item.get('cover_url') and not item.get('cover_is_nsfw'):
                    item_embed.set_thumbnail(url=item['cover_url'])
                elif item.get('cover_url') and item.get('cover_is_nsfw'):
                    item_embed.add_field(name="🔞 NSFW Cover", value="Cover hidden", inline=False)

                # Add stats
                item_embed.add_field(name="⏱️ Total Time", value=f"**{duration_str}**", inline=True)
                item_embed.add_field(name="📊 Log Count", value=f"**{count}** entries", inline=True)
                item_embed.add_field(name="📅 Last Logged", value=last_date, inline=True)
                
                # Add database link if available
                db_link = None
                if item['media_type'] == 'visual_novel' and item.get('vndb_id'):
                    db_link = f"[VNDB](https://vndb.org/{item['vndb_id']})"
                elif item['media_type'] in ['anime', 'manga'] and item.get('anilist_id'):
                    db_link = f"[AniList](https://anilist.co/{item['media_type']}/{item['anilist_id']})"
                elif item['media_type'] == 'game' and item.get('igdb_id'):
                    db_link = f"[IGDB](https://www.igdb.com/games/{item['igdb_id']})"
                elif item['media_type'] == 'video' and item.get('youtube_id'):
                    db_link = f"[YouTube](https://www.youtube.com/watch?v={item['youtube_id']})"
                
                if db_link:
                    item_embed.add_field(name="🔗 Database", value=db_link, inline=True)
                
                # Add media-specific metrics if available
                total_chars = item.get('total_characters', 0)
                total_item_pages = item.get('total_pages', 0)
                total_eps = item.get('total_episodes', 0)
                
                if total_chars > 0:
                    item_embed.add_field(name="🔡 Total Characters", value=f"{int(total_chars):,}", inline=True)
                if total_item_pages > 0:
                    formatted_pages = f"{total_item_pages:,.1f}".rstrip('0').rstrip('.')
                    item_embed.add_field(name="📄 Total Pages", value=formatted_pages, inline=True)
                if total_eps > 0:
                    item_embed.add_field(name="📺 Total Episodes", value=f"{int(total_eps)}", inline=True)
                
                # Add filter info to first embed only
                if i == start_idx + 1:
                    item_embed.set_author(
                        name=f"{target_user.display_name}'s Library",
                        icon_url=target_user.display_avatar.url if target_user.display_avatar else None
                    )
                
                # Add footer to last embed with pagination and filter info
                if i == start_idx + len(page_items):
                    footer_parts = [f"Page {page}/{total_pages}", f"{len(library_items)} total titles"]
                    if filter_parts:
                        footer_parts.extend(filter_parts)
                    item_embed.set_footer(text=" • ".join(footer_parts))
                
                embeds.append(item_embed)
            
            # Create navigation view if needed
            if total_pages > 1:
                view = LibraryNavigationView(
                    user_id=user_id,
                    target_user=target_user,
                    current_page=page,
                    total_pages=total_pages,
                    invoker_id=interaction.user.id,
                    media_type=media_type,
                    activity_type=activity_type,
                    sort_by=sort_by,
                    activity_repo=self.activity_repo,
                    cog=self
                )
                await interaction.followup.send(embeds=embeds, view=view)
            else:
                await interaction.followup.send(embeds=embeds)
            
        except Exception as e:
            logger.error(f"Error in log_library command: {e}")
            await interaction.followup.send("An error occurred while retrieving library.", ephemeral=True)
    
    @app_commands.command(name="log_server_leaderboard", description="View server rankings by total immersion time")
    @app_commands.describe(
        timeframe="Time period for the leaderboard",
        media_type="Filter by specific media type (optional)",
        activity_type="Filter by activity type (optional)",
        page="Page number to view (default: 1)"
    )
    @app_commands.choices(timeframe=[
        app_commands.Choice(name="Today", value="today"),
        app_commands.Choice(name="This Week", value="week"),
        app_commands.Choice(name="This Month", value="month"),
        app_commands.Choice(name="This Year", value="year"),
        app_commands.Choice(name="All Time", value="all")
    ], media_type=[
        app_commands.Choice(name="Book", value="book"),
        app_commands.Choice(name="Manga", value="manga"),
        app_commands.Choice(name="Anime", value="anime"),
        app_commands.Choice(name="Visual Novel", value="visual_novel"),
        app_commands.Choice(name="Video", value="video")
    ], activity_type=[
        app_commands.Choice(name="Reading", value="reading"),
        app_commands.Choice(name="Listening", value="listening")
    ])
    async def log_server_leaderboard(
        self,
        interaction: discord.Interaction,
        timeframe: str = "all",
        media_type: str = None,
        activity_type: str = None,
        page: int = 1,
    ):
        """Show server rankings based on total immersion time"""
        try:
            await interaction.response.defer()
            
            # Calculate time range
            now = datetime.now(timezone.utc)
            start_date = None
            
            if timeframe == "today":
                start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            elif timeframe == "week":
                days_since_monday = now.weekday()
                start_date = (now - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
            elif timeframe == "month":
                start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            elif timeframe == "year":
                start_date = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

            # Get server totals from database
            try:
                import aiosqlite
                
                # Build filters
                base_filters = ["ia.deleted_at IS NULL", "ia.user_id NOT IN (SELECT user_id FROM user_blacklist)"]
                filters = list(base_filters)
                params = []
                
                if start_date:
                    filters.append("ia.date >= ?")
                    params.append(start_date.isoformat())
                    filters.append("ia.date <= ?")
                    params.append(now.isoformat())
                    
                if media_type:
                    filters.append("ia.media_type = ?")
                    params.append(media_type)

                if activity_type:
                    filters.append("ia.primary_type = ?")
                    params.append(activity_type)
                
                # Build WHERE clause
                filtered_where_clause = " AND ".join(filters)
                primary_where_clause = " AND ".join(base_filters)

                async with aiosqlite.connect(self.db.db_path) as db:
                    db.row_factory = aiosqlite.Row

                    query = f"""
                        WITH single_guild_members AS (
                            SELECT guild_id, user_id
                            FROM immersion_guild_members
                            GROUP BY user_id
                            HAVING COUNT(*) = 1
                        ), activity_primary_base AS (
                            SELECT
                                ia.user_id,
                                COALESCE(ia.guild_id, sgm.guild_id) AS resolved_guild_id,
                                ia.duration
                            FROM immersion_activities AS ia
                            LEFT JOIN single_guild_members AS sgm ON sgm.user_id = ia.user_id
                            WHERE {primary_where_clause}
                        ), activity_base AS (
                            SELECT
                                ia.user_id,
                                COALESCE(ia.guild_id, sgm.guild_id) AS resolved_guild_id,
                                ia.duration,
                                ia.primary_type,
                                ia.media_type,
                                ia.date
                            FROM immersion_activities AS ia
                            LEFT JOIN single_guild_members AS sgm ON sgm.user_id = ia.user_id
                            WHERE {filtered_where_clause}
                        ), user_guild_totals AS (
                            SELECT
                                user_id,
                                resolved_guild_id,
                                SUM(duration) AS total_duration
                            FROM activity_primary_base
                            WHERE resolved_guild_id IS NOT NULL
                            GROUP BY user_id, resolved_guild_id
                        ), user_primary_calculated AS (
                            SELECT user_id, resolved_guild_id AS primary_guild_id
                            FROM (
                                SELECT
                                    user_id,
                                    resolved_guild_id,
                                    total_duration,
                                    ROW_NUMBER() OVER (
                                        PARTITION BY user_id
                                        ORDER BY total_duration DESC, resolved_guild_id
                                    ) AS rn
                                FROM user_guild_totals
                            )
                            WHERE rn = 1
                        ), user_primary_overrides AS (
                            SELECT user_id, primary_guild_id
                            FROM immersion_users
                            WHERE primary_guild_id IS NOT NULL
                        ), user_primary AS (
                            SELECT
                                calc.user_id,
                                COALESCE(override.primary_guild_id, calc.primary_guild_id) AS primary_guild_id
                            FROM user_primary_calculated AS calc
                            LEFT JOIN user_primary_overrides AS override ON override.user_id = calc.user_id
                            WHERE COALESCE(override.primary_guild_id, calc.primary_guild_id) IS NOT NULL
                            UNION
                            SELECT override.user_id, override.primary_guild_id
                            FROM user_primary_overrides AS override
                            WHERE override.user_id NOT IN (SELECT user_id FROM user_primary_calculated)
                        ), activity_resolved AS (
                            SELECT
                                COALESCE(up.primary_guild_id, ab.resolved_guild_id) AS resolved_guild_id,
                                ab.user_id,
                                ab.duration,
                                ab.primary_type,
                                ab.media_type,
                                ab.date
                            FROM activity_base AS ab
                            LEFT JOIN user_primary AS up ON up.user_id = ab.user_id
                        )
                        SELECT
                            ar.resolved_guild_id AS guild_id,
                            ig.guild_name,
                            ig.guild_icon_url,
                            COUNT(DISTINCT ar.user_id) AS unique_users,
                            SUM(ar.duration) AS total_duration,
                            COUNT(*) AS activity_count
                        FROM activity_resolved AS ar
                        LEFT JOIN immersion_guilds AS ig ON ig.guild_id = ar.resolved_guild_id
                        WHERE ar.resolved_guild_id IS NOT NULL
                        GROUP BY ar.resolved_guild_id
                        HAVING total_duration > 0
                        ORDER BY total_duration DESC
                    """

                    async with db.execute(query, params) as cursor:
                        rows = await cursor.fetchall()

                # Convert to list of dicts
                server_data = [
                    {
                        "guild_id": row["guild_id"],
                        "guild_name": row["guild_name"],
                        "guild_icon_url": row["guild_icon_url"],
                        "unique_users": row["unique_users"],
                        "total_duration": row["total_duration"],
                        "activity_count": row["activity_count"]
                    }
                    for row in rows
                ]

            except Exception as e:
                logger.error(f"Error fetching server leaderboard data: {e}")
                await interaction.followup.send("An error occurred while fetching server data.", ephemeral=True)
                return

            if not server_data:
                await interaction.followup.send("No server data found for the selected timeframe.", ephemeral=True)
                return

            # Pagination
            page_size = 10
            total_servers = len(server_data)
            page_count = (total_servers + page_size - 1) // page_size
            page = max(1, min(page, page_count))
            
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            page_data = server_data[start_idx:end_idx]

            # Build filter text for embed
            filter_parts = []
            if media_type:
                media_labels = {
                    "book": "Book",
                    "manga": "Manga",
                    "anime": "Anime",
                    "visual_novel": "Visual Novel",
                    "video": "Video"
                }
                filter_parts.append(media_labels.get(media_type, media_type.title()))
            if activity_type:
                filter_parts.append(activity_type.title())
            
            filter_text = ""
            if filter_parts:
                filter_text = f" ({', '.join(filter_parts)})"

            # Create embed with dynamic color based on position
            embed_color = discord.Color.gold()
            if interaction.guild_id:
                current_guild_id = str(interaction.guild_id)
                for idx, server in enumerate(page_data, start=start_idx + 1):
                    if server["guild_id"] == current_guild_id:
                        if idx == 1:
                            embed_color = discord.Color.from_rgb(255, 215, 0)  # Gold
                        elif idx == 2:
                            embed_color = discord.Color.from_rgb(192, 192, 192)  # Silver
                        elif idx == 3:
                            embed_color = discord.Color.from_rgb(205, 127, 50)  # Bronze
                        else:
                            embed_color = discord.Color.blurple()
                        break
            
            embed = discord.Embed(
                title=f"🏆 Server Leaderboard - {timeframe.title()}{filter_text}",
                color=embed_color,
                timestamp=now
            )
            
            # Add server entries with improved formatting
            leaderboard_lines = []
            for idx, server in enumerate(page_data, start=start_idx + 1):
                guild_id = server["guild_id"]
                duration = server["total_duration"]
                users = server["unique_users"]
                logs = server["activity_count"]
                
                # Try to get guild name and icon from multiple sources
                guild_name = server.get("guild_name") or None
                guild_icon_url = server.get("guild_icon_url") or None
                
                try:
                    guild_id_int = int(guild_id)
                    
                    # First try: Get from current bot instance
                    guild_obj = self.bot.get_guild(guild_id_int)
                    if guild_obj:
                        guild_name = guild_obj.name
                        guild_icon_url = guild_obj.icon.url if guild_obj.icon else None
                        # Sync to database for other bot instances
                        await self.db.sync_guild_info(guild_id, guild_name, guild_icon_url)
                    elif not guild_name:
                        # Second try: Get from database cache (synced by other bot instances)
                        cached_info = await self.db.get_guild_info(guild_id)
                        if cached_info:
                            guild_name = cached_info.get("guild_name")
                            guild_icon_url = cached_info.get("guild_icon_url")
                except (ValueError, TypeError):
                    pass
                
                # Fallback: Show shortened ID without conflicting markdown
                if not guild_name:
                    try:
                        guild_id_short = str(guild_id)[-4:]  # Last 4 digits
                        guild_name = f"Server #{guild_id_short}"
                    except:
                        guild_name = f"Server {guild_id}"
                
                # Medal emojis for top 3
                rank_emoji = ""
                if idx == 1:
                    rank_emoji = "🥇 "
                elif idx == 2:
                    rank_emoji = "🥈 "
                elif idx == 3:
                    rank_emoji = "🥉 "
                
                # Highlight current server
                is_current_server = interaction.guild_id and str(interaction.guild_id) == guild_id
                server_display = f"**{guild_name}**" if not is_current_server else f"**{guild_name}** ⭐"
                
                duration_display = _format_duration_value(duration)
                avg_per_user = duration / users if users > 0 else 0
                avg_display = _format_duration_value(int(avg_per_user))
                
                leaderboard_lines.append(
                    f"{rank_emoji}**#{idx}**: {server_display}\n"
                    f"└ 📊 {duration_display} • {users} users • {logs:,} logs • {avg_display}/user"
                )
            
            embed.description = "\n\n".join(leaderboard_lines)
            
            # Add the top server's icon as thumbnail on the first page
            if page == 1 and page_data:
                try:
                    top_guild_id = page_data[0]["guild_id"]
                    guild_id_int = int(top_guild_id)
                    
                    # Try to get icon from current bot or cache
                    guild_obj = self.bot.get_guild(guild_id_int)
                    if guild_obj and guild_obj.icon:
                        embed.set_thumbnail(url=guild_obj.icon.url)
                    elif page_data[0].get("guild_icon_url"):
                        embed.set_thumbnail(url=page_data[0]["guild_icon_url"])
                    else:
                        cached_info = await self.db.get_guild_info(top_guild_id)
                        if cached_info and cached_info.get("guild_icon_url"):
                            embed.set_thumbnail(url=cached_info["guild_icon_url"])
                except Exception as e:
                    logger.debug(f"Could not set thumbnail: {e}")
            
            # Add footer with pagination info
            embed.set_footer(text=f"Page {page}/{page_count} • {total_servers} servers total • ⭐ = Your server")
            
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in log_server_leaderboard command: {e}", exc_info=True)
            await interaction.followup.send("An error occurred while generating the server leaderboard.", ephemeral=True)
    
    @app_commands.command(name="profile", description="View an immersion profile for yourself or another member")
    @app_commands.describe(
        user="Member to inspect (defaults to yourself)",
        image="Display as image card instead of embed (default: True)"
    )
    async def profile(self, interaction: discord.Interaction, user: Optional[discord.User] = None, image: bool = True):
        guild = interaction.guild
        if not guild:
            if interaction.response.is_done():
                await interaction.followup.send("This command can only be used inside a server.", ephemeral=True)
            else:
                await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        # Convert User to Member if they're in the guild
        target_member: Optional[discord.Member] = None
        if user:
            # If user provided, try to get them as a member
            if isinstance(user, discord.Member):
                target_member = user
            else:
                target_member = guild.get_member(user.id)
        elif isinstance(interaction.user, discord.Member):
            target_member = interaction.user
        else:
            target_member = guild.get_member(interaction.user.id)

        if not target_member:
            if interaction.response.is_done():
                await interaction.followup.send("I couldn't find that member in this server.", ephemeral=True)
            else:
                await interaction.response.send_message("I couldn't find that member in this server.", ephemeral=True)
            return

        await interaction.response.defer()

        user_id = str(target_member.id)
        summary_task = asyncio.create_task(self.activity_repo.get_user_activity_summary(user_id))
        vn_duration_task = asyncio.create_task(
            self.activity_repo.sum_activity_duration(user_id, media_type=MediaType.VISUAL_NOVEL.value)
        )
        completions_task = asyncio.create_task(self.db.get_user_completion_stats(target_member.id))
        blacklist_task = asyncio.create_task(self.db.is_user_blacklisted(user_id))
        primary_guild_task = asyncio.create_task(self.activity_repo.get_user_primary_guild(user_id))

        summary, vn_seconds, completion_stats, is_blacklisted, primary_guild = await asyncio.gather(
            summary_task,
            vn_duration_task,
            completions_task,
            blacklist_task,
            primary_guild_task
        )

        now = datetime.now(timezone.utc)
        join_date = _ensure_utc(target_member.joined_at)
        time_in_server_text = _format_timespan(now - join_date) if join_date else None

        verification_cog = self.bot.get_cog("VerificationCog")
        if verification_cog and hasattr(verification_cog, "get_highest_quiz_level"):
            try:
                quiz_level = verification_cog.get_highest_quiz_level(target_member.id)
            except Exception:
                quiz_level = self._infer_quiz_level_from_roles(target_member)
        else:
            quiz_level = self._infer_quiz_level_from_roles(target_member)

        class_definitions = self._get_class_role_definitions_for_guild(guild)
        vn_hours = (vn_seconds or 0) / 3600
        current_class = "Unranked"
        current_threshold = 0
        next_class: Optional[Dict[str, Any]] = None
        class_role_color = None  # Will store RGB tuple
        
        if class_definitions:
            current_class = class_definitions[0]["name"]
            current_class_def = class_definitions[0]
            
            for definition in class_definitions:
                if vn_hours >= definition["min_hours"]:
                    current_class = definition["name"]
                    current_threshold = definition["min_hours"]
                    current_class_def = definition
                else:
                    next_class = definition
                    break
            
            # Get the role color for the current class
            # Look up role by name since definitions don't have role_id
            role_name = current_class_def.get("name")
            logger.info(f"Current class: {current_class}, looking for role with name: {role_name}")
            
            if role_name:
                # Find role by name in the guild
                role = discord.utils.get(guild.roles, name=role_name)
                logger.info(f"Found role: {role}, color value: {role.color.value if role else 'N/A'}")
                
                if role and role.color.value != 0:  # If role has a color set
                    # Convert discord.Color to RGB tuple
                    class_role_color = (role.color.r, role.color.g, role.color.b)
                    logger.info(f"Using class role color for {current_class}: {class_role_color}")
                else:
                    logger.info(f"Role '{role_name}' has no color set (value=0) or role not found")
            else:
                logger.info(f"No role name in class definition for {current_class}")

        hours_to_next: Optional[float] = None
        if next_class:
            hours_to_next = max(0.0, next_class["min_hours"] - vn_hours)

        summary = summary or {}
        total_logs = summary.get("total_logs", 0) or 0
        total_hours = (summary.get("total_duration", 0) or 0) / 3600
        reading_hours = (summary.get("reading_duration", 0) or 0) / 3600
        listening_hours = (summary.get("listening_duration", 0) or 0) / 3600
        avg_minutes = (summary.get("avg_duration", 0) or 0) / 60
        total_characters = summary.get("total_characters", 0) or 0
        last_log = _ensure_utc(summary.get("last_log"))
        first_log = _ensure_utc(summary.get("first_log"))
        media_breakdown = summary.get("media_breakdown", [])

        top_medium = next((entry for entry in media_breakdown if entry.get("duration")), None)
        completion_stats = completion_stats or {}
        monthly_records = completion_stats.get("monthly_completions") or []
        current_month_key = now.strftime("%Y-%m")
        this_month_completed = 0
        for record in monthly_records:
            if record.get("month") == current_month_key:
                this_month_completed = record.get("count", 0)
                break

        vn_of_month_completed = completion_stats.get("vn_of_month_completed", 0) or 0
        total_completed_vns = completion_stats.get("total_completed", 0) or 0

        # Create mobile-friendly profile
        embed_color = 0x7289DA  # Discord blurple
        if is_blacklisted:
            embed_color = discord.Color.dark_red().value

        embed = discord.Embed(
            title=f"📊 {target_member.display_name}",
            color=embed_color,
            timestamp=now
        )

        # User avatar
        avatar_url = target_member.display_avatar.url if target_member.display_avatar else None
        embed.set_thumbnail(url=avatar_url)

        # Build compact description with key info
        desc_lines = [
            f"**Level {quiz_level} • {current_class}**",
            f"{target_member.mention}",
        ]

        if is_blacklisted:
            desc_lines.append("🚫 **Blacklisted**")
        
        if time_in_server_text:
            desc_lines.append(f"*Member for {time_in_server_text}*")
        
        embed.description = "\n".join(desc_lines)

        # Primary guild information
        if primary_guild:
            guild_id_value = primary_guild.get("guild_id")
            guild_source = primary_guild.get("source", "detected")
            guild_name = primary_guild.get("guild_name")
            guild_icon_url = None

            if guild_id_value:
                try:
                    guild_obj = self.bot.get_guild(int(guild_id_value))
                except (TypeError, ValueError):
                    guild_obj = None

                if guild_obj:
                    guild_name = guild_obj.name
                    guild_icon_url = guild_obj.icon.url if guild_obj.icon else None
                    try:
                        await self.db.sync_guild_info(str(guild_obj.id), guild_name, guild_icon_url)
                    except Exception:
                        pass
                elif not guild_name:
                    guild_name = f"Server #{str(guild_id_value)[-4:]}" if guild_id_value else "Unknown Server"

            if not guild_id_value:
                guild_display = "Unknown Server"
            else:
                guild_display = guild_name or f"Server {guild_id_value}"

            detail_lines = [guild_display]

            if guild_id_value:
                detail_lines.append(f"`{guild_id_value}`")

            if guild_source == "override":
                detail_lines.append("*Set manually*")
            else:
                total_duration = primary_guild.get("total_duration") or 0
                if total_duration:
                    detected_hours = total_duration / 3600
                    detail_lines.append(f"*Auto-detected ({detected_hours:.1f}h logged)*")
                else:
                    detail_lines.append("*Auto-detected*")

            embed.add_field(name="Primary Server", value="\n".join(detail_lines), inline=False)
        else:
            embed.add_field(name="Primary Server", value="Not determined yet", inline=False)

        # Immersion Stats (consolidated)
        reading_pct = (reading_hours/total_hours*100) if total_hours > 0 else 0
        listening_pct = (listening_hours/total_hours*100) if total_hours > 0 else 0
        
        stats_value = (
            f"📖 **VN Time:** {vn_hours:.1f}h\n"
            f"📚 **Reading:** {reading_hours:.1f}h ({reading_pct:.0f}%)\n"
            f"🎧 **Listening:** {listening_hours:.1f}h ({listening_pct:.0f}%)\n"
            f"⏱️ **Total:** {total_hours:.1f}h • {total_logs:,} logs\n"
            f"📈 **Avg:** {avg_minutes:.1f}m per log"
        )
        
        embed.add_field(name="📊 Immersion Stats", value=stats_value, inline=False)

        # Class Progress (more compact)
        if next_class and hours_to_next is not None:
            current_progress = vn_hours
            next_threshold = next_class["min_hours"]
            
            progress_in_tier = current_progress - current_threshold
            tier_width = next_threshold - current_threshold
            progress_ratio = min(progress_in_tier / tier_width, 1.0) if tier_width > 0 else 1.0
            
            # Shorter progress bar for mobile
            progress_bar = self._create_detailed_progress_bar(progress_ratio, width=12)
            
            # Special message for base tier with no logs
            if vn_hours == 0 and current_class == "魑魅魍魎":
                # Show "first log" instead of hours for the 鬼 class
                next_class_display = f"{next_class['name']} (first log)" if next_class['name'] == "鬼" else f"{next_class['name']} ({next_threshold:.0f}h)"
                progress_value = (
                    f"**Current:** {current_class} ({current_threshold:.0f}h)\n"
                    f"**Next:** {next_class_display}\n"
                    f"{progress_bar}\n"
                    f"**Get started:** Log your first session to advance!"
                )
            else:
                # Show progress within current tier explicitly
                progress_value = (
                    f"**Current:** {current_class} ({current_threshold:.0f}h)\n"
                    f"**Next:** {next_class['name']} ({next_threshold:.0f}h)\n"
                    f"{progress_bar}\n"
                    f"**In Tier:** {_format_hours_display(progress_in_tier)} / {_format_hours_display(tier_width)}\n"
                    f"**Need:** {_format_hours_display(hours_to_next)} to advance"
                )
        else:
            progress_bar = "████████████"  # Shorter full bar
            progress_value = (
                f"**Current:** {current_class} ({current_threshold:.0f}h)\n"
                f"**Status:** ✨ MAX TIER\n"
                f"{progress_bar}\n"
                f"**VN Time:** {_format_hours_display(vn_hours)}"
            )

        embed.add_field(name="⚔️ VN Class Progress", value=progress_value, inline=False)

        # VN Completions (simplified)
        completions_value = (
            f"🏆 **This Month:** {int(this_month_completed)}\n"
            f"📅 **VN of Month:** {int(vn_of_month_completed)}\n"
            f"📚 **Other VNs:** {max(0, int(total_completed_vns) - int(vn_of_month_completed))}\n"
            f"🌟 **Total:** {int(total_completed_vns)} completed"
        )

        embed.add_field(name="🏆 Completions", value=completions_value, inline=False)

        # Simplified footer
        embed.set_footer(text=f"Profile • {total_hours:.1f}h logged", icon_url=avatar_url)

        # Generate image card if requested
        if image:
            generator = ProfileCardGenerator()
            try:
                card_buffer = await generator.generate_profile_card(
                    username=target_member.display_name,
                    user_id=str(target_member.id),
                    avatar_url=avatar_url,
                    quiz_level=quiz_level,
                    current_class=current_class,
                    vn_hours=vn_hours,
                    total_hours=total_hours,
                    total_logs=total_logs,
                    reading_hours=reading_hours,
                    listening_hours=listening_hours,
                    avg_minutes=avg_minutes,
                    current_threshold=current_threshold,
                    next_threshold=next_class.get("min_hours") if next_class else None,
                    next_class=next_class.get("name") if next_class else None,
                    member_duration=time_in_server_text,
                    is_blacklisted=is_blacklisted,
                    this_month_completed=this_month_completed,
                    total_completed=total_completed_vns,
                    class_role_color=class_role_color,
                    total_characters=total_characters
                )
                
                # Send as file attachment
                file = discord.File(card_buffer, filename="profile.png")
                await interaction.followup.send(file=file)
                return
            except Exception as e:
                logger.error(f"Error generating profile card: {e}", exc_info=True)
                # Fall back to embed if image generation fails
                await interaction.followup.send("Failed to generate profile image, showing embed instead:", embed=embed)
                return
            finally:
                # Always close the generator to prevent resource leaks
                await generator.close()
        
        # Send embed version
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="log_stats", description="View your immersion statistics")
    @app_commands.checks.cooldown(1, 15.0, key=lambda i: i.user.id)
    @app_commands.describe(
        user="User to view stats for (defaults to yourself)",
        days="Number of days to include (default: 30)",
        media_type="Filter by specific media type (optional)",
        activity_type="Filter by activity type (optional)",
        metric="Metric to display (default: duration)"
    )
    @app_commands.choices(
        media_type=[
            app_commands.Choice(name="Book", value="book"),
            app_commands.Choice(name="Manga", value="manga"),
            app_commands.Choice(name="Anime", value="anime"),
            app_commands.Choice(name="Visual Novel", value="visual_novel"),
            app_commands.Choice(name="Video", value="video"),
            app_commands.Choice(name="Game", value="game"),
            app_commands.Choice(name="Manual Log", value="manual")
        ],
        activity_type=[
            app_commands.Choice(name="Reading", value="reading"),
            app_commands.Choice(name="Listening", value="listening")
        ],
        metric=[
            app_commands.Choice(name="⏱️ Duration", value="duration"),
            app_commands.Choice(name="🔤 Characters", value="characters"),
            app_commands.Choice(name="📄 Pages", value="pages"),
            app_commands.Choice(name="📺 Episodes", value="episodes")
        ]
    )
    async def log_stats(self, interaction: discord.Interaction, 
                   user: discord.User = None, days: int = 30,
                   media_type: str = None, activity_type: str = None,
                   metric: str = "duration"):
        """View user statistics"""
        try:
            await interaction.response.defer()
            
            target_user = user or interaction.user
            user_id = str(target_user.id)
            
            logger.info(f"Parameters: days={days}, media_type={media_type}, activity_type={activity_type}, metric={metric}")
            
            # Check for incompatible filter combinations before proceeding
            if media_type and activity_type:
                expected_activity = MEDIA_ACTIVITY_COMPATIBILITY.get(media_type)
                logger.info(f"Filter compatibility check: media_type={media_type}, activity_type={activity_type}, expected={expected_activity}")
                if expected_activity and expected_activity != activity_type:
                    media_label = media_type.replace('_', ' ').title()
                    activity_label = activity_type.title()
                    expected_label = expected_activity.title()
                    
                    logger.warning(f"Incompatible filter combination detected")
                    embed = discord.Embed(
                        title="⚠️ Incompatible Filters",
                        description=(
                            f"**{media_label}** activities are tracked as **{expected_label}**, "
                            f"not **{activity_label}**.\n\n"
                            f"This filter combination will not return any results. "
                            f"Please adjust your filters:\n"
                            f"• Use **{expected_label}** for Activity Type, or\n"
                            f"• Choose a different Media Type"
                        ),
                        color=discord.Color.orange()
                    )
                    embed.set_footer(text="💡 Tip: Manga/VN/Book → Reading | Anime/Video → Listening")
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return
            
            # Get user stats with filters
            logger.info(f"Fetching stats from repository...")
            stats = await self.activity_repo.get_user_stats(
                user_id, None, days, 
                media_type=media_type, 
                activity_type=activity_type
            )
            
            logger.info(f"Raw stats received: {stats}")
            
            # Calculate key metrics
            total_duration = stats['total_duration']
            total_hours = total_duration / 3600 if total_duration > 0 else 0
            avg_per_day = total_hours / days if days > 0 else 0
            total_activities = stats['total_activities']
            reading_duration = stats['reading_duration']
            listening_duration = stats['listening_duration']
            reading_hours = reading_duration / 3600 if reading_duration > 0 else 0
            listening_hours = listening_duration / 3600 if listening_duration > 0 else 0
            
            logger.info(f"Calculated metrics:")
            logger.info(f"  total_duration={total_duration}s, total_hours={total_hours:.2f}h")
            logger.info(f"  total_activities={total_activities}")
            logger.info(f"  avg_per_day={avg_per_day:.2f}h/day")
            logger.info(f"  reading: {reading_duration}s ({reading_hours:.2f}h)")
            logger.info(f"  listening: {listening_duration}s ({listening_hours:.2f}h)")
            avg_duration_seconds = stats['avg_duration'] or 0
            
            # Determine primary color based on activity
            if total_hours > 100:
                color = discord.Color.from_rgb(255, 215, 0)  # Gold
            elif total_hours > 50:
                color = discord.Color.from_rgb(192, 192, 192)  # Silver
            elif total_hours > 25:
                color = discord.Color.from_rgb(205, 127, 50)  # Bronze
            else:
                color = discord.Color.from_rgb(88, 101, 242)  # Discord Blurple
            
            # Build title with active filters
            title = f"📊 Immersion Statistics"
            
            # Create description with filters
            description_parts = [f"**{days}-Day Overview**"]
            if media_type or activity_type:
                filter_parts = []
                if media_type:
                    filter_parts.append(f"📑 {media_type.replace('_', ' ').title()}")
                if activity_type:
                    filter_parts.append(f"⚡ {activity_type.title()}")
                description_parts.append("│ " + " • ".join(filter_parts))
            
            description = "\n".join(description_parts)
            
            # Create modern embed
            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
                timestamp=datetime.now(timezone.utc)
            )
            
            embed.set_author(
                name=target_user.display_name,
                icon_url=target_user.display_avatar.url if target_user.display_avatar else None
            )
            
            if target_user.display_avatar:
                embed.set_thumbnail(url=target_user.display_avatar.url)
            
            # Main stats section with emoji icons and better visual hierarchy
            # Always show "(filtered)" since log_stats always applies date range filter
            main_stats = f"""
**⏱️ Total Time Logged**
> **{total_hours:,.1f} hours** across **{total_activities:,} activities (filtered)**

**📈 Daily Average**  
> **{avg_per_day:.2f} hrs/day** • {avg_duration_seconds/60:.1f} min per log
"""
            embed.add_field(name="", value=main_stats, inline=False)
            
            logger.info(f"Embed created with title: {title}")
            logger.info(f"Description parts: {description_parts}")
            
            # Create interactive view first so its methods are available
            logger.info(f"Creating InteractiveStatsView with metric={metric}")
            view = InteractiveStatsView(
                self,
                target_user,
                user_id,
                interaction.user.id,
                days=days,
                media_type=media_type,
                activity_type=activity_type,
                metric=metric
            )
            
            # Generate initial chart with proper metric support
            chart_file = None
            try:
                logger.info(f"Generating initial overview with metric={metric}...")
                
                # Get stats with metric support (reuse view's method)
                metric_stats = await view._get_stats_with_metric()
                
                # Generate overview embed and chart
                overview_embed, overview_file = await view._generate_overview_embed(metric_stats)
                
                # Use the metric-aware embed and chart
                embed = overview_embed
                chart_file = overview_file
                
                logger.info(f"Initial overview generated successfully with metric={metric}")
            except Exception as e:
                logger.error(f"Error generating initial overview: {e}", exc_info=True)
                # Fallback to basic embed without chart if error occurs
            
            logger.info(f"Sending response to user...")
            if chart_file:
                await interaction.followup.send(embed=embed, file=chart_file, view=view)
            else:
                await interaction.followup.send(embed=embed, view=view)
            
            
        except Exception as e:
            logger.error(f"Error in log_stats command: {e}", exc_info=True)
            await interaction.followup.send("An error occurred while retrieving statistics.", ephemeral=True)

    @app_commands.command(name="log_overview", description="View server-wide and global immersion statistics")
    @app_commands.checks.cooldown(1, 15.0, key=lambda i: i.user.id)
    @app_commands.describe(
        timeframe="Time period to analyze",
        metric="Primary metric to display",
        guild="Filter to specific server (defaults to current)"
    )
    @app_commands.choices(
        timeframe=[
            app_commands.Choice(name="Last 7 Days", value="week7"),
            app_commands.Choice(name="Last 30 Days", value="month30"),
            app_commands.Choice(name="Last 90 Days", value="month90"),
            app_commands.Choice(name="This Year", value="year"),
            app_commands.Choice(name="All Time", value="all")
        ],
        metric=[
            app_commands.Choice(name="⏱️ Duration", value="duration"),
            app_commands.Choice(name="🔤 Characters", value="characters"),
            app_commands.Choice(name="📄 Pages", value="pages"),
            app_commands.Choice(name="📺 Episodes", value="episodes")
        ],
        guild=_build_leaderboard_guild_choices()
    )
    async def log_overview(
        self,
        interaction: discord.Interaction,
        timeframe: str = "month30",
        metric: str = "duration",
        guild: str = None
    ):
        """View server-wide immersion statistics"""
        try:
            # Defer immediately to prevent timeout
            try:
                await interaction.response.defer()
            except discord.errors.NotFound:
                # Interaction already expired, try to send followup anyway
                logger.warning("Interaction expired before defer, attempting followup")

            # Resolve guild filtering
            # If guild parameter is provided, use it for filtering
            # If not provided (None), show global stats across all guilds
            guild_filter = guild  # Use the provided guild parameter directly

            # Determine guild name for display
            if guild_filter:
                guild_name = LEADERBOARD_GUILD_FILTERS.get(guild_filter, f"Guild {guild_filter}")
            else:
                guild_name = "Global"

            # Calculate time range
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            start_date = self._calculate_overview_start_date(timeframe, now)

            logger.info(f"Guild: {guild_name} (ID: {guild_filter})")
            logger.info(f"Timeframe: {timeframe}, Metric: {metric}")
            logger.info(f"Start date: {start_date}, End date: {now}")

            # Fetch guild stats
            # guild_id=None means global (all guilds), otherwise filter to specific guild
            stats = await self.activity_repo.get_guild_overview_stats(
                guild_id=guild_filter,
                start_date=start_date,
                end_date=now,
                metric=metric
            )

            logger.info(f"Stats retrieved: {stats['total_logs']} logs, {stats['active_users']} users")

            # Handle empty state
            if stats['total_logs'] == 0:
                embed = discord.Embed(
                    title="📊 No Activity Found",
                    description=f"No immersion logs found for **{guild_name}** in this time period.\n\n"
                               f"**Suggestions:**\n"
                               f"• Try selecting a longer time period\n"
                               f"• Encourage members to log their immersion with `/log`\n"
                               f"• Use `/log_leaderboard` to see all-time rankings",
                    color=discord.Color.orange()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            # Create interactive view
            view = OverviewNavigationView(
                cog=self,
                guild_id=guild_filter,
                guild_name=guild_name,
                timeframe=timeframe,
                metric=metric,
                interaction_user=interaction.user
            )

            # Generate initial summary page
            logger.info("Generating initial summary page...")
            embed, chart_file = await view._generate_summary_page(stats, start_date, now)

            # Send response
            if chart_file:
                await interaction.followup.send(embed=embed, file=chart_file, view=view)
            else:
                await interaction.followup.send(embed=embed, view=view)


        except Exception as e:
            logger.error(f"Error in log_overview command: {e}", exc_info=True)
            await interaction.followup.send(
                "An error occurred while retrieving overview statistics.",
                ephemeral=True
            )

    def _calculate_overview_start_date(self, timeframe: str, now: datetime) -> Optional[datetime]:
        """Calculate start date from timeframe choice for overview command"""
        from datetime import timedelta
        if timeframe == "today":
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif timeframe == "week7":
            return (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif timeframe == "month30":
            return (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif timeframe == "month90":
            return (now - timedelta(days=89)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif timeframe == "year":
            return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif timeframe == "all":
            return None  # No start date = all time
        # Legacy support
        elif timeframe == "week":
            days_since_monday = now.weekday()
            return (now - timedelta(days=days_since_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        elif timeframe == "month":
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return None

    @app_commands.command(name="log_heatmap", description="View your activity heatmap")
    @app_commands.checks.cooldown(1, 15.0, key=lambda i: i.user.id)
    @app_commands.describe(
        user="User to view heatmap for (defaults to yourself)",
        media_type="Filter by specific media type (optional)",
        activity_type="Filter by activity type (optional)",
        metric="Metric to display (default: duration)"
    )
    @app_commands.choices(
        media_type=[
            app_commands.Choice(name="Book", value="book"),
            app_commands.Choice(name="Manga", value="manga"),
            app_commands.Choice(name="Anime", value="anime"),
            app_commands.Choice(name="Visual Novel", value="visual_novel"),
            app_commands.Choice(name="Video", value="video"),
            app_commands.Choice(name="Game", value="game")
        ],
        activity_type=[
            app_commands.Choice(name="Reading", value="reading"),
            app_commands.Choice(name="Listening", value="listening")
        ],
        metric=[
            app_commands.Choice(name="⏱️ Duration", value="duration"),
            app_commands.Choice(name="🔤 Characters", value="characters"),
            app_commands.Choice(name="📄 Pages", value="pages"),
            app_commands.Choice(name="📺 Episodes", value="episodes")
        ]
    )
    async def log_heatmap(self, interaction: discord.Interaction, 
                         user: discord.User = None,
                         media_type: str = None,
                         activity_type: str = None,
                         metric: str = "duration"):
        """View activity heatmap"""
        try:
            await interaction.response.defer()
            
            target_user = user or interaction.user
            user_id = str(target_user.id)
            
            # Check for incompatible metric combinations
            if metric in ["characters", "pages"] and media_type in ["anime", "video"]:
                metric_label = metric.title()
                media_label = media_type.replace('_', ' ').title()
                await interaction.followup.send(
                    f"❌ {metric_label} metric is not compatible with {media_label}. "
                    f"Please use Duration metric or choose a different media type.",
                    ephemeral=True
                )
                return
            
            # Check for incompatible filter combinations
            if media_type and activity_type:
                expected_activity = MEDIA_ACTIVITY_COMPATIBILITY.get(media_type)
                if expected_activity and expected_activity != activity_type:
                    media_label = media_type.replace('_', ' ').title()
                    activity_label = activity_type.title()
                    expected_label = expected_activity.title()
                    
                    embed = discord.Embed(
                        title="⚠️ Incompatible Filters",
                        description=(
                            f"**{media_label}** activities are tracked as **{expected_label}**, "
                            f"not **{activity_label}**.\n\n"
                            f"This filter combination will not return any results."
                        ),
                        color=discord.Color.orange()
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return
            
            # Get all-time stats for the heatmap
            if metric == "duration":
                all_time_stats = await self.activity_repo.get_user_stats(
                    user_id, None, days=9999,
                    media_type=media_type,
                    activity_type=activity_type
                )
                # Get 9 months of data for the heatmap display
                display_stats = await self.activity_repo.get_user_stats(
                    user_id, None, 270,
                    media_type=media_type,
                    activity_type=activity_type
                )
            else:
                # Non-duration metrics require querying activities and aggregating
                from collections import defaultdict
                
                # Get all-time activities
                end_date = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=999999)
                first_activity = await self.activity_repo.get_first_activity_date(
                    user_id=user_id,
                    media_type=media_type,
                    activity_type=activity_type
                )
                start_date_all = first_activity if first_activity else end_date - timedelta(days=365)
                
                activities_all = await self.activity_repo.get_activities_in_range(
                    user_id=user_id,
                    guild_id=None,
                    start_date=start_date_all,
                    end_date=end_date
                )
                
                # Apply filters
                if media_type:
                    # Handle 'manual' as a special filter for NULL media_type
                    if media_type == 'manual':
                        activities_all = [a for a in activities_all if a.get('media_type') is None]
                    else:
                        activities_all = [a for a in activities_all if a.get('media_type') == media_type]
                if activity_type:
                    activities_all = [a for a in activities_all if a.get('primary_type') == activity_type]
                
                # Aggregate all-time stats
                daily_breakdown_all = defaultdict(lambda: {'activity_count': 0, 'value': 0})
                for activity in activities_all:
                    meta = activity.get('meta', {})
                    value = meta.get(metric, 0) if isinstance(meta.get(metric), (int, float)) else 0
                    if value > 0:
                        activity_date = activity.get('date')
                        if isinstance(activity_date, str):
                            date_obj = datetime.fromisoformat(activity_date)
                        else:
                            date_obj = activity_date
                        date_str = date_obj.strftime('%Y-%m-%d')
                        daily_breakdown_all[date_str]['activity_count'] += 1
                        daily_breakdown_all[date_str]['value'] += value
                
                all_time_stats = {
                    'daily_breakdown': [
                        {
                            'date': date,
                            'activity_count': data['activity_count'],
                            'duration': data['value']
                        }
                        for date, data in sorted(daily_breakdown_all.items(), reverse=True)
                    ]
                }
                
                # Get 9 months of data
                start_date_display = end_date - timedelta(days=270)
                activities_display = await self.activity_repo.get_activities_in_range(
                    user_id=user_id,
                    guild_id=None,
                    start_date=start_date_display,
                    end_date=end_date
                )
                
                # Apply filters
                if media_type:
                    # Handle 'manual' as a special filter for NULL media_type
                    if media_type == 'manual':
                        activities_display = [a for a in activities_display if a.get('media_type') is None]
                    else:
                        activities_display = [a for a in activities_display if a.get('media_type') == media_type]
                if activity_type:
                    activities_display = [a for a in activities_display if a.get('primary_type') == activity_type]
                
                # Aggregate display stats
                daily_breakdown_display = defaultdict(lambda: {'activity_count': 0, 'value': 0})
                for activity in activities_display:
                    meta = activity.get('meta', {})
                    value = meta.get(metric, 0) if isinstance(meta.get(metric), (int, float)) else 0
                    if value > 0:
                        activity_date = activity.get('date')
                        if isinstance(activity_date, str):
                            date_obj = datetime.fromisoformat(activity_date)
                        else:
                            date_obj = activity_date
                        date_str = date_obj.strftime('%Y-%m-%d')
                        daily_breakdown_display[date_str]['activity_count'] += 1
                        daily_breakdown_display[date_str]['value'] += value
                
                display_stats = {
                    'daily_breakdown': [
                        {
                            'date': date,
                            'activity_count': data['activity_count'],
                            'duration': data['value']
                        }
                        for date, data in sorted(daily_breakdown_display.items(), reverse=True)
                    ]
                }
            
            # Generate heatmap
            image_buffer = await self._generate_activity_heatmap(
                display_stats.get('daily_breakdown', []),
                target_user.display_name,
                270,
                all_time_stats=all_time_stats,
                media_type=media_type,
                activity_type=activity_type,
                metric=metric
            )
            
            if not image_buffer:
                await interaction.followup.send("Unable to generate heatmap.", ephemeral=True)
                return
            
            # Build description with filters
            description_parts = ["*Last 9 months of activity*"]
            if media_type or activity_type or metric != "duration":
                filter_parts = []
                if media_type:
                    filter_parts.append(f"📑 {media_type.replace('_', ' ').title()}")
                if activity_type:
                    filter_parts.append(f"⚡ {activity_type.title()}")
                if metric != "duration":
                    metric_emojis = {"characters": "🔤", "pages": "📄", "episodes": "📺"}
                    filter_parts.append(f"{metric_emojis.get(metric, '📏')} {metric.title()}")
                description_parts.append(" • ".join(filter_parts))
            
            # Create embed for heatmap
            embed = discord.Embed(
                title="📊 Activity Heatmap",
                description="\n".join(description_parts),
                color=discord.Color.from_rgb(88, 101, 242),
                timestamp=datetime.now(timezone.utc)
            )
            
            embed.set_author(
                name=target_user.display_name,
                icon_url=target_user.display_avatar.url if target_user.display_avatar else None
            )
            
            file = discord.File(image_buffer, filename="heatmap.png")
            embed.set_image(url="attachment://heatmap.png")
            
            # Create navigation view
            heatmap_view = HeatmapNavigationView(
                self,
                target_user,
                user_id,
                media_type,
                activity_type,
                all_time_stats,
                interaction.user.id,
                offset_months=0,
                metric=metric
            )
            
            await interaction.followup.send(embed=embed, file=file, view=heatmap_view)
            
        except Exception as e:
            logger.error(f"Error in log_heatmap command: {e}")
            await interaction.followup.send("An error occurred while generating the heatmap.", ephemeral=True)
    
    def _configure_japanese_font(self):
        """Configure matplotlib with a Japanese-capable font CHAIN.

        Sets font.sans-serif to a fallback list (not a single family) ending in the
        bundled DejaVu Sans, so if the preferred font can't be loaded/rendered the
        text degrades to a readable fallback instead of silently disappearing.
        MS Gothic is demoted: some matplotlib builds render its .ttc as blank glyphs.
        """
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm

        # Preferred Japanese-capable fonts, most-reliable first.
        japanese_fonts = [
            'Meiryo', 'Yu Gothic',  # Windows (render reliably)
            'Noto Sans CJK JP', 'Noto Sans JP',  # Linux (Google Noto)
            'Yu Mincho', 'MS Gothic', 'MS Mincho',  # Windows older / .ttc fallbacks
            'Arial Unicode MS', 'Hiragino Sans',  # Mac
        ]
        # Always end the chain with bundled fonts so text never vanishes if the
        # preferred face fails to load (DejaVu lacks CJK but still renders Latin).
        fallback_fonts = ['DejaVu Sans', 'Liberation Sans']

        try:
            available = {
                fm.FontProperties(fname=fname).get_name()
                for fname in fm.findSystemFonts(fontpaths=None, fontext='ttf')
            }
        except Exception as e:
            logger.debug(f"Could not enumerate system fonts: {e}")
            available = set()

        chain = [f for f in japanese_fonts if f in available] + fallback_fonts
        plt.rcParams['font.family'] = 'sans-serif'
        plt.rcParams['font.sans-serif'] = chain
        plt.rcParams['axes.unicode_minus'] = False  # Fix minus sign rendering
        logger.info("matplotlib font chain: %s", ", ".join(chain))
    
    async def _generate_media_pie_chart(
        self,
        media_breakdown: List[Dict[str, Any]],
        username: str,
        metric: str = "duration"
    ) -> Optional[io.BytesIO]:
        """Generate a pie chart for media breakdown"""
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            
            # Configure Japanese font support
            self._configure_japanese_font()
            
            if not media_breakdown:
                return None
            
            # Sort and get top 5
            sorted_media = sorted(media_breakdown, key=lambda x: x['duration'], reverse=True)[:5]
            
            # Calculate total for consistent percentages
            total_media_duration = sum(m['duration'] for m in sorted_media)
            
            # Determine units based on metric
            if metric == "duration":
                unit_display = "h"
                value_format = ".1f"
                divisor = 3600
            elif metric == "characters":
                unit_display = " chars"
                value_format = ",.0f"
                divisor = 1
            elif metric == "pages":
                unit_display = " pgs"
                value_format = ".0f"
                divisor = 1
            else:  # episodes
                unit_display = " eps"
                value_format = ".0f"
                divisor = 1
            
            # Extract data
            labels = []
            sizes = []
            colors_list = []
            media_percentages = []
            
            media_colors = {
                'visual_novel': '#9B59B6',  # Purple
                'manga': '#E74C3C',         # Red
                'anime': '#3498DB',         # Blue
                'book': '#2ECC71',          # Green
                'video': '#E67E22',         # Orange
                'game': '#F39C12',          # Yellow/Gold
                'manual': '#95A5A6'         # Gray (for manual logs without media type)
            }
            
            for media in sorted_media:
                media_type = media['media_type']
                display_name = media_type.replace('_', ' ').title()
                value = media['duration'] / divisor
                percentage = (media['duration'] / total_media_duration * 100) if total_media_duration > 0 else 0
                
                labels.append(f"{display_name}\n{value:{value_format}}{unit_display}")
                sizes.append(media['duration'])
                colors_list.append(media_colors.get(media_type, '#95A5A6'))
                media_percentages.append(percentage)
            
            # Create figure
            fig, ax = plt.subplots(figsize=(8, 6), facecolor='#2C2F33')
            ax.set_facecolor('#2C2F33')
            
            # Create custom autopct function using the pre-calculated percentages
            percentage_values = media_percentages.copy()
            def make_autopct(values):
                def autopct(pct):
                    if values:
                        return f'{values.pop(0):.1f}%'
                    return f'{pct:.1f}%'
                return autopct
            
            # Create pie chart
            wedges, texts, autotexts = ax.pie(
                sizes,
                labels=labels,
                colors=colors_list,
                autopct=make_autopct(percentage_values),
                startangle=90,
                textprops={'color': 'white', 'fontsize': 11, 'weight': 'bold'}
            )
            
            # Style percentage text
            for autotext in autotexts:
                autotext.set_color('white')
                autotext.set_fontsize(10)
                autotext.set_weight('bold')
            
            # Equal aspect ratio ensures circular pie
            ax.axis('equal')
            
            # Handle possessive apostrophe
            possessive = f"{username}'" if username.endswith('s') else f"{username}'s"
            
            # Add title
            plt.title(f"{possessive} Media Distribution", 
                     color='white', fontsize=14, weight='bold', pad=20)
            
            # Save to buffer
            buffer = io.BytesIO()
            plt.savefig(buffer, format='png', dpi=100, bbox_inches='tight', 
                       facecolor='#2C2F33', edgecolor='none')
            buffer.seek(0)
            plt.close(fig)
            
            return buffer
            
        except Exception as e:
            logger.error(f"Error generating media pie chart: {e}")
            return None
    
    async def _generate_cumulative_progress_chart(
        self,
        user_id: str,
        days: int,
        username: str,
        media_type: Optional[str] = None,
        activity_type: Optional[str] = None,
        metric: str = "duration"
    ) -> Optional[io.BytesIO]:
        """Generate a cumulative progress line chart showing progress over time by media type

        Runs matplotlib operations in a thread pool to avoid blocking the event loop.
        """
        try:
            logger.info(f"Generating cumulative chart for {username}")
            
            # Get activities for the time period (async)
            # Use end of today in UTC so all of today's activities are included
            end_date = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=999999)
            
            # For "All Time" (days >= 9999), find the user's first activity
            if days >= 9999:
                # Get first activity date for this user
                first_activity = await self.activity_repo.get_first_activity_date(
                    user_id=user_id,
                    media_type=media_type,
                    activity_type=activity_type
                )
                if first_activity:
                    start_date = first_activity
                else:
                    # Fallback if no activities found
                    start_date = end_date - timedelta(days=365)
            else:
                start_date = end_date - timedelta(days=days)
            
            logger.info(f"Chart date range: {start_date} to {end_date}")
            
            activities = await self.activity_repo.get_activities_in_range(
                user_id=user_id,
                guild_id=None,
                start_date=start_date,
                end_date=end_date
            )
            
            logger.info(f"Retrieved {len(activities)} total activities for user {user_id}")
            
            if not activities:
                logger.warning("No activities available for cumulative chart")
                return None
            
            # Apply filters if provided
            filtered_activities = activities
            if media_type:
                # Handle 'manual' as a special filter for NULL media_type
                if media_type == 'manual':
                    filtered_activities = [a for a in filtered_activities if a.get('media_type') is None]
                else:
                    filtered_activities = [a for a in filtered_activities if a.get('media_type') == media_type]
                logger.info(f"After media filter ({media_type}): {len(filtered_activities)} activities")
            if activity_type:
                filtered_activities = [a for a in filtered_activities if a.get('primary_type') == activity_type]
                logger.info(f"After activity filter ({activity_type}): {len(filtered_activities)} activities")
            
            if not filtered_activities:
                logger.warning("No activities after filtering")
                return None
            
            # Move CPU-intensive matplotlib work to thread pool (prevents blocking event loop)
            loop = asyncio.get_event_loop()
            chart_bytes = await loop.run_in_executor(
                None,  # Use default ThreadPoolExecutor
                self._generate_chart_sync,
                filtered_activities,
                username,
                metric,
                start_date,
                end_date
            )
            
            if not chart_bytes:
                return None
            
            return io.BytesIO(chart_bytes)
                
        except Exception as e:
            logger.error(f"Error generating cumulative chart: {e}", exc_info=True)
            return None
    
    def _generate_chart_sync(
        self,
        activities: List[Dict],
        username: str,
        metric: str,
        start_date: datetime,
        end_date: datetime
    ) -> Optional[bytes]:
        """Synchronous chart generation (runs in thread pool)
        
        This method contains all the blocking matplotlib operations
        """
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            from collections import defaultdict
            
            # Configure Japanese font support
            self._configure_japanese_font()
            
            # Media colors matching the pie chart
            media_colors = {
                'visual_novel': '#9B59B6',  # Purple
                'manga': '#E74C3C',         # Red
                'anime': '#3498DB',         # Blue
                'book': '#2ECC71',          # Green
                'video': '#E67E22',         # Orange
                'game': '#F39C12',          # Yellow/Gold
                'manual': '#95A5A6'         # Gray (for manual logs without media type)
            }
            
            # Organize data by media type and date
            media_daily_values = defaultdict(lambda: defaultdict(float))
            
            # Create all dates in range (include days without activity for proper X-axis)
            all_dates = set()
            current_date = start_date.date()
            end_date_only = end_date.date()
            while current_date <= end_date_only:
                all_dates.add(current_date.strftime('%Y-%m-%d'))
                current_date += timedelta(days=1)
            
            # Determine unit based on metric
            if metric == "duration":
                unit_display = "Hours"
                divisor = 3600
            elif metric == "characters":
                unit_display = "Characters"
                divisor = 1
            elif metric == "pages":
                unit_display = "Pages"
                divisor = 1
            else:  # episodes
                unit_display = "Episodes"
                divisor = 1
            
            for activity in activities:
                activity_date = activity.get('date')
                if isinstance(activity_date, str):
                    date_obj = datetime.fromisoformat(activity_date)
                else:
                    date_obj = activity_date
                
                date_str = date_obj.strftime('%Y-%m-%d')
                
                media = activity.get('media_type')
                
                # Extract value based on metric
                if metric == "duration":
                    value = activity.get('duration', 0) / divisor
                else:
                    meta = activity.get('meta', {})
                    value = meta.get(metric, 0) if isinstance(meta.get(metric), (int, float)) else 0
                
                # Treat NULL media_type as 'manual' category
                media = media or 'manual'
                
                if value > 0:
                    media_daily_values[media][date_str] += value
            
            # Sort dates
            sorted_dates = sorted(all_dates)
            
            if not sorted_dates or not media_daily_values:
                return None
            
            # Calculate cumulative values for each media type
            fig, ax = plt.subplots(figsize=(10, 6), facecolor='#2C2F33')
            ax.set_facecolor('#2C2F33')
            
            lines_plotted = 0
            for media_type_key, daily_values in media_daily_values.items():
                cumulative_values = []
                total = 0
                
                for date in sorted_dates:
                    total += daily_values.get(date, 0)
                    cumulative_values.append(total)
                
                # Only plot if there's meaningful data
                if max(cumulative_values) > 0:
                    color = media_colors.get(media_type_key, '#95A5A6')
                    display_name = media_type_key.replace('_', ' ').title()
                    ax.plot(range(len(sorted_dates)), cumulative_values, 
                           color=color, linewidth=2.5, label=display_name, alpha=0.9)
                    lines_plotted += 1
            
            if lines_plotted == 0:
                plt.close(fig)
                return None
            
            # Handle possessive apostrophe
            possessive = f"{username}'" if username.endswith('s') else f"{username}'s"
            
            # Style the chart
            ax.set_xlabel('Date', color='white', fontsize=11, weight='bold')
            ax.set_ylabel(f'Cumulative {unit_display}', color='white', fontsize=11, weight='bold')
            ax.set_title(f"{possessive} Cumulative Progress", 
                        color='white', fontsize=14, weight='bold', pad=15)
            
            # Style grid
            ax.grid(True, alpha=0.2, color='#9e9e9e', linestyle='--', linewidth=0.5)
            
            # Style axes
            ax.tick_params(colors='white', labelsize=9)
            ax.spines['bottom'].set_color('#9e9e9e')
            ax.spines['left'].set_color('#9e9e9e')
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            
            # Add legend
            legend = ax.legend(loc='upper left', framealpha=0.8, facecolor='#2C2F33', 
                             edgecolor='#9e9e9e', fontsize=10)
            plt.setp(legend.get_texts(), color='white')
            
            # Set x-axis to show dates
            if len(sorted_dates) <= 7:
                tick_positions = range(len(sorted_dates))
                tick_labels = [sorted_dates[i][-5:] for i in tick_positions]
            elif len(sorted_dates) <= 30:
                step = 3
                tick_positions = range(0, len(sorted_dates), step)
                tick_labels = [sorted_dates[i][-5:] for i in tick_positions]
            else:
                step = max(1, len(sorted_dates) // 10)
                tick_positions = range(0, len(sorted_dates), step)
                tick_labels = [sorted_dates[i][-5:] for i in tick_positions]
            
            ax.set_xticks(tick_positions)
            ax.set_xticklabels(tick_labels, rotation=45, ha='right')
            
            # Save to buffer
            buffer = io.BytesIO()
            plt.tight_layout()
            plt.savefig(buffer, format='png', dpi=100, bbox_inches='tight', 
                       facecolor='#2C2F33', edgecolor='none')
            plt.close(fig)
            
            buffer.seek(0)
            return buffer.getvalue()
            
        except Exception as e:
            logger.error(f"Error in sync chart generation: {e}", exc_info=True)
            return None
            buffer.seek(0)
            plt.close(fig)
            
            logger.info(f"Successfully generated cumulative chart, buffer size: {buffer.getbuffer().nbytes} bytes")
            return buffer
            
        except Exception as e:
            logger.error(f"Error generating cumulative progress chart: {e}", exc_info=True)
            return None
    
    async def _generate_activity_heatmap(
        self,
        daily_breakdown: List[Dict[str, Any]],
        username: str,
        days: int,
        all_time_stats: Dict[str, Any],
        metric: str = "duration",
        media_type: Optional[str] = None,
        activity_type: Optional[str] = None,
        offset_months: int = 0
    ) -> Optional[io.BytesIO]:
        """Generate an Anki-style heatmap image of activity"""
        try:
            import matplotlib
            matplotlib.use('Agg')  # Use non-interactive backend
            import matplotlib.pyplot as plt
            import matplotlib.patches as patches
            import numpy as np
            
            # Configure Japanese font support
            self._configure_japanese_font()
            
            # Create a dictionary of dates to values based on metric
            date_to_values = {}
            date_to_count = {}
            for day_data in daily_breakdown:
                date_str = day_data['date']
                
                # Extract value based on metric
                # Note: 'duration' field contains the aggregated value for ALL metrics
                # (log_heatmap stores non-duration metrics in this field when building breakdown)
                if metric == "duration":
                    value = day_data['duration'] / 3600 if day_data['duration'] > 0 else 0
                else:
                    # For non-duration metrics (characters, pages, episodes), 
                    # the value is already aggregated in the 'duration' field
                    value = day_data['duration'] if day_data['duration'] > 0 else 0
                
                date_to_values[date_str] = value
                date_to_count[date_str] = day_data.get('activity_count', 0)
            
            # Calculate date range based on offset
            today = datetime.now().date()
            end_date = today - timedelta(days=offset_months * 30)
            start_date = end_date - timedelta(days=days - 1)
            
            # Extract all-time statistics from the provided stats
            all_time_daily = all_time_stats.get('daily_breakdown', [])
            
            # Calculate all-time values based on metric
            all_time_date_values = {}
            all_time_total = 0
            
            for d in all_time_daily:
                date_str = d['date']
                # Note: 'duration' field contains the aggregated value for ALL metrics
                if metric == "duration":
                    value = d['duration'] / 3600 if d['duration'] > 0 else 0
                else:
                    # For non-duration metrics, value is already aggregated in 'duration' field
                    value = d['duration'] if d['duration'] > 0 else 0
                
                all_time_total += value
                all_time_date_values[date_str] = value
            
            # Calculate all-time stats properly from first log to today
            # Find the actual date range (first log to today)
            if all_time_daily:
                dates_with_activity = [datetime.fromisoformat(d['date']).date() for d in all_time_daily]
                first_log_date = min(dates_with_activity)
                last_log_date = today
                actual_days_span = (last_log_date - first_log_date).days + 1
            else:
                actual_days_span = 1
                first_log_date = today
            
            all_time_avg = all_time_total / actual_days_span if actual_days_span > 0 else 0
            all_time_days_logged = sum(1 for d in all_time_daily if d.get('duration', 0) > 0)
            all_time_percentage = (all_time_days_logged / actual_days_span * 100) if actual_days_span > 0 else 0
            
            # Calculate all-time current streak (from today backwards)
            all_time_current_streak = 0
            check_date = today
            while check_date >= first_log_date:
                date_str = check_date.isoformat()
                if all_time_date_values.get(date_str, 0) > 0:
                    all_time_current_streak += 1
                    check_date -= timedelta(days=1)
                else:
                    break
            
            # Calculate all-time longest streak (need to check every date in range)
            all_time_longest_streak = 0
            temp_streak = 0
            check_date = first_log_date
            while check_date <= today:
                date_str = check_date.isoformat()
                if all_time_date_values.get(date_str, 0) > 0:
                    temp_streak += 1
                    all_time_longest_streak = max(all_time_longest_streak, temp_streak)
                else:
                    temp_streak = 0
                check_date += timedelta(days=1)
            
            # Determine unit label based on metric
            if metric == "duration":
                unit_label = "hours"
                value_format = "{:.1f}"
            elif metric == "characters":
                unit_label = "chars"
                value_format = "{:,.0f}"
            elif metric == "pages":
                unit_label = "pages"
                value_format = "{:,.1f}"
            else:
                unit_label = "hours"
                value_format = "{:.1f}"
            
            # Create data structure: 7 days (rows) x weeks (columns)
            weeks = []
            current_week = []
            current_date = start_date
            
            # Pad to start on Monday
            weekday = current_date.weekday()  # Monday = 0, Sunday = 6
            for _ in range(weekday):
                current_week.append(None)
            
            # Fill in the dates
            while current_date <= end_date:
                date_str = current_date.isoformat()
                value = date_to_values.get(date_str, 0)
                current_week.append(value)
                
                # Once a week (7 days) is complete, start a new one
                if len(current_week) == 7:
                    weeks.append(current_week)
                    current_week = []
                
                current_date += timedelta(days=1)
            
            # Pad the last week if necessary
            while len(current_week) > 0 and len(current_week) < 7:
                current_week.append(None)
            
            if current_week:
                weeks.append(current_week)
            
            # Transpose to get days x weeks for proper display
            if not weeks:
                return None
            
            max_week_len = max(len(week) for week in weeks)
            for week in weeks:
                while len(week) < max_week_len:
                    week.append(None)
            
            # Create numpy array (7 rows for days, N columns for weeks)
            data = np.array(weeks).T
            
            # Setup the plot with Anki-style proportions
            num_weeks = len(weeks)
            fig_width = max(14, num_weeks * 0.16)
            fig_height = 2.8
            fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=120)
            
            # Define color scheme (Anki-style greens)
            colors = ['#ebedf0', '#c6e48b', '#7bc96f', '#239a3b', '#196127']
            
            # Calculate thresholds for color mapping
            max_value = max([v for v in date_to_values.values()] + [0])
            if max_value == 0:
                max_value = 1
            
            # Draw the heatmap with smaller, tighter cells
            cell_size = 0.85
            cell_spacing = 0.15
            
            for week_idx in range(num_weeks):
                for day_idx in range(7):
                    value = data[day_idx, week_idx]
                    
                    if value is None:
                        continue  # Skip empty cells
                    elif value == 0:
                        color = colors[0]
                    else:
                        # Map value to color index
                        intensity = min(value / max_value, 1.0)
                        if intensity < 0.2:
                            color_idx = 1
                        elif intensity < 0.4:
                            color_idx = 2
                        elif intensity < 0.7:
                            color_idx = 3
                        else:
                            color_idx = 4
                        color = colors[color_idx]
                    
                    rect = patches.Rectangle(
                        (week_idx * (cell_size + cell_spacing), (6 - day_idx) * (cell_size + cell_spacing)),
                        cell_size, cell_size,
                        linewidth=0,
                        edgecolor='none',
                        facecolor=color
                    )
                    ax.add_patch(rect)
            
            # Add day labels (M, T, W, T, F, S, S) on the left
            day_labels = ['M', 'T', 'W', 'T', 'F', 'S', 'S']
            label_x = -0.8
            for i, label in enumerate(day_labels):
                y_pos = (6 - i) * (cell_size + cell_spacing) + cell_size / 2
                ax.text(label_x, y_pos, label, ha='center', va='center', 
                       fontsize=9, color='#767676', fontweight='normal')
            
            # Add month labels at the top
            current_date = start_date
            month_positions = {}
            month_years = {}
            
            for week_idx in range(num_weeks):
                month_year = current_date.strftime('%b \'%y')
                if month_year not in month_positions:
                    month_positions[month_year] = week_idx * (cell_size + cell_spacing)
                current_date += timedelta(days=7)
            
            label_y = 7 * (cell_size + cell_spacing) + 0.3
            for month_label, x_pos in month_positions.items():
                ax.text(x_pos, label_y, month_label, ha='left', va='bottom', 
                       fontsize=10, color='#767676')
            
            # Set axis limits (increased top limit for more padding above title)
            ax.set_xlim(-1.5, num_weeks * (cell_size + cell_spacing))
            ax.set_ylim(-3.3, 7 * (cell_size + cell_spacing) + 2.2)
            ax.axis('off')
            
            # Add title at the top with proper possessive handling
            filter_text = ""
            if media_type or activity_type:
                parts = []
                if media_type:
                    parts.append(media_type.replace('_', ' ').title())
                if activity_type:
                    parts.append(activity_type.title())
                filter_text = f" - {' • '.join(parts)}"
            
            # Possessive: a bare apostrophe for names ending in 's', otherwise "'s"
            if username.endswith('s'):
                possessive = f"{username}'"
            else:
                possessive = f"{username}'s"
            
            # Increased title_y for more spacing from month labels
            title_y = 7 * (cell_size + cell_spacing) + 1.9
            ax.text(num_weeks * (cell_size + cell_spacing) / 2, title_y, 
                   f"{possessive} Immersion Activity{filter_text}", 
                   ha='center', va='bottom', fontsize=16, fontweight='bold', color='#1a1a1a')
            
            # Add statistics at the bottom (Anki-style) with more spacing - ALL TIME VALUES
            stats_y = -1.6
            stats_spacing = num_weeks * (cell_size + cell_spacing) / 4
            
            # Daily average (all-time)
            ax.text(0, stats_y, 'Daily average:', ha='left', va='top', 
                   fontsize=11, color='#1a1a1a')
            avg_display = value_format.format(all_time_avg) + f' {unit_label}'
            ax.text(0, stats_y - 1.0, avg_display, ha='left', va='top', 
                   fontsize=14, color='#56d364', fontweight='bold')
            
            # Days logged percentage (all-time)
            ax.text(stats_spacing, stats_y, 'Days logged:', ha='left', va='top', 
                   fontsize=11, color='#1a1a1a')
            ax.text(stats_spacing, stats_y - 1.0, f'{all_time_percentage:.0f}%', ha='left', va='top', 
                   fontsize=14, color='#56d364', fontweight='bold')
            
            # Longest streak (all-time)
            ax.text(stats_spacing * 2, stats_y, 'Longest streak:', ha='left', va='top', 
                   fontsize=11, color='#1a1a1a')
            ax.text(stats_spacing * 2, stats_y - 1.0, 
                   f'{all_time_longest_streak} day{"s" if all_time_longest_streak != 1 else ""}', 
                   ha='left', va='top', fontsize=14, color='#56d364', fontweight='bold')
            
            # Current streak (all-time)
            ax.text(stats_spacing * 3, stats_y, 'Current streak:', ha='left', va='top', 
                   fontsize=11, color='#1a1a1a')
            ax.text(stats_spacing * 3, stats_y - 1.0, 
                   f'{all_time_current_streak} day{"s" if all_time_current_streak != 1 else ""}', 
                   ha='left', va='top', fontsize=14, color='#56d364', fontweight='bold')
            
            plt.tight_layout()
            
            # Save to buffer
            buffer = io.BytesIO()
            plt.savefig(buffer, format='png', bbox_inches='tight', 
                       facecolor='white', edgecolor='none', pad_inches=0.3)
            buffer.seek(0)
            plt.close(fig)
            
            return buffer
            
        except ImportError:
            logger.error("matplotlib is not installed. Cannot generate heatmap.")
            return None
        except Exception as e:
            logger.error(f"Error generating heatmap: {e}")
            return None
    
    @app_commands.command(name="log_config", description="Configure your immersion tracking settings")
    @app_commands.describe(
        setting="Setting to configure",
        value="New value for the setting"
    )
    @app_commands.choices(setting=[
        app_commands.Choice(name="Timezone", value="timezone"),
        app_commands.Choice(name="Primary Server", value="primary_server")
    ])
    async def log_config(self, interaction: discord.Interaction, setting: str, value: str = None):
        """Configure user settings"""
        try:
            await interaction.response.defer()
            
            user_id = str(interaction.user.id)
            success = False
            
            if setting == "timezone":
                tz_value = (value or "").strip()
                try:
                    import zoneinfo
                    zoneinfo.ZoneInfo(tz_value)
                except zoneinfo.ZoneInfoNotFoundError as e:
                    logger.warning(f"Timezone not found in tzdata: {tz_value!r} ({e})")
                    await interaction.followup.send(
                        f"Timezone database is missing on the server (couldn't find '{tz_value}'). "
                        f"Please ask the bot operator to install the `tzdata` package.",
                        ephemeral=True,
                    )
                    return
                except Exception as e:
                    logger.warning(f"Timezone validation failed for {tz_value!r}: {type(e).__name__}: {e}")
                    await interaction.followup.send(
                        f"Invalid timezone '{tz_value}'. Please use a valid IANA timezone "
                        f"(e.g., 'Asia/Tokyo', 'Europe/London', 'America/New_York').",
                        ephemeral=True,
                    )
                    return
                success = await self.user_repo.update_user_settings(user_id, timezone=tz_value)
                setting_name = "Timezone"
                display_value = tz_value
            elif setting == "primary_server":
                setting_name = "Primary Server"
                
                if not value:
                    await interaction.followup.send("Please select a server from the dropdown menu.", ephemeral=True)
                    return
                
                normalized_value = value.strip()

                if normalized_value.lower() in {"clear", "none", "reset", "remove"}:
                    success = await self.user_repo.update_user_settings(user_id, primary_guild_id=None)
                    display_value = "Auto-detected"
                else:
                    # Check if it's a known guild from LEADERBOARD_GUILD_FILTERS
                    if normalized_value in LEADERBOARD_GUILD_FILTERS:
                        guild_id = int(normalized_value)
                        display_value = LEADERBOARD_GUILD_FILTERS[normalized_value]
                    else:
                        # Fallback for manual input (ID, mention, or 'current')
                        guild_id = None
                        if normalized_value.lower() == "current" and interaction.guild_id:
                            guild_id = interaction.guild_id
                        else:
                            digits = ''.join(ch for ch in normalized_value if ch.isdigit())
                            if digits:
                                try:
                                    guild_id = int(digits)
                                except ValueError:
                                    guild_id = None

                        if not guild_id:
                            await interaction.followup.send(
                                "Please select a server from the dropdown menu or provide a valid server ID.",
                                ephemeral=True
                            )
                            return

                        # Check if guild exists in the database (for cross-bot support)
                        guild_info = await self.db.get_guild_info(str(guild_id))
                        guild_obj = self.bot.get_guild(guild_id)
                        
                        if not guild_obj and not guild_info:
                            await interaction.followup.send(
                                "I couldn't find that server. Make sure the server ID is correct and that at least one bot instance has access to it.",
                                ephemeral=True
                            )
                            return
                        
                        # Use guild name from live bot or cached info
                        if guild_obj:
                            display_value = guild_obj.name
                        elif guild_info and guild_info.get('guild_name'):
                            display_value = guild_info['guild_name']
                        else:
                            display_value = f"Server #{str(guild_id)[-4:]}"

                    success = await self.user_repo.update_user_settings(user_id, primary_guild_id=str(guild_id))
            
            if success:
                embed = discord.Embed(
                    title="Setting Updated",
                    description=f"{setting_name} has been set to {display_value}",
                    color=discord.Color.green()
                )
            else:
                embed = discord.Embed(
                    title="Update Failed",
                    description="Failed to update setting. Please try again.",
                    color=discord.Color.red()
                )
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in log_config command: {e}")
            await interaction.followup.send("An error occurred while updating the setting.", ephemeral=True)

    @log_config.autocomplete('value')
    async def log_config_value_autocomplete(self, interaction: discord.Interaction, current: str):
        """Provide autocomplete for value based on the selected setting"""
        query = current.lower() if current else ""
        
        # Define timezone choices
        timezone_choices = [
            app_commands.Choice(name="America/New_York (Eastern)", value="America/New_York"),
            app_commands.Choice(name="America/Chicago (Central)", value="America/Chicago"),
            app_commands.Choice(name="America/Denver (Mountain)", value="America/Denver"),
            app_commands.Choice(name="America/Los_Angeles (Pacific)", value="America/Los_Angeles"),
            app_commands.Choice(name="Europe/London", value="Europe/London"),
            app_commands.Choice(name="Europe/Paris", value="Europe/Paris"),
            app_commands.Choice(name="Asia/Tokyo", value="Asia/Tokyo"),
            app_commands.Choice(name="Asia/Seoul", value="Asia/Seoul"),
            app_commands.Choice(name="Australia/Sydney", value="Australia/Sydney"),
        ]
        
        # Define server choices
        server_choices = [
            app_commands.Choice(name="Clear (Auto-detect)", value="clear"),
            *[app_commands.Choice(name=label, value=guild_id) 
              for guild_id, label in LEADERBOARD_GUILD_FILTERS.items()]
        ]
        
        # Determine what type of value is being requested based on input patterns
        is_timezone_query = any([
            "/" in query,  # Common in timezone names like America/New_York
            any(tz_part in query for tz_part in ["america", "europe", "asia", "utc", "gmt", "eastern", "pacific", "central", "mountain"]),
            query in ["", "time"]  # Empty or generic time query
        ])
        
        is_server_query = any([
            query in ["clear", "auto", "none", "reset"],
            any(server_part in query for server_part in ["server", "guild", "vncr", "ljta", "visual", "anime"]),
        ])
        
        choices = []
        
        if is_timezone_query and not is_server_query:
            # Prioritize timezone choices
            for choice in timezone_choices:
                if not query or query in choice.name.lower() or query in choice.value.lower():
                    choices.append(choice)
        elif is_server_query and not is_timezone_query:
            # Prioritize server choices
            for choice in server_choices:
                if not query or query in choice.name.lower() or query in choice.value.lower():
                    choices.append(choice)
        else:
            # Mixed or unclear query - show both but make it clear what each is for
            if not query:
                # No input - show a helpful mix
                choices.extend([
                    app_commands.Choice(name="🌍 Timezone: Asia/Tokyo", value="Asia/Tokyo"),
                    app_commands.Choice(name="🌍 Timezone: Europe/London", value="Europe/London"),
                    app_commands.Choice(name="🌍 Timezone: America/New_York (Eastern)", value="America/New_York"),
                    app_commands.Choice(name="🏠 Server: Clear (Auto-detect)", value="clear"),
                    *[app_commands.Choice(name=f"🏠 Server: {label}", value=guild_id) 
                      for guild_id, label in list(LEADERBOARD_GUILD_FILTERS.items())[:3]]
                ])
            else:
                # Filter both types based on query
                for choice in timezone_choices:
                    if query in choice.name.lower() or query in choice.value.lower():
                        choices.append(app_commands.Choice(name=f"🌍 {choice.name}", value=choice.value))
                for choice in server_choices:
                    if query in choice.name.lower() or query in choice.value.lower():
                        choices.append(app_commands.Choice(name=f"🏠 {choice.name}", value=choice.value))
        
        return choices[:25]
    
    @app_commands.command(name="log_goal", description="Manage your immersion goals")
    @app_commands.describe(
        action="Action to perform",
        name="Name of the goal",
        target="Target time in minutes",
        period="Goal period (daily/weekly/monthly)",
        activity_type="Limit to activity type (optional)",
        media_type="Limit to media type (optional)"
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Create", value="create"),
            app_commands.Choice(name="List", value="list"),
            app_commands.Choice(name="Delete", value="delete")
        ],
        period=[
            app_commands.Choice(name="Daily", value="daily"),
            app_commands.Choice(name="Weekly", value="weekly"),
            app_commands.Choice(name="Monthly", value="monthly")
        ],
        activity_type=[
            app_commands.Choice(name="Reading", value="reading"),
            app_commands.Choice(name="Listening", value="listening")
        ],
        media_type=[
            app_commands.Choice(name="Book", value="book"),
            app_commands.Choice(name="Anime", value="anime"),
            app_commands.Choice(name="Manga", value="manga"),
            app_commands.Choice(name="Video", value="video"),
            app_commands.Choice(name="Visual Novel", value="visual_novel")
        ]
    )
    async def log_goal(self, interaction: discord.Interaction, action: str, 
                  name: str = None, target: int = None, period: str = None,
                  activity_type: str = None, media_type: str = None):
        """Manage immersion goals"""
        try:
            await interaction.response.defer()
            
            user_id = str(interaction.user.id)
            
            if action == "create":
                if not name or not target or not period:
                    await interaction.followup.send("Name, target, and period are required to create a goal.", ephemeral=True)
                    return
                
                if target <= 0:
                    await interaction.followup.send("Target must be a positive number.", ephemeral=True)
                    return
                
                if media_type and activity_type:
                    expected_activity = MEDIA_ACTIVITY_COMPATIBILITY.get(media_type)
                    if expected_activity and expected_activity != activity_type:
                        media_label = media_type.replace('_', ' ').title()
                        expected_label = expected_activity.capitalize()
                        await interaction.followup.send(
                            f"The `{media_label}` media type is tracked as `{expected_label}` activity. "
                            "Please choose the matching activity type or omit one of the filters.",
                            ephemeral=True
                        )
                        return

                # Create goal based on period
                if period == "daily":
                    goal_id = await self.goal_service.create_daily_goal(
                        user_id=user_id,
                        name=name,
                        target_minutes=target,
                        activity_type=activity_type,
                        media_type=media_type
                    )
                elif period == "weekly":
                    goal_id = await self.goal_service.create_weekly_goal(
                        user_id=user_id,
                        name=name,
                        target_minutes=target,
                        activity_type=activity_type,
                        media_type=media_type
                    )
                elif period == "monthly":
                    goal_id = await self.goal_service.create_monthly_goal(
                        user_id=user_id,
                        name=name,
                        target_minutes=target,
                        activity_type=activity_type,
                        media_type=media_type
                    )
                
                if goal_id:
                    embed = discord.Embed(
                        title="Goal Created",
                        description=f"Created {period} goal: {name} ({target} minutes)",
                        color=discord.Color.green()
                    )
                else:
                    embed = discord.Embed(
                        title="Goal Creation Failed",
                        description="Failed to create goal. Please try again.",
                        color=discord.Color.red()
                    )
                
                await interaction.followup.send(embed=embed)
            
            elif action == "list":
                goals_data = await self.goal_service.get_user_goals_with_progress(user_id)
                
                if not goals_data:
                    embed = discord.Embed(
                        title="No Goals",
                        description="You don't have any active goals. Use `/log_goal create` to create one!",
                        color=discord.Color.blue()
                    )
                else:
                    embed = discord.Embed(
                        title="Your Goals",
                        color=discord.Color.blue()
                    )
                    
                    for goal_data in goals_data[:10]:  # Limit to 10 goals
                        goal = goal_data['goal']
                        progress = goal_data['progress_percentage']
                        status = "✅ Completed" if goal_data['is_completed'] else f"{progress:.1f}% complete"
                        detail_lines = self._format_goal_filters(goal)
                        detail_lines.append(
                            f"Progress: {goal_data['current_str']} / {goal_data['target_str']} ({status})"
                        )

                        embed.add_field(
                            name=goal.name,
                            value="\n".join(detail_lines),
                            inline=False
                        )
                
                await interaction.followup.send(embed=embed)
            
            elif action == "delete":
                if not name:
                    await interaction.followup.send("Please provide the goal name to delete (case sensitive).", ephemeral=True)
                    return

                goals = await self.goal_service.find_user_goals_by_name(user_id, name)

                if not goals:
                    await interaction.followup.send(f"No active goal named `{name}` was found.", ephemeral=True)
                    return

                if len(goals) > 1:
                    goal_list = "\n".join([f"• {goal.name} (Created {goal.created_at.strftime('%Y-%m-%d') if goal.created_at else 'unknown'})" for goal in goals])
                    await interaction.followup.send(
                        "Multiple goals share that name. Please delete by ID using `/log_goal action:delete name:<exact name> input_id:<id>` once supported.\n"
                        "Currently matching goals:\n" + goal_list,
                        ephemeral=True
                    )
                    return

                goal = goals[0]
                success = await self.goal_service.delete_goal(goal.id, user_id)

                if success:
                    embed = discord.Embed(
                        title="Goal Deleted",
                        description=f"Removed goal `{goal.name}`.",
                        color=discord.Color.green()
                    )
                else:
                    embed = discord.Embed(
                        title="Deletion Failed",
                        description=f"Couldn't remove goal `{goal.name}`. Please try again.",
                        color=discord.Color.red()
                    )

                await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in log_goal command: {e}")
            await interaction.followup.send("An error occurred while managing goals.", ephemeral=True)
    
    def _parse_duration(self, duration_str: str) -> int:
        """Parse duration string to seconds"""
        # Handle simple numeric input (assume minutes)
        if duration_str.isdigit():
            return int(duration_str) * 60
        
        # Parse duration with units
        total_seconds = 0
        current_number = ""
        
        for char in duration_str.lower():
            if char.isdigit() or char == '.':
                current_number += char
            elif char in 'hms':
                if current_number:
                    try:
                        value = float(current_number)
                        if char == 'h':
                            total_seconds += int(value * 3600)
                        elif char == 'm':
                            total_seconds += int(value * 60)
                        elif char == 's':
                            total_seconds += int(value)
                        current_number = ""
                    except ValueError:
                        raise ValueError(
                            f"Invalid duration format: '{duration_str}'\n"
                            f"Valid formats: '30m', '1h30m', '1.5h', '90' (minutes)"
                        )
        
        # Handle leftover number without unit
        if current_number:
            raise ValueError(
                f"Invalid duration format: '{duration_str}'\n"
                f"Missing time unit. Valid formats: '30m', '1h30m', '1.5h', or '90' (minutes)"
            )
        
        if total_seconds == 0:
            raise ValueError(
                f"Invalid duration format: '{duration_str}'\n"
                f"Valid formats: '30m', '1h30m', '1.5h', or '90' (minutes)"
            )
        
        return total_seconds
    
    async def _validate_log_date(self, activity_date: datetime, user_tz) -> Optional[str]:
        """
        Validate the log date against the allowed window in calendar days
        (user's timezone). Allowed range: 7 days in the past through 1 day
        in the future, inclusive. Calendar-day comparison means "tomorrow"
        and "exactly 7 days ago" always pass regardless of clock time.
        Returns an error message if invalid, None if valid.
        """
        today = datetime.now(timezone.utc).astimezone(user_tz).date()
        activity_day = activity_date.astimezone(user_tz).date()
        days_diff = (today - activity_day).days  # positive = past, negative = future

        if days_diff > 7:
            return "❌ Cannot log activities more than 1 week in the past. Please keep your logs current."
        if days_diff < -1:
            return "❌ Cannot log activities more than 1 day in the future."
        return None
    
    async def _get_total_duration_for_day(self, user_id: str, activity_date: datetime) -> int:
        """
        Get the total duration (in seconds) of all activities for a specific day.
        Uses the start of day (00:00:00) and end of day (23:59:59) for the query.
        """
        # Get start and end of the day in UTC
        day_start = activity_date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = activity_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        # Query all activities for this day
        activities = await self.activity_repo.get_activities_in_range(
            user_id=user_id,
            start_date=day_start,
            end_date=day_end
        )
        
        # Sum up all durations
        total_duration = sum(activity.get('duration', 0) for activity in activities)
        return total_duration
    
    async def _validate_daily_duration_limit(
        self, user_id: str, activity_date: datetime, new_duration: int
    ) -> Optional[str]:
        """
        Validate that adding this log won't exceed 24 hours for the day.
        Returns error message if invalid, None if valid.
        """
        # Get existing duration for the day
        existing_duration = await self._get_total_duration_for_day(user_id, activity_date)
        
        # Calculate what the total would be with the new log
        total_duration = existing_duration + new_duration
        
        # 24 hours in seconds
        max_duration = 24 * 3600
        
        if total_duration > max_duration:
            existing_hours = existing_duration / 3600
            new_hours = new_duration / 3600
            total_hours = total_duration / 3600
            
            return (
                f"❌ Daily limit exceeded!\n"
                f"• Existing logs for this day: {existing_hours:.1f}h\n"
                f"• This log: {new_hours:.1f}h\n"
                f"• Total would be: {total_hours:.1f}h\n"
                f"• Maximum allowed: 24h per day"
            )
        
        return None
    
    async def _notify_goal_completions(self, interaction: discord.Interaction, goals: List[Goal]):
        """Send goal completion notifications"""
        try:
            if not goals:
                return

            embed = self._build_goal_completion_embed(goals)
            await interaction.followup.send(embed=embed, ephemeral=False)

        except Exception as e:
            logger.error(f"Error sending goal completions: {e}")

    async def _sync_roles_and_notify(
        self,
        interaction: discord.Interaction,
        source: str,
        actor: Optional[Union[discord.Member, discord.User]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Run class role sync and, if the actor's class changed, announce it."""
        if not interaction or not interaction.guild:
            return None

        stats: Optional[Dict[str, Any]] = None
        try:
            stats = await self.sync_reward_roles_for_guild(interaction.guild, source=source)
        except Exception as sync_error:
            logger.warning("Failed to sync roles after %s: %s", source, sync_error)
            return None

        if not stats:
            return None

        class_changes = stats.get("class_changes") if isinstance(stats, dict) else None
        if class_changes:
            await self._maybe_send_class_change_notification(interaction, class_changes, source, actor)

        return stats

    async def _sync_roles_for_member(
        self,
        member: discord.Member,
        channel: discord.abc.Messageable,
        source: str,
    ) -> Optional[Dict[str, Any]]:
        """Non-interaction variant of role sync, for paths like the kizuna
        webhook listener where there is no Discord interaction object. If the
        sync results in a class change for `member`, a class-change embed is
        posted to `channel`, and (for promotions) the existing welcome-channel
        announcement also fires.
        """
        guild = getattr(member, "guild", None)
        if guild is None:
            return None

        try:
            stats = await self.sync_reward_roles_for_guild(guild, source=source)
        except Exception as sync_error:
            logger.warning("Failed to sync roles after %s: %s", source, sync_error)
            return None

        if not stats:
            return None

        class_changes = stats.get("class_changes") if isinstance(stats, dict) else None
        if not class_changes:
            return stats

        matching_change = next(
            (change for change in class_changes if change.get("member_id") == member.id),
            None,
        )
        if not matching_change:
            return stats

        direction = self._get_class_change_direction(matching_change)
        embed = self._build_class_change_embed(member, matching_change, source, direction=direction)
        if embed is not None:
            try:
                await channel.send(embed=embed)
            except Exception as exc:
                logger.warning("Failed to send class change notification: %s", exc)

        if direction == "promotion":
            # Reuse the existing announcement helper. It only reads
            # interaction.guild, so a lightweight stand-in is enough.
            class _PseudoInteraction:
                def __init__(self, guild):
                    self.guild = guild

            try:
                await self._announce_class_promotion(_PseudoInteraction(guild), member, matching_change)
            except Exception as exc:
                logger.warning("Failed to announce class promotion: %s", exc)

        return stats

    async def _maybe_send_class_change_notification(
        self,
        interaction: discord.Interaction,
        class_changes: List[Dict[str, Any]],
        source: str,
        actor: Optional[Union[discord.Member, discord.User]] = None,
    ) -> None:
        if not interaction or not class_changes:
            return

        target = actor or interaction.user
        target_id = getattr(target, "id", None)
        if target_id is None:
            return

        matching_change = next(
            (change for change in class_changes if change.get("member_id") == target_id),
            None,
        )

        if not matching_change:
            return

        direction = self._get_class_change_direction(matching_change)
        embed = self._build_class_change_embed(target, matching_change, source, direction=direction)
        if embed is None:
            return

        try:
            await interaction.followup.send(embed=embed, ephemeral=False)
        except Exception as exc:
            logger.warning("Failed to send class change notification: %s", exc)

        if direction == "promotion":
            await self._announce_class_promotion(interaction, target, matching_change)

    def _get_class_change_direction(self, change: Dict[str, Any]) -> str:
        previous_index = change.get("previous_index")
        new_index = change.get("new_index")

        if new_index is not None and (previous_index is None or new_index > previous_index):
            return "promotion"
        if new_index is None and previous_index is not None:
            return "removal"
        if (
            previous_index is not None
            and new_index is not None
            and new_index < previous_index
        ):
            return "demotion"
        return "change"

    def _build_class_change_embed(
        self,
        actor: Union[discord.Member, discord.User],
        change: Dict[str, Any],
        source: str,
        direction: Optional[str] = None,
    ) -> Optional[discord.Embed]:
        previous_name = change.get("previous_role_name") or "Unranked"
        new_name = change.get("new_role_name") or "Unranked"
        total_seconds = change.get("total_seconds") or 0
        total_hours = max(total_seconds / 3600, 0)

        if direction is None:
            direction = self._get_class_change_direction(change)

        trigger_map = {
            "activity_log": "after your latest log.",
            "activity_undo": "after undoing a log.",
            "manual": "after a manual sync.",
        }
        trigger_phrase = trigger_map.get(source, "after your recent activity.")

        if direction == "promotion":
            title = "🎉 Class Promotion!"
            description = (
                f"Congrats {actor.mention}! You advanced from **{previous_name}** to **{new_name}** "
                f"{trigger_phrase} Keep it up!"
            )
            color = discord.Color.brand_green()
        elif direction in {"demotion", "removal"}:
            title = "Class Rank Update"
            encouragement = "Steady immersion will have you climbing again!"
            description = (
                f"Heads up {actor.mention}: your class changed from **{previous_name}** to "
                f"**{new_name}** {trigger_phrase} {encouragement}"
            )
            color = discord.Color.orange()
        else:
            title = "Class Rank Update"
            description = (
                f"{actor.mention}, your class role is now **{new_name}** {trigger_phrase}"
            )
            color = discord.Color.blurple()

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="New Class", value=new_name, inline=True)
        embed.add_field(name="VN Time Logged", value=_format_duration_value(total_seconds), inline=True)

        if direction == "promotion" and previous_name != "Unranked":
            embed.add_field(name="Previous Class", value=previous_name, inline=True)
        elif direction in {"demotion", "removal"} and previous_name:
            embed.add_field(name="Previous Class", value=previous_name, inline=True)

        return embed

    async def _announce_class_promotion(
        self,
        interaction: discord.Interaction,
        actor: Union[discord.Member, discord.User],
        change: Dict[str, Any],
    ) -> None:
        guild = getattr(interaction, "guild", None)
        if guild is None:
            return

        bot_config = getattr(self.bot, "config", {}) if hasattr(self, "bot") else {}
        guild_config = (bot_config.get("guilds") or {}).get(str(guild.id), {})
        channel_id = guild_config.get("welcome_channel")
        if not channel_id:
            return

        get_channel = getattr(guild, "get_channel", None)
        if not callable(get_channel):
            return

        channel = get_channel(int(channel_id))
        if channel is None:
            return

        new_name = change.get("new_role_name") or "their new class"
        
        # Find the minimum hours for this class rank
        min_hours = None
        for class_def in self._default_class_role_definitions:
            if class_def.get("name") == new_name:
                min_hours = class_def.get("min_hours")
                break
        
        # Format the message with hours requirement
        if min_hours is not None and min_hours > 0:
            if min_hours >= 1:
                hours_text = f"{min_hours:,.0f}h" if min_hours == int(min_hours) else f"{min_hours:.1f}h"
            else:
                # Handle fractional hours (like the 鬼 class with 1/3600 hours)
                hours_text = "first log"
            message = f"⚔️ **Class Promotion** · {actor.mention} advanced to **{new_name}** ({hours_text})!"
        else:
            message = f"⚔️ **Class Promotion** · {actor.mention} advanced to **{new_name}**!"

        try:
            await channel.send(message)
        except Exception as exc:
            logger.warning(
                "Failed to announce class promotion in guild %s: %s",
                getattr(guild, "id", "unknown"),
                exc,
            )

    def _build_goal_completion_embed(self, goals: List[Goal]) -> discord.Embed:
        embed = discord.Embed(
            title="🎉 Goals Completed!",
            description="Congratulations! You've completed the following goals:",
            color=discord.Color.gold()
        )

        for goal in goals[:5]:  # Limit to 5 goals
            detail_lines = self._format_goal_filters(goal)
            target_text = self._format_duration(goal.target)
            progress_text = self._format_duration(goal.current_value)
            detail_lines.append(f"Target: {target_text}")
            detail_lines.append(f"Progress: {progress_text}")

            embed.add_field(
                name=goal.name,
                value="\n".join(detail_lines),
                inline=False
            )

        if len(goals) > 5:
            extra_count = len(goals) - 5
            embed.add_field(
                name="...",
                value=f"And {extra_count} more!",
                inline=False
            )

        return embed
    
    def _format_duration(self, seconds: int) -> str:
        """Format duration in seconds to human readable string"""
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            minutes = seconds // 60
            return f"{minutes}m"
        else:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            if minutes == 0:
                return f"{hours}h"
            return f"{hours}h {minutes}m"

    def _goal_period_label(self, goal: Goal) -> str:
        """Return a human-readable label for a goal's recurrence period."""
        cron_map = {
            "0 0 * * *": "Daily",
            "0 0 * * 0": "Weekly",
            "0 0 1 * *": "Monthly",
        }
        if goal.cron:
            return cron_map.get(goal.cron, "Custom")
        return "Custom"

    def _format_goal_filters(self, goal: Goal) -> List[str]:
        """Create display lines for a goal's filters and recurrence."""
        period_label = self._goal_period_label(goal)
        activity_label = goal.activity_type.title() if goal.activity_type else "Any Activity"
        media_label = goal.media_type.replace('_', ' ').title() if goal.media_type else "Any Media"

        lines = [
            f"Period: {period_label}",
            f"Activity: {activity_label}",
            f"Media: {media_label}"
        ]

        if goal.youtube_channels:
            lines.append(f"Channels: {', '.join(goal.youtube_channels)}")

        return lines

    @app_commands.command(name="log_export", description="Export your logs to a file")
    @app_commands.describe(
        format="Export format (default: jsonl for backup/import, text for human-readable)"
    )
    @app_commands.choices(format=[
        app_commands.Choice(name="JSONL (machine-readable, for backup/import)", value="jsonl"),
        app_commands.Choice(name="Text (human-readable summary)", value="text")
    ])
    async def log_export(self, interaction: discord.Interaction, format: str = "jsonl"):
        """Export user's logs to a file"""
        try:
            await interaction.response.defer()

            user_id = str(interaction.user.id)

            # Get all user activities (no guild filtering)
            activities = await self.activity_repo.export_user_activities(user_id, None)

            if not activities:
                await interaction.followup.send("You have no logs to export.", ephemeral=True)
                return

            try:
                from utils.data_transfer import export_manager

                # Check rate limiting first
                time_until_next = export_manager.get_time_until_next_export(user_id)
                if time_until_next:
                    hours = int(time_until_next.total_seconds() // 3600)
                    minutes = int((time_until_next.total_seconds() % 3600) // 60)
                    await interaction.followup.send(
                        f"Export rate limited. You can export again in {hours}h {minutes}m.",
                        ephemeral=True
                    )
                    return

                # Export activities based on format
                if format == "text":
                    export_file = await export_manager.export_activities_text(
                        activities=activities,
                        user_id=user_id
                    )
                    filename = export_manager.get_export_filename(user_id, format="text")
                    format_display = "Text (.txt)"
                    footer_text = "Human-readable summary of your immersion logs"
                else:
                    export_file = await export_manager.export_activities_json(
                        activities=activities,
                        user_id=user_id,
                        compressed=True
                    )
                    filename = export_manager.get_export_filename(user_id, compressed=True, format="jsonl")
                    format_display = "Compressed JSONL (.jsonl.gz)"
                    footer_text = "Compressed JSONL backup of your immersion logs"

                # Send file
                export_file.seek(0)
                file = discord.File(export_file, filename=filename)

                embed = discord.Embed(
                    title="Activities Exported",
                    description=f"Successfully exported **{len(activities)}** activities.",
                    color=discord.Color.green()
                )
                embed.add_field(
                    name="Format",
                    value=format_display,
                    inline=True
                )
                embed.add_field(
                    name="Next Export",
                    value="Available in 10 minutes",
                    inline=True
                )
                embed.set_footer(text=footer_text)

                await interaction.followup.send(embed=embed, file=file)

            except ImportError:
                await interaction.followup.send("Export functionality not available. Please contact an administrator.", ephemeral=True)
            except ValueError as e:
                await interaction.followup.send(f"Export failed: {e}", ephemeral=True)
            except Exception as export_error:
                logger.error(f"Export error: {export_error}")
                await interaction.followup.send("Failed to export activities. Please try again later.", ephemeral=True)

        except Exception as e:
            logger.error(f"Error in log_export command: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("An error occurred while exporting activities.", ephemeral=True)
            else:
                await interaction.followup.send("An error occurred while exporting activities.", ephemeral=True)

    # Disabled: the command lets any user add arbitrary activity rows in bulk.
    # Uncomment the three decorators below to re-enable.
    # @app_commands.command(name="log_import", description="Import logs from a file or manage imports")
    # @app_commands.describe(
    #     action="Action to perform",
    #     file="JSONL file to import (for import action)"
    # )
    # @app_commands.choices(
    #     action=[
    #         app_commands.Choice(name="Import from file", value="import"),
    #         app_commands.Choice(name="List import history", value="list"),
    #         app_commands.Choice(name="Undo import", value="undo"),
    #         app_commands.Choice(name="Clean up history", value="cleanup")
    #     ]
    # )
    async def log_import(self, interaction: discord.Interaction,
                        action: app_commands.Choice[str],
                        file: Optional[discord.Attachment] = None,
                        timestamp: Optional[str] = None):
        """Import activities or manage import history"""
        try:
            await interaction.response.defer()
            
            user_id = str(interaction.user.id)
            
            if action.value == "list":
                # List import history
                history = await self.activity_repo.get_import_history(user_id)
                
                if not history:
                    await interaction.followup.send("No import history found.", ephemeral=True)
                    return
                
                embed = discord.Embed(
                    title="Import History",
                    description="✅ = Active imports, ❌ = Undone imports\n"
                               "Use `/log_import action:undo timestamp:<id>` to undo an import.\n"
                               "Use `/log_import action:cleanup` to remove undone import records.",
                    color=discord.Color.blue()
                )
                
                for import_record in history:
                    try:
                        import_time = datetime.fromisoformat(import_record['import_timestamp'])
                        timestamp_id = str(int(import_time.timestamp() * 1000000000))  # nanoseconds like botsu
                        
                        status = import_record.get('status', 'Unknown')
                        status_emoji = "✅" if status == 'Active' else "❌"
                        active_count = import_record.get('active_activities', 0)
                        
                        if status == 'Active':
                            description = f"**{status_emoji} Active** - {active_count} activities <t:{int(import_time.timestamp())}:R>"
                        else:
                            description = f"**{status_emoji} Undone** - Originally {import_record['activity_count']} activities <t:{int(import_time.timestamp())}:R>"
                        
                        embed.add_field(
                            name=timestamp_id,
                            value=description,
                            inline=False
                        )
                    except (ValueError, KeyError) as e:
                        logger.warning(f"Invalid import record: {e}")
                        continue
                
                await interaction.followup.send(embed=embed, ephemeral=True)
                
            elif action.value == "undo":
                # Undo import by timestamp
                if not timestamp:
                    await interaction.followup.send("Please provide a timestamp ID from `/log_import action:list`.", ephemeral=True)
                    return
                
                try:
                    # Convert nanosecond timestamp back to datetime
                    timestamp_ns = int(timestamp)
                    timestamp_seconds = timestamp_ns / 1000000000
                    import_time = datetime.fromtimestamp(timestamp_seconds)
                    
                    undo_result = await self.activity_repo.undo_import(user_id, import_time)
                    
                    if undo_result["count"] == 0:
                        embed = discord.Embed(
                            title="Import Not Found",
                            description="No activities were removed. Make sure you are using the correct timestamp ID.",
                            color=discord.Color.orange()
                        )
                    else:
                        await self.goal_service.refresh_user_goals(user_id)
                        
                        # Format statistics
                        total_duration = undo_result["total_duration"]
                        total_characters = undo_result["total_characters"]
                        hours = total_duration / 3600 if total_duration > 0 else 0
                        duration_str = f"{hours:.1f}h" if hours >= 1 else f"{total_duration // 60}m"
                        characters_str = f"{total_characters:,}" if total_characters > 0 else "0"
                        
                        embed = discord.Embed(
                            title="Import Undone",
                            description=f"Successfully removed **{undo_result['count']}** imported activities.",
                            color=discord.Color.green()
                        )
                        embed.add_field(
                            name="📉 Statistics Removed",
                            value=f"**Time:** {duration_str}\n**Characters:** {characters_str}",
                            inline=True
                        )
                        
                        # Trigger role sync after successful import undo
                        try:
                            await self._sync_roles_and_notify(interaction, "import_undo", interaction.user)
                        except Exception as sync_error:
                            logger.warning(f"Role sync failed after import undo: {sync_error}")
                    
                    await interaction.followup.send(embed=embed, ephemeral=False)
                    
                except (ValueError, OverflowError):
                    await interaction.followup.send("Invalid timestamp ID format.", ephemeral=True)
                
            elif action.value == "cleanup":
                # Clean up orphaned import records
                removed_count = await self.activity_repo.db.cleanup_orphaned_imports(user_id)
                
                embed = discord.Embed(
                    title="Import History Cleaned",
                    description=f"Removed {removed_count} orphaned import records from your history.",
                    color=discord.Color.green()
                )
                
                await interaction.followup.send(embed=embed, ephemeral=True)
                
            elif action.value == "import":
                # Import from file
                if not file:
                    await interaction.followup.send("Please attach a JSONL file to import.", ephemeral=True)
                    return
                
                # Check file extension
                filename = file.filename.lower()
                if not (filename.endswith('.jsonl') or filename.endswith('.jsonl.gz')):
                    await interaction.followup.send("Invalid file type. Please upload a .jsonl or .jsonl.gz file.", ephemeral=True)
                    return
                
                # Check file size (max 10MB)
                if file.size > 10 * 1024 * 1024:
                    await interaction.followup.send("File too large. Maximum size is 10MB.", ephemeral=True)
                    return
                
                try:
                    from utils.data_transfer import import_manager
                    
                    # Download file content
                    file_content = await file.read()
                    is_compressed = filename.endswith('.gz')
                    
                    # Parse activities
                    activities = await import_manager.parse_jsonl_file(
                        file_content,
                        filename=file.filename,
                        is_compressed=is_compressed,
                    )
                    
                    if not activities:
                        await interaction.followup.send("No valid activities found in the file.", ephemeral=True)
                        return
                    
                    # Validate activities
                    validation_errors = import_manager.validate_import_activities(activities)
                    if validation_errors:
                        error_text = "\n".join(validation_errors[:10])  # Show first 10 errors
                        if len(validation_errors) > 10:
                            error_text += f"\n... and {len(validation_errors) - 10} more errors"
                        
                        embed = discord.Embed(
                            title="Import Validation Failed",
                            description=f"Found {len(validation_errors)} validation errors:",
                            color=discord.Color.red()
                        )
                        embed.add_field(name="Errors", value=f"```{error_text}```", inline=False)
                        await interaction.followup.send(embed=embed, ephemeral=True)
                        return
                    
                    # Import activities
                    logger.info(f"Discord import: user {user_id}, {len(activities)} parsed activities")
                    result = await self.activity_repo.import_activities(
                        user_id=user_id,
                        activities=activities,
                        filename=file.filename
                    )
                    logger.info(f"Discord import result: {result}")
                    
                    if result['success']:
                        # Calculate statistics from imported activities
                        total_duration = sum(activity.get('duration', 0) for activity in activities)
                        total_characters = sum(activity.get('meta', {}).get('characters', 0) for activity in activities)
                        
                        # Format statistics
                        hours = total_duration / 3600 if total_duration > 0 else 0
                        duration_str = f"{hours:.1f}h" if hours >= 1 else f"{total_duration // 60}m"
                        characters_str = f"{total_characters:,}" if total_characters > 0 else "0"
                        
                        embed = discord.Embed(
                            title="Import Successful",
                            description=f"Successfully imported **{result['imported_count']}** activities.",
                            color=discord.Color.green()
                        )
                        embed.add_field(
                            name="📊 Statistics Added",
                            value=f"**Time:** {duration_str}\n**Characters:** {characters_str}",
                            inline=True
                        )
                        embed.add_field(
                            name="View History",
                            value="Use `/log_import action:list` to see your import history.",
                            inline=False
                        )
                        embed.set_footer(text="Activities are marked with import timestamps for easy management")
                        
                        # Trigger role sync after successful import
                        try:
                            await self._sync_roles_and_notify(interaction, "import", interaction.user)
                        except Exception as sync_error:
                            logger.warning(f"Role sync failed after import: {sync_error}")
                    else:
                        embed = discord.Embed(
                            title="Import Failed",
                            description=f"Import failed: {result.get('error', 'Unknown error')}",
                            color=discord.Color.red()
                        )
                    
                    await interaction.followup.send(embed=embed, ephemeral=False)
                    
                except ImportError:
                    await interaction.followup.send("Import functionality not available. Please contact an administrator.", ephemeral=True)
                except ValueError as e:
                    await interaction.followup.send(f"Import failed: {e}", ephemeral=True)
                except Exception as import_error:
                    logger.error(f"Import error: {import_error}")
                    await interaction.followup.send("Failed to import activities. Please check your file format and try again.", ephemeral=True)
            
            else:
                await interaction.followup.send("Invalid action specified.", ephemeral=True)
                
        except Exception as e:
            logger.error(f"Error in log_import command: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("An error occurred while processing the import.", ephemeral=True)
            else:
                await interaction.followup.send("An error occurred while processing the import.", ephemeral=True)

    def _normalize_class_role_definitions(self, roles: Optional[List[Any]]) -> List[Dict[str, float]]:
        """Normalize class role definitions to sorted dictionaries."""
        normalized: List[Dict[str, float]] = []
        if not roles:
            return normalized

        for entry in roles:
            name: Optional[str] = None
            min_hours_value: float = 0.0

            if isinstance(entry, dict):
                name = entry.get("name")
                min_hours_value = entry.get("min_hours", 0)
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                name = entry[0]
                min_hours_value = entry[1]

            if not name:
                continue

            try:
                hours = float(min_hours_value)
            except (TypeError, ValueError):
                hours = 0.0

            normalized.append({"name": str(name), "min_hours": max(0.0, hours)})

        normalized.sort(key=lambda item: item["min_hours"])
        return normalized

    def _normalize_rank_role_definitions(self, roles: Optional[List[Any]]) -> List[Dict[str, Any]]:
        """Normalize special ranking roles to dictionaries."""
        normalized: List[Dict[str, Any]] = []
        if not roles:
            return normalized

        for entry in roles:
            name: Optional[str] = None
            scope: str = "monthly"
            position: int = 1
            limit: int = 1

            if isinstance(entry, dict):
                name = entry.get("name")
                scope = str(entry.get("scope", "monthly")).lower()
                position = entry.get("position", 1)
                limit = entry.get("limit", entry.get("count", 1))
            elif isinstance(entry, (list, tuple)) and len(entry) >= 3:
                name = entry[0]
                scope = str(entry[1]).lower()
                position = entry[2]
                if len(entry) >= 4:
                    limit = entry[3]

            if not name:
                continue

            if scope not in {"monthly", "all_time"}:
                if scope in {"prev_month", "previous_month"}:
                    scope = "prev_month"
                else:
                    scope = "monthly"

            try:
                role_position = int(position)
            except (TypeError, ValueError):
                role_position = 1

            try:
                role_limit = int(limit)
            except (TypeError, ValueError):
                role_limit = 1

            normalized.append({
                "name": str(name),
                "scope": scope,
                "position": max(1, role_position),
                "limit": max(1, role_limit)
            })

        # Order by scope then position for consistent processing
        normalized.sort(key=lambda item: (item["scope"], item["position"]))
        return normalized

    def _config_enabled(self, value: Any, default: bool = True) -> bool:
        if isinstance(value, dict) and "enabled" in value:
            return bool(value["enabled"])
        if isinstance(value, bool):
            return value
        return default

    def _extract_class_role_override(self, value: Any) -> Optional[List[Dict[str, float]]]:
        if isinstance(value, dict):
            candidates = value.get("definitions") or value.get("roles")
        elif isinstance(value, list):
            candidates = value
        else:
            candidates = None

        if not candidates:
            return None

        normalized = self._normalize_class_role_definitions(candidates)
        return normalized or None

    def _extract_rank_role_override(self, value: Any) -> Optional[List[Dict[str, Any]]]:
        if isinstance(value, dict):
            candidates = value.get("definitions") or value.get("roles")
        elif isinstance(value, list):
            candidates = value
        else:
            candidates = None

        if not candidates:
            return None

        normalized = self._normalize_rank_role_definitions(candidates)
        return normalized or None

    def _get_class_role_definitions_for_guild(self, guild: discord.Guild) -> List[Dict[str, float]]:
        guild_config = self.bot.config.get("guilds", {}).get(str(guild.id), {})
        guild_override = self._extract_class_role_override(guild_config.get("class_roles"))
        if guild_override:
            return guild_override

        global_override = self._extract_class_role_override(self.bot.config.get("class_roles"))
        if global_override:
            return global_override

        return list(self._default_class_role_definitions)

    def _get_rank_role_definitions_for_guild(self, guild: discord.Guild) -> List[Dict[str, Any]]:
        guild_config = self.bot.config.get("guilds", {}).get(str(guild.id), {})
        guild_override = self._extract_rank_role_override(guild_config.get("class_rank_roles"))
        if guild_override:
            return guild_override

        global_override = self._extract_rank_role_override(self.bot.config.get("class_rank_roles"))
        if global_override:
            return global_override

        return list(self._default_rank_role_definitions)

    def _guild_allows_class_roles(self, guild: discord.Guild) -> bool:
        guild_config = self.bot.config.get("guilds", {}).get(str(guild.id), {})
        guild_setting = guild_config.get("class_roles")
        if guild_setting is not None and not self._config_enabled(guild_setting, True):
            return False

        global_setting = self.bot.config.get("class_roles")
        if global_setting is not None and not self._config_enabled(global_setting, True):
            return False

        return True

    def _should_sync_member(self, member: discord.Member) -> bool:
        return not member.bot

    @staticmethod
    def _infer_quiz_level_from_roles(member: discord.Member) -> int:
        levels: List[int] = []
        for role in member.roles:
            if not role.name:
                continue
            if role.name.isdigit():
                try:
                    levels.append(int(role.name))
                except ValueError:
                    continue
        return max(levels) if levels else 0

    @staticmethod
    def _create_progress_bar(current: float, target: float, width: int = 20) -> str:
        """Create a visual progress bar for VN class advancement."""
        if target <= 0:
            return "█" * width
        
        progress = min(current / target, 1.0)
        filled = int(progress * width)
        empty = width - filled
        
        return "█" * filled + "░" * empty

    @staticmethod
    def _create_detailed_progress_bar(progress_ratio: float, width: int = 20) -> str:
        """Create a detailed progress bar with percentage display."""
        progress_ratio = min(max(progress_ratio, 0.0), 1.0)
        filled = int(progress_ratio * width)
        empty = width - filled
        
        bar = "█" * filled + "░" * empty
        percentage = int(progress_ratio * 100)
        
        return f"{bar} {percentage}%"

    def _select_class_role(
        self,
        total_seconds: int,
        role_definitions: List[Dict[str, float]],
        role_map: Dict[str, discord.Role]
    ) -> Optional[discord.Role]:
        if not role_definitions:
            return None

        hours = (total_seconds or 0) / 3600
        selected: Optional[discord.Role] = None

        for definition in role_definitions:
            if hours >= definition["min_hours"]:
                candidate = role_map.get(definition["name"])
                if candidate:
                    selected = candidate
            else:
                break

        return selected

    async def ensure_class_roles_exist(
        self,
        guild: discord.Guild,
        role_definitions: List[Dict[str, float]]
    ) -> Dict[str, discord.Role]:
        role_map: Dict[str, discord.Role] = {}

        for definition in role_definitions:
            role_name = definition["name"]
            role = discord.utils.get(guild.roles, name=role_name)

            if not role:
                try:
                    role = await guild.create_role(
                        name=role_name,
                        reason="Class role synchronization",
                        color=discord.Color.teal()
                    )
                    logger.info("Created VN class role '%s' in %s", role_name, guild.name)
                except discord.Forbidden:
                    logger.warning("Missing permissions to create role '%s' in %s", role_name, guild.name)
                    continue
                except discord.HTTPException as exc:
                    logger.warning("Failed to create role '%s' in %s: %s", role_name, guild.name, exc)
                    continue

            role_map[role_name] = role

        return role_map

    async def ensure_rank_roles_exist(
        self,
        guild: discord.Guild,
        rank_definitions: List[Dict[str, Any]]
    ) -> Dict[str, discord.Role]:
        role_map: Dict[str, discord.Role] = {}

        for definition in rank_definitions:
            role_name = definition["name"]
            role = discord.utils.get(guild.roles, name=role_name)

            if not role:
                try:
                    role = await guild.create_role(
                        name=role_name,
                        reason="VN ranking role synchronization",
                        color=discord.Color.gold()
                    )
                    logger.info("Created VN ranking role '%s' in %s", role_name, guild.name)
                except discord.Forbidden:
                    logger.warning("Missing permissions to create ranking role '%s' in %s", role_name, guild.name)
                    continue
                except discord.HTTPException as exc:
                    logger.warning("Failed to create ranking role '%s' in %s: %s", role_name, guild.name, exc)
                    continue

            role_map[role_name] = role

        return role_map

    async def sync_reward_roles_for_guild(self, guild: discord.Guild, source: str = "scheduled") -> Dict[str, Any]:
        if not self._guild_allows_class_roles(guild):
            return {"class_assigned": 0, "class_removed": 0, "rank_assigned": 0, "rank_removed": 0}

        async with self.role_sync_lock:
            return await self._sync_reward_roles(guild, source)

    async def _sync_reward_roles(self, guild: discord.Guild, source: str) -> Dict[str, int]:
        role_definitions = self._get_class_role_definitions_for_guild(guild)
        if not role_definitions:
            logger.debug("No class role definitions found for guild %s", guild.name)
            return {"class_assigned": 0, "class_removed": 0, "rank_assigned": 0, "rank_removed": 0}

        class_roles = await self.ensure_class_roles_exist(guild, role_definitions)
        if not class_roles:
            logger.warning("Unable to prepare class roles for guild %s", guild.name)
            return {"class_assigned": 0, "class_removed": 0, "rank_assigned": 0, "rank_removed": 0}

        now = datetime.now(timezone.utc)
        guild_id_str = str(guild.id)

        if not guild.chunked:
            try:
                await guild.chunk(cache=True)
            except Exception as exc:
                logger.warning("Failed to chunk members for %s during VN role sync: %s", guild.name, exc)

        members = [member for member in guild.members if self._should_sync_member(member)]
        if not members:
            logger.debug("Class role sync found no eligible members in %s", guild.name)

        # ⚠️ DO NOT add guild-based filtering here. VNCRbot aggregates VN time across every
        # guild it serves; filtering by guild desyncs rank roles between guilds.
        all_time_durations = await self.activity_repo.get_vn_durations_by_user()
        class_assigned, class_removed, class_changes = await self._apply_class_roles(
            guild,
            members,
            role_definitions,
            class_roles,
            all_time_durations,
            source
        )

        rank_definitions = self._get_rank_role_definitions_for_guild(guild)
        rank_roles = await self.ensure_rank_roles_exist(guild, rank_definitions)

        # Sort rank definitions so that broader scopes (all_time) are processed before narrower ones
        # to avoid higher priority roles being overwritten by monthly roles later in the loop.
        scope_priority = {"all_time": 0, "monthly": 1, "prev_month": 2}
        rank_definitions = sorted(
            rank_definitions,
            key=lambda definition: (scope_priority.get(definition.get("scope"), 99), definition.get("position", 0))
        )

        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        monthly_durations = await self.activity_repo.get_vn_durations_by_user(
            start_date=month_start,
            end_date=now
        )

        if month_start.month == 1:
            prev_month_start = month_start.replace(year=month_start.year - 1, month=12)
        else:
            prev_month_start = month_start.replace(month=month_start.month - 1)

        prev_month_end = month_start - timedelta(microseconds=1)
        prev_month_durations = await self.activity_repo.get_vn_durations_by_user(
            start_date=prev_month_start,
            end_date=prev_month_end
        )

        duration_maps = {
            "all_time": all_time_durations,
            "monthly": monthly_durations,
            "prev_month": prev_month_durations
        }

        rank_assigned, rank_removed = await self._apply_rank_roles(
            guild,
            members,
            rank_definitions,
            rank_roles,
            duration_maps,
            now,
            source
        )

        return {
            "class_assigned": class_assigned,
            "class_removed": class_removed,
            "rank_assigned": rank_assigned,
            "rank_removed": rank_removed,
            "class_changes": class_changes,
        }

    async def _apply_class_roles(
        self,
        guild: discord.Guild,
        members: List[discord.Member],
        role_definitions: List[Dict[str, float]],
        role_map: Dict[str, discord.Role],
        duration_map: Dict[str, int],
        source: str
    ) -> Tuple[int, int, List[Dict[str, Any]]]:
        if not role_map:
            return 0, 0, []

        class_roles = [role_map.get(definition["name"]) for definition in role_definitions if role_map.get(definition["name"])]
        if not class_roles:
            return 0, 0, []

        class_role_set = set(class_roles)
        reason = f"Class role sync ({source})"

        assigned = 0
        removed = 0
        changes: List[Dict[str, Any]] = []
        role_order = {definition["name"]: index for index, definition in enumerate(role_definitions)}

        blacklisted_ids = await self.db.get_blacklisted_user_ids()

        for member in members:
            member_id_str = str(member.id)
            total_seconds = duration_map.get(member_id_str, 0)

            if member_id_str in blacklisted_ids:
                # For blacklisted users, force them to base role (first in definitions)
                base_role_name = role_definitions[0]["name"] if role_definitions else None
                target_role = role_map.get(base_role_name) if base_role_name else None
            else:
                # Normal role calculation for non-blacklisted users
                target_role = self._select_class_role(total_seconds, role_definitions, role_map)

            current_roles = [role for role in class_role_set if role in member.roles]

            previous_role: Optional[discord.Role] = None
            for definition in reversed(role_definitions):
                candidate = role_map.get(definition["name"])
                if candidate and candidate in current_roles:
                    previous_role = candidate
                    break

            to_remove = [role for role in current_roles if role != target_role]
            to_add: List[discord.Role] = []

            if target_role and target_role not in member.roles:
                to_add.append(target_role)

            if not to_remove and not to_add:
                continue

            # Determine if this is an actual class tier change
            previous_index = role_order.get(previous_role.name) if previous_role else None
            new_index = role_order.get(target_role.name) if target_role else None
            is_actual_change = previous_index != new_index

            change_record = {
                "member_id": member.id,
                "previous_role": previous_role,
                "previous_role_name": previous_role.name if previous_role else None,
                "previous_index": previous_index,
                "new_role": target_role,
                "new_role_name": target_role.name if target_role else None,
                "new_index": new_index,
                "total_seconds": total_seconds,
            }

            try:
                if to_remove:
                    await member.remove_roles(*to_remove, reason=reason)
                    removed += len(to_remove)
                if to_add:
                    await member.add_roles(*to_add, reason=reason)
                    assigned += len(to_add)
                # Only record as a "change" if the class tier actually changed
                if is_actual_change:
                    changes.append(change_record)
            except discord.Forbidden:
                logger.debug("Insufficient permissions to update class roles for %s", member)
            except discord.HTTPException as exc:
                logger.warning("Failed to update class roles for %s: %s", member, exc)

        guild_name = guild.name if guild else "unknown-guild"
        logger.debug(
            "Class role sync applied in %s: assigned=%d removed=%d members=%d",
            guild_name,
            assigned,
            removed,
            len(members)
        )
        return assigned, removed, changes

    async def _apply_rank_roles(
        self,
        guild: discord.Guild,
        members: List[discord.Member],
        rank_definitions: List[Dict[str, Any]],
        role_map: Dict[str, discord.Role],
        duration_maps: Dict[str, Dict[str, int]],
        now: datetime,
        source: str
    ):
        if not rank_definitions or not role_map:
            return 0, 0

        members_by_id = {str(member.id): member for member in members}

        # Ensure holders of existing ranking roles are considered even if not present in the initial member list
        for role in role_map.values():
            for holder in getattr(role, "members", []):
                if not self._should_sync_member(holder):
                    continue
                holder_id = str(holder.id)
                if holder_id not in members_by_id:
                    members_by_id[holder_id] = holder
                    members.append(holder)

    # Build ranking lists for the configured scopes
        blacklisted_ids = await self.db.get_blacklisted_user_ids()

        rankings: Dict[str, List[tuple[str, int]]] = {}
        for scope_key in {definition["scope"] for definition in rank_definitions}:
            duration_source = duration_maps.get(scope_key) or duration_maps.get("all_time", {})

            entries = [
                (user_id, total)
                for user_id, total in duration_source.items()
                if user_id in members_by_id and total > 0 and user_id not in blacklisted_ids
            ]

            # Sort by total duration desc, then by user id for stability
            entries.sort(key=lambda item: (-item[1], item[0]))

            unique_entries: List[tuple[str, int]] = []
            seen: set[str] = set()
            for user_id, total in entries:
                if user_id in seen:
                    continue
                unique_entries.append((user_id, total))
                seen.add(user_id)

            rankings[scope_key] = unique_entries

        reason = f"VN ranking role sync ({source})"
        total_assigned = 0
        total_removed = 0
        for definition in rank_definitions:
            role = role_map.get(definition["name"])
            if not role:
                continue

            ranking_list = rankings.get(definition["scope"], [])
            index = max(0, definition["position"] - 1)
            limit = max(1, definition.get("limit", 1))

            target_members: List[discord.Member] = []
            if index < len(ranking_list):
                end_index = min(len(ranking_list), index + limit)
                target_members = [
                    members_by_id.get(user_id)
                    for user_id, _ in ranking_list[index:end_index]
                    if members_by_id.get(user_id)
                ]

            removed, assigned = await self._update_rank_role(role, target_members, members, limit, reason)
            total_assigned += assigned
            total_removed += removed
            if removed or assigned:
                logger.debug(
                    "VN ranking role sync in %s (%s #%d): assigned=%d removed=%d",
                    guild.name,
                    definition["scope"],
                    definition["position"],
                    assigned,
                    removed
                )
        return total_assigned, total_removed

    async def _update_rank_role(
        self,
        role: discord.Role,
        target_members: List[Optional[discord.Member]],
        members: List[discord.Member],
        limit: int,
        reason: str
    ) -> tuple[int, int]:
        if limit > 0:
            target_members = target_members[:limit]

        target_set = {member for member in target_members if member is not None}
        removed = 0
        assigned = 0

        for member in members:
            has_role = role in member.roles
            should_have = member in target_set

            if has_role and not should_have:
                try:
                    await member.remove_roles(role, reason=reason)
                    removed += 1
                except discord.Forbidden:
                    logger.debug("Insufficient permissions to remove ranking role %s from %s", role.name, member)
                except discord.HTTPException as exc:
                    logger.warning("Failed to remove ranking role %s from %s: %s", role.name, member, exc)
            elif should_have and not has_role:
                try:
                    await member.add_roles(role, reason=reason)
                    assigned += 1
                except discord.Forbidden:
                    logger.debug("Insufficient permissions to assign ranking role %s to %s", role.name, member)
                except discord.HTTPException as exc:
                    logger.warning("Failed to assign ranking role %s to %s: %s", role.name, member, exc)

        return removed, assigned

    async def _initial_role_sync(self):
        await self.bot.wait_until_ready()

        for guild in self.bot.guilds:
            try:
                await self.sync_reward_roles_for_guild(guild, source="startup")
            except Exception as exc:
                logger.warning("Initial class role sync failed for %s: %s", guild.name, exc)

    async def _sync_guild_info(self):
        """Sync guild information to database for cross-bot compatibility"""
        await self.bot.wait_until_ready()
        try:
            # Wait a bit to ensure bot is fully ready
            await asyncio.sleep(3)
            
            # Sync information for all guilds the bot is in
            for guild in self.bot.guilds:
                try:
                    await self.db.sync_guild_info(guild.id, guild.name, str(guild.icon.url) if guild.icon else None)
                    logger.info(f"Synced guild info for {guild.name} ({guild.id})")
                except Exception as e:
                    logger.error(f"Failed to sync guild info for {guild.name} ({guild.id}): {e}")
            
            logger.info(f"Guild info sync completed for {len(self.bot.guilds)} guilds")
        except Exception as e:
            logger.error(f"Error during guild info sync: {e}")

    @tasks.loop(hours=1)
    async def class_role_sync(self):
        for guild in self.bot.guilds:
            try:
                await self.sync_reward_roles_for_guild(guild, source="scheduled")
            except Exception as exc:
                logger.warning("Scheduled class role sync failed for %s: %s", guild.name, exc)

    @class_role_sync.before_loop
    async def before_class_role_sync(self):
        await self.bot.wait_until_ready()

    

class LeaderboardView(discord.ui.View):
    """Interactive view for leaderboard pagination"""
    
    def __init__(self, bot: commands.Bot, activity_repo: ActivityRepository, current_page: int, 
                 total_pages: int, timeframe: str, start_date: Optional[datetime], 
                 now: datetime, media_type: Optional[str] = None, 
                 activity_type: Optional[str] = None, metric: str = "duration",
                 page_size: int = 10, guild_id: Optional[str] = None,
                 primary_guild_id: Optional[int] = None,
                 prefetched_entries: Optional[List[LeaderboardEntry]] = None):
        super().__init__(timeout=120.0)
        self.bot = bot
        self.activity_repo = activity_repo
        self.current_page = current_page
        self.total_pages = total_pages
        self.timeframe = timeframe
        self.start_date = start_date
        self.now = now
        self.media_type = media_type
        self.activity_type = activity_type
        self.metric = metric
        self.page_size = page_size
        self.guild_id = guild_id
        self.primary_guild_id = primary_guild_id
        self._display_name_cache: Dict[int, str] = {}
        self.prefetched_entries = prefetched_entries
        
        # Disable buttons if needed
        if current_page <= 1:
            self.previous_button.disabled = True
        if current_page >= total_pages:
            self.next_button.disabled = True
    
    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, emoji="◀️")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page <= 1:
            return
        
        await self._update_page(interaction, self.current_page - 1)
    
    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, emoji="▶️")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page >= self.total_pages:
            return
        
        await self._update_page(interaction, self.current_page + 1)
    
    async def _update_page(self, interaction: discord.Interaction, new_page: int):
        """Update the leaderboard page"""
        try:
            # Calculate offset for pagination
            offset = (new_page - 1) * self.page_size
            
            # Get top members for this page (fetches everything up to the current page)
            # because SQL LIMIT/OFFSET doesn't work with the aggregation
            total_to_fetch = new_page * self.page_size
            
            if self.prefetched_entries is not None:
                filtered_entries = self.prefetched_entries
                top_members = filtered_entries[offset:offset + self.page_size]
            else:
                top_members = await self.activity_repo.get_top_members(
                    guild_id=None,
                    limit=total_to_fetch,
                    start_date=self.start_date,
                    end_date=self.now,
                    media_type=self.media_type,
                    activity_type=self.activity_type,
                    metric=self.metric,
                )
                top_members = top_members[offset:offset + self.page_size]
            
            if not top_members:
                await interaction.response.send_message("No data available for this page.", ephemeral=True)
                return
            
            metric_key = (self.metric or "duration").lower()
            metric_labels = {
                "duration": "Duration",
                "characters": "Characters",
                "pages": "Pages"
            }
            metric_label = metric_labels.get(metric_key, "Duration")
            
            # Create leaderboard embed
            filters = []
            if self.activity_type:
                filters.append(self.activity_type.title())
            if self.media_type:
                filters.append(self.media_type.replace('_', ' ').title())
            if metric_key != "duration":
                filters.append(f"Metric: {metric_label}")
            if self.guild_id:
                filters.append(f"Guild: {LEADERBOARD_GUILD_FILTERS.get(self.guild_id, self.guild_id)}")
            filter_text = f" - {' | '.join(filters)}" if filters else ""
            embed = discord.Embed(
                title=f"Immersion Leaderboard - {self.timeframe.title()}{filter_text}",
                color=discord.Color.gold(),
                timestamp=self.now
            )
            
            if self.start_date:
                embed.set_footer(text=f"Page {new_page} of {self.total_pages} • Since {self.start_date.strftime('%Y-%m-%d')}")
            else:
                embed.set_footer(text=f"Page {new_page} of {self.total_pages}")
            
            # Add leaderboard entries (offset by current page rank)
            for i, member in enumerate(top_members, 1):
                rank = offset + i  # Calculate actual rank on full leaderboard
                display_name = await self._resolve_display_name(member.user_id, interaction.guild)
                
                metric_display = member.metric_display(metric_key)
                log_text = f"{member.activity_count} log{'s' if member.activity_count != 1 else ''}"
                details: List[str] = []
                if metric_key != "duration" and member.total_duration > 0:
                    details.append(f"Duration: {member.total_duration_str}")
                details.append(log_text)
                detail_text = " | ".join(details)
                value = metric_display
                if detail_text:
                    value += f"\n{detail_text}"
                
                embed.add_field(
                    name=f"{rank}. {display_name}",
                    value=value,
                    inline=False
                )
            
            # Update page state
            self.current_page = new_page
            
            # Update button states
            self.previous_button.disabled = (new_page <= 1)
            self.next_button.disabled = (new_page >= self.total_pages)
            
            await interaction.response.edit_message(embed=embed, view=self)
            
        except Exception as e:
            logger.error(f"Error updating leaderboard page: {e}")
            await interaction.response.send_message("Error updating page.", ephemeral=True)
    
    async def on_timeout(self):
        """Disable buttons when view times out"""
        for item in self.children:
            item.disabled = True

    async def _resolve_display_name(
        self,
        user_id: Union[int, str],
        interaction_guild: Optional[discord.Guild],
    ) -> str:
        """Resolve a user's display name across the bot's shared guilds."""
        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            return f"User {user_id}"

        cached = self._display_name_cache.get(user_id_int)
        if cached:
            return cached

        candidate_guilds: List[discord.Guild] = []
        if interaction_guild is not None:
            candidate_guilds.append(interaction_guild)

        if self.primary_guild_id is not None:
            primary_guild = self.bot.get_guild(self.primary_guild_id)
            if primary_guild and primary_guild not in candidate_guilds:
                candidate_guilds.append(primary_guild)

        for guild in getattr(self.bot, "guilds", []):
            if guild not in candidate_guilds:
                candidate_guilds.append(guild)

        for guild in candidate_guilds:
            member = guild.get_member(user_id_int)
            if member:
                display_name = member.display_name
                self._display_name_cache[user_id_int] = display_name
                return display_name

        user = self.bot.get_user(user_id_int)
        if user:
            display_name = getattr(user, "display_name", getattr(user, "name", f"User {user_id_int}"))
            self._display_name_cache[user_id_int] = display_name
            return display_name

        try:
            user = await self.bot.fetch_user(user_id_int)
        except Exception:
            user = None

        if user:
            display_name = getattr(user, "display_name", getattr(user, "name", f"User {user_id_int}"))
            self._display_name_cache[user_id_int] = display_name
            return display_name

        fallback = f"User {user_id_int}"
        self._display_name_cache[user_id_int] = fallback
        return fallback

class HistoryView(discord.ui.View):
    """Interactive view for history pagination"""
    
    def __init__(self, user_id: str, guild_id: str, current_page: int,
                 total_pages: int, activity_repo: ActivityRepository,
                 invoker_id: int, activity_type: str = None, media_type: str = None,
                 page_size: int = 6):
        super().__init__(timeout=120.0)
        self.user_id = user_id
        self.guild_id = guild_id
        self.current_page = current_page
        self.total_pages = total_pages
        self.activity_repo = activity_repo
        self.invoker_id = invoker_id
        self.activity_type = activity_type
        self.media_type = media_type
        self.page_size = page_size

        # Disable buttons if needed
        if current_page <= 1:
            self.previous_button.disabled = True
        if current_page >= total_pages:
            self.next_button.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, emoji="◀️")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page <= 1:
            return

        await self._update_page(interaction, self.current_page - 1)
    
    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, emoji="▶️")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page >= self.total_pages:
            return
        
        await self._update_page(interaction, self.current_page + 1)
    
    async def _update_page(self, interaction: discord.Interaction, new_page: int):
        """Update the history page"""
        try:
            # Get total count and adjust page if necessary
            total_count = await self.activity_repo.db.get_user_activity_count(
                self.user_id,
                self.guild_id,
                primary_type=self.activity_type,
                media_type=self.media_type
            )
            max_page = (total_count + self.page_size - 1) // self.page_size if total_count > 0 else 1
            
            if new_page > max_page:
                new_page = max_page
            elif new_page < 1:
                new_page = 1
                
            offset = (new_page - 1) * self.page_size
            
            # Get new page data
            activity_page = await self.activity_repo.page_by_user_id(
                user_id=self.user_id,
                guild_id=self.guild_id,
                page_size=self.page_size,
                offset=offset,
                primary_type=self.activity_type,
                media_type=self.media_type
            )
            
            # Update embed
            embed = discord.Embed(
                title="Log History",
                color=discord.Color.blue()
            )
            
            # Try to get member from guild first for server avatar, fallback to user
            user = None
            if interaction.guild:
                user = interaction.guild.get_member(int(self.user_id))
            if not user:
                user = interaction.client.get_user(int(self.user_id))
            
            if user:
                embed.set_author(
                    name=user.display_name,
                    icon_url=user.display_avatar.url if user.display_avatar else None
                )
            filters_text = []
            if self.activity_type:
                filters_text.append(f"Activity: {self.activity_type.title()}")
            if self.media_type:
                filters_text.append(f"Media: {self.media_type.replace('_', ' ').title()}")
            if filters_text:
                embed.description = " | ".join(filters_text)
            
            embed.set_footer(
                text=f"Page {activity_page.page} of {activity_page.page_count}"
            )
            
            # Add activity fields (always show IDs)
            for activity in activity_page.activities:
                field_name = f"ID: {activity.id}"
                embed.add_field(
                    name=field_name,
                    value=_format_history_entry(activity),
                    inline=True
                )
            
            # Update page state
            self.current_page = new_page
            self.total_pages = activity_page.page_count
            
            # Update button states
            self.previous_button.disabled = (new_page <= 1)
            self.next_button.disabled = (new_page >= self.total_pages)
            
            await interaction.response.edit_message(embed=embed, view=self)
            
        except Exception as e:
            logger.error(f"Error updating history page: {e}")
            await interaction.response.send_message("Error updating page.", ephemeral=True)
    
    async def on_timeout(self):
        """Disable buttons when view times out"""
        for item in self.children:
            item.disabled = True


class LibraryNavigationView(discord.ui.View):
    """Interactive view for library pagination"""
    
    def __init__(self, user_id: str, target_user: discord.User, current_page: int,
                 total_pages: int, invoker_id: int, media_type: str = None, activity_type: str = None,
                 sort_by: str = "recent", activity_repo: ActivityRepository = None, cog = None,
                 view_mode: str = "gallery"):
        super().__init__(timeout=300.0)  # 5 minutes
        self.user_id = user_id
        self.target_user = target_user
        self.current_page = current_page
        self.total_pages = total_pages
        self.invoker_id = invoker_id
        self.media_type = media_type
        self.activity_type = activity_type
        self.sort_by = sort_by
        self.activity_repo = activity_repo
        self.cog = cog
        self.view_mode = view_mode  # "gallery" or "list"

        # Disable buttons if needed
        if current_page <= 1:
            self.previous_button.disabled = True
        if current_page >= total_pages:
            self.next_button.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, emoji="◀️", row=0)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page <= 1:
            return
        
        await self._update_page(interaction, self.current_page - 1)
    
    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, emoji="▶️", row=0)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page >= self.total_pages:
            return
        
        await self._update_page(interaction, self.current_page + 1)
    
    @discord.ui.button(label="List", style=discord.ButtonStyle.primary, emoji="📝", row=0)
    async def toggle_view_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Toggle between gallery and list view"""
        new_mode = "list" if self.view_mode == "gallery" else "gallery"
        await self._update_page(interaction, self.current_page, new_view_mode=new_mode)
    
    async def _update_page(self, interaction: discord.Interaction, new_page: int, new_view_mode: str = None):
        """Update the library page"""
        try:
            # Update view mode if specified
            if new_view_mode:
                self.view_mode = new_view_mode
                # Update toggle button
                if self.view_mode == "gallery":
                    self.toggle_view_button.label = "List"
                    self.toggle_view_button.emoji = "📝"
                else:
                    self.toggle_view_button.label = "Gallery"
                    self.toggle_view_button.emoji = "🖼️"
            
            # Get library data
            library_items = await self.activity_repo.get_user_library(
                user_id=self.user_id,
                media_type=self.media_type,
                activity_type=self.activity_type,
                sort_by=self.sort_by
            )
            
            if not library_items:
                if interaction.response.is_done():
                    await interaction.followup.send("No items found.", ephemeral=True)
                else:
                    await interaction.response.send_message("No items found.", ephemeral=True)
                return

            if not interaction.response.is_done():
                await interaction.response.defer()
            
            # For alphabetical sort, fetch Japanese titles for ALL VNs BEFORE pagination
            # This ensures sorting happens with the actual displayed titles
            if self.sort_by == "alphabetical":
                vns_with_vndb_id = [
                    item for item in library_items
                    if item.get('vndb_id') and item['media_type'] == 'visual_novel'
                ]
                
                if vns_with_vndb_id:
                    async def fetch_japanese_title(session, item):
                        try:
                            payload = {
                                "filters": ["id", "=", item['vndb_id']],
                                "fields": "titles.title, titles.lang"
                            }
                            async with session.post(
                                "https://api.vndb.org/kana/vn",
                                json=payload,
                                headers={"Content-Type": "application/json"},
                                timeout=aiohttp.ClientTimeout(total=3)
                            ) as response:
                                if response.status == 200:
                                    data = await response.json()
                                    if data.get("results"):
                                        vn = data["results"][0]
                                        titles = vn.get("titles", [])
                                        for title_obj in titles:
                                            if title_obj.get("lang") == "ja":
                                                item['name'] = title_obj.get("title")
                                                break
                        except Exception as exc:
                            logger.warning(f"Failed to fetch title for {item.get('name')}: {exc}")
                    
                    async with aiohttp.ClientSession() as session:
                        await asyncio.gather(*[fetch_japanese_title(session, item) for item in vns_with_vndb_id], return_exceptions=True)
                    
                    # Re-sort with updated Japanese titles
                    import unicodedata
                    def normalize_for_sort(text: str) -> str:
                        return unicodedata.normalize('NFD', text.lower())
                    library_items.sort(key=lambda x: normalize_for_sort(x['name']))
                    logger.info(f"Re-sorted {len(library_items)} items alphabetically after fetching Japanese titles")

            # Pagination - different items per page based on view mode
            if self.view_mode == "gallery":
                items_per_page = 3  # mobile-friendly for embeds
            else:
                items_per_page = 10  # compact list can show more
            
            total_pages = (len(library_items) + items_per_page - 1) // items_per_page
            
            if new_page < 1:
                new_page = 1
            elif new_page > total_pages:
                new_page = total_pages

            start_idx = (new_page - 1) * items_per_page
            end_idx = start_idx + items_per_page
            page_items = library_items[start_idx:end_idx]
            
            # For VNs with VNDB ID, fetch Japanese titles and NSFW status from VNDB API
            # This must complete BEFORE embeds are built
            vns_to_check = [
                item for item in page_items
                if item.get('vndb_id') and item['media_type'] == 'visual_novel'
            ]
            
            if vns_to_check:
                async def check_vn(session, item):
                    try:
                        payload = {
                            "filters": ["id", "=", item['vndb_id']],
                            "fields": "titles.title, titles.lang, image.url, image.sexual, image.violence"
                        }
                        async with session.post(
                            "https://api.vndb.org/kana/vn",
                            json=payload,
                            headers={"Content-Type": "application/json"},
                            timeout=aiohttp.ClientTimeout(total=3)
                        ) as response:
                            if response.status == 200:
                                data = await response.json()
                                if data.get("results"):
                                    vn = data["results"][0]
                                    
                                    # Get Japanese title from titles array
                                    titles = vn.get("titles", [])
                                    for title_obj in titles:
                                        if title_obj.get("lang") == "ja":
                                            # Update the name in place
                                            item['name'] = title_obj.get("title")
                                            break
                                    
                                    # Update cover and NSFW status
                                    image_info = vn.get("image") or {}
                                    current_cover_url = image_info.get("url")
                                    
                                    if current_cover_url:
                                        item['cover_url'] = current_cover_url
                                    item['cover_is_nsfw'] = is_image_explicit(image_info.get("sexual"))
                    except asyncio.TimeoutError:
                        logger.warning("[Pagination] VNDB lookup timeout for %s", item.get('name', 'unknown'))
                    except Exception as exc:
                        logger.warning("[Pagination] Failed VNDB lookup for %s: %s", item.get('name', 'unknown'), exc)
                
                # Wait for ALL VNDB lookups to complete before proceeding
                async with aiohttp.ClientSession() as session:
                    await asyncio.gather(*[check_vn(session, item) for item in vns_to_check], return_exceptions=True)


            
            # Media type emojis
            media_emojis = {
                'visual_novel': '📖', 'manga': '📕', 'anime': '📺',
                'book': '📚', 'video': '🎬', 'game': '🎮', 'manual': '✍️'
            }
            
            # Build filter description
            filter_parts = []
            if self.media_type:
                filter_parts.append(f"📑 {self.media_type.replace('_', ' ').title()}")
            if self.activity_type:
                filter_parts.append(f"⚡ {self.activity_type.title()}")
            
            # Add sort indicator
            sort_labels = {
                "duration": "⏱️ Sorted by Time",
                "count": "📊 Sorted by Log Count",
                "recent": "📅 Sorted by Recent",
                "alphabetical": "🔤 Sorted Alphabetically"
            }
            filter_parts.append(sort_labels.get(self.sort_by, ""))
            
            # Render based on view mode
            if self.view_mode == "gallery":
                # Create multiple embeds - one per item (gallery style)
                embeds = []
                
                for i, item in enumerate(page_items, start=start_idx + 1):
                    emoji = media_emojis.get(item['media_type'], '📄')
                    duration_str = _format_duration_value(item['total_duration']) if item['total_duration'] > 0 else "0m"
                    count = item['total_count']

                    # Format dates
                    try:
                        if item['last_logged']:
                            last_date = datetime.fromisoformat(item['last_logged']).strftime('%Y-%m-%d')
                        else:
                            last_date = "Unknown"
                    except:
                        last_date = "Unknown"

                    # Truncate title if too long
                    display_name = item['name'][:250] if len(item['name']) > 250 else item['name']

                    # Create individual embed for this item
                    item_embed = discord.Embed(
                        title=f"{emoji} {display_name}",
                        color=discord.Color.purple()
                    )

                    # Add cover as thumbnail if available and not NSFW
                    if item.get('cover_url') and not item.get('cover_is_nsfw'):
                        item_embed.set_thumbnail(url=item['cover_url'])
                    elif item.get('cover_url') and item.get('cover_is_nsfw'):
                        item_embed.add_field(name="🔞 NSFW Cover", value="Cover hidden", inline=False)

                    # Add stats
                    item_embed.add_field(name="⏱️ Total Time", value=f"**{duration_str}**", inline=True)
                    item_embed.add_field(name="📊 Log Count", value=f"**{count}** entries", inline=True)
                    item_embed.add_field(name="📅 Last Logged", value=last_date, inline=True)
                    
                    # Add database link if available
                    db_link = None
                    if item['media_type'] == 'visual_novel' and item.get('vndb_id'):
                        db_link = f"[VNDB](https://vndb.org/{item['vndb_id']})"
                    elif item['media_type'] in ['anime', 'manga'] and item.get('anilist_id'):
                        db_link = f"[AniList](https://anilist.co/{item['media_type']}/{item['anilist_id']})"
                    elif item['media_type'] == 'game' and item.get('igdb_id'):
                        db_link = f"[IGDB](https://www.igdb.com/games/{item['igdb_id']})"
                    elif item['media_type'] == 'video' and item.get('youtube_id'):
                        db_link = f"[YouTube](https://www.youtube.com/watch?v={item['youtube_id']})"
                    
                    if db_link:
                        item_embed.add_field(name="🔗 Database", value=db_link, inline=True)
                    
                    # Add media-specific metrics if available
                    total_chars = item.get('total_characters', 0)
                    total_item_pages = item.get('total_pages', 0)
                    total_eps = item.get('total_episodes', 0)
                    
                    if total_chars > 0:
                        item_embed.add_field(name="🔡 Total Characters", value=f"{int(total_chars):,}", inline=True)
                    if total_item_pages > 0:
                        formatted_pages = f"{total_item_pages:,.1f}".rstrip('0').rstrip('.')
                        item_embed.add_field(name="📄 Total Pages", value=formatted_pages, inline=True)
                    if total_eps > 0:
                        item_embed.add_field(name="📺 Total Episodes", value=f"{int(total_eps)}", inline=True)
                    
                    # Add filter info to first embed only
                    if i == start_idx + 1:
                        item_embed.set_author(
                            name=f"{self.target_user.display_name}'s Library",
                            icon_url=self.target_user.display_avatar.url if self.target_user.display_avatar else None
                        )
                    
                    # Add footer to last embed with pagination and filter info
                    if i == start_idx + len(page_items):
                        footer_parts = [
                            f"Page {new_page}/{total_pages}",
                            f"{len(library_items)} total titles"
                        ]
                        if filter_parts:
                            footer_parts.extend(filter_parts)
                        item_embed.set_footer(text=" • ".join(footer_parts))
                    
                    embeds.append(item_embed)
            
            else:
                # List view - compact text format
                lines = []
                lines.append(f"**{self.target_user.display_name}'s Library**\n")
                
                for i, item in enumerate(page_items, start=start_idx + 1):
                    emoji = media_emojis.get(item['media_type'], '📄')
                    duration_str = _format_duration_value(item['total_duration']) if item['total_duration'] > 0 else "0m"
                    count = item['total_count']

                    # Truncate title if too long
                    display_name = item['name'][:100] if len(item['name']) > 100 else item['name']

                    # Build database link
                    db_link = None
                    if item['media_type'] == 'visual_novel' and item.get('vndb_id'):
                        db_link = f"[VNDB](https://vndb.org/{item['vndb_id']})"
                    elif item['media_type'] in ['anime', 'manga'] and item.get('anilist_id'):
                        db_link = f"[AniList](https://anilist.co/{item['media_type']}/{item['anilist_id']})"
                    elif item['media_type'] == 'game' and item.get('igdb_id'):
                        db_link = f"[IGDB](https://www.igdb.com/games/{item['igdb_id']})"
                    elif item['media_type'] == 'video' and item.get('youtube_id'):
                        db_link = f"[YouTube](https://www.youtube.com/watch?v={item['youtube_id']})"

                    # Format line
                    line = f"`{i}.` {emoji} **{display_name}**"
                    line += f"\n⏱️ {duration_str} • 📊 {count} entries"
                    
                    # Add media-specific metrics if available
                    extras = []
                    total_chars = item.get('total_characters', 0)
                    total_item_pages = item.get('total_pages', 0)
                    total_eps = item.get('total_episodes', 0)
                    
                    if total_chars > 0:
                        extras.append(f"🔡 {int(total_chars):,}")
                    if total_item_pages > 0:
                        formatted_pages = f"{total_item_pages:,.1f}".rstrip('0').rstrip('.')
                        extras.append(f"📄 {formatted_pages}")
                    if total_eps > 0:
                        extras.append(f"📺 {int(total_eps)}")
                    
                    if extras:
                        line += f" • {' • '.join(extras)}"
                    
                    # Add database link at the end
                    if db_link:
                        line += f" • {db_link}"
                    
                    lines.append(line)
                    lines.append("")  # Empty line between items
                
                # Add pagination footer
                footer_parts = [
                    f"Page {new_page}/{total_pages}",
                    f"{len(library_items)} total titles"
                ]
                if filter_parts:
                    footer_parts.extend(filter_parts)
                lines.append("─" * 40)
                lines.append(" • ".join(footer_parts))
                
                # Create single embed with all text
                embeds = [discord.Embed(
                    description="\n".join(lines),
                    color=discord.Color.purple()
                )]

            
            # Update page state
            self.current_page = new_page
            self.total_pages = total_pages
            
            # Update button states
            self.previous_button.disabled = (new_page <= 1)
            self.next_button.disabled = (new_page >= total_pages)
            
            if interaction.response.is_done():
                await interaction.edit_original_response(embeds=embeds, view=self)
            else:
                await interaction.response.edit_message(embeds=embeds, view=self)
            
        except Exception as e:
            logger.error(f"Error updating library page: {e}")
            if interaction.response.is_done():
                await interaction.followup.send("Error updating page.", ephemeral=True)
            else:
                await interaction.response.send_message("Error updating page.", ephemeral=True)
    
    async def on_timeout(self):
        """Disable buttons when view times out"""
        for item in self.children:
            item.disabled = True


class InteractiveStatsView(discord.ui.View):
    """Interactive view for navigating different stats pages"""
    
    def __init__(self, cog: 'ImmersionCog', target_user: discord.Member, user_id: str,
                 invoker_id: int, days: int = 30, media_type: Optional[str] = None,
                 activity_type: Optional[str] = None, metric: str = "duration"):
        super().__init__(timeout=180.0)
        self.cog = cog
        self.target_user = target_user
        self.user_id = user_id
        self.invoker_id = invoker_id
        self.current_days = days
        self.media_type = media_type
        self.activity_type = activity_type
        self.metric = metric
        self.current_page = "overview"

        # Set initial defaults for select menus after they're added by decorators
        self._update_select_defaults()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True
    
    def _update_select_defaults(self):
        """Update select menu defaults based on current values"""
        for item in self.children:
            if not isinstance(item, discord.ui.Select):
                continue
                
            if item.custom_id == "time_period_select":
                for option in item.options:
                    option.default = (option.value == str(self.current_days))
                    
            elif item.custom_id == "media_type_select":
                for option in item.options:
                    if self.media_type is None:
                        option.default = (option.value == "none")
                    else:
                        option.default = (option.value == self.media_type)
                        
            elif item.custom_id == "activity_type_select":
                for option in item.options:
                    if self.activity_type is None:
                        option.default = (option.value == "none")
                    else:
                        option.default = (option.value == self.activity_type)
                        
            elif item.custom_id == "metric_select":
                for option in item.options:
                    option.default = (option.value == self.metric)
        
    @discord.ui.button(label="", style=discord.ButtonStyle.primary, emoji="📈", custom_id="stats_overview", row=0)
    async def overview_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show overview stats"""
        await self._update_view(interaction, "overview")
    
    @discord.ui.button(label="", style=discord.ButtonStyle.secondary, emoji="🥧", custom_id="stats_breakdown", row=0)
    async def breakdown_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show detailed breakdown"""
        await self._update_view(interaction, "breakdown")
    
    @discord.ui.button(label="", style=discord.ButtonStyle.secondary, emoji="📊", custom_id="stats_timeline", row=0)
    async def timeline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show day-by-day timeline"""
        await self._update_view(interaction, "timeline")
    
    @discord.ui.select(
        placeholder="📆 Select Time Period",
        options=[
            discord.SelectOption(label="Last 7 Days", value="7", emoji="📅"),
            discord.SelectOption(label="Last 30 Days", value="30", emoji="📆", default=True),
            discord.SelectOption(label="Last 90 Days", value="90", emoji="🗓️"),
            discord.SelectOption(label="Last Year", value="365", emoji="📅"),
            discord.SelectOption(label="All Time", value="9999", emoji="♾️"),
        ],
        custom_id="time_period_select",
        row=1
    )
    async def time_period_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        """Change time period"""
        self.current_days = int(select.values[0])
        
        # Update default in options
        for option in select.options:
            option.default = (option.value == select.values[0])
        
        await self._update_view(interaction, self.current_page)
    
    @discord.ui.select(
        placeholder="📑 Filter by Media Type",
        options=[
            discord.SelectOption(label="All Media", value="none", emoji="📚", default=True),
            discord.SelectOption(label="Visual Novel", value="visual_novel", emoji="📖"),
            discord.SelectOption(label="Manga", value="manga", emoji="📕"),
            discord.SelectOption(label="Anime", value="anime", emoji="📺"),
            discord.SelectOption(label="Book", value="book", emoji="📚"),
            discord.SelectOption(label="Video", value="video", emoji="🎬"),
            discord.SelectOption(label="Game", value="game", emoji="🎮"),
            discord.SelectOption(label="Manual Log", value="manual", emoji="✍️"),
        ],
        custom_id="media_type_select",
        row=2
    )
    async def media_type_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        """Change media type filter"""
        selected = select.values[0]
        self.media_type = None if selected == "none" else selected
        
        # Update default in options
        for option in select.options:
            option.default = (option.value == selected)
        
        # Validate metric compatibility
        if self.metric in ["characters", "pages"] and self.media_type in ["anime", "video"]:
            self.metric = "duration"
            # Update metric select menu default
            for item in self.children:
                if isinstance(item, discord.ui.Select) and item.custom_id == "metric_select":
                    for opt in item.options:
                        opt.default = (opt.value == "duration")
        
        await self._update_view(interaction, self.current_page)
    
    @discord.ui.select(
        placeholder="⚡ Filter by Activity Type",
        options=[
            discord.SelectOption(label="All Activities", value="none", emoji="📋", default=True),
            discord.SelectOption(label="Reading", value="reading", emoji="📚"),
            discord.SelectOption(label="Listening", value="listening", emoji="🎧"),
        ],
        custom_id="activity_type_select",
        row=3
    )
    async def activity_type_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        """Change activity type filter"""
        selected = select.values[0]
        self.activity_type = None if selected == "none" else selected
        
        # Update default in options
        for option in select.options:
            option.default = (option.value == selected)
        
        await self._update_view(interaction, self.current_page)
    
    @discord.ui.select(
        placeholder="📏 Select Metric",
        options=[
            discord.SelectOption(label="Duration (Time)", value="duration", emoji="⏱️", default=True),
            discord.SelectOption(label="Characters", value="characters", emoji="🔤"),
            discord.SelectOption(label="Pages", value="pages", emoji="📄"),
            discord.SelectOption(label="Episodes", value="episodes", emoji="📺"),
        ],
        custom_id="metric_select",
        row=4
    )
    async def metric_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        """Change metric"""
        selected = select.values[0]
        
        # Validate metric compatibility with media type
        if selected in ["characters", "pages"]:
            incompatible_types = ["anime", "video"]
            if self.media_type in incompatible_types:
                media_name = self.media_type.replace('_', ' ').title()
                metric_name = selected.title()
                await interaction.response.send_message(
                    f"❌ {metric_name} metric is not compatible with {media_name}. "
                    f"Please use Duration metric or choose a different media type.",
                    ephemeral=True
                )
                return
        
        self.metric = selected
        
        # Update default in options
        for option in select.options:
            option.default = (option.value == selected)
        
        await self._update_view(interaction, self.current_page)
    
    async def _get_stats_with_metric(self) -> Dict[str, Any]:
        """Get stats calculated based on the selected metric"""
        logger.info(f"Metric: {self.metric}")
        
        # For duration, use the standard stats
        if self.metric == "duration":
            logger.info(f"Using standard duration stats")
            result = await self.cog.activity_repo.get_user_stats(
                self.user_id, None, self.current_days,
                media_type=self.media_type,
                activity_type=self.activity_type
            )
            return result
        
        # For characters/pages/episodes, calculate from activities
        logger.info(f"Calculating {self.metric} from individual activities")
        from collections import defaultdict
        # Use end of today in UTC so all of today's activities are included
        end_date = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=999999)
        
        # For "All Time", find the user's first activity
        if self.current_days >= 9999:
            first_activity = await self.cog.activity_repo.get_first_activity_date(
                user_id=self.user_id,
                media_type=self.media_type,
                activity_type=self.activity_type
            )
            if first_activity:
                start_date = first_activity
            else:
                start_date = end_date - timedelta(days=365)
        else:
            start_date = end_date - timedelta(days=self.current_days)
        
        activities = await self.cog.activity_repo.get_activities_in_range(
            user_id=self.user_id,
            guild_id=None,
            start_date=start_date,
            end_date=end_date
        )
        
        # Apply filters
        if self.media_type:
            # Handle 'manual' as a special filter for NULL media_type
            if self.media_type == 'manual':
                activities = [a for a in activities if a.get('media_type') is None]
            else:
                activities = [a for a in activities if a.get('media_type') == self.media_type]
        if self.activity_type:
            activities = [a for a in activities if a.get('primary_type') == self.activity_type]
        
        # Calculate metric-based stats
        total_value = 0
        reading_value = 0
        listening_value = 0
        activities_with_metric = 0  # Count only activities that have this metric
        media_breakdown = defaultdict(lambda: {'count': 0, 'value': 0})
        daily_breakdown = defaultdict(lambda: {'activity_count': 0, 'value': 0})
        
        # Create entries for all days in range to ensure consistent time period display
        current_date = start_date.date()
        end_date_only = end_date.date()
        while current_date <= end_date_only:
            date_str = current_date.strftime('%Y-%m-%d')
            daily_breakdown[date_str]  # Initialize entry
            current_date += timedelta(days=1)
        
        for activity in activities:
            meta = activity.get('meta', {})
            value = meta.get(self.metric, 0) if isinstance(meta.get(self.metric), (int, float)) else 0
            
            if value > 0:
                total_value += value
                activities_with_metric += 1  # Count this activity
                
                # Activity type breakdown
                if activity.get('primary_type') == 'reading':
                    reading_value += value
                elif activity.get('primary_type') == 'listening':
                    listening_value += value
                
                # Media breakdown
                media_type = activity.get('media_type')
                if media_type:
                    media_breakdown[media_type]['count'] += 1
                    media_breakdown[media_type]['value'] += value
                
                # Daily breakdown
                activity_date = activity.get('date')
                if isinstance(activity_date, str):
                    date_obj = datetime.fromisoformat(activity_date)
                else:
                    date_obj = activity_date
                date_str = date_obj.strftime('%Y-%m-%d')
                daily_breakdown[date_str]['activity_count'] += 1
                daily_breakdown[date_str]['value'] += value
        
        logger.info(f"Calculated stats for {self.metric}:")
        logger.info(f"  total_value: {total_value}")
        logger.info(f"  activities_with_metric: {activities_with_metric}")
        logger.info(f"  avg: {total_value / activities_with_metric if activities_with_metric > 0 else 0}")
        logger.info(f"  reading: {reading_value}, listening: {listening_value}")
        logger.info(f"  media_breakdown entries: {len(media_breakdown)}")
        logger.info(f"  daily_breakdown entries: {len(daily_breakdown)}")
        
        # Format stats to match expected structure
        result = {
            'period_days': self.current_days,
            'total_activities': activities_with_metric,  # Only count activities with this metric
            'total_duration': total_value,  # Actually the metric value
            'reading_duration': reading_value,
            'listening_duration': listening_value,
            'avg_duration': total_value / activities_with_metric if activities_with_metric > 0 else 0,
            'daily_breakdown': [
                {
                    'date': date,
                    'activity_count': data['activity_count'],
                    'duration': data['value']
                }
                for date, data in sorted(daily_breakdown.items(), reverse=True)
            ],
            'media_breakdown': [
                {
                    'media_type': media,
                    'count': data['count'],
                    'duration': data['value']  # Actually the metric value
                }
                for media, data in media_breakdown.items()
            ]
        }
        
        return result
    
    async def _update_view(self, interaction: discord.Interaction, page: str):
        """Update the view based on selected page"""
        await interaction.response.defer()
        
        try:
            logger.info(f"Page: {page}")
            logger.info(f"Current settings: days={self.current_days}, media_type={self.media_type}, activity_type={self.activity_type}, metric={self.metric}")
            
            self.current_page = page
            
            # Check for incompatible media/activity type combinations
            if self.media_type and self.activity_type:
                expected_activity = MEDIA_ACTIVITY_COMPATIBILITY.get(self.media_type)
                logger.info(f"Checking media/activity compatibility: expected={expected_activity}, actual={self.activity_type}")
                if expected_activity and expected_activity != self.activity_type:
                    # Incompatible combination detected
                    logger.warning(f"Incompatible media/activity combination")
                    media_label = self.media_type.replace('_', ' ').title()
                    activity_label = self.activity_type.title()
                    expected_label = expected_activity.title()
                    
                    embed = discord.Embed(
                        title="⚠️ Incompatible Filters",
                        description=(
                            f"**{media_label}** activities are tracked as **{expected_label}**, "
                            f"not **{activity_label}**.\n\n"
                            f"This filter combination will not return any results. "
                            f"Please adjust your filters:\n"
                            f"• Change Activity Type to **{expected_label}**, or\n"
                            f"• Choose a different Media Type"
                        ),
                        color=discord.Color.orange()
                    )
                    embed.set_footer(text="💡 Tip: Manga/VN/Book → Reading | Anime/Video → Listening")
                    
                    await interaction.edit_original_response(embed=embed, attachments=[], view=self)
                    return
            
            # Check for incompatible metric/activity type combinations
            if self.metric in ["characters", "pages"] and self.activity_type == "listening":
                metric_label = self.metric.title()
                
                embed = discord.Embed(
                    title="⚠️ Incompatible Filters",
                    description=(
                        f"**{metric_label}** metric is not available for **Listening** activities.\n\n"
                        f"Listening activities track **Duration** and **Episodes** (Anime only). "
                        f"Please adjust your filters:\n"
                        f"• Change Metric to **Duration** or **Episodes**, or\n"
                        f"• Change Activity Type to **Reading**"
                    ),
                    color=discord.Color.orange()
                )
                embed.set_footer(text="💡 Tip: Characters/Pages → Reading | Episodes → Anime")
                
                await interaction.edit_original_response(embed=embed, attachments=[], view=self)
                return
            
            # Check reading metrics with listening media types
            if self.metric in ["characters", "pages"] and self.media_type in ["anime", "video"]:
                metric_label = self.metric.title()
                media_label = self.media_type.replace('_', ' ').title()
                
                embed = discord.Embed(
                    title="⚠️ Incompatible Filters",
                    description=(
                        f"**{metric_label}** metric is not available for **{media_label}**.\n\n"
                        f"Anime tracks **Duration** and **Episodes**. Video tracks **Duration** only. "
                        f"Please adjust your filters:\n"
                        f"• Change Metric to **Duration** (or **Episodes** for Anime), or\n"
                        f"• Choose a reading-based media type (Manga, VN, Book)"
                    ),
                    color=discord.Color.orange()
                )
                embed.set_footer(text="💡 Tip: Characters/Pages → Reading | Episodes → Anime")
                
                await interaction.edit_original_response(embed=embed, attachments=[], view=self)
                return
            
            # Check episodes metric with non-anime types
            if self.metric == "episodes":
                if self.activity_type == "reading":
                    embed = discord.Embed(
                        title="⚠️ Incompatible Filters",
                        description=(
                            f"**Episodes** metric is only available for **Anime**.\n\n"
                            f"Reading activities track **Duration**, **Characters**, and **Pages**. "
                            f"Please adjust your filters:\n"
                            f"• Change Metric to **Duration**, **Characters**, or **Pages**, or\n"
                            f"• Change Activity Type to **Listening** and Media Type to **Anime**"
                        ),
                        color=discord.Color.orange()
                    )
                    embed.set_footer(text="💡 Tip: Episodes metric only works with Anime")
                    
                    await interaction.edit_original_response(embed=embed, attachments=[], view=self)
                    return
                
                if self.media_type and self.media_type != "anime":
                    media_label = self.media_type.replace('_', ' ').title()
                    
                    embed = discord.Embed(
                        title="⚠️ Incompatible Filters",
                        description=(
                            f"**Episodes** metric is only available for **Anime**.\n\n"
                            f"{media_label} does not support episode tracking. "
                            f"Please adjust your filters:\n"
                            f"• Change Media Type to **Anime**, or\n"
                            f"• Choose a different metric (**Duration**, **Characters**, **Pages**)"
                        ),
                        color=discord.Color.orange()
                    )
                    embed.set_footer(text="💡 Tip: Episodes metric only works with Anime")
                    
                    await interaction.edit_original_response(embed=embed, attachments=[], view=self)
                    return
            
            # Update button styles to show active page
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.custom_id in ["stats_overview", "stats_breakdown", "stats_timeline"]:
                    if (page == "overview" and item.custom_id == "stats_overview") or \
                       (page == "breakdown" and item.custom_id == "stats_breakdown") or \
                       (page == "timeline" and item.custom_id == "stats_timeline"):
                        item.style = discord.ButtonStyle.primary
                    else:
                        item.style = discord.ButtonStyle.secondary
            
            # Get stats with metric support
            logger.info(f"Fetching stats with metric={self.metric}...")
            stats = await self._get_stats_with_metric()
            logger.info(f"Stats received: total_activities={stats.get('total_activities')}, total_duration={stats.get('total_duration')}")

            
            # Generate appropriate embed based on page
            logger.info(f"Generating {page} embed...")
            if page == "overview":
                embed, file = await self._generate_overview_embed(stats)
            elif page == "breakdown":
                embed, file = await self._generate_breakdown_embed(stats)
            elif page == "timeline":
                embed, file = await self._generate_timeline_embed(stats)
            else:
                embed, file = await self._generate_overview_embed(stats)
            
            logger.info(f"Generated {page} embed, has file: {file is not None}")
            logger.info(f"Sending updated view to user...")
            
            if file:
                await interaction.edit_original_response(embed=embed, attachments=[file], view=self)
            else:
                await interaction.edit_original_response(embed=embed, attachments=[], view=self)
            
            
        except Exception as e:
            logger.error(f"Error updating stats view: {e}", exc_info=True)
            error_embed = discord.Embed(
                title="❌ Error Loading Stats",
                description="An error occurred while generating the statistics. Please try again.",
                color=discord.Color.red()
            )
            try:
                await interaction.edit_original_response(embed=error_embed, attachments=[], view=self)
            except:
                await interaction.followup.send("An error occurred while updating the view.", ephemeral=True)
    
    async def _generate_overview_embed(self, stats: Dict[str, Any]) -> discord.Embed:
        """Generate the overview embed (same as current log_stats)"""
        total_duration = stats['total_duration']
        total_activities = stats['total_activities']
        reading_duration = stats['reading_duration']
        listening_duration = stats['listening_duration']
        avg_duration_value = stats['avg_duration'] or 0
        
        # Determine units based on metric
        if self.metric == "duration":
            total_value = total_duration / 3600 if total_duration > 0 else 0
            reading_value = reading_duration / 3600 if reading_duration > 0 else 0
            listening_value = listening_duration / 3600 if listening_duration > 0 else 0
            avg_value = avg_duration_value / 60 if avg_duration_value > 0 else 0
            unit_display = "hours"
            unit_short = "hrs"
            avg_unit = "min"
            value_format = ".1f"
            avg_format = ".1f"
        elif self.metric == "characters":
            total_value = total_duration  # Actually character count
            reading_value = reading_duration
            listening_value = listening_duration
            avg_value = avg_duration_value
            unit_display = "characters"
            unit_short = "chars"
            avg_unit = "chars"
            value_format = ",.0f"
            avg_format = ",.0f"
        elif self.metric == "pages":
            total_value = total_duration  # Actually page count
            reading_value = reading_duration
            listening_value = listening_duration
            avg_value = avg_duration_value
            unit_display = "pages"
            unit_short = "pgs"
            avg_unit = "pgs"
            value_format = ",.0f"
            avg_format = ",.1f"
        else:  # episodes
            total_value = total_duration  # Actually episode count
            reading_value = reading_duration
            listening_value = listening_duration
            avg_value = avg_duration_value
            unit_display = "episodes"
            unit_short = "eps"
            avg_unit = "eps"
            value_format = ",.0f"
            avg_format = ",.1f"
        
        # Calculate proper daily average
        daily_breakdown = stats.get('daily_breakdown', [])
        if self.current_days >= 9999 and daily_breakdown:
            # For "All Time", calculate based on actual span from first log to today
            dates_with_activity = [datetime.fromisoformat(d['date']).date() for d in daily_breakdown]
            if dates_with_activity:
                first_log_date = min(dates_with_activity)
                today = datetime.now().date()
                actual_days = (today - first_log_date).days + 1
                avg_per_day = total_value / actual_days if actual_days > 0 else 0
            else:
                avg_per_day = 0
        else:
            # For specific time periods, use the requested days
            avg_per_day = total_value / self.current_days if self.current_days > 0 else 0
        
        # Determine color
        if total_value > 100:
            color = discord.Color.from_rgb(255, 215, 0)
        elif total_value > 50:
            color = discord.Color.from_rgb(192, 192, 192)
        elif total_value > 25:
            color = discord.Color.from_rgb(205, 127, 50)
        else:
            color = discord.Color.from_rgb(88, 101, 242)
        
        # Build description
        days_text = "All Time" if self.current_days >= 9999 else f"{self.current_days}-Day Overview"
        description_parts = [f"**{days_text}**"]
        if self.media_type or self.activity_type or self.metric != "duration":
            filter_parts = []
            if self.media_type:
                filter_parts.append(f"📑 {self.media_type.replace('_', ' ').title()}")
            if self.activity_type:
                filter_parts.append(f"⚡ {self.activity_type.title()}")
            if self.metric != "duration":
                metric_emojis = {"characters": "🔤", "pages": "📄", "episodes": "📺"}
                filter_parts.append(f"{metric_emojis.get(self.metric, '📏')} {self.metric.title()}")
            description_parts.append("│ " + " • ".join(filter_parts))
        
        embed = discord.Embed(
            title="📊 Immersion Statistics",
            description="\n".join(description_parts),
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.set_author(
            name=self.target_user.display_name,
            icon_url=self.target_user.display_avatar.url if self.target_user.display_avatar else None
        )
        
        if self.target_user.display_avatar:
            embed.set_thumbnail(url=self.target_user.display_avatar.url)
        
        # Create progress bars (without redundant percentage display)
        if total_duration > 0:
            reading_pct = (reading_duration / total_duration) * 100
            listening_pct = (listening_duration / total_duration) * 100
            
            # Create simple progress bars (shorter for mobile)
            reading_filled = int((reading_pct / 100) * 10)
            listening_filled = int((listening_pct / 100) * 10)
            reading_bar = "█" * reading_filled + "░" * (10 - reading_filled)
            listening_bar = "█" * listening_filled + "░" * (10 - listening_filled)
        else:
            reading_bar = listening_bar = "░" * 10
        
        # Build main stats with appropriate units
        metric_emoji = {"duration": "⏱️", "characters": "🔤", "pages": "📄", "episodes": "📺"}[self.metric]
        main_stats = f"""
**{metric_emoji} Total {unit_display.title()}**
> **{total_value:{value_format}} {unit_display}** across **{total_activities:,} activities**

**📈 Daily Average**  
> **{avg_per_day:{avg_format}} {unit_short}/day** • {avg_value:{avg_format}} {avg_unit} per log
"""
        embed.add_field(name="", value=main_stats, inline=False)
        
        # Generate cumulative progress chart
        try:
            logger.info(f"Attempting to generate cumulative chart for {self.target_user.display_name}")
            chart_buffer = await self.cog._generate_cumulative_progress_chart(
                user_id=self.user_id,
                days=self.current_days,
                username=self.target_user.display_name,
                media_type=self.media_type,
                activity_type=self.activity_type,
                metric=self.metric
            )
            
            if chart_buffer:
                logger.info("Chart buffer generated successfully")
                # Add timestamp to filename to prevent Discord caching
                timestamp = int(datetime.now().timestamp())
                chart_file = discord.File(chart_buffer, filename=f"cumulative_progress_{timestamp}.png")
                embed.set_image(url=f"attachment://cumulative_progress_{timestamp}.png")
                return embed, chart_file
            else:
                logger.warning("Chart buffer was None")
        except Exception as e:
            logger.error(f"Error generating cumulative chart in overview: {e}", exc_info=True)
        
        return embed, None
    
    async def _generate_breakdown_embed(self, stats: Dict[str, Any]) -> discord.Embed:
        """Generate detailed breakdown by media and activity type"""
        total_duration = stats['total_duration']
        reading_duration = stats['reading_duration']
        listening_duration = stats['listening_duration']
        
        # Determine units based on metric
        if self.metric == "duration":
            total_value = total_duration / 3600 if total_duration > 0 else 0
            reading_value = reading_duration / 3600 if reading_duration > 0 else 0
            listening_value = listening_duration / 3600 if listening_duration > 0 else 0
            unit_display = "hours"
            unit_short = "h"
            value_format = ".1f"
        elif self.metric == "characters":
            total_value = total_duration
            reading_value = reading_duration
            listening_value = listening_duration
            unit_display = "characters"
            unit_short = " chars"
            value_format = ",.0f"
        elif self.metric == "pages":
            total_value = total_duration
            reading_value = reading_duration
            listening_value = listening_duration
            unit_display = "pages"
            unit_short = " pgs"
            value_format = ",.0f"
        else:  # episodes
            total_value = total_duration
            reading_value = reading_duration
            listening_value = listening_duration
            unit_display = "episodes"
            unit_short = " eps"
            value_format = ",.0f"
        
        color = discord.Color.from_rgb(88, 101, 242)
        days_text = "All Time" if self.current_days >= 9999 else f"{self.current_days} Days"
        
        # Build description with filters
        description_parts = [f"**{days_text}** • {total_value:{value_format}} total {unit_display}"]
        if self.media_type or self.activity_type or self.metric != "duration":
            filter_parts = []
            if self.media_type:
                filter_parts.append(f"📑 {self.media_type.replace('_', ' ').title()}")
            if self.activity_type:
                filter_parts.append(f"⚡ {self.activity_type.title()}")
            if self.metric != "duration":
                metric_emojis = {"characters": "🔤", "pages": "📄"}
                filter_parts.append(f"{metric_emojis.get(self.metric, '📏')} {self.metric.title()}")
            description_parts.append(" • ".join(filter_parts))
        
        embed = discord.Embed(
            title="📈 Detailed Breakdown",
            description="\n".join(description_parts),
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.set_author(
            name=self.target_user.display_name,
            icon_url=self.target_user.display_avatar.url if self.target_user.display_avatar else None
        )
        
        if self.target_user.display_avatar:
            embed.set_thumbnail(url=self.target_user.display_avatar.url)
        
        # Reading vs Listening section (enhanced with more detail)
        if total_duration > 0:
            reading_pct = (reading_duration / total_duration) * 100
            listening_pct = (listening_duration / total_duration) * 100
            
            # Create simple progress bars without percentage display (shorter for mobile)
            reading_filled = int((reading_pct / 100) * 10)
            listening_filled = int((listening_pct / 100) * 10)
            reading_bar = "█" * reading_filled + "░" * (10 - reading_filled)
            listening_bar = "█" * listening_filled + "░" * (10 - listening_filled)
            
            # Calculate average per activity
            reading_count = sum(1 for m in stats.get('media_breakdown', []) if m.get('reading_duration', 0) > 0)
            listening_count = sum(1 for m in stats.get('media_breakdown', []) if m.get('listening_duration', 0) > 0)
            total_activities = stats.get('total_activities', 0)
            
            # Get per-activity averages
            reading_avg = reading_value / total_activities if total_activities > 0 else 0
            listening_avg = listening_value / total_activities if total_activities > 0 else 0
        else:
            reading_pct = listening_pct = 0
            reading_bar = listening_bar = "░" * 10
            reading_avg = listening_avg = 0
            total_activities = 0
        
        activity_split = f"""
**📚 Reading vs 🎧 Listening**
> 📚 {reading_bar} **{reading_value:{value_format}}{unit_short}** • {reading_pct:.2f}%
> └ *{reading_avg:{value_format}}{unit_short} avg per activity*

> 🎧 {listening_bar} **{listening_value:{value_format}}{unit_short}** • {listening_pct:.2f}%
> └ *{listening_avg:{value_format}}{unit_short} avg per activity*
"""
        embed.add_field(name="", value=activity_split, inline=False)
        
        # Generate and attach pie chart for media breakdown
        media_breakdown = stats.get('media_breakdown', [])
        pie_chart_file = None
        
        if media_breakdown:
            pie_chart_buffer = await self.cog._generate_media_pie_chart(
                media_breakdown,
                self.target_user.display_name,
                metric=self.metric
            )
            
            if pie_chart_buffer:
                # Add timestamp to filename to prevent Discord caching
                timestamp = int(datetime.now().timestamp())
                pie_chart_file = discord.File(pie_chart_buffer, filename=f"media_breakdown_{timestamp}.png")
                embed.set_image(url=f"attachment://media_breakdown_{timestamp}.png")
        
        # Media breakdown with more detail
        media_breakdown = stats.get('media_breakdown', [])
        if media_breakdown:
            sorted_media = sorted(media_breakdown, key=lambda x: x['duration'], reverse=True)
            
            media_emojis = {
                'visual_novel': '📖', 'manga': '📕', 'anime': '📺',
                'book': '📚', 'video': '🎬', 'game': '🎮', 'manual': '✍️'
            }
            
            for media in sorted_media:
                emoji = media_emojis.get(media['media_type'], '📄')
                display_name = media['media_type'].replace('_', ' ').title()
                media_value = media['duration']
                if self.metric == "duration":
                    media_value = media_value / 3600
                media_count = media['count']
                avg_per_log = media_value / media_count if media_count > 0 else 0
                percentage = (media['duration'] / total_duration * 100) if total_duration > 0 else 0
                
                value = f"""
**{media_value:{value_format}}{unit_short}** total • **{media_count}** logs
└ {avg_per_log:{value_format}}{unit_short} avg/log • {percentage:.2f}% of total
"""
                embed.add_field(name=f"{emoji} {display_name}", value=value, inline=True)
        
        return embed, pie_chart_file
    
    async def _generate_timeline_embed(self, stats: Dict[str, Any]) -> discord.Embed:
        """Generate day-by-day timeline with chart"""
        from utils.charts import generate_activity_chart
        from datetime import timezone as dt_timezone
        
        daily_breakdown = stats.get('daily_breakdown', [])
        
        color = discord.Color.from_rgb(88, 101, 242)
        days_text = "All Time" if self.current_days >= 9999 else f"Last {self.current_days} Days"
        
        # Build description with filters
        description_parts = [f"**{days_text}**"]
        if self.media_type or self.activity_type or self.metric != "duration":
            filter_parts = []
            if self.media_type:
                filter_parts.append(f"📑 {self.media_type.replace('_', ' ').title()}")
            if self.activity_type:
                filter_parts.append(f"⚡ {self.activity_type.title()}")
            if self.metric != "duration":
                metric_emojis = {"characters": "🔤", "pages": "📄"}
                filter_parts.append(f"{metric_emojis.get(self.metric, '📏')} {self.metric.title()}")
            description_parts.append(" • ".join(filter_parts))
        
        embed = discord.Embed(
            title="📅 Activity Timeline",
            description="\n".join(description_parts),
            color=color,
            timestamp=datetime.now(dt_timezone.utc)
        )
        
        embed.set_author(
            name=self.target_user.display_name,
            icon_url=self.target_user.display_avatar.url if self.target_user.display_avatar else None
        )
        
        if self.target_user.display_avatar:
            embed.set_thumbnail(url=self.target_user.display_avatar.url)
        
        chart_file = None
        
        # Determine units based on metric
        if self.metric == "duration":
            unit_display = "min"
            unit_format = ".0f"
            divisor = 60  # seconds to minutes
        elif self.metric == "characters":
            unit_display = "chars"
            unit_format = ",.0f"
            divisor = 1
        elif self.metric == "pages":
            unit_display = "pages"
            unit_format = ".0f"
            divisor = 1
        else:  # episodes
            unit_display = "episodes"
            unit_format = ".0f"
            unit_format = ".0f"
            divisor = 1
        
        if daily_breakdown:
            # Calculate stats from the metric-aware daily breakdown
            try:
                total_value = sum(d['duration'] for d in daily_breakdown) / divisor
                days_with_activity = sum(1 for d in daily_breakdown if d['duration'] > 0)
                
                # Calculate proper daily average
                if self.current_days >= 9999 and daily_breakdown:
                    # For "All Time", calculate based on actual span from first log to today
                    dates_with_activity = [datetime.fromisoformat(d['date']).date() for d in daily_breakdown]
                    if dates_with_activity:
                        first_log_date = min(dates_with_activity)
                        today = datetime.now().date()
                        actual_days = (today - first_log_date).days + 1
                        avg_value = total_value / actual_days if actual_days > 0 else 0
                    else:
                        avg_value = 0
                else:
                    # For specific time periods, use length of daily breakdown
                    avg_value = total_value / len(daily_breakdown) if daily_breakdown else 0
                
                # Find peak day only if there's actual activity
                best_day = max(daily_breakdown, key=lambda x: x['duration']) if daily_breakdown else None
                highest_value = (best_day['duration'] / divisor) if best_day else 0
                
                # Format peak day with year if there's activity
                if best_day and highest_value > 0:
                    # Parse the date to format it nicely (YYYY-MM-DD -> MM-DD-YYYY)
                    try:
                        date_parts = best_day['date'].split('-')
                        if len(date_parts) == 3:
                            highest_day_str = f"{date_parts[1]}-{date_parts[2]}-{date_parts[0]}"  # MM-DD-YYYY
                        else:
                            highest_day_str = best_day['date']
                    except:
                        highest_day_str = best_day['date']
                else:
                    highest_day_str = None
                
                # Generate chart for all metrics
                # Use end of today in UTC so all of today's activities are included
                end_date = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=999999)
                
                # For All Time, use first activity date; otherwise use specified days
                if self.current_days >= 9999:
                    first_activity = await self.cog.activity_repo.get_first_activity_date(
                        self.user_id, 
                        self.media_type, 
                        self.activity_type
                    )
                    if first_activity:
                        start_date = first_activity.replace(hour=0, minute=0, second=0, microsecond=0)
                    else:
                        start_date = end_date - timedelta(days=365)
                else:
                    start_date = end_date - timedelta(days=self.current_days)
                
                activities = await self.cog.activity_repo.get_activities_in_range(
                    user_id=self.user_id,
                    guild_id=None,
                    start_date=start_date,
                    end_date=end_date
                )
                
                # Apply filters before generating chart
                if activities:
                    filtered_activities = activities
                    if self.media_type:
                        # Handle 'manual' as a special filter for NULL media_type
                        if self.media_type == 'manual':
                            filtered_activities = [a for a in filtered_activities if a.get('media_type') is None]
                        else:
                            filtered_activities = [a for a in filtered_activities if a.get('media_type') == self.media_type]
                    if self.activity_type:
                        filtered_activities = [a for a in filtered_activities if a.get('primary_type') == self.activity_type]
                    
                    if filtered_activities:
                        # Pass the metric to generate appropriate chart
                        chart_image, _ = await generate_activity_chart(
                            activities=filtered_activities,
                            start_date=start_date,
                            end_date=end_date,
                            metric=self.metric
                        )
                        
                        # Add timestamp to filename to prevent Discord caching
                        timestamp = int(datetime.now().timestamp())
                        chart_file = discord.File(chart_image, filename=f"timeline_chart_{timestamp}.png")
                        embed.set_image(url=f"attachment://timeline_chart_{timestamp}.png")
                
                # Add statistics
                stats_lines = [
                    f"**{total_value:{unit_format}} {unit_display}** total logged",
                    f"**{avg_value:{unit_format}} {unit_display}** average per active day"
                ]
                
                # Only show peak day if there's activity
                if highest_day_str:
                    stats_lines.append(f"**{highest_value:{unit_format}} {unit_display}** peak day ({highest_day_str})")
                
                stats_lines.append(f"**{days_with_activity}** days with activity")
                
                stats_text = "\n".join(stats_lines)
                embed.add_field(name="📊 Statistics", value=stats_text, inline=False)
                    
            except Exception as e:
                logger.error(f"Error generating timeline stats: {e}")
                embed.add_field(name="Activity", value="*Unable to generate statistics*", inline=False)
        else:
            embed.add_field(name="Activity", value="*No activity data available*", inline=False)
        
        return embed, chart_file
    
    async def on_timeout(self):
        """Disable buttons when view times out"""
        for item in self.children:
            item.disabled = True


class OverviewNavigationView(discord.ui.View):
    """Interactive navigation for guild overview statistics"""

    def __init__(self, cog: 'ImmersionCog', guild_id: str, guild_name: str,
                 timeframe: str, metric: str, interaction_user: discord.User):
        super().__init__(timeout=300.0)  # 5 minutes
        self.cog = cog
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.timeframe = timeframe
        self.metric = metric
        self.interaction_user = interaction_user
        self.current_page = "summary"
        self._update_button_styles()
        self._update_select_defaults()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.interaction_user.id:
            await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True

    def _update_button_styles(self):
        """Update button styles based on current page"""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id == f"page_{self.current_page}":
                    item.style = discord.ButtonStyle.primary
                elif item.custom_id and item.custom_id.startswith("page_"):
                    item.style = discord.ButtonStyle.secondary

    def _update_select_defaults(self):
        """Update select menu defaults based on current values"""
        for item in self.children:
            if not isinstance(item, discord.ui.Select):
                continue

            if item.custom_id == "timeframe_select":
                for option in item.options:
                    option.default = (option.value == self.timeframe)

            elif item.custom_id == "metric_select":
                for option in item.options:
                    option.default = (option.value == self.metric)

    @discord.ui.button(label="", style=discord.ButtonStyle.primary,
                       emoji="📊", custom_id="page_summary", row=0)
    async def summary_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show summary overview (default)"""
        await self._update_view(interaction, "summary")

    @discord.ui.button(label="", style=discord.ButtonStyle.secondary,
                       emoji="🎭", custom_id="page_media", row=0)
    async def media_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show media type breakdown"""
        await self._update_view(interaction, "media")

    @discord.ui.button(label="", style=discord.ButtonStyle.secondary,
                       emoji="📈", custom_id="page_timeline", row=0)
    async def timeline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show detailed timeline"""
        await self._update_view(interaction, "timeline")

    @discord.ui.select(
        placeholder="📆 Select Time Period",
        options=[
            discord.SelectOption(label="Last 7 Days", value="week7", emoji="📅"),
            discord.SelectOption(label="Last 30 Days", value="month30", emoji="📆", default=True),
            discord.SelectOption(label="Last 90 Days", value="month90", emoji="🗓️"),
            discord.SelectOption(label="This Year", value="year", emoji="📅"),
            discord.SelectOption(label="All Time", value="all", emoji="♾️"),
        ],
        custom_id="timeframe_select",
        row=1
    )
    async def timeframe_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        """Change time period"""
        self.timeframe = select.values[0]

        # Update defaults
        for option in select.options:
            option.default = (option.value == self.timeframe)

        await self._update_view(interaction, self.current_page)

    @discord.ui.select(
        placeholder="📏 Select Metric",
        options=[
            discord.SelectOption(label="Duration", value="duration", emoji="⏱️", default=True),
            discord.SelectOption(label="Characters", value="characters", emoji="🔤"),
            discord.SelectOption(label="Pages", value="pages", emoji="📄"),
            discord.SelectOption(label="Episodes", value="episodes", emoji="📺"),
        ],
        custom_id="metric_select",
        row=2
    )
    async def metric_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        """Change metric"""
        self.metric = select.values[0]

        # Update defaults
        for option in select.options:
            option.default = (option.value == self.metric)

        await self._update_view(interaction, self.current_page)

    async def _update_view(self, interaction: discord.Interaction, page: str):
        """Update view to show different page"""
        try:
            await interaction.response.defer()

            self.current_page = page
            self._update_button_styles()

            # Calculate time range
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            start_date = self._calculate_start_date(now)

            # Fetch stats (use self.guild_id for filtering)
            stats = await self.cog.activity_repo.get_guild_overview_stats(
                guild_id=self.guild_id,
                start_date=start_date,
                end_date=now,
                metric=self.metric
            )

            # Generate page based on current page
            if page == "summary":
                embed, chart_file = await self._generate_summary_page(stats, start_date, now)
            elif page == "media":
                embed, chart_file = await self._generate_media_page(stats, start_date, now)
            elif page == "timeline":
                embed, chart_file = await self._generate_timeline_page(stats, start_date, now)
            else:
                embed, chart_file = await self._generate_summary_page(stats, start_date, now)

            # Edit message
            if chart_file:
                await interaction.edit_original_response(embed=embed, attachments=[chart_file], view=self)
            else:
                await interaction.edit_original_response(embed=embed, attachments=[], view=self)

        except Exception as e:
            logger.error(f"Error updating overview view: {e}", exc_info=True)
            error_embed = discord.Embed(
                title="❌ Error",
                description="An error occurred while updating the overview.",
                color=discord.Color.red()
            )
            try:
                await interaction.edit_original_response(embed=error_embed, attachments=[], view=self)
            except:
                await interaction.followup.send("An error occurred.", ephemeral=True)

    def _calculate_start_date(self, now: datetime) -> Optional[datetime]:
        """Calculate start date from timeframe"""
        from datetime import timedelta
        if self.timeframe == "today":
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif self.timeframe == "week7":
            return (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif self.timeframe == "month30":
            return (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif self.timeframe == "month90":
            return (now - timedelta(days=89)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif self.timeframe == "year":
            return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif self.timeframe == "all":
            return None
        # Legacy support
        elif self.timeframe == "week":
            days_since_monday = now.weekday()
            return (now - timedelta(days=days_since_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        elif self.timeframe == "month":
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return None

    def _format_date_range(self, start_date: Optional[datetime], end_date: datetime) -> str:
        """
        Format date range for display in embed descriptions.

        Args:
            start_date: Start of range (None for all-time)
            end_date: End of range (always UTC now)

        Returns:
            Formatted string like "Mon D - Mon D, YYYY UTC"
        """
        if start_date is None:
            return ""  # Will be populated by page-specific logic for all-time

        # Format: "Mon D - Mon D, YYYY UTC" (%-d removes leading zeros)
        # Use %-d on Unix/Mac, %#d on Windows, or use .day directly
        start_day = start_date.day
        end_day = end_date.day
        start_month = start_date.strftime('%b')
        end_month = end_date.strftime('%b')

        # Check if they're in different years
        if start_date.year != end_date.year:
            return f"{start_month} {start_day}, {start_date.year} - {end_month} {end_day}, {end_date.year} UTC"
        else:
            return f"{start_month} {start_day} - {end_month} {end_day}, {end_date.year} UTC"

    def _analyze_activity_patterns(self, stats: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze daily breakdown to extract day-of-week patterns and peak activity"""
        from datetime import datetime
        from collections import defaultdict

        if not stats.get('daily_breakdown'):
            return {
                'day_of_week_stats': {},
                'busiest_day_of_week': None,
                'peak_day': None
            }

        # Initialize day-of-week aggregators
        day_aggregates = defaultdict(lambda: {
            'count': 0,
            'duration': 0,
            'characters': 0,
            'pages': 0,
            'episodes': 0,
            'days_count': 0  # How many actual days contributed
        })

        peak_day = None
        peak_metric_value = 0

        # Process each day in breakdown
        for day_data in stats['daily_breakdown']:
            date_str = day_data['date']
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            day_name = date_obj.strftime('%A')  # Monday, Tuesday, etc.

            # Aggregate by day of week
            day_aggregates[day_name]['count'] += day_data['count']
            day_aggregates[day_name]['duration'] += day_data['duration']
            day_aggregates[day_name]['characters'] += day_data.get('characters', 0)
            day_aggregates[day_name]['pages'] += day_data.get('pages', 0)
            day_aggregates[day_name]['episodes'] += day_data.get('episodes', 0)
            day_aggregates[day_name]['days_count'] += 1

            # Track peak day
            if self.metric == "duration":
                metric_value = day_data['duration']
            elif self.metric == "characters":
                metric_value = day_data.get('characters', 0)
            elif self.metric == "pages":
                metric_value = day_data.get('pages', 0)
            elif self.metric == "episodes":
                metric_value = day_data.get('episodes', 0)
            else:
                metric_value = day_data['duration']

            if metric_value > peak_metric_value:
                peak_metric_value = metric_value
                peak_day = {
                    'date': date_str,
                    'count': day_data['count'],
                    'duration': day_data['duration'],
                    'users': day_data['users'],
                    'metric_value': metric_value
                }

        # Calculate metric values for each day of week
        day_of_week_stats = {}
        for day_name, aggregates in day_aggregates.items():
            if self.metric == "duration":
                metric_value = aggregates['duration']
            elif self.metric == "characters":
                metric_value = aggregates['characters']
            elif self.metric == "pages":
                metric_value = aggregates['pages']
            elif self.metric == "episodes":
                metric_value = aggregates['episodes']
            else:
                metric_value = aggregates['duration']

            day_of_week_stats[day_name] = {
                'count': aggregates['count'],
                'duration': aggregates['duration'],
                'characters': aggregates['characters'],
                'pages': aggregates['pages'],
                'episodes': aggregates['episodes'],
                'metric_value': metric_value,
                'days_count': aggregates['days_count']
            }

        # Find busiest day of week
        busiest_day_of_week = None
        if day_of_week_stats:
            busiest_day_of_week = max(
                day_of_week_stats.keys(),
                key=lambda d: day_of_week_stats[d]['metric_value']
            )

        return {
            'day_of_week_stats': day_of_week_stats,
            'busiest_day_of_week': busiest_day_of_week,
            'peak_day': peak_day
        }

    async def _generate_summary_page(self, stats: Dict[str, Any],
                                     start_date: Optional[datetime],
                                     end_date: datetime) -> Tuple[discord.Embed, Optional[discord.File]]:
        """Generate comprehensive summary page with timeline chart"""
        from utils.charts import ChartGenerator

        # Calculate metrics
        total_logs = stats['total_logs']
        total_duration = stats['total_duration']
        active_users = stats['active_users']
        avg_duration = stats['avg_duration']
        avg_user_duration = stats['avg_user_duration']
        reading_duration = stats['reading_duration']
        listening_duration = stats['listening_duration']

        # Get metric-specific values
        total_characters = stats.get('total_characters', 0)
        total_pages = stats.get('total_pages', 0)
        total_episodes = stats.get('total_episodes', 0)

        # Determine primary metric to display based on self.metric
        metric_value = 0
        metric_label = ""
        metric_unit = ""
        metric_per_user = 0

        if self.metric == "characters":
            metric_value = total_characters
            metric_label = "Characters"
            metric_unit = "characters"
            metric_per_user = (total_characters / active_users) if active_users > 0 else 0
        elif self.metric == "pages":
            metric_value = total_pages
            metric_label = "Pages"
            metric_unit = "pages"
            metric_per_user = (total_pages / active_users) if active_users > 0 else 0
        elif self.metric == "episodes":
            metric_value = total_episodes
            metric_label = "Episodes"
            metric_unit = "episodes"
            metric_per_user = (total_episodes / active_users) if active_users > 0 else 0
        else:  # duration (default)
            metric_value = total_duration / 3600  # Convert to hours
            metric_label = "Time"
            metric_unit = "hours"
            metric_per_user = (avg_user_duration / 3600) if avg_user_duration > 0 else 0

        # Format durations for secondary display
        total_hours = total_duration / 3600
        reading_hours = reading_duration / 3600
        listening_hours = listening_duration / 3600
        avg_hours_per_user = avg_user_duration / 3600
        avg_minutes_per_log = avg_duration / 60

        # Calculate percentages
        reading_pct = (reading_duration / total_duration * 100) if total_duration > 0 else 0
        listening_pct = (listening_duration / total_duration * 100) if total_duration > 0 else 0

        # Get timeframe label
        timeframe_labels = {
            "today": "Today",
            "week7": "Last 7 Days",
            "month30": "Last 30 Days",
            "month90": "Last 90 Days",
            "year": "This Year",
            "all": "All Time",
            # Legacy support
            "week": "This Week",
            "month": "This Month"
        }
        timeframe_label = timeframe_labels.get(self.timeframe, "Selected Period")

        # Format date range for display
        if start_date:
            date_range_str = self._format_date_range(start_date, end_date)
        else:
            # For all-time, get actual date range from data
            if stats['daily_breakdown']:
                first_date_str = stats['daily_breakdown'][0]['date']
                last_date_str = stats['daily_breakdown'][-1]['date']
                first_date_obj = datetime.strptime(first_date_str, '%Y-%m-%d')
                last_date_obj = datetime.strptime(last_date_str, '%Y-%m-%d')
                date_range_str = f"{first_date_obj.strftime('%b %d, %Y')} - {last_date_obj.strftime('%b %d, %Y')} UTC"
            else:
                date_range_str = "No data available"

        # Create embed
        embed = discord.Embed(
            title=f"📊 {self.guild_name} • Immersion Overview",
            description=f"**{timeframe_label}**\n*{date_range_str}*\n\nCommunity Statistics",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )

        # Main stats - show selected metric
        metric_emoji_map = {
            "duration": "⏱️",
            "characters": "🔤",
            "pages": "📄",
            "episodes": "📺"
        }
        metric_emoji = metric_emoji_map.get(self.metric, "⏱️")

        # Format metric value
        if self.metric == "duration":
            metric_display = f"{metric_value:,.1f} {metric_unit}"
            per_user_display = f"{metric_per_user:.1f} hrs/user"
        else:
            metric_display = f"{metric_value:,.0f} {metric_unit}"
            per_user_display = f"{metric_per_user:.0f} {metric_unit}/user"

        main_stats = f"""
**{metric_emoji} Total {metric_label} Logged**
> **{metric_display}** across **{total_logs:,} logs**

**👥 Community Activity**
> **{active_users:,} active users** • **{per_user_display}**
> **{avg_minutes_per_log:.1f} min** average per log
"""
        embed.add_field(name="", value=main_stats, inline=False)

        # Activity split
        activity_split = f"""
**📚 Reading vs 🎧 Listening**
> Reading: **{reading_hours:,.1f} hrs** ({reading_pct:.1f}%)
> Listening: **{listening_hours:,.1f} hrs** ({listening_pct:.1f}%)
"""
        embed.add_field(name="", value=activity_split, inline=False)

        # Top titles
        if stats['top_titles']:
            top_titles_text = ""
            for i, title in enumerate(stats['top_titles'][:5], 1):
                media_emoji = {
                    'visual_novel': '📖',
                    'manga': '📕',
                    'anime': '📺',
                    'book': '📚',
                    'video': '🎬',
                    'game': '🎮',
                    'manual': '✍️'
                }.get(title['media_type'], '📄')
                top_titles_text += f"{i}. {media_emoji} **{title['name']}** ({title['count']} logs)\n"

            embed.add_field(name="🏆 Most Logged Titles", value=top_titles_text, inline=False)

        # Activity Patterns
        patterns = self._analyze_activity_patterns(stats)

        # 📅 Busiest Days (top 3 days of week)
        if patterns['day_of_week_stats']:
            sorted_days = sorted(
                patterns['day_of_week_stats'].items(),
                key=lambda x: x[1]['metric_value'],
                reverse=True
            )[:3]

            busiest_days_text = ""
            for day, day_stats in sorted_days:
                if self.metric == "duration":
                    value_display = f"{day_stats['duration']/3600:.1f} hrs"
                elif self.metric == "characters":
                    value_display = f"{day_stats['metric_value']:,.0f} chars"
                elif self.metric == "pages":
                    value_display = f"{day_stats['metric_value']:,.0f} pages"
                elif self.metric == "episodes":
                    value_display = f"{day_stats['metric_value']:,.0f} eps"
                else:
                    value_display = f"{day_stats['duration']/3600:.1f} hrs"

                busiest_days_text += f"**{day}**: {value_display}\n"

            embed.add_field(name="📅 Busiest Days", value=busiest_days_text, inline=True)

        # ⚡ Peak Activity Day
        if patterns['peak_day']:
            peak = patterns['peak_day']
            peak_hours = peak['duration'] / 3600

            # Format date nicely
            from datetime import datetime as dt
            date_obj = dt.strptime(peak['date'], '%Y-%m-%d')
            formatted_date = date_obj.strftime('%b %d, %Y')  # "Mon DD, YYYY"

            # Format metric-specific display
            if self.metric == "duration":
                metric_display = f"{peak_hours:.1f} hrs"
            elif self.metric == "characters":
                metric_display = f"{peak['metric_value']:,.0f} chars"
            elif self.metric == "pages":
                metric_display = f"{peak['metric_value']:,.0f} pages"
            elif self.metric == "episodes":
                metric_display = f"{peak['metric_value']:,.0f} eps"
            else:
                metric_display = f"{peak_hours:.1f} hrs"

            peak_text = f"""**{formatted_date}**
{metric_display} • {peak['count']} logs
{peak['users']} active users"""

            embed.add_field(name="⚡ Peak Activity Day", value=peak_text, inline=True)

        # Media breakdown summary
        if stats['media_breakdown']:
            most_popular = max(stats['media_breakdown'], key=lambda x: x['count'])
            most_popular_name = most_popular['media_type'].replace('_', ' ').title()
            most_popular_count = most_popular['count']

            media_summary = f"**{most_popular_name}** is most popular\n({most_popular_count:,} logs)"
            embed.add_field(name="🎭 Most Popular Media", value=media_summary, inline=True)

        # New users
        if stats['new_users'] > 0:
            embed.add_field(
                name="🌟 New Loggers",
                value=f"**{stats['new_users']}** new users this period",
                inline=True
            )

        embed.set_footer(text="Use dropdown menus to filter")

        # Generate timeline chart
        chart_file = None
        try:
            if stats['daily_breakdown']:
                # Convert daily breakdown to chart format based on selected metric
                daily_data = {}
                for day in stats['daily_breakdown']:
                    if self.metric == "characters":
                        daily_data[day['date']] = day.get('characters', 0)
                    elif self.metric == "pages":
                        daily_data[day['date']] = day.get('pages', 0)
                    elif self.metric == "episodes":
                        daily_data[day['date']] = day.get('episodes', 0)
                    else:  # duration (default)
                        daily_data[day['date']] = day['duration'] / 60.0  # Convert to minutes

                if daily_data:
                    # If start_date is None (all-time), derive it from the data
                    chart_start_date = start_date
                    if chart_start_date is None and stats['daily_breakdown']:
                        first_date_str = min(day['date'] for day in stats['daily_breakdown'])
                        chart_start_date = datetime.strptime(first_date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)

                    if chart_start_date:
                        async with ChartGenerator() as chart_gen:
                            chart_image = await chart_gen.generate_duration_chart(
                                daily_data, chart_start_date, end_date, None
                            )
                            chart_file = discord.File(chart_image, filename="timeline.png")
                            embed.set_image(url="attachment://timeline.png")
        except Exception as e:
            logger.error(f"Error generating timeline chart: {e}", exc_info=True)

        return embed, chart_file

    async def _generate_media_page(self, stats: Dict[str, Any],
                                   start_date: Optional[datetime],
                                   end_date: datetime) -> Tuple[discord.Embed, Optional[discord.File]]:
        """Generate media breakdown page with pie chart"""
        from utils.charts import ChartGenerator

        # Get timeframe label
        timeframe_labels = {
            "today": "Today",
            "week7": "Last 7 Days",
            "month30": "Last 30 Days",
            "month90": "Last 90 Days",
            "year": "This Year",
            "all": "All Time",
            # Legacy support
            "week": "This Week",
            "month": "This Month"
        }
        timeframe_label = timeframe_labels.get(self.timeframe, "Selected Period")

        # Format date range for display
        if start_date:
            date_range_str = self._format_date_range(start_date, end_date)
        else:
            # For all-time, get actual date range from data
            if stats['daily_breakdown']:
                first_date_str = stats['daily_breakdown'][0]['date']
                last_date_str = stats['daily_breakdown'][-1]['date']
                first_date_obj = datetime.strptime(first_date_str, '%Y-%m-%d')
                last_date_obj = datetime.strptime(last_date_str, '%Y-%m-%d')
                date_range_str = f"{first_date_obj.strftime('%b %d, %Y')} - {last_date_obj.strftime('%b %d, %Y')} UTC"
            else:
                date_range_str = "No data available"

        embed = discord.Embed(
            title=f"🎭 {self.guild_name} • Media Breakdown",
            description=f"**{timeframe_label}**\n*{date_range_str}*\n\nDistribution by Media Type",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )

        # Calculate totals
        total_duration = stats['total_duration']
        total_logs = stats['total_logs']
        reading_duration = stats['reading_duration']
        listening_duration = stats['listening_duration']

        # Reading vs Listening split
        reading_pct = (reading_duration / total_duration * 100) if total_duration > 0 else 0
        listening_pct = (listening_duration / total_duration * 100) if total_duration > 0 else 0

        split_text = f"""
**📚 Reading:** {reading_duration/3600:.1f} hrs ({reading_pct:.1f}%)
**🎧 Listening:** {listening_duration/3600:.1f} hrs ({listening_pct:.1f}%)
"""
        embed.add_field(name="Activity Type Split", value=split_text, inline=False)

        # Media type breakdown
        if stats['media_breakdown']:
            breakdown_text = ""

            # Calculate total for the selected metric
            total_metric_value = 0
            if self.metric == "characters":
                total_metric_value = sum(item.get('characters', 0) for item in stats['media_breakdown'])
            elif self.metric == "pages":
                total_metric_value = sum(item.get('pages', 0) for item in stats['media_breakdown'])
            elif self.metric == "episodes":
                total_metric_value = sum(item.get('episodes', 0) for item in stats['media_breakdown'])
            else:
                total_metric_value = total_duration

            for item in stats['media_breakdown']:
                media_type = item['media_type'].replace('_', ' ').title()
                count = item['count']

                # Get metric value for this media type
                if self.metric == "characters":
                    metric_value = item.get('characters', 0)
                    metric_display = f"{metric_value:,.0f} chars"
                    avg_value = (metric_value / count) if count > 0 else 0
                    avg_display = f"{avg_value:.0f} chars/log"
                elif self.metric == "pages":
                    metric_value = item.get('pages', 0)
                    metric_display = f"{metric_value:,.0f} pages"
                    avg_value = (metric_value / count) if count > 0 else 0
                    avg_display = f"{avg_value:.0f} pages/log"
                elif self.metric == "episodes":
                    metric_value = item.get('episodes', 0)
                    metric_display = f"{metric_value:,.0f} eps"
                    avg_value = (metric_value / count) if count > 0 else 0
                    avg_display = f"{avg_value:.0f} eps/log"
                else:  # duration
                    metric_value = item['duration']
                    duration_hrs = item['duration'] / 3600
                    metric_display = f"{duration_hrs:.1f} hrs"
                    avg_duration = (item['duration'] / count / 60) if count > 0 else 0
                    avg_display = f"{avg_duration:.1f} min/log"

                percentage = (metric_value / total_metric_value * 100) if total_metric_value > 0 else 0

                emoji = {
                    'visual_novel': '📖',
                    'manga': '📕',
                    'anime': '📺',
                    'book': '📚',
                    'video': '🎬',
                    'game': '🎮',
                    'manual': '✍️'
                }.get(item['media_type'], '📄')

                breakdown_text += f"{emoji} **{media_type}**\n"
                breakdown_text += f"> {count:,} logs • {metric_display} ({percentage:.1f}%)\n"
                breakdown_text += f"> Avg: {avg_display}\n\n"

            embed.add_field(name="Media Type Details", value=breakdown_text, inline=False)

        metric_label_map = {
            "duration": "time spent",
            "characters": "characters read",
            "pages": "pages read",
            "episodes": "episodes watched"
        }
        embed.set_footer(text=f"Distribution shows {metric_label_map.get(self.metric, 'time spent')} per media type")

        # Generate pie chart
        chart_file = None
        try:
            if stats['media_breakdown']:
                async with ChartGenerator() as chart_gen:
                    chart_image = await chart_gen.generate_media_distribution_chart(
                        stats['media_breakdown'], metric=self.metric
                    )
                    chart_file = discord.File(chart_image, filename="media_pie.png")
                    embed.set_image(url="attachment://media_pie.png")
        except Exception as e:
            logger.error(f"Error generating pie chart: {e}", exc_info=True)

        return embed, chart_file

    async def _generate_timeline_page(self, stats: Dict[str, Any],
                                     start_date: Optional[datetime],
                                     end_date: datetime) -> Tuple[discord.Embed, Optional[discord.File]]:
        """Generate detailed timeline page"""
        from utils.charts import ChartGenerator

        # Get timeframe label
        timeframe_labels = {
            "today": "Today",
            "week7": "Last 7 Days",
            "month30": "Last 30 Days",
            "month90": "Last 90 Days",
            "year": "This Year",
            "all": "All Time",
            # Legacy support
            "week": "This Week",
            "month": "This Month"
        }
        timeframe_label = timeframe_labels.get(self.timeframe, "Selected Period")

        # Format date range for display
        if start_date:
            date_range_str = self._format_date_range(start_date, end_date)
        else:
            # For all-time, get actual date range from data
            if stats['daily_breakdown']:
                first_date_str = stats['daily_breakdown'][0]['date']
                last_date_str = stats['daily_breakdown'][-1]['date']
                first_date_obj = datetime.strptime(first_date_str, '%Y-%m-%d')
                last_date_obj = datetime.strptime(last_date_str, '%Y-%m-%d')
                date_range_str = f"{first_date_obj.strftime('%b %d, %Y')} - {last_date_obj.strftime('%b %d, %Y')} UTC"
            else:
                date_range_str = "No data available"

        embed = discord.Embed(
            title=f"📈 {self.guild_name} • Activity Timeline",
            description=f"**{timeframe_label}**\n*{date_range_str}*\n\nDaily Breakdown",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )

        # Calculate timeline stats
        if stats['daily_breakdown']:
            daily_breakdown = stats['daily_breakdown']
            # Calculate actual calendar days in range, not just days with activity
            if start_date:
                num_days = (end_date.date() - start_date.date()).days + 1
            else:
                # For all-time, calculate from earliest to latest activity date
                if daily_breakdown:
                    first_date = datetime.strptime(daily_breakdown[0]['date'], '%Y-%m-%d').date()
                    last_date = datetime.strptime(daily_breakdown[-1]['date'], '%Y-%m-%d').date()
                    num_days = (last_date - first_date).days + 1
                else:
                    num_days = 0

            # Calculate totals and averages based on selected metric
            total_duration = sum(day['duration'] for day in daily_breakdown)
            total_logs = sum(day['count'] for day in daily_breakdown)
            avg_logs_per_day = total_logs / num_days if num_days > 0 else 0

            # Extract metric-specific totals and averages
            if self.metric == "characters":
                total_metric = sum(day.get('characters', 0) for day in daily_breakdown)
                avg_metric_per_day = total_metric / num_days if num_days > 0 else 0
                metric_unit = "chars"
                metric_label = "characters"
            elif self.metric == "pages":
                total_metric = sum(day.get('pages', 0) for day in daily_breakdown)
                avg_metric_per_day = total_metric / num_days if num_days > 0 else 0
                metric_unit = "pages"
                metric_label = "pages"
            elif self.metric == "episodes":
                total_metric = sum(day.get('episodes', 0) for day in daily_breakdown)
                avg_metric_per_day = total_metric / num_days if num_days > 0 else 0
                metric_unit = "eps"
                metric_label = "episodes"
            else:  # duration (default)
                total_metric = total_duration / 3600  # Convert to hours
                avg_metric_per_day = (total_duration / num_days) / 3600 if num_days > 0 else 0
                metric_unit = "hrs"
                metric_label = "time"

            # Find most active day by selected metric
            if self.metric == "characters":
                most_active_day = max(daily_breakdown, key=lambda x: x.get('characters', 0))
                most_active_value = most_active_day.get('characters', 0)
            elif self.metric == "pages":
                most_active_day = max(daily_breakdown, key=lambda x: x.get('pages', 0))
                most_active_value = most_active_day.get('pages', 0)
            elif self.metric == "episodes":
                most_active_day = max(daily_breakdown, key=lambda x: x.get('episodes', 0))
                most_active_value = most_active_day.get('episodes', 0)
            else:  # duration
                most_active_day = max(daily_breakdown, key=lambda x: x['duration'])
                most_active_value = most_active_day['duration'] / 3600

            most_active_date = most_active_day['date']

            # Count active days (days with activity)
            active_days = sum(1 for day in daily_breakdown if day['count'] > 0)

            # Format metric display
            if self.metric in ["characters", "pages", "episodes"]:
                avg_display = f"{avg_metric_per_day:,.0f} {metric_unit}/day"
                most_active_display = f"{most_active_value:,.0f} {metric_unit}"
            else:  # duration
                avg_display = f"{avg_metric_per_day:.1f} {metric_unit}/day"
                most_active_display = f"{most_active_value:.1f} {metric_unit}"

            timeline_stats = f"""
**📊 Period Statistics**
> **{num_days}** total days • **{active_days}** with activity
> **{avg_display}** average
> **{avg_logs_per_day:.1f} logs/day** average

**🔥 Most Active Day**
> **{most_active_date}** • {most_active_display}
"""
            embed.add_field(name="", value=timeline_stats, inline=False)

            # Show recent days (last 7)
            recent_days_text = ""
            for day in daily_breakdown[-7:]:
                day_count = day['count']
                day_users = day['users']

                # Get metric-specific value
                if self.metric == "characters":
                    day_value = day.get('characters', 0)
                    value_display = f"{day_value:,.0f} {metric_unit}"
                elif self.metric == "pages":
                    day_value = day.get('pages', 0)
                    value_display = f"{day_value:,.0f} {metric_unit}"
                elif self.metric == "episodes":
                    day_value = day.get('episodes', 0)
                    value_display = f"{day_value:,.0f} {metric_unit}"
                else:  # duration
                    day_value = day['duration'] / 3600
                    value_display = f"{day_value:.1f} {metric_unit}"

                recent_days_text += f"**{day['date']}**\n"
                recent_days_text += f"> {value_display} • {day_count} logs • {day_users} users\n\n"

            if recent_days_text:
                embed.add_field(name="📅 Recent Activity", value=recent_days_text, inline=False)

        embed.set_footer(text="Timeline shows daily activity trends")

        # Generate timeline chart
        chart_file = None
        try:
            if stats['daily_breakdown']:
                daily_data = {}
                for day in stats['daily_breakdown']:
                    if self.metric == "characters":
                        daily_data[day['date']] = day.get('characters', 0)
                    elif self.metric == "pages":
                        daily_data[day['date']] = day.get('pages', 0)
                    elif self.metric == "episodes":
                        daily_data[day['date']] = day.get('episodes', 0)
                    else:  # duration (default)
                        daily_data[day['date']] = day['duration'] / 3600.0  # Convert to hours

                if daily_data:
                    # If start_date is None (all-time), derive it from the data
                    chart_start_date = start_date
                    if chart_start_date is None and stats['daily_breakdown']:
                        first_date_str = min(day['date'] for day in stats['daily_breakdown'])
                        chart_start_date = datetime.strptime(first_date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)

                    if chart_start_date:
                        async with ChartGenerator() as chart_gen:
                            chart_image = await chart_gen.generate_line_chart(
                                daily_data, chart_start_date, end_date, self.metric
                            )
                            chart_file = discord.File(chart_image, filename="timeline.png")
                            embed.set_image(url="attachment://timeline.png")
        except Exception as e:
            logger.error(f"Error generating timeline chart: {e}", exc_info=True)

        return embed, chart_file

    async def on_timeout(self):
        """Disable buttons when view times out"""
        for item in self.children:
            item.disabled = True


class HeatmapNavigationView(discord.ui.View):
    """Interactive view for heatmap navigation with arrow buttons"""
    
    def __init__(self, cog: 'ImmersionCog', target_user: discord.Member,
                 user_id: str, media_type: Optional[str], activity_type: Optional[str],
                 all_time_stats: Dict[str, Any], invoker_id: int,
                 offset_months: int = 0, metric: str = "duration"):
        super().__init__(timeout=180.0)
        self.cog = cog
        self.target_user = target_user
        self.user_id = user_id
        self.invoker_id = invoker_id
        self.metric = metric
        self.media_type = media_type
        self.activity_type = activity_type
        self.all_time_stats = all_time_stats
        self.offset_months = offset_months

        # Disable back button if at present
        if offset_months <= 0:
            self.forward_button.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, custom_id="heatmap_back")
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go back in time (older data)"""
        await self._update_heatmap(interaction, self.offset_months + 3)
    
    @discord.ui.button(emoji="⭕", style=discord.ButtonStyle.primary, custom_id="heatmap_today")
    async def today_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Return to present"""
        await self._update_heatmap(interaction, 0)
    
    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, custom_id="heatmap_forward")
    async def forward_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go forward in time (newer data)"""
        new_offset = max(0, self.offset_months - 3)
        await self._update_heatmap(interaction, new_offset)
    
    async def _update_heatmap(self, interaction: discord.Interaction, new_offset: int):
        """Update the heatmap with a new time offset"""
        try:
            await interaction.response.defer()
            
            # Calculate date range for the new offset
            today = datetime.now().date()
            end_date = today - timedelta(days=new_offset * 30)
            start_date = end_date - timedelta(days=270 - 1)
            
            # Get data for this time period
            display_stats = await self.cog.activity_repo.get_user_stats(
                self.user_id, None, days=270,
                media_type=self.media_type,
                activity_type=self.activity_type
            )
            
            # Filter daily breakdown to only include dates in the requested range
            filtered_breakdown = [
                d for d in display_stats.get('daily_breakdown', [])
                if start_date <= datetime.fromisoformat(d['date']).date() <= end_date
            ]
            
            # Generate new heatmap
            image_buffer = await self.cog._generate_activity_heatmap(
                filtered_breakdown,
                self.target_user.display_name,
                270,
                all_time_stats=self.all_time_stats,
                media_type=self.media_type,
                activity_type=self.activity_type,
                offset_months=new_offset,
                metric=self.metric
            )
            
            if not image_buffer:
                await interaction.followup.send(
                    "Unable to generate heatmap.",
                    ephemeral=True
                )
                return
            
            # Update embed
            title_parts = ["📅 Activity Heatmap"]
            if self.media_type or self.activity_type:
                filter_parts = []
                if self.media_type:
                    filter_parts.append(self.media_type.replace('_', ' ').title())
                if self.activity_type:
                    filter_parts.append(self.activity_type.title())
                title_parts.append(" • ".join(filter_parts))
            
            title = " - ".join(title_parts)
            
            # Determine description based on offset
            if new_offset == 0:
                description = "*Last 9 months of activity*"
            else:
                months_back_start = new_offset + 9
                months_back_end = new_offset
                description = f"*Activity from {months_back_start}-{months_back_end} months ago*"
            
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.from_rgb(88, 101, 242),
                timestamp=datetime.now(timezone.utc)
            )
            
            embed.set_author(
                name=self.target_user.display_name,
                icon_url=self.target_user.display_avatar.url if self.target_user.display_avatar else None
            )
            
            # Attach new image
            file = discord.File(image_buffer, filename="heatmap.png")
            embed.set_image(url="attachment://heatmap.png")
            
            # Update button states
            self.offset_months = new_offset
            self.forward_button.disabled = (new_offset <= 0)
            self.today_button.disabled = (new_offset == 0)
            
            await interaction.edit_original_response(embed=embed, attachments=[file], view=self)
            
        except Exception as e:
            logger.error(f"Error updating heatmap: {e}")
            await interaction.followup.send("Error updating heatmap.", ephemeral=True)
    
    async def on_timeout(self):
        """Disable buttons when view times out"""
        for item in self.children:
            item.disabled = True


async def setup(bot):
    await bot.add_cog(ImmersionCog(bot))