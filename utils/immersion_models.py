"""
Immersion tracking models and classes
Based on the botsu bot Go implementation
"""

import json
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union
from enum import Enum
import logging

logger = logging.getLogger(__name__)

class ActivityType(Enum):
    """Primary activity types"""
    READING = "reading"
    LISTENING = "listening"

class MediaType(Enum):
    """Media types for activities"""
    BOOK = "book"
    ANIME = "anime"
    MANGA = "manga"
    VIDEO = "video"
    VISUAL_NOVEL = "visual_novel"

class Activity:
    """Immersion activity model"""
    
    def __init__(self, user_id: str, guild_id: str = None, name: str = "", 
                 primary_type: str = ActivityType.READING.value, 
                 media_type: str = None, duration: int = 0, 
                 date: datetime = None, meta: Dict[str, Any] = None, comment: str = None):
        self.id: Optional[int] = None
        self.user_id = user_id
        self.guild_id = guild_id
        self.name = name
        self.primary_type = primary_type
        self.media_type = media_type
        self.duration = duration  # Duration in seconds
        self.date = date or datetime.now()
        self.meta = meta or {}
        self.comment = comment
        self.created_at: Optional[datetime] = None
        self.deleted_at: Optional[datetime] = None
        self.imported_at: Optional[datetime] = None
    
    def set_meta(self, key: str, value: Any):
        """Set metadata key-value pair"""
        self.meta[key] = value
    
    def get_meta(self, key: str, default: Any = None) -> Any:
        """Get metadata value"""
        return self.meta.get(key, default)
    
    @property
    def duration_minutes(self) -> int:
        """Get duration in minutes"""
        return self.duration // 60
    
    @property
    def duration_hours(self) -> float:
        """Get duration in hours"""
        return self.duration / 3600
    
    def duration_str(self) -> str:
        """Format duration as human-readable string"""
        if self.duration < 60:
            return f"{self.duration}s"
        elif self.duration < 3600:
            minutes = self.duration // 60
            seconds = self.duration % 60
            if seconds == 0:
                return f"{minutes}m"
            return f"{minutes}m {seconds}s"
        else:
            hours = self.duration // 3600
            minutes = (self.duration % 3600) // 60
            if minutes == 0:
                return f"{hours}h"
            return f"{hours}h {minutes}m"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database storage"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'guild_id': self.guild_id,
            'name': self.name,
            'primary_type': self.primary_type,
            'media_type': self.media_type,
            'duration': self.duration,
            'date': self.date.isoformat() if self.date else None,
            'meta': json.dumps(self.meta),
            'comment': self.comment,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'deleted_at': self.deleted_at.isoformat() if self.deleted_at else None,
            'imported_at': self.imported_at.isoformat() if self.imported_at else None
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Activity':
        """Create Activity from dictionary"""
        # Handle date field - could be string or datetime object
        date_value = data.get('date')
        if isinstance(date_value, str):
            date_value = datetime.fromisoformat(date_value) if date_value else None
        elif not isinstance(date_value, datetime):
            date_value = None
            
        # Handle meta field - could be string or dict
        meta_value = data.get('meta', '{}')
        if isinstance(meta_value, str):
            meta_value = json.loads(meta_value)
        
        activity = cls(
            user_id=data['user_id'],
            guild_id=data.get('guild_id'),
            name=data['name'],
            primary_type=data['primary_type'],
            media_type=data.get('media_type'),
            duration=data['duration'],
            date=date_value,
            meta=meta_value,
            comment=data.get('comment')
        )
        
        activity.id = data.get('id')
        
        # Handle created_at field - could be string or datetime object
        created_at_value = data.get('created_at')
        if isinstance(created_at_value, str):
            activity.created_at = datetime.fromisoformat(created_at_value) if created_at_value else None
        elif isinstance(created_at_value, datetime):
            activity.created_at = created_at_value
            
        # Handle deleted_at field
        deleted_at_value = data.get('deleted_at')
        if isinstance(deleted_at_value, str):
            activity.deleted_at = datetime.fromisoformat(deleted_at_value) if deleted_at_value else None
        elif isinstance(deleted_at_value, datetime):
            activity.deleted_at = deleted_at_value
            
        # Handle imported_at field  
        imported_at_value = data.get('imported_at')
        if isinstance(imported_at_value, str):
            activity.imported_at = datetime.fromisoformat(imported_at_value) if imported_at_value else None
        elif isinstance(imported_at_value, datetime):
            activity.imported_at = imported_at_value
        
        return activity

class VideoActivity(Activity):
    """Specialized activity for video content"""
    
    def __init__(self, user_id: str, guild_id: str = None, url: str = "", 
                 title: str = "", channel_name: str = "", duration: int = 0,
                 thumbnail: str = "", video_id: str = "", platform: str = ""):
        super().__init__(
            user_id=user_id,
            guild_id=guild_id,
            name=title,
            primary_type=ActivityType.LISTENING.value,
            media_type=MediaType.VIDEO.value,
            duration=duration
        )
        
        self.set_meta('url', url)
        self.set_meta('channel_name', channel_name)
        self.set_meta('thumbnail', thumbnail)
        self.set_meta('video_id', video_id)
        self.set_meta('platform', platform)
    
    @property
    def url(self) -> str:
        return self.get_meta('url', '')
    
    @property
    def channel_name(self) -> str:
        return self.get_meta('channel_name', '')
    
    @property
    def thumbnail(self) -> str:
        return self.get_meta('thumbnail', '')

