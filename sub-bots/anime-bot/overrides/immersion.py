"""
LJTA (Learn Japanese Through Anime) - Immersion Override
Customizes role progression to use anime time instead of VN time.

This override maintains cross-server data tracking while applying
anime-specific roles based on total anime watching time.

OVERRIDES PROVIDED:
✅ _sync_reward_roles() - Uses anime time for role assignments (core override)
✅ profile - Shows anime-focused immersion profile
✅ Helper methods for anime class progress display

INHERITED FROM PARENT (no override needed):
✅ log_anime, log_vn, log_manga, log_book, log_video, log_manual, log_game
✅ All validation (comment length, daily limits, date restrictions)
✅ Undo buttons and error handling
✅ Autocomplete functionality
✅ All other commands and features

The parent ImmersionCog already handles everything correctly - this sub-bot only
needs to customize role progression behavior for the anime server.
"""

import discord
from discord import app_commands
from datetime import datetime, timezone, timedelta
import sys
from pathlib import Path
from typing import Dict, Any, Optional, Union, List
import logging
import asyncio

logger = logging.getLogger(__name__)

# Import parent immersion cog and database
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from cogs.immersion import (
    ImmersionCog as ParentImmersionCog,
    DEFAULT_SPECIAL_RANK_ROLES,
    ANIME_CLASS_ROLE_DEFINITIONS,
    _format_hours_display,
)
from utils.database import Database
from utils.immersion_repository import ActivityRepository, GoalRepository, UserRepository
from utils.immersion_services import UserTimeService, GoalService, GuildRepository
from utils.immersion_media import MediaSearcher

