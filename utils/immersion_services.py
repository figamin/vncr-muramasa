"""
User management and goal system for immersion tracking
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Tuple
from utils.immersion_models import Goal, Activity
from utils.immersion_repository import UserRepository, GoalRepository, ActivityRepository

logger = logging.getLogger(__name__)

class UserTimeService:
    """Service for handling user timezone operations"""
    
    def __init__(self, user_repo: UserRepository, guild_repo=None):
        self.user_repo = user_repo
        self.guild_repo = guild_repo
    
    async def get_time_location(self, user_id: str, guild_id: str = None) -> timezone:
        """Get user's timezone, falling back to guild timezone if needed"""
        try:
            # Try to get user timezone first
            user_tz = await self.user_repo.get_user_timezone(user_id)
            if user_tz:
                import zoneinfo
                return zoneinfo.ZoneInfo(user_tz)
            
            # Fall back to guild timezone if available
            if guild_id and self.guild_repo:
                guild_tz = await self.guild_repo.get_guild_timezone(guild_id)
                if guild_tz:
                    import zoneinfo
                    return zoneinfo.ZoneInfo(guild_tz)
            
            # Default to UTC
            return timezone.utc
            
        except Exception as e:
            logger.warning(f"Error getting timezone for user {user_id}: {e}")
            return timezone.utc
    
    async def parse_user_datetime(self, user_id: str, guild_id: str, datetime_str: str) -> datetime:
        """Parse a datetime string in the user's timezone.

        Accepts (case-insensitive):
        - Relative keywords: today/t, yesterday/y
        - Negative integers for days ago: -0, -1, ..., -7
        - Weekday names: mon/monday, tue/tuesday, ... (most recent occurrence within last 7 days)
        - Short month/day: 6/15, 06-15, 6-15 (year inferred; rolls to prior year if future)
        - Full dates: YYYY-MM-DD, MM/DD/YYYY (with optional HH:MM[:SS])
        """
        try:
            user_tz = await self.get_time_location(user_id, guild_id)
            raw = datetime_str.strip()
            key = raw.lower()

            # Relative keywords (past-only; future logging isn't supported beyond a TZ-buffer)
            relative_keywords = {
                'today': 0, 't': 0,
                'yesterday': 1, 'y': 1,
            }
            if key in relative_keywords:
                today_local = datetime.now(user_tz)
                target = today_local - timedelta(days=relative_keywords[key])
                return target.replace(hour=12, minute=0, second=0, microsecond=0)

            # Bare negative integer (days ago)
            if len(key) >= 2 and key[0] == '-' and key[1:].isdigit():
                days_ago = int(key[1:])
                today_local = datetime.now(user_tz)
                target = today_local - timedelta(days=days_ago)
                return target.replace(hour=12, minute=0, second=0, microsecond=0)

            # Weekday names (full + 3-letter)
            weekday_map = {
                'monday': 0, 'mon': 0,
                'tuesday': 1, 'tue': 1, 'tues': 1,
                'wednesday': 2, 'wed': 2,
                'thursday': 3, 'thu': 3, 'thur': 3, 'thurs': 3,
                'friday': 4, 'fri': 4,
                'saturday': 5, 'sat': 5,
                'sunday': 6, 'sun': 6,
            }
            if key in weekday_map:
                today_local = datetime.now(user_tz)
                days_back = (today_local.weekday() - weekday_map[key]) % 7
                target = today_local - timedelta(days=days_back)
                return target.replace(hour=12, minute=0, second=0, microsecond=0)

            # Short month/day (no year): 6/15, 06-15, 6-15, etc.
            for sep in ('/', '-'):
                if sep in key and key.count(sep) == 1:
                    a, b = key.split(sep)
                    if a.isdigit() and b.isdigit() and 1 <= len(a) <= 2 and 1 <= len(b) <= 2:
                        month, day = int(a), int(b)
                        try:
                            today_local = datetime.now(user_tz)
                            candidate = datetime(today_local.year, month, day, 12, 0, 0, tzinfo=user_tz)
                            # Roll back only if the candidate is far in the future (>6 months).
                            # Handles New Year wraparound ("12/30" in early January = last December)
                            # while keeping near-future inputs ("4/26" tomorrow) in the current year
                            # so the validator returns a clear "more than 1 day in the future" error.
                            if (candidate - today_local).total_seconds() > 180 * 86400:
                                candidate = candidate.replace(year=today_local.year - 1)
                            return candidate
                        except ValueError:
                            # Invalid month/day combo (e.g. 13/45): fall through to legacy formats
                            break

            # Legacy formats with time components
            time_formats = [
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d %H:%M',
                '%m/%d/%Y %H:%M:%S',
                '%m/%d/%Y %H:%M',
            ]

            # Legacy date-only formats (default to noon to avoid timezone date-shift issues)
            date_only_formats = [
                '%Y-%m-%d',
                '%m/%d/%Y'
            ]

            for fmt in time_formats:
                try:
                    dt = datetime.strptime(raw, fmt)
                    return dt.replace(tzinfo=user_tz)
                except ValueError:
                    continue

            for fmt in date_only_formats:
                try:
                    dt = datetime.strptime(raw, fmt)
                    dt = dt.replace(hour=12, minute=0, second=0, microsecond=0)
                    return dt.replace(tzinfo=user_tz)
                except ValueError:
                    continue

            raise ValueError(
                f"Unable to parse date '{datetime_str}'. "
                f"Try 'yesterday', '-2' (days ago), 'mon', '6/15', or a full date like YYYY-MM-DD."
            )

        except Exception as e:
            logger.error(f"Error parsing user datetime: {e}")
            raise

