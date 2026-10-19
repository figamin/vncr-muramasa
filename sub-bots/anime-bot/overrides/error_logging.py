"""
LJTA (Learn Japanese Through Anime) - Error Logging Override
Restricts administrative log channel management commands for the shared deployment.
"""

import discord
from discord.ext import commands
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Make sure parent package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from cogs.error_logging import ErrorLoggingCog as ParentErrorLoggingCog


class ErrorLoggingCog(ParentErrorLoggingCog):
    """Anime bot error logging with restricted configuration surface."""


async def setup(bot: commands.Bot):
    cog = ErrorLoggingCog(bot)
    await bot.add_cog(cog)

    # Remove privileged log channel commands for the anime bot deployment
    restricted_commands = [
        "set_logs_channel",
        "remove_logs_channel",
    ]

    for command_name in restricted_commands:
        existing_command = bot.tree.get_command(command_name, type=discord.AppCommandType.chat_input)
        if existing_command:
            bot.tree.remove_command(command_name, type=discord.AppCommandType.chat_input)
            logger.info("Removed restricted command '%s' from anime bot", command_name)
