"""
Monthly banner image generator for the VN of the Month.

Produces a clean, light-themed 1200x480 PNG combining the VN cover with
reading stats from VNDB and jiten.moe. Designed for posting in announcement
channels: the chars/day figure uses the full month length so the banner is
date-stable.

Layout:
    +------------------------------------------------------------+
    | [cover]   ▍ MONTH YYYY  •  VN OF THE MONTH                 |
    |           Title  · YYYY                                    |
    |           Romaji subtitle                                  |
    |                                                            |
    |           ┌──────────── stats grid (2 col x 3 row) ──────┐ |
    |           │ LENGTH         │ VNDB SCORE                  │ |
    |           │ Medium         │ NN  (N.NK votes)            │ |
    |           │ TOTAL CHARS    │ UNIQUE KANJI                │ |
    |           │ NNN,NNN        │ N,NNN                       │ |
    |           │ DIFFICULTY     │ DIALOGUE                    │ |
    |           │ N.NN / 5       │ NN%                         │ |
    |           └────────────────────────────────────────────────┘
    |           ┌──────── NN,NNN chars/day · NN-day month ────┐ |
    +------------------------------------------------------------+
"""

import functools
import io
import logging
import os
from typing import Optional, Tuple

import aiohttp
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Cap decoded pixels well above any real cover so an oversized image cannot exhaust memory.
Image.MAX_IMAGE_PIXELS = 64_000_000

# Reject oversized downloads before decoding (covers are small).
_MAX_IMAGE_BYTES = 16 * 1024 * 1024


_BUNDLED_FONT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "fonts",
)


@functools.lru_cache(maxsize=64)
def _load_japanese_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """
    Locate a Noto Sans JP / CJK-capable TrueType font and load it at ``size``.

    Cached per ``(size, bold)`` so the discovery cascade (matplotlib font
    scan, disk lookups) only runs once per (size, weight) pair across the
    bot's lifetime, rather than once for every font each banner loads.
    ``bold=True`` prefers a bold weight when available. Falls back to PIL's
    bitmap default font if nothing usable is found.

    Order of preference:

    1. Bundled Noto Sans JP in ``assets/fonts/`` (committed to the repo,
       OFL-licensed). This is preferred so banners render identically on
       Windows dev and on the Linux live server. System fonts under the
       same brand name (NotoSansJP-VF.ttf vs NotoSansCJK-Regular.ttc) have
       visibly different metrics, so without a bundled font the dev preview
       won't match the live output.
    2. Explicit Linux Noto CJK paths (Dockerfile installs ``fonts-noto-cjk``).
       Safety net if the bundled fonts are ever missing on disk.
    3. matplotlib search (Windows / macOS dev fallback).
    4. Windows Fonts directory.
    5. Generic bitmap fallback.
    """
    fonts_to_try: list[str] = []

    # 1. Bundled fonts (most reliable for cross-environment parity).
    bundled = "NotoSansJP-Bold.ttf" if bold else "NotoSansJP-Regular.ttf"
    fonts_to_try.append(os.path.join(_BUNDLED_FONT_DIR, bundled))

    # 2. Explicit Linux Noto CJK paths (system fallback on the live server).
    if bold:
        fonts_to_try.extend([
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        ])
    fonts_to_try.extend([
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ])

    # 3. matplotlib font manager: only used if the explicit paths above
    #    failed (e.g. Windows/macOS dev). Tries Bold-keyword fname (when
    #    bold is requested), then Regular-keyword fname, then any Noto Sans
    #    JP that *isn't* a wrong-weight named file. The third pass catches
    #    variable fonts (e.g. ``NotoSansJP-VF.ttf`` on Windows) which don't
    #    contain "Regular" in the filename but render at the default weight.
    try:
        import matplotlib.font_manager as fm
        bold_keywords = ("Bold", "Heavy", "Black", "ExtraBold")
        mpl_fonts: list[str] = []
        if bold:
            for font in fm.fontManager.ttflist:
                if font.name == "Noto Sans JP" and any(k in font.fname for k in bold_keywords):
                    mpl_fonts.append(font.fname)
                    break
        if not mpl_fonts:
            for font in fm.fontManager.ttflist:
                if font.name == "Noto Sans JP" and "Regular" in font.fname:
                    mpl_fonts.append(font.fname)
                    break
        if not mpl_fonts:
            # Catchall, but when Regular was requested, exclude any file
            # whose name explicitly indicates a heavier weight so bold text
            # is not rendered by accident on systems with many weight
            # variants installed (e.g. fonts-noto-cjk-extra on Linux).
            skip = bold_keywords + ("Medium", "DemiLight", "Light", "Thin") if not bold else ()
            for font in fm.fontManager.ttflist:
                if skip and any(x in font.fname for x in skip):
                    continue
                if font.name == "Noto Sans JP":
                    mpl_fonts.append(font.fname)
                    break
        fonts_to_try.extend(mpl_fonts)
    except Exception as e:
        logger.debug("matplotlib font scan skipped: %s", e)

    if os.name == "nt":
        windows_fonts_dir = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
        if bold:
            fonts_to_try.extend([
                os.path.join(windows_fonts_dir, "NotoSansJP-Bold.otf"),
                os.path.join(windows_fonts_dir, "NotoSansJP-Bold.ttf"),
                os.path.join(windows_fonts_dir, "msgothic.ttc"),
            ])
        fonts_to_try.extend([
            os.path.join(windows_fonts_dir, "NotoSansCJK-Regular.ttc"),
            os.path.join(windows_fonts_dir, "NotoSansJP-Regular.otf"),
            os.path.join(windows_fonts_dir, "NotoSansJP-Regular.ttf"),
            os.path.join(windows_fonts_dir, "msgothic.ttc"),
            os.path.join(windows_fonts_dir, "meiryo.ttc"),
            os.path.join(windows_fonts_dir, "yugothic.ttf"),
        ])

    fonts_to_try.extend([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans.ttf",
        "arial.ttf",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    ])

    for path in fonts_to_try:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue

    logger.warning("No TrueType font found for size %d; using PIL default", size)
    return ImageFont.load_default()


