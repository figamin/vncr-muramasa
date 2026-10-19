"""
Activity repository for immersion tracking
Handles database operations for activities, goals, and user data
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Tuple
from utils.immersion_models import Activity, Goal, UserActivityPage, LeaderboardEntry
from utils.database import Database
from utils.immersion_media import is_image_explicit

logger = logging.getLogger(__name__)

def _clean_activity_name(name: str) -> str:
    """Clean up activity names that may have autocomplete display format embedded.
    
    Format examples to clean:
    - "Title — YYYY-MM-DD • rating/10" -> "Title"
    - "Title — Type • Status • Score%" -> "Title"
    """
    if not name:
        return name
    
    # Check if this looks like an autocomplete display value
    if " — " in name and ("•" in name or "/" in name):
        # Extract the title part before the " — " separator
        title_part = name.split(" — ")[0].strip()
        if title_part:
            return title_part
    
    return name

class ActivityRepository:
    """Repository for managing immersion activities"""
    
    def __init__(self, database: Database):
        self.db = database
        
        # Simple TTL cache for expensive queries (15 minute TTL for better performance)
        self._stats_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
        self._library_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
        self._cache_ttl = 900  # 15 minutes in seconds
    
    def _get_cache_entry(self, cache_dict: Dict[str, Tuple[Dict[str, Any], float]], key: str) -> Optional[Dict[str, Any]]:
        """Get cached entry if not expired"""
        if key in cache_dict:
            result, timestamp = cache_dict[key]
            if time.time() - timestamp < self._cache_ttl:
                logger.debug(f"Cache HIT for key: {key}")
                return result
            else:
                # Expired, remove it
                del cache_dict[key]
                logger.debug(f"Cache EXPIRED for key: {key}")
        logger.debug(f"Cache MISS for key: {key}")
        return None
    
    def _set_cache_entry(self, cache_dict: Dict[str, Tuple[Dict[str, Any], float]], key: str, value: Dict[str, Any]):
        """Store value in cache with current timestamp"""
        cache_dict[key] = (value, time.time())
        logger.debug(f"Cache SET for key: {key}")
    
    def invalidate_user_cache(self, user_id: int, guild_id: int):
        """Invalidate all cache entries for a user in a guild"""
        # Remove stats cache entries
        stats_keys = [k for k in self._stats_cache.keys() if k.startswith(f"{user_id}_{guild_id}_")]
        for key in stats_keys:
            del self._stats_cache[key]
        
        # Remove library cache entries
        library_keys = [k for k in self._library_cache.keys() if k.startswith(f"{user_id}_{guild_id}_")]
        for key in library_keys:
            del self._library_cache[key]
        
        if stats_keys or library_keys:
            logger.info(f"Invalidated {len(stats_keys)} stats + {len(library_keys)} library cache entries for user {user_id} in guild {guild_id}")
    
    async def create_activity(self, activity: Activity) -> Optional[int]:
        """Create a new activity and return its ID"""
        try:
            activity_id = await self.db.create_immersion_activity(
                user_id=activity.user_id,
                guild_id=activity.guild_id,
                name=activity.name,
                primary_type=activity.primary_type,
                duration=activity.duration,
                media_type=activity.media_type,
                date=activity.date.isoformat() if activity.date else None,
                meta=activity.meta,
                comment=activity.comment
            )
            
            if activity_id:
                activity.id = activity_id
                logger.info(f"Created activity {activity_id} for user {activity.user_id}")
                
                # Invalidate cache for this user since their data changed
                self.invalidate_user_cache(activity.user_id, activity.guild_id)
            
            return activity_id
            
        except ValueError as e:
            logger.warning(f"Validation error creating activity for user {activity.user_id}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error creating activity: {e}")
            return None
    
    async def get_activity_by_id(self, activity_id: int) -> Optional[Activity]:
        """Get a specific activity by ID"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM immersion_activities WHERE id = ? AND deleted_at IS NULL",
                    (activity_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    
                    if row:
                        data = dict(row)
                        # Parse the date string
                        if data['date']:
                            data['date'] = datetime.fromisoformat(data['date'])
                        if data.get('created_at'):
                            data['created_at'] = datetime.fromisoformat(data['created_at'])
                        
                        # Parse meta JSON
                        if isinstance(data.get('meta'), str):
                            data['meta'] = json.loads(data['meta'])
                        
                        return Activity.from_dict(data)
                    
                    return None
                    
        except Exception as e:
            logger.error(f"Error getting activity by ID: {e}")
            return None

    async def get_user_primary_guild(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Determine the user's primary guild, considering overrides and activity totals."""
        try:
            settings = await self.db.get_user_settings(user_id)
            override_guild_id = settings.get('primary_guild_id') if settings else None

            if override_guild_id:
                guild_info = await self.db.get_guild_info(override_guild_id)
                return {
                    'guild_id': override_guild_id,
                    'guild_name': guild_info.get('guild_name') if guild_info else None,
                    'source': 'override'
                }

            import aiosqlite

            async with aiosqlite.connect(self.db.db_path) as db:
                db.row_factory = aiosqlite.Row

                query = """
                    WITH single_guild_members AS (
                        SELECT guild_id, user_id
                        FROM immersion_guild_members
                        GROUP BY user_id
                        HAVING COUNT(*) = 1
                    ), activity_base AS (
                        SELECT
                            COALESCE(ia.guild_id, sgm.guild_id) AS resolved_guild_id,
                            ia.duration
                        FROM immersion_activities AS ia
                        LEFT JOIN single_guild_members AS sgm ON sgm.user_id = ia.user_id
                        WHERE ia.user_id = ? AND ia.deleted_at IS NULL
                    )
                    SELECT
                        resolved_guild_id AS guild_id,
                        SUM(duration) AS total_duration
                    FROM activity_base
                    WHERE resolved_guild_id IS NOT NULL
                    GROUP BY resolved_guild_id
                    ORDER BY total_duration DESC
                    LIMIT 1
                """

                async with db.execute(query, (user_id,)) as cursor:
                    row = await cursor.fetchone()

            if row and row['guild_id']:
                guild_id = row['guild_id']
                guild_info = await self.db.get_guild_info(guild_id)
                return {
                    'guild_id': guild_id,
                    'guild_name': guild_info.get('guild_name') if guild_info else None,
                    'source': 'detected',
                    'total_duration': row['total_duration']
                }

        except Exception as e:
            logger.error(f"Error determining primary guild for user {user_id}: {e}")

        return None
    
    async def get_user_activities(self, user_id: str, guild_id: str = None, 
                                limit: int = 50, offset: int = 0,
                                primary_type: str = None, media_type: str = None) -> List[Activity]:
        """Get user's activities with pagination"""
        try:
            activities_data = await self.db.get_user_activities(
                user_id=user_id, 
                guild_id=guild_id, 
                limit=limit, 
                offset=offset,
                primary_type=primary_type,
                media_type=media_type
            )
            
            activities = []
            for data in activities_data:
                try:
                    activity = Activity.from_dict(data)
                    activities.append(activity)
                except Exception as e:
                    logger.warning(f"Error parsing activity data: {e}")
                    continue
            
            return activities
            
        except Exception as e:
            logger.error(f"Error getting user activities: {e}")
            return []
    
    async def sum_activity_duration(
        self,
        user_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        primary_type: Optional[str] = None,
        media_type: Optional[str] = None,
        youtube_channels: Optional[List[str]] = None
    ) -> int:
        """Sum activity duration for a user within optional filters."""
        try:
            import aiosqlite

            query = [
                "SELECT COALESCE(SUM(duration), 0) as total_duration",
                "FROM immersion_activities",
                "WHERE user_id = ?",
                "AND deleted_at IS NULL"
            ]
            params: List[Any] = [user_id]

            if primary_type:
                query.append("AND primary_type = ?")
                params.append(primary_type)

            if media_type:
                query.append("AND media_type = ?")
                params.append(media_type)

            if start_date:
                query.append("AND date >= ?")
                params.append(start_date.isoformat())

            if end_date:
                query.append("AND date <= ?")
                params.append(end_date.isoformat())

            if youtube_channels:
                placeholders = ','.join('?' * len(youtube_channels))
                query.append(f"AND json_extract(meta, '$.channel_id') IN ({placeholders})")
                params.extend(youtube_channels)

            sql = "\n".join(query)

            async with aiosqlite.connect(self.db.db_path) as db:
                async with db.execute(sql, params) as cursor:
                    row = await cursor.fetchone()
                    return int(row[0]) if row and row[0] else 0

        except Exception as e:
            logger.error(f"Error summing activity duration: {e}")
            return 0

    async def get_title_stats(
        self,
        user_id: str,
        title_name: str,
        media_type: str = None,
        vndb_id: str = None
    ) -> Optional[Dict[str, Any]]:
        """Get aggregate stats for a specific title.

        Args:
            user_id: User ID to query
            title_name: The title/name to match
            media_type: Optional media type filter (e.g., 'visual_novel', 'anime')
            vndb_id: Optional VNDB ID for VN deduplication

        Returns:
            Dict with: total_duration, total_count, total_characters,
                       total_pages, total_episodes, first_logged, last_logged
            Returns None if no activities found.
        """
        try:
            import aiosqlite

            # Clean the title name (strip autocomplete format)
            cleaned_name = _clean_activity_name(title_name)

            # Build the query based on whether a VNDB ID is available
            if vndb_id and media_type == 'visual_novel':
                # For VNs with VNDB ID, match by ID OR by cleaned name
                # so every entry for the same title counts,
                # including entries logged without a VNDB ID
                query = """
                    SELECT
                        COUNT(*) as total_count,
                        COALESCE(SUM(duration), 0) as total_duration,
                        MIN(date) as first_logged,
                        MAX(date) as last_logged,
                        COALESCE(SUM(CAST(json_extract(meta, '$.characters') AS INTEGER)), 0) as total_characters,
                        COALESCE(SUM(CAST(json_extract(meta, '$.pages') AS REAL)), 0) as total_pages,
                        COALESCE(SUM(CAST(json_extract(meta, '$.episodes') AS INTEGER)), 0) as total_episodes
                    FROM immersion_activities
                    WHERE user_id = ?
                    AND deleted_at IS NULL
                    AND media_type = 'visual_novel'
                    AND (
                        json_extract(meta, '$.vndb_id') = ?
                        OR json_extract(meta, '$.vn.id') = ?
                        OR CASE
                            WHEN name LIKE '% — %' THEN TRIM(SUBSTR(name, 1, INSTR(name, ' — ') - 1))
                            ELSE name
                        END = ?
                    )
                """
                params = [user_id, vndb_id, vndb_id, cleaned_name]
            else:
                # For other media types or VNs without ID, match by cleaned name
                query = """
                    SELECT
                        COUNT(*) as total_count,
                        COALESCE(SUM(duration), 0) as total_duration,
                        MIN(date) as first_logged,
                        MAX(date) as last_logged,
                        COALESCE(SUM(CAST(json_extract(meta, '$.characters') AS INTEGER)), 0) as total_characters,
                        COALESCE(SUM(CAST(json_extract(meta, '$.pages') AS REAL)), 0) as total_pages,
                        COALESCE(SUM(CAST(json_extract(meta, '$.episodes') AS INTEGER)), 0) as total_episodes
                    FROM immersion_activities
                    WHERE user_id = ?
                    AND deleted_at IS NULL
                    AND (
                        name = ?
                        OR name LIKE ? || ' — %'
                        OR CASE
                            WHEN name LIKE '% — %' AND (name LIKE '%•%' OR name LIKE '%/%') THEN
                                TRIM(SUBSTR(name, 1, INSTR(name, ' — ') - 1))
                            ELSE name
                        END = ?
                    )
                """
                params = [user_id, cleaned_name, cleaned_name, cleaned_name]

                if media_type:
                    query += " AND media_type = ?"
                    params.append(media_type)

            async with aiosqlite.connect(self.db.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(query, params) as cursor:
                    row = await cursor.fetchone()

                    if not row or row['total_count'] == 0:
                        return None

                    return {
                        'total_count': row['total_count'],
                        'total_duration': row['total_duration'],
                        'first_logged': row['first_logged'],
                        'last_logged': row['last_logged'],
                        'total_characters': row['total_characters'] or 0,
                        'total_pages': row['total_pages'] or 0,
                        'total_episodes': row['total_episodes'] or 0
                    }

        except Exception as e:
            logger.error(f"Error getting title stats for '{title_name}': {e}")
            return None

    async def get_user_activity_summary(self, user_id: str) -> Dict[str, Any]:
        """Return lifetime immersion aggregates for a user."""
        try:
            import aiosqlite

            async with aiosqlite.connect(self.db.db_path) as db:
                db.row_factory = aiosqlite.Row

                async with db.execute(
                    """
                    SELECT
                        COUNT(*) AS total_logs,
                        COALESCE(SUM(duration), 0) AS total_duration,
                        COALESCE(SUM(CASE WHEN primary_type = 'reading' THEN duration ELSE 0 END), 0)
                            AS reading_duration,
                        COALESCE(SUM(CASE WHEN primary_type = 'listening' THEN duration ELSE 0 END), 0)
                            AS listening_duration,
                        COALESCE(AVG(CASE WHEN duration > 0 THEN duration END), 0) AS avg_duration,
                        COALESCE(SUM(COALESCE(CAST(json_extract(meta, '$.characters') AS INTEGER), 0)), 0) AS total_characters,
                        MIN(date) AS first_log,
                        MAX(date) AS last_log
                    FROM immersion_activities
                    WHERE user_id = ? AND deleted_at IS NULL
                    """,
                    (user_id,)
                ) as cursor:
                    summary_row = await cursor.fetchone()

                async with db.execute(
                    """
                    SELECT media_type,
                           COUNT(*) AS count,
                           COALESCE(SUM(duration), 0) AS duration
                    FROM immersion_activities
                    WHERE user_id = ? AND deleted_at IS NULL AND media_type IS NOT NULL
                    GROUP BY media_type
                    ORDER BY duration DESC
                    """,
                    (user_id,)
                ) as cursor:
                    media_rows = await cursor.fetchall()

            summary_row = dict(summary_row) if summary_row else {}
            media_breakdown = [
                {
                    "media_type": row["media_type"],
                    "count": row["count"],
                    "duration": row["duration"],
                }
                for row in media_rows
            ]

            def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
                if not value:
                    return None
                try:
                    return datetime.fromisoformat(value)
                except (TypeError, ValueError):
                    return None

            return {
                "total_logs": summary_row.get("total_logs", 0),
                "total_duration": summary_row.get("total_duration", 0) or 0,
                "reading_duration": summary_row.get("reading_duration", 0) or 0,
                "listening_duration": summary_row.get("listening_duration", 0) or 0,
                "avg_duration": summary_row.get("avg_duration", 0) or 0,
                "total_characters": summary_row.get("total_characters", 0) or 0,
                "first_log": _parse_timestamp(summary_row.get("first_log")),
                "last_log": _parse_timestamp(summary_row.get("last_log")),
                "media_breakdown": media_breakdown,
            }

        except Exception as e:
            logger.error(f"Error summarizing user activity: {e}")
            return {
                "total_logs": 0,
                "total_duration": 0,
                "reading_duration": 0,
                "listening_duration": 0,
                "avg_duration": 0,
                "total_characters": 0,
                "first_log": None,
                "last_log": None,
                "media_breakdown": [],
            }

    async def page_by_user_id(self, user_id: str, guild_id: str = None, 
                             page_size: int = 6, offset: int = 0,
                             primary_type: str = None, media_type: str = None) -> UserActivityPage:
        """Get paginated activities for a user"""
        try:
            # Get total count
            total_count = await self.db.get_user_activity_count(
                user_id,
                guild_id,
                primary_type=primary_type,
                media_type=media_type
            )
            
            # Calculate page info
            page_count = (total_count + page_size - 1) // page_size if total_count > 0 else 1
            current_page = (offset // page_size) + 1
            
            # Get activities for this page
            activities = await self.get_user_activities(
                user_id=user_id,
                guild_id=guild_id,
                limit=page_size,
                offset=offset,
                primary_type=primary_type,
                media_type=media_type
            )
            
            return UserActivityPage(
                activities=activities,
                page=current_page,
                page_count=page_count
            )
            
        except Exception as e:
            logger.error(f"Error getting user activity page: {e}")
            return UserActivityPage(activities=[], page=1, page_count=1)
    
    async def get_top_members(
        self,
        guild_id: str = None,
        limit: int = 10,
        start_date: datetime = None,
        end_date: datetime = None,
        media_type: str = None,
        activity_type: str = None,
        metric: str = "duration"
    ) -> List[LeaderboardEntry]:
        """Get top members ordered by the requested aggregate metric."""
        try:
            import aiosqlite
            
            # Build filters
            filters = []
            params = []
            metric_key = (metric or "duration").lower()
            if metric_key not in {"duration", "pages", "characters"}:
                metric_key = "duration"
            metric_expr_map = {
                "duration": "SUM(duration)",
                "pages": "SUM(COALESCE(CAST(json_extract(meta, '$.pages') AS REAL), 0))",
                "characters": "SUM(COALESCE(CAST(json_extract(meta, '$.characters') AS REAL), 0))"
            }
            metric_expr = metric_expr_map[metric_key]
            
            # Only filter by guild if guild_id is provided
            if guild_id:
                filters.append("guild_id = ?")
                params.append(guild_id)
            
            if start_date and end_date:
                filters.append("date BETWEEN ? AND ?")
                params.extend([start_date.isoformat(), end_date.isoformat()])
            elif start_date:
                filters.append("date >= ?")
                params.append(start_date.isoformat())
            elif end_date:
                filters.append("date <= ?")
                params.append(end_date.isoformat())
                
            if media_type:
                filters.append("media_type = ?")
                params.append(media_type)

            if activity_type:
                filters.append("primary_type = ?")
                params.append(activity_type)
            
            # Exclude blacklisted users
            filters.append("user_id NOT IN (SELECT user_id FROM user_blacklist)")
            
            # Build WHERE clause
            where_clause = "WHERE deleted_at IS NULL"
            if filters:
                where_clause += " AND " + " AND ".join(filters)

            params.append(limit)

            async with aiosqlite.connect(self.db.db_path) as db:
                db.row_factory = aiosqlite.Row

                query = f"""
                    SELECT user_id, 
                           SUM(duration) AS total_duration,
                           SUM(COALESCE(CAST(json_extract(meta, '$.pages') AS REAL), 0)) AS total_pages,
                           SUM(COALESCE(CAST(json_extract(meta, '$.characters') AS REAL), 0)) AS total_characters,
                           COUNT(*) AS activity_count,
                           {metric_expr} AS metric_value
                    FROM immersion_activities 
                    {where_clause}
                    GROUP BY user_id 
                    HAVING metric_value > 0
                    ORDER BY metric_value DESC 
                    LIMIT ?
                """

                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()

            blacklisted_ids = await self.db.get_blacklisted_user_ids()

            return [
                LeaderboardEntry(
                    user_id=row['user_id'],
                    total_duration=row['total_duration'],
                    activity_count=row['activity_count'],
                    total_pages=row['total_pages'],
                    total_characters=row['total_characters']
                )
                for row in rows
                if row['user_id'] not in blacklisted_ids
            ]
                    
        except Exception as e:
            logger.error(f"Error getting top members: {e}")
            return []

    async def get_leaderboard_user_count(
        self,
        guild_id: str = None,
        start_date: datetime = None,
        end_date: datetime = None,
        media_type: str = None,
        activity_type: str = None,
        metric: str = "duration"
    ) -> int:
        """Get total count of users who would appear on the leaderboard."""
        try:
            import aiosqlite
            
            # Build filters
            filters = []
            params = []
            metric_key = (metric or "duration").lower()
            if metric_key not in {"duration", "pages", "characters"}:
                metric_key = "duration"
            metric_expr_map = {
                "duration": "SUM(duration)",
                "pages": "SUM(COALESCE(CAST(json_extract(meta, '$.pages') AS REAL), 0))",
                "characters": "SUM(COALESCE(CAST(json_extract(meta, '$.characters') AS REAL), 0))"
            }
            metric_expr = metric_expr_map[metric_key]
            
            # Only filter by guild if guild_id is provided
            if guild_id:
                filters.append("guild_id = ?")
                params.append(guild_id)
            
            if start_date and end_date:
                filters.append("date BETWEEN ? AND ?")
                params.extend([start_date.isoformat(), end_date.isoformat()])
            elif start_date:
                filters.append("date >= ?")
                params.append(start_date.isoformat())
            elif end_date:
                filters.append("date <= ?")
                params.append(end_date.isoformat())
                
            if media_type:
                filters.append("media_type = ?")
                params.append(media_type)

            if activity_type:
                filters.append("primary_type = ?")
                params.append(activity_type)
            
            # Exclude blacklisted users
            filters.append("user_id NOT IN (SELECT user_id FROM user_blacklist)")
            
            # Build WHERE clause
            where_clause = "WHERE deleted_at IS NULL"
            if filters:
                where_clause += " AND " + " AND ".join(filters)
            
            async with aiosqlite.connect(self.db.db_path) as db:
                query = f"""
                    SELECT user_id, {metric_expr} AS metric_value
                    FROM immersion_activities 
                    {where_clause}
                    GROUP BY user_id 
                    HAVING metric_value > 0
                """

                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()

            blacklisted_ids = await self.db.get_blacklisted_user_ids()
            return sum(1 for row in rows if row[0] not in blacklisted_ids)
                    
        except Exception as e:
            logger.error(f"Error getting leaderboard user count: {e}")
            return 0

    async def get_vn_durations_by_user(
        self,
        guild_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> Dict[str, int]:
        """Aggregate VN reading time (seconds) per user with optional filters.

        ⚠️ IMPORTANT: `guild_id` IS IGNORED ON PURPOSE.

        - VNCRbot is designed for a single consolidated server and combines data across
          all guilds/channels.
        - Lifetime leaderboards and special rank roles are computed from combined
          totals, so filtering by guild here would silently desync them.
        - Leave this behaviour as-is unless the overall deployment model changes.

        The parameter is kept only for backwards compatibility with legacy callers.
        """
        try:
            import aiosqlite

            clauses = ["media_type = 'visual_novel'", "deleted_at IS NULL"]
            params: List[Any] = []

            if start_date:
                clauses.append("date >= ?")
                params.append(start_date.isoformat())

            if end_date:
                clauses.append("date <= ?")
                params.append(end_date.isoformat())

            where_clause = " AND ".join(clauses)

            sql = [
                "SELECT user_id, SUM(duration) AS total_duration",
                "FROM immersion_activities",
                f"WHERE {where_clause}",
                "GROUP BY user_id",
                "HAVING total_duration > 0",
                "ORDER BY total_duration DESC"
            ]

            if limit and limit > 0:
                sql.append("LIMIT ?")
                params.append(limit)

            query = "\n".join(sql)

            results: Dict[str, int] = {}
            async with aiosqlite.connect(self.db.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(query, params) as cursor:
                    async for row in cursor:
                        user_id = row["user_id"]
                        total_duration = int(row["total_duration"] or 0)
                        results[user_id] = total_duration

            if results:
                blacklisted_ids = await self.db.get_blacklisted_user_ids()
                for blacklisted_id in blacklisted_ids:
                    results.pop(blacklisted_id, None)

            return results

        except Exception as e:
            logger.error(f"Error aggregating VN durations: {e}")
            return {}
    
    async def delete_activity(self, activity_id: int, user_id: str) -> bool:
        """Soft delete an activity (for undo functionality)"""
        try:
            # Get activity info before deletion to get guild_id for cache invalidation
            activity = await self.get_activity_by_id(activity_id)
            
            success = await self.db.delete_activity(activity_id, user_id)
            if success:
                logger.info(f"Deleted activity {activity_id} for user {user_id}")
                
                # Invalidate cache when guild_id is known
                if activity and activity.guild_id:
                    self.invalidate_user_cache(user_id, activity.guild_id)
            
            return success
            
        except Exception as e:
            logger.error(f"Error deleting activity: {e}")
            return False
    
    async def get_latest_activity(self, user_id: str, guild_id: str = None) -> Optional[Activity]:
        """Get user's most recent activity"""
        try:
            activities = await self.get_user_activities(
                user_id=user_id,
                guild_id=guild_id,
                limit=1,
                offset=0
            )
            
            return activities[0] if activities else None
            
        except Exception as e:
            logger.error(f"Error getting latest activity: {e}")
            return None
    
    async def get_total_watch_time_of_video_by_user_id(self, user_id: str, 
                                                      platform: str, video_id: str) -> int:
        """Get total time user has spent watching a specific video"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db.db_path) as db:
                async with db.execute("""
                    SELECT SUM(duration) as total_duration
                    FROM immersion_activities 
                    WHERE user_id = ? 
                    AND media_type = 'video' 
                    AND deleted_at IS NULL
                    AND json_extract(meta, '$.platform') = ?
                    AND json_extract(meta, '$.video_id') = ?
                """, (user_id, platform, video_id)) as cursor:
                    result = await cursor.fetchone()
                    return result[0] if result and result[0] else 0
                    
        except Exception as e:
            logger.error(f"Error getting video watch time: {e}")
            return 0
    

    
    async def export_user_activities(self, user_id: str, guild_id: str = None, 
                                   start_date: datetime = None, end_date: datetime = None) -> List[Dict[str, Any]]:
        """Export user's activities as JSON data"""
        try:
            import aiosqlite
            
            # Build query with optional filters
            where_clauses = ["user_id = ?", "deleted_at IS NULL"]
            params = [user_id]
            
            if guild_id:
                where_clauses.append("guild_id = ?")
                params.append(guild_id)
            
            if start_date:
                where_clauses.append("date >= ?")
                params.append(start_date.isoformat())
            
            if end_date:
                where_clauses.append("date <= ?")
                params.append(end_date.isoformat())
            
            where_clause = " AND ".join(where_clauses)
            
            async with aiosqlite.connect(self.db.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                async with db.execute(f"""
                    SELECT * FROM immersion_activities 
                    WHERE {where_clause}
                    ORDER BY date DESC
                """, params) as cursor:
                    rows = await cursor.fetchall()
                    
                    activities_data = []
                    for row in rows:
                        data = dict(row)
                        # Parse meta JSON for export
                        if isinstance(data.get('meta'), str):
                            data['meta'] = json.loads(data['meta'])
                        activities_data.append(data)
                    
                    return activities_data
                    
        except Exception as e:
            logger.error(f"Error exporting activities: {e}")
            return []
    
    async def get_activities_in_range(self, user_id: str, guild_id: str = None,
                                    start_date: datetime = None, 
                                    end_date: datetime = None) -> List[Dict[str, Any]]:
        """Get user activities within a date range for chart generation"""
        try:
            import aiosqlite
            
            # Build query (no guild filtering)
            where_clauses = ["user_id = ?", "deleted_at IS NULL"]
            params = [user_id]
            
            if start_date:
                where_clauses.append("date >= ?")
                params.append(start_date.isoformat())
            
            if end_date:
                where_clauses.append("date <= ?")
                params.append(end_date.isoformat())
            
            where_clause = " AND ".join(where_clauses)
            
            async with aiosqlite.connect(self.db.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                query = f"""
                    SELECT * FROM immersion_activities 
                    WHERE {where_clause}
                    ORDER BY date ASC
                """
                
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    
                    activities_data = []
                    for row in rows:
                        data = dict(row)
                        
                        # Parse date string to datetime object
                        data['date'] = datetime.fromisoformat(data['date'])
                        
                        # Parse meta JSON
                        if isinstance(data.get('meta'), str):
                            try:
                                data['meta'] = json.loads(data['meta'])
                            except (json.JSONDecodeError, TypeError):
                                data['meta'] = {}
                        
                        activities_data.append(data)
                    
                    return activities_data
                    
        except Exception as e:
            logger.error(f"Error getting activities in range: {e}")
            return []
    
    async def get_first_activity_date(self, user_id: str, media_type: str = None, 
                                     activity_type: str = None) -> Optional[datetime]:
        """Get the date of the user's first activity, optionally filtered by media/activity type"""
        try:
            import aiosqlite
            
            where_clauses = ["user_id = ?", "deleted_at IS NULL", "duration > 0"]
            params = [user_id]
            
            if media_type:
                where_clauses.append("media_type = ?")
                params.append(media_type)
            
            if activity_type:
                where_clauses.append("primary_type = ?")
                params.append(activity_type)
            
            where_clause = " AND ".join(where_clauses)
            
            async with aiosqlite.connect(self.db.db_path) as db:
                query = f"""
                    SELECT MIN(date) as first_date
                    FROM immersion_activities 
                    WHERE {where_clause}
                """
                
                async with db.execute(query, params) as cursor:
                    row = await cursor.fetchone()
                    if row and row[0]:
                        return datetime.fromisoformat(row[0])
                    return None
                    
        except Exception as e:
            logger.error(f"Error getting first activity date: {e}")
            return None
    
    async def get_user_stats(self, user_id: str, guild_id: str = None,
                           days: int = 30, media_type: str = None,
                           activity_type: str = None) -> Dict[str, Any]:
        """Get user statistics for the past N days with optional filters"""
        try:
            import aiosqlite
            
            # Check cache first
            cache_key = f"{user_id}_{guild_id}_{days}_{media_type}_{activity_type}"
            cached_result = self._get_cache_entry(self._stats_cache, cache_key)
            if cached_result is not None:
                logger.info(f"Returning cached stats for user {user_id}")
                return cached_result
            
            logger.debug(f"Input params: user_id={user_id}, guild_id={guild_id}, days={days}")
            logger.debug(f"Filters: media_type={media_type}, activity_type={activity_type}")
            
            # Use end of today in UTC so all of today's activities are included
            end_date = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=999999)
            
            # For "All Time" (days >= 9999), find the user's first activity
            if days >= 9999:
                logger.info(f"All-time stats requested, finding first activity date...")
                first_activity_date = await self.get_first_activity_date(
                    user_id=user_id,
                    media_type=media_type,
                    activity_type=activity_type
                )
                if first_activity_date:
                    start_date = first_activity_date
                    logger.info(f"First activity found: {start_date}")
                else:
                    start_date = end_date - timedelta(days=365)
                    logger.info(f"No first activity found, using 365 days ago: {start_date}")
            else:
                start_date = end_date - timedelta(days=days)
                logger.info(f"Date range: {start_date} to {end_date}")
            
            # Build query with optional filters
            where_clauses = ["user_id = ?", "deleted_at IS NULL", "date >= ?"]
            params = [user_id, start_date.isoformat()]
            
            # Add media type filter
            if media_type:
                if media_type == 'manual':
                    # Manual logs have NULL media_type in database
                    where_clauses.append("media_type IS NULL")
                    logger.info(f"Added media_type filter: manual (NULL)")
                else:
                    where_clauses.append("media_type = ?")
                    params.append(media_type)
                    logger.info(f"Added media_type filter: {media_type}")
            
            # Add activity type filter
            if activity_type:
                where_clauses.append("primary_type = ?")
                params.append(activity_type)
                logger.info(f"Added activity_type filter: {activity_type}")
            
            where_clause = " AND ".join(where_clauses)
            logger.debug(f"Final WHERE clause: {where_clause}")
            logger.debug(f"Query params: {params}")
            
            async with aiosqlite.connect(self.db.db_path) as db:
                # OPTIMIZED: Single consolidated query instead of 3 separate queries
                # This reduces database scans from 3N to N rows
                db.row_factory = aiosqlite.Row
                
                query = f"""
                    WITH base_activities AS (
                        SELECT 
                            duration,
                            primary_type,
                            media_type,
                            DATE(date) as activity_date
                        FROM immersion_activities
                        WHERE {where_clause} AND duration > 0
                    ),
                    total_stats AS (
                        SELECT 
                            COUNT(*) as total_activities,
                            SUM(duration) as total_duration,
                            SUM(CASE WHEN primary_type = 'reading' THEN duration ELSE 0 END) as reading_duration,
                            SUM(CASE WHEN primary_type = 'listening' THEN duration ELSE 0 END) as listening_duration,
                            AVG(duration) as avg_duration
                        FROM base_activities
                    ),
                    daily_stats AS (
                        SELECT 
                            activity_date as day,
                            COUNT(*) as activity_count,
                            SUM(duration) as daily_duration
                        FROM base_activities
                        GROUP BY activity_date
                        ORDER BY activity_date DESC
                    ),
                    media_stats AS (
                        SELECT 
                            COALESCE(media_type, 'manual') as media_type,
                            COUNT(*) as count,
                            SUM(duration) as duration
                        FROM base_activities
                        GROUP BY COALESCE(media_type, 'manual')
                        ORDER BY duration DESC
                    )
                    SELECT 
                        (SELECT total_activities FROM total_stats) as total_activities,
                        (SELECT total_duration FROM total_stats) as total_duration,
                        (SELECT reading_duration FROM total_stats) as reading_duration,
                        (SELECT listening_duration FROM total_stats) as listening_duration,
                        (SELECT avg_duration FROM total_stats) as avg_duration,
                        (SELECT json_group_array(
                            json_object(
                                'date', day,
                                'activity_count', activity_count,
                                'duration', daily_duration
                            )
                         ) FROM daily_stats) as daily_json,
                        (SELECT json_group_array(
                            json_object(
                                'media_type', media_type,
                                'count', count,
                                'duration', duration
                            )
                         ) FROM media_stats) as media_json
                """
                logger.info(f"Executing consolidated stats query (1 query instead of 3)...")
                
                async with db.execute(query, params) as cursor:
                    row = await cursor.fetchone()
                    
                    if not row:
                        logger.warning("No stats data returned")
                        return {
                            'period_days': days,
                            'total_activities': 0,
                            'total_duration': 0,
                            'reading_duration': 0,
                            'listening_duration': 0,
                            'avg_duration': 0,
                            'daily_breakdown': [],
                            'media_breakdown': []
                        }
                    
                    # Parse JSON arrays
                    import json
                    daily_breakdown = json.loads(row['daily_json']) if row['daily_json'] else []
                    media_breakdown = json.loads(row['media_json']) if row['media_json'] else []
                    
                    logger.info(f"Query complete - found {len(daily_breakdown)} days, {len(media_breakdown)} media types")
                
                result = {
                    'period_days': days,
                    'total_activities': row['total_activities'] or 0,
                    'total_duration': row['total_duration'] or 0,
                    'reading_duration': row['reading_duration'] or 0,
                    'listening_duration': row['listening_duration'] or 0,
                    'avg_duration': row['avg_duration'] or 0,
                    'daily_breakdown': daily_breakdown,
                    'media_breakdown': media_breakdown
                }
                
                logger.info(f"Final result summary:")
                logger.info(f"  total_activities: {result['total_activities']}")
                logger.info(f"  total_duration: {result['total_duration']}s")
                logger.info(f"  daily_breakdown entries: {len(result['daily_breakdown'])}")
                logger.info(f"  media_breakdown entries: {len(result['media_breakdown'])}")
                
                # Cache the result before returning
                self._set_cache_entry(self._stats_cache, cache_key, result)
                
                return result
                
        except Exception as e:
            logger.error(f"Error getting user stats: {e}", exc_info=True)
            return {
                'period_days': days,
                'total_activities': 0,
                'total_duration': 0,
                'reading_duration': 0,
                'listening_duration': 0,
                'avg_duration': 0,
                'daily_breakdown': [],
                'media_breakdown': []
            }

    async def get_guild_overview_stats(self, guild_id: str,
                                      start_date: Optional[datetime] = None,
                                      end_date: Optional[datetime] = None,
                                      metric: str = "duration") -> Dict[str, Any]:
        """Get comprehensive guild-wide statistics for overview command

        Args:
            guild_id: Guild ID to query
            start_date: Start date for filtering (None = all time)
            end_date: End date for filtering
            metric: Primary metric being displayed ("duration", "characters", "pages", "episodes")

        Returns:
            Dictionary containing guild-wide aggregate statistics
        """
        try:
            import aiosqlite

            # Check cache first
            cache_key = f"overview_{guild_id}_{start_date}_{end_date}_{metric}"
            cached_result = self._get_cache_entry(self._stats_cache, cache_key)
            if cached_result is not None:
                logger.info(f"Returning cached guild overview for guild {guild_id}")
                return cached_result

            logger.debug(f"Input params: guild_id={guild_id}, start_date={start_date}, end_date={end_date}")

            # Build WHERE clause
            # NOTE: Like the leaderboard, guild_id is not filtered when it is None
            # This aggregates across all guilds for the consolidated bot design
            where_clauses = ["ia.deleted_at IS NULL", "ia.duration > 0"]
            params = []

            if guild_id is not None:
                where_clauses.append("ia.guild_id = ?")
                params.append(guild_id)

            if start_date:
                where_clauses.append("ia.date >= ?")
                params.append(start_date.isoformat())

            if end_date:
                where_clauses.append("ia.date <= ?")
                params.append(end_date.isoformat())

            where_clause = " AND ".join(where_clauses)

            async with aiosqlite.connect(self.db.db_path) as db:
                db.row_factory = aiosqlite.Row

                # Single consolidated query with CTEs for all stats
                query = f"""
                    WITH base_activities AS (
                        SELECT
                            ia.user_id,
                            ia.duration,
                            ia.primary_type,
                            ia.media_type,
                            ia.name,
                            ia.meta,
                            DATE(ia.date) as activity_date,
                            ia.date as full_date
                        FROM immersion_activities ia
                        LEFT JOIN user_blacklist ub ON ia.user_id = ub.user_id
                        WHERE {where_clause}
                          AND ub.user_id IS NULL
                    ),
                    total_stats AS (
                        SELECT
                            COUNT(*) as total_logs,
                            COUNT(DISTINCT user_id) as active_users,
                            SUM(duration) as total_duration,
                            AVG(duration) as avg_duration,
                            SUM(CASE WHEN primary_type = 'reading' THEN duration ELSE 0 END) as reading_duration,
                            SUM(CASE WHEN primary_type = 'listening' THEN duration ELSE 0 END) as listening_duration,
                            COALESCE(SUM(CAST(json_extract(meta, '$.characters') AS INTEGER)), 0) as total_characters,
                            COALESCE(SUM(CAST(json_extract(meta, '$.pages') AS REAL)), 0) as total_pages,
                            COALESCE(SUM(CAST(json_extract(meta, '$.episodes') AS INTEGER)), 0) as total_episodes
                        FROM base_activities
                    ),
                    media_stats AS (
                        SELECT
                            COALESCE(media_type, 'manual') as media_type,
                            COUNT(*) as count,
                            SUM(duration) as duration,
                            COALESCE(SUM(CAST(json_extract(meta, '$.characters') AS INTEGER)), 0) as characters,
                            COALESCE(SUM(CAST(json_extract(meta, '$.pages') AS REAL)), 0) as pages,
                            COALESCE(SUM(CAST(json_extract(meta, '$.episodes') AS INTEGER)), 0) as episodes
                        FROM base_activities
                        GROUP BY media_type
                        ORDER BY duration DESC
                    ),
                    daily_stats AS (
                        SELECT
                            activity_date,
                            COUNT(*) as count,
                            SUM(duration) as duration,
                            COUNT(DISTINCT user_id) as users
                        FROM base_activities
                        GROUP BY activity_date
                        ORDER BY activity_date
                    ),
                    top_users AS (
                        SELECT
                            user_id,
                            SUM(duration) as duration,
                            COUNT(*) as count
                        FROM base_activities
                        GROUP BY user_id
                        ORDER BY duration DESC
                        LIMIT 10
                    ),
                    top_titles AS (
                        SELECT
                            name,
                            COALESCE(media_type, 'manual') as media_type,
                            COUNT(*) as count
                        FROM base_activities
                        WHERE name IS NOT NULL AND name != ''
                        GROUP BY name, media_type
                        ORDER BY count DESC
                        LIMIT 5
                    ),
                    new_users AS (
                        SELECT COUNT(DISTINCT user_id) as new_user_count
                        FROM base_activities ba
                        WHERE NOT EXISTS (
                            SELECT 1 FROM immersion_activities ia2
                            WHERE ia2.user_id = ba.user_id
                              {'AND ia2.guild_id = ?' if guild_id is not None else ''}
                              AND ia2.deleted_at IS NULL
                              AND (? IS NULL OR ia2.date < ?)
                        )
                    )
                    SELECT
                        ts.total_logs,
                        ts.active_users,
                        ts.total_duration,
                        ts.avg_duration,
                        ts.reading_duration,
                        ts.listening_duration,
                        ts.total_characters,
                        ts.total_pages,
                        ts.total_episodes,
                        nu.new_user_count
                    FROM total_stats ts, new_users nu
                """

                # Add params for new_users subquery
                new_user_params = params.copy()
                if guild_id is not None:
                    new_user_params.append(guild_id)
                new_user_params.extend([start_date.isoformat() if start_date else None,
                                       start_date.isoformat() if start_date else None])

                logger.info(f"Executing main query with {len(new_user_params)} params")
                cursor = await db.execute(query, new_user_params)
                row = await cursor.fetchone()

                if not row:
                    logger.warning("No data returned from main query")
                    return self._empty_guild_overview_stats()

                # Fetch media breakdown
                media_query = f"""
                    SELECT
                        COALESCE(ia.media_type, 'manual') as media_type,
                        COUNT(*) as count,
                        SUM(ia.duration) as duration,
                        COALESCE(SUM(CAST(json_extract(ia.meta, '$.characters') AS INTEGER)), 0) as characters,
                        COALESCE(SUM(CAST(json_extract(ia.meta, '$.pages') AS REAL)), 0) as pages,
                        COALESCE(SUM(CAST(json_extract(ia.meta, '$.episodes') AS INTEGER)), 0) as episodes
                    FROM immersion_activities ia
                    LEFT JOIN user_blacklist ub ON ia.user_id = ub.user_id
                    WHERE {where_clause}
                      AND ub.user_id IS NULL
                    GROUP BY ia.media_type
                    ORDER BY duration DESC
                """

                cursor = await db.execute(media_query, params)
                media_rows = await cursor.fetchall()
                media_breakdown = [dict(r) for r in media_rows]

                # Fetch daily breakdown
                daily_query = f"""
                    SELECT
                        DATE(ia.date) as date,
                        COUNT(*) as count,
                        SUM(ia.duration) as duration,
                        COUNT(DISTINCT ia.user_id) as users,
                        COALESCE(SUM(CAST(json_extract(ia.meta, '$.characters') AS INTEGER)), 0) as characters,
                        COALESCE(SUM(CAST(json_extract(ia.meta, '$.pages') AS REAL)), 0) as pages,
                        COALESCE(SUM(CAST(json_extract(ia.meta, '$.episodes') AS INTEGER)), 0) as episodes
                    FROM immersion_activities ia
                    LEFT JOIN user_blacklist ub ON ia.user_id = ub.user_id
                    WHERE {where_clause}
                      AND ub.user_id IS NULL
                    GROUP BY DATE(ia.date)
                    ORDER BY date
                """

                cursor = await db.execute(daily_query, params)
                daily_rows = await cursor.fetchall()
                daily_breakdown = [dict(r) for r in daily_rows]

                # Fetch top users
                top_users_query = f"""
                    SELECT
                        ia.user_id,
                        SUM(ia.duration) as duration,
                        COUNT(*) as count
                    FROM immersion_activities ia
                    LEFT JOIN user_blacklist ub ON ia.user_id = ub.user_id
                    WHERE {where_clause}
                      AND ub.user_id IS NULL
                    GROUP BY ia.user_id
                    ORDER BY duration DESC
                    LIMIT 10
                """

                cursor = await db.execute(top_users_query, params)
                top_users_rows = await cursor.fetchall()
                top_users = [dict(r) for r in top_users_rows]

                # Fetch top titles
                top_titles_query = f"""
                    SELECT
                        ia.name,
                        COALESCE(ia.media_type, 'manual') as media_type,
                        COUNT(*) as count
                    FROM immersion_activities ia
                    LEFT JOIN user_blacklist ub ON ia.user_id = ub.user_id
                    WHERE {where_clause}
                      AND ub.user_id IS NULL
                      AND ia.name IS NOT NULL
                      AND ia.name != ''
                    GROUP BY ia.name, ia.media_type
                    ORDER BY count DESC
                    LIMIT 5
                """

                cursor = await db.execute(top_titles_query, params)
                top_titles_rows = await cursor.fetchall()
                top_titles = [dict(r) for r in top_titles_rows]

                # Build result dictionary
                result = {
                    'total_logs': row['total_logs'] or 0,
                    'total_duration': row['total_duration'] or 0,
                    'active_users': row['active_users'] or 0,
                    'avg_duration': row['avg_duration'] or 0,
                    'avg_user_duration': (row['total_duration'] / row['active_users']) if row['active_users'] > 0 else 0,
                    'reading_duration': row['reading_duration'] or 0,
                    'listening_duration': row['listening_duration'] or 0,
                    'total_characters': row['total_characters'] or 0,
                    'total_pages': row['total_pages'] or 0,
                    'total_episodes': row['total_episodes'] or 0,
                    'new_users': row['new_user_count'] or 0,
                    'media_breakdown': media_breakdown,
                    'daily_breakdown': daily_breakdown,
                    'top_users': top_users,
                    'top_titles': top_titles
                }

                # Cache the result
                self._set_cache_entry(self._stats_cache, cache_key, result)

                logger.info(f"Guild overview stats: {result['total_logs']} logs, {result['active_users']} users")

                return result

        except Exception as e:
            logger.error(f"Error in get_guild_overview_stats: {e}", exc_info=True)
            return self._empty_guild_overview_stats()

    def _empty_guild_overview_stats(self) -> Dict[str, Any]:
        """Return empty stats structure for guild overview"""
        return {
            'total_logs': 0,
            'total_duration': 0,
            'active_users': 0,
            'avg_duration': 0,
            'avg_user_duration': 0,
            'reading_duration': 0,
            'listening_duration': 0,
            'total_characters': 0,
            'total_pages': 0,
            'total_episodes': 0,
            'new_users': 0,
            'media_breakdown': [],
            'daily_breakdown': [],
            'top_users': [],
            'top_titles': []
        }

    async def get_user_library(self, user_id: str, media_type: str = None,
                              activity_type: str = None, sort_by: str = "duration") -> List[Dict[str, Any]]:
        """Get user's library: all unique titles they've logged with aggregate statistics
        
        Args:
            user_id: User ID to query
            media_type: Optional media type filter
            activity_type: Optional activity type filter
            sort_by: Sort field - "duration" (default), "count", "recent", "alphabetical"
            
        Returns:
            List of dictionaries containing title, media_type, activity_type, total_duration,
            total_count, first_logged, last_logged, and cover_url (if available)
        """
        try:
            import aiosqlite
            
            # Check cache first (the entire library query is cached, not each page)
            cache_key = f"{user_id}_None_{media_type}_{activity_type}_{sort_by}"
            cached_result = self._get_cache_entry(self._library_cache, cache_key)
            if cached_result is not None:
                logger.debug(f"Returning cached library for user {user_id}")
                return cached_result.get('library', [])
            
            # Build query with optional filters
            where_clauses = ["user_id = ?", "deleted_at IS NULL"]
            params = [user_id]
            
            if media_type:
                if media_type == 'manual':
                    # Manual logs have NULL media_type in database
                    where_clauses.append("media_type IS NULL")
                else:
                    where_clauses.append("media_type = ?")
                    params.append(media_type)
            
            if activity_type:
                where_clauses.append("primary_type = ?")
                params.append(activity_type)
            
            where_clause = " AND ".join(where_clauses)
            
            # Determine sort order - use generic ORDER BY for alphabetical since it is sorted in Python
            if sort_by == "count":
                order_clause = "total_count DESC, total_duration DESC"
            elif sort_by == "recent":
                order_clause = "last_logged DESC"
            elif sort_by == "alphabetical":
                # Don't sort in SQL - Unicode-aware sorting happens in Python
                order_clause = "name"  # Placeholder, will be sorted properly in Python
            else:  # duration (default)
                order_clause = "total_duration DESC"
            
            async with aiosqlite.connect(self.db.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                # Get aggregate stats and fetch one activity per group for metadata
                # For VNs with VNDB ID, consolidate by ID to handle title variations
                # For other media, group by name
                query = f"""
                    WITH enriched_activities AS (
                        SELECT 
                            *,
                            -- Clean autocomplete format from names (e.g., "Title — Date • Rating" -> "Title")
                            CASE 
                                WHEN name LIKE '% — %' AND (name LIKE '%•%' OR name LIKE '%/%') THEN
                                    TRIM(SUBSTR(name, 1, INSTR(name, ' — ') - 1))
                                ELSE name
                            END as cleaned_name,
                            COALESCE(
                                json_extract(meta, '$.vndb_id'),
                                json_extract(meta, '$.vn.id'),
                                NULL
                            ) as vndb_id,
                            json_extract(meta, '$.anilist.id') as anilist_id,
                            json_extract(meta, '$.igdb.id') as igdb_id
                        FROM immersion_activities
                        WHERE {where_clause}
                    ),
                    -- For VNs without metadata, try to find VNDB ID from other entries with same name
                    vndb_id_lookup AS (
                        SELECT DISTINCT
                            cleaned_name,
                            media_type,
                            primary_type,
                            user_id,
                            vndb_id
                        FROM enriched_activities
                        WHERE media_type = 'visual_novel'
                        AND vndb_id IS NOT NULL
                    ),
                    enriched_with_lookup AS (
                        SELECT 
                            e.*,
                            COALESCE(e.vndb_id, l.vndb_id) as final_vndb_id
                        FROM enriched_activities e
                        LEFT JOIN vndb_id_lookup l 
                            ON e.cleaned_name = l.cleaned_name 
                            AND e.media_type = l.media_type
                            AND e.primary_type = l.primary_type
                            AND e.user_id = l.user_id
                            AND e.media_type = 'visual_novel'
                    ),
                    grouped_data AS (
                        SELECT 
                            -- Use a canonical name: prefer one with Japanese characters if available
                            CASE 
                                WHEN media_type = 'visual_novel' AND final_vndb_id IS NOT NULL THEN
                                    (SELECT cleaned_name FROM enriched_with_lookup e2 
                                     WHERE e2.final_vndb_id = e1.final_vndb_id 
                                     AND e2.media_type = e1.media_type
                                     AND e2.primary_type = e1.primary_type
                                     AND e2.user_id = e1.user_id
                                     -- Prioritize names with Japanese characters
                                     ORDER BY (
                                        CASE WHEN cleaned_name GLOB '*[㐀-䶵一-鿋豈-舘並-龎]*' OR cleaned_name GLOB '*[ぁ-ゖ]*' OR cleaned_name GLOB '*[ァ-ヺ]*' THEN 0 ELSE 1 END
                                     ), LENGTH(cleaned_name) DESC
                                     LIMIT 1)
                                ELSE cleaned_name
                            END as canonical_name,
                            media_type,
                            primary_type as activity_type,
                            -- Group by VNDB ID for VNs (using final_vndb_id), otherwise by cleaned name
                            COALESCE(
                                CASE WHEN media_type = 'visual_novel' THEN final_vndb_id END,
                                cleaned_name
                            ) as grouping_key,
                            COUNT(*) as total_count,
                            SUM(duration) as total_duration,
                            MIN(date) as first_logged,
                            MAX(date) as last_logged,
                            final_vndb_id as vndb_id,
                            SUM(COALESCE(CAST(json_extract(meta, '$.characters') AS INTEGER), 0)) as total_characters,
                            SUM(COALESCE(CAST(json_extract(meta, '$.pages') AS REAL), 0)) as total_pages,
                            SUM(COALESCE(CAST(json_extract(meta, '$.episodes') AS INTEGER), 0)) as total_episodes
                        FROM enriched_with_lookup e1
                        GROUP BY grouping_key, media_type, primary_type
                    )
                    SELECT 
                        canonical_name as name,
                        media_type,
                        activity_type,
                        total_count,
                        total_duration,
                        first_logged,
                        last_logged,
                        (SELECT meta FROM enriched_with_lookup e3
                         WHERE (e3.final_vndb_id = grouped_data.vndb_id AND grouped_data.vndb_id IS NOT NULL)
                            OR (e3.name = grouped_data.canonical_name AND grouped_data.vndb_id IS NULL)
                         AND e3.media_type = grouped_data.media_type
                         AND e3.primary_type = grouped_data.activity_type
                         AND e3.meta IS NOT NULL
                         AND LENGTH(e3.meta) > 10
                         -- Prioritize entries with original vndb_id (not looked up)
                         ORDER BY CASE WHEN e3.vndb_id IS NOT NULL THEN 0 ELSE 1 END
                         LIMIT 1) as sample_meta,
                        total_characters,
                        total_pages,
                        total_episodes
                    FROM grouped_data
                    ORDER BY {order_clause}
                """
                
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    
                    library_items = []
                    for row in rows:
                        # Extract cover URL from metadata if available
                        cover_url = None
                        cover_is_nsfw = False
                        vndb_id = None
                        anilist_id = None
                        igdb_id = None
                        youtube_id = None
                        
                        if row['sample_meta']:
                            try:
                                meta = json.loads(row['sample_meta'])
                                logger.debug(f"Parsing metadata for {row['name']}: {list(meta.keys()) if isinstance(meta, dict) else type(meta)}")
                                
                                # Extract IDs for database links
                                vndb_id = meta.get('vndb_id')
                                
                                # Check for top-level 'thumbnail' key (older format for VN/anime)
                                if 'thumbnail' in meta and meta['thumbnail']:
                                    cover_url = meta['thumbnail']
                                    logger.debug(f"Found thumbnail for {row['name']}: {cover_url}, vndb_id: {vndb_id}")
                                
                                # Check for nested VN metadata (newer format)
                                if 'vn' in meta and isinstance(meta['vn'], dict):
                                    vn_data = meta['vn']
                                    if not cover_url:
                                        cover_url = vn_data.get('image_url') or vn_data.get('image')
                                    # Check NSFW status using the shared explicit-image threshold
                                    cover_is_nsfw = is_image_explicit(vn_data.get('image_sexual'))
                                    vndb_id = vn_data.get('id') or vndb_id
                                    logger.debug(f"Found VN cover for {row['name']}: {cover_url} (NSFW: {cover_is_nsfw})")
                                
                                # Check for nested AniList metadata (anime/manga)
                                if 'anilist' in meta and isinstance(meta['anilist'], dict):
                                    anilist_data = meta['anilist']
                                    if not cover_url:
                                        cover_url = anilist_data.get('cover_url') or anilist_data.get('cover_image')
                                    cover_is_nsfw = anilist_data.get('is_adult', False) or anilist_data.get('isAdult', False)
                                    anilist_id = anilist_data.get('id')
                                    logger.debug(f"Found AniList cover for {row['name']}: {cover_url} (NSFW: {cover_is_nsfw})")
                                
                                # Check for nested IGDB metadata (games)
                                if 'igdb' in meta and isinstance(meta['igdb'], dict):
                                    igdb_data = meta['igdb']
                                    if not cover_url:
                                        cover_url = igdb_data.get('cover_url')
                                    # Extract URL (preferred) or fall back to ID
                                    igdb_url = igdb_data.get('url')
                                    if igdb_url:
                                        # Extract slug from full URL (e.g., https://www.igdb.com/games/final-fantasy-ix--3 -> final-fantasy-ix--3)
                                        igdb_id = igdb_url.split('/games/')[-1] if '/games/' in igdb_url else igdb_data.get('id')
                                    else:
                                        igdb_id = igdb_data.get('id')
                                    logger.debug(f"Found IGDB cover for {row['name']}: {cover_url}, id/slug: {igdb_id}")
                                
                                # Check for video metadata (YouTube)
                                if 'video_id' in meta and 'platform' in meta:
                                    platform = meta.get('platform', '').lower()
                                    video_id = meta.get('video_id')
                                    # Only support YouTube for now
                                    if platform == 'youtube' and video_id:
                                        youtube_id = video_id
                                        if not cover_url:
                                            cover_url = meta.get('thumbnail')
                                        logger.debug(f"Found YouTube video for {row['name']}: {youtube_id}")
                                
                            except (json.JSONDecodeError, TypeError, KeyError) as e:
                                logger.warning(f"Could not parse metadata for {row['name']}: {e}")
                        else:
                            logger.debug(f"No metadata found for {row['name']}")
                        
                        # Note: name is already cleaned by SQL, but apply Python cleaning as safety net
                        cleaned_name = _clean_activity_name(row['name'])
                        
                        library_items.append({
                            'name': cleaned_name,
                            'media_type': row['media_type'],
                            'activity_type': row['activity_type'],
                            'total_count': row['total_count'],
                            'total_duration': row['total_duration'] or 0,
                            'first_logged': row['first_logged'],
                            'last_logged': row['last_logged'],
                            'cover_url': cover_url,
                            'cover_is_nsfw': cover_is_nsfw,
                            'vndb_id': vndb_id,
                            'anilist_id': anilist_id,
                            'igdb_id': igdb_id,
                            'youtube_id': youtube_id,
                            'total_characters': row['total_characters'] or 0,
                            'total_pages': row['total_pages'] or 0,
                            'total_episodes': row['total_episodes'] or 0
                        })
                    
                    # If alphabetical sort was requested, sort properly in Python with Unicode normalization
                    # SQLite's COLLATE doesn't handle Japanese/international characters well
                    if sort_by == "alphabetical":
                        import unicodedata
                        
                        def normalize_for_sort(text: str) -> str:
                            """Normalize text for proper Unicode sorting"""
                            # Convert to NFD (decomposed) form for consistent comparison
                            # Convert to lowercase for case-insensitive sorting
                            return unicodedata.normalize('NFD', text.lower())
                        
                        library_items.sort(key=lambda x: normalize_for_sort(x['name']))
                        logger.debug(f"Sorted {len(library_items)} items alphabetically with Unicode normalization")
                    
                    # Cache the result before returning
                    self._set_cache_entry(self._library_cache, cache_key, {'library': library_items})
                    
                    return library_items
                    
        except Exception as e:
            logger.error(f"Error getting user library: {e}")
            return []
    
    async def get_import_history(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get user's import history"""
        try:
            return await self.db.get_import_history(user_id, limit)
        except Exception as e:
            logger.error(f"Error getting import history: {e}")
            return []
    
    async def undo_import(self, user_id: str, import_timestamp: datetime) -> Dict[str, Any]:
        """Undo an import by timestamp"""
        try:
            return await self.db.undo_import_by_timestamp(user_id, import_timestamp)
        except Exception as e:
            logger.error(f"Error undoing import: {e}")
            return {"count": 0, "total_duration": 0, "total_characters": 0}

    async def cleanup_orphaned_imports(self, user_id: str = None) -> int:
        """Remove import records that have no associated activities"""
        try:
            return await self.db.cleanup_orphaned_imports(user_id)
        except Exception as e:
            logger.error(f"Error cleaning up import records: {e}")
            return 0
    
    async def import_activities(self, user_id: str, activities: List[Dict[str, Any]], 
                              filename: str = None) -> Dict[str, Any]:
        """Import activities from external source"""
        try:
            logger.info(f"Starting import for user {user_id}: {len(activities)} activities from {filename}")
            import_timestamp = datetime.now()
            
            # Override user_id for all activities (security)
            for i, activity in enumerate(activities):
                activity['user_id'] = user_id
                # Log first 3 activities for debugging
                if i < 3:
                    logger.info(f"Activity {i+1}: {activity['name']} - {activity['duration']}s")
            
            # Import activities with timestamp
            logger.info(f"Calling database import_activities_batch...")
            imported_count = await self.db.import_activities_batch(activities, import_timestamp)
            logger.info(f"Database returned imported_count: {imported_count}")
            
            # Record import in history
            if imported_count > 0:
                import_id = await self.db.record_import(
                    user_id=user_id,
                    import_timestamp=import_timestamp,
                    activity_count=imported_count,
                    filename=filename
                )
                
                logger.info(f"Import successful: {imported_count} activities, import_id: {import_id}")
                return {
                    'success': True,
                    'imported_count': imported_count,
                    'import_id': import_id,
                    'import_timestamp': import_timestamp
                }
            else:
                logger.warning(f"Import failed: No activities were imported")
                return {
                    'success': False,
                    'error': 'No activities were imported'
                }
                
        except ValueError as e:
            logger.warning(f"Validation error importing activities for user {user_id}: {e}")
            return {
                'success': False,
                'error': str(e)
            }
        except Exception as e:
            logger.error(f"Error importing activities: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {
                'success': False,
                'error': str(e)
            }

class GoalRepository:
    """Repository for managing user goals"""
    
    def __init__(self, database: Database):
        self.db = database
    
    async def create_goal(self, goal: Goal) -> Optional[int]:
        """Create a new goal"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db.db_path) as db:
                cursor = await db.execute("""
                    INSERT INTO immersion_goals 
                    (user_id, name, activity_type, media_type, youtube_channels, 
                     cron, target, current_value, due_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    goal.user_id,
                    goal.name,
                    goal.activity_type,
                    goal.media_type,
                    ','.join(goal.youtube_channels) if goal.youtube_channels else None,
                    goal.cron,
                    goal.target,
                    goal.current_value,
                    goal.due_at.isoformat() if goal.due_at else None
                ))
                
                await db.commit()
                goal.id = cursor.lastrowid
                logger.info(f"Created goal {goal.id} for user {goal.user_id}")
                return goal.id
                
        except Exception as e:
            logger.error(f"Error creating goal: {e}")
            return None
    
    async def get_user_goals(self, user_id: str) -> List[Goal]:
        """Get all active goals for a user"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                async with db.execute("""
                    SELECT * FROM immersion_goals 
                    WHERE user_id = ? AND deleted_at IS NULL
                    ORDER BY created_at DESC
                """, (user_id,)) as cursor:
                    rows = await cursor.fetchall()
                    
                    goals = []
                    for row in rows:
                        data = dict(row)
                        
                        # Parse timestamps
                        if data.get('due_at'):
                            data['due_at'] = datetime.fromisoformat(data['due_at'])
                        if data.get('created_at'):
                            data['created_at'] = datetime.fromisoformat(data['created_at'])
                        
                        goal = Goal.from_dict(data)
                        goals.append(goal)
                    
                    return goals
                    
        except Exception as e:
            logger.error(f"Error getting user goals: {e}")
            return []
    
    async def update_goal_progress(self, goal_id: int, new_value: int) -> bool:
        """Update goal progress"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db.db_path) as db:
                cursor = await db.execute("""
                    UPDATE immersion_goals 
                    SET current_value = ? 
                    WHERE id = ?
                """, (new_value, goal_id))
                
                await db.commit()
                return cursor.rowcount > 0
                
        except Exception as e:
            logger.error(f"Error updating goal progress: {e}")
            return False
    
    async def delete_goal(self, goal_id: int, user_id: str) -> bool:
        """Soft delete a goal"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db.db_path) as db:
                cursor = await db.execute("""
                    UPDATE immersion_goals 
                    SET deleted_at = CURRENT_TIMESTAMP 
                    WHERE id = ? AND user_id = ?
                """, (goal_id, user_id))
                
                await db.commit()
                success = cursor.rowcount > 0
                
                if success:
                    logger.info(f"Deleted goal {goal_id} for user {user_id}")
                
                return success
                
        except Exception as e:
            logger.error(f"Error deleting goal: {e}")
            return False

class UserRepository:
    """Repository for managing user settings and preferences"""
    
    def __init__(self, database: Database):
        self.db = database
    
    async def get_user_settings(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user's immersion tracking settings"""
        return await self.db.get_user_settings(user_id)
    
    async def update_user_settings(self, user_id: str, **kwargs) -> bool:
        """Update user's settings"""
        return await self.db.update_user_settings(user_id, **kwargs)
    
    async def get_user_timezone(self, user_id: str) -> Optional[str]:
        """Get user's timezone setting"""
        settings = await self.get_user_settings(user_id)
        return settings.get('timezone') if settings else None
    
    async def set_user_timezone(self, user_id: str, timezone_str: str) -> bool:
        """Set user's timezone"""
        return await self.update_user_settings(user_id, timezone=timezone_str)
    