class GoalService:
    """Service for managing and checking user goals"""
    
    def __init__(self, goal_repo: GoalRepository, time_service: UserTimeService,
                 activity_repo: Optional[ActivityRepository] = None):
        self.goal_repo = goal_repo
        self.time_service = time_service
        self.activity_repo = activity_repo

    async def find_user_goals_by_name(self, user_id: str, name: str) -> List[Goal]:
        goals = await self.goal_repo.get_user_goals(user_id)
        return [goal for goal in goals if goal.name == name]

    async def delete_goal(self, goal_id: int, user_id: str) -> bool:
        return await self.goal_repo.delete_goal(goal_id, user_id)
    
    async def create_daily_goal(self, user_id: str, name: str, target_minutes: int,
                              activity_type: str = None, media_type: str = None) -> Optional[int]:
        """Create a daily goal"""
        try:
            # Calculate next midnight for due date
            now = datetime.now(timezone.utc)
            next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            
            goal = Goal(
                user_id=user_id,
                name=name,
                target=target_minutes * 60,  # Convert to seconds
                cron="0 0 * * *",  # Daily at midnight
                activity_type=activity_type,
                media_type=media_type
            )
            goal.due_at = next_midnight
            
            return await self.goal_repo.create_goal(goal)
            
        except Exception as e:
            logger.error(f"Error creating daily goal: {e}")
            return None
    
    async def create_weekly_goal(self, user_id: str, name: str, target_minutes: int,
                               activity_type: str = None, media_type: str = None) -> Optional[int]:
        """Create a weekly goal"""
        try:
            # Calculate next Sunday midnight
            now = datetime.now(timezone.utc)
            days_until_sunday = (6 - now.weekday()) % 7
            if days_until_sunday == 0:
                days_until_sunday = 7
            next_sunday = (now + timedelta(days=days_until_sunday)).replace(hour=0, minute=0, second=0, microsecond=0)
            
            goal = Goal(
                user_id=user_id,
                name=name,
                target=target_minutes * 60,  # Convert to seconds
                cron="0 0 * * 0",  # Weekly on Sunday at midnight
                activity_type=activity_type,
                media_type=media_type
            )
            goal.due_at = next_sunday
            
            return await self.goal_repo.create_goal(goal)
            
        except Exception as e:
            logger.error(f"Error creating weekly goal: {e}")
            return None
    
    async def create_monthly_goal(self, user_id: str, name: str, target_minutes: int,
                                activity_type: str = None, media_type: str = None) -> Optional[int]:
        """Create a monthly goal"""
        try:
            # Calculate first day of next month
            now = datetime.now(timezone.utc)
            if now.month == 12:
                next_month = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            else:
                next_month = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
            
            goal = Goal(
                user_id=user_id,
                name=name,
                target=target_minutes * 60,  # Convert to seconds
                cron="0 0 1 * *",  # Monthly on the 1st at midnight
                activity_type=activity_type,
                media_type=media_type
            )
            goal.due_at = next_month
            
            return await self.goal_repo.create_goal(goal)
            
        except Exception as e:
            logger.error(f"Error creating monthly goal: {e}")
            return None
    
    async def check_completed(self, activity: Activity) -> List[Goal]:
        """Check if any goals were completed by this activity"""
        try:
            completed_goals = []
            
            # Get all active goals for the user
            goals = await self.goal_repo.get_user_goals(activity.user_id)
            
            for goal in goals:
                # Check if this activity matches the goal criteria
                if not self._activity_matches_goal(activity, goal):
                    continue
                
                # Calculate progress for the current period (includes the activity just logged)
                total_progress = await self._calculate_goal_progress(goal)
                
                # Calculate progress BEFORE this activity by subtracting its duration
                old_progress = max(0, total_progress - activity.duration)
                was_completed_before_activity = old_progress >= goal.target
                
                # Update the goal's stored current_value
                await self.goal_repo.update_goal_progress(goal.id, total_progress)
                goal.current_value = total_progress
                now_completed = total_progress >= goal.target
                
                # Check if goal became completed with this activity
                # Only notify if it wasn't completed before this activity AND is completed now
                if not was_completed_before_activity and now_completed and old_progress < goal.target <= total_progress:
                    completed_goals.append(goal)
            
            return completed_goals
            
        except Exception as e:
            logger.error(f"Error checking completed goals: {e}")
            return []
    
    def _activity_matches_goal(self, activity: Activity, goal: Goal) -> bool:
        """Check if an activity matches a goal's criteria"""
        # Check activity type filter
        if goal.activity_type and activity.primary_type != goal.activity_type:
            return False
        
        # Check media type filter
        if goal.media_type and activity.media_type != goal.media_type:
            return False
        
        # Check YouTube channel filter for videos
        if goal.youtube_channels and activity.media_type == 'video':
            channel_id = activity.get_meta('channel_id')
            if not channel_id or channel_id not in goal.youtube_channels:
                return False
        
        # Check if activity is within goal period
        if goal.due_at:
            due_at = self._ensure_timezone(goal.due_at)
            activity_time = self._ensure_timezone(activity.date)

            # Only enforce the due_at window if the deadline is still in the future.
            if due_at >= datetime.now(timezone.utc) and activity_time > due_at:
                return False
        
        return True
    
    async def _update_goal_progress(self, goal: Goal, activity: Activity):
        """Update goal progress with new activity"""
        try:
            new_progress = await self._calculate_goal_progress(goal)
            await self.goal_repo.update_goal_progress(goal.id, new_progress)
            goal.current_value = new_progress
            
        except Exception as e:
            logger.error(f"Error updating goal progress: {e}")
    
    async def get_user_goals_with_progress(self, user_id: str) -> List[Dict[str, Any]]:
        """Get user goals with current progress"""
        try:
            goals = await self.goal_repo.get_user_goals(user_id)
            
            goals_data = []
            for goal in goals:
                # Calculate actual progress from activities
                await self._recalculate_goal_progress(goal, persist=True)
                
                goals_data.append({
                    'goal': goal,
                    'progress_percentage': goal.progress_percentage,
                    'is_completed': goal.is_completed,
                    'time_remaining': self._get_time_remaining(goal),
                    'target_str': self._format_duration(goal.target),
                    'current_str': self._format_duration(goal.current_value)
                })
            
            return goals_data
            
        except Exception as e:
            logger.error(f"Error getting user goals with progress: {e}")
            return []
    
    async def refresh_user_goals(self, user_id: str) -> List[Goal]:
        """Recalculate and persist progress for all of a user's goals."""
        goals = await self.goal_repo.get_user_goals(user_id)
        for goal in goals:
            await self._recalculate_goal_progress(goal, persist=True)
        return goals

    async def _recalculate_goal_progress(self, goal: Goal, persist: bool = True):
        """Recalculate goal progress from actual activities"""
        try:
            if not self.activity_repo:
                return

            new_value = await self._calculate_goal_progress(goal)
            goal.current_value = new_value

            if persist and goal.id is not None:
                await self.goal_repo.update_goal_progress(goal.id, new_value)
            
        except Exception as e:
            logger.error(f"Error recalculating goal progress: {e}")

    async def _calculate_goal_progress(self, goal: Goal) -> int:
        """Calculate goal progress by summing activities in the current period."""
        if not self.activity_repo:
            return goal.current_value

        start, end = self._get_goal_period_window(goal)

        youtube_channels = goal.youtube_channels if goal.youtube_channels else None

        total = await self.activity_repo.sum_activity_duration(
            user_id=goal.user_id,
            start_date=start,
            end_date=end,
            primary_type=goal.activity_type,
            media_type=goal.media_type,
            youtube_channels=youtube_channels
        )

        return total

    def _get_goal_period_window(self, goal: Goal) -> Tuple[datetime, datetime]:
        """Determine the time window that applies to the goal's current period."""
        now = datetime.now(timezone.utc)
        reference = now

        if goal.cron == "0 0 * * *":  # Daily
            start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
        elif goal.cron == "0 0 * * 0":  # Weekly (Sunday reset)
            weekday = reference.weekday()  # Monday=0, Sunday=6
            days_since_sunday = (weekday + 1) % 7
            start = (reference - timedelta(days=days_since_sunday)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif goal.cron == "0 0 1 * *":  # Monthly
            start = reference.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            start = goal.created_at or reference

        start = self._ensure_timezone(start)
        if start > reference:
            start = reference

        return start, reference

    def _ensure_timezone(self, value: datetime) -> datetime:
        """Ensure datetime values are timezone-aware in UTC."""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    
    def _get_time_remaining(self, goal: Goal) -> Optional[timedelta]:
        """Get time remaining until goal deadline"""
        if not goal.due_at:
            return None
        
        now = datetime.now(timezone.utc)
        if goal.due_at <= now:
            return timedelta(0)
        
        return goal.due_at - now
    
    def _format_duration(self, seconds: int) -> str:
        """Format duration in seconds to human readable string"""
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            minutes = seconds // 60
            return f"{minutes}m"
        else:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            if minutes == 0:
                return f"{hours}h"
            return f"{hours}h {minutes}m"
    
    async def reset_expired_goals(self):
        """Reset goals that have passed their deadline (background task)"""
        try:
            # No-op: progress is always computed from the current period window.
            pass
            
        except Exception as e:
            logger.error(f"Error resetting expired goals: {e}")

class GuildRepository:
    """Repository for guild-specific settings"""
    
    def __init__(self, database):
        self.db = database
    
    async def get_guild_timezone(self, guild_id: str) -> Optional[str]:
        """Get guild's default timezone"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db.db_path) as db:
                async with db.execute(
                    "SELECT timezone FROM immersion_guilds WHERE guild_id = ?",
                    (guild_id,)
                ) as cursor:
                    result = await cursor.fetchone()
                    return result[0] if result else None
                    
        except Exception as e:
            logger.error(f"Error getting guild timezone: {e}")
            return None
    
    async def set_guild_timezone(self, guild_id: str, timezone_str: str) -> bool:
        """Set guild's default timezone"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO immersion_guilds (guild_id, timezone)
                    VALUES (?, ?)
                """, (guild_id, timezone_str))
                
                await db.commit()
                return True
                
        except Exception as e:
            logger.error(f"Error setting guild timezone: {e}")
            return False
    
    async def add_guild_member(self, guild_id: str, user_id: str):
        """Add or update guild member tracking"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db.db_path) as db:
                # Ensure guild exists
                await db.execute(
                    "INSERT OR IGNORE INTO immersion_guilds (guild_id) VALUES (?)",
                    (guild_id,)
                )
                
                # Add/update member
                await db.execute("""
                    INSERT OR REPLACE INTO immersion_guild_members 
                    (guild_id, user_id, last_seen_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                """, (guild_id, user_id))
                
                await db.commit()
                
        except Exception as e:
            logger.error(f"Error adding guild member: {e}")
    
    async def remove_members(self, guild_id: str, user_ids: List[str]) -> bool:
        """Remove members who are no longer in the guild"""
        try:
            if not user_ids:
                return True
            
            import aiosqlite
            
            async with aiosqlite.connect(self.db.db_path) as db:
                placeholders = ','.join('?' * len(user_ids))
                await db.execute(f"""
                    DELETE FROM immersion_guild_members 
                    WHERE guild_id = ? AND user_id IN ({placeholders})
                """, [guild_id] + user_ids)
                
                await db.commit()
                logger.info(f"Removed {len(user_ids)} members from guild {guild_id}")
                return True
                
        except Exception as e:
            logger.error(f"Error removing guild members: {e}")
            return False