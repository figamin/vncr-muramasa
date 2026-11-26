"""VN of the Month — reading-window estimation.

This iteration ships the estimator only: it answers "how long should we give
people to read this VN?" using

* VNDB's average playtime (``length_minutes``) as the anchor,
* jiten.moe's character count (moji) and difficulty as the script evidence,
* the server's own measured reading pace (moji/hour and session length) taken
  from the immersion activities table.

The maths lives in :mod:`utils.vn_duration` so it can be unit-tested and reused
by the vote/cycle machinery that comes next. Nothing here writes state: the
command is a read-only preview.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.helpers import (
    bangumi_subject_url,
    egs_game_url,
    extract_vndb_id,
    jiten_deck_url,
)
from utils.jiten_client import JitenClient
from utils.pg_db import get_pg_db
from utils.vn_duration import (
    PARAMS_VERSION,
    DurationParams,
    WindowEstimate,
    estimate_from_sources,
    format_hours,
)
from utils.vndb_client import VNDBClient

logger = logging.getLogger(__name__)


class VnOfMonthCog(commands.Cog):
    """Estimates how long a group needs to read a visual novel."""

    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # Data gathering
    # ------------------------------------------------------------------

    async def _speed_stats(self, lookback_days: int) -> dict:
        immersion = self.bot.get_cog("ImmersionCog")
        repo = getattr(immersion, "activity_repo", None)
        if repo is None:
            logger.warning("VN window: ImmersionCog activity repository is unavailable")
            return {}
        return await repo.get_reading_speed_stats(lookback_days=lookback_days)

    @staticmethod
    async def _vn_data(vndb_id: str) -> dict:
        try:
            async with VNDBClient() as vndb:
                return await vndb.get_vn_by_id(vndb_id) or {}
        except Exception:
            logger.warning("VN window: VNDB lookup failed for %s", vndb_id, exc_info=True)
            return {}

    @staticmethod
    async def _jiten_data(vndb_id: str):
        try:
            async with JitenClient() as jiten:
                return await jiten.get_by_vndb_id(vndb_id)
        except Exception:
            logger.warning("VN window: jiten lookup failed for %s", vndb_id, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _build_embed(
        self,
        estimate: WindowEstimate,
        vn_data: dict,
        stats: dict,
    ) -> discord.Embed:
        title = (vn_data.get("title") or estimate.vndb_id or "Unknown VN")[:200]
        embed = discord.Embed(
            title=f"📐 Reading window — {title}",
            description=(
                f"**{estimate.days} days**"
                + (
                    " ⚠️ *(above the maximum — see notes)*"
                    if estimate.exceeded_max else ""
                )
                + f"\n≈ {format_hours(estimate.hours_per_week)} of reading per week"
            ),
            color=discord.Color.gold() if estimate.fits else discord.Color.orange(),
        )
        if estimate.vndb_id:
            embed.url = f"https://vndb.org/{estimate.vndb_id}"

        rows = estimate.lines()
        half = (len(rows) + 1) // 2
        for label, chunk in (
            ("🧮 Inputs & blend", rows[:half]),
            ("📈 Pace & window", rows[half:]),
        ):
            embed.add_field(
                name=label,
                value="\n".join(f"**{name}:** {value}" for name, value in chunk)[:1024],
                inline=True,
            )

        if stats:
            source = (
                f"Measured over the last **{stats.get('lookback_days', 90)} days** from "
                f"**{stats.get('users', 0)} members** "
                f"({stats.get('hours', 0):.1f} h logged)."
            )
        else:
            source = "Server reading history unavailable."
        embed.add_field(name="📊 Data source", value=source, inline=False)

        if estimate.notes:
            embed.add_field(
                name="ℹ️ Notes",
                value="\n".join(f"• {note}" for note in estimate.notes)[:1024],
                inline=False,
            )

        start = datetime.now(timezone.utc).date()
        end = start + timedelta(days=estimate.days)
        embed.add_field(
            name="🗓️ If the cycle started today",
            value=f"{start:%d %b %Y} → **{end:%d %b %Y}**",
            inline=False,
        )

        ext = vn_data.get("external_ratings") or {}
        jiten_info = vn_data.get("jiten") or {}
        links = []
        if estimate.vndb_id:
            links.append(f"[VNDB](https://vndb.org/{estimate.vndb_id})")
        if ext.get("egs_id"):
            links.append(f"[EroGamescape]({ext.get('egs_url') or egs_game_url(ext['egs_id'])})")
        if ext.get("bangumi_id"):
            links.append(f"[Bangumi]({ext.get('bangumi_url') or bangumi_subject_url(ext['bangumi_id'])})")
        if jiten_info.get("deck_id"):
            links.append(f"[jiten.moe]({jiten_deck_url(jiten_info['deck_id'])})")
        if links:
            score_bits = []
            if ext.get("egs_median") is not None:
                score_bits.append(f"EGS: {ext['egs_median']} median")
            if ext.get("bangumi_score") is not None:
                score_bits.append(f"Bangumi: {ext['bangumi_score']:.1f}/10")
            if ext.get("consensus_score") is not None:
                score_bits.append(f"Consensus: {ext['consensus_score']:.2f}/10")
            value = " • ".join(links)
            if score_bits:
                value = " • ".join(score_bits) + "\n" + value
            embed.add_field(name="🔗 Multi-DB Sources", value=value[:1024], inline=False)

        embed.set_footer(
            text=f"Estimator {PARAMS_VERSION} · preview only, nothing was scheduled"
        )
        return embed

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="vn_window",
        description="[ADMIN] Preview the reading window a visual novel would need",
    )
    @app_commands.describe(
        vndb_id="VNDB id or URL, e.g. v38511 or https://vndb.org/v38511",
        lookback_days="Days of server history to measure reading pace over (default 90).",
        slack="Extra headroom as a fraction, e.g. 0.25 for 25% (default 0.25).",
        max_days="Maximum acceptable window in days (default 84).",
        assume_chars_per_hour="Override the measured server pace (moji/hour).",
        assume_session_hours="Override the measured session length (hours per active day).",
        public="Post the result in the channel instead of only showing it to you (default: private).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def vn_window(
        self,
        interaction: discord.Interaction,
        vndb_id: str,
        lookback_days: Optional[app_commands.Range[int, 7, 365]] = None,
        slack: Optional[app_commands.Range[float, 0.0, 1.0]] = None,
        max_days: Optional[app_commands.Range[int, 7, 365]] = None,
        assume_chars_per_hour: Optional[app_commands.Range[int, 1000, 200000]] = None,
        assume_session_hours: Optional[app_commands.Range[float, 0.25, 12.0]] = None,
        public: Optional[bool] = None,
    ):
        # Validated before acknowledging so this error can stay private even when
        # the result itself is going to be posted publicly.
        pg = get_pg_db()
        resolved = None
        if pg.is_configured:
            try:
                resolved = await pg.resolve_vndb_id(vndb_id)
            except Exception as exc:
                logger.debug("PG resolve_vndb_id failed in vn_window: %s", exc)
        if not resolved:
            resolved = extract_vndb_id(vndb_id) or extract_vndb_id(f"v{vndb_id.strip()}")
        if not resolved:
            await interaction.response.send_message(
                "❌ That doesn't look like a VNDB, EroGamescape, or Bangumi id/URL. "
                "Try `v38511` or `https://vndb.org/v38511`.",
                ephemeral=True,
            )
            return

        # Private by default; `public:true` posts the estimate in the channel.
        ephemeral = not public
        await interaction.response.defer(ephemeral=ephemeral)

        defaults = DurationParams()
        params = DurationParams(
            slack=slack if slack is not None else defaults.slack,
            max_days=int(max_days) if max_days is not None else defaults.max_days,
        )
        stats = await self._speed_stats(int(lookback_days or 90))
        vn_data = await self._vn_data(resolved)
        jiten_info = await self._jiten_data(resolved)

        if not vn_data and not jiten_info:
            await interaction.followup.send(
                f"❌ Could not find **{resolved}** on VNDB or jiten.moe.",
                ephemeral=ephemeral,
            )
            return

        try:
            estimate = estimate_from_sources(
                vndb_data=vn_data,
                jiten_info=jiten_info,
                speed_stats=stats,
                params=params,
                vndb_id=resolved,
                assume_chars_per_hour=assume_chars_per_hour,
                assume_session_hours=assume_session_hours,
            )
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=ephemeral)
            return

        logger.info(
            "VN window estimate for %s: %sd (raw %.1f, T_jp %.1fh, r %.2f, d %.2fh, "
            "moji=%s, length_minutes=%s, votes=%s)",
            resolved, estimate.days, estimate.raw_days, estimate.native_hours,
            estimate.speed_ratio, estimate.daily_hours, estimate.moji,
            estimate.length_minutes, estimate.length_votes,
        )

        embed = self._build_embed(estimate, vn_data, stats)
        await interaction.followup.send(
            embed=embed,
            ephemeral=ephemeral,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot):
    await bot.add_cog(VnOfMonthCog(bot))