def _format_compact_count(n: int) -> str:
    """1234 -> '1.2K', 12345 -> '12.3K', 1500000 -> '1.5M'."""
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        # 1.0K looks weird; use 1K when there's no fractional part
        v = n / 1_000
        return f"{v:.1f}K" if v < 10 else f"{v:.0f}K"
    return f"{n}"


def _release_year(released: Optional[str]) -> Optional[str]:
    """VNDB ``released`` like '2007-08-10' or '2007' -> '2007'. ``None`` if unparseable."""
    if not released:
        return None
    head = released.strip().split("-", 1)[0]
    return head if head.isdigit() and len(head) == 4 else None


class MonthlyBannerGenerator:
    """Render a 1200x480 monthly book-club banner.

    All layout constants below are stated in *logical* pixels (the design
    grid). The image is actually rendered at SCALE× those dimensions for
    crisp display on HiDPI screens; every coord and font size is multiplied
    by ``SCALE`` at draw time.
    """

    SCALE = 2  # render at 2x; bump higher only if Discord's 8MB cap allows

    BANNER_WIDTH = 1200 * SCALE
    BANNER_HEIGHT = 480 * SCALE

    COVER_WIDTH = 280 * SCALE
    COVER_HEIGHT = 400 * SCALE
    COVER_X = 40 * SCALE
    COVER_Y = 40 * SCALE
    TEXT_X = 360 * SCALE       # right column starts here
    TEXT_RIGHT = 1160 * SCALE  # end of usable text area

    # Light, paper-cream palette
    BG = (251, 248, 241)
    INK_PRIMARY = (28, 27, 42)
    INK_SECONDARY = (88, 84, 110)
    INK_TERTIARY = (146, 140, 160)
    HAIRLINE = (216, 210, 196)
    PANEL_BG = (243, 238, 224)
    CALLOUT_BG = (247, 242, 230)  # subtle cream tint, lighter than PANEL_BG
    ACCENT = (88, 70, 150)        # deep purple
    ACCENT_INK = (255, 255, 255)  # text on accent
    NSFW_BG = (210, 196, 200)
    PLACEHOLDER_BG = (232, 226, 212)

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "MonthlyBannerGenerator":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ------------------------------------------------------------------
    # cover loading
    # ------------------------------------------------------------------
    async def _fetch_cover(self, cover_url: str) -> Optional[Image.Image]:
        if not self.session:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Muramasa/1.0"},
            )
        try:
            async with self.session.get(cover_url) as resp:
                if resp.status != 200:
                    logger.warning("cover %s -> %d", cover_url, resp.status)
                    return None
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > _MAX_IMAGE_BYTES:
                    logger.warning("cover %s too large (%s bytes)", cover_url, declared)
                    return None
                # Read the full body in chunks, aborting past the cap. (content.read(n)
                # returns only the first buffered chunk, which truncates the image.)
                data = b""
                async for chunk in resp.content.iter_chunked(65536):
                    data += chunk
                    if len(data) > _MAX_IMAGE_BYTES:
                        logger.warning("cover %s exceeded %d byte cap", cover_url, _MAX_IMAGE_BYTES)
                        return None
        except Exception as e:
            logger.warning("cover fetch failed for %s: %s", cover_url, e)
            return None

        try:
            img = Image.open(io.BytesIO(data))
            img.load()
            return img.convert("RGB")
        except Exception as e:
            logger.warning("cover decode failed for %s: %s", cover_url, e)
            return None

    def _fit_cover(self, img: Image.Image) -> Image.Image:
        target_ratio = self.COVER_WIDTH / self.COVER_HEIGHT
        src_ratio = img.width / img.height
        if src_ratio > target_ratio:
            new_h = self.COVER_HEIGHT
            new_w = int(img.width * (new_h / img.height))
        else:
            new_w = self.COVER_WIDTH
            new_h = int(img.height * (new_w / img.width))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = (new_w - self.COVER_WIDTH) // 2
        top = (new_h - self.COVER_HEIGHT) // 2
        return img.crop((left, top, left + self.COVER_WIDTH, top + self.COVER_HEIGHT))

    # ------------------------------------------------------------------
    # primitives
    # ------------------------------------------------------------------
    def _draw_rounded_rect(self, draw: ImageDraw.ImageDraw, coords, radius: int,
                           fill, outline=None, width: int = 1):
        x1, y1, x2, y2 = coords
        if fill:
            draw.rectangle([x1 + radius, y1, x2 - radius, y2], fill=fill)
            draw.rectangle([x1, y1 + radius, x2, y2 - radius], fill=fill)
            draw.pieslice([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=fill)
            draw.pieslice([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=fill)
            draw.pieslice([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=fill)
            draw.pieslice([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=fill)
        if outline:
            draw.line([x1 + radius, y1, x2 - radius, y1], fill=outline, width=width)
            draw.line([x1 + radius, y2, x2 - radius, y2], fill=outline, width=width)
            draw.line([x1, y1 + radius, x1, y2 - radius], fill=outline, width=width)
            draw.line([x2, y1 + radius, x2, y2 - radius], fill=outline, width=width)
            draw.arc([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=outline, width=width)
            draw.arc([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=outline, width=width)
            draw.arc([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=outline, width=width)
            draw.arc([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=outline, width=width)

    def _truncate_to_width(self, draw: ImageDraw.ImageDraw, text: str,
                           font: ImageFont.ImageFont, max_width: int) -> str:
        if not text:
            return ""
        if draw.textlength(text, font=font) <= max_width:
            return text
        ellipsis = "…"
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            candidate = text[:mid].rstrip() + ellipsis
            if draw.textlength(candidate, font=font) <= max_width:
                lo = mid + 1
            else:
                hi = mid
        return text[: max(1, lo - 1)].rstrip() + ellipsis

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def generate(
        self,
        title: str,
        subtitle: Optional[str],
        cover_url: Optional[str],
        cover_is_nsfw: bool,
        month_label: str,
        month_days: int,
        total_chars: Optional[int],
        chars_per_day: Optional[int],
        difficulty: Optional[float],
        length_tier: Optional[str],
        vndb_rating: Optional[float] = None,
        vndb_votecount: Optional[int] = None,
        unique_kanji: Optional[int] = None,
        dialogue_pct: Optional[float] = None,
        release_year: Optional[str] = None,
        top_tags: Optional[list[str]] = None,
    ) -> io.BytesIO:
        S = self.SCALE
        img = Image.new("RGB", (self.BANNER_WIDTH, self.BANNER_HEIGHT), self.BG)
        draw = ImageDraw.Draw(img)

        # outer hairline (anti-aliased via oversample+LANCZOS)
        self._paste_aa_rounded(
            img, (16 * S, 16 * S, self.BANNER_WIDTH - 16 * S, self.BANNER_HEIGHT - 16 * S),
            radius=18 * S, outline=self.HAIRLINE, outline_w=1 * S,
        )

        # ---- cover ----
        cover_box = (self.COVER_X, self.COVER_Y,
                     self.COVER_X + self.COVER_WIDTH, self.COVER_Y + self.COVER_HEIGHT)
        if cover_url and not cover_is_nsfw:
            raw = await self._fetch_cover(cover_url)
            if raw is not None:
                fitted = self._fit_cover(raw)
                # Mask the cover with the same rounded shape as its border so
                # the image's square corners don't poke past the rounded
                # outline. Render the mask at OVERSAMPLE× and downsample with
                # LANCZOS so the cover's clipped corners get anti-aliased
                # transitions, matching the AA outline drawn over them.
                cover_radius = 4 * S
                oversample = 4
                ow, oh = fitted.size[0] * oversample, fitted.size[1] * oversample
                big_mask = Image.new("L", (ow, oh), 0)
                ImageDraw.Draw(big_mask).rounded_rectangle(
                    [(0, 0), (ow - 1, oh - 1)],
                    radius=cover_radius * oversample,
                    fill=255,
                )
                mask = big_mask.resize(fitted.size, Image.Resampling.LANCZOS)
                img.paste(fitted, (self.COVER_X, self.COVER_Y), mask)
            else:
                self._draw_cover_placeholder(draw, cover_box, "Cover unavailable")
        elif cover_is_nsfw:
            self._draw_cover_placeholder(draw, cover_box, "🔞 NSFW", bg=self.NSFW_BG)
        else:
            self._draw_cover_placeholder(draw, cover_box, "No cover")

        # subtle frame around cover area (anti-aliased)
        self._paste_aa_rounded(img, cover_box, radius=4 * S,
                               outline=self.HAIRLINE, outline_w=1 * S)

        # ---- fonts (logical sizes; rendered at SCALE×) ----
        font_eyebrow = _load_japanese_font(20 * S, bold=True)
        font_title = _load_japanese_font(40 * S, bold=True)
        font_year = _load_japanese_font(22 * S)
        font_subtitle = _load_japanese_font(18 * S)
        font_stat_label = _load_japanese_font(13 * S, bold=True)
        font_stat_value = _load_japanese_font(22 * S, bold=True)
        font_callout_big = _load_japanese_font(38 * S, bold=True)
        font_callout_sub = _load_japanese_font(18 * S)

        # ---- header (eyebrow + attribution) ----
        bar_x = self.TEXT_X
        # Top of the bar aligns with the cover's top edge. This keeps the right
        # column's top margin equal to the cover's top margin, and the
        # bottom margins land closer to balanced for both subtitle cases.
        bar_y = self.COVER_Y
        bar_h = 22 * S
        draw.rectangle([bar_x, bar_y, bar_x + 6 * S, bar_y + bar_h], fill=self.ACCENT)
        eyebrow = f"{month_label.upper()}  •  VNCR VN OF THE MONTH"
        # Vertically center the eyebrow text in the bar using anchor='lm'
        # so positioning doesn't depend on font-specific ascender padding.
        bar_center_y = bar_y + bar_h // 2
        draw.text((bar_x + 16 * S, bar_center_y), eyebrow,
                  fill=self.ACCENT, font=font_eyebrow, anchor="lm")

        # Attribution paired with eyebrow on the right edge.
        # Share the eyebrow's *baseline* (not bbox-middle) so both texts
        # read as if on the same line. Computed from the larger font's
        # metrics so it adapts to font changes.
        font_attr = _load_japanese_font(11 * S)
        eb_ascent, eb_descent = font_eyebrow.getmetrics()
        eyebrow_baseline = bar_center_y + (eb_ascent - eb_descent) // 2
        attr_text = "Data: VNDB · jiten.moe"
        draw.text((self.TEXT_RIGHT, eyebrow_baseline),
                  attr_text, fill=self.INK_TERTIARY, font=font_attr, anchor="rs")

        # ---- title (with optional release year inline) ----
        title_y = bar_y + 38 * S
        if release_year:
            year_text = f"  ·  {release_year}"
            year_w = draw.textlength(year_text, font=font_year)
        else:
            year_text = ""
            year_w = 0

        max_title_w = self.TEXT_RIGHT - self.TEXT_X - int(year_w)
        title_drawn = self._truncate_to_width(draw, title, font_title, max_title_w)
        title_ascent, title_descent = font_title.getmetrics()
        title_baseline = title_y + title_ascent
        # Visual middle of the title's font extent, used to vertically
        # center the smaller inline year so it doesn't sit visually below
        # the title's center despite sharing its baseline.
        title_visual_middle = title_baseline - (title_ascent - title_descent) // 2
        draw.text((self.TEXT_X, title_baseline), title_drawn,
                  fill=self.INK_PRIMARY, font=font_title, anchor="ls")
        if year_text:
            title_w = draw.textlength(title_drawn, font=font_title)
            draw.text((self.TEXT_X + title_w, title_visual_middle),
                      year_text, fill=self.INK_TERTIARY, font=font_year, anchor="lm")

        subtitle_y = title_y + 50 * S
        if subtitle and subtitle.strip() and subtitle.strip() != title.strip():
            sub = self._truncate_to_width(draw, subtitle, font_subtitle,
                                          self.TEXT_RIGHT - self.TEXT_X)
            draw.text((self.TEXT_X, subtitle_y), sub,
                      fill=self.INK_SECONDARY, font=font_subtitle)
            after_subtitle_y = subtitle_y + 30 * S
        else:
            after_subtitle_y = subtitle_y + 14 * S

        # ---- tag pills (between title block and stats panel) ----
        if top_tags:
            self._draw_tag_pills(img, draw, after_subtitle_y, top_tags)
            stats_top = after_subtitle_y + (24 + 12) * S  # pill_h + gap below
        else:
            stats_top = after_subtitle_y + 4 * S

        # ---- stats grid (2 col x 3 row) ----
        # 178 = 18 top pad + 142 content (3 rows) + 18 bottom pad, symmetric.
        stats_panel_h = 178 * S
        panel = (self.TEXT_X, stats_top, self.TEXT_RIGHT, stats_top + stats_panel_h)
        self._draw_rounded_rect(draw, panel, radius=12 * S,
                                fill=self.PANEL_BG, outline=None)

        # column layout: row_h must clear (label_height + label_value_gap + value_height)
        # plus a few px of breathing room, otherwise the value text crowds the next label.
        col_gap = 24 * S
        pad_x = 24 * S
        # Top pad is smaller than the implied bottom pad (panel_h - top_pad -
        # content) to compensate for the visual asymmetry between the small
        # LABEL row at the top (which has ~3px of ascender padding above the
        # cap line) and the larger VALUE row at the bottom (whose digits have
        # no descenders and bottom out at the baseline).
        pad_y = 14 * S
        row_h = 50 * S
        col_w = (panel[2] - panel[0] - pad_x * 2 - col_gap) // 2
        col1_x = panel[0] + pad_x
        col2_x = col1_x + col_w + col_gap

        if total_chars is None:
            notice = "📊 Reading stats unavailable on jiten.moe"
            tw = draw.textlength(notice, font=font_stat_value)
            nx = panel[0] + (panel[2] - panel[0] - tw) / 2
            ny = panel[1] + (panel[3] - panel[1] - 22 * S) / 2
            draw.text((nx, ny), notice, fill=self.INK_SECONDARY, font=font_stat_value)
        else:
            left_rows = [
                ("LENGTH", length_tier or "Unknown"),
                ("TOTAL CHARACTERS", f"{total_chars:,}"),
                ("DIFFICULTY",
                 f"{difficulty:.2f} / 5" if difficulty and difficulty > 0 else "—"),
            ]
            right_rows = [
                ("VNDB SCORE", self._format_vndb_score(vndb_rating, vndb_votecount)),
                ("UNIQUE KANJI", f"{unique_kanji:,}" if unique_kanji else "—"),
                ("DIALOGUE",
                 f"{dialogue_pct:.0f}%" if dialogue_pct and dialogue_pct > 0 else "—"),
            ]
            self._draw_stat_column(draw, col1_x, panel[1] + pad_y, row_h,
                                   left_rows, font_stat_label, font_stat_value)
            self._draw_stat_column(draw, col2_x, panel[1] + pad_y, row_h,
                                   right_rows, font_stat_label, font_stat_value)

        # ---- callout (quote-style: cream fill, accent bar on left) ----
        callout_top = stats_top + stats_panel_h + 14 * S
        callout_h = 70 * S
        callout = (self.TEXT_X, callout_top, self.TEXT_RIGHT, callout_top + callout_h)
        self._draw_rounded_rect(draw, callout, radius=10 * S,
                                fill=self.CALLOUT_BG, outline=None)

        # Left accent bar
        bar_w = 8 * S
        draw.rectangle(
            [callout[0], callout[1], callout[0] + bar_w, callout[3]],
            fill=self.ACCENT,
        )

        if chars_per_day is not None and chars_per_day > 0:
            big_text = f"{chars_per_day:,}"
            unit_text = "chars/day"
            tail_text = f"·  {month_days}-day month"
        else:
            big_text = "Read freely"
            unit_text = ""
            tail_text = f"·  {month_days}-day month"

        gap_after_big = 12 * S    # px between big number and unit text
        gap_before_tail = 14 * S  # px between unit text and the tail

        # Left-aligned after the accent bar, matching the rest of the right
        # column (title, tags, stat labels are all left-aligned) and keeping
        # the visual rhythm consistent.
        big_w = draw.textlength(big_text, font=font_callout_big)
        unit_w = draw.textlength(unit_text, font=font_callout_sub) if unit_text else 0
        text_x = callout[0] + bar_w + 24 * S

        # Big number vertically centered in the callout. Smaller inline
        # pieces share its baseline (sit lower than visual center); that
        # reads as "trailing detail" against the focal big number, which
        # feels more anchored than visually-centering the smaller text.
        callout_center_y = callout[1] + callout_h // 2
        big_ascent, big_descent = font_callout_big.getmetrics()
        baseline_y = callout_center_y + (big_ascent - big_descent) // 2
        draw.text((text_x, callout_center_y), big_text,
                  fill=self.ACCENT, font=font_callout_big, anchor="lm")
        unit_x = text_x + big_w + (gap_after_big if unit_text else 0)
        draw.text((unit_x, baseline_y), unit_text,
                  fill=self.INK_PRIMARY, font=font_callout_sub, anchor="ls")
        tail_x = unit_x + unit_w + (gap_before_tail if unit_text else gap_after_big)
        draw.text((tail_x, baseline_y), tail_text,
                  fill=self.INK_TERTIARY, font=font_callout_sub, anchor="ls")

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _format_vndb_score(self, rating: Optional[float], votes: Optional[int]) -> str:
        if rating is None or rating <= 0:
            return "—"
        votes_str = f"  ({_format_compact_count(votes)} votes)" if votes else ""
        return f"{rating:.0f}{votes_str}"

    def _draw_stat_column(self, draw: ImageDraw.ImageDraw, x: int, y: int, row_h: int,
                          rows: list[tuple[str, str]],
                          font_label: ImageFont.ImageFont,
                          font_value: ImageFont.ImageFont):
        S = self.SCALE
        for label, value in rows:
            draw.text((x, y), label, fill=self.INK_SECONDARY, font=font_label)
            draw.text((x, y + 18 * S), value, fill=self.INK_PRIMARY, font=font_value)
            y += row_h

    def _draw_tag_pills(self, img: Image.Image, draw: ImageDraw.ImageDraw,
                        y: int, tags: list[str]):
        """Render tags as small left-aligned pills, attached visually to the title block."""
        if not tags:
            return
        S = self.SCALE
        font = _load_japanese_font(13 * S)
        pill_h = 24 * S
        pad_x = 12 * S
        gap = 8 * S

        widths = [int(draw.textlength(t, font=font)) + pad_x * 2 for t in tags]

        avail = self.TEXT_RIGHT - self.TEXT_X
        kept = list(zip(tags, widths))
        while kept and (sum(w for _, w in kept) + gap * (len(kept) - 1)) > avail:
            kept.pop()
        if not kept:
            return

        x = self.TEXT_X
        pill_center_y = y + pill_h // 2

        # Pre-render an anti-aliased pill shape by drawing at OVERSAMPLE×
        # the target size into a temp image and downsampling with LANCZOS.
        # The text is drawn directly on the canvas afterwards (PIL handles
        # font hinting/AA itself, so text doesn't need oversampling).
        OVERSAMPLE = 4
        radius = 12 * S

        for label, w in kept:
            self._paste_aa_rounded(
                img, (x, y, x + w, y + pill_h), radius,
                fill=self.PANEL_BG,
                outline=self.INK_TERTIARY,
                outline_w=1 * S,
                oversample=OVERSAMPLE,
            )
            draw.text((x + pad_x, pill_center_y), label,
                      fill=self.INK_PRIMARY, font=font, anchor="lm")
            x += w + gap

    def _paste_aa_rounded(self, img: Image.Image, box, radius: int,
                          fill=None, outline=None, outline_w: int = 1,
                          oversample: int = 4):
        """Render a rounded rectangle at OVERSAMPLE× resolution and downsample
        with LANCZOS to anti-alias the corners. PIL's draw primitives don't
        AA at integer pixels, so this is the standard workaround for smooth
        small-radius shapes. Works for fill+outline, outline-only, or
        fill-only by passing None for the unused parameter."""
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        ow, oh = w * oversample, h * oversample
        layer = Image.new("RGBA", (ow, oh), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle(
            [(0, 0), (ow - 1, oh - 1)],
            radius=radius * oversample,
            fill=fill,
            outline=outline,
            width=outline_w * oversample,
        )
        layer = layer.resize((w, h), Image.Resampling.LANCZOS)
        img.paste(layer, (x0, y0), layer)

    def _draw_cover_placeholder(self, draw: ImageDraw.ImageDraw,
                                box: Tuple[int, int, int, int], label: str,
                                bg: Optional[Tuple[int, int, int]] = None):
        S = self.SCALE
        bg = bg or self.PLACEHOLDER_BG
        self._draw_rounded_rect(draw, box, radius=4 * S, fill=bg,
                                outline=self.HAIRLINE, width=1 * S)
        font = _load_japanese_font(22 * S)
        text_w = draw.textlength(label, font=font)
        x = box[0] + ((box[2] - box[0]) - text_w) / 2
        y = box[1] + ((box[3] - box[1]) - 24 * S) / 2
        draw.text((x, y), label, fill=self.INK_SECONDARY, font=font)

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None
