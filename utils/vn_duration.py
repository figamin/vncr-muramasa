"""Reading-window estimation for the VN-of-the-Month cycle.

The window a group gets to read a chosen visual novel is derived from three
kinds of evidence:

1. **VNDB playtime** (``length_minutes``, averaged over ``length_votes``
   voters) — the anchor. VNDB does not expose a per-language breakdown, so this
   is the global average, not a Japanese-only one.
2. **The script itself** — jiten.moe's ``characterCount`` (moji) converted to
   hours with a *native Japanese* reading rate, plus a difficulty correction.
   Thin VNDB samples (few votes) are pulled toward this estimate.
3. **The server's own measured pace** — characters per hour of logged VN time
   and the typical session length, both taken from the immersion activities
   table. These turn "hours of reading" into "calendar days".

Everything here is pure and deterministic: same inputs, same window. The
caller is expected to snapshot the inputs (and this module's ``PARAMS_VERSION``)
so a later VNDB edit cannot move a deadline.

Model, in order::

    T_M   = moji / R_jp                     (hours at native speed)
    m_D   = clamp((difficulty / D_0) ** g)  (difficulty correction)
    T_jp  = L * ((T_M * m_D) / L) ** beta   (blend toward VNDB)
    r     = clamp(R_jp / R_srv)             (server speed ratio)
    d     = clamp(session_hours * phi)      (hours per calendar day)
    W     = ceil_week(T_jp * r * (1 + s) / d)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

PARAMS_VERSION = "vn-window-1"

# Fallback when the server has no usable immersion history: a little over an
# hour of reading on an active day.
DEFAULT_SESSION_HOURS = 1.5


def clamp(value: float, low: float, high: float) -> float:
    """Constrain ``value`` to the inclusive ``[low, high]`` range."""
    return max(low, min(high, value))


def weighted_median(pairs: Sequence[Tuple[float, float]]) -> Optional[float]:
    """Weighted median of ``(value, weight)`` pairs; None when there is no weight.

    Ties resolve to the midpoint of the two straddling values, which keeps the
    result stable when the sample size is even.
    """
    usable = [(float(v), float(w)) for v, w in pairs if w is not None and w > 0]
    if not usable:
        return None
    usable.sort(key=lambda item: item[0])
    total = sum(weight for _, weight in usable)
    if total <= 0:
        return None
    half = total / 2.0
    cumulative = 0.0
    for index, (value, weight) in enumerate(usable):
        cumulative += weight
        if cumulative == half and index + 1 < len(usable):
            return (value + usable[index + 1][0]) / 2.0
        if cumulative >= half:
            return value
    return usable[-1][0]


@dataclass(frozen=True)
class DurationParams:
    """Tunables for :func:`estimate_window`.

    Defaults are the values agreed in the design; change them deliberately and
    bump :data:`PARAMS_VERSION` so stored cycles stay explainable.
    """

    #: Native Japanese reading rate for VN prose, in moji per hour (400/min).
    native_chars_per_hour: float = 24_000.0
    #: Difficulty pivot; jiten's ``difficultyRaw`` for an average VN.
    difficulty_pivot: float = 2.0
    difficulty_gamma: float = 0.25
    difficulty_min: float = 0.85
    difficulty_max: float = 1.35
    #: Blend exponent toward the text estimate for a confident / thin VNDB sample.
    blend_beta_confident: float = 0.35
    blend_beta_thin: float = 0.60
    #: ``length_votes`` at or above which the VNDB average is trusted.
    confident_votes: int = 20
    speed_ratio_min: float = 0.75
    speed_ratio_max: float = 1.75
    #: Fraction of days a participant is expected to actually read.
    participation_factor: float = 0.6
    daily_hours_min: float = 0.5
    daily_hours_max: float = 4.0
    slack: float = 0.25
    min_days: int = 14
    max_days: int = 84
    #: Windows are rounded up to a multiple of this many days (whole weeks).
    week_alignment: int = 7


@dataclass
class WindowEstimate:
    """Result of :func:`estimate_window`, including every intermediate step."""

    vndb_id: Optional[str] = None
    moji: Optional[int] = None
    difficulty: Optional[float] = None
    length_minutes: Optional[float] = None
    length_votes: Optional[int] = None
    egs_playtime_hours: Optional[float] = None

    text_hours: Optional[float] = None          # T_M
    difficulty_multiplier: float = 1.0          # m_D
    blend_beta: float = 0.0                     # beta
    native_hours: float = 0.0                   # T_jp

    server_chars_per_hour: Optional[float] = None
    speed_ratio: float = 1.0                    # r
    server_hours: float = 0.0                   # T_jp * r

    session_hours: float = DEFAULT_SESSION_HOURS
    daily_hours: float = DEFAULT_SESSION_HOURS  # d
    total_hours: float = 0.0                    # incl. slack
    raw_days: float = 0.0
    days: int = 0
    clamped_low: bool = False
    exceeded_max: bool = False
    max_days: int = 84
    min_days: int = 14

    params_version: str = PARAMS_VERSION
    notes: List[str] = field(default_factory=list)

    @property
    def fits(self) -> bool:
        """True when the computed window fits inside the configured maximum."""
        return not self.exceeded_max

    @property
    def hours_per_week(self) -> float:
        """Reading load implied by the window, for the announcement."""
        if self.days <= 0:
            return 0.0
        return self.total_hours / (self.days / 7.0)

    def lines(self) -> List[Tuple[str, str]]:
        """Human readable ``(label, value)`` pairs, used by embeds and tests."""
        def hours(value: Optional[float]) -> str:
            return format_hours(value) if value is not None else "—"

        rows: List[Tuple[str, str]] = [
            ("VNDB playtime", (
                f"{hours((self.length_minutes or 0) / 60)} "
                f"({self.length_votes or 0} votes)"
            )),
        ]
        if self.egs_playtime_hours:
            rows.append(("EGS playtime", f"{format_hours(self.egs_playtime_hours)} (median)"))
        rows.extend([
            ("Script length", f"{self.moji:,} moji" if self.moji else "unknown"),
            ("Difficulty (jiten)", f"{self.difficulty:.2f}" if self.difficulty is not None else "unknown"),
            ("Native estimate T_M", hours(self.text_hours)),
            ("Difficulty factor m_D", f"×{self.difficulty_multiplier:.3f}"),
            ("Blend β / T_jp", f"{self.blend_beta:.2f} → {hours(self.native_hours)}"),
            ("Server pace", (
                f"{self.server_chars_per_hour:,.0f} moji/h"
                if self.server_chars_per_hour else "unavailable"
            )),
            ("Speed ratio r", f"×{self.speed_ratio:.2f} → {hours(self.server_hours)}"),
            ("Daily commitment d", f"{self.daily_hours:.2f} h/day"),
            ("With slack", hours(self.total_hours)),
            ("Raw window", f"{self.raw_days:.1f} days"),
        ])
        return rows


def format_hours(value: Any) -> str:
    """Render hours as ``12h 30m`` (minutes precision, no seconds)."""
    try:
        total_minutes = int(round(float(value) * 60))
    except (TypeError, ValueError):
        return "—"
    hours, minutes = divmod(max(total_minutes, 0), 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def hours_from_moji(moji: Optional[float], params: DurationParams) -> Optional[float]:
    """Hours a native reader needs for ``moji`` characters."""
    if not moji or moji <= 0 or params.native_chars_per_hour <= 0:
        return None
    return float(moji) / params.native_chars_per_hour


def difficulty_multiplier(difficulty: Optional[float], params: DurationParams) -> float:
    """Scale a text-based estimate by the VN's measured difficulty."""
    if not difficulty or difficulty <= 0 or params.difficulty_pivot <= 0:
        return 1.0
    raw = (float(difficulty) / params.difficulty_pivot) ** params.difficulty_gamma
    return clamp(raw, params.difficulty_min, params.difficulty_max)


