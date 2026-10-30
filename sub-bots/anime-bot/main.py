import asyncio
import discord
from discord.ext import commands
import json
import os
import sys
import platform
import logging
from pathlib import Path

# Add parent directory to path so the parent bot's cogs are importable
PARENT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PARENT_DIR))

# Import bot registry for web console integration
from utils.bot_registry import update_bot_guilds, remove_bot_guilds

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Bot configuration
intents = discord.Intents.default()
intents.members = True  # Required for on_member_join
intents.message_content = True  # For command processing
intents.presences = True  # Required to track online/offline status
bot = commands.Bot(command_prefix='!', intents=intents, description="LJTA (Learn Japanese Through Anime) Bot")

# IMPORTANT: This bot shares the parent bot's database (../../data/vnbot.db)
# This enables cross-server time tracking and unified data management
# The parent bot handles all database backups

# Configuration file handling
CONFIG_FILE = 'config.json'
TOKEN_FILE = 'token.json'
DEFAULT_CONFIG = {'guilds': {}}

def load_config():
    """Load config from anime bot's directory"""
    config_path = Path(__file__).parent / CONFIG_FILE
    if config_path.exists():
        with open(config_path, 'r') as f:
            return json.load(f)
    return DEFAULT_CONFIG

def save_config(config):
    """Save config to anime bot's directory"""
    config_path = Path(__file__).parent / CONFIG_FILE
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=4)

def load_token():
    """Load token from anime bot's directory"""
    token_path = Path(__file__).parent / TOKEN_FILE
    if not token_path.exists():
        raise FileNotFoundError(f"{TOKEN_FILE} not found. Please create it with your anime bot token.")
    try:
        with open(token_path, 'r') as f:
            data = json.load(f)
            token = data.get('token')
            if not token:
                raise ValueError(f"No 'token' key found in {TOKEN_FILE}.")
            return token
    except json.JSONDecodeError:
        raise ValueError(f"Invalid JSON in {TOKEN_FILE}.")

bot.config = load_config()

# Add save_config method to bot
def save_bot_config():
    save_config(bot.config)

bot.save_config = save_bot_config

# Add reload_config method to bot (for hot-reloading config changes)
def reload_bot_config():
    bot.config = load_config()
    logger.info("Config reloaded from disk")
    return bot.config

bot.reload_config = reload_bot_config

# Load cogs from parent directory
async def load_extensions():
    """Load cogs from parent bot directory, with optional overrides"""
    cogs_to_load = [
        'cogs.commands',
        'cogs.events',
        # 'cogs.vn_month',  # Not loaded: not needed for anime bot
        # 'cogs.backup',  # Not loaded: muramasa owns DB backups; sakura is lent to
        # servers whose admins must not reach /manage_backups on the shared DB.
        'cogs.error_logging',
        'cogs.verification',  # Level quizzes (N5-Insane) that grant roles
        'cogs.immersion',  # Core time tracking - works for anime too
        'cogs.highlights'
    ]
    
    for cog in cogs_to_load:
        try:
            # Check if there's an override in sub-bots/anime-bot/overrides/
            override_name = cog.split('.')[-1]
            override_path = Path(__file__).parent / 'overrides' / f'{override_name}.py'
            
            if override_path.exists():
                # Load override version
                logger.info(f'Loading override for {cog}: overrides.{override_name}')
                await bot.load_extension(f'sub-bots.anime-bot.overrides.{override_name}')
            else:
                # Load parent version
                await bot.load_extension(cog)
                logger.info(f'Loaded cog: {cog}')
        except Exception as e:
            logger.error(f'Failed to load cog {cog}: {e}')

# Bot ready event
@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    
    # Update guild registry for web console
    update_bot_guilds("Anime Bot", bot)
    
    # Set bot status (customize for anime bot)
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="anime with you 📺"
        )
    )
    logger.info('Bot status set')
    
    try:
        # Sync slash commands
        synced = await bot.tree.sync()
        logger.info(f'Synced {len(synced)} slash commands')
        for cmd in synced:
            logger.info(f'  - {cmd.name}')
    except Exception as e:
        logger.error(f'Failed to sync slash commands: {e}')
    
    # Start periodic guild registry updates
    if not hasattr(bot, '_registry_update_task'):
        bot._registry_update_task = bot.loop.create_task(periodic_guild_update())

async def periodic_guild_update():
    """Update guild registry every 5 minutes"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await asyncio.sleep(300)  # 5 minutes
            update_bot_guilds("Anime Bot", bot)
        except Exception as e:
            logger.error(f"Error updating guild registry: {e}")

@bot.event
async def on_guild_join(guild):
    """Update registry when bot joins a new guild"""
    logger.info(f"Joined new guild: {guild.name} ({guild.id})")
    update_bot_guilds("Anime Bot", bot)

@bot.event
async def on_guild_remove(guild):
    """Update registry when bot leaves a guild"""
    logger.info(f"Left guild: {guild.name} ({guild.id})")
    update_bot_guilds("Anime Bot", bot)

# Main function
async def main():
    async with bot:
        try:
            await load_extensions()
            token = load_token()
            await bot.start(token)
        finally:
            # Clean up registry on shutdown
            logger.info("Cleaning up guild registry...")
            remove_bot_guilds("Anime Bot")

# Pyodide-compatible execution
if platform.system() == "Emscripten":
    asyncio.ensure_future(main())
else:
    if __name__ == "__main__":
        asyncio.run(main())
