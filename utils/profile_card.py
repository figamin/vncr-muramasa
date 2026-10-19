"""
Profile card image generator for immersion profiles
"""
import io
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import aiohttp

logger = logging.getLogger(__name__)

# Cap decoded pixels well above any real avatar so an oversized image cannot exhaust memory.
Image.MAX_IMAGE_PIXELS = 64_000_000

# Reject oversized downloads before decoding (avatars are tiny).
_MAX_IMAGE_BYTES = 16 * 1024 * 1024


class ProfileCardGenerator:
    """Generate image-based profile cards"""
    
    # Card dimensions
    CARD_WIDTH = 700
    CARD_HEIGHT = 450
    
    # Colors (RPG-inspired theme)
    BG_COLOR = (30, 30, 35)  # Darker background
    CARD_BG = (45, 45, 52)  # Card background
    CARD_BORDER = (120, 120, 140)  # Border color
    TEXT_PRIMARY = (255, 255, 255)  # White
    TEXT_SECONDARY = (180, 180, 190)  # Light gray
    TEXT_TERTIARY = (140, 140, 150)  # Dimmer gray
    ACCENT_COLOR = (255, 215, 0)  # Gold
    ACCENT_SECONDARY = (88, 101, 242)  # Blurple
    PROGRESS_BG = (25, 25, 30)  # Very dark gray
    PROGRESS_FILL = (255, 215, 0)  # Gold
    STAT_LABEL_COLOR = (200, 180, 140)  # Muted gold

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def _get_avatar(self, avatar_url: str) -> Optional[Image.Image]:
        """Download and process user avatar"""
        try:
            if not self.session:
                self.session = aiohttp.ClientSession()

            async with self.session.get(
                avatar_url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    declared = resp.headers.get("Content-Length")
                    if declared and int(declared) > _MAX_IMAGE_BYTES:
                        logger.warning("avatar too large (%s bytes), skipping", declared)
                        return None
                    # Read the full body in chunks, aborting past the cap. (content.read(n)
                    # returns only the first buffered chunk, which truncates the image.)
                    avatar_data = b""
                    async for chunk in resp.content.iter_chunked(65536):
                        avatar_data += chunk
                        if len(avatar_data) > _MAX_IMAGE_BYTES:
                            logger.warning("avatar exceeded %d byte cap, skipping", _MAX_IMAGE_BYTES)
                            return None
                    avatar = Image.open(io.BytesIO(avatar_data))
                    # Resize to 120x120 and make circular
                    avatar = avatar.resize((120, 120), Image.Resampling.LANCZOS)
                    return self._make_circular(avatar)
        except Exception as e:
            logger.error(f"Error loading avatar: {e}")
        return None
    
    def _make_circular(self, image: Image.Image) -> Image.Image:
        """Make an image circular with transparency"""
        # Create a mask
        mask = Image.new('L', image.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0, image.size[0], image.size[1]), fill=255)
        
        # Apply mask
        result = Image.new('RGBA', image.size, (0, 0, 0, 0))
        result.paste(image, (0, 0))
        result.putalpha(mask)
        return result
    
    def _draw_rounded_rectangle(self, draw: ImageDraw.Draw, coords, radius: int, fill, outline=None, width=1):
        """Draw a rounded rectangle with optional outline"""
        x1, y1, x2, y2 = coords
        
        # Fill
        if fill:
            draw.rectangle([x1 + radius, y1, x2 - radius, y2], fill=fill)
            draw.rectangle([x1, y1 + radius, x2, y2 - radius], fill=fill)
            draw.pieslice([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=fill)
            draw.pieslice([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=fill)
            draw.pieslice([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=fill)
            draw.pieslice([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=fill)
        
        # Outline
        if outline:
            # Top and bottom lines
            draw.line([x1 + radius, y1, x2 - radius, y1], fill=outline, width=width)
            draw.line([x1 + radius, y2, x2 - radius, y2], fill=outline, width=width)
            # Left and right lines
            draw.line([x1, y1 + radius, x1, y2 - radius], fill=outline, width=width)
            draw.line([x2, y1 + radius, x2, y2 - radius], fill=outline, width=width)
            # Corner arcs
            draw.arc([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=outline, width=width)
            draw.arc([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=outline, width=width)
            draw.arc([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=outline, width=width)
            draw.arc([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=outline, width=width)
    
    def _draw_progress_bar(self, draw: ImageDraw.Draw, x: int, y: int, width: int, height: int, 
                          progress: float, bg_color, fill_color, border_color=None):
        """Draw a progress bar with optional border"""
        # Background
        draw.rectangle([x, y, x + width, y + height], fill=bg_color)
        
        # Border
        if border_color:
            draw.rectangle([x, y, x + width, y + height], outline=border_color, width=1)
        
        # Fill
        fill_width = int((width - 2) * min(progress, 1.0))
        if fill_width > 0:
            draw.rectangle([x + 1, y + 1, x + 1 + fill_width, y + height - 1], fill=fill_color)
    
    async def generate_profile_card(
        self,
        username: str,
        user_id: str,
        avatar_url: Optional[str],
        quiz_level: int,
        current_class: str,
        vn_hours: float,
        total_hours: float,
        total_logs: int,
        reading_hours: float,
        listening_hours: float,
        avg_minutes: float,
        current_threshold: float,
        next_threshold: Optional[float],
        next_class: Optional[str],
        member_duration: Optional[str] = None,
        is_blacklisted: bool = False,
        this_month_completed: int = 0,
        total_completed: int = 0,
        class_role_color: Optional[Tuple[int, int, int]] = None,
        total_characters: int = 0
    ) -> io.BytesIO:
        """
        Generate a profile card image
        
        Args:
            class_role_color: Optional RGB tuple for the user's class role color
        
        Returns:
            BytesIO object containing the PNG image
        """
        # Use class role color if provided, otherwise default gold
        accent_color = class_role_color if class_role_color else self.ACCENT_COLOR
        
        # Create base image
        img = Image.new('RGB', (self.CARD_WIDTH, self.CARD_HEIGHT), self.BG_COLOR)
        draw = ImageDraw.Draw(img)
        
        # Draw card background with border (RPG card style)
        self._draw_rounded_rectangle(
            draw,
            (15, 15, self.CARD_WIDTH - 15, self.CARD_HEIGHT - 15),
            15,
            self.CARD_BG,
            outline=self.CARD_BORDER,
            width=2
        )
        
        # Draw inner decorative border with accent color
        self._draw_rounded_rectangle(
            draw,
            (20, 20, self.CARD_WIDTH - 20, self.CARD_HEIGHT - 20),
            12,
            None,
            outline=accent_color,
            width=1
        )
        
        # Get and draw avatar with accent color border
        if avatar_url:
            avatar = await self._get_avatar(avatar_url)
            if avatar:
                # Create accent color border for avatar
                border_size = 4
                avatar_with_border = Image.new('RGBA', (128, 128), (0, 0, 0, 0))
                # Draw accent color circle
                border_draw = ImageDraw.Draw(avatar_with_border)
                border_draw.ellipse([0, 0, 128, 128], fill=accent_color)
                # Paste avatar on top
                avatar_with_border.paste(avatar, (border_size, border_size), avatar)
                # Position avatar in top-left area
                img.paste(avatar_with_border, (35, 35), avatar_with_border)
        
        # Try to load fonts with Japanese support, fall back to default if not available
        try:
            import os
            from PIL import ImageFont
            import matplotlib.font_manager as fm
            
            # Try to find Noto Sans JP using matplotlib font manager
            noto_font_path = None
            try:
                # First, try to find Regular weight specifically
                for font in fm.fontManager.ttflist:
                    if font.name == 'Noto Sans JP' and 'Regular' in font.fname:
                        noto_font_path = font.fname
                        logger.debug(f"Found Noto Sans JP Regular at: {noto_font_path}")
                        break
                
                # If no Regular found, try any non-variable, non-bold weight
                if not noto_font_path:
                    for font in fm.fontManager.ttflist:
                        # Skip variable fonts and heavy weights
                        if any(x in font.fname for x in ['Variable', 'variable', 'Black', 'Bold', 'Heavy', 'ExtraBold']):
                            continue
                        if font.name == 'Noto Sans JP':
                            noto_font_path = font.fname
                            logger.debug(f"Found Noto Sans JP at: {noto_font_path}")
                            break
            except Exception as e:
                logger.debug(f"Could not search fonts: {e}")
            
            # Prioritize Noto fonts for consistency across platforms
            fonts_to_try = []
            
            # Add the discovered Noto font path first
            if noto_font_path:
                fonts_to_try.append(noto_font_path)
            
            # Linux font paths
            fonts_to_try.extend([
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            ])
            
            # For Windows, check common Noto font locations
            if os.name == 'nt':  # Windows
                windows_fonts_dir = os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts')
                noto_paths = [
                    os.path.join(windows_fonts_dir, 'NotoSansCJK-Regular.ttc'),
                    os.path.join(windows_fonts_dir, 'NotoSansJP-Regular.otf'),
                    os.path.join(windows_fonts_dir, 'NotoSansJP-Regular.ttf'),
                ]
                fonts_to_try.extend(noto_paths)
            
            # Only add Windows/Mac fonts as last resort
            fonts_to_try.extend([
                # Windows fonts (fallback)
                "msgothic.ttc",
                "meiryo.ttc",
                "yugothic.ttf",
                "C:\\Windows\\Fonts\\msgothic.ttc",
                "C:\\Windows\\Fonts\\meiryo.ttc",
                # Generic fallbacks
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "DejaVuSans.ttf",
                "arial.ttf",
                # Mac fonts
                "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
                "Hiragino Sans GB.ttc"
            ])
            
            font_large = None
            for font_name in fonts_to_try:
                try:
                    font_large = ImageFont.truetype(font_name, 24)
                    font_medium = ImageFont.truetype(font_name, 18)
                    font_small = ImageFont.truetype(font_name, 14)
                    font_tiny = ImageFont.truetype(font_name, 12)
                    logger.debug(f"Successfully loaded font: {font_name}")
                    break
                except:
                    continue
            
            if not font_large:
                raise Exception("No suitable fonts found")
        except:
            # Fall back to default font
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()
            font_small = ImageFont.load_default()
            font_tiny = ImageFont.load_default()
        
        # Username and ID (to the right of avatar)
        text_x = 185
        draw.text((text_x, 40), username, fill=accent_color, font=font_large)
        draw.text((text_x, 70), f"ID: {user_id}", fill=self.TEXT_TERTIARY, font=font_tiny)
        
        # Level and Class with brackets for RPG feel
        level_text = f"[ Lv.{quiz_level} ] {current_class}"
        if is_blacklisted:
            level_text += " [BLACKLISTED]"
        draw.text((text_x, 95), level_text, fill=self.TEXT_PRIMARY, font=font_medium)
        
        # Member duration (if provided)
        if member_duration:
            draw.text((text_x, 120), f"» Member for {member_duration}", 
                     fill=self.TEXT_SECONDARY, font=font_tiny)
        
        # Decorative divider line (double line for RPG aesthetic)
        divider_y = 175
        draw.line([(35, divider_y), (self.CARD_WIDTH - 35, divider_y)], fill=accent_color, width=2)
        draw.line([(35, divider_y + 3), (self.CARD_WIDTH - 35, divider_y + 3)], fill=self.CARD_BORDER, width=1)
        
        # Stats section
        stats_y = 195
        
        # Left column - Main stats with RPG-style formatting
        # Header with decorative brackets
        draw.text((35, stats_y), "═══ IMMERSION STATS ═══", fill=self.STAT_LABEL_COLOR, font=font_medium)
        stats_y += 35
        
        # Stat lines with » bullet points
        draw.text((35, stats_y), f"» VN Time:", fill=self.TEXT_SECONDARY, font=font_small)
        draw.text((150, stats_y), f"{vn_hours:.1f}h", fill=self.TEXT_PRIMARY, font=font_small)
        stats_y += 28
        
        reading_pct = (reading_hours/total_hours*100) if total_hours > 0 else 0
        listening_pct = (listening_hours/total_hours*100) if total_hours > 0 else 0
        
        draw.text((35, stats_y), f"» Reading:", fill=self.TEXT_SECONDARY, font=font_small)
        draw.text((150, stats_y), f"{reading_hours:.1f}h ({reading_pct:.0f}%)", fill=self.TEXT_PRIMARY, font=font_small)
        stats_y += 28
        
        draw.text((35, stats_y), f"» Listening:", fill=self.TEXT_SECONDARY, font=font_small)
        draw.text((150, stats_y), f"{listening_hours:.1f}h ({listening_pct:.0f}%)", fill=self.TEXT_PRIMARY, font=font_small)
        stats_y += 28
        
        # Characters if user has any logged
        if total_characters > 0:
            # Format character count (e.g., 1.2M, 450K, 1,234)
            if total_characters >= 1_000_000:
                char_display = f"{total_characters / 1_000_000:.1f}M"
            elif total_characters >= 1_000:
                char_display = f"{total_characters / 1_000:.0f}K"
            else:
                char_display = f"{total_characters:,}"
            
            draw.text((35, stats_y), f"» Characters:", fill=self.TEXT_SECONDARY, font=font_small)
            draw.text((150, stats_y), char_display, fill=self.TEXT_PRIMARY, font=font_small)
            stats_y += 28
        
        # Total with different style
        draw.line([(35, stats_y), (300, stats_y)], fill=self.CARD_BORDER, width=1)
        stats_y += 8
        draw.text((35, stats_y), f"» Total:", fill=accent_color, font=font_small)
        draw.text((150, stats_y), f"{total_hours:.1f}h", fill=accent_color, font=font_small)
        draw.text((235, stats_y), f"({total_logs:,} logs)", fill=self.TEXT_TERTIARY, font=font_tiny)
        
        # Right column - Progress with RPG styling
        progress_x = 360
        progress_y = 195
        
        draw.text((progress_x, progress_y), "═══ CLASS PROGRESS ═══", 
                 fill=self.STAT_LABEL_COLOR, font=font_medium)
        progress_y += 35
        
        draw.text((progress_x, progress_y), f"» Current:", fill=self.TEXT_SECONDARY, font=font_small)
        draw.text((progress_x + 90, progress_y), f"{current_class} ({current_threshold:.0f}h)", fill=self.TEXT_PRIMARY, font=font_small)
        progress_y += 28
        
        if next_class and next_threshold:
            draw.text((progress_x, progress_y), f"» Next:", fill=self.TEXT_SECONDARY, font=font_small)
            # Show "first log" for 鬼 class instead of (0h)
            if next_class == "鬼" and next_threshold < 0.001:  # Essentially 0 hours
                next_display = f"{next_class} (first log)"
            else:
                next_display = f"{next_class} ({next_threshold:.0f}h)"
            draw.text((progress_x + 90, progress_y), next_display, 
                     fill=self.TEXT_PRIMARY, font=font_small)
            progress_y += 35
            
            # Progress bar with RPG styling
            progress_in_tier = vn_hours - current_threshold
            tier_width = next_threshold - current_threshold
            progress_ratio = min(progress_in_tier / tier_width, 1.0) if tier_width > 0 else 0
            
            self._draw_progress_bar(
                draw, progress_x, progress_y, 305, 22,
                progress_ratio, self.PROGRESS_BG, accent_color, self.CARD_BORDER
            )
            
            # Progress percentage with HP bar style text
            progress_y += 28
            progress_text = f"{progress_ratio*100:.0f}%  |  {progress_in_tier:.1f} / {tier_width:.0f}h"
            draw.text((progress_x, progress_y), progress_text, 
                     fill=self.TEXT_SECONDARY, font=font_tiny)
        
        # Completions (bottom section - no box)
        if total_completed > 0:
            completion_y = self.CARD_HEIGHT - 75
            # Draw completion text without box
            draw.text((370, completion_y), "COMPLETIONS", 
                     fill=self.STAT_LABEL_COLOR, font=font_tiny)
            draw.text((370, completion_y + 15), 
                     f"{total_completed} total  •  {this_month_completed} this month", 
                     fill=self.TEXT_SECONDARY, font=font_tiny)
        
        # Convert to BytesIO
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        
        return buffer
    
    async def close(self):
        """Close the aiohttp session"""
        if self.session:
            await self.session.close()