def blend_beta(length_votes: Optional[int], has_moji: bool, params: DurationParams) -> float:
    """Exponent used to blend the text estimate toward the VNDB average."""
    if not has_moji:
        return 0.0
    try:
        votes = int(length_votes or 0)
    except (TypeError, ValueError):
        votes = 0
    return (
        params.blend_beta_confident
        if votes >= params.confident_votes
        else params.blend_beta_thin
    )


def estimate_window(
    *,
    moji: Optional[int] = None,
    difficulty: Optional[float] = None,
    length_minutes: Optional[float] = None,
    length_votes: Optional[int] = None,
    egs_playtime_hours: Optional[float] = None,
    server_chars_per_hour: Optional[float] = None,
    session_hours: Optional[float] = None,
    params: DurationParams = DurationParams(),
    vndb_id: Optional[str] = None,
) -> WindowEstimate:
    """Compute how many days a group needs to read one visual novel.

    ``moji``, ``length_minutes``, and/or ``egs_playtime_hours`` must be supplied;
    everything else degrades gracefully to a neutral assumption (documented in ``notes``).
    """
    estimate = WindowEstimate(
        vndb_id=vndb_id,
        moji=int(moji) if moji else None,
        difficulty=float(difficulty) if difficulty else None,
        length_minutes=float(length_minutes) if length_minutes else None,
        length_votes=int(length_votes) if length_votes is not None else None,
        egs_playtime_hours=float(egs_playtime_hours) if egs_playtime_hours else None,
        max_days=params.max_days,
        min_days=params.min_days,
    )
    notes = estimate.notes

    if not estimate.moji and not estimate.length_minutes and not estimate.egs_playtime_hours:
        raise ValueError("estimate_window needs moji, length_minutes, or egs_playtime_hours")

    # --- 1. text-based native estimate ------------------------------------
    text_hours = hours_from_moji(estimate.moji, params)
    if text_hours is not None:
        estimate.text_hours = text_hours
        estimate.difficulty_multiplier = difficulty_multiplier(estimate.difficulty, params)
    else:
        notes.append("No jiten character count: using playtime average only.")
        estimate.difficulty_multiplier = 1.0

    adjusted_text = (
        estimate.text_hours * estimate.difficulty_multiplier
        if estimate.text_hours is not None else None
    )

    # --- 2. blend toward VNDB (or EGS median playtime fallback) ------------
    estimate.blend_beta = blend_beta(
        estimate.length_votes, estimate.text_hours is not None, params
    )
    if estimate.length_minutes and adjusted_text:
        anchor = estimate.length_minutes / 60.0
        ratio = adjusted_text / anchor if anchor > 0 else 1.0
        estimate.native_hours = anchor * (ratio ** estimate.blend_beta)
        if (estimate.length_votes or 0) < params.confident_votes:
            notes.append(
                f"VNDB has only {estimate.length_votes or 0} playtime votes; the "
                "script length carries more weight."
            )
    elif estimate.length_minutes:
        estimate.native_hours = estimate.length_minutes / 60.0
    elif estimate.egs_playtime_hours and adjusted_text:
        anchor = estimate.egs_playtime_hours
        ratio = adjusted_text / anchor if anchor > 0 else 1.0
        estimate.native_hours = anchor * (ratio ** estimate.blend_beta)
        notes.append("No VNDB playtime: using EroGamescape median playtime.")
    elif estimate.egs_playtime_hours:
        estimate.native_hours = estimate.egs_playtime_hours
        notes.append("No VNDB playtime: using EroGamescape median playtime.")
    else:
        estimate.native_hours = adjusted_text or 0.0
        notes.append("No VNDB or EGS playtime: using the script-based estimate.")

    # --- 3. server speed ratio --------------------------------------------
    if server_chars_per_hour and server_chars_per_hour > 0:
        estimate.server_chars_per_hour = float(server_chars_per_hour)
        estimate.speed_ratio = clamp(
            params.native_chars_per_hour / estimate.server_chars_per_hour,
            params.speed_ratio_min,
            params.speed_ratio_max,
        )
        if estimate.server_chars_per_hour <= 0:  # pragma: no cover - guarded above
            estimate.speed_ratio = 1.0
    else:
        notes.append("No server reading pace available: assuming native speed.")

    estimate.server_hours = estimate.native_hours * estimate.speed_ratio

    # --- 4. calendar conversion -------------------------------------------
    if session_hours and session_hours > 0:
        estimate.session_hours = float(session_hours)
    else:
        estimate.session_hours = DEFAULT_SESSION_HOURS
        notes.append(
            f"No session-length history: assuming {DEFAULT_SESSION_HOURS:.1f} h "
            "of reading per active day."
        )
    estimate.daily_hours = clamp(
        estimate.session_hours * params.participation_factor,
        params.daily_hours_min,
        params.daily_hours_max,
    )

    estimate.total_hours = estimate.server_hours * (1.0 + params.slack)
    estimate.raw_days = estimate.total_hours / estimate.daily_hours

    # --- 5. rounding and bounds -------------------------------------------
    aligned = math.ceil(estimate.raw_days / params.week_alignment) * params.week_alignment
    if aligned < params.min_days:
        aligned = params.min_days
        estimate.clamped_low = True
    if estimate.raw_days > params.max_days:
        estimate.exceeded_max = True
        notes.append(
            f"Needs ≈{estimate.raw_days:.0f} days, above the {params.max_days}-day "
            "maximum: pick a shorter VN, or confirm a capped cycle with checkpoints."
        )
    estimate.days = int(min(aligned, params.max_days))
    return estimate


