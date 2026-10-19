"""
Rendered tier-progress bar for activity log embeds. Discord embed text can't do
color, so the bar is drawn as an image: prior progress in green, the portion
gained by the current log highlighted in yellow.
"""
import io

from PIL import Image, ImageDraw

# Rendered at 2x and LANCZOS-downsampled for clean rounded edges.
WIDTH, HEIGHT = 640, 48
_SS = 2
# Transparent bleed around the track: Discord crops the extremes of very wide
# images, so the canvas edges must hold nothing that matters.
_MARGIN_X = 16 * _SS
_MARGIN_Y = 3 * _SS
_PAD = 8 * _SS  # gap between track edge and fill pills

TRACK = (32, 34, 37, 255)
TRACK_OUTLINE = (62, 65, 70, 255)
PRIOR_FILL = (59, 165, 93, 255)    # green, matches the log embed color
GAINED_FILL = (254, 231, 92, 255)  # yellow highlight for this session


def render_class_progress_bar(progress_ratio: float, gained_ratio: float = 0.0) -> bytes:
    """PNG progress bar; fills render as pills floating inside the track.

    The inset pills are concentric with the track's rounded corners, so they can
    never poke outside it and no clipping mask is needed.
    """
    progress_ratio = min(max(progress_ratio, 0.0), 1.0)
    gained_ratio = min(max(gained_ratio, 0.0), progress_ratio)
    w, h = WIDTH * _SS, HEIGHT * _SS
    track_left, track_top = _MARGIN_X, _MARGIN_Y
    track_right, track_bottom = w - 1 - _MARGIN_X, h - 1 - _MARGIN_Y
    track_h = track_bottom - track_top + 1

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [track_left, track_top, track_right, track_bottom],
        radius=track_h // 2, fill=TRACK, outline=TRACK_OUTLINE, width=_SS,
    )

    if progress_ratio > 0:
        left, top = track_left + _PAD, track_top + _PAD
        right, bottom = track_right - _PAD, track_bottom - _PAD
        inner_w, inner_h = right - left + 1, bottom - top + 1
        radius = inner_h // 2
        # Pills need at least their height in width to keep rounded caps intact.
        fill_w = max(round(progress_ratio * inner_w), inner_h)
        draw.rounded_rectangle([left, top, left + fill_w - 1, bottom], radius=radius, fill=PRIOR_FILL)
        if gained_ratio > 0:
            # Straight left edge = flat boundary against the green; the right side
            # keeps the rounded cap. A gain covering the whole fill rounds both ends.
            fill_right = left + fill_w - 1
            gain_w = max(round(gained_ratio * inner_w), radius)
            gain_left = max(left + fill_w - gain_w, left)
            if gain_left == left:
                draw.rounded_rectangle([left, top, fill_right, bottom], radius=radius, fill=GAINED_FILL)
            else:
                cap_cx = fill_right - radius  # cap circle center, matches the green pill's cap
                gain_left = min(gain_left, cap_cx)
                draw.pieslice([cap_cx - radius, top, cap_cx + radius, bottom], -90, 90, fill=GAINED_FILL)
                draw.rectangle([gain_left, top, cap_cx, bottom], fill=GAINED_FILL)

    img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
