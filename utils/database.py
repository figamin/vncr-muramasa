"""
Database utilities for VN of the month feature
"""

import aiosqlite
import json
import logging
import re
import shutil
import asyncio
from datetime import datetime, date, timedelta, timezone
from typing import Any, Dict, List, Optional, Set
from pathlib import Path

from utils.backup_schedule import BACKUP_INTERVAL_HOURS, next_backup_run_after

# Import NSFW detection function (avoid circular import)
try:
    from .helpers import is_content_nsfw
except ImportError:
    # Fallback if circular import occurs
    def is_content_nsfw(vn_data): return False

logger = logging.getLogger(__name__)


def parse_approver_ids(raw) -> list:
    """The stored onboarding approver list as ints, in order, skipping unreadable entries.

    Kept beside the column it reads so the bot and the web console cannot drift into two
    different ideas of who is on the list. Anything unparseable is dropped rather than
    raising: one malformed entry must not take the decision away from the names either
    side of it.
    """
    seen = []
    for part in str(raw or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0 and value not in seen:
            seen.append(value)
    return seen


def format_approver_ids(ids) -> str:
    return ",".join(str(i) for i in ids)


class Database:
    """
    SQLite database manager for VN of the month data
    """
    
    def __init__(self, db_path: str = "data/vnbot.db"):
        self.db_path = db_path
        # Ensure the data directory exists
        Path(db_path).parent.mkdir(exist_ok=True)
        
        # Backup configuration
        self.backup_dir = Path(db_path).parent / "backups"
        self.backup_dir.mkdir(exist_ok=True)
        self.max_backups = 60  # Snapshots are full copies, so this count multiplies DB size on disk
        self.backup_interval_hours = BACKUP_INTERVAL_HOURS
        self._backup_name_pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
    
    def _coerce_activity_datetime(self, value: Any) -> datetime:
        """Parse a stored activity date into an aware datetime."""
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            raw = value.strip()
            if not raw:
                raise ValueError("Activity date string cannot be empty")
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
        else:
            raise ValueError("Unsupported activity date type")

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    def _normalize_activity_datetime(self, value: Any) -> Optional[str]:
        """Validate and normalize an activity date value."""
        if value in (None, ""):
            return None

        dt = self._coerce_activity_datetime(value)
        now = datetime.now(timezone.utc)
        # Allow up to 48 hours ahead in UTC. The cog enforces a calendar-day
        # window in the user's timezone; this DB-level check is a sanity guard,
        # so it must be permissive enough to accept "tomorrow" logged from any
        # timezone at any hour (worst case: ~36h offset between UTC and user TZ).
        future_limit = now + timedelta(hours=48)

        if dt.astimezone(timezone.utc) > future_limit:
            raise ValueError("Activity date cannot be more than 48 hours in the future.")

        return dt.isoformat()

    async def initialize(self):
        """Initialize database with required tables"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS vn_of_month (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vndb_id TEXT NOT NULL,
                    month INTEGER NOT NULL,
                    year INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    image_url TEXT,
                    rating REAL,
                    length TEXT,
                    suggester_name TEXT,
                    admin_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(month, year)
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS vn_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vndb_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    reason TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS vn_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vndb_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    image_url TEXT,
                    rating REAL,
                    length TEXT,
                    added_by_id INTEGER NOT NULL,
                    added_by_name TEXT,
                    target_month INTEGER,
                    target_year INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS vn_votes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vn_month_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 10),
                    review TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(vn_month_id) REFERENCES vn_of_month(id),
                    UNIQUE(vn_month_id, user_id)
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS vndb_cache (
                    vndb_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_completions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    pool_id INTEGER NOT NULL,
                    vndb_id TEXT NOT NULL,
                    rating INTEGER CHECK(rating >= 1 AND rating <= 10),
                    review TEXT,
                    completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(pool_id) REFERENCES vn_pool(id),
                    UNIQUE(user_id, pool_id)
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS backup_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, channel_id)
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS error_log_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id)
                )
            """)

            # Immersion tracking tables
            await db.execute("""
                CREATE TABLE IF NOT EXISTS immersion_users (
                    user_id TEXT PRIMARY KEY,
                    timezone TEXT,
                    vn_reading_speed REAL DEFAULT 150,
                    book_reading_speed REAL DEFAULT 120,
                    manga_reading_speed REAL DEFAULT 200,
                    daily_goal INTEGER DEFAULT 0,
                    primary_guild_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS immersion_guilds (
                    guild_id TEXT PRIMARY KEY,
                    timezone TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS immersion_guild_members (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS immersion_activities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    guild_id TEXT,
                    name TEXT NOT NULL,
                    primary_type TEXT NOT NULL CHECK(primary_type IN ('reading', 'listening')),
                    media_type TEXT CHECK(media_type IN ('book', 'anime', 'manga', 'video', 'visual_novel', 'game')),
                    duration INTEGER NOT NULL,
                    date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    deleted_at TIMESTAMP,
                    imported_at TIMESTAMP,
                    meta TEXT DEFAULT '{}',
                    comment TEXT
                )
            """)
            
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_immersion_activities_date ON immersion_activities (date)
            """)
            
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_immersion_activities_user_id ON immersion_activities (user_id)
            """)
            
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_immersion_activities_type ON immersion_activities (primary_type, media_type)
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS immersion_goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    activity_type TEXT CHECK(activity_type IN ('reading', 'listening')),
                    media_type TEXT CHECK(media_type IN ('book', 'anime', 'manga', 'video', 'visual_novel', 'game')),
                    youtube_channels TEXT,
                    cron TEXT NOT NULL,
                    target INTEGER NOT NULL,
                    current_value INTEGER DEFAULT 0,
                    due_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    deleted_at TIMESTAMP
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS immersion_imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    import_timestamp TIMESTAMP NOT NULL,
                    activity_count INTEGER NOT NULL,
                    filename TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_immersion_imports_user_id ON immersion_imports (user_id)
            """)
            
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_immersion_imports_timestamp ON immersion_imports (import_timestamp)
            """)
            
            # Blacklist table for users who should not appear in leaderboards/rankings
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_blacklist (
                    user_id TEXT PRIMARY KEY,
                    reason TEXT,
                    blacklisted_by TEXT NOT NULL,
                    blacklisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Mapping of kizuna usernames to discord users so kizuna webhook
            # logs can be auto-attributed. UNIQUE(kizuna_username) enforces a
            # 1:1 from kizuna name to discord user; one discord user may hold
            # multiple kizuna names. kizuna_username is stored lowercased.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS kizuna_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kizuna_username TEXT NOT NULL,
                    discord_user_id TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(kizuna_username)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_kizuna_links_user ON kizuna_links(discord_user_id)
            """)

            # Message word-filter: rules, a single settings row, exempt roles,
            # and an audit log of every filtered original. rules_version on the
            # settings row is bumped on any rule/role/settings change so the cog
            # reloads live without a restart (see cogs/message_filter.py).
            await db.execute("""
                CREATE TABLE IF NOT EXISTS message_filters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern TEXT NOT NULL,
                    replacement TEXT NOT NULL DEFAULT '',
                    is_regex INTEGER NOT NULL DEFAULT 0,
                    ignore_case INTEGER NOT NULL DEFAULT 1,
                    whole_word INTEGER NOT NULL DEFAULT 0,
                    match_plurals INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS message_filter_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    log_channel_id INTEGER,
                    rules_version INTEGER NOT NULL DEFAULT 0
                )
            """)
            # Default OFF: this feature deletes user messages, so an admin must
            # explicitly enable it after setting up rules/log channel/exemptions.
            await db.execute("""
                INSERT OR IGNORE INTO message_filter_settings (id, enabled, rules_version)
                VALUES (1, 0, 0)
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS message_filter_excluded_roles (
                    role_id INTEGER PRIMARY KEY,
                    added_by TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS message_filter_excluded_channels (
                    channel_id INTEGER PRIMARY KEY,
                    added_by TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS message_filter_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    author_id INTEGER NOT NULL,
                    original_content TEXT NOT NULL,
                    cleaned_content TEXT NOT NULL,
                    matched_rule_ids TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_filter_logs_created ON message_filter_logs(created_at)
            """)

            # Media flagging: a single settings row, the reposted media items,
            # per-user flag/safe votes, the exempt/ignored role lists, and the
            # per-member opt-out list. See _ensure_media_flag_tables for the
            # mirror used by early callers.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS media_flag_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    log_channel_id INTEGER,
                    threshold INTEGER NOT NULL DEFAULT 2
                )
            """)
            # Default OFF: an admin must explicitly enable reposting/flagging.
            await db.execute("""
                INSERT OR IGNORE INTO media_flag_settings (id, enabled, threshold)
                VALUES (1, 0, 2)
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS media_flag_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_channel_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    repost_channel_id INTEGER NOT NULL,
                    repost_message_id INTEGER NOT NULL,
                    author_id INTEGER NOT NULL,
                    report_message_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(repost_message_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS media_flag_votes (
                    item_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    vote TEXT NOT NULL CHECK (vote IN ('flag','safe')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (item_id, user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS media_flag_excluded_roles (
                    role_id INTEGER PRIMARY KEY,
                    added_by TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS media_flag_ignored_roles (
                    role_id INTEGER PRIMARY KEY,
                    added_by TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS media_flag_optout_users (
                    user_id INTEGER PRIMARY KEY,
                    added_by TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS media_flag_reports (
                    item_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (item_id, user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS repost_authors (
                    repost_message_id INTEGER PRIMARY KEY,
                    author_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.commit()

            # Run migrations to ensure all columns exist
            await self._run_migrations(db)
            
            # Add performance indexes
            await self._add_performance_indexes(db)
            
            logger.info("Database initialized successfully")
    
    async def _run_migrations(self, db):
        """Run database migrations to ensure all columns exist"""
        try:
            # Add match_plurals to message_filters for DBs created before that column.
            async with db.execute("PRAGMA table_info(message_filters)") as cursor:
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]
                if columns and 'match_plurals' not in column_names:
                    logger.info("Adding match_plurals column to message_filters table")
                    await db.execute("ALTER TABLE message_filters ADD COLUMN match_plurals INTEGER NOT NULL DEFAULT 0")
                    await db.commit()

            # Check if suggester_name column exists in vn_of_month table
            async with db.execute("PRAGMA table_info(vn_of_month)") as cursor:
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]
                
                if 'suggester_name' not in column_names:
                    logger.info("Adding suggester_name column to vn_of_month table")
                    await db.execute("ALTER TABLE vn_of_month ADD COLUMN suggester_name TEXT")
                    await db.commit()
            
            # Check if status column exists in vn_pool table
            async with db.execute("PRAGMA table_info(vn_pool)") as cursor:
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]
                
                if 'status' not in column_names:
                    logger.info("Adding status column to vn_pool table")
                    await db.execute("ALTER TABLE vn_pool ADD COLUMN status TEXT DEFAULT 'pool'")
                    await db.commit()
                
                if 'is_nsfw' not in column_names:
                    logger.info("Adding is_nsfw column to vn_pool table")
                    await db.execute("ALTER TABLE vn_pool ADD COLUMN is_nsfw INTEGER DEFAULT 0")
                    await db.commit()
            
            # Check if vn_pool has UNIQUE constraint on vndb_id (needs to be removed for multi-month support)
            async with db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='vn_pool'") as cursor:
                result = await cursor.fetchone()
                if result:
                    create_sql = result[0]
                    # Check if vndb_id has UNIQUE constraint
                    if "vndb_id TEXT NOT NULL UNIQUE" in create_sql or "UNIQUE(vndb_id)" in create_sql:
                        logger.info("UNIQUE constraint detected on vn_pool.vndb_id")
                        
                        # Count existing rows BEFORE migration
                        async with db.execute("SELECT COUNT(*) FROM vn_pool") as count_cursor:
                            original_count = (await count_cursor.fetchone())[0]
                        logger.info(f"Original vn_pool row count: {original_count}")
                        
                        # Check if backup already exists (migration was interrupted)
                        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vn_pool_backup'") as backup_cursor:
                            backup_exists = await backup_cursor.fetchone()
                        
                        if backup_exists:
                            logger.warning("vn_pool_backup already exists - skipping migration to prevent data issues")
                            logger.warning("If you want to re-run migration, manually drop vn_pool_backup first")
                        else:
                            logger.info("Removing UNIQUE constraint from vn_pool.vndb_id to allow same VN for different months")
                            
                            # Create backup
                            await db.execute("CREATE TABLE vn_pool_backup AS SELECT * FROM vn_pool")
                            await db.commit()
                            
                            # Verify backup was created with correct row count
                            async with db.execute("SELECT COUNT(*) FROM vn_pool_backup") as backup_count_cursor:
                                backup_count = (await backup_count_cursor.fetchone())[0]
                            
                            if backup_count != original_count:
                                logger.error(f"BACKUP FAILED: Expected {original_count} rows, got {backup_count}")
                                logger.error("ABORTING MIGRATION - Original table unchanged")
                                await db.execute("DROP TABLE vn_pool_backup")
                                await db.commit()
                                raise Exception("Backup verification failed - migration aborted")
                            
                            logger.info(f"Backup verified: {backup_count} rows")
                            
                            # Drop and recreate without UNIQUE constraint
                            await db.execute("DROP TABLE vn_pool")
                            await db.execute("""
                                CREATE TABLE vn_pool (
                                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                                    vndb_id TEXT NOT NULL,
                                    title TEXT NOT NULL,
                                    description TEXT,
                                    image_url TEXT,
                                    rating REAL,
                                    length TEXT,
                                    added_by_id INTEGER NOT NULL,
                                    added_by_name TEXT,
                                    target_month INTEGER,
                                    target_year INTEGER,
                                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                    status TEXT DEFAULT 'pool',
                                    is_nsfw INTEGER DEFAULT 0
                                )
                            """)
                            
                            # Restore data
                            await db.execute("""
                                INSERT INTO vn_pool 
                                (id, vndb_id, title, description, image_url, rating, length, 
                                 added_by_id, added_by_name, target_month, target_year, created_at, status, is_nsfw)
                                SELECT id, vndb_id, title, description, image_url, rating, length,
                                       added_by_id, added_by_name, target_month, target_year, created_at, 
                                       COALESCE(status, 'pool'), COALESCE(is_nsfw, 0)
                                FROM vn_pool_backup
                            """)
                            await db.commit()
                            
                            # Verify restored row count matches original
                            async with db.execute("SELECT COUNT(*) FROM vn_pool") as final_count_cursor:
                                final_count = (await final_count_cursor.fetchone())[0]
                            
                            if final_count != original_count:
                                logger.error(f"RESTORE FAILED: Expected {original_count} rows, got {final_count}")
                                logger.error("ROLLING BACK - Restoring from backup")
                                await db.execute("DROP TABLE vn_pool")
                                await db.execute("ALTER TABLE vn_pool_backup RENAME TO vn_pool")
                                await db.commit()
                                raise Exception("Restore verification failed - rolled back to original table")
                            
                            logger.info(f"Migration successful: {final_count} rows restored")
                            logger.info("Backup table 'vn_pool_backup' kept for safety - you can drop it later if everything works fine")
            
            # Check and migrate immersion tracking tables
            await self._migrate_immersion_tables(db)
                    
        except Exception as e:
            logger.error(f"Error running migrations: {e}")
    
    async def _migrate_media_type_constraint(self, db, existing_tables: List[str]):
        """Migrate media_type CHECK constraint to include 'game'"""
        try:
            # Check if immersion_activities needs migration
            if 'immersion_activities' in existing_tables:
                async with db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='immersion_activities'") as cursor:
                    result = await cursor.fetchone()
                    if result:
                        create_sql = result[0]
                        # Check if 'game' is already in the constraint
                        if "'game'" not in create_sql and '"game"' not in create_sql:
                            logger.info("Migrating immersion_activities to add 'game' media type")
                            
                            # Create new table with updated constraint
                            await db.execute("""
                                CREATE TABLE immersion_activities_new (
                                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                                    user_id TEXT NOT NULL,
                                    guild_id TEXT,
                                    name TEXT NOT NULL,
                                    primary_type TEXT NOT NULL CHECK(primary_type IN ('reading', 'listening')),
                                    media_type TEXT CHECK(media_type IN ('book', 'anime', 'manga', 'video', 'visual_novel', 'game')),
                                    duration INTEGER NOT NULL,
                                    date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                    deleted_at TIMESTAMP,
                                    imported_at TIMESTAMP,
                                    meta TEXT DEFAULT '{}'
                                )
                            """)
                            
                            # Copy data
                            await db.execute("""
                                INSERT INTO immersion_activities_new
                                SELECT * FROM immersion_activities
                            """)
                            
                            # Drop old table and rename new one
                            await db.execute("DROP TABLE immersion_activities")
                            await db.execute("ALTER TABLE immersion_activities_new RENAME TO immersion_activities")
                            
                            # Recreate indexes
                            await db.execute("""
                                CREATE INDEX IF NOT EXISTS idx_immersion_activities_date ON immersion_activities (date)
                            """)
                            await db.execute("""
                                CREATE INDEX IF NOT EXISTS idx_immersion_activities_user_id ON immersion_activities (user_id)
                            """)
                            await db.execute("""
                                CREATE INDEX IF NOT EXISTS idx_immersion_activities_type ON immersion_activities (primary_type, media_type)
                            """)
                            
                            await db.commit()
                            logger.info("Successfully migrated immersion_activities table")
            
            # Check if immersion_goals needs migration
            if 'immersion_goals' in existing_tables:
                async with db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='immersion_goals'") as cursor:
                    result = await cursor.fetchone()
                    if result:
                        create_sql = result[0]
                        # Check if 'game' is already in the constraint
                        if "'game'" not in create_sql and '"game"' not in create_sql:
                            logger.info("Migrating immersion_goals to add 'game' media type")
                            
                            # Create new table with updated constraint
                            await db.execute("""
                                CREATE TABLE immersion_goals_new (
                                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                                    user_id TEXT NOT NULL,
                                    name TEXT NOT NULL,
                                    activity_type TEXT CHECK(activity_type IN ('reading', 'listening')),
                                    media_type TEXT CHECK(media_type IN ('book', 'anime', 'manga', 'video', 'visual_novel', 'game')),
                                    youtube_channels TEXT,
                                    cron TEXT NOT NULL,
                                    target INTEGER NOT NULL,
                                    current_value INTEGER DEFAULT 0,
                                    due_at TIMESTAMP NOT NULL,
                                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                    deleted_at TIMESTAMP
                                )
                            """)
                            
                            # Copy data
                            await db.execute("""
                                INSERT INTO immersion_goals_new
                                SELECT * FROM immersion_goals
                            """)
                            
                            # Drop old table and rename new one
                            await db.execute("DROP TABLE immersion_goals")
                            await db.execute("ALTER TABLE immersion_goals_new RENAME TO immersion_goals")
                            
                            await db.commit()
                            logger.info("Successfully migrated immersion_goals table")
                            
        except Exception as e:
            logger.error(f"Error migrating media_type constraint: {e}", exc_info=True)
    
    async def _migrate_immersion_tables(self, db):
        """Migrate immersion tracking tables if needed"""
        try:
            # Check if immersion tables exist
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'immersion_%'") as cursor:
                existing_tables = [row[0] for row in await cursor.fetchall()]
            
            # Migrate media_type CHECK constraint to include 'game'
            await self._migrate_media_type_constraint(db, existing_tables)
            
            # Add any missing columns to existing immersion tables
            for table_name in ['immersion_users', 'immersion_activities', 'immersion_goals', 'immersion_guilds']:
                if table_name in existing_tables:
                    # Refresh column list for each table to read the current schema
                    async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
                        columns = await cursor.fetchall()
                        column_names = [col[1] for col in columns]
                        
                        # Add any missing columns based on table
                        if table_name == 'immersion_users':
                            missing_columns = {
                                'vn_reading_speed': 'REAL DEFAULT 150',
                                'book_reading_speed': 'REAL DEFAULT 120',
                                'manga_reading_speed': 'REAL DEFAULT 200',
                                'daily_goal': 'INTEGER DEFAULT 0',
                                'primary_guild_id': 'TEXT'
                            }
                        elif table_name == 'immersion_activities':
                            missing_columns = {
                                'deleted_at': 'TIMESTAMP',
                                'imported_at': 'TIMESTAMP',
                                'comment': 'TEXT'
                            }
                        elif table_name == 'immersion_goals':
                            missing_columns = {
                                'youtube_channels': 'TEXT',
                                'deleted_at': 'TIMESTAMP'
                            }
                        elif table_name == 'immersion_guilds':
                            missing_columns = {
                                'guild_name': 'TEXT',
                                'guild_icon_url': 'TEXT'
                            }
                        else:
                            missing_columns = {}
                        
                        # Only add columns that don't exist
                        for col_name, col_def in missing_columns.items():
                            if col_name not in column_names:
                                try:
                                    logger.info(f"Adding {col_name} column to {table_name} table")
                                    await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")
                                    await db.commit()
                                    logger.info(f"Successfully added {col_name} to {table_name}")
                                except Exception as col_error:
                                    # Check if error is due to column already existing
                                    if "duplicate column" in str(col_error).lower():
                                        logger.debug(f"Column {col_name} already exists in {table_name}, skipping")
                                    else:
                                        # Re-raise if it's a different error
                                        raise
                                
        except Exception as e:
            logger.error(f"Error migrating immersion tables: {e}", exc_info=True)
    
    async def _add_performance_indexes(self, db):
        """Add composite indexes for query performance optimization"""
        try:
            logger.info("Adding performance indexes for immersion tracking...")
            
            # Index for log_library queries (user + filters + sorting)
            # Covers: WHERE user_id = ? AND deleted_at IS NULL AND media_type = ? AND primary_type = ?
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_activities_user_deleted_media 
                ON immersion_activities(user_id, deleted_at, media_type, primary_type)
            """)
            logger.info("Created index: idx_activities_user_deleted_media")
            
            # Index for log_stats date range queries
            # Covers: WHERE user_id = ? AND deleted_at IS NULL AND date >= ? ORDER BY date DESC
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_activities_user_date_range
                ON immersion_activities(user_id, deleted_at, date DESC, duration)
            """)
            logger.info("Created index: idx_activities_user_date_range")
            
            # Index for filtered queries (stats with media/activity type filters)
            # Covers: WHERE user_id = ? AND deleted_at IS NULL AND primary_type = ? AND media_type = ? AND date >= ?
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_activities_filters
                ON immersion_activities(user_id, deleted_at, primary_type, media_type, date)
            """)
            logger.info("Created index: idx_activities_filters")
            
            # Index for leaderboard queries (covers blacklist join)
            # Covers: WHERE deleted_at IS NULL AND guild_id = ? ORDER BY duration DESC
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_activities_leaderboard
                ON immersion_activities(deleted_at, guild_id, duration DESC, user_id)
            """)
            logger.info("Created index: idx_activities_leaderboard")
            
            # Index for activity lookup by name (for library grouping)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_activities_name_lookup
                ON immersion_activities(user_id, name, media_type, deleted_at)
            """)
            logger.info("Created index: idx_activities_name_lookup")
            
            # Index for chart generation queries (date range with sorting)
            # Covers: WHERE user_id = ? AND deleted_at IS NULL AND date >= ? AND date <= ? ORDER BY date ASC
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_activities_chart_range
                ON immersion_activities(user_id, deleted_at, date ASC)
            """)
            logger.info("Created index: idx_activities_chart_range")
            
            await db.commit()
            logger.info("Performance indexes created successfully")
            
        except Exception as e:
            logger.error(f"Error adding performance indexes: {e}", exc_info=True)
    
    async def set_vn_of_month(self, vndb_id: str, month: int, year: int, 
                             title: str, admin_id: int, description: str = None,
                             image_url: str = None, rating: float = None, 
                             length: str = None, suggester_name: str = None) -> bool:
        """Set the VN of the month for a specific month/year"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO vn_of_month 
                    (vndb_id, month, year, title, description, image_url, rating, length, suggester_name, admin_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (vndb_id, month, year, title, description, image_url, rating, length, suggester_name, admin_id))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error setting VN of month: {e}")
            return False
    
    async def get_vn_of_month(self, month: int = None, year: int = None) -> Optional[Dict[str, Any]]:
        """Get VN of the month (current month if not specified)"""
        if month is None or year is None:
            now = datetime.now()
            month = month or now.month
            year = year or now.year
            
        logger.info(f"get_vn_of_month: Querying for month={month}, year={year}")
            
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT * FROM vn_of_month WHERE month = ? AND year = ?
                """, (month, year)) as cursor:
                    row = await cursor.fetchone()
                    result = dict(row) if row else None
                    logger.debug(f"get_vn_of_month: Raw row={row}, result={result}")
                    return result
        except Exception as e:
            logger.error(f"Error getting VN of month: {e}")
            return None
    
    async def get_all_vn_months(self, limit: int = 12) -> List[Dict[str, Any]]:
        """Get all VN of the month entries from the pool table, ordered by date"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT * FROM vn_pool 
                    WHERE status = 'vn_of_month'
                    ORDER BY target_year DESC, target_month DESC 
                    LIMIT ?
                """, (limit,)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting VN months from pool: {e}")
            return []
    
    async def add_suggestion(self, vndb_id: str, user_id: int, guild_id: int,
                           title: str, reason: str = None) -> bool:
        """Add a VN suggestion"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO vn_suggestions (vndb_id, user_id, guild_id, title, reason)
                    VALUES (?, ?, ?, ?, ?)
                """, (vndb_id, user_id, guild_id, title, reason))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding suggestion: {e}")
            return False
    
    async def get_suggestions(self, status: str = "pending", limit: int = 10) -> List[Dict[str, Any]]:
        """Get VN suggestions"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT * FROM vn_suggestions 
                    WHERE status = ? 
                    ORDER BY created_at DESC 
                    LIMIT ?
                """, (status, limit)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting suggestions: {e}")
            return []
    
    async def cache_vndb_data(self, vndb_id: str, data: Dict[str, Any]) -> bool:
        """Cache VNDB data"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO vndb_cache (vndb_id, data)
                    VALUES (?, ?)
                """, (vndb_id, json.dumps(data)))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error caching VNDB data: {e}")
            return False
    
    async def get_cached_vndb_data(self, vndb_id: str, max_age_hours: int = 24) -> Optional[Dict[str, Any]]:
        """Get cached VNDB data if not too old"""
        try:
            try:
                age_hours = int(max_age_hours)
            except (TypeError, ValueError):
                logger.warning("Invalid max_age_hours=%s supplied to get_cached_vndb_data; defaulting to 24", max_age_hours)
                age_hours = 24

            # Clamp to a bounded range; the value is formatted into the offset string
            if age_hours < 0:
                age_hours = 0
            if age_hours > 720:
                age_hours = 720

            offset_expr = f"-{age_hours} hours"

            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT data FROM vndb_cache 
                    WHERE vndb_id = ? 
                    AND datetime(cached_at) > datetime('now', ?)
                """, (vndb_id, offset_expr)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return json.loads(row['data'])
                    return None
        except Exception as e:
            logger.error(f"Error getting cached data: {e}")
            return None
    
    async def add_vn_to_pool(self, vndb_id: str, title: str, added_by_id: int,
                           added_by_name: str, target_month: int = None, target_year: int = None,
                           description: str = None, image_url: str = None, 
                           rating: float = None, length: str = None, vn_data: dict = None) -> int:
        """Add a VN to the pool and return the pool ID"""
        try:
            # Detect NSFW content if vn_data is provided
            is_nsfw = 0
            if vn_data:
                is_nsfw = 1 if is_content_nsfw(vn_data) else 0
            
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    INSERT INTO vn_pool 
                    (vndb_id, title, description, image_url, rating, length, 
                     added_by_id, added_by_name, target_month, target_year, is_nsfw, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (vndb_id, title, description, image_url, rating, length,
                      added_by_id, added_by_name, target_month, target_year, is_nsfw, 'pool'))
                await db.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Error adding VN to pool: {e}")
            return None
    
    async def get_vn_pool(self, target_month: int = None, target_year: int = None, 
                         limit: int = 50) -> List[Dict[str, Any]]:
        """Get VNs from the pool, optionally filtered by month/year"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                if target_month and target_year:
                    query = """
                        SELECT * FROM vn_pool 
                        WHERE target_month = ? AND target_year = ?
                        ORDER BY created_at DESC 
                        LIMIT ?
                    """
                    params = (target_month, target_year, limit)
                elif target_month or target_year:
                    # Show all if only one filter is provided
                    query = """
                        SELECT * FROM vn_pool 
                        WHERE (target_month = ? OR ? IS NULL) 
                        AND (target_year = ? OR ? IS NULL)
                        ORDER BY created_at DESC 
                        LIMIT ?
                    """
                    params = (target_month, target_month, target_year, target_year, limit)
                else:
                    query = """
                        SELECT * FROM vn_pool 
                        ORDER BY created_at DESC 
                        LIMIT ?
                    """
                    params = (limit,)
                
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting VN pool: {e}")
            return []
    
    async def remove_vn_from_pool(self, vndb_id: str) -> bool:
        """Remove a VN from the pool"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM vn_pool WHERE vndb_id = ?", (vndb_id,))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error removing VN from pool: {e}")
            return False
    
    async def get_pool_entry_by_id(self, pool_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific pool entry by its ID"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT * FROM vn_pool WHERE id = ?", (pool_id,)) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting pool entry by ID: {e}")
            return None
    
    async def update_pool_target_month(self, pool_id: int, month: int = None, year: int = None) -> bool:
        """Update a pool entry's target month and year"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    UPDATE vn_pool 
                    SET target_month = ?, target_year = ? 
                    WHERE id = ?
                """, (month, year, pool_id))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error updating pool target month: {e}")
            return False
    
    async def remove_vn_from_pool_by_id(self, pool_id: int) -> bool:
        """Remove a VN from the pool by pool ID"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM vn_pool WHERE id = ?", (pool_id,))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error removing VN from pool by ID: {e}")
            return False
    
    async def remove_vn_of_month(self, month: int, year: int) -> bool:
        """Remove a VN of the month entry"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM vn_of_month WHERE month = ? AND year = ?", (month, year))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error removing VN of month: {e}")
            return False
    
    async def set_vn_of_month_from_pool(self, pool_id: int, month: int, year: int) -> bool:
        """Set a pool entry as VN of the month by updating its status and month/year"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Check if this pool entry is already VN of the month for this period
                async with db.execute("""
                    SELECT id FROM vn_pool 
                    WHERE id = ? AND status = 'vn_of_month' AND target_month = ? AND target_year = ?
                """, (pool_id, month, year)) as cursor:
                    existing = await cursor.fetchone()
                    if existing:
                        return True  # Already set as VN of the month for this period
                
                # Set the VN as VN of the month (no need to clear others - multiple allowed)
                await db.execute("""
                    UPDATE vn_pool 
                    SET status = 'vn_of_month', target_month = ?, target_year = ? 
                    WHERE id = ?
                """, (month, year, pool_id))
                
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error setting VN of month from pool: {e}")
            return False
    
    async def remove_vn_of_month_from_pool(self, month: int, year: int) -> bool:
        """Remove VN of the month status from pool entry"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Find the pool entry that is currently VN of the month for this month/year
                async with db.execute("""
                    UPDATE vn_pool 
                    SET status = NULL
                    WHERE status = 'vn_of_month' 
                    AND target_month = ? AND target_year = ?
                """, (month, year)) as cursor:
                    await db.commit()
                    return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error removing VN of month from pool: {e}")
            return False
    
    async def remove_vn_of_month_status_by_id(self, pool_id: int) -> bool:
        """Remove VN of the month status from a specific pool entry by ID"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    UPDATE vn_pool 
                    SET status = NULL
                    WHERE id = ? AND status = 'vn_of_month'
                """, (pool_id,)) as cursor:
                    await db.commit()
                    return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error removing VN of month status by ID: {e}")
            return False
    
    async def get_vn_of_month_from_pool(self, month: int = None, year: int = None) -> Optional[Dict[str, Any]]:
        """Get VN of the month from pool table (returns first one for backward compatibility)"""
        vns = await self.get_vns_of_month_from_pool(month, year)
        return vns[0] if vns else None
    
    async def get_vns_of_month_from_pool(self, month: int = None, year: int = None) -> List[Dict[str, Any]]:
        """Get ALL VNs of the month from pool table for a given period"""
        if month is None or year is None:
            now = datetime.now()
            month = month or now.month
            year = year or now.year
            
        logger.info(f"get_vns_of_month_from_pool: Querying for month={month}, year={year}")
            
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT * FROM vn_pool 
                    WHERE status = 'vn_of_month' AND target_month = ? AND target_year = ?
                    ORDER BY created_at ASC
                """, (month, year)) as cursor:
                    rows = await cursor.fetchall()
                    results = [dict(row) for row in rows] if rows else []
                    logger.info(f"get_vns_of_month_from_pool: Found {len(results)} VNs of the month")
                    return results
        except Exception as e:
            logger.error(f"Error getting VNs of month from pool: {e}")
            return []

    async def get_pool_entry_by_vndb_id(self, vndb_id: str) -> Optional[Dict[str, Any]]:
        """Get a pool entry by VNDB ID"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT * FROM vn_pool WHERE vndb_id = ?", (vndb_id,)) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting pool entry by VNDB ID: {e}")
            return None

    async def get_pool_entry_by_vndb_id_and_month(self, vndb_id: str, month: int, year: int) -> Optional[Dict[str, Any]]:
        """Get a pool entry by VNDB ID for a specific month/year combination"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM vn_pool WHERE vndb_id = ? AND target_month = ? AND target_year = ?", 
                    (vndb_id, month, year)
                ) as cursor:
                    row = await cursor.fetchone()
                    result = dict(row) if row else None
                    logger.debug(f"Query for vndb_id={vndb_id}, month={month}, year={year} returned: {result}")
                    return result
        except Exception as e:
            logger.error(f"Error getting pool entry by VNDB ID and month: {e}")
            return None

    async def update_pool_entry(self, pool_id: int, target_month: int = None, target_year: int = None, 
                              added_by_id: int = None, added_by_name: str = None) -> bool:
        """Update a pool entry's target month, year, and/or suggester"""
        try:
            # Validate inputs
            if target_month is not None and not (1 <= target_month <= 12):
                logger.error(f"Invalid target_month: {target_month}")
                return False
            
            if target_year is not None and not (2020 <= target_year <= 2030):
                logger.error(f"Invalid target_year: {target_year}")
                return False
            
            # Build dynamic query with ONLY safe, predefined column names
            allowed_updates = {
                'target_month': target_month,
                'target_year': target_year,
                'added_by_id': added_by_id,
                'added_by_name': added_by_name
            }
            
            # Filter out None values and build safe query parts
            updates = []
            params = []
            
            for column, value in allowed_updates.items():
                if value is not None:
                    updates.append(f"{column} = ?")  # Safe: column names are hardcoded
                    params.append(value)
            
            if not updates:
                return False  # Nothing to update
            
            params.append(pool_id)  # Add pool_id for WHERE clause
            
            async with aiosqlite.connect(self.db_path) as db:
                # Safe: updates list contains only hardcoded column names
                query = f"UPDATE vn_pool SET {', '.join(updates)} WHERE id = ?"
                await db.execute(query, params)
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error updating pool entry: {e}")
            return False

    async def remove_from_pool(self, pool_id: int) -> bool:
        """Remove a VN from the pool entirely"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM vn_pool WHERE id = ?", (pool_id,))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error removing from pool: {e}")
            return False

    async def get_vn_month_status(self, vndb_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Get VN of the month status for a list of VNDB IDs"""
        try:
            if not vndb_ids:
                return {}
            
            # Accept only VNDB IDs in the vN format
            validated_ids = []
            for vndb_id in vndb_ids:
                if isinstance(vndb_id, str) and re.match(r'^v\d+$', vndb_id):
                    validated_ids.append(vndb_id)
                else:
                    logger.warning(f"Invalid VNDB ID format rejected: {vndb_id}")
            
            if not validated_ids:
                return {}
            
            # Safe: validated_ids contains only vN format strings
            placeholders = ','.join('?' * len(validated_ids))
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(f"""
                    SELECT vndb_id, month, year FROM vn_of_month 
                    WHERE vndb_id IN ({placeholders})
                """, validated_ids) as cursor:
                    rows = await cursor.fetchall()
                    return {row['vndb_id']: {'month': row['month'], 'year': row['year']} for row in rows}
        except Exception as e:
            logger.error(f"Error getting VN month status: {e}")
            return {}

    # Backup Channel Management Methods
    
    async def _ensure_backup_channels_table(self):
        """Ensure backup_channels table exists"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS backup_channels (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        channel_id INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(guild_id, channel_id)
                    )
                """)
                await db.commit()
        except Exception as e:
            logger.error(f"Error ensuring backup_channels table: {e}")
    
    async def set_backup_channel(self, guild_id: int, channel_id: int) -> bool:
        """Set a backup channel for a guild"""
        try:
            await self._ensure_backup_channels_table()
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO backup_channels (guild_id, channel_id)
                    VALUES (?, ?)
                """, (guild_id, channel_id))
                await db.commit()
                logger.info(f"Set backup channel {channel_id} for guild {guild_id}")
                return True
        except Exception as e:
            logger.error(f"Error setting backup channel: {e}")
            return False
    
    async def get_backup_channels(self) -> List[Dict[str, Any]]:
        """Get all backup channels across all guilds"""
        try:
            await self._ensure_backup_channels_table()
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT guild_id, channel_id, created_at FROM backup_channels
                    ORDER BY created_at ASC
                """) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting backup channels: {e}")
            return []
    
    async def get_guild_backup_channel(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Get backup channel for a specific guild"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT guild_id, channel_id, created_at FROM backup_channels
                    WHERE guild_id = ?
                """, (guild_id,)) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting guild backup channel: {e}")
            return None
    
    async def remove_backup_channel(self, guild_id: int) -> bool:
        """Remove backup channel for a guild"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    DELETE FROM backup_channels WHERE guild_id = ?
                """, (guild_id,))
                await db.commit()
                if cursor.rowcount > 0:
                    logger.info(f"Removed backup channel for guild {guild_id}")
                    return True
                return False
        except Exception as e:
            logger.error(f"Error removing backup channel: {e}")
            return False

    # Error Log Channel Management Methods

    async def _ensure_error_log_channels_table(self):
        """Ensure error_log_channels table exists"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS error_log_channels (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        channel_id INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(guild_id)
                    )
                """)
                await db.commit()
        except Exception as e:
            logger.error(f"Error ensuring error_log_channels table: {e}")

    async def set_error_log_channel(self, guild_id: int, channel_id: int) -> bool:
        """Set an error log channel for a guild"""
        try:
            await self._ensure_error_log_channels_table()
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO error_log_channels (guild_id, channel_id)
                    VALUES (?, ?)
                """, (guild_id, channel_id))
                await db.commit()
                logger.info(f"Set error log channel {channel_id} for guild {guild_id}")
                return True
        except Exception as e:
            logger.error(f"Error setting error log channel: {e}")
            return False

    async def get_error_log_channels(self) -> List[Dict[str, Any]]:
        """Get all configured error log channels"""
        try:
            await self._ensure_error_log_channels_table()
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT guild_id, channel_id, created_at FROM error_log_channels
                    ORDER BY created_at ASC
                """) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting error log channels: {e}")
            return []

    async def get_guild_error_log_channel(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Get the error log channel for a specific guild"""
        try:
            await self._ensure_error_log_channels_table()
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT guild_id, channel_id, created_at FROM error_log_channels
                    WHERE guild_id = ?
                """, (guild_id,)) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting guild error log channel: {e}")
            return None

    async def remove_error_log_channel(self, guild_id: int) -> bool:
        """Remove the error log channel configuration for a guild"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    DELETE FROM error_log_channels WHERE guild_id = ?
                """, (guild_id,))
                await db.commit()
                if cursor.rowcount > 0:
                    logger.info(f"Removed error log channel for guild {guild_id}")
                    return True
                return False
        except Exception as e:
            logger.error(f"Error removing error log channel: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Message word-filter
    # ------------------------------------------------------------------ #

    async def _ensure_message_filter_tables(self):
        """Create the message-filter tables if missing. initialize() also creates
        them; this guards callers (e.g. the web console) that may touch the DB
        before the cog has run initialize()."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS message_filters (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        pattern TEXT NOT NULL,
                        replacement TEXT NOT NULL DEFAULT '',
                        is_regex INTEGER NOT NULL DEFAULT 0,
                        ignore_case INTEGER NOT NULL DEFAULT 1,
                        whole_word INTEGER NOT NULL DEFAULT 0,
                        match_plurals INTEGER NOT NULL DEFAULT 0,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        created_by TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # A message_filters table from before the match_plurals column needs
                # the ALTER here too (not just in _run_migrations), so a web-console
                # request that lands before the cog runs initialize() on an upgrade
                # boot does not hit "no such column: match_plurals".
                async with db.execute("PRAGMA table_info(message_filters)") as cursor:
                    cols = [row[1] for row in await cursor.fetchall()]
                if 'match_plurals' not in cols:
                    await db.execute("ALTER TABLE message_filters ADD COLUMN match_plurals INTEGER NOT NULL DEFAULT 0")
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS message_filter_settings (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        enabled INTEGER NOT NULL DEFAULT 1,
                        log_channel_id INTEGER,
                        rules_version INTEGER NOT NULL DEFAULT 0
                    )
                """)
                await db.execute(
                    "INSERT OR IGNORE INTO message_filter_settings (id, enabled, rules_version) VALUES (1, 0, 0)"
                )
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS message_filter_excluded_roles (
                        role_id INTEGER PRIMARY KEY,
                        added_by TEXT,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS message_filter_excluded_channels (
                        channel_id INTEGER PRIMARY KEY,
                        added_by TEXT,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS message_filter_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        author_id INTEGER NOT NULL,
                        original_content TEXT NOT NULL,
                        cleaned_content TEXT NOT NULL,
                        matched_rule_ids TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.commit()
        except Exception as e:
            logger.error(f"Error ensuring message filter tables: {e}")

    async def _bump_message_filter_version(self, db):
        """Increment rules_version on the settings row (within an open tx). The
        running cog watches this value and reloads its compiled rules when it
        changes, so console/command edits go live without a restart."""
        await db.execute(
            "UPDATE message_filter_settings SET rules_version = rules_version + 1 WHERE id = 1"
        )

    async def get_message_filter_rules(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """Return filter rules ordered by sort_order then id."""
        await self._ensure_message_filter_tables()
        query = (
            "SELECT id, pattern, replacement, is_regex, ignore_case, whole_word, "
            "match_plurals, enabled, sort_order, created_by, created_at FROM message_filters"
        )
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY sort_order ASC, id ASC"
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(query) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting message filter rules: {e}")
            return []

    async def add_message_filter_rule(self, pattern: str, replacement: str = "", is_regex: bool = False,
                                       ignore_case: bool = True, whole_word: bool = False,
                                       match_plurals: bool = False,
                                       created_by: Optional[str] = None) -> Optional[int]:
        """Insert a rule, bump the version, and return the new row id (or None)."""
        await self._ensure_message_filter_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "INSERT INTO message_filters (pattern, replacement, is_regex, ignore_case, whole_word, match_plurals, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (pattern, replacement or "", int(bool(is_regex)), int(bool(ignore_case)),
                     int(bool(whole_word)), int(bool(match_plurals)),
                     str(created_by) if created_by is not None else None),
                )
                await self._bump_message_filter_version(db)
                await db.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Error adding message filter rule: {e}")
            return None

    async def update_message_filter_rule(self, rule_id: int, *, pattern: Optional[str] = None,
                                         replacement: Optional[str] = None, is_regex: Optional[bool] = None,
                                         ignore_case: Optional[bool] = None, whole_word: Optional[bool] = None,
                                         match_plurals: Optional[bool] = None,
                                         enabled: Optional[bool] = None) -> bool:
        """Update the supplied fields on a rule and bump the version."""
        await self._ensure_message_filter_tables()
        fields: List[str] = []
        params: List[Any] = []
        if pattern is not None:
            fields.append("pattern = ?"); params.append(pattern)
        if replacement is not None:
            fields.append("replacement = ?"); params.append(replacement)
        if is_regex is not None:
            fields.append("is_regex = ?"); params.append(int(bool(is_regex)))
        if ignore_case is not None:
            fields.append("ignore_case = ?"); params.append(int(bool(ignore_case)))
        if whole_word is not None:
            fields.append("whole_word = ?"); params.append(int(bool(whole_word)))
        if match_plurals is not None:
            fields.append("match_plurals = ?"); params.append(int(bool(match_plurals)))
        if enabled is not None:
            fields.append("enabled = ?"); params.append(int(bool(enabled)))
        if not fields:
            return False
        params.append(int(rule_id))
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    f"UPDATE message_filters SET {', '.join(fields)} WHERE id = ?", params
                )
                await self._bump_message_filter_version(db)
                await db.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating message filter rule: {e}")
            return False

    async def delete_message_filter_rule(self, rule_id: int) -> bool:
        """Delete a rule and bump the version."""
        await self._ensure_message_filter_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("DELETE FROM message_filters WHERE id = ?", (int(rule_id),))
                await self._bump_message_filter_version(db)
                await db.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error deleting message filter rule: {e}")
            return False

    async def get_message_filter_settings(self) -> Dict[str, Any]:
        """Return the single settings row: enabled, log_channel_id, rules_version."""
        await self._ensure_message_filter_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT enabled, log_channel_id, rules_version FROM message_filter_settings WHERE id = 1"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return {
                            "enabled": bool(row["enabled"]),
                            "log_channel_id": row["log_channel_id"],
                            "rules_version": row["rules_version"],
                        }
                    return {"enabled": False, "log_channel_id": None, "rules_version": 0}
        except Exception as e:
            logger.error(f"Error getting message filter settings: {e}")
            # Fail closed on error so a DB hiccup never silently starts filtering.
            return {"enabled": False, "log_channel_id": None, "rules_version": 0}

    async def set_message_filter_enabled(self, enabled: bool) -> bool:
        """Toggle the global filter on/off."""
        await self._ensure_message_filter_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE message_filter_settings SET enabled = ? WHERE id = 1", (int(bool(enabled)),)
                )
                await self._bump_message_filter_version(db)
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error setting message filter enabled: {e}")
            return False

    async def set_message_filter_log_channel(self, channel_id: Optional[int]) -> bool:
        """Set (or clear, with None) the mod-log channel for filtered originals."""
        await self._ensure_message_filter_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE message_filter_settings SET log_channel_id = ? WHERE id = 1",
                    (int(channel_id) if channel_id is not None else None,),
                )
                await self._bump_message_filter_version(db)
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error setting message filter log channel: {e}")
            return False

    async def get_message_filter_excluded_roles(self) -> List[int]:
        """Return the list of role ids exempt from filtering."""
        await self._ensure_message_filter_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT role_id FROM message_filter_excluded_roles") as cursor:
                    rows = await cursor.fetchall()
                    return [int(row[0]) for row in rows]
        except Exception as e:
            logger.error(f"Error getting message filter excluded roles: {e}")
            return []

    async def add_message_filter_excluded_role(self, role_id: int, added_by: Optional[str] = None) -> bool:
        """Exempt a role from filtering."""
        await self._ensure_message_filter_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO message_filter_excluded_roles (role_id, added_by) VALUES (?, ?)",
                    (int(role_id), str(added_by) if added_by is not None else None),
                )
                await self._bump_message_filter_version(db)
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding message filter excluded role: {e}")
            return False

    async def remove_message_filter_excluded_role(self, role_id: int) -> bool:
        """Stop exempting a role."""
        await self._ensure_message_filter_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "DELETE FROM message_filter_excluded_roles WHERE role_id = ?", (int(role_id),)
                )
                await self._bump_message_filter_version(db)
                await db.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error removing message filter excluded role: {e}")
            return False

    async def get_message_filter_excluded_channels(self) -> List[int]:
        """Return channel ids where the whole pipeline (filter + media review) is skipped."""
        await self._ensure_message_filter_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT channel_id FROM message_filter_excluded_channels") as cursor:
                    rows = await cursor.fetchall()
                    return [int(row[0]) for row in rows]
        except Exception as e:
            logger.error(f"Error getting message filter excluded channels: {e}")
            return []

    async def add_message_filter_excluded_channel(self, channel_id: int, added_by: Optional[str] = None) -> bool:
        """Skip the whole moderation pipeline (filter + media review) in a channel."""
        await self._ensure_message_filter_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO message_filter_excluded_channels (channel_id, added_by) VALUES (?, ?)",
                    (int(channel_id), str(added_by) if added_by is not None else None),
                )
                await self._bump_message_filter_version(db)
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding message filter excluded channel: {e}")
            return False

    async def remove_message_filter_excluded_channel(self, channel_id: int) -> bool:
        """Resume the pipeline in a channel."""
        await self._ensure_message_filter_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "DELETE FROM message_filter_excluded_channels WHERE channel_id = ?", (int(channel_id),)
                )
                await self._bump_message_filter_version(db)
                await db.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error removing message filter excluded channel: {e}")
            return False

    async def add_message_filter_log(self, channel_id: int, message_id: int, author_id: int,
                                     original_content: str, cleaned_content: str,
                                     matched_rule_ids: Any) -> bool:
        """Record a filtered original for the mod-log viewer."""
        await self._ensure_message_filter_tables()
        matched = matched_rule_ids if isinstance(matched_rule_ids, str) else json.dumps(matched_rule_ids)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO message_filter_logs "
                    "(channel_id, message_id, author_id, original_content, cleaned_content, matched_rule_ids) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (int(channel_id), int(message_id), int(author_id), original_content, cleaned_content, matched),
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding message filter log: {e}")
            return False

    async def get_message_filter_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return the most recent filtered-message log entries."""
        await self._ensure_message_filter_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT id, channel_id, message_id, author_id, original_content, cleaned_content, "
                    "matched_rule_ids, created_at FROM message_filter_logs ORDER BY created_at DESC, id DESC LIMIT ?",
                    (int(limit),),
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting message filter logs: {e}")
            return []

    # Bumped on any media-flag config write (enabled/excluded-roles/opt-outs) so the cog can
    # cache should_attach's config and reload only when it actually changes, instead
    # of reading the DB on every message. Class-level so the bot's and web console's
    # Database instances (same process) share one counter.
    _media_flag_config_version = 0

    def _bump_media_flag_config_version(self):
        Database._media_flag_config_version += 1

    def get_media_flag_config_version(self) -> int:
        return Database._media_flag_config_version

    async def _ensure_media_flag_tables(self):
        """Create the media-flag tables if missing. initialize() also creates
        them; this guards callers (e.g. the web console) that may touch the DB
        before the cog has run initialize()."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS media_flag_settings (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        enabled INTEGER NOT NULL DEFAULT 0,
                        log_channel_id INTEGER,
                        threshold INTEGER NOT NULL DEFAULT 2
                    )
                """)
                await db.execute(
                    "INSERT OR IGNORE INTO media_flag_settings (id, enabled, threshold) VALUES (1, 0, 2)"
                )
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS media_flag_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_channel_id INTEGER NOT NULL,
                        source_message_id INTEGER NOT NULL,
                        repost_channel_id INTEGER NOT NULL,
                        repost_message_id INTEGER NOT NULL,
                        author_id INTEGER NOT NULL,
                        report_message_id INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(repost_message_id)
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS media_flag_votes (
                        item_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        vote TEXT NOT NULL CHECK (vote IN ('flag','safe')),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (item_id, user_id)
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS media_flag_excluded_roles (
                        role_id INTEGER PRIMARY KEY,
                        added_by TEXT,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS media_flag_ignored_roles (
                        role_id INTEGER PRIMARY KEY,
                        added_by TEXT,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS media_flag_optout_users (
                        user_id INTEGER PRIMARY KEY,
                        added_by TEXT,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS media_flag_reports (
                        item_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (item_id, user_id)
                    )
                """)
                await db.commit()
        except Exception as e:
            logger.error(f"Error ensuring media flag tables: {e}")

    async def get_media_flag_settings(self) -> Dict[str, Any]:
        """Return the single settings row: enabled, log_channel_id, threshold."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT enabled, log_channel_id, threshold FROM media_flag_settings WHERE id = 1"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return {
                            "enabled": bool(row["enabled"]),
                            "log_channel_id": row["log_channel_id"],
                            "threshold": row["threshold"],
                        }
                    return {"enabled": False, "log_channel_id": None, "threshold": 2}
        except Exception as e:
            logger.error(f"Error getting media flag settings: {e}")
            # Fail closed on error so a DB hiccup never silently starts flagging.
            return {"enabled": False, "log_channel_id": None, "threshold": 2}

    async def set_media_flag_enabled(self, enabled: bool) -> bool:
        """Toggle media flagging on/off."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE media_flag_settings SET enabled = ? WHERE id = 1", (int(bool(enabled)),)
                )
                await db.commit()
                self._bump_media_flag_config_version()
                return True
        except Exception as e:
            logger.error(f"Error setting media flag enabled: {e}")
            return False

    async def set_media_flag_log_channel(self, channel_id: Optional[int]) -> bool:
        """Set (or clear, with None) the mod-log channel for flagged media."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE media_flag_settings SET log_channel_id = ? WHERE id = 1",
                    (int(channel_id) if channel_id is not None else None,),
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error setting media flag log channel: {e}")
            return False

    async def set_media_flag_threshold(self, threshold: int) -> bool:
        """Set the flag-vote count needed to trigger a report."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE media_flag_settings SET threshold = ? WHERE id = 1", (int(threshold),)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error setting media flag threshold: {e}")
            return False

    async def add_media_flag_item(self, source_channel_id: int, source_message_id: int,
                                  repost_channel_id: int, repost_message_id: int,
                                  author_id: int) -> Optional[int]:
        """Record a reposted media item and return the new row id (or None)."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "INSERT INTO media_flag_items "
                    "(source_channel_id, source_message_id, repost_channel_id, repost_message_id, author_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (int(source_channel_id), int(source_message_id), int(repost_channel_id),
                     int(repost_message_id), int(author_id)),
                )
                await db.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Error adding media flag item: {e}")
            return None

    async def get_media_flag_item_by_repost(self, repost_message_id: int) -> Optional[Dict[str, Any]]:
        """Return the media item for a repost message id, or None."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT id, source_channel_id, source_message_id, repost_channel_id, "
                    "repost_message_id, author_id, report_message_id, created_at "
                    "FROM media_flag_items WHERE repost_message_id = ?",
                    (int(repost_message_id),),
                ) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting media flag item by repost: {e}")
            return None

    async def set_media_flag_report_message(self, item_id: int, report_message_id: int) -> bool:
        """Attach the mod-report message id to an item once it is posted."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "UPDATE media_flag_items SET report_message_id = ? WHERE id = ?",
                    (int(report_message_id), int(item_id)),
                )
                await db.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error setting media flag report message: {e}")
            return False

    async def record_media_flag_vote(self, item_id: int, user_id: int, vote: str) -> bool:
        """Record (or change) a user's flag/safe vote on an item. Upserts so a
        user switching their vote does not double-count."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO media_flag_votes (item_id, user_id, vote) VALUES (?, ?, ?)",
                    (int(item_id), int(user_id), str(vote)),
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error recording media flag vote: {e}")
            return False

    async def count_media_flag_votes(self, item_id: int) -> Dict[str, int]:
        """Return {"flag": n, "safe": m} vote counts for an item (0 if missing)."""
        await self._ensure_media_flag_tables()
        counts = {"flag": 0, "safe": 0}
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT vote, COUNT(*) FROM media_flag_votes WHERE item_id = ? GROUP BY vote",
                    (int(item_id),),
                ) as cursor:
                    rows = await cursor.fetchall()
                    for vote, n in rows:
                        counts[vote] = int(n)
                    return counts
        except Exception as e:
            logger.error(f"Error counting media flag votes: {e}")
            return counts

    async def get_media_flag_voters(self, item_id: int, vote: str) -> List[int]:
        """Return the user ids who cast the given vote on an item."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT user_id FROM media_flag_votes WHERE item_id = ? AND vote = ?",
                    (int(item_id), str(vote)),
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [int(row[0]) for row in rows]
        except Exception as e:
            logger.error(f"Error getting media flag voters: {e}")
            return []

    async def claim_media_flag_report(self, item_id: int, user_id: int) -> bool:
        """Atomically record that a mod-log report has been posted for this flagger on
        this item, returning True only the FIRST time. So a user cannot re-trigger a
        report by toggling their vote (flag -> safe -> flag): each person's flag on a
        given item posts at most one report, ever."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO media_flag_reports (item_id, user_id) VALUES (?, ?)",
                    (int(item_id), int(user_id)),
                )
                await db.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error claiming media flag report: {e}")
            return False

    async def get_media_flag_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Recent flagged media items with their vote tallies, in one aggregate query
        rather than one query per item."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """
                    SELECT i.id, i.source_channel_id, i.source_message_id, i.repost_channel_id,
                           i.repost_message_id, i.author_id, i.report_message_id, i.created_at,
                           COALESCE(SUM(CASE WHEN v.vote = 'flag' THEN 1 ELSE 0 END), 0) AS flag_count,
                           COALESCE(SUM(CASE WHEN v.vote = 'safe' THEN 1 ELSE 0 END), 0) AS safe_count
                    FROM media_flag_items i
                    LEFT JOIN media_flag_votes v ON v.item_id = i.id
                    GROUP BY i.id
                    ORDER BY i.created_at DESC, i.id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ) as cursor:
                    return [dict(r) for r in await cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error getting media flag logs: {e}")
            return []

    async def get_media_flag_excluded_roles(self) -> List[int]:
        """Return role ids whose members' media is exempt from reposting."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT role_id FROM media_flag_excluded_roles") as cursor:
                    rows = await cursor.fetchall()
                    return [int(row[0]) for row in rows]
        except Exception as e:
            logger.error(f"Error getting media flag excluded roles: {e}")
            return []

    async def add_media_flag_excluded_role(self, role_id: int, added_by: Optional[str] = None) -> bool:
        """Exempt a role from media reposting."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO media_flag_excluded_roles (role_id, added_by) VALUES (?, ?)",
                    (int(role_id), str(added_by) if added_by is not None else None),
                )
                await db.commit()
                self._bump_media_flag_config_version()
                return True
        except Exception as e:
            logger.error(f"Error adding media flag excluded role: {e}")
            return False

    async def remove_media_flag_excluded_role(self, role_id: int) -> bool:
        """Stop exempting a role from media reposting."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "DELETE FROM media_flag_excluded_roles WHERE role_id = ?", (int(role_id),)
                )
                await db.commit()
                self._bump_media_flag_config_version()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error removing media flag excluded role: {e}")
            return False

    async def get_media_flag_ignored_roles(self) -> List[int]:
        """Return role ids whose members may not cast flag/safe votes."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT role_id FROM media_flag_ignored_roles") as cursor:
                    rows = await cursor.fetchall()
                    return [int(row[0]) for row in rows]
        except Exception as e:
            logger.error(f"Error getting media flag ignored roles: {e}")
            return []

    async def add_media_flag_ignored_role(self, role_id: int, added_by: Optional[str] = None) -> bool:
        """Bar a role from casting flag/safe votes."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO media_flag_ignored_roles (role_id, added_by) VALUES (?, ?)",
                    (int(role_id), str(added_by) if added_by is not None else None),
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding media flag ignored role: {e}")
            return False

    async def remove_media_flag_ignored_role(self, role_id: int) -> bool:
        """Let a role cast flag/safe votes again."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "DELETE FROM media_flag_ignored_roles WHERE role_id = ?", (int(role_id),)
                )
                await db.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error removing media flag ignored role: {e}")
            return False

    async def get_media_flag_optout_users(self) -> List[Dict[str, Any]]:
        """Members who asked for their media not to be reposted, newest first."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT user_id, added_by, added_at FROM media_flag_optout_users "
                    "ORDER BY added_at DESC, user_id DESC"
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [{"user_id": int(r[0]), "added_by": r[1], "added_at": r[2]} for r in rows]
        except Exception as e:
            logger.error(f"Error getting media flag opt-out users: {e}")
            return []

    async def get_media_flag_optout_user_ids(self) -> List[int]:
        """Just the ids, for the cog's per-message membership check."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT user_id FROM media_flag_optout_users") as cursor:
                    rows = await cursor.fetchall()
                    return [int(row[0]) for row in rows]
        except Exception as e:
            logger.error(f"Error getting media flag opt-out user ids: {e}")
            return []

    async def is_media_flag_opted_out(self, user_id: int) -> bool:
        """Whether this member has opted their media out of reposting."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT 1 FROM media_flag_optout_users WHERE user_id = ?", (int(user_id),)
                ) as cursor:
                    return await cursor.fetchone() is not None
        except Exception as e:
            logger.error(f"Error checking media flag opt-out: {e}")
            return False

    async def add_media_flag_optout_user(self, user_id: int, added_by: Optional[str] = None) -> bool:
        """Exempt one member's media from reposting. added_by records who set it
        ('self' when the member ran the command, otherwise an admin id or 'web-console')."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO media_flag_optout_users (user_id, added_by) VALUES (?, ?)",
                    (int(user_id), str(added_by) if added_by is not None else None),
                )
                await db.commit()
                self._bump_media_flag_config_version()
                return True
        except Exception as e:
            logger.error(f"Error adding media flag opt-out user: {e}")
            return False

    async def remove_media_flag_optout_user(self, user_id: int) -> bool:
        """Put one member's media back in scope for reposting."""
        await self._ensure_media_flag_tables()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "DELETE FROM media_flag_optout_users WHERE user_id = ?", (int(user_id),)
                )
                await db.commit()
                self._bump_media_flag_config_version()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error removing media flag opt-out user: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Repost authors (so highlights can treat the bot's webhook reposts as the
    # original poster's message rather than a non-user)
    # ------------------------------------------------------------------ #

    async def _ensure_repost_authors_table(self):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS repost_authors (
                        repost_message_id INTEGER PRIMARY KEY,
                        author_id INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.commit()
        except Exception as e:
            logger.error(f"Error ensuring repost_authors table: {e}")

    async def add_repost_author(self, repost_message_id: int, author_id: int) -> bool:
        """Record that a repost (webhook message) stands in for a given poster."""
        await self._ensure_repost_authors_table()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO repost_authors (repost_message_id, author_id) VALUES (?, ?)",
                    (int(repost_message_id), int(author_id)),
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding repost author: {e}")
            return False

    async def get_repost_author(self, repost_message_id: int) -> Optional[int]:
        """Return the original poster's user id for one of the bot's reposts, or None
        if the message id is not a recorded repost."""
        await self._ensure_repost_authors_table()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT author_id FROM repost_authors WHERE repost_message_id = ?",
                    (int(repost_message_id),),
                ) as cursor:
                    row = await cursor.fetchone()
                    return int(row[0]) if row else None
        except Exception as e:
            logger.error(f"Error getting repost author: {e}")
            return None

    # User Completion System Methods
    
    async def mark_vn_completed(self, user_id: int, pool_id: int, rating: int = None, review: str = None, completed_date: str = None) -> bool:
        """Mark a VN as completed by a user"""
        try:
            # First get the VNDB ID from the pool entry
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT vndb_id FROM vn_pool WHERE id = ?", (pool_id,)) as cursor:
                    pool_entry = await cursor.fetchone()
                    
                if not pool_entry:
                    logger.error(f"Pool entry {pool_id} not found")
                    return False
                
                vndb_id = pool_entry['vndb_id']
                
                # Use provided date or current timestamp
                if completed_date:
                    # Insert or update the completion with custom date
                    await db.execute("""
                        INSERT OR REPLACE INTO user_completions 
                        (user_id, pool_id, vndb_id, rating, review, completed_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (user_id, pool_id, vndb_id, rating, review, completed_date))
                else:
                    # Insert or update the completion with current timestamp
                    await db.execute("""
                        INSERT OR REPLACE INTO user_completions 
                        (user_id, pool_id, vndb_id, rating, review)
                        VALUES (?, ?, ?, ?, ?)
                    """, (user_id, pool_id, vndb_id, rating, review))
                await db.commit()
                return True
                
        except Exception as e:
            logger.error(f"Error marking VN as completed: {e}")
            return False
    
    async def get_user_completions(self, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        """Get all VNs completed by a user"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT uc.*, vp.title, vp.vndb_id, vp.target_month, vp.target_year, vp.image_url, vp.rating as vndb_rating, vp.length, vp.status
                    FROM user_completions uc
                    JOIN vn_pool vp ON uc.pool_id = vp.id
                    WHERE uc.user_id = ?
                    ORDER BY uc.completed_at DESC
                    LIMIT ?
                """, (user_id, limit)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
                    
        except Exception as e:
            logger.error(f"Error getting user completions: {e}")
            return []
    
    async def get_user_completion_stats(self, user_id: int) -> Dict[str, Any]:
        """Get completion statistics for a user"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                # Get total completions
                async with db.execute("""
                    SELECT COUNT(*) as total_completed FROM user_completions WHERE user_id = ?
                """, (user_id,)) as cursor:
                    total_row = await cursor.fetchone()
                    total_completed = total_row['total_completed'] if total_row else 0
                
                # Get average rating
                async with db.execute("""
                    SELECT AVG(rating) as avg_rating FROM user_completions 
                    WHERE user_id = ? AND rating IS NOT NULL
                """, (user_id,)) as cursor:
                    avg_row = await cursor.fetchone()
                    avg_rating = avg_row['avg_rating'] if avg_row and avg_row['avg_rating'] else None
                
                # Get completions by month
                async with db.execute("""
                    SELECT strftime('%Y-%m', completed_at) as month, COUNT(*) as count
                    FROM user_completions 
                    WHERE user_id = ?
                    GROUP BY strftime('%Y-%m', completed_at)
                    ORDER BY month DESC
                    LIMIT 12
                """, (user_id,)) as cursor:
                    monthly_rows = await cursor.fetchall()
                    monthly_completions = [dict(row) for row in monthly_rows]
                
                # Get VN of the Month completions
                async with db.execute("""
                    SELECT COUNT(*) as vn_of_month_completed
                    FROM user_completions uc
                    JOIN vn_pool vp ON uc.pool_id = vp.id
                    WHERE uc.user_id = ? AND vp.status = 'vn_of_month'
                """, (user_id,)) as cursor:
                    vn_month_row = await cursor.fetchone()
                    vn_of_month_completed = vn_month_row['vn_of_month_completed'] if vn_month_row else 0
                
                return {
                    'total_completed': total_completed,
                    'average_rating': round(avg_rating, 1) if avg_rating else None,
                    'monthly_completions': monthly_completions,
                    'vn_of_month_completed': vn_of_month_completed
                }
                
        except Exception as e:
            logger.error(f"Error getting user completion stats: {e}")
            return {
                'total_completed': 0,
                'average_rating': None,
                'monthly_completions': [],
                'vn_of_month_completed': 0
            }
    
    async def is_vn_completed_by_user(self, user_id: int, pool_id: int) -> bool:
        """Check if a VN is already completed by a user"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT id FROM user_completions WHERE user_id = ? AND pool_id = ?
                """, (user_id, pool_id)) as cursor:
                    row = await cursor.fetchone()
                    return row is not None
                    
        except Exception as e:
            logger.error(f"Error checking VN completion: {e}")
            return False
    
    async def remove_completion(self, user_id: int, pool_id: int) -> bool:
        """Remove a completion entry"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    DELETE FROM user_completions WHERE user_id = ? AND pool_id = ?
                """, (user_id, pool_id))
                await db.commit()
                return True
                
        except Exception as e:
            logger.error(f"Error removing completion: {e}")
            return False

    async def get_leaderboard(self, timeframe: str, month: int = None, year: int = None) -> List[Dict[str, Any]]:
        """Get leaderboard data for specified timeframe"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                if timeframe == "all-time":
                    # All-time leaderboard - no date filtering
                    query = """
                        SELECT uc.user_id, COUNT(*) as completion_count,
                               SUM(CASE 
                                   WHEN vp.length IS NULL THEN 100
                                   WHEN vp.length = 1 THEN 100 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   WHEN vp.length = 2 THEN 120 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   WHEN vp.length = 3 THEN 160 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   WHEN vp.length = 4 THEN 200 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   WHEN vp.length = 5 THEN 250 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   ELSE 100
                               END) as total_points
                        FROM user_completions uc
                        JOIN vn_pool vp ON uc.pool_id = vp.id
                        GROUP BY uc.user_id
                        ORDER BY total_points DESC, completion_count DESC
                    """
                    params = ()
                    
                elif timeframe == "monthly":
                    # Monthly leaderboard
                    query = """
                        SELECT uc.user_id, COUNT(*) as completion_count,
                               SUM(CASE 
                                   WHEN vp.length IS NULL THEN 100
                                   WHEN vp.length = 1 THEN 100 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   WHEN vp.length = 2 THEN 120 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   WHEN vp.length = 3 THEN 160 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   WHEN vp.length = 4 THEN 200 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   WHEN vp.length = 5 THEN 250 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   ELSE 100
                               END) as total_points
                        FROM user_completions uc
                        JOIN vn_pool vp ON uc.pool_id = vp.id
                        WHERE strftime('%Y', uc.completed_at) = ? AND strftime('%m', uc.completed_at) = ?
                        GROUP BY uc.user_id
                        ORDER BY total_points DESC, completion_count DESC
                    """
                    params = (str(year), f"{month:02d}")
                    
                elif timeframe == "quarterly":
                    # Quarterly leaderboard
                    quarter_months = {
                        1: ["01", "02", "03"],
                        2: ["04", "05", "06"], 
                        3: ["07", "08", "09"],
                        4: ["10", "11", "12"]
                    }
                    quarter = ((month - 1) // 3) + 1
                    months_in_quarter = quarter_months[quarter]
                    
                    query = """
                        SELECT uc.user_id, COUNT(*) as completion_count,
                               SUM(CASE 
                                   WHEN vp.length IS NULL THEN 100
                                   WHEN vp.length = 1 THEN 100 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   WHEN vp.length = 2 THEN 120 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   WHEN vp.length = 3 THEN 160 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   WHEN vp.length = 4 THEN 200 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   WHEN vp.length = 5 THEN 250 * 
                                       CASE WHEN vp.status = 'vn_of_month' THEN 2 ELSE 1 END *
                                       CASE WHEN strftime('%Y-%m', uc.completed_at) = printf('%04d-%02d', vp.target_year, vp.target_month) THEN 1.5 ELSE 1 END
                                   ELSE 100
                               END) as total_points
                        FROM user_completions uc
                        JOIN vn_pool vp ON uc.pool_id = vp.id
                        WHERE strftime('%Y', uc.completed_at) = ? AND strftime('%m', uc.completed_at) IN (?, ?, ?)
                        GROUP BY uc.user_id
                        ORDER BY total_points DESC, completion_count DESC
                    """
                    params = (str(year), months_in_quarter[0], months_in_quarter[1], months_in_quarter[2])
                
                else:
                    return []
                
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
                    
        except Exception as e:
            logger.error(f"Error getting leaderboard data: {e}")
            return []

    # Database Backup System
    
    def _resolve_backup_path(self, backup_name: str) -> Path:
        if not backup_name:
            raise ValueError("Backup name is required")

        if not self._backup_name_pattern.fullmatch(backup_name):
            raise ValueError("Backup name contains invalid characters")

        backup_dir = self.backup_dir.resolve()
        backup_path = (backup_dir / backup_name).resolve()

        if backup_path.parent != backup_dir:
            raise ValueError("Backup path must reside inside the backup directory")

        return backup_path

    async def create_backup(self, backup_name: str = None, bot=None) -> bool:
        """Create a backup of the database"""
        try:
            if not backup_name:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_name = f"vnbot_backup_{timestamp}.db"
            
            backup_path = self._resolve_backup_path(backup_name)
            backup_path.parent.mkdir(exist_ok=True)
            
            # Check if backup already exists (handle concurrent backup attempts)
            if backup_path.exists():
                logger.info(f"Database backup already exists: {backup_path} (skipping duplicate)")
                return True
            
            # Create backup using SQLite VACUUM INTO (safer than file copy for active databases)
            async with aiosqlite.connect(self.db_path) as db:
                backup_path_sql = str(backup_path).replace("'", "''")
                await db.execute(f"VACUUM INTO '{backup_path_sql}'")
            
            logger.info(f"Database backup created: {backup_path}")
            
            # Upload backup to Discord channels if bot is provided
            if bot:
                await self._upload_backup_to_channels(bot, backup_path)
            
            # Clean up old backups
            await self._cleanup_old_backups()
            
            return True
            
        except Exception as e:
            logger.error(f"Error creating database backup: {e}")
            return False
    
    async def _upload_backup_to_channels(self, bot, backup_path):
        """Upload backup file to all configured backup channels"""
        try:
            import discord
            
            backup_channels = await self.get_backup_channels()
            if not backup_channels:
                logger.info("No backup channels configured for Discord upload")
                return
            
            # Check if backup file exists
            if not backup_path.exists():
                logger.error(f"Backup file not found for Discord upload: {backup_path}")
                return
            
            file_size = backup_path.stat().st_size
            
            # Create embed with backup information
            embed = discord.Embed(
                title="🔒 Database Backup Created",
                description=f"Automatic backup of database completed successfully.",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            
            embed.add_field(
                name="Backup Details",
                value=f"**Filename:** `{backup_path.name}`\n"
                      f"**Size:** {file_size / (1024*1024):.2f} MB\n"
                      f"**Created:** <t:{int(datetime.now().timestamp())}:F>",
                inline=False
            )
            
            embed.set_footer(text="Backup files are automatically created and distributed to configured servers")
            
            # Upload to all configured backup channels
            success_count = 0
            for channel_info in backup_channels:
                try:
                    channel = bot.get_channel(channel_info['channel_id'])
                    if not channel:
                        logger.warning(f"Backup channel not found: {channel_info['channel_id']} in guild {channel_info['guild_id']}")
                        continue
                    
                    # Create a new file object for each upload (Discord files can only be sent once)
                    channel_file = discord.File(backup_path, filename=backup_path.name)
                    
                    await channel.send(embed=embed, file=channel_file)
                    success_count += 1
                    logger.info(f"Backup uploaded to channel {channel_info['channel_id']} in guild {channel_info['guild_id']}")
                    
                except discord.errors.Forbidden:
                    logger.error(f"No permission to send files to channel {channel_info['channel_id']}")
                except discord.errors.HTTPException as e:
                    if "Request entity too large" in str(e) or "413" in str(e):
                        logger.warning(f"Backup file too large for channel {channel_info['channel_id']} (size: {file_size / (1024*1024):.1f}MB)")
                    else:
                        logger.error(f"HTTP error uploading to channel {channel_info['channel_id']}: {e}")
                except FileNotFoundError:
                    logger.error(f"Backup file not found when uploading to channel {channel_info['channel_id']}")
                except Exception as e:
                    logger.error(f"Unexpected error uploading to channel {channel_info['channel_id']}: {e}")
            
            logger.info(f"Backup uploaded to {success_count}/{len(backup_channels)} configured channels")
                    
        except Exception as e:
            logger.error(f"Error uploading backup to Discord channels: {e}")
    
    async def create_backup_copy(self, backup_name: str = None) -> bool:
        """Create a backup using file copy (alternative method)"""
        try:
            if not backup_name:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_name = f"vnbot_backup_{timestamp}.db"
            
            backup_path = self._resolve_backup_path(backup_name)
            backup_path.parent.mkdir(exist_ok=True)
            
            # Use file copy method (less safe for active databases but more compatible)
            shutil.copy2(self.db_path, backup_path)
            
            logger.info(f"Database backup (copy) created: {backup_path}")
            
            # Clean up old backups
            await self._cleanup_old_backups()
            
            return True
            
        except Exception as e:
            logger.error(f"Error creating database backup copy: {e}")
            return False
    
    async def _cleanup_old_backups(self):
        """Remove old backup files, keeping only the most recent ones"""
        try:
            backup_files = list(self.backup_dir.glob("*.db"))
            
            # Sort by modification time (newest first)
            backup_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            
            # Remove excess backups
            if len(backup_files) > self.max_backups:
                for old_backup in backup_files[self.max_backups:]:
                    try:
                        old_backup.unlink()
                        logger.info(f"Removed old backup: {old_backup}")
                    except Exception as e:
                        logger.warning(f"Failed to remove old backup {old_backup}: {e}")
                        
        except Exception as e:
            logger.error(f"Error cleaning up old backups: {e}")
    
    async def list_backups(self) -> List[Dict[str, Any]]:
        """List all available backups"""
        try:
            backups = []
            backup_files = list(self.backup_dir.glob("*.db"))
            
            for backup_file in backup_files:
                stat = backup_file.stat()
                backups.append({
                    'name': backup_file.name,
                    'path': str(backup_file),
                    'size': stat.st_size,
                    'created': datetime.fromtimestamp(stat.st_mtime),
                    'size_mb': round(stat.st_size / (1024 * 1024), 2)
                })
            
            # Sort by creation time (newest first)
            backups.sort(key=lambda x: x['created'], reverse=True)
            
            return backups
            
        except Exception as e:
            logger.error(f"Error listing backups: {e}")
            return []
    
    async def restore_backup(self, backup_name: str) -> bool:
        """Restore database from a backup file"""
        try:
            backup_path = self._resolve_backup_path(backup_name)
            
            if not backup_path.exists():
                logger.error(f"Backup file not found: {backup_path}")
                return False
            
            # Create a backup of current database before restore
            await self.create_backup(f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
            
            # Close any existing connections and restore
            shutil.copy2(backup_path, self.db_path)
            
            logger.info(f"Database restored from backup: {backup_name}")
            return True
            
        except Exception as e:
            logger.error(f"Error restoring from backup: {e}")
            return False
    
    async def should_create_backup(self) -> bool:
        """Check if it's time to create a new backup"""
        try:
            backups = await self.list_backups()
            
            if not backups:
                return True  # No backups exist, create one
            
            latest_backup = backups[0]
            next_due = next_backup_run_after(latest_backup['created'])
            now = datetime.now(next_due.tzinfo) if next_due.tzinfo else datetime.now()

            return now >= next_due
            
        except Exception as e:
            logger.error(f"Error checking backup status: {e}")
            return False
    
    async def get_backup_status(self) -> Dict[str, Any]:
        """Get backup system status information"""
        try:
            backups = await self.list_backups()
            
            status = {
                'backup_count': len(backups),
                'max_backups': self.max_backups,
                'backup_interval_hours': self.backup_interval_hours,
                'backup_dir': str(self.backup_dir),
                'latest_backup': None,
                'next_backup_due': None,
                'total_backup_size_mb': 0
            }
            
            if backups:
                latest = backups[0]
                status['latest_backup'] = {
                    'name': latest['name'],
                    'created': latest['created'],
                    'size_mb': latest['size_mb']
                }
                
                status['next_backup_due'] = next_backup_run_after(latest['created'])
                
                status['total_backup_size_mb'] = round(sum(b['size'] for b in backups) / (1024 * 1024), 2)
            
            return status
            
        except Exception as e:
            logger.error(f"Error getting backup status: {e}")
            return {}
    
    # Immersion Tracking Methods
    
    async def create_immersion_activity(self, user_id: str, guild_id: str, name: str, 
                                      primary_type: str, duration: int, 
                                      media_type: str = None, date: str = None, 
                                      meta: dict = None, comment: str = None) -> int:
        """Create a new immersion activity"""
        try:
            import json
            meta_json = json.dumps(meta or {})
            normalized_date = self._normalize_activity_datetime(date)
            
            async with aiosqlite.connect(self.db_path) as db:
                # Ensure user and guild exist
                await db.execute(
                    "INSERT OR IGNORE INTO immersion_users (user_id) VALUES (?)",
                    (user_id,)
                )
                
                if guild_id:
                    await db.execute(
                        "INSERT OR IGNORE INTO immersion_guilds (guild_id) VALUES (?)",
                        (guild_id,)
                    )
                    await db.execute(
                        "INSERT OR REPLACE INTO immersion_guild_members (guild_id, user_id, last_seen_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                        (guild_id, user_id)
                    )
                
                # Create the activity
                cursor = await db.execute("""
                    INSERT INTO immersion_activities 
                    (user_id, guild_id, name, primary_type, media_type, duration, date, meta, comment)
                    VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?)
                """, (user_id, guild_id, name, primary_type, media_type, duration, normalized_date, meta_json, comment))
                
                await db.commit()
                return cursor.lastrowid
                
        except ValueError as e:
            logger.warning(f"Validation error creating immersion activity: {e}")
            raise
        except Exception as e:
            logger.error(f"Error creating immersion activity: {e}")
            return None
    
    async def get_user_activities(self, user_id: str, guild_id: str = None, 
                                limit: int = 50, offset: int = 0,
                                primary_type: str = None, media_type: str = None) -> List[Dict[str, Any]]:
        """Get user's immersion activities"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                filters = ["user_id = ?", "deleted_at IS NULL"]
                params: List[Any] = [user_id]

                if guild_id:
                    filters.append("guild_id = ?")
                    params.append(guild_id)

                if primary_type:
                    filters.append("primary_type = ?")
                    params.append(primary_type)

                if media_type:
                    filters.append("media_type = ?")
                    params.append(media_type)

                where_clause = " AND ".join(filters)

                query = f"""
                    SELECT * FROM immersion_activities 
                    WHERE {where_clause}
                    ORDER BY date DESC 
                    LIMIT ? OFFSET ?
                """

                params.extend([limit, offset])

                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
                    
        except Exception as e:
            logger.error(f"Error getting user activities: {e}")
            return []
    
    async def get_user_activity_count(self, user_id: str, guild_id: str = None,
                                    primary_type: str = None, media_type: str = None) -> int:
        """Get total count of user's activities"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                filters = ["user_id = ?", "deleted_at IS NULL"]
                params: List[Any] = [user_id]

                if guild_id:
                    filters.append("guild_id = ?")
                    params.append(guild_id)

                if primary_type:
                    filters.append("primary_type = ?")
                    params.append(primary_type)

                if media_type:
                    filters.append("media_type = ?")
                    params.append(media_type)

                where_clause = " AND ".join(filters)

                query = f"SELECT COUNT(*) FROM immersion_activities WHERE {where_clause}"

                async with db.execute(query, params) as cursor:
                    result = await cursor.fetchone()
                    return result[0] if result else 0
                    
        except Exception as e:
            logger.error(f"Error getting user activity count: {e}")
            return 0
    
    async def get_guild_leaderboard(self, guild_id: str, timeframe: str = 'all', 
                                  limit: int = 10) -> List[Dict[str, Any]]:
        """Get guild leaderboard for immersion activities"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                # Build time filter
                time_filter = ""
                params = [guild_id]
                
                if timeframe == 'today':
                    time_filter = "AND date >= date('now', 'start of day')"
                elif timeframe == 'week':
                    time_filter = "AND date >= date('now', 'weekday 0', '-6 days')"
                elif timeframe == 'month':
                    time_filter = "AND date >= date('now', 'start of month')"
                elif timeframe == 'year':
                    time_filter = "AND date >= date('now', 'start of year')"
                
                query = f"""
                    SELECT user_id, 
                           SUM(duration) as total_duration,
                           COUNT(*) as activity_count
                    FROM immersion_activities 
                    WHERE guild_id = ? AND deleted_at IS NULL {time_filter}
                    GROUP BY user_id 
                    ORDER BY total_duration DESC 
                    LIMIT ?
                """
                
                params.append(limit)
                
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
                    
        except Exception as e:
            logger.error(f"Error getting guild leaderboard: {e}")
            return []
    
    async def delete_activity(self, activity_id: int, user_id: str) -> bool:
        """Soft delete an activity (undo functionality)"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    UPDATE immersion_activities 
                    SET deleted_at = CURRENT_TIMESTAMP 
                    WHERE id = ? AND user_id = ? AND deleted_at IS NULL
                """, (activity_id, user_id))
                
                await db.commit()
                return cursor.rowcount > 0
                
        except Exception as e:
            logger.error(f"Error deleting activity: {e}")
            return False
    
    async def get_user_settings(self, user_id: str) -> Dict[str, Any]:
        """Get user's immersion tracking settings"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM immersion_users WHERE user_id = ?", 
                    (user_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
                    
        except Exception as e:
            logger.error(f"Error getting user settings: {e}")
            return None
    
    async def update_user_settings(self, user_id: str, **kwargs) -> bool:
        """Update user's immersion tracking settings"""
        try:
            # Filter valid settings (allow None to clear values)
            valid_fields = {
                'timezone', 'vn_reading_speed', 'book_reading_speed', 
                'manga_reading_speed', 'daily_goal', 'primary_guild_id'
            }

            updates = {k: kwargs[k] for k in valid_fields if k in kwargs}

            if not updates:
                return False
            
            async with aiosqlite.connect(self.db_path) as db:
                # Ensure user exists
                await db.execute(
                    "INSERT OR IGNORE INTO immersion_users (user_id) VALUES (?)",
                    (user_id,)
                )
                
                # Build update query
                set_clause = ', '.join(f"{k} = ?" for k in updates.keys())
                values = list(updates.values()) + [user_id]
                
                await db.execute(
                    f"UPDATE immersion_users SET {set_clause} WHERE user_id = ?",
                    values
                )
                
                await db.commit()
                return True
                
        except Exception as e:
            logger.error(f"Error updating user settings: {e}")
            return False
    
    # Import tracking methods
    async def record_import(self, user_id: str, import_timestamp: datetime, 
                          activity_count: int, filename: str = None) -> int:
        """Record an import operation"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    INSERT INTO immersion_imports (user_id, import_timestamp, activity_count, filename)
                    VALUES (?, ?, ?, ?)
                """, (user_id, import_timestamp.isoformat(), activity_count, filename))
                
                await db.commit()
                return cursor.lastrowid
                
        except Exception as e:
            logger.error(f"Error recording import: {e}")
            return None
    
    async def get_import_history(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get user's import history with status indicating if activities still exist"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                async with db.execute("""
                    SELECT i.*, 
                           COALESCE(COUNT(a.id), 0) as active_activities,
                           CASE WHEN COUNT(a.id) > 0 THEN 'Active' ELSE 'Undone' END as status
                    FROM immersion_imports i
                    LEFT JOIN immersion_activities a ON (
                        i.user_id = a.user_id 
                        AND i.import_timestamp = a.imported_at
                        AND a.deleted_at IS NULL
                    )
                    WHERE i.user_id = ? 
                    GROUP BY i.id
                    ORDER BY i.import_timestamp DESC 
                    LIMIT ?
                """, (user_id, limit)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
                    
        except Exception as e:
            logger.error(f"Error getting import history: {e}")
            return []

    async def cleanup_orphaned_imports(self, user_id: str = None) -> int:
        """Remove import records that have no associated activities"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                if user_id:
                    cursor = await db.execute("""
                        DELETE FROM immersion_imports 
                        WHERE user_id = ? 
                        AND import_timestamp NOT IN (
                            SELECT DISTINCT imported_at 
                            FROM immersion_activities 
                            WHERE user_id = ? AND imported_at IS NOT NULL AND deleted_at IS NULL
                        )
                    """, (user_id, user_id))
                else:
                    cursor = await db.execute("""
                        DELETE FROM immersion_imports 
                        WHERE import_timestamp NOT IN (
                            SELECT DISTINCT imported_at 
                            FROM immersion_activities 
                            WHERE imported_at IS NOT NULL AND deleted_at IS NULL
                        )
                    """)
                
                await db.commit()
                removed_count = cursor.rowcount
                logger.info(f"Cleaned up {removed_count} orphaned import records")
                return removed_count
                
        except Exception as e:
            logger.error(f"Error cleaning up orphaned imports: {e}")
            return 0

    async def import_activities_batch(self, activities: List[Dict[str, Any]], 
                                   import_timestamp: datetime) -> int:
        """Import multiple activities in a batch with import timestamp"""
        try:
            logger.info(f"Starting batch import of {len(activities)} activities")
            async with aiosqlite.connect(self.db_path) as db:
                inserted_count = 0
                
                for i, activity in enumerate(activities):
                    try:
                        # Set import timestamp
                        activity['imported_at'] = import_timestamp.isoformat()
                        
                        # Ensure meta is JSON string
                        meta_str = json.dumps(activity.get('meta', {}))
                        
                        date_str = self._normalize_activity_datetime(activity.get('date'))
                        
                        logger.debug(f"Inserting activity {i+1}/{len(activities)}: {activity['name']} ({activity['duration']}s)")
                        
                        await db.execute("""
                            INSERT INTO immersion_activities 
                            (user_id, guild_id, name, primary_type, media_type, duration, date, imported_at, meta)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            activity['user_id'],
                            activity.get('guild_id'),
                            activity['name'],
                            activity['primary_type'],
                            activity.get('media_type'),
                            activity['duration'],
                            date_str,
                            activity['imported_at'],
                            meta_str
                        ))
                        inserted_count += 1
                        
                        # Log progress every 50 activities
                        if (i + 1) % 50 == 0:
                            logger.info(f"Imported {i + 1}/{len(activities)} activities...")
                            
                    except ValueError as e:
                        logger.error(f"Validation error inserting activity {i+1} ({activity.get('name', 'Unknown')}): {e}")
                        raise
                    except Exception as e:
                        logger.error(f"Error inserting activity {i+1} ({activity.get('name', 'Unknown')}): {e}")
                        continue
                
                await db.commit()
                logger.info(f"Successfully imported {inserted_count} activities to database")
                return inserted_count
                
        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Error importing activities: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return 0
    
    async def undo_import_by_timestamp(self, user_id: str, import_timestamp: datetime) -> Dict[str, Any]:
        """Undo import by removing all activities and import record with specific import timestamp"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # First get statistics before deletion
                cursor = await db.execute("""
                    SELECT 
                        COUNT(*) as count,
                        COALESCE(SUM(duration), 0) as total_duration,
                        COALESCE(SUM(JSON_EXTRACT(meta, '$.characters')), 0) as total_characters
                    FROM immersion_activities 
                    WHERE user_id = ? AND imported_at = ?
                """, (user_id, import_timestamp.isoformat()))
                
                stats_row = await cursor.fetchone()
                if not stats_row or stats_row[0] == 0:
                    return {"count": 0, "total_duration": 0, "total_characters": 0}
                
                count, total_duration, total_characters = stats_row
                
                # Remove activities
                await db.execute("""
                    DELETE FROM immersion_activities 
                    WHERE user_id = ? AND imported_at = ?
                """, (user_id, import_timestamp.isoformat()))
                
                # Remove import record from tracking table
                await db.execute("""
                    DELETE FROM immersion_imports 
                    WHERE user_id = ? AND import_timestamp = ?
                """, (user_id, import_timestamp.isoformat()))
                
                await db.commit()
                logger.info(f"Removed {count} activities (duration: {total_duration}s, characters: {total_characters}) for timestamp {import_timestamp}")
                
                return {
                    "count": count,
                    "total_duration": int(total_duration or 0),
                    "total_characters": int(total_characters or 0)
                }
                
        except Exception as e:
            logger.error(f"Error undoing import: {e}")
            return 0
    
    # ==================== Blacklist Management ====================
    
    async def add_user_to_blacklist(self, user_id: str, blacklisted_by: str, reason: Optional[str] = None) -> bool:
        """
        Add a user to the blacklist.
        
        Args:
            user_id: Discord user ID to blacklist
            blacklisted_by: Discord user ID of the admin who added them
            reason: Optional reason for blacklisting
            
        Returns:
            True if successful, False otherwise
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO user_blacklist (user_id, reason, blacklisted_by, blacklisted_at)
                    VALUES (?, ?, ?, ?)
                """, (user_id, reason, blacklisted_by, datetime.now(timezone.utc).isoformat()))
                await db.commit()
                logger.info(f"User {user_id} added to blacklist by {blacklisted_by}")
                return True
        except Exception as e:
            logger.error(f"Error adding user to blacklist: {e}")
            return False
    
    async def remove_user_from_blacklist(self, user_id: str) -> bool:
        """
        Remove a user from the blacklist.
        
        Args:
            user_id: Discord user ID to remove from blacklist
            
        Returns:
            True if user was removed, False if they weren't blacklisted or error occurred
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    DELETE FROM user_blacklist WHERE user_id = ?
                """, (user_id,))
                await db.commit()
                
                if cursor.rowcount > 0:
                    logger.info(f"User {user_id} removed from blacklist")
                    return True
                return False
        except Exception as e:
            logger.error(f"Error removing user from blacklist: {e}")
            return False
    
    async def is_user_blacklisted(self, user_id: str) -> bool:
        """
        Check if a user is blacklisted.
        
        Args:
            user_id: Discord user ID to check
            
        Returns:
            True if user is blacklisted, False otherwise
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT 1 FROM user_blacklist WHERE user_id = ?
                """, (user_id,)) as cursor:
                    result = await cursor.fetchone()
                    return result is not None
        except Exception as e:
            logger.error(f"Error checking blacklist status: {e}")
            return False
    
    async def get_blacklist_entry(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Get blacklist entry details for a user.
        
        Args:
            user_id: Discord user ID to look up
            
        Returns:
            Dictionary with blacklist details or None if not blacklisted
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT user_id, reason, blacklisted_by, blacklisted_at
                    FROM user_blacklist
                    WHERE user_id = ?
                """, (user_id,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return {
                            "user_id": row[0],
                            "reason": row[1],
                            "blacklisted_by": row[2],
                            "blacklisted_at": row[3]
                        }
                    return None
        except Exception as e:
            logger.error(f"Error getting blacklist entry: {e}")
            return None
    
    async def get_all_blacklisted_users(self) -> List[Dict[str, Any]]:
        """
        Get all blacklisted users.
        
        Returns:
            List of dictionaries containing blacklist entries
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT user_id, reason, blacklisted_by, blacklisted_at
                    FROM user_blacklist
                    ORDER BY blacklisted_at DESC
                """) as cursor:
                    rows = await cursor.fetchall()
                    return [
                        {
                            "user_id": row[0],
                            "reason": row[1],
                            "blacklisted_by": row[2],
                            "blacklisted_at": row[3]
                        }
                        for row in rows
                    ]
        except Exception as e:
            logger.error(f"Error getting blacklisted users: {e}")
            return []

    async def get_blacklisted_user_ids(self) -> Set[str]:
        """Return a set of all blacklisted user IDs."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT user_id FROM user_blacklist") as cursor:
                    rows = await cursor.fetchall()
                    return {row[0] for row in rows if row and row[0]}
        except Exception as e:
            logger.error(f"Error getting blacklist id set: {e}")
            return set()

    async def add_kizuna_link(self, kizuna_username: str, discord_user_id: str) -> bool:
        """Link a kizuna username to a discord user. Returns False if the
        kizuna_username is already linked (UNIQUE violation) or on error."""
        normalized = (kizuna_username or "").strip().casefold()
        if not normalized:
            return False
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO kizuna_links (kizuna_username, discord_user_id) VALUES (?, ?)",
                    (normalized, str(discord_user_id)),
                )
                await db.commit()
                return True
        except aiosqlite.IntegrityError:
            return False
        except Exception as e:
            logger.error(f"Error adding kizuna link: {e}")
            return False

    async def remove_kizuna_link(self, kizuna_username: str, discord_user_id: str) -> bool:
        """Remove a kizuna link, but only if it currently belongs to the given
        discord user (prevents one user from clearing another's link)."""
        normalized = (kizuna_username or "").strip().casefold()
        if not normalized:
            return False
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "DELETE FROM kizuna_links WHERE kizuna_username = ? AND discord_user_id = ?",
                    (normalized, str(discord_user_id)),
                )
                await db.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error removing kizuna link: {e}")
            return False

    async def get_discord_user_by_kizuna(self, kizuna_username: str) -> Optional[str]:
        """Return the discord user id linked to the given kizuna username, or None."""
        normalized = (kizuna_username or "").strip().casefold()
        if not normalized:
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT discord_user_id FROM kizuna_links WHERE kizuna_username = ?",
                    (normalized,),
                ) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else None
        except Exception as e:
            logger.error(f"Error fetching kizuna link: {e}")
            return None

    async def list_kizuna_links_for_user(self, discord_user_id: str) -> List[str]:
        """Return all kizuna usernames linked to the given discord user."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT kizuna_username FROM kizuna_links WHERE discord_user_id = ? ORDER BY kizuna_username",
                    (str(discord_user_id),),
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [row[0] for row in rows if row and row[0]]
        except Exception as e:
            logger.error(f"Error listing kizuna links: {e}")
            return []

    async def get_guild_member_ids(self, guild_id: str) -> Set[str]:
        """
        Get all user IDs who are members of a guild from the immersion_guild_members table.
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            Set of user IDs who are tracked as members of this guild
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT user_id FROM immersion_guild_members WHERE guild_id = ?",
                    (guild_id,)
                ) as cursor:
                    rows = await cursor.fetchall()
                    return {row[0] for row in rows if row and row[0]}
        except Exception as e:
            logger.error(f"Error getting guild member IDs: {e}")
            return set()
    
    async def sync_guild_members(self, guild_id: str, member_ids: set) -> None:
        """
        Sync guild member list. Adds new members and removes those who left.
        
        Args:
            guild_id: Discord guild ID
            member_ids: Set of current member user IDs
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Get current members in database
                async with db.execute(
                    "SELECT user_id FROM immersion_guild_members WHERE guild_id = ?",
                    (guild_id,)
                ) as cursor:
                    rows = await cursor.fetchall()
                    db_members = {row[0] for row in rows if row and row[0]}
                
                # Add new members
                new_members = member_ids - db_members
                if new_members:
                    await db.executemany(
                        "INSERT OR REPLACE INTO immersion_guild_members (guild_id, user_id, last_seen_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                        [(guild_id, user_id) for user_id in new_members]
                    )
                
                # Remove members who left
                removed_members = db_members - member_ids
                if removed_members:
                    await db.executemany(
                        "DELETE FROM immersion_guild_members WHERE guild_id = ? AND user_id = ?",
                        [(guild_id, user_id) for user_id in removed_members]
                    )
                
                await db.commit()
                
                if new_members or removed_members:
                    logger.info(f"Guild {guild_id} member sync: +{len(new_members)} -{len(removed_members)}")
                    
        except Exception as e:
            logger.error(f"Error syncing guild members: {e}")

    async def sync_guild_info(self, guild_id: str, guild_name: str, guild_icon_url: str = None) -> None:
        """
        Sync guild information to database for cross-bot lookup.
        
        Args:
            guild_id: Discord guild ID
            guild_name: Guild name
            guild_icon_url: URL to guild icon (optional)
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO immersion_guilds (guild_id, guild_name, guild_icon_url)
                    VALUES (?, ?, ?)
                """, (guild_id, guild_name, guild_icon_url))
                await db.commit()
                logger.debug(f"Synced guild info for {guild_name} ({guild_id})")
        except Exception as e:
            logger.error(f"Error syncing guild info: {e}")

    async def get_guild_info(self, guild_id: str) -> dict:
        """
        Get cached guild information from database.
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            Dict with guild_name and guild_icon_url, or None if not found
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT guild_name, guild_icon_url FROM immersion_guilds WHERE guild_id = ?",
                    (guild_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return {
                            "guild_name": row["guild_name"],
                            "guild_icon_url": row["guild_icon_url"]
                        }
                    return None
        except Exception as e:
            logger.error(f"Error getting guild info: {e}")
            return None
            logger.error(f"Error syncing guild members: {e}")



    # ------------------------------------------------------------------
    # Onboarding: applications and per-guild settings
    # ------------------------------------------------------------------

    async def _ensure_onboarding_tables(self):
        """Ensure the onboarding application and settings tables exist"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS verification_applications (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        thread_id INTEGER,
                        review_message_id INTEGER,
                        reading_answer TEXT,
                        profile_answer TEXT,
                        status TEXT NOT NULL DEFAULT 'awaiting_quiz',
                        quiz_level INTEGER,
                        quiz_passed_at TIMESTAMP,
                        decided_by INTEGER,
                        decided_at TIMESTAMP,
                        decision_note TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("""
                    CREATE INDEX IF NOT EXISTS idx_verification_applications_lookup
                    ON verification_applications (guild_id, user_id, status)
                """)
                await db.execute("""
                    CREATE INDEX IF NOT EXISTS idx_verification_applications_thread
                    ON verification_applications (thread_id)
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS onboarding_settings (
                        guild_id INTEGER PRIMARY KEY,
                        enabled INTEGER NOT NULL DEFAULT 0,
                        entry_channel_id INTEGER,
                        entry_message_id INTEGER,
                        review_channel_id INTEGER,
                        moderator_role_id INTEGER,
                        quiz_channel_id INTEGER,
                        auto_approve_join_requests INTEGER NOT NULL DEFAULT 0,
                        reapply_cooldown_days INTEGER NOT NULL DEFAULT 0,
                        approver_ids TEXT NOT NULL DEFAULT '',
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # Added after the table shipped, so an existing install needs it back-filled.
                async with db.execute(
                    "PRAGMA table_info(onboarding_settings)"
                ) as cursor:
                    columns = {row[1] for row in await cursor.fetchall()}
                if "moderator_role_id" not in columns:
                    await db.execute(
                        "ALTER TABLE onboarding_settings ADD COLUMN moderator_role_id INTEGER"
                    )
                if "quiz_channel_id" not in columns:
                    await db.execute(
                        "ALTER TABLE onboarding_settings ADD COLUMN quiz_channel_id INTEGER"
                    )
                if "auto_approve_join_requests" not in columns:
                    await db.execute(
                        "ALTER TABLE onboarding_settings ADD COLUMN "
                        "auto_approve_join_requests INTEGER NOT NULL DEFAULT 0"
                    )
                if "approver_ids" not in columns:
                    # Empty on an existing install, which admits nobody until the list is
                    # filled in. Widening on upgrade would hand out the decision to
                    # whoever happened to satisfy the old permission check.
                    await db.execute(
                        "ALTER TABLE onboarding_settings ADD COLUMN "
                        "approver_ids TEXT NOT NULL DEFAULT ''"
                    )
                await db.commit()
        except Exception as e:
            logger.error(f"Error ensuring onboarding tables: {e}")

    async def get_onboarding_settings(self, guild_id: int) -> Dict[str, Any]:
        """Settings for a guild, with defaults for one that has never been configured"""
        defaults = {
            "guild_id": guild_id,
            "enabled": 0,
            "entry_channel_id": None,
            "entry_message_id": None,
            "review_channel_id": None,
            "moderator_role_id": None,
            "quiz_channel_id": None,
            "auto_approve_join_requests": 0,
            "reapply_cooldown_days": 0,
            "approver_ids": "",
        }
        try:
            await self._ensure_onboarding_tables()
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM onboarding_settings WHERE guild_id = ?", (guild_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else defaults
        except Exception as e:
            logger.error(f"Error getting onboarding settings: {e}")
            return defaults

    async def update_onboarding_settings(self, guild_id: int, **fields) -> bool:
        """Write only the named settings columns, leaving the rest untouched"""
        allowed = {
            "enabled", "entry_channel_id", "entry_message_id",
            "review_channel_id", "moderator_role_id", "quiz_channel_id",
            "reapply_cooldown_days", "auto_approve_join_requests",
            "approver_ids",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        try:
            await self._ensure_onboarding_tables()
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO onboarding_settings (guild_id) VALUES (?)",
                    (guild_id,)
                )
                assignments = ", ".join(f"{key} = ?" for key in updates)
                await db.execute(
                    f"UPDATE onboarding_settings SET {assignments}, "
                    "updated_at = CURRENT_TIMESTAMP WHERE guild_id = ?",
                    (*updates.values(), guild_id)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error updating onboarding settings: {e}")
            return False

    async def create_verification_application(
        self, guild_id: int, user_id: int, reading_answer: str,
        profile_answer: Optional[str]
    ) -> Optional[int]:
        """Open an application and return its id"""
        try:
            await self._ensure_onboarding_tables()
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    INSERT INTO verification_applications
                        (guild_id, user_id, reading_answer, profile_answer, status)
                    VALUES (?, ?, ?, ?, 'awaiting_quiz')
                """, (guild_id, user_id, reading_answer, profile_answer))
                await db.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Error creating verification application: {e}")
            return None

    async def get_open_application(self, guild_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        """The application still moving through the flow, if there is one"""
        try:
            await self._ensure_onboarding_tables()
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT * FROM verification_applications
                    WHERE guild_id = ? AND user_id = ?
                      AND status IN ('awaiting_quiz', 'awaiting_review')
                    ORDER BY id DESC LIMIT 1
                """, (guild_id, user_id)) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting open application: {e}")
            return None

    async def get_latest_application(self, guild_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        """Most recent application of any status, which is what a cooldown is measured from"""
        try:
            await self._ensure_onboarding_tables()
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT * FROM verification_applications
                    WHERE guild_id = ? AND user_id = ?
                    ORDER BY id DESC LIMIT 1
                """, (guild_id, user_id)) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting latest application: {e}")
            return None

    async def get_application_by_id(self, app_id: int) -> Optional[Dict[str, Any]]:
        """Fetch one application by its primary key"""
        try:
            await self._ensure_onboarding_tables()
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM verification_applications WHERE id = ?", (app_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting application by id: {e}")
            return None

    async def update_verification_application(self, app_id: int, **fields) -> bool:
        """Write only the named application columns"""
        allowed = {
            "thread_id", "review_message_id", "status", "quiz_level",
            "quiz_passed_at", "decided_by", "decided_at", "decision_note",
            "reading_answer", "profile_answer",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        try:
            await self._ensure_onboarding_tables()
            async with aiosqlite.connect(self.db_path) as db:
                assignments = ", ".join(f"{key} = ?" for key in updates)
                await db.execute(
                    f"UPDATE verification_applications SET {assignments} WHERE id = ?",
                    (*updates.values(), app_id)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error updating verification application: {e}")
            return False

    async def claim_application_for_review(
        self, app_id: int, quiz_level: int, passed_at: str
    ) -> bool:
        """Move an application into review, and report whether this caller was the one to
        do it. The status test lives in the UPDATE rather than in a preceding SELECT because
        one quiz pass reaches the bot as two separate events, and both would otherwise pass
        a check-then-write and post a review card each."""
        try:
            await self._ensure_onboarding_tables()
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    UPDATE verification_applications
                    SET status = 'awaiting_review', quiz_level = ?, quiz_passed_at = ?
                    WHERE id = ? AND status = 'awaiting_quiz'
                """, (quiz_level, passed_at, app_id))
                await db.commit()
                return cursor.rowcount == 1
        except Exception as e:
            logger.error(f"Error claiming application for review: {e}")
            return False

    OPEN_STATUSES = ("awaiting_quiz", "awaiting_review")

    async def claim_application_decision(
        self, app_id: int, status: str, decided_by: int, decided_at: str,
        note: Optional[str] = None, from_statuses: tuple = ("awaiting_review",),
    ) -> bool:
        """Record a decision, and report whether this caller was the one to make it.

        The status test lives in the UPDATE rather than in a preceding SELECT because a
        decision can arrive from a Discord button or from the web console, and a
        check-then-write lets both pass and deliver two outcomes to the applicant.
        """
        try:
            await self._ensure_onboarding_tables()
            async with aiosqlite.connect(self.db_path) as db:
                placeholders = ", ".join("?" for _ in from_statuses)
                cursor = await db.execute(f"""
                    UPDATE verification_applications
                    SET status = ?, decided_by = ?, decided_at = ?, decision_note = ?
                    WHERE id = ? AND status IN ({placeholders})
                """, (status, decided_by, decided_at, note, app_id, *from_statuses))
                await db.commit()
                return cursor.rowcount == 1
        except Exception as e:
            logger.error(f"Error claiming application decision: {e}")
            return False

    async def get_applications_by_status(
        self, guild_id: int, status: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Applications in one state, oldest first so a queue reads as a queue"""
        try:
            await self._ensure_onboarding_tables()
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT * FROM verification_applications
                    WHERE guild_id = ? AND status = ?
                    ORDER BY id ASC LIMIT ?
                """, (guild_id, status, limit)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting applications by status: {e}")
            return []

    async def count_applications_without_card(self, guild_id: int) -> int:
        """Applications in review whose card never posted.

        Their applicant has already been told the moderators have it, so a non-zero
        result is somebody waiting on a decision nobody can see to make.
        """
        try:
            await self._ensure_onboarding_tables()
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT COUNT(*) FROM verification_applications
                    WHERE guild_id = ? AND status = 'awaiting_review'
                      AND review_message_id IS NULL
                """, (guild_id,)) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else 0
        except Exception as e:
            logger.error(f"Error counting applications without a card: {e}")
            return 0

    async def get_onboarding_funnel_counts(self, guild_id: int) -> Dict[str, int]:
        """Counts per status, which is the funnel the native join form cannot report"""
        counts = {
            "awaiting_quiz": 0, "awaiting_review": 0,
            "approved": 0, "rejected": 0, "left": 0,
        }
        try:
            await self._ensure_onboarding_tables()
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT status, COUNT(*) FROM verification_applications
                    WHERE guild_id = ? GROUP BY status
                """, (guild_id,)) as cursor:
                    for status, total in await cursor.fetchall():
                        counts[status] = total
            return counts
        except Exception as e:
            logger.error(f"Error getting onboarding funnel counts: {e}")
            return counts
