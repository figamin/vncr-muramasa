"""
Database backup system cog
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from utils.database import Database
from utils.backup_schedule import BACKUP_RUN_TIMES

logger = logging.getLogger(__name__)

class BackupCog(commands.Cog):
    """Database backup system with automatic scheduling"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db = Database()
        
    async def cog_load(self):
        """Initialize database and start backup tasks when cog loads"""
        await self.db.initialize()
        self.auto_backup_task.start()
        
        # Schedule startup backups after bot is ready
        if hasattr(self.bot, 'loop') and self.bot.loop:
            self.bot.loop.create_task(self._delayed_startup_backups())
        
        logger.info("Backup system started")
    
    async def cog_unload(self):
        """Clean up when cog unloads"""
        self.auto_backup_task.cancel()
        logger.info("Backup system stopped")
    
    @tasks.loop(time=BACKUP_RUN_TIMES, reconnect=True)
    async def auto_backup_task(self):
        """Run database backups at fixed four-hour intervals."""
        try:
            success = await self.db.create_backup(bot=self.bot)
            if success:
                logger.info("Scheduled database backup created successfully")
            else:
                logger.error("Scheduled database backup failed")
        except Exception as e:
            logger.error(f"Error in automatic backup task: {e}")
    
    @auto_backup_task.before_loop
    async def before_auto_backup_task(self):
        """Wait for bot to be ready before starting backup task"""
        await self.bot.wait_until_ready()
    
    async def _delayed_startup_backups(self):
        """Wait for bot to be ready, then create startup backups"""
        await self.bot.wait_until_ready()
        # Wait a bit more for all cogs to be loaded
        await asyncio.sleep(2)
        await self._create_startup_backups()
    
    async def _create_startup_backups(self):
        """Create startup backups for both database and verification data"""
        try:
            # 1. Database backup (includes VN monthly data) - uses normal naming
            db_success = await self.db.create_backup(bot=self.bot)
            if db_success:
                logger.info("Startup database backup created successfully")
            else:
                logger.error("Failed to create startup database backup")
            
            # 2. Verification data backup - uses normal naming
            verification_cog = self.bot.get_cog('VerificationCog')
            if verification_cog:
                verification_success = await verification_cog.create_verification_backup()
                if verification_success:
                    logger.info("Startup verification data backup created successfully")
                else:
                    logger.error("Failed to create startup verification data backup")
            else:
                logger.warning("VerificationCog not found - skipping verification data startup backup")
            
        except Exception as e:
            logger.error(f"Error creating startup backups: {e}")
    
    async def _handle_set_channel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel]):
        """Set backup channel for this guild"""
        if channel is None:
            await interaction.followup.send("❌ Pick a channel for `set_channel`.", ephemeral=True)
            return
        try:
            success = await self.db.set_backup_channel(interaction.guild.id, channel.id)
            
            if success:
                embed = discord.Embed(
                    title="✅ Backup Channel Set",
                    description=f"Database backups will now be sent to {channel.mention}",
                    color=discord.Color.green(),
                    timestamp=datetime.now()
                )
                
                embed.add_field(
                    name="Information",
                    value="• Backups from **all servers** will be sent to this channel\n"
                          "• Only administrators can see this message\n"
                          "• Backups run every 4 hours (00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC)",
                    inline=False
                )
                
            else:
                embed = discord.Embed(
                    title="❌ Failed to Set Backup Channel",
                    description="An error occurred while setting the backup channel.",
                    color=discord.Color.red(),
                    timestamp=datetime.now()
                )
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in set_backup_channel command: {e}")
            await interaction.followup.send(f"❌ An error occurred: {e}", ephemeral=True)
    
    @app_commands.command(name="manage_backups", description="[ADMIN] Database backup management and backup channel configuration")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(
        action="Action to perform",
        backup_name="Name of backup file (for restore action)",
        channel="(set_channel) Channel to receive database backups"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="create", value="create"),
        app_commands.Choice(name="list", value="list"),
        app_commands.Choice(name="status", value="status"),
        app_commands.Choice(name="restore", value="restore"),
        app_commands.Choice(name="channels", value="channels"),
        app_commands.Choice(name="set_channel", value="set_channel"),
        app_commands.Choice(name="remove_channel", value="remove_channel")
    ])
    async def manage_backups(self, interaction: discord.Interaction, action: str,
                             backup_name: Optional[str] = None,
                             channel: Optional[discord.TextChannel] = None):
        """Database backup management command"""

        # Check if user has admin permissions
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need administrator permissions to use backup commands.", ephemeral=True)
            return

        # Restore exposes a destructive button; keep it ephemeral so only the invoker sees it.
        await interaction.response.defer(ephemeral=(action == "restore"))

        try:
            if action == "create":
                await self._handle_create_backup(interaction, backup_name)
            elif action == "list":
                await self._handle_list_backups(interaction)
            elif action == "status":
                await self._handle_backup_status(interaction)
            elif action == "restore":
                await self._handle_restore_backup(interaction, backup_name)
            elif action == "channels":
                await self._handle_backup_channels(interaction)
            elif action == "set_channel":
                await self._handle_set_channel(interaction, channel)
            elif action == "remove_channel":
                await self._handle_remove_channel(interaction)

        except Exception as e:
            logger.error(f"Error in backup command: {e}")
            await interaction.followup.send(f"❌ An error occurred: {e}", ephemeral=True)
    
    async def _handle_create_backup(self, interaction: discord.Interaction, backup_name: Optional[str]):
        """Handle manual backup creation"""
        success = await self.db.create_backup(backup_name, self.bot)
        
        if success:
            embed = discord.Embed(
                title="✅ Backup Created",
                description="Database backup created successfully!",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            
            # Get backup info
            backups = await self.db.list_backups()
            if backups:
                latest = backups[0]
                embed.add_field(
                    name="Backup Details",
                    value=f"**Name:** {latest['name']}\n**Size:** {latest['size_mb']} MB\n**Created:** <t:{int(latest['created'].timestamp())}:F>",
                    inline=False
                )
        else:
            embed = discord.Embed(
                title="❌ Backup Failed",
                description="Failed to create database backup. Check logs for details.",
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
        
        await interaction.followup.send(embed=embed)
    
    async def _handle_list_backups(self, interaction: discord.Interaction):
        """Handle backup listing"""
        backups = await self.db.list_backups()
        
        if not backups:
            embed = discord.Embed(
                title="📁 Database Backups",
                description="No backups found.",
                color=discord.Color.orange()
            )
        else:
            embed = discord.Embed(
                title="📁 Database Backups",
                description=f"Found {len(backups)} backup(s)",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            
            # Show up to 10 most recent backups
            for i, backup in enumerate(backups[:10]):
                embed.add_field(
                    name=f"{i+1}. {backup['name']}",
                    value=f"**Size:** {backup['size_mb']} MB\n**Created:** <t:{int(backup['created'].timestamp())}:R>",
                    inline=True
                )
            
            if len(backups) > 10:
                embed.add_field(
                    name="Note",
                    value=f"Showing 10 of {len(backups)} backups. Use `/manage_backups action:status` for more details.",
                    inline=False
                )
        
        await interaction.followup.send(embed=embed)
    
    async def _handle_backup_status(self, interaction: discord.Interaction):
        """Handle backup status display"""
        status = await self.db.get_backup_status()
        
        embed = discord.Embed(
            title="📊 Backup System Status",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        if status:
            embed.add_field(
                name="Configuration",
                value=f"**Backup Interval:** {status.get('backup_interval_hours', 'N/A')} hours\n"
                      f"**Max Backups:** {status.get('max_backups', 'N/A')}\n"
                      f"**Backup Directory:** `{status.get('backup_dir', 'N/A')}`",
                inline=False
            )
            
            embed.add_field(
                name="Current Status",
                value=f"**Total Backups:** {status.get('backup_count', 0)}\n"
                      f"**Total Size:** {status.get('total_backup_size_mb', 0)} MB",
                inline=True
            )
            
            latest = status.get('latest_backup')
            if latest:
                embed.add_field(
                    name="Latest Backup",
                    value=f"**Name:** {latest['name']}\n"
                          f"**Size:** {latest['size_mb']} MB\n"
                          f"**Created:** <t:{int(latest['created'].timestamp())}:R>",
                    inline=True
                )
            
            next_due = status.get('next_backup_due')
            if next_due:
                embed.add_field(
                    name="Next Automatic Backup",
                    value=f"<t:{int(next_due.timestamp())}:R>",
                    inline=False
                )
        else:
            embed.description = "Could not retrieve backup status."
        
        await interaction.followup.send(embed=embed)
    
    async def _handle_restore_backup(self, interaction: discord.Interaction, backup_name: Optional[str]):
        """Handle backup restoration"""
        if not backup_name:
            await interaction.followup.send("❌ Please specify a backup name to restore.", ephemeral=True)
            return
        
        # Show confirmation dialog, restricted to the invoking admin.
        view = BackupRestoreConfirmView(self.db, backup_name, interaction.user.id)

        embed = discord.Embed(
            title="⚠️ Confirm Database Restore",
            description=f"Are you sure you want to restore from backup `{backup_name}`?\n\n"
                       "**This will:**\n"
                       "• Create a backup of the current database\n"
                       "• Replace the current database with the backup\n"
                       "• This action cannot be easily undone",
            color=discord.Color.red()
        )

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    
    async def _handle_backup_channels(self, interaction: discord.Interaction):
        """Handle backup channels listing and management"""
        backup_channels = await self.db.get_backup_channels()
        
        embed = discord.Embed(
            title="🔗 Backup Channels Configuration",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        if not backup_channels:
            embed.description = "No backup channels configured.\nUse `/manage_backups action:set_channel` to configure one."
        else:
            embed.description = f"Database backups are sent to {len(backup_channels)} channel(s):"
            
            for i, channel_info in enumerate(backup_channels, 1):
                try:
                    channel = self.bot.get_channel(channel_info['channel_id'])
                    guild = self.bot.get_guild(channel_info['guild_id'])
                    
                    if channel and guild:
                        # Handle created_at timestamp
                        created_at = channel_info['created_at']
                        if isinstance(created_at, str):
                            try:
                                created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                            except:
                                created_at = datetime.now()
                        elif not isinstance(created_at, datetime):
                            created_at = datetime.now()
                        
                        embed.add_field(
                            name=f"{i}. {guild.name}",
                            value=f"**Channel:** {channel.mention}\n"
                                  f"**Set:** <t:{int(created_at.timestamp())}:R>",
                            inline=True
                        )
                    else:
                        embed.add_field(
                            name=f"{i}. Unknown Server",
                            value=f"**Channel ID:** {channel_info['channel_id']}\n"
                                  f"**Guild ID:** {channel_info['guild_id']}\n"
                                  f"❌ Channel/Guild not accessible",
                            inline=True
                        )
                except Exception as e:
                    logger.error(f"Error displaying backup channel info: {e}")
                    embed.add_field(
                        name=f"{i}. Error",
                        value=f"Channel ID: {channel_info['channel_id']}",
                        inline=True
                    )
        
        # Add management buttons
        view = BackupChannelManageView(self.db, interaction.guild.id if backup_channels else None)
        await interaction.followup.send(embed=embed, view=view)

    async def _handle_remove_channel(self, interaction: discord.Interaction):
        """Remove backup channel for this guild"""
        try:
            # Check if there's a backup channel set for this guild
            current_channel = await self.db.get_guild_backup_channel(interaction.guild.id)
            
            if not current_channel:
                embed = discord.Embed(
                    title="ℹ️ No Backup Channel Set",
                    description="This server doesn't have a backup channel configured.",
                    color=discord.Color.orange()
                )
            else:
                success = await self.db.remove_backup_channel(interaction.guild.id)
                
                if success:
                    embed = discord.Embed(
                        title="✅ Backup Channel Removed",
                        description="The backup channel configuration for this server has been removed.",
                        color=discord.Color.green(),
                        timestamp=datetime.now()
                    )
                else:
                    embed = discord.Embed(
                        title="❌ Failed to Remove Backup Channel",
                        description="An error occurred while removing the backup channel configuration.",
                        color=discord.Color.red(),
                        timestamp=datetime.now()
                    )
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in remove_backup_channel command: {e}")
            await interaction.followup.send(f"❌ An error occurred: {e}", ephemeral=True)

class BackupChannelManageView(discord.ui.View):
    """View for managing backup channels"""
    
    def __init__(self, db, guild_id=None):
        super().__init__(timeout=300)
        self.db = db
        self.guild_id = guild_id
        
        # Only show remove button if this guild has a backup channel
        if not guild_id:
            self.remove_button.disabled = True
    
    @discord.ui.button(label="Remove This Server's Channel", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Remove backup channel for current guild"""
        if self.guild_id != interaction.guild.id:
            await interaction.response.send_message("❌ This button can only be used by the server that has a backup channel configured.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        success = await self.db.remove_backup_channel(interaction.guild.id)
        
        if success:
            embed = discord.Embed(
                title="✅ Backup Channel Removed",
                description="The backup channel for this server has been removed.",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="❌ Remove Failed",
                description="Failed to remove the backup channel configuration.",
                color=discord.Color.red()
            )
        
        # Disable all buttons
        for item in self.children:
            if hasattr(item, 'disabled'):
                item.disabled = True
        
        await interaction.edit_original_response(embed=embed, view=self)

class BackupRestoreConfirmView(discord.ui.View):
    """Confirmation view for database restore operations"""

    def __init__(self, db: Database, backup_name: str, requester_id: int):
        super().__init__(timeout=60)
        self.db = db
        self.backup_name = backup_name
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the admin who started this restore can use these buttons.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm Restore", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm_restore(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Confirm and execute the restore operation"""
        # Views can outlive the command context; re-verify admin before a destructive write.
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member or not member.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You need administrator permissions to restore a backup.", ephemeral=True
            )
            return

        await interaction.response.defer()

        success = await self.db.restore_backup(self.backup_name)
        
        if success:
            embed = discord.Embed(
                title="✅ Database Restored",
                description=f"Successfully restored database from backup `{self.backup_name}`.\n\n"
                           "A backup of the previous database was created before restore.",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
        else:
            embed = discord.Embed(
                title="❌ Restore Failed",
                description=f"Failed to restore from backup `{self.backup_name}`. Check logs for details.",
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
        
        # Disable all buttons
        for item in self.children:
            item.disabled = True
        
        await interaction.edit_original_response(embed=embed, view=self)
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_restore(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel the restore operation"""
        embed = discord.Embed(
            title="❌ Restore Cancelled",
            description="Database restore operation was cancelled.",
            color=discord.Color.orange()
        )
        
        # Disable all buttons
        for item in self.children:
            item.disabled = True
        
        await interaction.response.edit_message(embed=embed, view=self)

async def setup(bot):
    await bot.add_cog(BackupCog(bot))
