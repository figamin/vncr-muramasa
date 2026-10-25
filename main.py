import asyncio
import discord
from discord.ext import commands
import json
import os
import platform
import logging
import sys
import threading
from pathlib import Path
import importlib.util
from dotenv import load_dotenv
from utils.bot_registry import update_bot_guilds, remove_bot_guilds

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Bot configuration
intents = discord.Intents.default()
intents.members = True  # Required for on_member_join
intents.message_content = True  # For command processing
intents.presences = True  # Required to track online/offline status
bot = commands.Bot(command_prefix='!', intents=intents, description="Expandable Welcome Bot")

# Configuration file handling
CONFIG_FILE = 'config.json'
TOKEN_FILE = 'token.json'
DEFAULT_CONFIG = {'guilds': {}}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        # Loud on purpose. Every per-guild feature (verification, quiz levels, welcome
        # messages, highlights, ...) reads its switches from this file, so starting
        # without it leaves them all silently off: the bot looks like it is ignoring
        # its settings rather than like it failed to start.
        logger.error(
            "%s is missing from %s; starting with an empty guild configuration. "
            "Per-guild features stay disabled until it is created. A file containing "
            "{} is enough: /manage_* commands and the web console fill it in.",
            CONFIG_FILE, os.getcwd(),
        )
        return dict(DEFAULT_CONFIG, guilds={})
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
    if not isinstance(config, dict):
        logger.error("%s does not contain a JSON object; ignoring its contents.", CONFIG_FILE)
        return dict(DEFAULT_CONFIG, guilds={})
    # The README tells operators to create the file containing {}, and several callers
    # index bot.config['guilds'] directly, so the key is normalised here.
    config.setdefault('guilds', {})
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

def load_token():
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError(f"{TOKEN_FILE} not found. Please create it with your bot token.")
    try:
        with open(TOKEN_FILE, 'r') as f:
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

# Start Web Console in background thread
def start_web_console_thread():
    """Start the web console in a daemon thread"""
    try:
        import web_console
        logger.info("Starting web console in background thread...")
        # Pass bot instance to web console
        web_console.bot_instance = bot
        console_thread = threading.Thread(target=web_console.run_web_console, daemon=True)
        console_thread.start()
        logger.info(f"✓ Web console started - Access at http://localhost:{web_console.PORT}")
    except ImportError as e:
        logger.warning(f"Web console not available: {e}")
        logger.warning("Install Flask to enable web console: pip install flask")
    except Exception as e:
        logger.error(f"Failed to start web console: {e}")

# Start web console immediately
start_web_console_thread()

# Load cogs
async def load_extensions():
    for cog in ['cogs.commands', 'cogs.events', 'cogs.vn_month', 'cogs.backup', 'cogs.server_backup', 'cogs.error_logging', 'cogs.verification', 'cogs.onboarding', 'cogs.immersion', 'cogs.highlights', 'cogs.sticky_messages', 'cogs.kizuna', 'cogs.message_purge', 'cogs.role_menu', 'cogs.message_filter', 'cogs.media_flag']:
        try:
            await bot.load_extension(cog)
            logger.info(f'Loaded cog: {cog}')
        except Exception as e:
            logger.error(f'Failed to load cog {cog}: {e}')

# Bot ready event
@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    
    # Update guild registry for web console
    update_bot_guilds("Main Bot", bot)
    
    # Set bot status
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.playing,
            name="/help"
        )
    )
    logger.info('Bot status set to: /help | vnclub.figamin.net')
    
    try:
        # Full sync; Discord handles the diff
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
            update_bot_guilds("Main Bot", bot)
        except Exception as e:
            logger.error(f"Error updating guild registry: {e}")

@bot.event
async def on_guild_join(guild):
    """Update registry when bot joins a new guild"""
    logger.info(f"Joined new guild: {guild.name} ({guild.id})")
    update_bot_guilds("Main Bot", bot)

@bot.event
async def on_guild_remove(guild):
    """Update registry when bot leaves a guild"""
    logger.info(f"Left guild: {guild.name} ({guild.id})")
    update_bot_guilds("Main Bot", bot)

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
            remove_bot_guilds("Main Bot")

# Anime bot startup function
async def start_anime_bot():
    """Start the anime bot in a separate task"""
    try:
        anime_bot_path = Path(__file__).parent / 'sub-bots' / 'anime-bot'
        if not anime_bot_path.exists():
            logger.warning(f'Anime bot directory not found at {anime_bot_path}')
            return
        
        logger.info('Starting anime bot...')
        # Import anime bot token/config paths
        anime_token_path = anime_bot_path / 'token.json'
        anime_main_path = anime_bot_path / 'main.py'

        if not anime_token_path.exists():
            logger.warning(f'Anime bot token.json not found at {anime_token_path}. Skipping anime bot.')
            return

        if not anime_main_path.exists():
            logger.warning(f'Anime bot main.py not found at {anime_main_path}. Skipping anime bot.')
            return

        # Dynamically import the anime bot module
        spec = importlib.util.spec_from_file_location('anime_bot_main', anime_main_path)
        if spec is None or spec.loader is None:
            logger.error('Unable to load anime bot module spec')
            return

        anime_module = importlib.util.module_from_spec(spec)
        sys.modules['anime_bot_main'] = anime_module
        spec.loader.exec_module(anime_module)

        anime_main_coroutine = getattr(anime_module, 'main', None)
        if anime_main_coroutine is None or not asyncio.iscoroutinefunction(anime_main_coroutine):
            logger.error('Anime bot module main() coroutine not found')
            return

        await anime_main_coroutine()

    except Exception as e:
        logger.error(f'Failed to start anime bot: {e}', exc_info=True)

# Combined main function to run both bots
async def run_both_bots():
    """Run both the main bot and anime bot concurrently"""
    try:
        # Create tasks for both bots
        main_bot_task = asyncio.create_task(main())
        anime_bot_task = asyncio.create_task(start_anime_bot())
        
        # Wait for both bots to complete (they should run indefinitely)
        await asyncio.gather(main_bot_task, anime_bot_task)
    except Exception as e:
        logger.error(f'Error running bots: {e}', exc_info=True)

# Pyodide-compatible execution
if platform.system() == "Emscripten":
    asyncio.ensure_future(run_both_bots())
else:
    if __name__ == "__main__":
        asyncio.run(run_both_bots())