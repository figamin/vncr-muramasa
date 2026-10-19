"""
Helper utilities for the VN bot
"""

import calendar
import math
import re
from datetime import datetime, date
from typing import Any, Optional, Tuple

def format_date(dt: Any) -> str:
    """Format a date for display"""
    if isinstance(dt, str):
        # Try to parse common date formats
        for fmt in ["%Y-%m-%d", "%Y-%m", "%Y"]:
            try:
                dt = datetime.strptime(dt, fmt).date()
                break
            except ValueError:
                continue
        else:
            return dt  # Return original string if parsing fails
    
    if isinstance(dt, datetime):
        dt = dt.date()
    
    if isinstance(dt, date):
        return dt.strftime("%B %Y")
    
    return str(dt)

def validate_month_year(month: int, year: int) -> Tuple[bool, str]:
    """
    Validate month and year values
    
    Args:
        month: Month value (1-12)
        year: Year value
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    current_date = datetime.now()
    current_year = current_date.year
    current_month = current_date.month
    
    # Check month range
    if not (1 <= month <= 12):
        return False, "Month must be between 1 and 12"
    
    # Check year range (reasonable bounds)
    if year < 2020 or year > current_year + 5:
        return False, f"Year must be between 2020 and {current_year + 5}"
    
    # Don't allow dates too far in the past
    if year < current_year - 2:
        return False, "Cannot set VN for more than 2 years in the past"
    
    # Don't allow dates too far in the future
    if year > current_year + 1:
        return False, "Cannot set VN for more than 1 year in the future"
    
    # For current year, don't allow past months (with some flexibility)
    if year == current_year and month < current_month - 1:
        return False, "Cannot set VN for months more than 1 month in the past"
    
    return True, ""

def parse_completion_date(date_str: str) -> tuple[bool, str, str]:
    """Parse and validate a completion date string
    
    Args:
        date_str: Date string in YYYY-MM-DD format
        
    Returns:
        Tuple of (is_valid, formatted_date, error_message)
    """
    if not date_str:
        return True, None, ""
    
    try:
        # Try to parse the date
        parsed_date = datetime.strptime(date_str, "%Y-%m-%d")
        
        # Check if date is not in the future
        today = datetime.now().date()
        if parsed_date.date() > today:
            return False, None, "Completion date cannot be in the future"
        
        # Check if date is not too far in the past (e.g., more than 10 years)
        if parsed_date.year < today.year - 10:
            return False, None, "Completion date cannot be more than 10 years in the past"
        
        # Return formatted date as ISO string
        return True, parsed_date.strftime("%Y-%m-%d %H:%M:%S"), ""
        
    except ValueError:
        return False, None, "Invalid date format. Use YYYY-MM-DD (e.g., 2025-07-30)"

def get_month_name(month: int) -> str:
    """Get month name from number"""
    months = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]
    if 1 <= month <= 12:
        return months[month - 1]
    return f"Month {month}"

def safe_filename(filename: str) -> str:
    """Convert a string to a safe filename"""
    # Remove or replace invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    filename = re.sub(r'\s+', '_', filename)
    filename = filename.strip('._')
    
    # Ensure it's not too long
    if len(filename) > 100:
        filename = filename[:100]
    
    return filename or "unknown"

def truncate_text(text: str, max_length: int = 1000, suffix: str = "...") -> str:
    """Truncate text to a maximum length"""
    if not text or len(text) <= max_length:
        return text
    
    # Try to cut at a sentence boundary
    truncated = text[:max_length - len(suffix)]
    last_sentence = truncated.rfind('.')
    
    if last_sentence > max_length * 0.7:  # If a reasonable sentence break was found
        return truncated[:last_sentence + 1] + suffix
    
    # Otherwise just cut and add suffix
    return truncated + suffix

def convert_bbcode_to_markdown(text: str) -> str:
    """Convert BBCode formatting to Discord Markdown and clean VNDB links"""
    if not text:
        return text
    
    # Convert [url=link]text[/url] to [text](link) - use re.DOTALL for multiline
    text = re.sub(r'\[url=([^\]]+)\](.*?)\[/url\]', r'[\2](\1)', text, flags=re.DOTALL)
    
    # Convert [b]text[/b] to **text**
    text = re.sub(r'\[b\](.*?)\[/b\]', r'**\1**', text, flags=re.DOTALL)
    
    # Convert [i]text[/i] to *text*
    text = re.sub(r'\[i\](.*?)\[/i\]', r'*\1*', text, flags=re.DOTALL)
    
    # Convert [u]text[/u] to __text__ (underline)
    text = re.sub(r'\[u\](.*?)\[/u\]', r'__\1__', text, flags=re.DOTALL)
    
    # Remove common unsupported BBCode tags (but not the Markdown links created above)
    # Be more specific about what to remove to avoid removing Markdown links
    text = re.sub(r'\[(?:color|size|font|quote|code|spoiler|center|left|right)[^\]]*\]', '', text)
    text = re.sub(r'\[/(?:color|size|font|quote|code|spoiler|center|left|right)\]', '', text)
    
    # Convert VNDB internal links to working URLs
    # Convert [text](/cXXXX) to [text](https://vndb.org/cXXXX)
    text = re.sub(r'\[([^\]]+)\]\(/([cvsprt]\d+)\)', r'[\1](https://vndb.org/\2)', text)
    
    return text

def format_vn_length(length_enum) -> str:
    """Format VN length enum to readable text"""
    # Handle None or empty values
    if not length_enum:
        return "Unknown"
    
    # Handle numeric VNDB length values
    numeric_map = {
        1: "Very Short",
        2: "Short", 
        3: "Medium",
        4: "Long",
        5: "Very Long"
    }
    
    # Handle string length values
    string_map = {
        "vshort": "Very Short",
        "short": "Short", 
        "medium": "Medium",
        "long": "Long",
        "vlong": "Very Long"
    }
    
    # If it's a number, convert directly to readable format
    if isinstance(length_enum, (int, float)):
        return numeric_map.get(int(length_enum), f"Unknown ({length_enum})")
    
    # If it's a string, check if it's a numeric string first
    if isinstance(length_enum, str):
        # Try to convert numeric strings to integers
        if length_enum.isdigit():
            try:
                numeric_value = int(length_enum)
                return numeric_map.get(numeric_value, f"Unknown ({length_enum})")
            except ValueError:
                pass
        
        # Handle named string values
        return string_map.get(length_enum.lower(), length_enum.title())
    
    # Fallback for any other type
    return str(length_enum)

def format_rating(rating: float) -> str:
    """Format a rating for display (VNDB uses 1-100 scale)"""
    if rating is None:
        return "No rating"
    # VNDB ratings are already on a 1-100 scale
    return f"{rating:.0f}/100"

def extract_vndb_id(text: str) -> Optional[str]:
    """Extract VNDB ID from various formats"""
    # If it's just a number, add 'v' prefix
    if text.isdigit():
        return f"v{text}"
    
    # Match v12345 format
    match = re.search(r'v(\d+)', text.lower())
    if match:
        return f"v{match.group(1)}"
    
    # Match full VNDB URLs
    match = re.search(r'vndb\.org/v(\d+)', text.lower())
    if match:
        return f"v{match.group(1)}"
    
    return None

from .constants import NSFW_SEXUAL_THRESHOLD

def is_content_nsfw(vn_data: dict) -> bool:
    """
    Check if a VN's cover should be treated as NSFW.

    Trusts VNDB's own image rating (``image.sexual``): VNDB moderators rate
    each cover image directly on a 0/1/2 scale (safe/suggestive/explicit), and
    that rating is the only reliable per-cover signal. Covers at or above
    NSFW_SEXUAL_THRESHOLD are hidden.

    Tag ratings are not used: they reflect whether a *theme* is present in
    the work, not whether the *cover art* depicts it, so a VN can carry highly
    rated explicit-theme tags while having an entirely safe cover.

    Args:
        vn_data: VN data from VNDB API

    Returns:
        True if the cover should be hidden, False otherwise
    """
    image_sexual = vn_data.get('image', {}).get('sexual', 0)
    return image_sexual >= NSFW_SEXUAL_THRESHOLD

def get_safe_image_url(vn_data: dict, hide_nsfw: bool = True) -> Optional[str]:
    """
    Get image URL, filtering out NSFW content if requested
    
    Args:
        vn_data: VN data from VNDB API
        hide_nsfw: Whether to hide NSFW images
        
    Returns:
        Image URL if safe/allowed, None if NSFW and hidden
    """
    image_url = vn_data.get('image', {}).get('url')
    
    if not image_url:
        return None
    
    if hide_nsfw and is_content_nsfw(vn_data):
        return None
    
    return image_url

def get_nsfw_warning_text(vn_data: dict) -> Optional[str]:
    """
    Get NSFW warning text if content is NSFW
    
    Args:
        vn_data: VN data from VNDB API
        
    Returns:
        Warning text if NSFW, None otherwise
    """
    if is_content_nsfw(vn_data):
        return "⚠️ **Content Warning**: This VN contains adult/sexual content"
    return None

def days_in_month(target_year: int, target_month: int) -> int:
    """Total number of days in the given month (handles leap years)."""
    return calendar.monthrange(target_year, target_month)[1]


def chars_per_day(total_chars: int, target_year: int, target_month: int) -> int:
    """
    Characters per day to finish ``total_chars`` over the full target month.

    Always uses the full month length so the banner reads identically whether
    it is posted on day 1, day 5, or shared later; this is meant to be a
    static announcement figure, not a rolling "you must catch up" counter.
    Rounds up.
    """
    if total_chars <= 0:
        return 0
    return math.ceil(total_chars / days_in_month(target_year, target_month))


def calculate_vn_score(length, is_vn_of_month: bool = False, completed_in_target_month: bool = False) -> int:
    """
    Calculate the score for a VN based on length and bonuses
    
    Args:
        length: VN length (1-5 numeric or string like "vshort", "short", etc.)
        is_vn_of_month: Whether this VN was/is VN of the month
        completed_in_target_month: Whether the VN was completed in its target month
        
    Returns:
        Calculated score as integer
    """
    base_score = 100
    
    # Length multipliers
    length_multipliers = {
        1: 1.0,    # Very Short
        2: 1.2,    # Short  
        3: 1.6,    # Medium
        4: 2.0,    # Long
        5: 2.5,    # Very Long
        # String variants
        "vshort": 1.0,
        "short": 1.2,
        "medium": 1.6,
        "long": 2.0,
        "vlong": 2.5
    }
    
    # Get length multiplier
    length_multiplier = 1.0
    if length is not None:
        # Handle numeric strings (like "4")
        if isinstance(length, str) and length.isdigit():
            try:
                length = int(length)
            except ValueError:
                pass
        
        # Look up multiplier
        if isinstance(length, str):
            length_multiplier = length_multipliers.get(length.lower(), 1.0)
        else:
            length_multiplier = length_multipliers.get(length, 1.0)
    
    # Apply length multiplier
    score = base_score * length_multiplier
    
    # Apply bonuses
    if is_vn_of_month:
        score *= 2.0  # VN of the month bonus: 2x
    
    if completed_in_target_month:
        score *= 1.5  # Target month completion bonus: 1.5x
    
    # Return as integer
    return int(round(score))
