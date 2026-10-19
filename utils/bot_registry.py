"""
Bot Registry - Shared guild information for web console
Allows multiple bot instances to report their guild information to the web console
"""
import json
from pathlib import Path
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

REGISTRY_FILE = Path(__file__).parent.parent / 'data' / 'sub_bot_guilds.json'

def update_bot_guilds(bot_name, bot_instance):
    """
    Update guild information for a bot instance
    
    Args:
        bot_name: Name identifier for the bot (e.g., "Anime Bot", "Main Bot")
        bot_instance: Discord bot instance with guilds
    """
    try:
        # Ensure data directory exists
        REGISTRY_FILE.parent.mkdir(exist_ok=True)
        
        # Load existing registry
        registry = {}
        if REGISTRY_FILE.exists():
            try:
                with open(REGISTRY_FILE, 'r') as f:
                    registry = json.load(f)
            except json.JSONDecodeError:
                logger.warning("Invalid registry file, creating new one")
                registry = {}
        
        # Collect guild information
        guilds_info = []
        for guild in bot_instance.guilds:
            # Count non-bot members
            human_count = sum(1 for member in guild.members if not member.bot)
            
            # Count online users (all users including bots with status != offline)
            # Status can be: online, idle, dnd (do not disturb), offline, streaming
            # Note: Presence intent must be enabled in Discord Developer Portal for this to work
            try:
                total_online = sum(1 for member in guild.members 
                                 if str(member.status) != 'offline')
                
                # Count online human users only (excluding bots)
                human_online = sum(1 for member in guild.members 
                                 if not member.bot and str(member.status) != 'offline')
            except Exception as e:
                logger.warning(f"Could not get online count for guild {guild.name}: {e}. Presence intent may not be enabled.")
                total_online = 0
                human_online = 0
            
            guild_data = {
                'id': str(guild.id),
                'name': guild.name,
                'member_count': guild.member_count,
                'human_count': human_count,
                'online_count': total_online,
                'human_online_count': human_online,
                'owner_id': str(guild.owner_id) if guild.owner_id else None,
                'created_at': guild.created_at.strftime('%Y-%m-%d %H:%M:%S') if guild.created_at else None,
                'text_channels': len(guild.text_channels),
                'voice_channels': len(guild.voice_channels),
                'roles': len(guild.roles) - 1,  # Subtract 1 to exclude @everyone role
                'icon_url': str(guild.icon.url) if guild.icon else None,
                'features': guild.features if hasattr(guild, 'features') else [],
                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            guilds_info.append(guild_data)
        
        # Update registry
        registry[bot_name] = guilds_info
        
        # Save registry
        with open(REGISTRY_FILE, 'w') as f:
            json.dump(registry, f, indent=2)
        
        logger.info(f"Updated guild registry for {bot_name}: {len(guilds_info)} guilds")
        return True
        
    except Exception as e:
        logger.error(f"Failed to update bot registry for {bot_name}: {e}")
        return False

def remove_bot_guilds(bot_name):
    """
    Remove a bot's guild information from the registry
    
    Args:
        bot_name: Name identifier for the bot
    """
    try:
        if not REGISTRY_FILE.exists():
            return True
        
        with open(REGISTRY_FILE, 'r') as f:
            registry = json.load(f)
        
        if bot_name in registry:
            del registry[bot_name]
            
            with open(REGISTRY_FILE, 'w') as f:
                json.dump(registry, f, indent=2)
            
            logger.info(f"Removed guild registry for {bot_name}")
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to remove bot registry for {bot_name}: {e}")
        return False
