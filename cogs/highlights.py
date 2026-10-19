import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, Dict, Set
import json
from datetime import datetime
import asyncio
import os

from utils.database import Database
from utils.message_filter import strip_author_marker


class HighlightsCog(commands.Cog):
    """Highlights feature - sends messages with enough unique reactions to a designated channel"""
    
    HIGHLIGHTED_MESSAGES_FILE = 'data/highlighted_messages.json'
    
    def __init__(self, bot):
        self.bot = bot
        self.db = Database()  # for the repost_authors lookup (highlights the bot's own reposts)
        # Track which messages have already been highlighted to prevent duplicates
        # Format: {message_id: timestamp}
        self.highlighted_messages: Dict[int, datetime] = {}
        # Lock to prevent race conditions when checking/highlighting messages
        self._highlight_locks: Dict[int, asyncio.Lock] = {}
        # Load previously highlighted messages from file
        self._load_highlighted_messages()
        
    def _load_highlighted_messages(self):
        """Load highlighted messages from JSON file"""
        try:
            if os.path.exists(self.HIGHLIGHTED_MESSAGES_FILE):
                with open(self.HIGHLIGHTED_MESSAGES_FILE, 'r') as f:
                    data = json.load(f)
                    # Convert string keys back to integers and string timestamps to datetime
                    self.highlighted_messages = {
                        int(msg_id): datetime.fromisoformat(timestamp)
                        for msg_id, timestamp in data.items()
                    }
                print(f"Loaded {len(self.highlighted_messages)} previously highlighted messages")
            else:
                print("No previous highlighted messages file found, starting fresh")
        except Exception as e:
            print(f"Error loading highlighted messages: {e}")
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
            
            with open(self.HIGHLIGHTED_MESSAGES_FILE, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Error saving highlighted messages: {e}")
        
    def _get_message_lock(self, message_id: int) -> asyncio.Lock:
        """Get or create a lock for a specific message"""
        if message_id not in self._highlight_locks:
            self._highlight_locks[message_id] = asyncio.Lock()
        return self._highlight_locks[message_id]
        
    def _get_guild_config(self, guild_id: int) -> dict:
        """Get guild configuration"""
        guild_id_str = str(guild_id)
        return self.bot.config['guilds'].get(guild_id_str, {})
    
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
            return int(channel_id)
        return None
    
    def _get_reaction_threshold(self, guild_id: int) -> int:
        """Get the unique reactor threshold for highlights"""
        config = self._get_guild_config(guild_id)
        return config.get('highlights_threshold', 5)  # Default to 5 unique reactors
    
    def _has_confirmed_reader_role(self, member: discord.Member) -> bool:
        """Check if a member has the Confirmed Reader role"""
        if not member:
            return False
        confirmed_reader_role = discord.utils.get(member.guild.roles, name="Confirmed Reader")
        if not confirmed_reader_role:
            return False
        return confirmed_reader_role in member.roles
    
    async def _count_unique_reactors(self, message: discord.Message) -> Set[int]:
        """Count unique users who reacted to a message (excluding bots and users without Confirmed Reader)"""
        unique_reactors = set()
        
        for reaction in message.reactions:
            async for user in reaction.users():
                # Skip bots
                if user.bot:
                    continue
                
                # Check if user has Confirmed Reader role
                member = message.guild.get_member(user.id)
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
        is_visual_novels_channel = message.channel.name.lower() == "vn-chat"
        
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
        footer_text = f"#{message.channel.name} • {reactor_count} {reactor_text}"
        embed.set_footer(text=footer_text)
        
        return embed
    
    async def _check_and_highlight_message(self, message: discord.Message):
        """Check if a message should be highlighted and post it if so"""
        # Use a lock for this specific message to prevent race conditions
        async with self._get_message_lock(message.id):
            try:
                # Determine the effective poster. The bot's webhook reposts (word filter /
                # media flag) carry the original poster's identity, so a bot/webhook
                # message is eligible only if it is one of those reposts; in that case
                # the gate is the ORIGINAL poster's reader role, not the webhook.
                effective_author_id = message.author.id
                if message.author.bot or message.webhook_id is not None:
                    repost_author_id = await self.db.get_repost_author(message.id)
                    if repost_author_id is None:
                        return  # a bot/webhook message that is not one of the bot's reposts
                    effective_author_id = repost_author_id

                # Check if the (effective) author has the Confirmed Reader role
                author_member = message.guild.get_member(effective_author_id)
                if not author_member or not self._has_confirmed_reader_role(author_member):
                    return
                
                # Check if highlights is enabled
                if not self._is_highlights_enabled(message.guild.id):
                    return
                
                # Get highlights channel
                highlights_channel_id = self._get_highlights_channel_id(message.guild.id)
                if not highlights_channel_id:
                    return
                
                highlights_channel = message.guild.get_channel(highlights_channel_id)
                if not highlights_channel:
                    return
                
                # Check if message was already highlighted
                if message.id in self.highlighted_messages:
                    return
                
                # Don't highlight messages from the highlights channel itself
                if message.channel.id == highlights_channel_id:
                    return
                
                # Don't highlight messages from excluded channels
                excluded_channels = ["announcements", "rules-info", "resources"]
                if message.channel.name.lower() in excluded_channels:
                    return
                
                # Don't highlight messages older than 7 days
                from datetime import datetime, timedelta, timezone
                message_age = datetime.now(timezone.utc) - message.created_at
                if message_age > timedelta(days=7):
                    return
                
                # Count unique reactors with Confirmed Reader role
                unique_reactors = await self._count_unique_reactors(message)
                threshold = self._get_reaction_threshold(message.guild.id)
                
                # Check if threshold is met
                if len(unique_reactors) >= threshold:
                    # Mark as highlighted BEFORE sending to prevent duplicates
                    self.highlighted_messages[message.id] = datetime.now()
                    # Save to file immediately to persist across restarts
                    self._save_highlighted_messages()
                    print(f"Marking message {message.id} as highlighted")
                    
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
                    
                    await highlights_channel.send(embed=embed, view=view)
                    
                    print(f"Successfully highlighted message {message.id} with {len(unique_reactors)} unique reactors")
                    
            except Exception as e:
                print(f"Error checking/highlighting message: {e}")
    
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle when a reaction is added to a message"""
        try:
            # Get the guild
            guild = self.bot.get_guild(payload.guild_id)
            if not guild:
                return
            
            # Get the channel and message
            channel = guild.get_channel(payload.channel_id)
            if not channel:
                return
            
            try:
                message = await channel.fetch_message(payload.message_id)
            except (discord.NotFound, discord.Forbidden):
                return
            
            # Check if this message should be highlighted
            await self._check_and_highlight_message(message)
            
        except Exception as e:
            print(f"Error in on_raw_reaction_add: {e}")
    
    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Handle when a reaction is removed from a message"""
        # Highlights stay posted once sent; reaction removals are ignored
        pass
    
    @app_commands.command(name="manage_highlights", description="[ADMIN] Configure or view the highlights feature")
    @app_commands.describe(
        action="What to do.",
        channel="(setup) The channel where highlighted messages will be posted.",
        threshold="(setup) Number of unique reactors needed to highlight a message.",
        enabled="(setup) Enable or disable the highlights feature."
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="setup", value="setup"),
        app_commands.Choice(name="status", value="status"),
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
        else:
            await self._highlights_setup(interaction, channel, threshold, enabled)

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
                channel_obj = interaction.guild.get_channel(int(channel_id))
                channel_text = channel_obj.mention if channel_obj else f"Channel ID: {channel_id} (not found)"
            else:
                channel_text = "Not set"

            embed.add_field(
                name="Highlights Channel",
                value=channel_text,
                inline=True
            )

            threshold_value = guild_config.get('highlights_threshold', 5)
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
        guild_config = self.bot.config['guilds'].get(guild_id_str, {})
        
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
            channel_obj = interaction.guild.get_channel(int(channel_id))
            channel_text = channel_obj.mention if channel_obj else f"Channel ID: {channel_id} (not found)"
        else:
            channel_text = "Not set"

        embed.add_field(
            name="Highlights Channel",
            value=channel_text,
            inline=True
        )
        
        # Threshold
        threshold_value = guild_config.get('highlights_threshold', 5)
        embed.add_field(
            name="Reaction Threshold",
            value=f"{threshold_value} unique reactors",
            inline=True
        )
        
        # Statistics
        highlights_today = len([ts for ts in self.highlighted_messages.values() 
                               if (datetime.now() - ts).days == 0])
        
        embed.add_field(
            name="📊 Statistics",
            value=f"Highlights today: {highlights_today}\n"
                  f"Total tracked: {len(self.highlighted_messages)}",
            inline=False
        )
        
        embed.add_field(
            name="ℹ️ Requirements",
            value="• Message author must have **Confirmed Reader** role\n"
                  "• Only reactions from users with **Confirmed Reader** role count\n"
                  f"• Message needs {threshold_value} unique reactor{'s' if threshold_value != 1 else ''}\n"
                  "• Message must be less than **7 days old**\n"
                  "• Excluded channels: `announcements`, `rules-info`, `resources`",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(HighlightsCog(bot))
