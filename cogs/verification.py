import discord
from discord.ext import commands, tasks
import discord.app_commands
import json
import os
import re
import requests
import asyncio
import shutil
import traceback
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Any
from pathlib import Path

from utils.backup_schedule import BACKUP_INTERVAL_HOURS, BACKUP_RUN_TIMES, next_backup_run_after

class VerificationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.verification_data_file = os.path.join('data', 'verification_data.json')
        self.verification_data = self.load_verification_data()
        
        # Backup configuration
        self.backup_dir = Path('data/backups')
        self.backup_dir.mkdir(exist_ok=True)
        self.max_backups = 60  # Kept in step with the database snapshot cap
        self.backup_interval_hours = BACKUP_INTERVAL_HOURS
        # Allowlist for backup file names, so create/restore stay inside the backup directory.
        self._backup_name_pattern = re.compile(r"^[A-Za-z0-9_.-]+$")

        # Kotoba bot ID
        self.KOTOBA_ID = 251239170058616833
        
        # User operation locks to prevent race conditions
        self._user_locks = {}
        
        # Initialize sync flag - task will be created in cog_load
        self._sync_task = None
        
        # Initialize database instance for backup operations
        self.backup_db = None
        
        # Migrate existing data if needed
        self.migrate_existing_data()

        # Level 1 quiz settings (lowest level)
        self.LEVEL_1_QUIZ = {
            'decks': ['vncr1'],
            'deck_range': None,
            'score_limit': 8,
            'max_missed': 4,
            'time_limit': 16000,
            'font_size': 100,
            'foreground': '#f173ff'
        }

        # Quiz command for level 1
        self.LEVEL_1_COMMAND = "k!quiz vncr1 8 hardcore nd mmq=4 dauq=1 font=1 size=100 atl=16"

        # Level 2-5 quiz settings
        self.LEVEL_2_5_QUIZZES = {}
        self.LEVEL_2_5_COMMANDS = {}

        for level in range(2, 6):
            self.LEVEL_2_5_QUIZZES[level] = {
                'decks': [f'vncr{level}'],
                'deck_range': None,
                'score_limit': 30,
                'max_missed': 4,
                'time_limit': 16000,
                'font_size': 100,
                'foreground': '#f173ff',
                'effect': 'antiocr'
            }
            self.LEVEL_2_5_COMMANDS[level] = f"k!quiz vncr{level} 30 nd hardcore dauq=1 font=5 atl=16 mmq=4 color=#f173ff size=100 effect=antiocr"

        # Level 6-10 quiz settings
        self.LEVEL_6_10_QUIZZES = {}
        self.LEVEL_6_10_COMMANDS = {}

        for level in range(6, 11):
            self.LEVEL_6_10_QUIZZES[level] = {
                'decks': [f'vncr{level}'],
                'deck_range': None,
                'score_limit': 35,
                'max_missed': 3,
                'time_limit': 16000,
                'font_size': 100,
                'foreground': '#f173ff',
                'effect': 'antiocr'
            }
            self.LEVEL_6_10_COMMANDS[level] = f"k!quiz vncr{level} 35 nd hardcore dauq=1 font=5 atl=16 mmq=3 color=#f173ff size=100 effect=antiocr"

        # Level 11-15 quiz settings
        self.LEVEL_11_15_QUIZZES = {}
        self.LEVEL_11_15_COMMANDS = {}

        for level in range(11, 16):
            self.LEVEL_11_15_QUIZZES[level] = {
                'decks': [f'vncr{level}'],
                'deck_range': None,
                'score_limit': 40,
                'max_missed': 3,
                'time_limit': 16000,
                'font_size': 100,
                'foreground': '#f173ff',
                'effect': 'antiocr'
            }
            self.LEVEL_11_15_COMMANDS[level] = f"k!quiz vncr{level} 40 nd hardcore dauq=1 font=5 atl=16 mmq=3 color=#f173ff size=100 effect=antiocr"
    
    async def cog_load(self):
        """Initialize backup system when cog loads"""
        # Initialize backup database instance
        from utils.database import Database
        self.backup_db = Database()
        await self.backup_db.initialize()
        
        self.auto_backup_task.start()
        
        # Schedule initial sync after bot is ready
        if hasattr(self.bot, 'loop') and self.bot.loop:
            self._sync_task = self.bot.loop.create_task(self.delayed_sync())
        
        print("Verification backup system started")
    
    async def cog_unload(self):
        """Clean up when cog unloads"""
        self.auto_backup_task.cancel()
        
        # Cancel sync task if it exists
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
        
        print("Verification backup system stopped")
    
    async def delayed_sync(self):
        """Wait for bot to be ready, then sync verification data"""
        await self.bot.wait_until_ready()
        # Wait a bit more for guilds to be available
        await asyncio.sleep(5)
        
        for guild in self.bot.guilds:
            guild_config = self.bot.config['guilds'].get(str(guild.id), {})
            if guild_config.get('verification_enabled', False):
                print(f"Syncing verification data for guild: {guild.name}")
                # Ensure roles exist
                await self.ensure_roles_exist(guild)
                # Migrate high-level users before sync (safety measure)
                await self.migrate_high_level_users(guild)
                # Then sync data
                await self.sync_verification_data(guild)
                # Assign level 0 to any Confirmed Reader users without tracked quiz levels
                await self.assign_level_0_to_untracked_users(guild)

    def load_verification_data(self) -> Dict:
        """Load and validate verification data from file"""
        os.makedirs('data', exist_ok=True)
        if os.path.exists(self.verification_data_file):
            try:
                with open(self.verification_data_file, 'r') as f:
                    data = json.load(f)
                    # Validate and clean data structure
                    validated_data = self._validate_data_structure(data)
                    return validated_data
            except (json.JSONDecodeError, FileNotFoundError) as e:
                print(f"Error loading verification data: {e}")
        return {"verified_users": [], "quiz_attempts": {}, "highest_quiz_levels": {}}
    
    def _validate_data_structure(self, data: Dict) -> Dict:
        """Validate and clean data structure"""
        validated = {
            "verified_users": [],
            "quiz_attempts": {},
            "highest_quiz_levels": {}
        }
        
        # Validate verified_users
        if isinstance(data.get("verified_users"), list):
            validated["verified_users"] = [str(uid) for uid in data["verified_users"] if str(uid).isdigit()]
        
        # Validate quiz_attempts
        if isinstance(data.get("quiz_attempts"), dict):
            for uid, attempts in data["quiz_attempts"].items():
                if str(uid).isdigit() and isinstance(attempts, list):
                    validated["quiz_attempts"][str(uid)] = attempts
        
        # Validate highest_quiz_levels
        if isinstance(data.get("highest_quiz_levels"), dict):
            for uid, level in data["highest_quiz_levels"].items():
                if str(uid).isdigit() and isinstance(level, int) and 0 <= level <= 15:
                    validated["highest_quiz_levels"][str(uid)] = level
        
        return validated
    
    def migrate_existing_data(self):
        """Migrate existing verified users to have a highest quiz level of 1"""
        needs_save = False
        
        # Check for verified users who don't have a highest quiz level yet
        for user_id_str in self.verification_data["verified_users"]:
            if user_id_str not in self.verification_data["highest_quiz_levels"]:
                # Assume they passed level 1 since they're verified
                self.verification_data["highest_quiz_levels"][user_id_str] = 1
                needs_save = True
                print(f"Migrated user {user_id_str} to highest quiz level 1")
        
        if needs_save:
            self.save_verification_data()
            print("Data migration completed")
    
    async def _get_user_lock(self, user_id: int):
        """Get or create a lock for specific user operations"""
        user_id_str = str(user_id)
        if user_id_str not in self._user_locks:
            self._user_locks[user_id_str] = asyncio.Lock()
        return self._user_locks[user_id_str]
    
    async def _validate_single_quiz_role(self, member: discord.Member):
        """Ensure user has exactly one quiz level role"""
        quiz_roles = []
        for level in range(16):  # 0-15
            level_role = discord.utils.get(member.guild.roles, name=str(level))
            if level_role and level_role in member.roles:
                quiz_roles.append((level, level_role))
        
        if len(quiz_roles) > 1:
            # Remove all but the highest level role
            highest_level = max(quiz_roles, key=lambda x: x[0])
            for level, role in quiz_roles:
                if level != highest_level[0]:
                    try:
                        await member.remove_roles(role, reason="Multiple quiz roles detected")
                        print(f"Removed duplicate quiz role {level} from {member.name}")
                    except discord.Forbidden:
                        print(f"Cannot remove duplicate role {level} from {member.name}")
            return highest_level[0]
        elif len(quiz_roles) == 1:
            return quiz_roles[0][0]
        else:
            return None
    
    async def migrate_high_level_users(self, guild):
        """Migrate users who have high-level roles (6-15) to ensure their data is correct"""
        print("Checking for users with high-level roles that need migration...")
        migrated_count = 0
        
        try:
            for member in guild.members:
                # Skip bots
                if member.bot:
                    continue
                
                user_id_str = str(member.id)
                
                # Find their actual highest role (check 15 down to 0)
                actual_highest_role = None
                for level in range(15, -1, -1):
                    level_role = discord.utils.get(guild.roles, name=str(level))
                    if level_role and level_role in member.roles:
                        actual_highest_role = level
                        break
                
                # Check their tracked level
                tracked_level = self.verification_data["highest_quiz_levels"].get(user_id_str, 0)
                
                # If they have a higher role than what's tracked, update the tracking
                if actual_highest_role is not None and actual_highest_role > tracked_level:
                    print(f"Migrating {member.name}: Discord role Level {actual_highest_role}, tracked Level {tracked_level}")
                    self.verification_data["highest_quiz_levels"][user_id_str] = actual_highest_role
                    migrated_count += 1
            
            if migrated_count > 0:
                self.save_verification_data()
                print(f"✅ Migration complete: Updated {migrated_count} users with high-level roles")
            else:
                print("✅ No high-level role migrations needed")
                
        except Exception as e:
            print(f"❌ Error during high-level user migration: {e}")
    
    async def assign_level_0_to_untracked_users(self, guild):
        """Assign level 0 to any users who don't have a quiz level tracked"""
        try:            
            level_0_role = discord.utils.get(guild.roles, name="0")
            if not level_0_role:
                return
            
            assigned_count = 0
            for member in guild.members:
                # Skip bots
                if member.bot:
                    continue
                    
                user_id = member.id
                user_id_str = str(user_id)
                
                # If user has no tracked quiz level, assign them level 0
                if user_id_str not in self.verification_data["highest_quiz_levels"]:
                    # Set their highest quiz level to 0
                    self.verification_data["highest_quiz_levels"][user_id_str] = 0
                    
                    # Assign level 0 role if they don't have it
                    if level_0_role not in member.roles:
                        try:
                            await member.add_roles(level_0_role)
                            print(f"Assigned level 0 role to untracked user {member.name}")
                        except discord.Forbidden:
                            print(f"Cannot assign level 0 role to {member.name}")
                    
                    assigned_count += 1
            
            if assigned_count > 0:
                self.save_verification_data()
                print(f"Assigned level 0 to {assigned_count} untracked users")
                
        except Exception as e:
            print(f"Error assigning level 0 to untracked users: {e}")
    
    def save_verification_data(self):
        """Save verification data to file"""
        try:
            with open(self.verification_data_file, 'w') as f:
                json.dump(self.verification_data, f, indent=2)
        except Exception as e:
            print(f"Error saving verification data: {e}")
    
    def is_user_verified(self, user_id: int) -> bool:
        """Check if a user is verified"""
        return str(user_id) in self.verification_data["verified_users"]
    
    def add_verified_user(self, user_id: int):
        """Add a user to the verified list"""
        user_id_str = str(user_id)
        if user_id_str not in self.verification_data["verified_users"]:
            self.verification_data["verified_users"].append(user_id_str)
            self.save_verification_data()
    
    def record_quiz_attempt(self, user_id: int, quiz_command: str, result: str):
        """Record a quiz attempt"""
        user_id_str = str(user_id)
        if user_id_str not in self.verification_data["quiz_attempts"]:
            self.verification_data["quiz_attempts"][user_id_str] = []
        
        attempt_data = {
            "quiz_command": quiz_command,
            "result": result,
            "timestamp": datetime.now().isoformat()
        }
        
        self.verification_data["quiz_attempts"][user_id_str].append(attempt_data)
        self.save_verification_data()
    
    def get_last_attempt(self, user_id: int):
        """Get the last quiz attempt for a user"""
        user_id_str = str(user_id)
        if user_id_str in self.verification_data["quiz_attempts"]:
            attempts = self.verification_data["quiz_attempts"][user_id_str]
            if attempts:
                return attempts[-1]
        return None
    
    def get_highest_quiz_level(self, user_id: int) -> int:
        """Get the highest quiz level passed by a user"""
        user_id_str = str(user_id)
        return self.verification_data["highest_quiz_levels"].get(user_id_str, 0)
    
    def set_highest_quiz_level(self, user_id: int, level: int):
        """Set the highest quiz level for a user if it's higher than current"""
        user_id_str = str(user_id)
        current_highest = self.get_highest_quiz_level(user_id)
        if level > current_highest:
            self.verification_data["highest_quiz_levels"][user_id_str] = level
            self.save_verification_data()
            print(f"Updated highest quiz level for user {user_id} to level {level}")
    
    def get_role_name_for_level(self, level: int) -> str:
        """Get the role name for a given quiz level"""
        return str(level)  # Role names are "1", "2", "3", etc.
    

    
    async def restore_user_roles(self, member: discord.Member, highest_level: int):
        """Restore roles for a returning user based on their highest quiz level"""
        try:
            # Get the role for their highest level
            role_name = self.get_role_name_for_level(highest_level)
            verified_role = discord.utils.get(member.guild.roles, name=role_name)
            confirmed_reader_role = discord.utils.get(member.guild.roles, name="Confirmed Reader")
            unverified_role = discord.utils.get(member.guild.roles, name="0")
            
            # Remove any incorrect quiz level roles first
            for check_level in range(0, 16):  # Check levels 0-15
                if check_level != highest_level:  # Remove any role that's not their correct level
                    level_role = discord.utils.get(member.guild.roles, name=str(check_level))
                    if level_role and level_role in member.roles:
                        try:
                            await member.remove_roles(level_role)
                            print(f"Removed incorrect quiz level role '{check_level}' from {member.name}")
                        except discord.Forbidden:
                            print(f"Cannot remove role '{check_level}' from {member.name}")
            
            # Assign the verified role for their level
            if verified_role and verified_role not in member.roles:
                try:
                    await member.add_roles(verified_role)
                    print(f"Restored role '{role_name}' to returning user {member.name}")
                except discord.Forbidden:
                    print(f"Cannot assign role '{role_name}' to {member.name}")
            
            # Assign Confirmed Reader role ONLY if they're in the verified users list (had server access before)
            user_id_str = str(member.id)
            was_verified = user_id_str in self.verification_data["verified_users"]
            if was_verified and confirmed_reader_role and confirmed_reader_role not in member.roles:
                try:
                    await member.add_roles(confirmed_reader_role)
                    print(f"Restored 'Confirmed Reader' role to {member.name}")
                except discord.Forbidden:
                    print(f"Cannot assign 'Confirmed Reader' role to {member.name}")
            
            # For level 0, don't remove the unverified role since that's what level 0 is
            # For level 1+, remove unverified role if they somehow have it
            if highest_level > 0 and unverified_role and unverified_role in member.roles:
                try:
                    await member.remove_roles(unverified_role)
                    print(f"Removed unverified role from returning user {member.name}")
                except discord.Forbidden:
                    print(f"Cannot remove unverified role from {member.name}")

            # Remove "Unconfirmed Reader" role if they have it (since they're verified)
            unconfirmed_reader_role = discord.utils.get(member.guild.roles, name="Unconfirmed Reader")
            if was_verified and unconfirmed_reader_role and unconfirmed_reader_role in member.roles:
                try:
                    await member.remove_roles(unconfirmed_reader_role)
                    print(f"Removed 'Unconfirmed Reader' from returning verified user {member.name}")
                except discord.Forbidden:
                    print(f"Cannot remove 'Unconfirmed Reader' role from {member.name}")

            # Update verified users list (only for level 1+)
            if highest_level > 0:
                self.add_verified_user(member.id)
            
        except Exception as e:
            print(f"Error restoring user roles for {member.name}: {e}")
    
    async def sync_verification_data(self, guild):
        """Sync verification data to match actual Discord roles (server roles are the source of truth)"""
        data_changed = False
        confirmed_reader_role = discord.utils.get(guild.roles, name="Confirmed Reader")
        
        # Scan all members and update data based on their actual roles
        for member in guild.members:
            # Skip bots
            if member.bot:
                continue
                
            user_id = member.id
            user_id_str = str(user_id)
            
            # Find their highest quiz level role (0-15)
            actual_level = None
            for level in range(15, -1, -1):  # Check from 15 down to 0
                level_role = discord.utils.get(guild.roles, name=str(level))
                if level_role and level_role in member.roles:
                    actual_level = level
                    break
            
            # If they have no level role, assign level 0
            if actual_level is None:
                level_0_role = discord.utils.get(guild.roles, name="0")
                if level_0_role:
                    try:
                        await member.add_roles(level_0_role)
                        actual_level = 0
                        print(f"Assigned level 0 to user {member.name} (no level role)")
                    except Exception as e:
                        print(f"Error assigning level 0 to {member.name}: {e}")
                        continue
            
            # Update tracking data to match actual level
            current_tracked = self.verification_data["highest_quiz_levels"].get(user_id_str, None)
            if current_tracked != actual_level:
                self.verification_data["highest_quiz_levels"][user_id_str] = actual_level
                print(f"Updated {member.name} quiz level: {current_tracked} → {actual_level}")
                data_changed = True
            
            # Update verified users list based on Confirmed Reader role
            has_confirmed_reader = confirmed_reader_role and confirmed_reader_role in member.roles
            is_in_verified_list = user_id_str in self.verification_data["verified_users"]

            if has_confirmed_reader and not is_in_verified_list:
                # Has server access but not in verified list - add them
                self.verification_data["verified_users"].append(user_id_str)
                print(f"Added {member.name} to verified list (has Confirmed Reader)")
                data_changed = True
            elif not has_confirmed_reader and is_in_verified_list:
                # In verified list but no server access - remove them
                self.verification_data["verified_users"].remove(user_id_str)
                print(f"Removed {member.name} from verified list (no Confirmed Reader)")
                data_changed = True

            # Sync Unconfirmed Reader role (mutually exclusive with Confirmed Reader)
            unconfirmed_reader_role = discord.utils.get(guild.roles, name="Unconfirmed Reader")
            if unconfirmed_reader_role:
                has_unconfirmed = unconfirmed_reader_role in member.roles

                if has_confirmed_reader and has_unconfirmed:
                    # Has both - remove Unconfirmed Reader
                    try:
                        await member.remove_roles(unconfirmed_reader_role, reason="Sync: has Confirmed Reader")
                        print(f"Removed 'Unconfirmed Reader' from {member.name} (has Confirmed Reader)")
                    except discord.Forbidden:
                        print(f"Cannot remove 'Unconfirmed Reader' from {member.name}")
                elif not has_confirmed_reader and not has_unconfirmed:
                    # Has neither - add Unconfirmed Reader
                    try:
                        await member.add_roles(unconfirmed_reader_role, reason="Sync: no Confirmed Reader")
                        print(f"Added 'Unconfirmed Reader' to {member.name} (no Confirmed Reader)")
                    except discord.Forbidden:
                        print(f"Cannot add 'Unconfirmed Reader' to {member.name}")

        if data_changed:
            self.save_verification_data()
            print("Verification data updated to match server roles")
    
    async def ensure_roles_exist(self, guild):
        """Ensure that the required verification roles exist"""
        # Check if role "0" exists
        unverified_role = discord.utils.get(guild.roles, name="0")
        if not unverified_role:
            try:
                unverified_role = await guild.create_role(
                    name="0",
                    color=discord.Color.default(),  # Colorless/transparent
                    reason="Verification system setup - unverified role"
                )
                print(f"Created role '0' in guild {guild.name}")
            except discord.Forbidden:
                print(f"Cannot create role '0' in guild {guild.name} - insufficient permissions")
                return
        
        # Check if role "1" exists
        verified_role = discord.utils.get(guild.roles, name="1")
        if not verified_role:
            try:
                verified_role = await guild.create_role(
                    name="1",
                    color=discord.Color.default(),  # Colorless/transparent
                    reason="Verification system setup - verified role"
                )
                print(f"Created role '1' in guild {guild.name}")
            except discord.Forbidden:
                print(f"Cannot create role '1' in guild {guild.name} - insufficient permissions")
                return
        
        # Check if "Confirmed Reader" role exists
        confirmed_reader_role = discord.utils.get(guild.roles, name="Confirmed Reader")
        if not confirmed_reader_role:
            try:
                confirmed_reader_role = await guild.create_role(
                    name="Confirmed Reader",
                    color=discord.Color.default(),  # Colorless/transparent
                    reason="Verification system setup - confirmed reader role"
                )
                print(f"Created role 'Confirmed Reader' in guild {guild.name}")
            except discord.Forbidden:
                print(f"Cannot create role 'Confirmed Reader' in guild {guild.name} - insufficient permissions")
                return

        # Check if "Unconfirmed Reader" role exists
        unconfirmed_reader_role = discord.utils.get(guild.roles, name="Unconfirmed Reader")
        if not unconfirmed_reader_role:
            try:
                unconfirmed_reader_role = await guild.create_role(
                    name="Unconfirmed Reader",
                    color=discord.Color.default(),  # Colorless/transparent
                    reason="Verification system setup - unconfirmed reader role"
                )
                print(f"Created role 'Unconfirmed Reader' in guild {guild.name}")
            except discord.Forbidden:
                print(f"Cannot create role 'Unconfirmed Reader' in guild {guild.name} - insufficient permissions")

        # Check if higher level roles exist (2-15) for all quiz levels
        for level in range(2, 16):
            level_role = discord.utils.get(guild.roles, name=str(level))
            if not level_role:
                try:
                    level_role = await guild.create_role(
                        name=str(level),
                        color=discord.Color.default(),  # Colorless/transparent
                        reason=f"Verification system setup - level {level} role"
                    )
                    print(f"Created role '{level}' in guild {guild.name}")
                except discord.Forbidden:
                    print(f"Cannot create role '{level}' in guild {guild.name} - insufficient permissions")
                    continue
        
        # Role ordering is left to manual management
    
    def _required_quiz_tokens(self, level: int) -> set:
        """Lowercase tokens that MUST appear in the k!quiz command for this level."""
        quiz_config = self.get_level_settings(level)['quiz']
        tokens = {
            'hardcore',
            'nd',
            'atl=16',
            'size=100',
            'dauq=1',
            f'mmq={quiz_config["max_missed"]}',
            f'font={5 if level >= 2 else 1}',
        }
        if level >= 2:
            tokens.add('effect=antiocr')
            tokens.add(f'color={quiz_config["foreground"].lower()}')
        return tokens

    def check_quiz_settings_match(self, report_data: dict, level: int = None) -> list:
        """Validate the quiz report against this level's required settings.

        Returns a list of user-readable failure reasons. Empty list means OK.
        """
        try:
            print("=== CHECKING QUIZ SETTINGS ===")

            # Auto-detect level if not provided
            if level is None:
                level = self.detect_quiz_level_from_report(report_data)
                if level is None:
                    print("FAILED: Could not detect quiz level")
                    return ["couldn't identify which Level this quiz was for"]

            print(f"Checking settings for Level {level}")

            level_settings = self.get_level_settings(level)
            if not level_settings:
                print(f"FAILED: No settings found for level {level}")
                return [f"no requirements configured for Level {level}. Contact an admin"]

            quiz_config = level_settings['quiz']
            required_deck = quiz_config['decks'][0]

            # Hard-fail: wrong deck. Nothing else is meaningful if the deck is wrong.
            report_decks = {deck['shortName'] for deck in report_data.get('decks', [])}
            print(f"Report decks: {report_decks}, Required: {quiz_config['decks']}")
            if not report_decks.issubset(set(quiz_config['decks'])):
                print("FAILED: Deck mismatch")
                actual = ', '.join(sorted(report_decks)) or 'unknown'
                expected = ', '.join(quiz_config['decks'])
                return [f"wrong deck. You ran `{actual}` but Level {level} requires `{expected}`"]

            # Hard-fail: no score recorded.
            if not report_data.get('scores') or len(report_data['scores']) == 0:
                print("FAILED: No scores found")
                return ["the quiz didn't record a score"]

            # Hard-fail: the quiz's score limit was set lower than this level requires.
            # This branch fires when the user (or Kotoba's default)
            # set a score limit below what Level N needs. Kotoba ended the quiz at
            # that lower number, which is what `score` reflects here.
            score = report_data['scores'][0].get('score', 0)
            required_score = quiz_config['score_limit']
            print(f"Score: {score}, Required: >= {required_score}")
            if score < required_score:
                print("FAILED: Score limit too low")
                return [
                    f"score limit is too low. Your quiz ended at {score} points, but "
                    f"Level {level} requires a score limit of {required_score}. "
                    f"Add `{required_score}` as the second argument: "
                    f"`k!quiz {required_deck} {required_score} ...`"
                ]

            failures: list = []
            # Track which required tokens have already been called out by a descriptive
            # settings-side bullet, so the rawStartCommand check at the end doesn't
            # duplicate them. The rawStartCommand bullet then only reports tokens that
            # don't have a more-specific message (mainly `hardcore` and `font=N`).
            settings_reported_tokens: set = set()

            settings = report_data.get('settings', {})
            print(f"All settings: {settings}")

            # Answer time limit
            time_limit = settings.get('answerTimeLimitInMs', 0)
            print(f"Time limit: {time_limit}, Required: {quiz_config['time_limit']}")
            if time_limit != quiz_config['time_limit']:
                print("FAILED: Time limit mismatch")
                failures.append("answer time limit is wrong. Add `atl=16` to your command")
                settings_reported_tokens.add('atl=16')

            # Font name is NOT verified against settings.font: Kotoba stores the
            # resolved font family name there, which depends on the running Kotoba
            # instance's installed fonts list and is not a stable mapping from `font=N`.
            # The rawStartCommand check at the bottom enforces literal `font=N` from
            # what the user typed, which is the authoritative source of intent.

            # Font size
            font_size = settings.get('fontSize', 0)
            print(f"Font size: {font_size}, Required: {quiz_config['font_size']}")
            if font_size != quiz_config['font_size']:
                print("FAILED: Font size mismatch")
                failures.append("font size is wrong. Add `size=100` to your command")
                settings_reported_tokens.add('size=100')

            # Anti-OCR effect (L2+)
            if level >= 2:
                effect = settings.get('effect')
                required_effect = quiz_config.get('effect')
                print(f"Effect: '{effect}', Required: '{required_effect}'")
                if effect != required_effect:
                    print("FAILED: Effect mismatch")
                    failures.append(f"anti-OCR effect is missing. Add `effect={required_effect}` to your command")
                    settings_reported_tokens.add(f'effect={required_effect}')

            # nd: no delay between questions
            delay_answered = settings.get('newQuestionDelayAfterAnsweredInMs')
            print(f"Delay-after-answered: {delay_answered}, Required: 0")
            if delay_answered != 0:
                print("FAILED: 'nd' missing (delay-after-answered != 0)")
                failures.append("missing `nd` flag (no delay between questions). Add `nd` to your command")
                settings_reported_tokens.add('nd')

            # dauq=1: 1-second delay after an unanswered question
            delay_unanswered = settings.get('newQuestionDelayAfterUnansweredInMs')
            print(f"Delay-after-unanswered: {delay_unanswered}, Required: 1000")
            if delay_unanswered != 1000:
                print("FAILED: 'dauq=1' missing")
                failures.append("missing `dauq=1` flag (1-second delay after an unanswered question). Add `dauq=1` to your command")
                settings_reported_tokens.add('dauq=1')

            # mmq: max missed questions before the quiz auto-ends
            required_mmq = quiz_config['max_missed']
            mmq = settings.get('maxMissedQuestions')
            print(f"Max missed: {mmq}, Required: {required_mmq}")
            if mmq != required_mmq:
                print(f"FAILED: 'mmq={required_mmq}' missing or wrong")
                failures.append(f"max-missed-questions is wrong. Add `mmq={required_mmq}` to your command")
                settings_reported_tokens.add(f'mmq={required_mmq}')

            # color: foreground text color (L2+ only; L1 doesn't enforce color)
            if level >= 2:
                required_color = quiz_config['foreground'].lower()
                font_color = (settings.get('fontColor') or "").lower()
                print(f"Font color: '{font_color}', Required: '{required_color}'")
                if font_color != required_color:
                    print(f"FAILED: 'color={required_color}' missing or wrong")
                    failures.append(f"text color is wrong. Add `color={required_color}` to your command")
                    settings_reported_tokens.add(f'color={required_color}')

            # rawStartCommand check: the only way to enforce `hardcore` (Kotoba does
            # not store it under settings). Also catches `font=N`, which is not
            # verified via settings. Skip tokens already reported by a more descriptive
            # settings-side bullet above to avoid duplication.
            raw_command = (report_data.get('rawStartCommand') or "").lower()
            print(f"rawStartCommand: {raw_command}")
            missing = self._required_quiz_tokens(level) - set(raw_command.split()) - settings_reported_tokens
            if missing:
                print(f"FAILED: rawStartCommand missing required flags: {sorted(missing)}")
                failures.append(f"your command is missing these required flags: `{', '.join(sorted(missing))}`")

            if failures:
                return failures

            print("SUCCESS: All settings match!")
            return []

        except Exception as e:
            print(f"Error checking quiz settings: {e}")
            return ["a validation error occurred. Contact an admin"]
    
    def detect_quiz_level_from_report(self, report_data: dict) -> int:
        """Detect quiz level from report data"""
        try:
            # Get deck name and score limit
            report_decks = [deck['shortName'] for deck in report_data.get('decks', [])]
            if not report_decks:
                return None
            
            deck_name = report_decks[0]  # Assume single deck
            
            if not report_data.get('scores') or len(report_data['scores']) == 0:
                return None
            
            score = report_data['scores'][0].get('score', 0)
            
            # Match deck and score to level
            if deck_name == 'vncr1' and score >= 8:
                return 1
            elif deck_name.startswith('vncr') and score >= 30:
                try:
                    level_num = int(deck_name[4:])
                    if 2 <= level_num <= 5:
                        return level_num
                except (ValueError, IndexError):
                    pass
            elif deck_name.startswith('vncr') and score >= 35:
                try:
                    level_num = int(deck_name[4:])
                    if 6 <= level_num <= 10:
                        return level_num
                except (ValueError, IndexError):
                    pass
            elif deck_name.startswith('vncr') and score >= 40:
                try:
                    level_num = int(deck_name[4:])
                    if 11 <= level_num <= 15:
                        return level_num
                except (ValueError, IndexError):
                    pass
            
            return None
        except Exception as e:
            print(f"Error detecting quiz level from report: {e}")
            return None
    
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Handle role changes and update verification data accordingly"""
        # Use user lock to prevent race conditions
        async with await self._get_user_lock(after.id):
            await self._process_member_update(before, after)
    
    async def _process_member_update(self, before: discord.Member, after: discord.Member):
        """Internal method to process member updates with lock protection"""
        guild_id = str(after.guild.id)
        guild_config = self.bot.config['guilds'].get(guild_id, {})
        
        # Only process if verification is enabled for this guild
        if not guild_config.get('verification_enabled', False):
            return
        
        # Check if roles actually changed
        if before.roles == after.roles:
            return
        
        user_id = after.id
        user_id_str = str(user_id)
        data_changed = False
        
        # Validate single quiz role first
        actual_level = await self._validate_single_quiz_role(after)
        
        # Check for quiz level role changes (0-15)
        before_level = None
        after_level = actual_level
        
        # Find before level
        for level in range(15, -1, -1):
            level_role = discord.utils.get(before.guild.roles, name=str(level))
            if level_role and level_role in before.roles:
                before_level = level
                break
        
        # Validate level changes and warn about significant downgrades
        if before_level is not None and after_level is not None:
            if after_level < before_level and before_level - after_level > 1:
                print(f"WARNING: {after.name} downgraded from {before_level} to {after_level}")
        
        # Update quiz level data if it changed
        if before_level != after_level and after_level is not None:
            current_tracked = self.verification_data["highest_quiz_levels"].get(user_id_str, None)
            if current_tracked != after_level:
                self.verification_data["highest_quiz_levels"][user_id_str] = after_level
                print(f"Auto-updated {after.name} quiz level: {current_tracked} → {after_level}")
                data_changed = True
        
        # Check for Confirmed Reader role changes
        confirmed_reader_role = discord.utils.get(after.guild.roles, name="Confirmed Reader")
        unconfirmed_reader_role = discord.utils.get(after.guild.roles, name="Unconfirmed Reader")
        if confirmed_reader_role:
            had_confirmed = confirmed_reader_role in before.roles
            has_confirmed = confirmed_reader_role in after.roles
            is_in_verified_list = user_id_str in self.verification_data["verified_users"]

            if has_confirmed and not had_confirmed:
                # Gained Confirmed Reader role - add to verified list
                if not is_in_verified_list:
                    self.verification_data["verified_users"].append(user_id_str)
                    print(f"Auto-added {after.name} to verified list (gained Confirmed Reader)")
                    data_changed = True

                # Remove Unconfirmed Reader if they have it (roles are mutually exclusive)
                if unconfirmed_reader_role and unconfirmed_reader_role in after.roles:
                    try:
                        await after.remove_roles(unconfirmed_reader_role, reason="Gained Confirmed Reader")
                        print(f"Removed 'Unconfirmed Reader' from {after.name} (gained Confirmed Reader)")
                    except discord.Forbidden:
                        print(f"Cannot remove 'Unconfirmed Reader' from {after.name}")

            elif not has_confirmed and had_confirmed:
                # Lost Confirmed Reader role - remove from verified list
                if is_in_verified_list:
                    self.verification_data["verified_users"].remove(user_id_str)
                    print(f"Auto-removed {after.name} from verified list (lost Confirmed Reader)")
                    data_changed = True

                # Add Unconfirmed Reader if they don't have it
                if unconfirmed_reader_role and unconfirmed_reader_role not in after.roles:
                    try:
                        await after.add_roles(unconfirmed_reader_role, reason="Lost Confirmed Reader")
                        print(f"Added 'Unconfirmed Reader' to {after.name} (lost Confirmed Reader)")
                    except discord.Forbidden:
                        print(f"Cannot add 'Unconfirmed Reader' to {after.name}")

        if data_changed:
            self.save_verification_data()
    
    async def get_user_class_info(self, member: discord.Member):
        """Get user's VN class information for welcome message"""
        try:
            immersion_cog = self.bot.get_cog("ImmersionCog")
            if not immersion_cog:
                return None
            
            # Get VN hours
            user_id = str(member.id)
            vn_seconds = await immersion_cog.activity_repo.sum_activity_duration(
                user_id, 
                media_type="visual_novel"
            )
            vn_hours = (vn_seconds or 0) / 3600
            
            # Get class definitions
            class_definitions = immersion_cog._get_class_role_definitions_for_guild(member.guild)
            
            # Find current class
            current_class = "魑魅魍魎"  # Default base class
            for definition in class_definitions:
                if vn_hours >= definition["min_hours"]:
                    current_class = definition["name"]
            
            return {
                "class_name": current_class,
                "vn_hours": vn_hours
            }
        except Exception as e:
            print(f"Error getting class info for {member.name}: {e}")
            return None
    
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Handle new member joins and restore roles for returning users"""
        guild_id = str(member.guild.id)
        guild_config = self.bot.config['guilds'].get(guild_id, {})
        
        # Check if verification is enabled for this guild
        if not guild_config.get('verification_enabled', False):
            return
        
        verification_channel_id = guild_config.get('verification_channel')
        
        if not verification_channel_id:
            return
        
        # Ensure roles exist
        await self.ensure_roles_exist(member.guild)
        
        # Check if this user has previously passed any quiz levels
        user_id = member.id
        highest_level = self.get_highest_quiz_level(user_id)
        
        # Check if user has tracked quiz data (not just default 0 return)
        user_id_str = str(user_id)
        has_tracked_data = user_id_str in self.verification_data["highest_quiz_levels"]
        
        if has_tracked_data and highest_level > 0:
            # User has a tracked quiz level above 0, restore their roles
            await self.restore_user_roles(member, highest_level)
            
            # Get class role information from immersion cog
            class_role_info = await self.get_user_class_info(member)
            
            # Send welcome back message to welcome channel (plain text format)
            welcome_channel_id = self.bot.config['guilds'].get(guild_id, {}).get('welcome_channel')

            if welcome_channel_id:
                welcome_channel = member.guild.get_channel(int(welcome_channel_id))
                if welcome_channel:
                    try:
                        welcome_parts = [f"🔄 Welcome back, {member.mention}!"]
                        welcome_parts.append(f"Your **Level {highest_level}** role has been restored.")
                        
                        if class_role_info:
                            welcome_parts.append(f"Your **{class_role_info['class_name']}** class role has been restored ({class_role_info['vn_hours']:.1f}h VN time).")
                        
                        welcome_back_text = " ".join(welcome_parts)
                        await welcome_channel.send(welcome_back_text)
                    except discord.Forbidden:
                        pass
                    except Exception as e:
                        print(f"Error sending welcome message: {e}")
        elif has_tracked_data and highest_level == 0:
            # User has tracked data but is at level 0, restore roles
            await self.restore_user_roles(member, highest_level)
            
            # Check if they were a verified user (had server access before)
            user_id_str = str(user_id)
            was_verified = user_id_str in self.verification_data["verified_users"]
            
            if was_verified:
                # Get class role information from immersion cog
                class_role_info = await self.get_user_class_info(member)
                
                # Send welcome back message for returning verified users at level 0
                welcome_channel_id = self.bot.config['guilds'].get(guild_id, {}).get('welcome_channel')

                if welcome_channel_id:
                    welcome_channel = member.guild.get_channel(int(welcome_channel_id))
                    if welcome_channel:
                        try:
                            welcome_parts = [f"🔄 Welcome back, {member.mention}!"]
                            welcome_parts.append("Your server access has been restored.")
                            
                            if class_role_info:
                                welcome_parts.append(f"Your **{class_role_info['class_name']}** class role has been restored ({class_role_info['vn_hours']:.1f}h VN time).")
                            
                            welcome_back_text = " ".join(welcome_parts)
                            await welcome_channel.send(welcome_back_text)
                        except discord.Forbidden:
                            pass
                        except Exception as e:
                            print(f"Error sending welcome message: {e}")
        else:
            # New user, assign unverified role
            unverified_role = discord.utils.get(member.guild.roles, name="0")
            
            if not unverified_role:
                print(f"Role '0' not found in guild {member.guild.name}")
                return
            
            # Assign unverified role
            try:
                await member.add_roles(unverified_role)
                print(f"Assigned unverified role to new user {member.name}")
            except discord.Forbidden:
                print(f"Cannot assign unverified role to {member.name}")
        
        # Note: Welcome message is handled by events.py to avoid duplication
    
    async def check_quiz_start(self, message):
        """Check if a user started a Level 1 quiz"""
        # Only process messages from actual users, not bots
        if message.author.bot:
            return
            
        guild_id = str(message.guild.id)
        guild_config = self.bot.config['guilds'].get(guild_id, {})
        
        # Check if verification is enabled for this guild
        if not guild_config.get('verification_enabled', False):
            return
        
        # Parse the quiz command
        command_parts = message.content.split()
        if len(command_parts) < 2:
            return

        # k!quiz <deck> [score] [flags...]
        if command_parts[0] == 'k!quiz':
            deck = command_parts[1]
            score_target = command_parts[2] if len(command_parts) > 2 else ""

            # Detect the VNCR level from the deck name alone (vncr1..vncr15). The score
            # is validated separately so a missing or wrong score still gets a warning.
            detected_level = None
            if deck.startswith('vncr'):
                try:
                    level_num = int(deck[4:])
                    if 1 <= level_num <= 15:
                        detected_level = level_num
                except (ValueError, IndexError):
                    pass

            if detected_level:
                typed_tokens = {p.lower() for p in command_parts}
                missing = self._required_quiz_tokens(detected_level) - typed_tokens

                expected_score = str(self.get_level_settings(detected_level)['quiz']['score_limit'])
                wrong_score = score_target != expected_score

                if missing or wrong_score:
                    problems = []
                    if wrong_score:
                        problems.append(
                            f"score limit must be `{expected_score}`. Set it as the "
                            f"second argument: `k!quiz {deck} {expected_score} ...`"
                        )
                    if missing:
                        problems.append(
                            f"your command is missing these required flags: "
                            f"`{', '.join(sorted(missing))}`"
                        )
                    bullets = "\n".join(f"• {p}" for p in problems)
                    await message.channel.send(
                        f"⚠️ {message.author.mention}, your Level {detected_level} quiz command isn't valid:\n"
                        f"{bullets}\n"
                        f"If you run this quiz it won't count toward your level. "
                        f"Use `/quiz_progress` to copy the correct command."
                    )
                    return

                # Command is fully valid. Announce promotion/skip/retake based on current level.
                user_id = message.author.id
                current_level = self.get_highest_quiz_level(user_id)
                expected_next_level = current_level + 1

                if detected_level == expected_next_level:
                    if detected_level == 1:
                        await message.channel.send(
                            f"🎯 **VERIFICATION QUIZ** started by {message.author.mention}!\n"
                            f"Good luck! Unlimited tries, no cooldown.\n"
                            f"Guide: <https://vnclub.figamin.net/guide/> · Word list: <https://vnclub.figamin.net/level1/>"
                        )
                    else:
                        await message.channel.send(
                            f"🎯 **PROMOTION QUIZ** started by {message.author.mention}! "
                            f"Level {detected_level} ({current_level} → {detected_level}) - This will advance your level!"
                        )
                elif detected_level > expected_next_level:
                    await message.channel.send(
                        f"⚠️ Level {detected_level} quiz started by {message.author.mention} "
                        f"(Current: {current_level}) - **Warning: You must complete Level {expected_next_level} first!**"
                    )
                elif detected_level <= current_level:
                    await message.channel.send(
                        f"🔄 Level {detected_level} quiz started by {message.author.mention} "
                        f"(Current: {current_level}) - **Retake: No level progression**"
                    )
                else:
                    await message.channel.send(f"🎯 Level {detected_level} quiz started by {message.author.mention}!")
    
    async def check_quiz_failure(self, message, report_data, level=1, failures=None):
        """Report a quiz that didn't meet the level's requirements, listing every reason."""
        try:
            participants = report_data.get('participants', [])
            if len(participants) != 1:
                return
            user_id = int(participants[0]['discordUser']['id'])
            member = message.guild.get_member(user_id)
            if not member:
                return

            if failures:
                bullets = "\n".join(f"• {reason}" for reason in failures)
                await message.channel.send(
                    f"❌ {member.mention}, your Level {level} quiz didn't count toward verification because:\n"
                    f"{bullets}\n"
                    f"Use `/quiz_progress` to copy the correct command, then try again."
                )
            else:
                await message.channel.send(
                    f"❌ {member.mention}, your Level {level} quiz didn't count toward verification. "
                    f"Use `/quiz_progress` to copy the correct command, then try again."
                )
        except Exception as e:
            print(f"Error reporting quiz failure: {e}")
    
    @staticmethod
    def _quiz_taker_from_embed(embed, message):
        """The id of whoever took the quiz, read from the mention Kotoba echoes back."""
        import re as _re
        for text in (getattr(embed, "description", "") or "", message.content or ""):
            found = _re.findall(r"<@!?(\d+)>", text)
            if found:
                return int(found[0])
        return None

    async def check_quiz_ended_failure(self, message, embed):
        """Check if this was a VNCR quiz that ended prematurely (any level 1-15)"""
        try:
            guild_id = str(message.guild.id)
            guild_config = self.bot.config['guilds'].get(guild_id, {})
            
            # Check if verification is enabled for this guild
            if not guild_config.get('verification_enabled', False):
                return
            
            # Get embed content
            embed_title = embed.title or ""
            embed_text = embed.description or ""
            
            # Also check embed fields for more context
            for field in embed.fields:
                embed_text += f" {field.name} {field.value}"
            
            
            # The calling function has already established this is a VNCR quiz
            # Just extract the level number from the title
            import re
            vncr_pattern = re.compile(r'VNCR.*?Level (\d+)', re.IGNORECASE)
            level_match = vncr_pattern.search(embed_title)
            
            detected_level = None
            if level_match:
                detected_level = int(level_match.group(1))
            else:
                # Fallback: try direct VNCR pattern
                vncr_pattern2 = re.compile(r'VNCR(\d+)', re.IGNORECASE)
                level_match2 = vncr_pattern2.search(embed_title)
                if level_match2:
                    detected_level = int(level_match2.group(1))
            
            if not detected_level:
                return
            
            # Check if this was actually a quiz failure
            full_text = f"{embed_title} {embed_text}"
            
            # Check for failure indicators
            failure_indicators = [
                "questions total weren't answered",
                "So I stopped!",
                "Unanswered Questions",
                "wasn't answered", 
                "stopped",
                "gave up",
                "quit",
                "timeout"
            ]
            
            found_indicator = None
            for indicator in failure_indicators:
                if indicator.lower() in full_text.lower():
                    found_indicator = indicator
                    break
            
            if found_indicator:
                # Failures are recorded alongside passes so that "nobody reached the quiz",
                # "everyone is failing it" and "Kotoba stopped answering" stay distinct in
                # the data.
                try:
                    quiz_taker = self._quiz_taker_from_embed(embed, message)
                    if quiz_taker:
                        self.record_quiz_attempt(
                            quiz_taker, f"vncr{detected_level}", "FAILED"
                        )
                        self.save_verification_data()
                except Exception as exc:
                    print(f"Could not record failed attempt: {exc}")
                # Send appropriate failure message for the detected level with encouragement
                if detected_level == 1:
                    await message.channel.send(
                        f"❌ Level 1 quiz incomplete. Don't give up - unlimited tries, no cooldown!\n"
                        f"Guide: <https://vnclub.figamin.net/guide/> · Word list: <https://vnclub.figamin.net/level1/>\n"
                        f"`/quiz_verify` for the quiz command"
                    )
                else:
                    await message.channel.send(
                        f"❌ Level {detected_level} quiz incomplete - Some questions went unanswered.\n\n"
                        f"Use `/quiz_progress` to see the command again."
                    )
                    
        except Exception as e:
            print(f"Error checking quiz ended failure: {e}")
            import traceback
            print(f"Full traceback: {traceback.format_exc()}")
    
    async def check_quiz_completion_success(self, message, embed):
        """Handle successful quiz completion with score limit reached"""
        try:
            print(f"=== QUIZ COMPLETION SUCCESS DETECTED ===")
            print(f"Embed title: {embed.title}")
            print(f"Embed description: {embed.description}")
            
            guild_id = str(message.guild.id)
            guild_config = self.bot.config['guilds'].get(guild_id, {})
            
            # Check if verification is enabled for this guild
            if not guild_config.get('verification_enabled', False):
                print("Verification not enabled for this guild")
                return
            
            # Check if this is a VNCR1 quiz (Level 1 attempt)
            # Use a more flexible approach to detect quiz completions
            embed_title = embed.title or ""
            embed_description = str(embed.description or "").lower()
            
            # Check for completion indicators for any level
            embed_title = embed.title or ""
            embed_description = str(embed.description or "").lower()
            
            detected_level = None
            
            # Detect level from title (e.g., "VNCR Level X Quiz")
            if "vncr" in embed_title.lower() and "level" in embed_title.lower():
                import re
                level_match = re.search(r'level (\d+)', embed_title.lower())
                if level_match:
                    try:
                        detected_level = int(level_match.group(1))
                        print(f"Detected Level {detected_level} from title: '{embed_title}'")
                    except ValueError:
                        pass
            
            # Also check for score limits in description
            if not detected_level:
                if "score limit of 8 was reached" in embed_description:
                    detected_level = 1
                elif "score limit of 30 was reached" in embed_description:
                    # Could be levels 2-5; narrowed down later
                    detected_level = "2-5"
                elif "score limit of 35 was reached" in embed_description:
                    # Could be levels 6-10
                    detected_level = "6-10"
                elif "score limit of 40 was reached" in embed_description:
                    # Could be levels 11-15
                    detected_level = "11-15"
                
                print(f"Detected level range from score limit: {detected_level}")
            
            if detected_level:
                print(f"Processing quiz completion for level: {detected_level}")
                # Process this as a successful completion directly
                # No need to wait for the "View a report" message
                await self.process_successful_completion(message, embed, detected_level)
                    
        except Exception as e:
            print(f"Error checking quiz completion success: {e}")
    
    async def process_successful_completion(self, message, embed, detected_level):
        """Process a successful quiz completion directly from the completion embed"""
        try:
            print(f"Processing successful Level {detected_level} completion directly...")
            
            # If detected_level is a range, determine the exact level from the embed
            if isinstance(detected_level, str):
                # Extract actual level from embed title or content
                embed_title = embed.title or ""
                if "vncr" in embed_title.lower() and "level" in embed_title.lower():
                    import re
                    level_match = re.search(r'level (\d+)', embed_title.lower())
                    if level_match:
                        try:
                            detected_level = int(level_match.group(1))
                        except ValueError:
                            print(f"Could not determine exact level from range {detected_level}")
                            return
                else:
                    print(f"Could not determine exact level from range {detected_level}")
                    return
            
            print(f"Processing completion for Level {detected_level}")
            
            # Extract user information from the embed description or message content
            # Look for mentions in both the embed description and the original message
            user_mentions = []
            
            # Check embed description for mentions
            description = embed.description or ""
            if description:
                import re
                user_mentions.extend(re.findall(r'<@!?(\d+)>', description))
            
            # Also check the original message content for mentions
            if message.content:
                import re
                user_mentions.extend(re.findall(r'<@!?(\d+)>', message.content))
            
            # Remove duplicates
            user_mentions = list(set(user_mentions))
            
            print(f"Found mentions: {user_mentions}")
            
            if user_mentions:
                # Process the first mentioned user (should be the quiz taker)
                user_id = int(user_mentions[0])
                member = message.guild.get_member(user_id)
                if member:
                    print(f"Found user: {member.name} (ID: {user_id})")
                    
                    # Ensure roles exist
                    await self.ensure_roles_exist(message.guild)
                    
                    # Find roles by name
                    verified_role = discord.utils.get(message.guild.roles, name=str(detected_level))
                    unverified_role = discord.utils.get(message.guild.roles, name="0")
                    confirmed_reader_role = discord.utils.get(message.guild.roles, name="Confirmed Reader")
                    
                    if not verified_role or not confirmed_reader_role:
                        print(f"Required roles not found")
                        return
                    
                    # Check if user is already verified at this level or higher
                    current_highest = self.get_highest_quiz_level(user_id)
                    if current_highest >= detected_level and verified_role in member.roles:
                        await message.channel.send(f"{member.mention} has already completed Level {detected_level} or higher!")
                        return
                    
                    # Process the verification
                    await self.complete_verification(message, member, verified_role, unverified_role, confirmed_reader_role, level=detected_level)
                else:
                    print(f"User with ID {user_id} not found in guild")
            else:
                print(f"No user mentions found in quiz completion message")
                print(f"Message content: {message.content[:200]}...")
                print(f"Embed description: {description[:200]}...")
                        
        except Exception as e:
            print(f"Error processing successful Level {detected_level} completion: {e}")
    
    async def complete_verification(self, message, member, verified_role, unverified_role, confirmed_reader_role, level=1):
        """Complete the verification process for a user"""
        # Use user lock to prevent race conditions during verification
        async with await self._get_user_lock(member.id):
            # An open application routes an entry pass into manual review instead of
            # granting access. This sits at the shared entry point rather than in the
            # callers because a pass arrives by two routes, the completion embed and the
            # report link, and both grant. Level progression for an existing member has
            # no open application and falls through to the direct grant path.
            onboarding = self.bot.get_cog("OnboardingCog")
            if onboarding is not None:
                try:
                    if await onboarding.intercept_quiz_pass(member, level):
                        return
                except Exception as exc:
                    print(f"Onboarding handoff failed, granting directly: {exc}")

            await self._process_verification(message, member, verified_role, unverified_role, confirmed_reader_role, level)
    
    async def _process_verification(self, message, member, verified_role, unverified_role, confirmed_reader_role, level=1):
        """Internal method to process verification with lock protection"""
        try:
            user_id = member.id
            
            # Get the quiz settings for this level
            level_settings = self.get_level_settings(level)
            if not level_settings:
                print(f"No settings found for level {level}")
                return
            
            # Record the successful attempt
            quiz_command = level_settings['command']
            self.record_quiz_attempt(user_id, quiz_command, "PASSED")
            
            # Check if user had server access BEFORE this verification is processed
            had_server_access_before = confirmed_reader_role and confirmed_reader_role in member.roles
            
            # Get current highest level before updating
            current_highest = self.get_highest_quiz_level(user_id)
            
            # Check if user is trying to skip levels (enforce sequential progression)
            expected_next_level = current_highest + 1
            if level > expected_next_level:
                # User is trying to skip levels
                await message.channel.send(
                    f"❌ {member.mention}, you must complete levels in order! "
                    f"Your current level is {current_highest}. "
                    f"Please complete Level {expected_next_level} next. "
                    f"Use `/quiz_progress` to see the required quiz."
                )
                return
            elif level <= current_highest:
                # User is retaking a level they've already completed
                await message.channel.send(
                    f"✅ {member.mention}, you've already completed Level {level}! "
                    f"Your current highest level is {current_highest}. "
                    f"Use `/quiz_progress` to see your next challenge."
                )
                return
            
            # Perform role updates with transaction-like behavior
            success = await self._perform_role_updates(member, verified_role, unverified_role, confirmed_reader_role, level)
            
            if success:
                # Only update data after successful role changes
                self.set_highest_quiz_level(user_id, level)
                self.add_verified_user(user_id)
                
                # Send success messages
                await self._send_verification_success_messages(message, member, level, level_settings, current_highest, had_server_access_before)
            else:
                await message.channel.send(f"❌ Error during verification for {member.mention}. Please contact an administrator.")
                
        except Exception as e:
            print(f"Error completing verification: {e}")
            await message.channel.send(f"❌ Verification failed for {member.mention}. Please try again or contact an administrator.")
    
    async def _perform_role_updates(self, member, verified_role, unverified_role, confirmed_reader_role, level):
        """Perform all role updates with rollback capability"""
        try:
            # Store original roles for potential rollback
            original_roles = list(member.roles)
            
            # Update highest quiz level achieved
            final_highest = level
            
            # Remove ALL other quiz level roles - user should only have their highest level role
            for check_level in range(0, 16):  # Check levels 0-15
                if check_level != final_highest:  # Remove any role that's not their highest
                    level_role = discord.utils.get(member.guild.roles, name=str(check_level))
                    if level_role and level_role in member.roles:
                        try:
                            await member.remove_roles(level_role)
                            print(f"Removed quiz level role '{check_level}' from {member.name}")
                        except discord.Forbidden:
                            print(f"Cannot remove role '{check_level}' from {member.name}")
            
            # Assign the role for their HIGHEST level
            highest_level_role = discord.utils.get(member.guild.roles, name=str(final_highest))
            if highest_level_role and highest_level_role not in member.roles:
                try:
                    await member.add_roles(highest_level_role)
                    print(f"Assigned highest level role '{final_highest}' to {member.name}")
                except discord.Forbidden:
                    print(f"Cannot assign highest level role to {member.name}")
                    return False
            
            # Assign the "Confirmed Reader" role (only if they don't have it)
            if confirmed_reader_role and confirmed_reader_role not in member.roles:
                try:
                    await member.add_roles(confirmed_reader_role)
                    print(f"Assigned 'Confirmed Reader' role to {member.name}")
                except discord.Forbidden:
                    print(f"Cannot assign Confirmed Reader role to {member.name}")
                    return False
            
            # Remove unverified role if they have it
            if unverified_role and unverified_role in member.roles:
                try:
                    await member.remove_roles(unverified_role)
                    print(f"Removed role '0' from {member.name}")
                except discord.Forbidden:
                    print(f"Cannot remove unverified role from {member.name}")

            # Remove "Unconfirmed Reader" role if they have it
            unconfirmed_reader_role = discord.utils.get(member.guild.roles, name="Unconfirmed Reader")
            if unconfirmed_reader_role and unconfirmed_reader_role in member.roles:
                try:
                    await member.remove_roles(unconfirmed_reader_role)
                    print(f"Removed 'Unconfirmed Reader' role from {member.name}")
                except discord.Forbidden:
                    print(f"Cannot remove Unconfirmed Reader role from {member.name}")

            return True
            
        except Exception as e:
            print(f"Error during role updates for {member.name}: {e}")
            return False
    
    async def _send_verification_success_messages(self, message, member, level, level_settings, current_highest, had_server_access_before):
        """Send verification success messages"""
        try:
            final_highest = self.get_highest_quiz_level(member.id)
            quiz_info = level_settings['quiz_info']
            quiz_command = level_settings['command']
            
            # Check if this was actually a new achievement
            role_actually_changed = current_highest != final_highest
            is_first_time_verification = level == 1 and not had_server_access_before
            
            # Determine which command to show
            # Level 1: Show the command they just used (for reference)
            # Levels 2-15: Show the NEXT level's command (if available)
            command_to_show = quiz_command
            command_label = "Command Used"
            
            if level > 1 and level < 15:
                # Get next level's settings
                next_level_settings = self.get_level_settings(level + 1)
                if next_level_settings:
                    command_to_show = next_level_settings['command']
                    command_label = f"Next Challenge (Level {level + 1})"
            elif level == 15:
                # Max level reached - show completion message instead
                command_to_show = None
                command_label = None
            
            if is_first_time_verification:
                # First-time verification
                embed = discord.Embed(
                    title="🎉 Verification Complete!",
                    description=f"Congratulations {member.mention}! You have successfully completed the **Level {level}** Japanese vocabulary quiz and are now verified!",
                    color=discord.Color.green()
                )
                embed.add_field(name="Quiz Level", value=f"Level {level} ({quiz_info['deck_display']})", inline=True)
                embed.add_field(name="Score Achieved", value=f"{level_settings['quiz']['score_limit']} points reached", inline=True)
                embed.add_field(name="Status", value=f"✅ PASSED - Role '{final_highest}' + Server Access", inline=True)
                
                dm_message = f"🎉 Congratulations! You have completed Level {level} and gained server access!"
            elif role_actually_changed:
                # New higher level achievement
                embed = discord.Embed(
                    title="🎆 New Level Achieved!",
                    description=f"Outstanding {member.mention}! You have successfully completed **Level {level}** and achieved a new highest level!",
                    color=discord.Color.gold()
                )
                embed.add_field(name="Quiz Level", value=f"Level {level} ({quiz_info['deck_display']})", inline=True)
                embed.add_field(name="Score Achieved", value=f"{level_settings['quiz']['score_limit']} points reached", inline=True)
                embed.add_field(name="Status", value=f"✅ PASSED - Role Updated to '{final_highest}'", inline=True)
                
                dm_message = f"🎆 Outstanding! You have completed Level {level} and achieved a new highest level!"
            else:
                # Retaking a level
                embed = discord.Embed(
                    title="✅ Quiz Complete!",
                    description=f"Well done {member.mention}! You have successfully completed the **Level {level}** quiz!",
                    color=discord.Color.blue()
                )
                embed.add_field(name="Quiz Level", value=f"Level {level} ({quiz_info['deck_display']})", inline=True)
                embed.add_field(name="Score Achieved", value=f"{level_settings['quiz']['score_limit']} points reached", inline=True)
                embed.add_field(name="Status", value=f"✅ PASSED - Role '{final_highest}' (No Change)", inline=True)
                
                dm_message = f"✅ Well done! You completed Level {level}."
            
            # Add the command field
            if command_to_show and command_label:
                embed.add_field(name=command_label, value=f"```{command_to_show}```", inline=False)
            elif level == 15:
                # Max level reached
                embed.add_field(name="🏆 Maximum Level", value="Congratulations! You've completed all 15 levels!", inline=False)
            
            await message.channel.send(embed=embed)
            
            # Send announcement to welcome channel if it's a new achievement
            if role_actually_changed:
                await self._send_welcome_announcement(message, member, level, had_server_access_before)
            
            # Send DM to user
            try:
                await member.send(dm_message)
            except discord.Forbidden:
                pass  # User has DMs disabled
                
        except Exception as e:
            print(f"Error sending verification success messages: {e}")
    
    async def _send_welcome_announcement(self, message, member, level, had_server_access_before):
        """Send announcement to welcome channel"""
        try:
            guild_id = str(message.guild.id)
            welcome_channel_id = self.bot.config['guilds'].get(guild_id, {}).get('welcome_channel')
            if welcome_channel_id:
                welcome_channel = message.guild.get_channel(int(welcome_channel_id))
                if welcome_channel:
                    if level == 1 and not had_server_access_before:
                        notification_text = f"🎉 **Level Up** · Welcome to the server, {member.mention}! You've reached **Level {level}**!"
                    else:
                        notification_text = f"🎯 **Level Up** · {member.mention} has reached **Level {level}**!"
                    await welcome_channel.send(notification_text)
        except Exception as e:
            print(f"Error sending welcome announcement: {e}")

    def get_level_settings(self, level):
        """Get quiz settings for a specific level"""
        if level == 1:
            return {
                'quiz': self.LEVEL_1_QUIZ,
                'command': self.LEVEL_1_COMMAND,
                'quiz_info': {
                    'deck_display': 'VNCR1',
                    'description': 'Basic Japanese vocabulary'
                }
            }
        elif 2 <= level <= 5:
            return {
                'quiz': self.LEVEL_2_5_QUIZZES[level],
                'command': self.LEVEL_2_5_COMMANDS[level],
                'quiz_info': {
                    'deck_display': f'VNCR{level}',
                    'description': f'Visual Novel Japanese vocabulary - Level {level}'
                }
            }
        elif 6 <= level <= 10:
            return {
                'quiz': self.LEVEL_6_10_QUIZZES[level],
                'command': self.LEVEL_6_10_COMMANDS[level],
                'quiz_info': {
                    'deck_display': f'VNCR{level}',
                    'description': f'Visual Novel Japanese vocabulary - Level {level}'
                }
            }
        elif 11 <= level <= 15:
            return {
                'quiz': self.LEVEL_11_15_QUIZZES[level],
                'command': self.LEVEL_11_15_COMMANDS[level],
                'quiz_info': {
                    'deck_display': f'VNCR{level}',
                    'description': f'Visual Novel Japanese vocabulary - Level {level}'
                }
            }
        return None
    
    def detect_quiz_level(self, command):
        """Detect which level quiz is being attempted based on the command"""
        # Level 1 (vncr1 8)
        if 'vncr1' in command and '8' in command:
            return 1
        
        # Levels 2-5 (vncr2-5 30)
        for level in range(2, 6):
            if f'vncr{level}' in command and '30' in command:
                return level
        
        # Levels 6-10 (vncr6-10 35)
        for level in range(6, 11):
            if f'vncr{level}' in command and '35' in command:
                return level
        
        # Levels 11-15 (vncr11-15 40)
        for level in range(11, 16):
            if f'vncr{level}' in command and '40' in command:
                return level
        
        return 1  # Default to level 1
    
    @commands.Cog.listener()
    async def on_message(self, message):
        """Monitor messages for Kotoba bot quiz starts, completions, and failures"""
        # Check for quiz start commands from users
        if message.author.id != self.KOTOBA_ID and message.content.startswith('k!quiz'):
            await self.check_quiz_start(message)
            return
        
        # Only process other messages from Kotoba bot
        if message.author.id != self.KOTOBA_ID:
            return
        
        print(f"Processing Kotoba message with {len(message.embeds)} embeds")
        
        # Check for quiz completion messages (both success and failure)
        if message.embeds:
            for embed in message.embeds:
                print(f"Embed title: '{embed.title}', Description preview: '{str(embed.description)[:100]}...'")
                if embed.title and "Quiz Ended" in embed.title:
                    # More flexible detection for all quiz levels
                    embed_title = embed.title or ""
                    embed_description = str(embed.description or "").lower()
                    
                    # Check if this is a VNCR quiz - look for VNCR pattern directly
                    is_vncr_quiz = False
                    detected_level = None
                    
                    # First try to detect from VNCR pattern in title
                    import re
                    vncr_pattern = re.compile(r'VNCR(\d+)', re.IGNORECASE)
                    vncr_match = vncr_pattern.search(embed_title)
                    
                    if vncr_match:
                        is_vncr_quiz = True
                        try:
                            detected_level = int(vncr_match.group(1))
                        except ValueError:
                            pass
                    elif "VNCR" in embed_title and "Level" in embed_title:
                        # Fallback: look for "Level X" pattern
                        is_vncr_quiz = True
                        level_match = re.search(r'Level (\d+)', embed_title)
                        if level_match:
                            try:
                                detected_level = int(level_match.group(1))
                            except ValueError:
                                pass
                    
                    if is_vncr_quiz and detected_level:
                        # Distinguish "quiz reached SOME score limit" (Kotoba-side completion,
                        # may still be a muramasa-side fail if the score is wrong or flags are
                        # missing) from "quiz aborted" (timeout / k!stop / unanswered questions).
                        # Any numeric score is accepted here on purpose; the validator will
                        # return "score too low" if the user used the wrong score limit.
                        if re.search(r"score limit of \d+ was reached", embed_description):
                            print(f"Detected possible Level {detected_level} completion; validating via report.")
                            # fall through to the report-link path below
                        else:
                            # Genuine quiz abort (stop/timeout). Give the user the "incomplete" feedback.
                            print(f"Detected aborted Level {detected_level} quiz. Title: {embed_title}")
                            await self.check_quiz_ended_failure(message, embed)
                            return
        
        # Check if message has embeds with quiz reports
        if not message.embeds:
            print("No embeds in Kotoba message")
            return
        
        # Look for "View a report" links in embed fields and description
        report_links = []
        for embed in message.embeds:
            print(f"Checking embed: {embed.title}")
            print(f"Embed description: {embed.description}")
            
            # Check embed description for report links
            if embed.description and 'View a report' in embed.description:
                # Extract the link from the description
                description_lines = embed.description.split('\n')
                for line in description_lines:
                    if 'View a report' in line:
                        report_links.append(line)
                        print(f"Found report link in description: {line}")
            
            # Check embed fields
            for i, field in enumerate(embed.fields):
                print(f"Field {i}: name='{field.name}', value='{field.value}'")
                if 'View a report' in field.value:
                    report_links.append(field.value)
                    print(f"Found report link in field: {field.value}")
                elif 'game_reports' in field.value:
                    # The link text can vary
                    report_links.append(field.value)
                    print(f"Found report link (alternative): {field.value}")
        
        if not report_links:
            print("No report links found in Kotoba message")
            return
        
        print(f"Processing {len(report_links)} report links")
        # Process each report link
        for link in report_links:
            await self.process_quiz_report(message, link)
    
    async def process_quiz_report(self, message, report_link):
        """Process a quiz report from Kotoba"""
        try:
            print(f"Starting to process quiz report: {report_link}")
            # Extract report ID from the link
            match = re.search(r'game_reports/([^)]*)\)', report_link)
            if not match:
                print("Failed to extract report ID from link")
                return
            
            report_id = match.group(1)
            print(f"Extracted report ID: {report_id}")
            
            # Fetch the quiz report from Kotoba API. Off-loop with a timeout so a
            # slow upstream can't stall the whole bot.
            url = f'https://kotobaweb.com/api/game_reports/{report_id}'
            print(f"Fetching from: {url}")
            try:
                response = await asyncio.to_thread(requests.get, url, timeout=10)
            except requests.RequestException as e:
                print(f"Failed to fetch quiz report: {e}")
                return
            if response.status_code != 200:
                print(f"Failed to fetch quiz report: {response.status_code}")
                return
            
            report_data = response.json()
            print(f"Successfully fetched quiz report data")

            # Only continue if this is a VNCR-themed quiz. Kotoba posts report links
            # for every quiz, but muramasa should stay silent for unrelated decks.
            report_decks = [d.get('shortName', '') for d in report_data.get('decks', [])]
            if not any(d.lower().startswith('vncr') for d in report_decks):
                print(f"Non-VNCR deck(s) {report_decks}, skipping verification flow")
                return

            # Get the participant first to determine their current level
            participants = report_data.get('participants', [])
            if len(participants) != 1:
                await message.channel.send("This quiz has multiple participants and cannot be used for verification.")
                return
            
            # Get the user who took the quiz
            user_id = int(participants[0]['discordUser']['id'])
            member = message.guild.get_member(user_id)
            if not member:
                print(f"Member {user_id} not found in guild")
                return
            
            # Get user's current level and determine expected level
            current_level = self.get_highest_quiz_level(user_id)
            expected_level = current_level + 1
            
            print(f"User {member.name} current level: {current_level}, expected next: {expected_level}")
            
            # Check if this matches the requirements for their expected level
            failures = self.check_quiz_settings_match(report_data, expected_level)
            print(f"Settings match for level {expected_level}: {'PASS' if not failures else f'FAIL ({failures})'}")
            if failures:
                await self.check_quiz_failure(message, report_data, expected_level, failures)
                return
            
            # Ensure roles exist
            await self.ensure_roles_exist(message.guild)
            
            # Find roles by name for the expected level
            verified_role = discord.utils.get(message.guild.roles, name=str(expected_level))
            unverified_role = discord.utils.get(message.guild.roles, name="0")
            confirmed_reader_role = discord.utils.get(message.guild.roles, name="Confirmed Reader")
            
            if not verified_role:
                print(f"Role '{expected_level}' not found in guild {message.guild.name}")
                return
            
            if not confirmed_reader_role:
                print(f"Role 'Confirmed Reader' not found in guild {message.guild.name}")
                return
            
            # Check if user is already verified at this level or higher
            current_highest = self.get_highest_quiz_level(user_id)
            if current_highest >= expected_level and verified_role in member.roles:
                await message.channel.send(f"{member.mention} has already completed Level {expected_level} or higher!")
                return
            
            # Process the verification for the expected level. complete_verification
            # writes the new highest level via _process_verification, so no further
            # bookkeeping is needed here.
            await self.complete_verification(message, member, verified_role, unverified_role, confirmed_reader_role, level=expected_level)

        except Exception as e:
            print(f"Error processing quiz report: {e}")
            print(f"Error type: {type(e)}")
            import traceback
            print(f"Full traceback: {traceback.format_exc()}")
            await message.channel.send("There was an error processing the quiz report. Please contact an administrator.")
    

    @discord.app_commands.command(name="manage_quiz", description="[ADMIN] Sync verification data, restore a user's roles, or set their quiz level")
    @discord.app_commands.default_permissions(administrator=True)
    @discord.app_commands.checks.has_permissions(administrator=True)
    @discord.app_commands.guild_only()
    @discord.app_commands.describe(
        action="What to do.",
        user="(restore/setlevel) Target member.",
        level="(setlevel) Quiz level to set.",
    )
    @discord.app_commands.choices(action=[
        discord.app_commands.Choice(name="sync", value="sync"),
        discord.app_commands.Choice(name="restore", value="restore"),
        discord.app_commands.Choice(name="setlevel", value="setlevel"),
    ])
    async def manage_quiz(self, interaction: discord.Interaction, action: str,
                          user: Optional[discord.Member] = None, level: Optional[int] = None):
        if action == "sync":
            await self._quiz_sync(interaction)
        elif action == "restore":
            if user is None:
                await interaction.response.send_message("Pick a `user` for restore.", ephemeral=True)
                return
            await self._quiz_restore(interaction, user)
        else:  # setlevel
            if user is None or level is None:
                await interaction.response.send_message("Provide `user` and `level` for setlevel.", ephemeral=True)
                return
            await self._quiz_setlevel(interaction, user, level)

    async def _quiz_sync(self, interaction: discord.Interaction):
        """Sync verification data with actual Discord roles (Admin only)"""
        guild_id = str(interaction.guild.id)
        guild_config = self.bot.config['guilds'].get(guild_id, {})
        
        if not guild_config.get('verification_enabled', False):
            await interaction.response.send_message("Verification is not enabled for this server.")
            return
        
        # Ensure roles exist first
        await self.ensure_roles_exist(interaction.guild)
        
        # Sync the data
        await self.sync_verification_data(interaction.guild)
        
        verified_count = len(self.verification_data["verified_users"])
        embed = discord.Embed(
            title="Verification Data Synced",
            description="Verification data has been synchronized with Discord roles.",
            color=discord.Color.green()
        )
        embed.add_field(name="Verified Users", value=verified_count, inline=True)
        
        await interaction.response.send_message(embed=embed)
    
    @discord.app_commands.command(name="quiz_verify", description="Show verification help")
    async def quiz_verify(self, interaction: discord.Interaction):
        """Show verification help"""
        embed = discord.Embed(
            title="How to Get Verified",
            description=(
                f"Copy and paste the command below to take the quiz. "
                f"Score 8 points to pass (max 4 mistakes, 16s per question). Unlimited tries.\n\n"
                f"Guide: https://vnclub.figamin.net/guide/ · Word list: https://vnclub.figamin.net/level1/\n\n"
                f"認証のため、下のコマンドをコピペしてクイズに挑戦してください。合格点は8点、ミスは4回まで、制限時間は1問16秒です。何回でもチャレンジ可能です。"
            ),
            color=discord.Color.blue()
        )
        embed.add_field(
            name="Quiz Command",
            value=f"```{self.LEVEL_1_COMMAND}```",
            inline=False
        )

        await interaction.response.send_message(embed=embed)
    
    @discord.app_commands.command(name="quiz_progress", description="Show your quiz progress and next level to advance")
    @discord.app_commands.guild_only()
    async def quiz_progress(self, interaction: discord.Interaction, user: discord.Member = None):
        """Show quiz progress and the next quiz needed to advance to the next level"""
        if user is None:
            user = interaction.user
        
        guild_id = str(interaction.guild.id)
        guild_config = self.bot.config['guilds'].get(guild_id, {})
        
        if not guild_config.get('verification_enabled', False):
            await interaction.response.send_message("Verification is not enabled for this server.", ephemeral=True)
            return
        
        user_id = user.id
        current_highest = self.get_highest_quiz_level(user_id)
        next_level = current_highest + 1

        # Check if user has tracked data
        user_id_str = str(user_id)
        has_tracked_data = user_id_str in self.verification_data["highest_quiz_levels"]
        
        # Check if they've reached max level
        if next_level > 15:
            embed = discord.Embed(
                title="🎉 Maximum Level Reached!",
                description=f"{user.display_name} has completed all available quiz levels!",
                color=discord.Color.gold()
            )
            embed.add_field(name="Current Level", value=f"Level {current_highest}", inline=True)
            embed.add_field(name="Status", value="🏆 Max Level", inline=True)
            await interaction.response.send_message(embed=embed)
            return
        
        # Get the settings for the next level
        next_level_settings = self.get_level_settings(next_level)
        if not next_level_settings:
            embed = discord.Embed(
                title="❌ Level Not Available",
                description=f"Level {next_level} is not available yet.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed)
            return
        
        quiz_config = next_level_settings['quiz']
        command = next_level_settings['command']
        quiz_info = next_level_settings['quiz_info']
        
        embed = discord.Embed(
            title=f"📈 Quiz Progress - {user.display_name}",
            color=discord.Color.blue() if current_highest > 0 else discord.Color.orange()
        )
        
        # Progress information section
        if has_tracked_data:
            if current_highest > 0:
                embed.add_field(name="✅ Current Level", value=f"Level {current_highest}", inline=True)
                embed.add_field(name="🎯 Next Level", value=f"Level {next_level}", inline=True)
            else:
                embed.add_field(name="📝 Current Level", value="Level 0 (Unverified)", inline=True)
                embed.add_field(name="🎯 Next Level", value=f"Level {next_level}", inline=True)
        else:
            embed.add_field(name="❓ Status", value="Not tracked", inline=True)
            embed.add_field(name="🎯 Next Level", value=f"Level {next_level}", inline=True)
        
        embed.add_field(
            name="Quiz Command",
            value=f"```{command}```",
            inline=False
        )
        
        embed.add_field(
            name="📋 Instructions",
            value=f"1. Copy the command above\n2. Paste it in any channel\n3. Complete the quiz and reach {quiz_config['score_limit']} points\n4. Your level will be automatically updated!",
            inline=False
        )
        
        # Add progress indicator
        progress_bar = "🟩" * current_highest + "⬜" * (15 - current_highest)
        embed.add_field(
            name="Progress",
            value=f"Level {current_highest}/15\n{progress_bar}",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed)
    
    async def _quiz_restore(self, interaction: discord.Interaction, user: discord.Member):
        """Manually restore roles for a user based on their highest quiz level"""
        guild_id = str(interaction.guild.id)
        guild_config = self.bot.config['guilds'].get(guild_id, {})
        
        if not guild_config.get('verification_enabled', False):
            await interaction.response.send_message("Verification is not enabled for this server.", ephemeral=True)
            return
        
        user_id = user.id
        highest_level = self.get_highest_quiz_level(user_id)
        
        if highest_level == 0:
            await interaction.response.send_message(f"{user.display_name} has not passed any quiz levels yet.", ephemeral=True)
            return
        
        # Restore the user's roles
        await self.restore_user_roles(user, highest_level)
        
        embed = discord.Embed(
            title="✅ Roles Restored",
            description=f"Successfully restored roles for {user.mention}",
            color=discord.Color.green()
        )
        embed.add_field(name="Highest Quiz Level", value=f"Level {highest_level}", inline=True)
        embed.add_field(name="Roles Assigned", value=f"Role '{highest_level}' + Server Access", inline=True)
        
        await interaction.response.send_message(embed=embed)
    
    async def _quiz_setlevel(self, interaction: discord.Interaction, user: discord.Member, level: int):
        """Manually set a user's quiz level"""
        guild_id = str(interaction.guild.id)
        guild_config = self.bot.config['guilds'].get(guild_id, {})
        
        if not guild_config.get('verification_enabled', False):
            await interaction.response.send_message("Verification is not enabled for this server.", ephemeral=True)
            return
        
        # Validate level
        if level < 0 or level > 15:
            await interaction.response.send_message("Quiz level must be between 0 and 15.", ephemeral=True)
            return
        
        # Ensure roles exist
        await self.ensure_roles_exist(interaction.guild)
        
        old_level = self.get_highest_quiz_level(user.id)
        
        # Update quiz level data
        user_id_str = str(user.id)
        self.verification_data["highest_quiz_levels"][user_id_str] = level
        self.save_verification_data()
        
        # Handle quiz level role assignments only
        # Remove ALL other quiz level roles - user should only have their current level role
        for check_level in range(0, 16):  # Check levels 0-15
            if check_level != level:  # Remove any role that's not their new level
                level_role = discord.utils.get(interaction.guild.roles, name=str(check_level))
                if level_role and level_role in user.roles:
                    try:
                        await user.remove_roles(level_role, reason=f"Quiz level changed by {interaction.user.name}")
                        print(f"Removed quiz level role '{check_level}' from {user.name}")
                    except discord.Forbidden:
                        print(f"Cannot remove role '{check_level}' from {user.name}")
        
        # Add new level role
        new_role = discord.utils.get(interaction.guild.roles, name=str(level))
        if new_role and new_role not in user.roles:
            try:
                await user.add_roles(new_role, reason=f"Quiz level set to {level} by {interaction.user.name}")
            except discord.Forbidden:
                pass
        
        # Don't touch Confirmed Reader role or verified status - that's independent
        # This command only manages quiz level progression
        
        embed = discord.Embed(
            title="✅ Quiz Level Updated",
            description=f"Successfully updated {user.mention}'s quiz level",
            color=discord.Color.green()
        )
        embed.add_field(name="Previous Level", value=f"Level {old_level}", inline=True)
        embed.add_field(name="New Level", value=f"Level {level}", inline=True)
        embed.add_field(name="Role Updated", value=f"Role '{level}'", inline=True)
        
        await interaction.response.send_message(embed=embed)
    
    @discord.app_commands.command(name="role_distribution", description="Show role distribution statistics for the server")
    @discord.app_commands.guild_only()
    @discord.app_commands.describe(filter="Choose which members to include in the statistics")
    @discord.app_commands.choices(filter=[
        discord.app_commands.Choice(name="All Members", value="all"),
        discord.app_commands.Choice(name="Confirmed Readers Only", value="confirmed")
    ])
    async def role_distribution(self, interaction: discord.Interaction, filter: str = "all"):
        """Show statistics on role distribution in the server"""
        await interaction.response.defer()
        
        try:
            guild = interaction.guild
            confirmed_reader_role = discord.utils.get(guild.roles, name="Confirmed Reader")
            
            # Get all members (excluding bots)
            all_members = [member for member in guild.members if not member.bot]
            if filter == "confirmed":
                if not confirmed_reader_role:
                    await interaction.followup.send("❌ The Confirmed Reader role does not exist in this server.")
                    return
                all_members = [member for member in all_members if confirmed_reader_role in member.roles]

            total_users = len(all_members)
            
            if total_users == 0:
                message = "No users found in the server."
                if filter == "confirmed":
                    message = "No confirmed readers found in the server."
                await interaction.followup.send(message)
                return
            
            # Initialize counters
            quiz_role_counts = {}
            for level in range(16):  # Levels 0-15
                quiz_role_counts[level] = 0
            
            confirmed_readers = 0
            regular_readers = 0
            
            # Get role objects
            quiz_roles = {}
            for level in range(16):
                quiz_roles[level] = discord.utils.get(guild.roles, name=str(level))
            
            # Count role distributions
            for member in all_members:
                # Count quiz level roles
                for level in range(16):
                    if quiz_roles[level] and quiz_roles[level] in member.roles:
                        quiz_role_counts[level] += 1
                
                # Classify reader types - simple: either confirmed reader or not
                has_confirmed_reader = confirmed_reader_role and confirmed_reader_role in member.roles
                
                if has_confirmed_reader:
                    confirmed_readers += 1
                else:
                    regular_readers += 1
            
            # Create embed
            description = f"Server: **{guild.name}**"
            if filter == "confirmed":
                description += "\nFilter: Confirmed Readers Only"

            embed = discord.Embed(
                title="Role Distribution Statistics",
                description=description,
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            
            # Total users section
            member_label = "confirmed readers (excluding bots)" if filter == "confirmed" else "members (excluding bots)"
            embed.add_field(
                name="👥 Total Users",
                value=f"**{total_users:,}** {member_label}",
                inline=False
            )
            
            # Quiz level roles section
            quiz_stats_text = ""
            for level in range(16):
                count = quiz_role_counts[level]
                percentage = (count / total_users * 100) if total_users > 0 else 0
                
                if count > 0:  # Only show levels that have users
                    user_word = "user" if count == 1 else "users"
                    quiz_stats_text += f"**Level {level}**: {count:,} {user_word} ({percentage:.1f}%)\n"
            
            if not quiz_stats_text:
                quiz_stats_text = "No users have quiz level roles"
            
            embed.add_field(
                name="🎯 Quiz Level Distribution",
                value=quiz_stats_text,
                inline=False
            )
            
            # Reader status section - simple two categories
            confirmed_pct = (confirmed_readers / total_users * 100) if total_users > 0 else 0
            regular_pct = (regular_readers / total_users * 100) if total_users > 0 else 0
            
            confirmed_word = "user" if confirmed_readers == 1 else "users"
            regular_word = "user" if regular_readers == 1 else "users"
            
            reader_text = f"**Confirmed Readers**: {confirmed_readers:,} {confirmed_word} ({confirmed_pct:.1f}%)\n"
            reader_text += f"**Unconfirmed Readers**: {regular_readers:,} {regular_word} ({regular_pct:.1f}%)"
            
            # VN Class Roles section
            vn_class_roles = [
                "魑魅魍魎", "鬼", "餓鬼", "邪鬼", "幽鬼", "霊鬼", 
                "妖鬼", "怪鬼", "冥鬼", "吸血鬼", "魔鬼", "神鬼"
            ]
            
            vn_class_counts = {}
            vn_class_role_objects = {}
            
            # Get VN class role objects and initialize counts
            for class_name in vn_class_roles:
                role = discord.utils.get(guild.roles, name=class_name)
                vn_class_role_objects[class_name] = role
                vn_class_counts[class_name] = 0
            
            # Count VN class role distributions
            for member in all_members:
                for class_name in vn_class_roles:
                    role = vn_class_role_objects[class_name]
                    if role and role in member.roles:
                        vn_class_counts[class_name] += 1
            
            # Build VN class stats text
            vn_class_text = ""
            total_vn_class_users = sum(vn_class_counts.values())
            
            for class_name in vn_class_roles:
                count = vn_class_counts[class_name]
                if count > 0:  # Only show classes that have users
                    percentage = (count / total_users * 100) if total_users > 0 else 0
                    user_word = "user" if count == 1 else "users"
                    vn_class_text += f"**{class_name}**: {count:,} {user_word} ({percentage:.1f}%)\n"
            
            if not vn_class_text:
                vn_class_text = "No users have VN class roles"
            else:
                # Add summary line
                no_class_users = total_users - total_vn_class_users
                no_class_pct = (no_class_users / total_users * 100) if total_users > 0 else 0
                if no_class_users > 0:
                    no_class_word = "user" if no_class_users == 1 else "users"
                    vn_class_text += f"**No VN Class**: {no_class_users:,} {no_class_word} ({no_class_pct:.1f}%)"
            
            embed.add_field(
                name="⚔️ VN Class Distribution",
                value=vn_class_text,
                inline=False
            )
            
            if filter == "all":
                embed.add_field(
                    name="📖 Reader Status Distribution",
                    value=reader_text,
                    inline=False
                )
            
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            print(f"Error in role_distribution command: {e}")
            await interaction.followup.send(f"❌ An error occurred while generating role statistics: {e}", ephemeral=True)

    @discord.app_commands.command(name="manage_quiz_backups", description="[ADMIN] Manage verification data backups")
    @discord.app_commands.default_permissions(administrator=True)
    @discord.app_commands.guild_only()
    @discord.app_commands.describe(
        action="Action to perform",
        backup_name="Name of backup file (for restore action)"
    )
    @discord.app_commands.choices(action=[
        discord.app_commands.Choice(name="create", value="create"),
        discord.app_commands.Choice(name="list", value="list"),
        discord.app_commands.Choice(name="status", value="status"),
        discord.app_commands.Choice(name="restore", value="restore")
    ])
    async def quiz_backup(self, interaction: discord.Interaction, action: str, backup_name: Optional[str] = None):
        """Verification data backup management command"""
        
        # Check if user has admin permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need administrator permissions to use backup commands.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        try:
            if action == "create":
                await self._handle_create_verification_backup(interaction, backup_name)
            elif action == "list":
                await self._handle_list_verification_backups(interaction)
            elif action == "status":
                await self._handle_verification_backup_status(interaction)
            elif action == "restore":
                await self._handle_restore_verification_backup(interaction, backup_name)
            
        except Exception as e:
            print(f"Error in verification backup command: {e}")
            await interaction.followup.send(f"❌ An error occurred: {e}", ephemeral=True)
    
    async def _handle_create_verification_backup(self, interaction: discord.Interaction, backup_name: Optional[str]):
        """Handle manual verification backup creation"""
        success = await self.create_verification_backup(backup_name)
        
        if success:
            embed = discord.Embed(
                title="✅ Verification Backup Created",
                description="Verification data backup created successfully!",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            
            # Get backup info
            backups = await self.list_verification_backups()
            if backups:
                latest = backups[0]
                embed.add_field(
                    name="Backup Details",
                    value=f"**Name:** {latest['name']}\n**Size:** {latest['size_kb']} KB\n**Created:** <t:{int(latest['created'].timestamp())}:F>",
                    inline=False
                )
        else:
            embed = discord.Embed(
                title="❌ Verification Backup Failed",
                description="Failed to create verification data backup. Check logs for details.",
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
        
        await interaction.followup.send(embed=embed)
    
    async def _handle_list_verification_backups(self, interaction: discord.Interaction):
        """Handle verification backup listing"""
        backups = await self.list_verification_backups()
        
        if not backups:
            embed = discord.Embed(
                title="📁 Verification Data Backups",
                description="No verification backups found.",
                color=discord.Color.orange()
            )
        else:
            embed = discord.Embed(
                title="📁 Verification Data Backups",
                description=f"Found {len(backups)} backup(s)",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            
            # Show up to 10 most recent backups
            for i, backup in enumerate(backups[:10]):
                embed.add_field(
                    name=f"{i+1}. {backup['name']}",
                    value=f"**Size:** {backup['size_kb']} KB\n**Created:** <t:{int(backup['created'].timestamp())}:R>",
                    inline=True
                )
            
            if len(backups) > 10:
                embed.add_field(
                    name="Note",
                    value=f"Showing 10 of {len(backups)} backups. Use `/manage_quiz_backups action:status` for more details.",
                    inline=False
                )
        
        await interaction.followup.send(embed=embed)
    
    async def _handle_verification_backup_status(self, interaction: discord.Interaction):
        """Handle verification backup status display"""
        status = await self.get_verification_backup_status()
        
        embed = discord.Embed(
            title="📊 Verification Backup System Status",
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
                      f"**Total Size:** {status.get('total_backup_size_kb', 0)} KB",
                inline=True
            )
            
            latest = status.get('latest_backup')
            if latest:
                embed.add_field(
                    name="Latest Backup",
                    value=f"**Name:** {latest['name']}\n"
                          f"**Size:** {latest['size_kb']} KB\n"
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
            embed.description = "Could not retrieve verification backup status."
        
        await interaction.followup.send(embed=embed)
    
    async def _handle_restore_verification_backup(self, interaction: discord.Interaction, backup_name: Optional[str]):
        """Handle verification backup restoration"""
        if not backup_name:
            await interaction.followup.send("❌ Please specify a backup name to restore.", ephemeral=True)
            return
        
        # Show confirmation dialog
        view = VerificationBackupRestoreConfirmView(self, backup_name)
        
        embed = discord.Embed(
            title="⚠️ Confirm Verification Data Restore",
            description=f"Are you sure you want to restore from backup `{backup_name}`?\n\n"
                       "**This will:**\n"
                       "• Create a backup of the current verification data\n"
                       "• Replace the current verification data with the backup\n"
                       "• This action cannot be easily undone",
            color=discord.Color.red()
        )
        
        await interaction.followup.send(embed=embed, view=view)
    
    # Backup System Methods
    
    @tasks.loop(time=BACKUP_RUN_TIMES, reconnect=True)
    async def auto_backup_task(self):
        """Automatic backup task for verification data"""
        try:
            success = await self.create_verification_backup()
            if success:
                print("Scheduled verification data backup created successfully")
            else:
                print("Scheduled verification data backup failed")
        except Exception as e:
            print(f"Error in automatic verification backup task: {e}")
    
    @auto_backup_task.before_loop
    async def before_auto_backup_task(self):
        """Wait for bot to be ready before starting backup task"""
        await self.bot.wait_until_ready()
    
    def _resolve_verification_backup_path(self, backup_name: str) -> Path:
        """Validate a backup name and resolve it inside the backups dir."""
        if not backup_name:
            raise ValueError("Backup name is required")
        if not self._backup_name_pattern.fullmatch(backup_name):
            raise ValueError("Backup name contains invalid characters")

        backup_dir = self.backup_dir.resolve()
        backup_path = (backup_dir / backup_name).resolve()
        if backup_path.parent != backup_dir:
            raise ValueError("Backup path must reside inside the backup directory")
        return backup_path

    async def create_verification_backup(self, backup_name: str = None) -> bool:
        """Create a backup of the verification data JSON file"""
        try:
            if not backup_name:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_name = f"verification_backup_{timestamp}.json"

            backup_path = self._resolve_verification_backup_path(backup_name)
            
            # Copy the verification data file
            if os.path.exists(self.verification_data_file):
                shutil.copy(self.verification_data_file, backup_path)
                print(f"Verification data backup created: {backup_path}")
                
                # Upload backup to Discord channels if bot is available
                if hasattr(self, 'bot'):
                    await self._upload_verification_backup_to_channels(backup_path)
                
                # Clean up old backups
                await self._cleanup_old_verification_backups()
                return True
            else:
                print(f"Verification data file not found: {self.verification_data_file}")
                return False
                
        except Exception as e:
            print(f"Error creating verification data backup: {e}")
            return False
    
    async def _cleanup_old_verification_backups(self):
        """Remove old verification backup files, keeping only the most recent ones"""
        try:
            backup_files = list(self.backup_dir.glob("verification_backup_*.json"))
            
            # Sort by modification time (newest first)
            backup_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            
            # Remove excess backups
            if len(backup_files) > self.max_backups:
                for old_backup in backup_files[self.max_backups:]:
                    try:
                        old_backup.unlink()
                        print(f"Removed old verification backup: {old_backup}")
                    except Exception as e:
                        print(f"Failed to remove old verification backup {old_backup}: {e}")
                        
        except Exception as e:
            print(f"Error cleaning up old verification backups: {e}")
    
    async def _upload_verification_backup_to_channels(self, backup_path):
        """Upload verification backup file to all configured backup channels"""
        try:
            # Use the initialized backup database instance, or create one if needed
            if self.backup_db:
                backup_channels = await self.backup_db.get_backup_channels()
            else:
                from utils.database import Database
                db = Database()
                await db.initialize()  # Ensure it's initialized
                backup_channels = await db.get_backup_channels()
            
            if not backup_channels:
                print("No backup channels configured for Discord upload")
                return
            
            # Check if backup file exists
            if not backup_path.exists():
                print(f"Verification backup file not found for Discord upload: {backup_path}")
                return
            
            file_size = backup_path.stat().st_size
            
            # Create Discord file object
            discord_file = discord.File(backup_path, filename=backup_path.name)
            
            # Create embed with backup information
            embed = discord.Embed(
                title="🔐 Verification Data Backup Created",
                description=f"Automatic backup of verification data completed successfully.",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            
            embed.add_field(
                name="Backup Details",
                value=f"**Filename:** `{backup_path.name}`\n"
                      f"**Size:** {file_size / 1024:.2f} KB\n"
                      f"**Created:** <t:{int(datetime.now().timestamp())}:F>",
                inline=False
            )
            
            embed.set_footer(text="Verification backup files contain user authentication data")
            
            # Upload to all configured backup channels
            success_count = 0
            for channel_info in backup_channels:
                try:
                    channel = self.bot.get_channel(channel_info['channel_id'])
                    if not channel:
                        print(f"Backup channel not found: {channel_info['channel_id']} in guild {channel_info['guild_id']}")
                        continue
                    
                    # Create a new file object for each upload
                    channel_file = discord.File(backup_path, filename=backup_path.name)
                    
                    await channel.send(embed=embed, file=channel_file)
                    success_count += 1
                    print(f"Verification backup uploaded to channel {channel_info['channel_id']} in guild {channel_info['guild_id']}")
                    
                except discord.errors.Forbidden:
                    print(f"No permission to send files to verification backup channel {channel_info['channel_id']}")
                except discord.errors.HTTPException as e:
                    if "Request entity too large" in str(e) or "413" in str(e):
                        print(f"Verification backup file too large for channel {channel_info['channel_id']} (size: {file_size / 1024:.1f}KB)")
                    else:
                        print(f"HTTP error uploading verification backup to channel {channel_info['channel_id']}: {e}")
                except FileNotFoundError:
                    print(f"Verification backup file not found when uploading to channel {channel_info['channel_id']}")
                except Exception as e:
                    print(f"Unexpected error uploading verification backup to channel {channel_info['channel_id']}: {e}")
            
            print(f"Verification backup uploaded to {success_count}/{len(backup_channels)} configured channels")
                    
        except Exception as e:
            print(f"Error uploading verification backup to Discord channels: {e}")
    
    async def list_verification_backups(self) -> List[Dict[str, Any]]:
        """List all available verification backups"""
        try:
            backups = []
            backup_files = list(self.backup_dir.glob("verification_backup_*.json"))
            
            for backup_file in backup_files:
                stat = backup_file.stat()
                backups.append({
                    'name': backup_file.name,
                    'path': str(backup_file),
                    'size': stat.st_size,
                    'created': datetime.fromtimestamp(stat.st_mtime),
                    'size_kb': round(stat.st_size / 1024, 2)
                })
            
            # Sort by creation time (newest first)
            backups.sort(key=lambda x: x['created'], reverse=True)
            
            return backups
            
        except Exception as e:
            print(f"Error listing verification backups: {e}")
            return []
    
    async def restore_verification_backup(self, backup_name: str) -> bool:
        """Restore verification data from a backup file"""
        try:
            backup_path = self._resolve_verification_backup_path(backup_name)

            if not backup_path.exists():
                print(f"Verification backup file not found: {backup_path}")
                return False
            
            # Create a backup of current data before restore
            await self.create_verification_backup(f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            
            # Restore the backup
            shutil.copy2(backup_path, self.verification_data_file)
            
            # Reload the data in memory
            self.verification_data = self.load_verification_data()
            
            print(f"Verification data restored from backup: {backup_name}")
            return True
            
        except Exception as e:
            print(f"Error restoring verification data from backup: {e}")
            return False
    
    async def should_create_backup(self) -> bool:
        """Check if it's time to create a new verification backup"""
        try:
            backups = await self.list_verification_backups()
            
            if not backups:
                return True  # No backups exist, create one
            
            latest_backup = backups[0]
            next_due = next_backup_run_after(latest_backup['created'])
            now = datetime.now(next_due.tzinfo) if next_due.tzinfo else datetime.now()

            return now >= next_due
            
        except Exception as e:
            print(f"Error checking verification backup status: {e}")
            return False
    
    async def get_verification_backup_status(self) -> Dict[str, Any]:
        """Get verification backup system status information"""
        try:
            backups = await self.list_verification_backups()
            
            status = {
                'backup_count': len(backups),
                'max_backups': self.max_backups,
                'backup_interval_hours': self.backup_interval_hours,
                'backup_dir': str(self.backup_dir),
                'latest_backup': None,
                'next_backup_due': None,
                'total_backup_size_kb': 0
            }
            
            if backups:
                latest = backups[0]
                status['latest_backup'] = {
                    'name': latest['name'],
                    'created': latest['created'],
                    'size_kb': latest['size_kb']
                }
                
                status['next_backup_due'] = next_backup_run_after(latest['created'])
                
                status['total_backup_size_kb'] = round(sum(b['size'] for b in backups) / 1024, 2)
            
            return status
            
        except Exception as e:
            print(f"Error getting verification backup status: {e}")
            return {}

class VerificationBackupRestoreConfirmView(discord.ui.View):
    """Confirmation view for verification data restore operations"""
    
    def __init__(self, verification_cog, backup_name: str):
        super().__init__(timeout=60)
        self.verification_cog = verification_cog
        self.backup_name = backup_name
    
    @discord.ui.button(label="Confirm Restore", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm_restore(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Confirm and execute the restore operation"""
        await interaction.response.defer()
        
        success = await self.verification_cog.restore_verification_backup(self.backup_name)
        
        if success:
            embed = discord.Embed(
                title="✅ Verification Data Restored",
                description=f"Successfully restored verification data from backup `{self.backup_name}`.\n\n"
                           "A backup of the previous data was created before restore.",
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
            description="Verification data restore operation was cancelled.",
            color=discord.Color.orange()
        )
        
        # Disable all buttons
        for item in self.children:
            item.disabled = True
        
        await interaction.response.edit_message(embed=embed, view=self)

async def setup(bot):
    await bot.add_cog(VerificationCog(bot))