class AnimeActivity(Activity):
    """Specialized activity for anime"""
    
    def __init__(self, user_id: str, guild_id: str = None, name: str = "",
                 episodes: int = 1, episode_duration: int = 24):
        duration_seconds = episodes * episode_duration * 60
        super().__init__(
            user_id=user_id,
            guild_id=guild_id,
            name=name,
            primary_type=ActivityType.LISTENING.value,
            media_type=MediaType.ANIME.value,
            duration=duration_seconds
        )
        
        self.set_meta('episodes', episodes)
        self.set_meta('episode_duration', episode_duration)
    
    @property
    def episodes(self) -> int:
        return self.get_meta('episodes', 1)
    
    @property
    def episode_duration(self) -> int:
        return self.get_meta('episode_duration', 24)

class ReadingActivity(Activity):
    """Base class for reading activities"""
    
    def __init__(self, user_id: str, guild_id: str = None, name: str = "",
                 media_type: str = MediaType.BOOK.value, pages: float = 0,
                 characters: int = 0, reading_speed: float = None, duration: int = 0):

        super().__init__(
            user_id=user_id,
            guild_id=guild_id,
            name=name,
            primary_type=ActivityType.READING.value,
            media_type=media_type,
            duration=duration
        )
        
        if pages > 0:
            self.set_meta('pages', pages)
        if characters > 0:
            self.set_meta('characters', characters)
        if reading_speed is not None:
            self.set_meta('reading_speed', reading_speed)

class VNActivity(ReadingActivity):
    """Visual Novel activity"""
    
    def __init__(self, user_id: str, guild_id: str = None, name: str = "",
                 characters: int = 0, reading_speed: float = None, duration: int = 0):
        super().__init__(
            user_id=user_id,
            guild_id=guild_id,
            name=name,
            media_type=MediaType.VISUAL_NOVEL.value,
            characters=characters,
            reading_speed=reading_speed,
            duration=duration
        )

class BookActivity(ReadingActivity):
    """Book reading activity"""
    
    def __init__(self, user_id: str, guild_id: str = None, name: str = "",
                 pages: float = 0, duration: int = 0):
        super().__init__(
            user_id=user_id,
            guild_id=guild_id,
            name=name,
            media_type=MediaType.BOOK.value,
            pages=pages,
            duration=duration
        )

class MangaActivity(ReadingActivity):
    """Manga reading activity"""
    
    def __init__(self, user_id: str, guild_id: str = None, name: str = "",
                 pages: float = 0, duration: int = 0):
        super().__init__(
            user_id=user_id,
            guild_id=guild_id,
            name=name,
            media_type=MediaType.MANGA.value,
            pages=pages,
            duration=duration
        )

class Goal:
    """User goal for immersion tracking"""
    
    def __init__(self, user_id: str, name: str, target: int, cron: str,
                 activity_type: str = None, media_type: str = None,
                 youtube_channels: List[str] = None):
        self.id: Optional[int] = None
        self.user_id = user_id
        self.name = name
        self.activity_type = activity_type
        self.media_type = media_type
        self.youtube_channels = youtube_channels or []
        self.cron = cron
        self.target = target  # Target in seconds
        self.current_value = 0
        self.due_at: Optional[datetime] = None
        self.created_at: Optional[datetime] = None
        self.deleted_at: Optional[datetime] = None
    
    @property
    def is_completed(self) -> bool:
        """Check if goal is completed"""
        return self.current_value >= self.target
    
    @property
    def progress_percentage(self) -> float:
        """Get progress as percentage"""
        if self.target == 0:
            return 0.0
        return min(100.0, (self.current_value / self.target) * 100)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database storage"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'name': self.name,
            'activity_type': self.activity_type,
            'media_type': self.media_type,
            'youtube_channels': ','.join(self.youtube_channels) if self.youtube_channels else None,
            'cron': self.cron,
            'target': self.target,
            'current_value': self.current_value,
            'due_at': self.due_at.isoformat() if self.due_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'deleted_at': self.deleted_at.isoformat() if self.deleted_at else None
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Goal':
        """Create Goal from dictionary"""
        youtube_channels = []
        if data.get('youtube_channels'):
            youtube_channels = data['youtube_channels'].split(',')
        
        goal = cls(
            user_id=data['user_id'],
            name=data['name'],
            target=data['target'],
            cron=data['cron'],
            activity_type=data.get('activity_type'),
            media_type=data.get('media_type'),
            youtube_channels=youtube_channels
        )
        
        goal.id = data.get('id')
        goal.current_value = data.get('current_value', 0)

        due_at_value = data.get('due_at')
        if isinstance(due_at_value, str):
            goal.due_at = datetime.fromisoformat(due_at_value) if due_at_value else None
        elif isinstance(due_at_value, datetime):
            goal.due_at = due_at_value
        else:
            goal.due_at = None

        created_at_value = data.get('created_at')
        if isinstance(created_at_value, str):
            goal.created_at = datetime.fromisoformat(created_at_value) if created_at_value else None
        elif isinstance(created_at_value, datetime):
            goal.created_at = created_at_value

        deleted_at_value = data.get('deleted_at')
        if isinstance(deleted_at_value, str):
            goal.deleted_at = datetime.fromisoformat(deleted_at_value) if deleted_at_value else None
        elif isinstance(deleted_at_value, datetime):
            goal.deleted_at = deleted_at_value
        
        return goal

