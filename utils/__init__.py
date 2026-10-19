"""
Muramasa (VNCRbot) utilities package
"""

from .vndb_client import VNDBClient
from .anilist_client import AniListClient
from .jiten_client import JitenClient, JitenInfo
from .database import Database
from .monthly_banner import MonthlyBannerGenerator
from .helpers import (
    format_date, safe_filename, truncate_text, convert_bbcode_to_markdown, format_rating,
    format_vn_length, extract_vndb_id, validate_month_year, get_month_name,
    is_content_nsfw, get_safe_image_url, get_nsfw_warning_text, parse_completion_date,
    calculate_vn_score, days_in_month, chars_per_day,
)

__all__ = [
    'VNDBClient', 'AniListClient', 'JitenClient', 'JitenInfo', 'Database',
    'MonthlyBannerGenerator', 'format_date', 'safe_filename', 'truncate_text',
    'convert_bbcode_to_markdown', 'format_rating', 'format_vn_length', 'extract_vndb_id',
    'validate_month_year', 'get_month_name', 'is_content_nsfw', 'get_safe_image_url',
    'get_nsfw_warning_text', 'parse_completion_date', 'calculate_vn_score',
    'days_in_month', 'chars_per_day',
]
