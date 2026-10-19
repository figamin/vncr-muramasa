"""
LJTA (Learn Japanese Through Anime) - Backup Override
Disables backup system since the anime bot shares the main bot's database.
All backups are handled by the main bot to avoid conflicts.
"""

import discord
from discord.ext import commands
import logging
import asyncio
import sys
from pathlib import Path

# Import parent backup cog
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from cogs.backup import BackupCog as ParentBackupCog

logger = logging.getLogger(__name__)

class BackupCog(ParentBackupCog):
    """LJTA Backup system - disabled (main bot handles backups for shared database)"""
    
    async def cog_load(self):
        """Initialize database but don't start backup tasks"""
        await self.db.initialize()
        # Don't start auto_backup_task - main bot handles this
        logger.info("Backup cog loaded (backup creation disabled - main bot handles backups)")
    
    async def cog_unload(self):
        """Clean up when cog unloads"""
        # Don't cancel backup task since it is never started
        logger.info("Backup cog unloaded")
    
    async def _create_startup_backups(self):
        """Skip startup backups - main bot handles this"""
        logger.info("Startup backups skipped (main bot handles backups for shared database)")
        return

async def setup(bot: commands.Bot):
    cog = BackupCog(bot)
    await bot.add_cog(cog)

    # Remove privileged backup commands for the anime bot deployment
    restricted_commands = [
        "backup",
        "set_backup_channel",
        "remove_backup_channel",
    ]

    for command_name in restricted_commands:
        existing_command = bot.tree.get_command(command_name, type=discord.AppCommandType.chat_input)
        if existing_command:
            bot.tree.remove_command(command_name, type=discord.AppCommandType.chat_input)
            logger.info("Removed restricted command '%s' from anime bot", command_name)
