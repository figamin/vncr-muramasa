"""
Data export/import utilities for immersion tracking
"""

import json
import gzip
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from io import BytesIO, StringIO
import tempfile

logger = logging.getLogger(__name__)

class ExportManager:
    """Manages data export functionality with rate limiting"""
    
    def __init__(self):
        # In-memory rate limiting (10 minutes)
        self._export_history = {}
        self._rate_limit_minutes = 10
    
    def _can_export(self, user_id: str) -> bool:
        """Check if user can export (rate limiting)"""
        now = datetime.now()
        last_export = self._export_history.get(user_id)
        
        if not last_export:
            return True
        
        time_diff = now - last_export
        return time_diff.total_seconds() >= (self._rate_limit_minutes * 60)
    
    def _record_export(self, user_id: str):
        """Record export timestamp for rate limiting"""
        self._export_history[user_id] = datetime.now()
    
    def _clean_old_records(self):
        """Clean up old export records"""
        cutoff = datetime.now() - timedelta(minutes=self._rate_limit_minutes + 1)
        self._export_history = {
            k: v for k, v in self._export_history.items() 
            if v > cutoff
        }
    
    async def export_activities_json(self, activities: List[Dict[str, Any]],
                                   user_id: str, compressed: bool = True,
                                   enforce_rate_limit: bool = True) -> BytesIO:
        """
        Export activities to JSONL format (optionally compressed)

        Args:
            activities: List of activity dictionaries
            user_id: User ID for rate limiting
            compressed: Whether to compress with gzip
            enforce_rate_limit: Off for admin-initiated exports, which must not
                consume the subject's own cooldown budget

        Returns:
            BytesIO containing the exported data
        """
        if enforce_rate_limit:
            if not self._can_export(user_id):
                last_export = self._export_history.get(user_id)
                next_allowed = last_export + timedelta(minutes=self._rate_limit_minutes)
                raise ValueError(f"Export rate limited. Next export allowed at {next_allowed.strftime('%Y-%m-%d %H:%M:%S')}")

            self._record_export(user_id)
            self._clean_old_records()

        # Prepare data for export
        export_data = []
        for activity in activities:
            # Create export-friendly format
            export_activity = {
                "id": activity.get("id"),
                "user_id": activity.get("user_id"),
                "guild_id": activity.get("guild_id"),
                "name": activity.get("name"),
                "primary_type": activity.get("primary_type"),
                "media_type": activity.get("media_type"),
                "duration": activity.get("duration"),
                "date": activity.get("date").isoformat() if isinstance(activity.get("date"), datetime) else activity.get("date"),
                "created_at": activity.get("created_at"),
                "meta": activity.get("meta", {})
            }
            export_data.append(export_activity)
        
        # Create JSONL content
        buffer = BytesIO()
        
        if compressed:
            # Compressed export - use BytesIO directly
            with gzip.GzipFile(fileobj=buffer, mode='wb') as gzf:
                for activity in export_data:
                    line = json.dumps(activity, ensure_ascii=False) + '\n'
                    gzf.write(line.encode('utf-8'))
        else:
            # Uncompressed export
            text_buffer = StringIO()
            for activity in export_data:
                json.dump(activity, text_buffer, ensure_ascii=False)
                text_buffer.write('\n')
            
            # Convert to bytes
            content = text_buffer.getvalue().encode('utf-8')
            buffer.write(content)
        
        buffer.seek(0)
        return buffer
    
    def get_export_filename(self, user_id: str, compressed: bool = True, format: str = "jsonl") -> str:
        """Generate filename for export"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if format == "text":
            return f"immersion_log_{user_id}_{timestamp}.txt"
        extension = "jsonl.gz" if compressed else "jsonl"
        return f"immersion_activities_{user_id}_{timestamp}.{extension}"

    def _format_duration_human(self, seconds: int) -> str:
        """Format duration in human-readable format (e.g., '2h 30m')"""
        if seconds < 60:
            return f"{seconds}s"
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if hours > 0:
            if minutes > 0:
                return f"{hours}h {minutes}m"
            return f"{hours}h"
        return f"{minutes}m"

    async def export_activities_text(self, activities: List[Dict[str, Any]],
                                     user_id: str,
                                     enforce_rate_limit: bool = True) -> BytesIO:
        """
        Export activities to human-readable text format

        Args:
            activities: List of activity dictionaries
            user_id: User ID for rate limiting
            enforce_rate_limit: Off for admin-initiated exports, which must not
                consume the subject's own cooldown budget

        Returns:
            BytesIO containing the text export
        """
        if enforce_rate_limit:
            if not self._can_export(user_id):
                last_export = self._export_history.get(user_id)
                next_allowed = last_export + timedelta(minutes=self._rate_limit_minutes)
                raise ValueError(f"Export rate limited. Next export allowed at {next_allowed.strftime('%Y-%m-%d %H:%M:%S')}")

            self._record_export(user_id)
            self._clean_old_records()

        lines = []
        line_width = 65

        # Header
        lines.append("═" * line_width)
        lines.append("IMMERSION LOG EXPORT".center(line_width))
        lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}".center(line_width))
        lines.append(f"User ID: {user_id}".center(line_width))
        lines.append("═" * line_width)
        lines.append("")

        # Calculate summary statistics
        total_duration = sum(a.get('duration', 0) for a in activities)
        dates = []
        for a in activities:
            date_val = a.get('date')
            if isinstance(date_val, datetime):
                dates.append(date_val)
            elif isinstance(date_val, str):
                try:
                    dates.append(datetime.fromisoformat(date_val.replace('Z', '+00:00')))
                except:
                    pass

        # Group by media type
        media_groups = {}
        for activity in activities:
            media_type = activity.get('media_type') or 'manual'
            if media_type not in media_groups:
                media_groups[media_type] = []
            media_groups[media_type].append(activity)

        # Summary section
        lines.append("SUMMARY")
        lines.append("─" * line_width)
        lines.append(f"Total Activities: {len(activities)}")
        lines.append(f"Total Time: {self._format_duration_human(total_duration)}")
        if dates:
            lines.append(f"Date Range: {min(dates).strftime('%Y-%m-%d')} to {max(dates).strftime('%Y-%m-%d')}")
        lines.append("")
        lines.append("By Media Type:")

        # Media type display names and emojis
        media_display = {
            'visual_novel': ('📖 Visual Novels', 'Visual Novels'),
            'anime': ('📺 Anime', 'Anime'),
            'manga': ('📚 Manga', 'Manga'),
            'book': ('📕 Books', 'Books'),
            'game': ('🎮 Games', 'Games'),
            'video': ('📹 Videos', 'Videos'),
            'manual': ('📝 Manual Logs', 'Manual Logs')
        }

        # Calculate per-media-type stats
        for media_type in ['visual_novel', 'anime', 'manga', 'book', 'game', 'video', 'manual']:
            if media_type in media_groups:
                group = media_groups[media_type]
                group_duration = sum(a.get('duration', 0) for a in group)
                emoji_name, _ = media_display.get(media_type, ('📄', media_type))
                lines.append(f"  {emoji_name}: {self._format_duration_human(group_duration)} ({len(group)} logs)")

        # Detailed sections for each media type
        for media_type in ['visual_novel', 'anime', 'manga', 'book', 'game', 'video', 'manual']:
            if media_type not in media_groups:
                continue

            group = media_groups[media_type]
            _, section_title = media_display.get(media_type, ('📄', media_type.upper()))

            lines.append("")
            lines.append("═" * line_width)
            lines.append(section_title.upper().center(line_width))
            lines.append("═" * line_width)

            # Group activities by title within this media type
            titles = {}
            for activity in group:
                name = activity.get('name', 'Unknown')
                if name not in titles:
                    titles[name] = []
                titles[name].append(activity)

            # Sort titles by most recent log date (descending - newest first)
            def get_max_date(activities):
                dates = []
                for a in activities:
                    date_val = a.get('date')
                    if isinstance(date_val, datetime):
                        dates.append(date_val)
                    elif isinstance(date_val, str):
                        try:
                            dates.append(datetime.fromisoformat(date_val.replace('Z', '+00:00')))
                        except:
                            pass
                return max(dates) if dates else datetime.min

            sorted_titles = sorted(
                titles.items(),
                key=lambda x: get_max_date(x[1]),
                reverse=True
            )

            for title_name, title_activities in sorted_titles:
                lines.append("")
                lines.append(f"Title: {title_name}")

                # Calculate title totals
                title_duration = sum(a.get('duration', 0) for a in title_activities)
                title_chars = sum(a.get('meta', {}).get('characters', 0) or 0 for a in title_activities)
                title_pages = sum(a.get('meta', {}).get('pages', 0) or 0 for a in title_activities)
                title_episodes = sum(a.get('meta', {}).get('episodes', 0) or 0 for a in title_activities)

                # Build stats line based on media type
                stats_parts = [f"Total Time: {self._format_duration_human(title_duration)}"]
                if title_chars > 0:
                    stats_parts.append(f"Characters: {title_chars:,}")
                if title_pages > 0:
                    stats_parts.append(f"Pages: {title_pages:,.1f}".rstrip('0').rstrip('.'))
                if title_episodes > 0:
                    stats_parts.append(f"Episodes: {title_episodes}")
                stats_parts.append(f"Logs: {len(title_activities)}")
                lines.append(" | ".join(stats_parts))

                # Get date range for this title
                title_dates = []
                for a in title_activities:
                    date_val = a.get('date')
                    if isinstance(date_val, datetime):
                        title_dates.append(date_val)
                    elif isinstance(date_val, str):
                        try:
                            title_dates.append(datetime.fromisoformat(date_val.replace('Z', '+00:00')))
                        except:
                            pass

                if title_dates:
                    lines.append(f"First: {min(title_dates).strftime('%Y-%m-%d')} | Last: {max(title_dates).strftime('%Y-%m-%d')}")

                lines.append("─" * line_width)

                # Sort sessions chronologically
                sorted_sessions = sorted(
                    title_activities,
                    key=lambda x: x.get('date') if isinstance(x.get('date'), datetime)
                    else datetime.fromisoformat(str(x.get('date', '')).replace('Z', '+00:00')) if x.get('date')
                    else datetime.min
                )

                for activity in sorted_sessions:
                    date_val = activity.get('date')
                    if isinstance(date_val, datetime):
                        date_str = date_val.strftime('%Y-%m-%d')
                    elif isinstance(date_val, str):
                        try:
                            date_str = datetime.fromisoformat(date_val.replace('Z', '+00:00')).strftime('%Y-%m-%d')
                        except:
                            date_str = 'Unknown'
                    else:
                        date_str = 'Unknown'

                    duration = activity.get('duration', 0)
                    duration_str = self._format_duration_human(duration)

                    # Build session details
                    details = []
                    meta = activity.get('meta', {})
                    if meta.get('characters'):
                        details.append(f"{meta['characters']:,} chars")
                    if meta.get('pages'):
                        pages_str = f"{meta['pages']:,.1f}".rstrip('0').rstrip('.')
                        details.append(f"{pages_str} pages")
                    if meta.get('episodes'):
                        details.append(f"{meta['episodes']} eps")

                    if details:
                        lines.append(f"  • {date_str}: {duration_str} ({', '.join(details)})")
                    else:
                        lines.append(f"  • {date_str}: {duration_str}")

        # Footer
        lines.append("")
        lines.append("═" * line_width)
        lines.append("End of Export".center(line_width))
        lines.append("═" * line_width)

        # Create buffer
        content = "\n".join(lines)
        buffer = BytesIO()
        buffer.write(content.encode('utf-8'))
        buffer.seek(0)
        return buffer

    def get_time_until_next_export(self, user_id: str) -> Optional[timedelta]:
        """Get time until user can export again"""
        if self._can_export(user_id):
            return None
        
        last_export = self._export_history.get(user_id)
        if not last_export:
            return None
        
        next_allowed = last_export + timedelta(minutes=self._rate_limit_minutes)
        return next_allowed - datetime.now()


class ImportManager:
    """Manages data import functionality"""
    
    def __init__(self):
        # Upload safety limits
        self._max_upload_bytes = 8 * 1024 * 1024          # 8 MB compressed/uncompressed input limit
        self._max_decompressed_bytes = 16 * 1024 * 1024   # 16 MB after decompression
        self._max_activities_per_file = 5000              # hard cap on activities per import
        self._max_line_bytes = 32 * 1024                  # 32 KB per JSONL line
        self._max_name_length = 256                      # reasonable cap for activity names
        self._max_meta_bytes = 25_000                    # ~25 KB of metadata per activity
        self._max_meta_depth = 5
        self._max_meta_entries = 200
        self._max_meta_list_length = 500
        self._max_meta_string_length = 4000
    
    def _is_botsu_format(self, activity_data: Dict[str, Any]) -> bool:
        """
        Detect if this is a botsu format import vs this bot's own export format
        Botsu format indicators:
        - Very large duration values (nanoseconds, >1 billion)
        - Has 'deleted_at', 'imported_at' fields
        - Different date format patterns
        """
        # Check for botsu-specific fields
        if any(field in activity_data for field in ['deleted_at', 'imported_at']):
            return True
            
        # Check if duration is in nanosecond range (botsu uses nanoseconds)
        duration = activity_data.get('duration')
        if isinstance(duration, (int, float)) and duration > 1000000000:  # > 1 billion = likely nanoseconds
            return True
            
        return False

    def _normalize_botsu_format(self, activity_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize botsu format to the internal format
        Handles field name mapping and format differences
        """
        normalized = activity_data.copy()
        
        # Map botsu field names to internal field names
        field_mapping = {
            # Botsu uses 'type' instead of 'primary_type'
            'type': 'primary_type',
            # Botsu uses 'medium' instead of 'media_type'  
            'medium': 'media_type',
            # Botsu might use 'time' instead of 'date'
            'time': 'date',
            # Botsu might use 'created' instead of 'created_at'
            'created': 'created_at',
            # Botsu uses 'userID' instead of 'user_id'
            'userID': 'user_id',
            # Botsu uses 'guildID' instead of 'guild_id'  
            'guildID': 'guild_id'
        }
        
        # Apply field mapping
        for botsu_field, our_field in field_mapping.items():
            if botsu_field in normalized:
                normalized[our_field] = normalized.pop(botsu_field)
        
        # Convert medium values from botsu format
        if 'media_type' in normalized:
            medium_mapping = {
                'Book': 'book',
                'Manga': 'manga', 
                'Anime': 'anime',
                'Visual Novel': 'visual_novel',
                'VN': 'visual_novel',
                'Video': 'video'
            }
            if normalized['media_type'] in medium_mapping:
                normalized['media_type'] = medium_mapping[normalized['media_type']]
        
        # Convert primary_type values from botsu format  
        if 'primary_type' in normalized:
            type_mapping = {
                'Reading': 'reading',
                'Listening': 'listening'
            }
            if normalized['primary_type'] in type_mapping:
                normalized['primary_type'] = type_mapping[normalized['primary_type']]
        
        # Handle botsu's duration format (stored in nanoseconds)
        if 'duration' in normalized:
            duration = normalized['duration']
            # If duration is a string with time format (e.g., "1h30m"), convert to seconds
            if isinstance(duration, str):
                normalized['duration'] = self._parse_duration_string(duration)
            elif isinstance(duration, (int, float)):
                duration_value = float(duration)

                # Probe multiple unit scales (seconds, ms, µs, ns) and select the first
                # that yields a plausible duration (1s - 24h). Fall back to best guess.
                unit_factors = (1_000_000_000, 1_000_000, 1_000, 1)
                candidates: List[float] = []

                for factor in unit_factors:
                    converted = duration_value / factor
                    if converted >= 1:
                        candidates.append(converted)

                seconds = None
                if candidates:
                    viable = [value for value in candidates if value >= 60]
                    seconds = min(viable) if viable else max(candidates)

                if seconds is None:
                    if duration_value >= 1_000_000_000:
                        seconds = duration_value / 1_000_000_000
                    elif duration_value >= 1_000_000:
                        seconds = duration_value / 1_000_000
                    elif duration_value >= 1_000:
                        seconds = duration_value / 1_000
                    else:
                        seconds = duration_value

                normalized['duration'] = self._coerce_duration(
                    seconds,
                    normalized.get('name')
                )
        
        # Ensure required fields have defaults
        if 'meta' not in normalized:
            normalized['meta'] = {}
        if 'date' not in normalized and 'time' not in activity_data:
            normalized['date'] = datetime.now().isoformat()
        
        return normalized

    def _decompress_gzip_with_limit(self, compressed_bytes: bytes) -> str:
        """Safely decompress gzip content enforcing a maximum size limit."""
        compressed_stream = BytesIO(compressed_bytes)
        with gzip.GzipFile(fileobj=compressed_stream, mode='rb') as gz:
            chunks: List[bytes] = []
            total = 0
            while True:
                chunk = gz.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > self._max_decompressed_bytes:
                    raise ValueError(
                        f"Decompressed content exceeds {self._max_decompressed_bytes // (1024 * 1024)} MB limit"
                    )
                chunks.append(chunk)

        return b"".join(chunks).decode("utf-8")

    def _strip_untrusted_fields(self, activity: Dict[str, Any]) -> None:
        """Remove fields that should never be user controlled."""
        for field in [
            "user_id",
            "guild_id",
            "id",
            "import_id",
            "created_at",
            "updated_at",
            "imported_at",
        ]:
            activity.pop(field, None)

    def _validate_meta_value(self, value: Any, depth: int = 0) -> None:
        """Validate meta recursively so its size and nesting stay within limits."""
        if depth > self._max_meta_depth:
            raise ValueError("Meta structure is too deeply nested")

        if isinstance(value, dict):
            if len(value) > self._max_meta_entries:
                raise ValueError("Meta dictionary has too many entries")
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("Meta keys must be strings")
                if len(key) > self._max_meta_string_length:
                    raise ValueError("Meta key exceeds maximum length")
                self._validate_meta_value(item, depth + 1)
        elif isinstance(value, list):
            if len(value) > self._max_meta_list_length:
                raise ValueError("Meta list has too many items")
            for item in value:
                self._validate_meta_value(item, depth + 1)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and len(value) > self._max_meta_string_length:
                raise ValueError("Meta string value exceeds maximum length")
        else:
            raise ValueError(f"Unsupported meta value type: {type(value).__name__}")

    def _enforce_activity_constraints(self, activity: Dict[str, Any]) -> None:
        """Apply security-focused validation to an activity."""
        name = activity.get("name")
        if not isinstance(name, str):
            raise ValueError("Activity name must be a string")
        name = name.strip()
        if not name:
            raise ValueError("Activity name cannot be empty")
        if len(name) > self._max_name_length:
            raise ValueError(
                f"Activity name exceeds {self._max_name_length} characters"
            )
        activity["name"] = name

        # Validate meta payload
        meta = activity.get("meta", {})
        if meta is None:
            meta = {}
        if not isinstance(meta, dict):
            raise ValueError("Meta field must be a JSON object")

        self._validate_meta_value(meta)
        try:
            meta_size = len(json.dumps(meta))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Meta contains non-serializable values: {exc}")

        if meta_size > self._max_meta_bytes:
            raise ValueError(
                f"Meta section exceeds {self._max_meta_bytes} bytes when serialized"
            )

        activity["meta"] = meta

        # Ensure date is reasonable datetime object
        if isinstance(activity.get("date"), datetime):
            pass
        elif isinstance(activity.get("date"), str):
            try:
                activity["date"] = datetime.fromisoformat(
                    activity["date"].replace("Z", "+00:00")
                )
            except ValueError:
                activity["date"] = datetime.now()
        else:
            activity["date"] = datetime.now()

        # Prevent user-provided IDs from persisting
        self._strip_untrusted_fields(activity)
    
    def _parse_duration_string(self, duration_str: str) -> int:
        """
        Parse duration string (e.g., "1h30m", "90m", "5400s") to seconds
        """
        if isinstance(duration_str, (int, float)):
            return int(duration_str)
        
        duration_str = str(duration_str).lower().strip()
        
        # If it's just a number, assume seconds
        if duration_str.isdigit():
            return int(duration_str)
        
        # Parse time components
        total_seconds = 0
        current_number = ""
        
        for char in duration_str:
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

        return total_seconds if total_seconds > 0 else 0

    def _coerce_duration(self, value: Any, activity_name: Optional[str] = None) -> int:
        """Normalize raw duration input into a bounded integer number of seconds."""

        name = activity_name or "Unknown"

        if isinstance(value, (int, float)):
            seconds = float(value)
        elif isinstance(value, str):
            parsed = self._parse_duration_string(value)
            normalized_input = value.strip().lower()
            if parsed == 0 and normalized_input not in {"0", "0s", "0m", "0h"}:
                raise ValueError("Duration string could not be parsed")
            seconds = float(parsed)
        else:
            raise ValueError(f"Unsupported duration type: {type(value).__name__}")

        if seconds < 0:
            raise ValueError("Duration cannot be negative")

        coerced = int(round(seconds))

        return coerced
    
    async def parse_jsonl_file(self, file_content: bytes, 
                             filename: str = None, is_compressed: bool = None) -> List[Dict[str, Any]]:
        """
        Parse JSONL file content into activity dictionaries
        
        Args:
            file_content: Raw file content
            filename: Filename to help detect compression
            is_compressed: Whether the content is gzip compressed (auto-detect if None)
            
        Returns:
            List of activity dictionaries
        """
        activities = []

        if len(file_content) > self._max_upload_bytes:
            raise ValueError(
                f"Import file is too large (>{self._max_upload_bytes // (1024 * 1024)} MB)."
            )

        try:
            # Auto-detect compression if not specified
            if is_compressed is None:
                # Check file extension
                if filename and filename.endswith('.gz'):
                    is_compressed = True
                # Check magic bytes for gzip
                elif len(file_content) >= 2 and file_content[:2] == b'\x1f\x8b':
                    is_compressed = True
                else:
                    is_compressed = False
            
            if is_compressed:
                content = self._decompress_gzip_with_limit(file_content)
            else:
                if len(file_content) > self._max_decompressed_bytes:
                    raise ValueError(
                        f"Import file exceeds {self._max_decompressed_bytes // (1024 * 1024)} MB limit"
                    )
                content = file_content.decode('utf-8')
            
            # Parse each line as JSON
            stripped_content = content.strip()
            if not stripped_content:
                return activities

            for line_num, line in enumerate(stripped_content.split('\n'), 1):
                if not line.strip():
                    continue

                if line_num > self._max_activities_per_file:
                    raise ValueError(
                        f"Import contains more than {self._max_activities_per_file} activities"
                    )

                if len(line.encode('utf-8')) > self._max_line_bytes:
                    raise ValueError(
                        f"Activity on line {line_num} exceeds permitted size ({self._max_line_bytes} bytes)"
                    )
                
                try:
                    activity_data = json.loads(line)
                    
                    # Convert botsu format to the internal format if needed (detect format first)
                    if self._is_botsu_format(activity_data):
                        activity_data = self._normalize_botsu_format(activity_data)

                    self._enforce_activity_constraints(activity_data)
                    
                    # Validate required fields
                    required_fields = ['name', 'primary_type', 'duration']
                    for field in required_fields:
                        if field not in activity_data:
                            raise ValueError(f"Missing required field: {field}")
                    
                    # Validate activity type
                    if activity_data['primary_type'] not in ['reading', 'listening']:
                        raise ValueError(f"Invalid primary_type: {activity_data['primary_type']}")
                    
                    # Validate duration (allow 0 for bookmarks/entries without time tracking)
                    if not isinstance(activity_data['duration'], (int, float)) or activity_data['duration'] < 0:
                        raise ValueError(f"Invalid duration: {activity_data['duration']}")

                    try:
                        activity_data['duration'] = self._coerce_duration(
                            activity_data.get('duration'),
                            activity_data.get('name')
                        )
                    except ValueError as exc:
                        raise ValueError(f"Invalid duration: {activity_data.get('duration')} ({exc})")
                    
                    # Parse date if string
                    if isinstance(activity_data.get('date'), str):
                        try:
                            activity_data['date'] = datetime.fromisoformat(activity_data['date'].replace('Z', '+00:00'))
                        except ValueError:
                            # Use current time if date parsing fails
                            activity_data['date'] = datetime.now()
                    
                    # Ensure meta is dict
                    if 'meta' not in activity_data:
                        activity_data['meta'] = {}
                    elif not isinstance(activity_data['meta'], dict):
                        activity_data['meta'] = {}
                    
                    activities.append(activity_data)
                    
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON on line {line_num}: {e}")
                except ValueError as e:
                    raise ValueError(f"Invalid activity on line {line_num}: {e}")
        
        except OSError:
            raise ValueError("Failed to decompress gzip content. The file may be corrupt or not a valid gzip archive.")
        except UnicodeDecodeError:
            raise ValueError("Invalid file encoding. Expected UTF-8.")
        except Exception as e:
            logger.error(f"Error parsing import file: {e}")
            raise ValueError(f"Failed to parse import file: {e}")
        
        return activities
    
    def validate_import_activities(self, activities: List[Dict[str, Any]]) -> List[str]:
        """
        Validate imported activities and return list of errors
        
        Args:
            activities: List of activity dictionaries
            
        Returns:
            List of validation error messages
        """
        errors = []
        
        for i, activity in enumerate(activities, 1):
            try:
                # Check required fields
                if not activity.get('name'):
                    errors.append(f"Activity {i}: Missing or empty name")
                
                if activity.get('primary_type') not in ['reading', 'listening']:
                    errors.append(f"Activity {i}: Invalid primary_type '{activity.get('primary_type')}'")
                
                if not isinstance(activity.get('duration'), (int, float)) or activity.get('duration') < 0:
                    errors.append(f"Activity {i}: Invalid duration '{activity.get('duration')}'")
                
                # Check optional fields
                if activity.get('media_type') and activity.get('media_type') not in ['book', 'anime', 'manga', 'video', 'visual_novel']:
                    errors.append(f"Activity {i}: Invalid media_type '{activity.get('media_type')}'")
                
            except Exception as e:
                errors.append(f"Activity {i}: Validation error - {e}")
        
        return errors

    async def parse_import_file(
        self,
        file_content: bytes,
        filename: Optional[str] = None,
        is_compressed: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Backward-compatible wrapper for legacy callers expecting parse_import_file."""

        return await self.parse_jsonl_file(
            file_content=file_content,
            filename=filename,
            is_compressed=is_compressed,
        )

    def _validate_activities_batch(self, activities: List[Dict[str, Any]]) -> List[str]:
        """Legacy helper maintained for older tests; delegates to validate_import_activities."""

        return self.validate_import_activities(activities)


# Global instances
export_manager = ExportManager()
import_manager = ImportManager()