def estimate_from_sources(
    *,
    vndb_data: Optional[Dict[str, Any]],
    jiten_info: Optional[Any],
    speed_stats: Optional[Dict[str, Any]],
    params: DurationParams = DurationParams(),
    vndb_id: Optional[str] = None,
    assume_chars_per_hour: Optional[float] = None,
    assume_session_hours: Optional[float] = None,
) -> WindowEstimate:
    """Adapt VNDB + jiten + repository payloads into :func:`estimate_window`.

    ``jiten_info`` is a ``JitenInfo`` (duck-typed: ``character_count`` and
    ``difficulty_raw``) and ``speed_stats`` is
    ``ActivityRepository.get_reading_speed_stats()`` output.
    """
    vndb = vndb_data or {}
    moji = getattr(jiten_info, "character_count", None) if jiten_info else None
    difficulty = getattr(jiten_info, "difficulty_raw", None) if jiten_info else None

    ext = vndb.get("external_ratings") or {}
    egs_playtime_hours = ext.get("egs_playtime_median")

    length_minutes = vndb.get("length_minutes")
    length_votes = vndb.get("length_votes")
    if not length_minutes and not egs_playtime_hours:
        # Fall back to the length bucket (1-5) midpoints when neither
        # length_minutes nor EGS playtime is available.
        bucket_hours = {1: 1.5, 2: 6.0, 3: 20.0, 4: 40.0, 5: 80.0}
        bucket = vndb.get("length")
        if isinstance(bucket, (int, float)) and int(bucket) in bucket_hours:
            length_minutes = bucket_hours[int(bucket)] * 60.0
            length_votes = length_votes or 0

    stats = speed_stats or {}
    server_cph = assume_chars_per_hour or stats.get("chars_per_hour")
    session = assume_session_hours or stats.get("session_hours")

    estimate = estimate_window(
        moji=moji,
        difficulty=difficulty,
        length_minutes=length_minutes,
        length_votes=length_votes,
        egs_playtime_hours=egs_playtime_hours,
        server_chars_per_hour=server_cph,
        session_hours=session,
        params=params,
        vndb_id=vndb_id,
    )
    if not stats.get("usable", True):
        estimate.notes.append(
            f"Server pace is based on a thin sample ({stats.get('users', 0)} "
            f"members, {stats.get('hours', 0):.1f} h)."
        )
    return estimate