class UserActivityPage:
    """Paginated user activities"""
    
    def __init__(self, activities: List[Activity], page: int, page_count: int):
        self.activities = activities
        self.page = page
        self.page_count = page_count

class LeaderboardEntry:
    """Leaderboard entry for a user"""
    
    def __init__(
        self,
        user_id: str,
        total_duration: int,
        activity_count: int = 0,
        total_pages: float = 0,
        total_characters: float = 0
    ):
        self.user_id = user_id
        self.total_duration = int(total_duration or 0)  # in seconds
        self.activity_count = activity_count
        self.total_pages = float(total_pages or 0)
        self.total_characters = int(total_characters or 0)
    
    @property
    def total_duration_str(self) -> str:
        """Format total duration as human-readable string"""
        return self._format_duration(self.total_duration)
    
    def metric_value(self, metric: str) -> float:
        """Return raw aggregate for the requested metric."""
        if metric == "pages":
            return self.total_pages
        if metric == "characters":
            return self.total_characters
        return self.total_duration

    def metric_display(self, metric: str) -> str:
        """Generate friendly display text for the metric column."""
        if metric == "pages":
            # Format pages with up to 2 decimal places, removing trailing zeros
            formatted_pages = f"{self.total_pages:,.2f}".rstrip('0').rstrip('.')
            return f"{formatted_pages} pages"
        if metric == "characters":
            return f"{self.total_characters:,} characters"
        return self.total_duration_str
    
    def _format_duration(self, seconds: int) -> str:
        """Format duration as human-readable string"""
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

def parse_duration_complex(duration_str: str, upper_duration: int = 0, 
                         variables: Dict[str, int] = None) -> int:
    """
    Parse complex duration expressions like "duration1:duration2" or ":duration2"
    Based on the Go implementation's parseDurationComplex function
    """
    variables = variables or {}
    
    def parse_duration_or_var(s: str) -> int:
        """Parse duration string or variable"""
        if not s:
            raise ValueError("Expected duration string to not be empty")
        
        # Check if it's a variable
        if s in variables:
            return variables[s]
        
        # Check if it's a duration string
        if s[0] in '0123456789-+.':
            return parse_duration_string(s)
        else:
            raise ValueError(f"Unknown duration variable: {s}")
    
    parts = duration_str.split(':')
    
    # Single duration
    if len(parts) == 1:
        return parse_duration_or_var(parts[0])
    
    if len(parts) != 2:
        raise ValueError("Invalid duration format")
    
    # Handle ":"
    if parts[0] == "" and parts[1] == "":
        return upper_duration
    
    # Handle ":duration2"
    if parts[0] == "":
        d = parse_duration_or_var(parts[1])
        if d < 0:
            d = upper_duration - abs(d)
        return d
    
    # Handle "duration1:"
    if parts[1] == "":
        d1 = parse_duration_or_var(parts[0])
        return upper_duration - d1
    
    # Handle "duration1:duration2"
    d1 = parse_duration_or_var(parts[0])
    d2 = parse_duration_or_var(parts[1])
    
    if d2 < 0:
        d2 = upper_duration - abs(d2)
    
    return d2 - d1

def parse_duration_string(duration_str: str) -> int:
    """
    Parse duration string like "5m30s" or "1h30m" into seconds
    Simple implementation for common formats
    """
    if not duration_str:
        return 0
    
    # Handle simple numeric input (assume minutes)
    if duration_str.isdigit():
        return int(duration_str) * 60
    
    # Parse duration with units
    total_seconds = 0
    current_number = ""
    
    for char in duration_str.lower():
        if char.isdigit() or char == '.':
            current_number += char
        elif char in 'hms':
            if current_number:
                value = float(current_number)
                if char == 'h':
                    total_seconds += int(value * 3600)
                elif char == 'm':
                    total_seconds += int(value * 60)
                elif char == 's':
                    total_seconds += int(value)
                current_number = ""
    
    return total_seconds

def is_autocompleted_entry(input_str: str) -> bool:
    """Check if input is an autocompleted entry from media search"""
    return input_str.startswith("${") and input_str.endswith("}")

def validate_vndb_id(vndb_id: str) -> bool:
    """Validate VNDB ID format"""
    return bool(re.match(r'^v\d+$', vndb_id))

def truncate_string(s: str, max_length: int) -> str:
    """Truncate string to max length with ellipsis"""
    if len(s) <= max_length:
        return s
    return s[:max_length - 3] + "..."