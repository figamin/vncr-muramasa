import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, Dict, Set
import json
import logging
from datetime import datetime, timedelta, timezone
import asyncio
import os

from utils.database import Database
from utils.message_filter import strip_author_marker

logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime, treating naive values as UTC.

    Older files stored naive timestamps (``datetime.now()``); mixing those with
    aware ones raises TypeError when subtracted, so everything is normalised here.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class HighlightsCog(commands.Cog):
    """Highlights feature - sends messages with enough unique reactions to a designated channel"""
    
    HIGHLIGHTED_MESSAGES_FILE = 'data/highlighted_messages.json'
    # Only messages newer than this can be highlighted. The one-time repair scan
    # uses the same window, since older messages are never candidates anyway.
    MESSAGE_MAX_AGE_DAYS = 30
    # Records which guilds already had their repair scan, so a restart does not
    # repeat it. Delete this file (or use /manage_highlights backfill) to redo it.
    BACKFILL_MARKER_FILE = 'data/highlights_backfill.json'
    
    def __init__(self, bot):
        self.bot = bot
        self.db = Database()  # for the repost_authors lookup (highlights the bot's own reposts)
        # Track which messages have already been highlighted to prevent duplicates
        # Format: {message_id: timestamp}
        self.highlighted_messages: Dict[int, datetime] = {}
        # Lock to prevent race conditions when checking/highlighting messages
        self._highlight_locks: Dict[int, asyncio.Lock] = {}
        # Channels we already warned about for missing permissions (avoid log spam
        # on every reaction in a channel the bot cannot read).
        self._permission_warnings: Set[int] = set()
        # One-time backfill task and the lock that keeps scans from overlapping.
        self._backfill_task: Optional[asyncio.Task] = None
        # Manual scans started from /manage_highlights. Kept referenced so they are
        # not garbage-collected while running.
        self._backfill_tasks: Set[asyncio.Task] = set()
        self._backfill_lock = asyncio.Lock()
        # Load previously highlighted messages from file
        self._load_highlighted_messages()

    async def cog_load(self):
        """Warn at startup about conditions that silently disable highlights."""
        intents = getattr(self.bot, 'intents', None)
        if intents is not None:
            if not intents.guild_reactions:
                logger.error(
                    "Highlights: the Guild Reactions intent is disabled; "
                    "reaction events will never be received."
                )
            if not intents.members:
                logger.error(
                    "Highlights: the Server Members intent is disabled; "
                    "the 'Confirmed Reader' checks will fail."
                )
        logger.info(
            "Highlights cog loaded (tracking %d previously highlighted messages)",
            len(self.highlighted_messages),
        )
        # Guilds are only available after READY, hence the wait inside the task.
        # cog_load runs inside the event loop, so create the task there.
        self._backfill_task = asyncio.create_task(
            self._run_backfill_after_ready(),
            name="highlights-backfill",
        )

    async def cog_unload(self):
        if self._backfill_task and not self._backfill_task.done():
            self._backfill_task.cancel()
        for task in list(self._backfill_tasks):
            if not task.done():
                task.cancel()


    def _load_highlighted_messages(self):
        """Load highlighted messages from JSON file"""
        try:
            if os.path.exists(self.HIGHLIGHTED_MESSAGES_FILE):
                with open(self.HIGHLIGHTED_MESSAGES_FILE, 'r') as f:
                    data = json.load(f)
                    # Convert string keys back to integers and string timestamps to datetime
                    self.highlighted_messages = {
                        int(msg_id): _as_utc(datetime.fromisoformat(timestamp))
                        for msg_id, timestamp in data.items()
                    }
                logger.info("Loaded %d previously highlighted messages", len(self.highlighted_messages))
            else:
                logger.info("No previous highlighted messages file found, starting fresh")
        except Exception:
            logger.exception("Error loading highlighted messages from %s", self.HIGHLIGHTED_MESSAGES_FILE)
            self.highlighted_messages = {}
    
    def _save_highlighted_messages(self):
        """Save highlighted messages to JSON file"""
        try:
            # Ensure data directory exists
            os.makedirs(os.path.dirname(self.HIGHLIGHTED_MESSAGES_FILE), exist_ok=True)
            
            # Convert to serializable format
            data = {
                str(msg_id): timestamp.isoformat()
                for msg_id, timestamp in self.highlighted_messages.items()
            }
            
            # Write via a temp file so a crash mid-write can't truncate the history.
            tmp_path = f"{self.HIGHLIGHTED_MESSAGES_FILE}.tmp"
            with open(tmp_path, 'w') as f:
                json.dump(data, f, indent=4)
            os.replace(tmp_path, self.HIGHLIGHTED_MESSAGES_FILE)
        except Exception:
            logger.exception("Error saving highlighted messages to %s", self.HIGHLIGHTED_MESSAGES_FILE)
        
    def _get_message_lock(self, message_id: int) -> asyncio.Lock:
        """Get or create a lock for a specific message"""
        # Locks are created per message and otherwise never removed; prune the
        # unlocked ones occasionally so a long-running process doesn't grow forever.
        if len(self._highlight_locks) > 5000:
            for lock_id in [mid for mid, lock in self._highlight_locks.items() if not lock.locked()][:2500]:
                self._highlight_locks.pop(lock_id, None)
        if message_id not in self._highlight_locks:
            self._highlight_locks[message_id] = asyncio.Lock()
        return self._highlight_locks[message_id]
        
    def _get_guild_config(self, guild_id: int) -> dict:
        """Get guild configuration"""
        guild_id_str = str(guild_id)
        # A config written by hand (or by the sub-bot's loader) may not have a
        # 'guilds' key at all; indexing directly raised KeyError and killed every
        # highlight silently.
        return (self.bot.config.get('guilds') or {}).get(guild_id_str, {})
    
    def _is_highlights_enabled(self, guild_id: int) -> bool:
        """Check if highlights feature is enabled for this guild"""
        config = self._get_guild_config(guild_id)
        return config.get('highlights_enabled', False)
    
    def _get_highlights_channel_id(self, guild_id: int) -> Optional[int]:
        """Get the configured highlights channel ID"""
        config = self._get_guild_config(guild_id)
        channel_id = config.get('highlights_channel')
        # Convert to int if stored as string (to handle JSON precision issues)
        if channel_id is not None:
            try:
                return int(channel_id)
            except (TypeError, ValueError):
                logger.error(
                    "Highlights: invalid highlights_channel value %r for guild %s",
                    channel_id, guild_id,
                )
                return None
        return None
    
    def _get_reaction_threshold(self, guild_id: int) -> int:
        """Get the unique reactor threshold for highlights"""
        config = self._get_guild_config(guild_id)
        try:
            # The web console can hand back numbers as strings; comparing
            # len(...) >= "5" raised TypeError and silently disabled highlights.
            return max(1, int(config.get('highlights_threshold', 5)))
        except (TypeError, ValueError):
            return 5
    
    def _get_confirmed_reader_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        """Return the guild's Confirmed Reader role, if it exists"""
        return discord.utils.get(guild.roles, name="Confirmed Reader")

    def _resolve_configured_channel(self, guild: discord.Guild, channel_id) -> Optional[discord.abc.GuildChannel]:
        """Resolve the configured highlights channel, tolerating a string/invalid id"""
        try:
            return guild.get_channel(int(channel_id))
        except (TypeError, ValueError):
            return None

    def _has_confirmed_reader_role(self, member: discord.Member) -> bool:
        """Check if a member has the Confirmed Reader role"""
        if not member:
            return False
        confirmed_reader_role = self._get_confirmed_reader_role(member.guild)
        if not confirmed_reader_role:
            return False
        return confirmed_reader_role in member.roles

    async def _resolve_member(self, guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
        """Resolve a member from cache, falling back to an API fetch.

        Without the fallback, a cold or incomplete member cache makes every
        author/reactor look like a non-reader and highlights never fire.
        """
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Highlights: could not fetch member %s in guild %s: %s", user_id, guild.id, exc)
            return None
    
    async def _count_unique_reactors(self, message: discord.Message) -> Set[int]:
        """Count unique users who reacted to a message (excluding bots and users without Confirmed Reader)"""
        unique_reactors = set()
        
        for reaction in message.reactions:
            try:
                users = [user async for user in reaction.users()]
            except discord.Forbidden:
                # reaction.users() needs Read Message History in this channel.
                logger.error(
                    "Highlights: missing 'Read Message History' in #%s (guild %s); "
                    "cannot read reactors on message %s",
                    getattr(message.channel, 'name', message.channel.id),
                    message.guild.id,
                    message.id,
                )
                continue
            except discord.HTTPException as exc:
                logger.warning("Highlights: failed to read reactors on message %s: %s", message.id, exc)
                continue

            for user in users:
                # Skip bots
                if user.bot:
                    continue
                
                # Check if user has Confirmed Reader role
                member = await self._resolve_member(message.guild, user.id)
                if member and self._has_confirmed_reader_role(member):
                    unique_reactors.add(user.id)
        
        return unique_reactors
    
    async def _create_highlight_embed(
        self,
        message: discord.Message,
        reactor_count: int
    ) -> discord.Embed:
        """Create an embed for a highlighted message"""
        author = message.author
        
        # Check if the message is from an NSFW channel
        is_nsfw = False
        if hasattr(message.channel, 'nsfw'):
            is_nsfw = message.channel.nsfw
        
        # A channel marked NSFW whose rules require explicit content to be spoilered:
        # unspoilered content there is treated as SFW.
        is_visual_novels_channel = getattr(message.channel, 'name', '').lower() == "vn-chat"
        
        # Create embed with message content
        # Preserve spoiler tags by keeping them in the content
        # Strip the bot's invisible repost author-marker so it never lands (invisibly) in a
        # public highlight embed. No-op for normal (non-repost) messages.
        stripped = strip_author_marker(message.content) if message.content else ""
        content_text = stripped[:4000] if stripped else None
        
        # Check if the original message has embeds (like link previews)
        has_link_embed = len(message.embeds) > 0
        
        embed = discord.Embed(
            description=content_text,
            color=discord.Color.gold(),
            timestamp=message.created_at
        )
        
        # Set author info
        embed.set_author(
            name=f"{author.display_name}",
            icon_url=author.display_avatar.url if author.display_avatar else None
        )
        
        # Check if message has image attachments
        has_image_attachment = False
        if message.attachments:
            first_attachment = message.attachments[0]
            if first_attachment.content_type and first_attachment.content_type.startswith('image'):
                has_image_attachment = True
        
        # If the original message has a link embed, copy its preview info (but not for NSFW channels)
        # Exception: the spoiler-ruled channel allows non-spoilered content
        # Priority: Image attachments take precedence over link preview images
        should_show_preview = has_link_embed and (not is_nsfw or is_visual_novels_channel)
        
        if should_show_preview:
            original_embed = message.embeds[0]
            
            # Add link preview info as a field if available
            # Discord typically shows title for most links, but descriptions mainly for articles
            if original_embed.title or original_embed.description:
                preview_text = ""
                if original_embed.title:
                    # Include the URL if available
                    if original_embed.url:
                        preview_text += f"**[{original_embed.title}]({original_embed.url})**"
                    else:
                        preview_text += f"**{original_embed.title}**"
                
                # Only add description for certain embed types (articles, rich content)
                # Skip for video embeds (like YouTube) and simple link previews
                should_show_description = (
                    original_embed.description and 
                    original_embed.type in ['rich', 'article', 'link'] and
                    original_embed.type != 'video'
                )
                
                if should_show_description:
                    # Truncate description if too long
                    desc = original_embed.description[:150]
                    if len(original_embed.description) > 150:
                        desc += "..."
                    preview_text += f"\n{desc}"
                
                if preview_text:
                    embed.add_field(
                        name="🔗 Link Preview",
                        value=preview_text,
                        inline=False
                    )
            
            # Only use link preview image if there's no image attachment
            if not has_image_attachment:
                # Set image at the bottom (full width) instead of thumbnail (side corner)
                # This prevents text from being squeezed into a narrow column on mobile
                if original_embed.image and original_embed.image.url:
                    embed.set_image(url=original_embed.image.url)
                elif original_embed.thumbnail and original_embed.thumbnail.url:
                    # If no main image, use thumbnail as full image
                    embed.set_image(url=original_embed.thumbnail.url)
        
        # Add first attachment if present
        if message.attachments:
            attachment = message.attachments[0]
            
            # Check if attachment is spoilered
            is_spoiler = attachment.is_spoiler()
            
            if attachment.content_type and attachment.content_type.startswith('image'):
                # For the spoiler-ruled channel: non-spoilered content is treated as SFW
                # For other NSFW channels: hide all images
                should_hide_image = is_nsfw and not is_visual_novels_channel
                
                if should_hide_image:
                    # For content from NSFW channels, provide a manual link without embedding the image
                    embed.add_field(
                        name="📎 Attachment",
                        value=f"[{attachment.filename}]({message.jump_url})",
                        inline=False
                    )
                elif is_spoiler:
                    # For spoilered images (including in the spoiler-ruled channel), don't embed - provide a link to the original message
                    embed.add_field(
                        name="📎 Spoiler Image",
                        value=f"[{attachment.filename}]({message.jump_url})",
                        inline=False
                    )
                else:
                    # Normal image embedding (including non-spoilered content in the spoiler-ruled channel)
                    embed.set_image(url=attachment.url)
            else:
                embed.add_field(
                    name="📎 Attachment",
                    value=f"[{attachment.filename}]({attachment.url})",
                    inline=False
                )
        
        # Build reactions breakdown
        reaction_lines = []
        for reaction in message.reactions:
            # Get the emoji string
            if isinstance(reaction.emoji, str):
                emoji_str = reaction.emoji
            else:
                # Custom emoji
                emoji_str = f"<:{reaction.emoji.name}:{reaction.emoji.id}>"
            
            reaction_lines.append(f"{emoji_str} {reaction.count}")
        
        if reaction_lines:
            # Add reactions field at the bottom
            embed.add_field(
                name="Reactions:",
                value=" • ".join(reaction_lines),
                inline=False
            )
        
        # Add footer with channel and stats
        reactor_text = "unique reactor" if reactor_count == 1 else "unique reactors"
        footer_text = f"#{getattr(message.channel, 'name', 'unknown')} • {reactor_count} {reactor_text}"
        embed.set_footer(text=footer_text)
        
        return embed
    
    @staticmethod
    def _missing_channel_permissions(channel, guild, required) -> list:
        """Return human-readable names of permissions the bot lacks in a channel"""
        perms = channel.permissions_for(guild.me)
        return [label for label, attr in required if not getattr(perms, attr, False)]

    async def _check_and_highlight_message(self, message: discord.Message, ignore_enabled: bool = False):
        """Check if a message should be highlighted and post it if so.

        ``ignore_enabled`` is used by the repair scan: an admin explicitly asked
        for a backfill, even though live highlighting may still be switched off.
        """
        # Use a lock for this specific message to prevent race conditions
        async with self._get_message_lock(message.id):
            try:
                guild = message.guild
                if guild is None:
                    return

                # Check if highlights is enabled
                if not ignore_enabled and not self._is_highlights_enabled(guild.id):
                    logger.debug("Highlights: disabled for guild %s", guild.id)
                    return

                # Get highlights channel
                highlights_channel_id = self._get_highlights_channel_id(guild.id)
                if not highlights_channel_id:
                    logger.debug("Highlights: no highlights channel configured for guild %s", guild.id)
                    return

                highlights_channel = guild.get_channel(highlights_channel_id)
                if not highlights_channel:
                    logger.error(
                        "Highlights: configured highlights channel %s does not exist in guild %s",
                        highlights_channel_id, guild.id,
                    )
                    return

                # Check if message was already highlighted
                if message.id in self.highlighted_messages:
                    return

                # Don't highlight messages from the highlights channel itself
                if message.channel.id == highlights_channel_id:
                    return

                # Don't highlight messages from excluded channels
                excluded_channels = ["announcements", "rules-info", "resources"]
                if getattr(message.channel, 'name', '').lower() in excluded_channels:
                    return

                # Don't highlight messages older than MESSAGE_MAX_AGE_DAYS
                message_age = datetime.now(timezone.utc) - message.created_at
                if message_age > timedelta(days=self.MESSAGE_MAX_AGE_DAYS):
                    return

                # Determine the effective poster. The bot's webhook reposts (word filter /
                # media flag) carry the original poster's identity, so a bot/webhook
                # message is eligible only if it is one of those reposts; in that case
                # the gate is the ORIGINAL poster's reader role, not the webhook.
                effective_author_id = message.author.id
                if message.author.bot or message.webhook_id is not None:
                    repost_author_id = await self.db.get_repost_author(message.id)
                    if repost_author_id is None:
                        logger.debug(
                            "Highlights: skipping bot/webhook message %s (not a recorded repost)",
                            message.id,
                        )
                        return  # a bot/webhook message that is not one of the bot's reposts
                    effective_author_id = repost_author_id

                # Check if the (effective) author has the Confirmed Reader role
                author_member = await self._resolve_member(guild, effective_author_id)
                if not author_member or not self._has_confirmed_reader_role(author_member):
                    logger.debug(
                        "Highlights: author %s of message %s is not a Confirmed Reader",
                        effective_author_id, message.id,
                    )
                    return
                
                # Count unique reactors with Confirmed Reader role
                unique_reactors = await self._count_unique_reactors(message)
                threshold = self._get_reaction_threshold(guild.id)
                
                # Check if threshold is met
                if len(unique_reactors) < threshold:
                    logger.debug(
                        "Highlights: message %s has %d/%d confirmed readers",
                        message.id, len(unique_reactors), threshold,
                    )
                    return

                # Fail with a useful log instead of a generic exception when the
                # bot cannot actually post in the configured channel.
                missing = self._missing_channel_permissions(
                    highlights_channel,
                    guild,
                    (("Send Messages", "send_messages"), ("Embed Links", "embed_links")),
                )
                if missing:
                    logger.error(
                        "Highlights: bot is missing %s in #%s (guild %s); message %s was not posted",
                        ", ".join(missing),
                        getattr(highlights_channel, 'name', highlights_channel_id),
                        guild.id,
                        message.id,
                    )
                    return
                
                # Create and send embed with button
                embed = await self._create_highlight_embed(
                    message,
                    len(unique_reactors)
                )
                
                # Create a view with a button linking to the original message
                view = discord.ui.View()
                button = discord.ui.Button(
                    label="Jump to Message",
                    style=discord.ButtonStyle.link,
                    url=message.jump_url,
                    emoji="💬"
                )
                view.add_item(button)
                
                try:
                    await highlights_channel.send(
                        embed=embed,
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    # Do NOT mark it as highlighted: marking before a successful
                    # send meant one failure (or a missing permission) blacklisted
                    # the message forever.
                    logger.exception(
                        "Highlights: failed to post message %s to #%s (guild %s)",
                        message.id,
                        getattr(highlights_channel, 'name', highlights_channel_id),
                        guild.id,
                    )
                    return

                # Mark as highlighted only after the message was actually sent.
                self.highlighted_messages[message.id] = datetime.now(timezone.utc)
                # Save to file immediately to persist across restarts
                self._save_highlighted_messages()
                logger.info(
                    "Highlighted message %s from #%s (guild %s) with %d unique reactors",
                    message.id,
                    getattr(message.channel, 'name', message.channel.id),
                    guild.id,
                    len(unique_reactors),
                )
                    
            except Exception:
                logger.exception("Error checking/highlighting message %s", message.id)
    
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle when a reaction is added to a message"""
        try:
            if payload.guild_id is None:
                return
            # Get the guild
            guild = self.bot.get_guild(payload.guild_id)
            if not guild:
                return
            
            # Get the channel and message
            channel = guild.get_channel(payload.channel_id) or guild.get_thread(payload.channel_id)
            if not channel:
                return

            # fetch_message()/reaction.users() need Read Message History. Without it
            # the handler used to bail out silently, so warn once per channel.
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.read_message_history):
                if payload.channel_id not in self._permission_warnings:
                    self._permission_warnings.add(payload.channel_id)
                    logger.error(
                        "Highlights: bot lacks View Channel / Read Message History in #%s "
                        "(guild %s); reactions there cannot be evaluated",
                        getattr(channel, 'name', payload.channel_id),
                        guild.id,
                    )
                return
            
            try:
                message = await channel.fetch_message(payload.message_id)
            except discord.NotFound:
                return
            except discord.Forbidden:
                logger.warning(
                    "Highlights: forbidden while fetching message %s in channel %s",
                    payload.message_id, payload.channel_id,
                )
                return
            
            # Check if this message should be highlighted
            await self._check_and_highlight_message(message)
            
        except Exception:
            logger.exception("Error in on_raw_reaction_add for message %s", payload.message_id)
    
    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Handle when a reaction is removed from a message"""
        # Highlights stay posted once sent; reaction removals are ignored
        pass

    # ------------------------------------------------------------------
    # One-time repair scan
    # ------------------------------------------------------------------

    @staticmethod
    def _message_id_from_jump_url(url: Optional[str]) -> Optional[int]:
        """Extract the message id from a Discord jump URL."""
        if not url:
            return None
        try:
            return int(url.rstrip('/').rsplit('/', 1)[-1])
        except (TypeError, ValueError):
            return None

    def _load_backfill_state(self) -> dict:
        """Load the marker recording which guilds were already repaired."""
        try:
            if os.path.exists(self.BACKFILL_MARKER_FILE):
                with open(self.BACKFILL_MARKER_FILE, 'r') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault('completed_guilds', {})
                    return data
                logger.error(
                    "Highlights backfill: %s is not a JSON object; ignoring it",
                    self.BACKFILL_MARKER_FILE,
                )
        except Exception:
            logger.exception(
                "Highlights backfill: could not read marker %s", self.BACKFILL_MARKER_FILE
            )
        return {'completed_guilds': {}}

    def _save_backfill_state(self, state: dict):
        """Persist the marker so restarts do not repeat the scan."""
        try:
            os.makedirs(os.path.dirname(self.BACKFILL_MARKER_FILE), exist_ok=True)
            tmp_path = f"{self.BACKFILL_MARKER_FILE}.tmp"
            with open(tmp_path, 'w') as f:
                json.dump(state, f, indent=4)
            os.replace(tmp_path, self.BACKFILL_MARKER_FILE)
        except Exception:
            logger.exception(
                "Highlights backfill: could not write marker %s", self.BACKFILL_MARKER_FILE
            )

    async def _collect_highlighted_message_ids(
        self,
        highlights_channel: discord.abc.GuildChannel,
        cutoff: datetime,
    ) -> Set[int]:
        """Recover the original message ids already posted in the highlights channel.

        Every highlight is posted with a "Jump to Message" link button, so the id
        of the original message is the last segment of that URL.
        """
        found: Set[int] = set()
        if self.bot.user is None:
            return found
        try:
            async for posted in highlights_channel.history(
                limit=None, after=cutoff, oldest_first=True
            ):
                if posted.author.id != self.bot.user.id:
                    continue
                for row in posted.components:
                    for child in getattr(row, 'children', []):
                        message_id = self._message_id_from_jump_url(getattr(child, 'url', None))
                        if message_id is None:
                            continue
                        found.add(message_id)
                        # Treat it as highlighted so it is never posted twice, even
                        # if it was not in the local state file.
                        self.highlighted_messages.setdefault(message_id, _as_utc(posted.created_at))
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Highlights backfill: could not read existing highlights in #%s",
                getattr(highlights_channel, 'name', highlights_channel.id),
            )
        return found

    async def _backfill_guild(self, guild: discord.Guild, ignore_enabled: bool = False) -> dict:
        """Scan a guild's recent messages and post any highlight that was missed."""
        stats = {
            'guild_id': guild.id,
            'channels_scanned': 0,
            'channels_skipped': [],
            'messages_scanned': 0,
            'candidates': 0,
            'posted': 0,
            'error': None,
        }

        highlights_channel_id = self._get_highlights_channel_id(guild.id)
        highlights_channel = (
            guild.get_channel(highlights_channel_id) if highlights_channel_id else None
        )
        if highlights_channel is None:
            stats['error'] = 'highlights channel is not configured or not found'
            logger.error("Highlights backfill: %s (guild %s)", stats['error'], guild.id)
            return stats

        missing = self._missing_channel_permissions(
            highlights_channel,
            guild,
            (
                ("View Channel", "view_channel"),
                ("Read Message History", "read_message_history"),
                ("Send Messages", "send_messages"),
                ("Embed Links", "embed_links"),
            ),
        )
        if missing:
            stats['error'] = f"missing permissions in highlights channel: {', '.join(missing)}"
            logger.error("Highlights backfill: %s (guild %s)", stats['error'], guild.id)
            return stats

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.MESSAGE_MAX_AGE_DAYS)
        threshold = self._get_reaction_threshold(guild.id)

        # Read the highlights channel first: this is both the duplicate guard and
        # the comparison the operator asked for ("compare with existing highlights").
        existing = await self._collect_highlighted_message_ids(highlights_channel, cutoff)
        logger.info(
            "Highlights backfill: found %d existing highlight(s) in #%s (guild %s)",
            len(existing),
            getattr(highlights_channel, 'name', highlights_channel.id),
            guild.id,
        )

        excluded_channels = ["announcements", "rules-info", "resources"]

        # Text channels plus currently active threads; archived threads are left
        # alone so a one-time repair cannot turn into a multi-hour crawl.
        channels = list(guild.text_channels) + list(guild.threads)
        for channel in channels:
            if channel.id == highlights_channel.id:
                continue
            parent = getattr(channel, 'parent', None)
            if getattr(channel, 'name', '').lower() in excluded_channels:
                continue
            if parent is not None and (
                parent.id == highlights_channel.id
                or parent.name.lower() in excluded_channels
            ):
                continue

            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.read_message_history):
                stats['channels_skipped'].append(channel.name)
                logger.warning(
                    "Highlights backfill: skipping #%s (guild %s); "
                    "bot lacks View Channel / Read Message History",
                    channel.name, guild.id,
                )
                continue

            stats['channels_scanned'] += 1
            try:
                async for message in channel.history(
                    limit=None, after=cutoff, oldest_first=True
                ):
                    stats['messages_scanned'] += 1
                    if stats['messages_scanned'] % 1000 == 0:
                        logger.info(
                            "Highlights backfill: %d message(s) scanned so far (guild %s)",
                            stats['messages_scanned'], guild.id,
                        )

                    # Already handled (existing highlight or posted in this pass)
                    if message.id in self.highlighted_messages:
                        continue
                    # Another bot's message can never be highlighted (only this
                    # bot's webhook reposts are eligible).
                    if message.author.bot and message.webhook_id is None:
                        continue
                    # Cheap lower bound: unique reactors can never exceed the total
                    # reaction count, so this skips most messages without API calls.
                    if sum(reaction.count for reaction in message.reactions) < threshold:
                        continue

                    stats['candidates'] += 1
                    await self._check_and_highlight_message(
                        message, ignore_enabled=ignore_enabled
                    )
                    if message.id in self.highlighted_messages:
                        stats['posted'] += 1
            except (discord.Forbidden, discord.HTTPException):
                stats['channels_skipped'].append(channel.name)
                logger.exception(
                    "Highlights backfill: failed reading #%s (guild %s)",
                    channel.name, guild.id,
                )

        # Persist the ids recovered from the highlights channel as well as new ones.
        self._save_highlighted_messages()
        logger.info(
            "Highlights backfill done for guild %s: %d channel(s), %d message(s), "
            "%d candidate(s), %d posted",
            guild.id,
            stats['channels_scanned'],
            stats['messages_scanned'],
            stats['candidates'],
            stats['posted'],
        )
        return stats

    async def _backfill_missed_highlights(
        self,
        force: bool = False,
        guild: Optional[discord.Guild] = None,
    ) -> Optional[list]:
        """Repair highlights that were missed while the bot was not working.

        Runs at most once per guild, tracked in BACKFILL_MARKER_FILE. ``force``
        (used by /manage_highlights backfill) scans the guild again. A manual run
        ignores the ``highlights_enabled`` flag: that flag also gates live posting,
        so it may be the very thing that was broken, and an admin asking for a
        backfill clearly wants the repair. Returns None if a scan is already
        running, otherwise the per-guild result dicts.
        """
        if self._backfill_lock.locked():
            logger.info("Highlights backfill: a scan is already running; skipping")
            return None

        results = []
        async with self._backfill_lock:
            state = self._load_backfill_state()
            completed = state.setdefault('completed_guilds', {})
            targets = [guild] if guild is not None else list(self.bot.guilds)
            manual = force or guild is not None

            for target in targets:
                if target is None:
                    continue

                if not manual:
                    # The automatic startup scan only runs where highlights are
                    # actually in use.
                    if not self._is_highlights_enabled(target.id):
                        logger.debug(
                            "Highlights backfill: feature disabled for guild %s; skipping",
                            target.id,
                        )
                        continue
                    if self._get_highlights_channel_id(target.id) is None:
                        continue

                if not force and str(target.id) in completed:
                    logger.debug(
                        "Highlights backfill: guild %s already repaired, skipping", target.id
                    )
                    continue

                logger.info(
                    "Highlights backfill: scanning guild %s (%s)", target.id, target.name
                )
                stats = await self._backfill_guild(target, ignore_enabled=manual)
                results.append(stats)

                if stats.get('error'):
                    # Leave it unmarked so the next start retries after the
                    # configuration or permissions are fixed.
                    continue

                completed[str(target.id)] = datetime.now(timezone.utc).isoformat()
                self._save_backfill_state(state)

        return results

    async def _run_backfill_after_ready(self):
        """Wrapper that waits for READY before scanning, once per process."""
        try:
            await self.bot.wait_until_ready()
            await self._backfill_missed_highlights()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Highlights backfill: one-time scan failed")

    async def _highlights_backfill(self, interaction: discord.Interaction):
        """Manually re-run the repair scan for this guild.

        The scan can easily outlive Discord's 15-minute interaction token (it reads
        up to MESSAGE_MAX_AGE_DAYS of history), so it runs as a background task.
        The invocation is acknowledged immediately and the summary is delivered when
        the scan finishes, falling back to a normal channel message if the token has
        expired by then.
        """
        await interaction.response.defer(ephemeral=True)

        if self._backfill_lock.locked():
            await self._send_followup_safe(
                interaction,
                "⏳ A backfill scan is already running. Try again when it finishes.",
            )
            return

        await self._send_followup_safe(
            interaction,
            "🧹 Backfill scan started. It reads up to "
            f"{self.MESSAGE_MAX_AGE_DAYS} days of history, so it can take a while. "
            "The summary will be posted when it finishes.",
        )

        task = asyncio.create_task(
            self._run_manual_backfill(interaction),
            name=f"highlights-backfill-{interaction.guild_id}",
        )
        self._backfill_tasks.add(task)
        task.add_done_callback(self._backfill_tasks.discard)

    async def _run_manual_backfill(self, interaction: discord.Interaction):
        """Run the manual scan and deliver its summary, even if the token expired."""
        try:
            results = await self._backfill_missed_highlights(
                force=True, guild=interaction.guild
            )
        except Exception:
            logger.exception("Highlights backfill: manual scan failed")
            results = None

        if results is None:
            embed = discord.Embed(
                title="🧹 Highlight backfill",
                description="Another scan started first; nothing was done.",
                color=discord.Color.orange(),
            )
        elif not results:
            embed = discord.Embed(
                title="🧹 Highlight backfill",
                description="Nothing to scan for this server.",
                color=discord.Color.orange(),
            )
        else:
            stats = results[0]
            if stats.get('error'):
                embed = discord.Embed(
                    title="🧹 Highlight backfill failed",
                    description=f"❌ {stats['error']}",
                    color=discord.Color.red(),
                )
            else:
                embed = self._build_backfill_report(interaction, stats)

        await self._deliver_backfill_report(interaction, embed)

    def _build_backfill_report(self, interaction: discord.Interaction, stats: dict) -> discord.Embed:
        lines = [
            f"Channels scanned: **{stats['channels_scanned']}**",
            f"Messages scanned: **{stats['messages_scanned']}**",
            f"Messages that met the reaction count: **{stats['candidates']}**",
            f"Highlights posted: **{stats['posted']}**",
        ]
        skipped = stats.get('channels_skipped') or []
        if skipped:
            lines.append(
                "Skipped channels (no access): "
                + ", ".join(f"`{name}`" for name in skipped)
            )
        if not self._is_highlights_enabled(interaction.guild.id):
            lines.append(
                "⚠️ **Live highlights are currently disabled.** These were one-time "
                "repair posts; enable live highlighting with "
                "`/manage_highlights setup enabled:true`."
            )
        return discord.Embed(
            title="🧹 Highlight backfill complete",
            description="\n".join(lines),
            color=discord.Color.green(),
        )

    async def _deliver_backfill_report(self, interaction: discord.Interaction, embed: discord.Embed):
        """Deliver the summary without raising if the interaction token has expired."""
        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        except (discord.NotFound, discord.HTTPException) as exc:
            # 50027 "Invalid Webhook Token" means the 15-minute window closed while
            # a long scan was running. That is expected, not a command failure.
            logger.warning(
                "Highlights backfill: interaction token no longer usable (%s); "
                "posting the summary in the channel instead", exc,
            )
        except Exception:
            logger.exception("Highlights backfill: could not deliver the summary")

        channel = interaction.channel
        if channel is not None:
            try:
                await channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except Exception:
                logger.exception("Highlights backfill: could not post the summary in the channel")

        logger.info(
            "Highlights backfill summary (guild %s): %s",
            getattr(interaction, 'guild_id', '?'),
            getattr(embed, 'description', ''),
        )

    @staticmethod
    async def _send_followup_safe(interaction: discord.Interaction, content: str):
        """Best-effort interaction reply; an expired token is logged, not raised."""
        try:
            await interaction.followup.send(content, ephemeral=True)
        except (discord.NotFound, discord.HTTPException) as exc:
            logger.warning("Highlights: could not reply to the interaction: %s", exc)
    
    @app_commands.command(name="manage_highlights", description="[ADMIN] Configure, view, test or repair the highlights feature")
    @app_commands.describe(
        action="What to do.",
        channel="(setup) The channel where highlighted messages will be posted.",
        threshold="(setup) Number of unique reactors needed to highlight a message.",
        enabled="(setup) Enable or disable the highlights feature."
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="setup", value="setup"),
        app_commands.Choice(name="status", value="status"),
        app_commands.Choice(name="test", value="test"),
        app_commands.Choice(name="backfill", value="backfill"),
    ])
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def manage_highlights(
        self,
        interaction: discord.Interaction,
        action: str,
        channel: Optional[discord.TextChannel] = None,
        threshold: Optional[int] = None,
        enabled: Optional[bool] = None
    ):
        if action == "status":
            await self._highlights_status(interaction)
        elif action == "test":
            await self._highlights_test(interaction)
        elif action == "backfill":
            await self._highlights_backfill(interaction)
        else:
            await self._highlights_setup(interaction, channel, threshold, enabled)

    async def _highlights_test(self, interaction: discord.Interaction):
        """Run a self-check and post a sample highlight to the configured channel."""
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        lines = []

        intents = self.bot.intents
        lines.append(
            f"{'✅' if intents.guild_reactions else '❌'} Guild Reactions intent "
            f"(required for highlight detection)"
        )
        lines.append(
            f"{'✅' if intents.members else '❌'} Server Members intent "
            f"(required for Confirmed Reader checks)"
        )

        enabled = self._is_highlights_enabled(guild.id)
        lines.append(f"{'✅' if enabled else '❌'} Feature enabled")

        role = self._get_confirmed_reader_role(guild)
        if role:
            lines.append(f"✅ Role 'Confirmed Reader' exists ({len(role.members)} members)")
        else:
            lines.append("❌ Role 'Confirmed Reader' does not exist")

        threshold = self._get_reaction_threshold(guild.id)
        lines.append(f"ℹ️ Threshold: {threshold} unique reactors")

        channel_id = self._get_highlights_channel_id(guild.id)
        highlights_channel = guild.get_channel(channel_id) if channel_id else None
        if not channel_id:
            lines.append("❌ No highlights channel configured")
        elif not highlights_channel:
            lines.append(f"❌ Highlights channel {channel_id} not found in this guild")
        else:
            missing = self._missing_channel_permissions(
                highlights_channel,
                guild,
                (
                    ("View Channel", "view_channel"),
                    ("Send Messages", "send_messages"),
                    ("Embed Links", "embed_links"),
                ),
            )
            if missing:
                lines.append(
                    f"❌ Missing permissions in {highlights_channel.mention}: {', '.join(missing)}"
                )
            else:
                lines.append(f"✅ Bot can post in {highlights_channel.mention}")
                try:
                    sample = discord.Embed(
                        title="🧪 Highlights test",
                        description="If you can see this, the bot can post highlights here.",
                        color=discord.Color.gold(),
                    )
                    sample.set_footer(text=f"Requested by {interaction.user.display_name}")
                    await highlights_channel.send(
                        embed=sample,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    lines.append("✅ Test embed sent")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    lines.append(f"❌ Test embed failed: {exc}")

        # Reading reactors needs history access wherever people react, not just in
        # the highlights channel. Report it for the channel the command was used in.
        if interaction.channel is not None and hasattr(interaction.channel, 'permissions_for'):
            perms = interaction.channel.permissions_for(guild.me)
            ok = perms.view_channel and perms.read_message_history
            lines.append(
                f"{'✅' if ok else '❌'} Bot can read message history in {interaction.channel.mention}"
            )

        embed = discord.Embed(
            title="🧪 Highlights self-check",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="ℹ️ Reminder",
            value="The message author **and** at least "
                  f"{threshold} unique reactors must have the **Confirmed Reader** role. "
                  f"Messages older than {self.MESSAGE_MAX_AGE_DAYS} days are ignored.",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _highlights_setup(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        threshold: Optional[int] = None,
        enabled: Optional[bool] = None
    ):
        """Configure highlights settings for the server"""
        guild_id_str = str(interaction.guild.id)
        
        # Initialize guild config if it doesn't exist
        self.bot.config.setdefault('guilds', {})
        if guild_id_str not in self.bot.config['guilds']:
            self.bot.config['guilds'][guild_id_str] = {}
        
        guild_config = self.bot.config['guilds'][guild_id_str]
        changes = []
        
        # Update channel if provided
        if channel is not None:
            guild_config['highlights_channel'] = str(channel.id)
            changes.append(f"Highlights channel set to {channel.mention}")
        
        # Update threshold if provided
        if threshold is not None:
            if threshold < 1:
                await interaction.response.send_message("❌ Threshold must be at least 1.", ephemeral=True)
                return
            guild_config['highlights_threshold'] = threshold
            changes.append(f"Reaction threshold set to {threshold} unique reactors")
        
        # Update enabled status if provided
        if enabled is not None:
            guild_config['highlights_enabled'] = enabled
            status_text = "enabled" if enabled else "disabled"
            changes.append(f"Highlights feature {status_text}")
        
        # Save configuration
        self.bot.save_config()
        
        # Create response embed
        if not changes:
            # No changes, show current settings
            embed = discord.Embed(
                title="📋 Current Highlights Settings",
                color=discord.Color.blue()
            )
            
            is_enabled = guild_config.get('highlights_enabled', False)
            embed.add_field(
                name="Status",
                value="✅ Enabled" if is_enabled else "❌ Disabled",
                inline=True
            )
            
            channel_id = guild_config.get('highlights_channel')
            if channel_id:
                channel_obj = self._resolve_configured_channel(interaction.guild, channel_id)
                channel_text = channel_obj.mention if channel_obj else f"Channel ID: {channel_id} (not found)"
            else:
                channel_text = "Not set"

            embed.add_field(
                name="Highlights Channel",
                value=channel_text,
                inline=True
            )

            threshold_value = self._get_reaction_threshold(interaction.guild.id)
            embed.add_field(
                name="Reaction Threshold",
                value=f"{threshold_value} unique reactors",
                inline=True
            )
            
            embed.add_field(
                name="ℹ️ Info",
                value="Only messages from users with the **Confirmed Reader** role can be highlighted, "
                      "and only reactions from users with this role count toward the threshold.",
                inline=False
            )
        else:
            # Show what was changed
            embed = discord.Embed(
                title="✅ Highlights Settings Updated",
                description="\n".join(f"• {change}" for change in changes),
                color=discord.Color.green()
            )
            
            embed.add_field(
                name="ℹ️ Info",
                value="Only messages from users with the **Confirmed Reader** role can be highlighted, "
                      "and only reactions from users with this role count toward the threshold.",
                inline=False
            )
        
        await interaction.response.send_message(embed=embed)
    
    async def _highlights_status(self, interaction: discord.Interaction):
        """Show current highlights configuration"""
        guild_id_str = str(interaction.guild.id)
        guild_config = (self.bot.config.get('guilds') or {}).get(guild_id_str, {})
        
        embed = discord.Embed(
            title="📋 Highlights Configuration",
            color=discord.Color.blue()
        )
        
        # Enabled status
        is_enabled = guild_config.get('highlights_enabled', False)
        embed.add_field(
            name="Status",
            value="✅ Enabled" if is_enabled else "❌ Disabled",
            inline=True
        )
        
        # Channel
        channel_id = guild_config.get('highlights_channel')
        if channel_id:
            channel_obj = self._resolve_configured_channel(interaction.guild, channel_id)
            channel_text = channel_obj.mention if channel_obj else f"Channel ID: {channel_id} (not found)"
        else:
            channel_text = "Not set"

        embed.add_field(
            name="Highlights Channel",
            value=channel_text,
            inline=True
        )
        
        # Threshold
        threshold_value = self._get_reaction_threshold(interaction.guild.id)
        embed.add_field(
            name="Reaction Threshold",
            value=f"{threshold_value} unique reactors",
            inline=True
        )
        
        # Statistics
        now = datetime.now(timezone.utc)
        highlights_today = len([
            ts for ts in self.highlighted_messages.values()
            if (now - _as_utc(ts)).days == 0
        ])
        
        embed.add_field(
            name="📊 Statistics",
            value=f"Highlights today: {highlights_today}\n"
                  f"Total tracked: {len(self.highlighted_messages)}",
            inline=False
        )

        # One-time repair scan status
        completed_at = (self._load_backfill_state().get('completed_guilds') or {}).get(guild_id_str)
        embed.add_field(
            name="🧹 Backfill",
            value=(
                f"✅ Completed {completed_at}"
                if completed_at
                else "⏳ Not run yet (runs once at startup, or `/manage_highlights backfill`)"
            ),
            inline=False
        )
        
        embed.add_field(
            name="ℹ️ Requirements",
            value="• Message author must have **Confirmed Reader** role\n"
                  "• Only reactions from users with **Confirmed Reader** role count\n"
                  f"• Message needs {threshold_value} unique reactor{'s' if threshold_value != 1 else ''}\n"
                  f"• Message must be less than **{self.MESSAGE_MAX_AGE_DAYS} days old**\n"
                  "• Excluded channels: `announcements`, `rules-info`, `resources`",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(HighlightsCog(bot))