class ImmersionCog(ParentImmersionCog):
    """Extended immersion cog with anime-focused role progression"""
    
    def __init__(self, bot):
        # Don't call super().__init__ yet - the database path must be set up first
        self.bot = bot
        self._display_name_cache: Dict[int, str] = {}
        
        # 🎯 CRITICAL: Use absolute path to parent database for cross-server tracking
        parent_dir = Path(__file__).resolve().parent.parent.parent.parent
        db_path = parent_dir / "data" / "vnbot.db"
        
        logger.info(f"LJTA Immersion Cog - Using shared database at: {db_path}")
        self.db = Database(str(db_path))
        
        # Initialize repositories and services (same as parent)
        self.activity_repo = ActivityRepository(self.db)
        self.user_repo = UserRepository(self.db)
        self.goal_repo = GoalRepository(self.db)
        self.guild_repo = GuildRepository(self.db)
        
        # Initialize services
        self.time_service = UserTimeService(self.user_repo, self.guild_repo)
        self.goal_service = GoalService(self.goal_repo, self.time_service, self.activity_repo)
        self.media_searcher = MediaSearcher()

        # Copy the rest of the parent initialization
        import asyncio

        self._default_class_role_definitions = self._normalize_class_role_definitions(ANIME_CLASS_ROLE_DEFINITIONS)
        self._default_rank_role_definitions = self._normalize_rank_role_definitions(DEFAULT_SPECIAL_RANK_ROLES)
        self.role_sync_lock = asyncio.Lock()
        self._initial_sync_task = None
        
        logger.info("LJTA Immersion Cog initialized - using anime time for role progression")
    
    async def cog_load(self):
        """Initialize the cog"""
        await self.db.initialize()
        logger.info("Immersion tracking cog loaded")

        if not self.class_role_sync.is_running():
            self.class_role_sync.start()

        if not self._initial_sync_task:
            self._initial_sync_task = asyncio.create_task(self._initial_role_sync())
        
        logger.info("Anime immersion cog ready with custom command overrides")
    
    async def _sync_reward_roles(self, guild: discord.Guild, source: str) -> Dict[str, int]:
        """
        Override role sync to use anime time instead of VN time for LJTA.
        
        This maintains the same structure as the parent but filters by anime
        media type instead of visual_novel.
        """
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
                logger.warning("Failed to chunk members for %s during anime role sync: %s", guild.name, exc)

        members = [member for member in guild.members if self._should_sync_member(member)]
        if not members:
            logger.debug("Anime class role sync found no eligible members in %s", guild.name)

        # 🎌 LJTA MODIFICATION: Use anime time instead of VN time
        # Note: Cross-server tracking is maintained - anime time is aggregated across ALL servers
        all_time_durations = await self._get_anime_durations_by_user()
        
        class_assigned, class_removed, class_changes = await self._apply_class_roles(
            guild,
            members,
            role_definitions,
            class_roles,
            all_time_durations,
            source
        )

        # 🎌 LJTA MODIFICATION: Skip special rank roles (天上天下唯我独尊, 魔王, 四天王)
        # These are awarded based on VN time leaderboards and should only be given by the main bot
        rank_assigned = 0
        rank_removed = 0

        return {
            "class_assigned": class_assigned,
            "class_removed": class_removed,
            "rank_assigned": rank_assigned,
            "rank_removed": rank_removed,
            "class_changes": class_changes,
        }
    
    async def _get_anime_durations_by_user(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> Dict[str, int]:
        """
        Get anime watching time (seconds) per user across all servers.
        
        Similar to get_vn_durations_by_user but filters by anime media type.
        Cross-server tracking is intentional - all anime time counts toward roles.
        """
        try:
            import aiosqlite

            clauses = ["media_type = 'anime'", "deleted_at IS NULL"]
            params = []

            if start_date:
                clauses.append("date >= ?")
                params.append(start_date.isoformat())

            if end_date:
                clauses.append("date <= ?")
                params.append(end_date.isoformat())

            where_clause = " AND ".join(clauses)

            sql = [
                "SELECT user_id, SUM(duration) AS total_duration",
                "FROM immersion_activities",
                f"WHERE {where_clause}",
                "GROUP BY user_id",
                "HAVING total_duration > 0",
                "ORDER BY total_duration DESC"
            ]

            if limit and limit > 0:
                sql.append("LIMIT ?")
                params.append(limit)

            query = "\n".join(sql)

            results: Dict[str, int] = {}
            async with aiosqlite.connect(self.db.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(query, params) as cursor:
                    async for row in cursor:
                        user_id = row["user_id"]
                        total_duration = int(row["total_duration"] or 0)
                        results[user_id] = total_duration

            if results:
                blacklisted_ids = await self.db.get_blacklisted_user_ids()
                for blacklisted_id in blacklisted_ids:
                    results.pop(blacklisted_id, None)

            logger.debug(f"Retrieved anime durations for {len(results)} users")
            return results

        except Exception as exc:
            logger.error(f"Error getting anime durations: {exc}", exc_info=True)
            return {}
    
    def _determine_class_progress(
        self,
        total_hours: float,
        class_definitions: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Optional[Any]]:
        """Determine current and next class definitions for a given total."""
        if not class_definitions:
            return {
                "current": None,
                "next": None,
            }

        current_definition = class_definitions[0]
        next_definition: Optional[Dict[str, Any]] = None

        for definition in class_definitions:
            min_hours = float(definition.get("min_hours", 0.0) or 0.0)
            if total_hours >= min_hours:
                current_definition = definition
                continue
            next_definition = definition
            break

        return {
            "current": current_definition,
            "next": next_definition,
        }

    def _build_class_progress_value(
        self,
        total_hours: float,
        current_definition: Optional[Dict[str, Any]],
        next_definition: Optional[Dict[str, Any]],
        media_label: str,
        class_type: str = "anime"
    ) -> Optional[str]:
        """Create a readable class progress block for embeds."""
        if not current_definition:
            return None

        current_name = current_definition.get("name", "Unranked")
        current_min = float(current_definition.get("min_hours", 0.0) or 0.0)

        if next_definition:
            next_name = next_definition.get("name", "")
            next_min = float(next_definition.get("min_hours", 0.0) or 0.0)
            
            # Calculate progress within current tier
            progress_in_tier = total_hours - current_min
            tier_width = next_min - current_min
            progress_ratio = min(max(progress_in_tier / tier_width, 0.0), 1.0) if tier_width > 0 else 1.0
            
            # Shorter progress bar for mobile
            progress_bar = self._create_detailed_progress_bar(progress_ratio, width=12)
            
            # Special message for base tier with no logs
            if total_hours == 0 and current_name == "人間":
                return (
                    f"**Current:** {current_name} ({current_min:.0f}h)\n"
                    f"**Next:** {next_name} ({next_min:.0f}h)\n"
                    f"{progress_bar}\n"
                    f"**Get started:** Log your first session to advance!"
                )
            else:
                # Show progress within current tier explicitly
                hours_to_next = max(next_min - total_hours, 0.0)
                return (
                    f"**Current:** {current_name} ({current_min:.0f}h)\n"
                    f"**Next:** {next_name} ({next_min:.0f}h)\n"
                    f"{progress_bar}\n"
                    f"**In Tier:** {_format_hours_display(progress_in_tier)} / {_format_hours_display(tier_width)}\n"
                    f"**Need:** {_format_hours_display(hours_to_next)} to advance"
                )

        # Max tier reached
        progress_bar = "████████████"
        return (
            f"**Current:** {current_name} ({current_min:.0f}h)\n"
            f"**Status:** ✨ MAX TIER\n"
            f"{progress_bar}\n"
            f"**{media_label} Time:** {_format_hours_display(total_hours)}"
        )

    def _format_class_ranks_field(
        self,
        class_definitions: Optional[List[Dict[str, Any]]],
        current_definition: Optional[Dict[str, Any]],
        next_definition: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Format a compact list of class ranks highlighting current and next."""
        if not class_definitions:
            return None

        current_name = current_definition.get("name") if current_definition else None
        next_name = next_definition.get("name") if next_definition else None

        lines: List[str] = []
        for definition in class_definitions:
            name = definition.get("name", "?")
            min_hours = float(definition.get("min_hours", 0.0) or 0.0)

            if min_hours <= 0:
                threshold_display = "0h"
            elif min_hours < 1:
                threshold_display = "<1h"
            elif min_hours < 100:
                threshold_display = f"{min_hours:.0f}h"
            else:
                threshold_display = f"{min_hours:.0f}h"

            marker = "-"
            display_name = name

            if current_name and name == current_name:
                marker = ">"
                display_name = f"**{name}**"
            elif next_name and name == next_name:
                marker = "*"

            lines.append(f"{marker} {display_name}: {threshold_display}")

        return "\n".join(lines)

    @app_commands.command(name="profile", description="View an immersion profile for yourself or another member")
    @app_commands.describe(user="Member to inspect (defaults to yourself)")
    async def profile(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        """
        Override profile command to show anime-focused stats for LJTA.
        Shows anime time for class progression instead of VN time.
        Removes VN completion stats.
        """
        from utils.immersion_models import MediaType
        from cogs.immersion import _ensure_utc, _format_timespan
        
        guild = interaction.guild
        if not guild:
            if interaction.response.is_done():
                await interaction.followup.send("This command can only be used inside a server.", ephemeral=True)
            else:
                await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        target_member: Optional[discord.Member]
        if user:
            target_member = user
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
        # 🎌 LJTA: Get anime time instead of VN time
        anime_duration_task = asyncio.create_task(
            self.activity_repo.sum_activity_duration(user_id, media_type=MediaType.ANIME.value)
        )
        blacklist_task = asyncio.create_task(self.db.is_user_blacklisted(user_id))
        primary_guild_task = asyncio.create_task(self.activity_repo.get_user_primary_guild(user_id))

        summary, anime_seconds, is_blacklisted, primary_guild = await asyncio.gather(
            summary_task,
            anime_duration_task,
            blacklist_task,
            primary_guild_task
        )

        now = datetime.now(timezone.utc)
        join_date = _ensure_utc(target_member.joined_at)
        time_in_server_text = _format_timespan(now - join_date) if join_date else None

        # LJTA has no quiz levels - use role inference or default to 0
        quiz_level = self._infer_quiz_level_from_roles(target_member)

        class_definitions = self._get_class_role_definitions_for_guild(guild)
        anime_hours = (anime_seconds or 0) / 3600
        if not class_definitions:
            class_definitions = list(self._default_class_role_definitions)

        class_progress = self._determine_class_progress(anime_hours, class_definitions)
        current_definition = class_progress.get("current")
        next_definition = class_progress.get("next")
        current_class = current_definition.get("name", "Unranked") if current_definition else "Unranked"

        summary = summary or {}
        total_logs = summary.get("total_logs", 0) or 0
        total_hours = (summary.get("total_duration", 0) or 0) / 3600
        reading_hours = (summary.get("reading_duration", 0) or 0) / 3600
        listening_hours = (summary.get("listening_duration", 0) or 0) / 3600
        avg_minutes = (summary.get("avg_duration", 0) or 0) / 60

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

        # Build compact description with key info (no level for anime bot)
        desc_lines = [
            f"**{current_class}**",
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
                # First try to get guild info from database cache
                try:
                    cached_guild = await self.db.get_guild_info(int(guild_id_value))
                    if cached_guild:
                        guild_name = cached_guild.get("guild_name") or guild_name
                        guild_icon_url = cached_guild.get("guild_icon_url")
                except Exception:
                    pass
                
                # If not in cache, try direct bot access
                if not guild_name:
                    try:
                        guild_obj = self.bot.get_guild(int(guild_id_value))
                        if guild_obj:
                            guild_name = guild_obj.name
                            guild_icon_url = guild_obj.icon.url if guild_obj.icon else None
                            try:
                                await self.db.sync_guild_info(str(guild_obj.id), guild_name, guild_icon_url)
                            except Exception:
                                pass
                    except (TypeError, ValueError):
                        pass
                
                # Final fallback
                if not guild_name:
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

        # Immersion Stats (consolidated) - anime-focused
        reading_pct = (reading_hours/total_hours*100) if total_hours > 0 else 0
        listening_pct = (listening_hours/total_hours*100) if total_hours > 0 else 0
        
        stats_value = (
            f"📺 **Anime Time:** {anime_hours:.1f}h\n"
            f"📚 **Reading:** {reading_hours:.1f}h ({reading_pct:.0f}%)\n"
            f"🎧 **Listening:** {listening_hours:.1f}h ({listening_pct:.0f}%)\n"
            f"⏱️ **Total:** {total_hours:.1f}h • {total_logs:,} logs\n"
            f"📈 **Avg:** {avg_minutes:.1f}m per log"
        )
        
        embed.add_field(name="📊 Immersion Stats", value=stats_value, inline=False)

        # Class Progress (more compact) - anime-based
        progress_value = self._build_class_progress_value(
            anime_hours,
            current_definition,
            next_definition,
            media_label="Anime",
        )

        embed.add_field(name="⚔️ Anime Class Progress", value=progress_value, inline=False)

        class_ranks_value = self._format_class_ranks_field(class_definitions, current_definition, next_definition)
        if class_ranks_value:
            embed.add_field(name="🏅 Anime Class Ranks", value=class_ranks_value, inline=False)

        # Simplified footer
        embed.set_footer(text=f"Profile • {total_hours:.1f}h logged", icon_url=avatar_url)

        await interaction.followup.send(embed=embed)

    # ==================================================================================
    # The logging commands (/log_anime, /log_vn and the rest) are inherited from the parent
    # ImmersionCog unchanged. Overridden here is only what differs for an anime server:
    # reward roles and class progress count anime time instead of VN time, which changes
    # /profile, class announcements, /manage_class_roles and /log_server_leaderboard.
    # ==================================================================================

    
    def _build_class_change_embed(
        self,
        actor: Union[discord.Member, discord.User],
        change: Dict[str, Any],
        source: str,
        direction: Optional[str] = None,
        class_type: str = "anime"
    ) -> Optional[discord.Embed]:
        """
        Show correct time label for class change embed.
        """
        from discord import Embed, Color
        from cogs.immersion import _format_duration_value
        
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
                f"{trigger_phrase} Keep going!"
            )
            color = Color.brand_green()
        elif direction in {"demotion", "removal"}:
            title = "Class Rank Update"
            encouragement = "Steady immersion will have you climbing again!"
            description = (
                f"Heads up {actor.mention}: your class changed from **{previous_name}** to "
                f"**{new_name}** {trigger_phrase} {encouragement}"
            )
            color = Color.orange()
        else:
            title = "Class Rank Update"
            description = (
                f"{actor.mention}, your class role is now **{new_name}** {trigger_phrase}"
            )
            color = Color.blurple()

        embed = Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="New Class", value=new_name, inline=True)
        label = "Anime Time Logged" if class_type == "anime" else "VN Time Logged"
        embed.add_field(name=label, value=_format_duration_value(total_seconds), inline=True)

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
        """Send class promotions to the configured anime announcement channel."""
        guild = getattr(interaction, "guild", None)
        if guild is None:
            return

        # Reload config from disk to pick up any web console changes
        if hasattr(self.bot, 'reload_config'):
            self.bot.reload_config()

        bot_config = getattr(self.bot, "config", {}) if hasattr(self, "bot") else {}
        guild_config = (bot_config.get("guilds") or {}).get(str(guild.id), {})
        channel_id = (
            guild_config.get("class_announcements_channel")
            or guild_config.get("class_announcement_channel")
        )

        if channel_id:
            channel = guild.get_channel(int(channel_id))
            if channel is not None:
                new_name = change.get("new_role_name") or "their new class"
                
                # Find the minimum hours for this anime class rank
                min_hours = None
                for class_def in self._default_class_role_definitions:
                    if class_def.get("name") == new_name:
                        min_hours = class_def.get("min_hours")
                        break
                
                # Format the message with hours requirement
                if min_hours is not None and min_hours > 0:
                    hours_text = f"{min_hours:,.0f}h" if min_hours == int(min_hours) else f"{min_hours:.1f}h"
                    message = f"⚔️ **Class Promotion** · {actor.mention} advanced to **{new_name}** ({hours_text})!"
                else:
                    message = f"⚔️ **Class Promotion** · {actor.mention} advanced to **{new_name}**!"
                    
                try:
                    await channel.send(message)
                except Exception as exc:
                    logger.warning(
                        "Failed to announce anime class promotion in guild %s: %s",
                        getattr(guild, "id", "unknown"),
                        exc,
                    )
                return

        await super()._announce_class_promotion(interaction, actor, change)

    @app_commands.command(name="manage_class_roles", description="[ADMIN] Force refresh of anime class and ranking roles")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def class_roles_sync(self, interaction: discord.Interaction):
        """Manually trigger anime class role synchronization."""
        try:
            await interaction.response.defer(ephemeral=True)

            if not interaction.guild:
                await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
                return

            stats = await self.sync_reward_roles_for_guild(interaction.guild, source="manual")
            await interaction.followup.send(
                "Anime class roles have been synchronized. "
                f"Assigned: {stats.get('class_assigned', 0)} anime class roles, "
                f"Removed: {stats.get('class_removed', 0)}. "
                f"Ranking roles → assigned: {stats.get('rank_assigned', 0)}, removed: {stats.get('rank_removed', 0)}.",
                ephemeral=True
            )

        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to modify roles.", ephemeral=True)
        except Exception as exc:
            logger.error(f"Error while running class_roles_sync: {exc}")
            await interaction.followup.send("An error occurred while syncing roles.", ephemeral=True)

    @app_commands.command(
        name="manage_class_announcements",
        description="[ADMIN] Choose where anime class promotions are announced"
    )
    @app_commands.describe(channel="Channel for class announcements (leave blank to disable)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def set_class_announcements_channel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ):
        """Update the anime bot config so class promotions post in the selected channel."""
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command must be used inside the server you want to configure.", ephemeral=True)
            return

        if channel and channel.guild != guild:
            await interaction.followup.send("Please pick a channel from this server.", ephemeral=True)
            return

        bot_config = getattr(self.bot, "config", {})
        guilds_config = bot_config.setdefault("guilds", {})
        guild_config = guilds_config.setdefault(str(guild.id), {})

        if channel:
            guild_config["class_announcements_channel"] = channel.id
            response = f"Class announcements will now post in {channel.mention}."
        else:
            guild_config.pop("class_announcements_channel", None)
            response = "Class announcements channel cleared. Falling back to the welcome channel."

        save_config = getattr(self.bot, "save_config", None)
        if callable(save_config):
            try:
                save_config()
            except Exception as exc:
                logger.error("Failed to persist class announcement channel update: %s", exc)
                await interaction.followup.send(
                    "Updated in memory, but failed to write config file. Check logs for details.",
                    ephemeral=True,
                )
                return

        await interaction.followup.send(response, ephemeral=True)

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
                filters = ["deleted_at IS NULL"]
                params = []
                
                if start_date:
                    filters.append("date >= ?")
                    params.append(start_date.isoformat())
                    filters.append("date <= ?")
                    params.append(now.isoformat())
                    
                if media_type:
                    filters.append("media_type = ?")
                    params.append(media_type)

                if activity_type:
                    filters.append("primary_type = ?")
                    params.append(activity_type)
                
                # Exclude blacklisted users
                filters.append("user_id NOT IN (SELECT user_id FROM user_blacklist)")
                
                # Build WHERE clause
                where_clause = " AND ".join(filters)

                async with aiosqlite.connect(self.db.db_path) as db:
                    db.row_factory = aiosqlite.Row

                    query = f"""
                        SELECT 
                            guild_id,
                            COUNT(DISTINCT user_id) AS unique_users,
                            SUM(duration) AS total_duration,
                            COUNT(*) AS activity_count
                        FROM immersion_activities 
                        WHERE {where_clause} AND guild_id IS NOT NULL
                        GROUP BY guild_id 
                        HAVING total_duration > 0
                        ORDER BY total_duration DESC
                    """

                    async with db.execute(query, params) as cursor:
                        rows = await cursor.fetchall()

                # Convert to list of dicts
                server_data = [
                    {
                        "guild_id": row["guild_id"],
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
            from cogs.immersion import _format_duration_value
            
            # Dynamic color for current user's server
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
                guild_name = None
                guild_icon_url = None
                
                try:
                    guild_id_int = int(guild_id)
                    
                    # First try: Get from current bot instance
                    guild_obj = self.bot.get_guild(guild_id_int)
                    if guild_obj:
                        guild_name = guild_obj.name
                        guild_icon_url = guild_obj.icon.url if guild_obj.icon else None
                        # Sync to database for other bot instances
                        await self.db.sync_guild_info(guild_id, guild_name, guild_icon_url)
                    else:
                        # Second try: Get from database cache (synced by other bot instances)
                        cached_info = await self.db.get_guild_info(guild_id)
                        if cached_info:
                            guild_name = cached_info.get("guild_name")
                            guild_icon_url = cached_info.get("guild_icon_url")
                except (ValueError, TypeError):
                    pass
                
                # Fallback: Show shortened ID
                if not guild_name:
                    try:
                        guild_id_short = str(guild_id)[-4:]  # Last 4 digits
                        guild_name = f"Server ***{guild_id_short}"
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
            
            # Add server icon as thumbnail when showing top servers
            if page == 1 and page_data:
                try:
                    top_guild_id = page_data[0]["guild_id"]
                    guild_id_int = int(top_guild_id)
                    
                    # Try to get icon from current bot or cache
                    guild_obj = self.bot.get_guild(guild_id_int)
                    if guild_obj and guild_obj.icon:
                        embed.set_thumbnail(url=guild_obj.icon.url)
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

async def setup(bot):
    await bot.add_cog(ImmersionCog(bot))
