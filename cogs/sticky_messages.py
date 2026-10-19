import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import json
import logging
import os
from typing import Optional, Dict

logger = logging.getLogger(__name__)

class StickyMessagesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data_file = os.path.join('data', 'sticky_messages.json')
        self.sticky_messages = self.load_sticky_messages()
        self._processing_channels = set()  # Track channels currently processing to prevent loops
    
    def load_sticky_messages(self) -> Dict:
        """Load sticky messages from file"""
        try:
            os.makedirs('data', exist_ok=True)
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"Error loading sticky messages: {e}")
            return {}
    
    def save_sticky_messages(self):
        """Save sticky messages to file"""
        try:
            os.makedirs('data', exist_ok=True)
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.sticky_messages, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving sticky messages: {e}")
    
    @app_commands.command(name="manage_sticky", description="[ADMIN] Set, remove, or list sticky messages")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    @app_commands.describe(
        action="What to do.",
        message="(set) The message content to keep sticky at the bottom.",
        channel="Target channel (default: current channel).",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="set", value="set"),
        app_commands.Choice(name="remove", value="remove"),
        app_commands.Choice(name="list", value="list"),
    ])
    async def manage_sticky(self, interaction: discord.Interaction, action: str,
                            message: str = None, channel: discord.TextChannel = None):
        if action == "set":
            if message is None:
                await interaction.response.send_message("Provide a `message` for `set`.", ephemeral=True)
                return
            await self._sticky_set(interaction, message, channel)
        elif action == "remove":
            await self._sticky_remove(interaction, channel)
        else:
            await self._sticky_list(interaction)

    async def _sticky_set(self, interaction: discord.Interaction, message: str, channel: discord.TextChannel = None):
        """Set a sticky message for a channel"""
        try:
            target_channel = channel or interaction.channel
            
            # Verify bot has permissions
            permissions = target_channel.permissions_for(interaction.guild.me)
            if not permissions.send_messages or not permissions.manage_messages:
                await interaction.response.send_message(
                    "❌ I need **Send Messages** and **Manage Messages** permissions in that channel!",
                    ephemeral=True
                )
                return
            
            # Delete old sticky message if it exists
            channel_key = str(target_channel.id)
            if channel_key in self.sticky_messages:
                old_message_id = self.sticky_messages[channel_key].get('message_id')
                if old_message_id:
                    try:
                        old_message = await target_channel.fetch_message(old_message_id)
                        await old_message.delete()
                    except discord.NotFound:
                        pass  # Message already deleted
                    except Exception as e:
                        logger.warning(f"Could not delete old sticky message: {e}")
            
            # Send the sticky message (replace \n with actual newlines)
            formatted_message = message.replace('\\n', '\n')
            sticky_msg = await target_channel.send(formatted_message)
            
            # Store sticky message data
            self.sticky_messages[channel_key] = {
                'message_id': sticky_msg.id,
                'content': formatted_message,
                'guild_id': interaction.guild_id,
                'channel_id': target_channel.id
            }
            self.save_sticky_messages()
            
            await interaction.response.send_message(
                f"✅ Sticky message set in {target_channel.mention}!\n"
                f"It will automatically repost when new messages are sent.",
                ephemeral=True
            )
            
        except discord.app_commands.errors.MissingPermissions:
            await interaction.response.send_message(
                "❌ You need **Manage Messages** permission to use this command!",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to send messages in that channel!",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in /manage_sticky set: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"❌ An error occurred: {str(e)}",
                    ephemeral=True
                )
    
    async def _sticky_remove(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        """Remove a sticky message from a channel"""
        try:
            target_channel = channel or interaction.channel
            channel_key = str(target_channel.id)
            
            if channel_key not in self.sticky_messages:
                await interaction.response.send_message(
                    f"ℹ️ No sticky message is set in {target_channel.mention}",
                    ephemeral=True
                )
                return
            
            # Delete the sticky message
            message_id = self.sticky_messages[channel_key].get('message_id')
            if message_id:
                try:
                    sticky_msg = await target_channel.fetch_message(message_id)
                    await sticky_msg.delete()
                except discord.NotFound:
                    pass  # Already deleted
                except Exception as e:
                    logger.warning(f"Could not delete sticky message: {e}")
            
            # Remove from storage
            del self.sticky_messages[channel_key]
            self.save_sticky_messages()
            
            await interaction.response.send_message(
                f"✅ Sticky message removed from {target_channel.mention}",
                ephemeral=True
            )
            
        except discord.app_commands.errors.MissingPermissions:
            await interaction.response.send_message(
                "❌ You need **Manage Messages** permission to use this command!",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in /manage_sticky remove: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"❌ An error occurred: {str(e)}",
                    ephemeral=True
                )
    
    async def _sticky_list(self, interaction: discord.Interaction):
        """List all sticky messages in the server"""
        try:
            guild_stickies = []
            
            for channel_id, data in self.sticky_messages.items():
                if data['guild_id'] == interaction.guild_id:
                    channel = interaction.guild.get_channel(int(channel_id))
                    if channel:
                        # Truncate content if too long
                        content_preview = data['content'][:50]
                        if len(data['content']) > 50:
                            content_preview += "..."
                        guild_stickies.append(f"• {channel.mention}: `{content_preview}`")
            
            if not guild_stickies:
                await interaction.response.send_message(
                    "ℹ️ No sticky messages are set in this server",
                    ephemeral=True
                )
                return
            
            embed = discord.Embed(
                title="📌 Sticky Messages",
                description="\n".join(guild_stickies),
                color=discord.Color.blue(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text=f"{len(guild_stickies)} sticky message(s)")
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except discord.app_commands.errors.MissingPermissions:
            await interaction.response.send_message(
                "❌ You need **Manage Messages** permission to use this command!",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in /manage_sticky list: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"❌ An error occurred: {str(e)}",
                    ephemeral=True
                )
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handle sticky message reposting on new messages"""
        # Ignore DMs
        if not message.guild:
            return
        
        # Check if this channel has a sticky message
        channel_key = str(message.channel.id)
        if channel_key not in self.sticky_messages:
            return
        
        # Prevent recursive processing
        if channel_key in self._processing_channels:
            return
        
        sticky_data = self.sticky_messages[channel_key]
        
        # Check if this message IS the current sticky message (prevent infinite loop)
        if message.id == sticky_data.get('message_id'):
            return
        
        try:
            # Mark channel as processing
            self._processing_channels.add(channel_key)
            
            # Verify bot still has permissions
            permissions = message.channel.permissions_for(message.guild.me)
            if not permissions.send_messages or not permissions.manage_messages:
                logger.warning(f"Missing permissions for sticky message in channel {message.channel.id}")
                self._processing_channels.discard(channel_key)
                return
            
            # Delete the old sticky message
            old_message_id = sticky_data.get('message_id')
            if old_message_id:
                try:
                    old_sticky = await message.channel.fetch_message(old_message_id)
                    await old_sticky.delete()
                except discord.NotFound:
                    pass  # Message was already deleted
                except discord.Forbidden:
                    logger.warning(f"No permission to delete sticky message in {message.channel.id}")
                    self._processing_channels.discard(channel_key)
                    return
                except Exception as e:
                    logger.warning(f"Error deleting old sticky message: {e}")

            # Small delay to avoid rate limiting other bot operations
            await asyncio.sleep(0.5)

            # Resend the sticky message
            new_sticky = await message.channel.send(sticky_data['content'])
            
            # Update stored message ID
            self.sticky_messages[channel_key]['message_id'] = new_sticky.id
            self.save_sticky_messages()
            
        except discord.Forbidden:
            logger.warning(f"No permission to send sticky message in {message.channel.id}")
        except Exception as e:
            logger.error(f"Error handling sticky message in {message.channel.id}: {e}")
        finally:
            # Always remove from processing set
            self._processing_channels.discard(channel_key)

async def setup(bot):
    await bot.add_cog(StickyMessagesCog(bot))
