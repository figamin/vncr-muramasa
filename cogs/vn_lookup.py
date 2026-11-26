"""Multi-database visual novel lookup command (``/vn``).

Combines VNDB, EroGamescape, Bangumi, and Jiten data from the VNCR
PostgreSQL multi-database architecture (with graceful VNDB HTTP fallback).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.helpers import (
    bangumi_subject_url,
    convert_bbcode_to_markdown,
    egs_game_url,
    extract_vndb_id,
    format_vn_length,
    is_content_nsfw,
    jiten_deck_url,
    truncate_text,
)
from utils.immersion_media import MediaSearcher, normalize_vn_titles
from utils.pg_db import get_pg_db
from utils.vndb_client import VNDBClient

logger = logging.getLogger(__name__)

_VN_BRACKET_ID_RE = re.compile(r"\[(v\d+)\]", re.IGNORECASE)


class VNLookupCog(commands.Cog):
    """Provides ``/vn`` lookup across VNDB, EroGamescape, Bangumi, and Jiten."""

    def __init__(self, bot):
        self.bot = bot
        self.media_searcher = MediaSearcher()

    async def _resolve_target_vn_id(self, query: str) -> Optional[str]:
        raw = (query or "").strip()
        if not raw:
            return None

        parsed = self.media_searcher.parse_autocomplete_value(raw)
        if parsed:
            item_id, _, source = parsed
            if not source or source.lower().startswith("vndb"):
                return item_id if item_id.startswith("v") else f"v{item_id}"

        bracket = _VN_BRACKET_ID_RE.search(raw)
        if bracket:
            return bracket.group(1).lower()

        pg = get_pg_db()
        if pg.is_configured:
            resolved = await pg.resolve_vndb_id(raw)
            if resolved:
                return resolved

        direct = extract_vndb_id(raw)
        if direct:
            return direct.lower()

        search_title = raw.split(" — ")[0].strip() if " — " in raw else raw
        results = await self.media_searcher.search_visual_novel(search_title, limit=1)
        if results and results[0].get("id"):
            return str(results[0]["id"]).lower()
        return None

    @staticmethod
    def _format_scores(vn: Dict[str, Any]) -> Optional[str]:
        lines: List[str] = []
        rating = vn.get("rating")
        votecount = vn.get("votecount")
        if rating is not None and rating > 0:
            rating_10 = rating / 10.0 if rating > 10 else rating
            v_str = f" · {int(votecount):,} votes" if votecount else ""
            lines.append(f"**VNDB:** {rating_10:.2f}/10{v_str}")

        ext = vn.get("external_ratings") or {}
        egs = vn.get("egs_details") or {}

        egs_median = ext.get("egs_median") if ext.get("egs_median") is not None else egs.get("median")
        egs_avg = ext.get("egs_average") if ext.get("egs_average") is not None else egs.get("average")
        egs_count = ext.get("egs_count") if ext.get("egs_count") is not None else egs.get("count")
        if egs_median is not None:
            parts = [f"{egs_median} median"]
            if egs_avg is not None:
                parts.append(f"{egs_avg} avg")
            if egs_count:
                parts.append(f"{int(egs_count):,} votes")
            lines.append(f"**EroGamescape:** {' · '.join(parts)}")

        bgm = vn.get("bangumi_details") or {}
        bgm_score = ext.get("bangumi_score") if ext.get("bangumi_score") is not None else bgm.get("score")
        bgm_rank = ext.get("bangumi_rank") if ext.get("bangumi_rank") is not None else bgm.get("rank")
        bgm_votes = ext.get("bangumi_votes") if ext.get("bangumi_votes") is not None else bgm.get("votes")
        if bgm_score is not None and bgm_score > 0:
            parts = [f"{float(bgm_score):.1f}/10"]
            if bgm_rank:
                parts.append(f"#{int(bgm_rank):,}")
            if bgm_votes:
                parts.append(f"{int(bgm_votes):,} votes")
            lines.append(f"**Bangumi:** {' · '.join(parts)}")

        consensus = ext.get("consensus_score")
        if consensus is not None:
            lines.append(f"**Consensus:** {float(consensus):.2f}/10")

        return "\n".join(lines) if lines else None

    @staticmethod
    def _format_length_and_reading(vn: Dict[str, Any]) -> Optional[str]:
        lines: List[str] = []
        length_tier = format_vn_length(vn.get("length"))
        length_minutes = vn.get("length_minutes")
        length_votes = vn.get("length_votes")

        if length_tier != "Unknown" or length_minutes:
            parts = []
            if length_tier != "Unknown":
                parts.append(length_tier)
            if length_minutes:
                hrs = int(round(float(length_minutes) / 60.0))
                parts.append(f"~{hrs}h")
            if length_votes:
                parts.append(f"{int(length_votes):,} votes")
            lines.append(f"**VNDB Length:** {' · '.join(parts)}")

        ext = vn.get("external_ratings") or {}
        egs = vn.get("egs_details") or {}
        egs_play = ext.get("egs_playtime_median") or egs.get("total_play_time_median")
        egs_fun = ext.get("egs_fun_time_median") or egs.get("time_before_fun_median")
        if egs_play:
            play_str = f"{int(egs_play)}h median"
            if egs_fun:
                play_str += f" ({int(egs_fun)}h until fun)"
            lines.append(f"**EGS Playtime:** {play_str}")

        jiten = vn.get("jiten") or {}
        if jiten.get("character_count"):
            j_parts = [f"{int(jiten['character_count']):,} chars"]
            if jiten.get("unique_kanji_count"):
                j_parts.append(f"{int(jiten['unique_kanji_count']):,} kanji")
            if jiten.get("dialogue_percentage"):
                j_parts.append(f"{float(jiten['dialogue_percentage']):.0f}% dialogue")
            if jiten.get("difficulty_raw"):
                j_parts.append(f"Diff {float(jiten['difficulty_raw']):.2f}/5")
            lines.append(f"**Jiten:** {' · '.join(j_parts)}")

        return "\n".join(lines) if lines else None

    @staticmethod
    def _format_tags_and_povs(vn: Dict[str, Any]) -> Optional[str]:
        sections: List[str] = []
        raw_tags = vn.get("tags") or []
        content_tags = [
            t for t in raw_tags
            if t.get("category") == "cont" and (t.get("spoiler") or 0) == 0 and t.get("name")
        ]
        content_tags.sort(key=lambda t: t.get("rating") or 0, reverse=True)
        if content_tags:
            sections.append("**VNDB Tags:** " + ", ".join(t["name"] for t in content_tags[:6]))

        ext = vn.get("external_ratings") or {}
        egs = vn.get("egs_details") or {}
        genre = ext.get("egs_genre") or egs.get("genre")
        if genre:
            sections.append(f"**EGS Genre:** {genre}")

        povs = egs.get("povs") or []
        if povs:
            sections.append("**EGS POVs:** " + ", ".join(p["title"] for p in povs[:5]))

        bgm = vn.get("bangumi_details") or {}
        bgm_tags = bgm.get("tags") or []
        if bgm_tags:
            sections.append("**Bangumi Tags:** " + ", ".join(t["name"] for t in bgm_tags[:5]))

        if not sections:
            return None
        return "\n".join(sections)[:1024]

    @staticmethod
    def _build_links_view(vn: Dict[str, Any]) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        vn_id = vn.get("id")
        if vn_id:
            view.add_item(discord.ui.Button(
                label="VNDB",
                style=discord.ButtonStyle.link,
                url=f"https://vndb.org/{vn_id}",
            ))
        ext = vn.get("external_ratings") or {}
        egs_id = ext.get("egs_id")
        if egs_id:
            view.add_item(discord.ui.Button(
                label="EroGamescape",
                style=discord.ButtonStyle.link,
                url=ext.get("egs_url") or egs_game_url(egs_id),
            ))
        bangumi_id = ext.get("bangumi_id")
        if bangumi_id:
            view.add_item(discord.ui.Button(
                label="Bangumi",
                style=discord.ButtonStyle.link,
                url=ext.get("bangumi_url") or bangumi_subject_url(bangumi_id),
            ))
        jiten = vn.get("jiten") or {}
        deck_id = jiten.get("deck_id")
        if deck_id:
            view.add_item(discord.ui.Button(
                label="jiten.moe",
                style=discord.ButtonStyle.link,
                url=jiten_deck_url(deck_id),
            ))
        return view

    @app_commands.command(
        name="vn",
        description="Look up a visual novel across VNDB, EroGamescape, Bangumi, and Jiten",
    )
    @app_commands.describe(query="VN title or VNDB / EroGamescape / Bangumi ID or URL")
    async def vn_lookup(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        try:
            vn_id = await self._resolve_target_vn_id(query)
            if not vn_id:
                await interaction.followup.send(
                    f"❌ Could not find a visual novel matching `{query}`.",
                    ephemeral=True,
                )
                return

            async with VNDBClient() as vndb:
                vn = await vndb.get_vn_full(vn_id)

            if not vn:
                await interaction.followup.send(
                    f"❌ Could not load details for `{vn_id}`.",
                    ephemeral=True,
                )
                return

            titles = normalize_vn_titles(vn)
            primary_title = titles.get("ja") or titles.get("primary") or vn.get("title") or vn_id
            alt_titles = [
                t for t in (titles.get("primary"), titles.get("romaji"), titles.get("en"))
                if t and t != primary_title
            ]
            ext = vn.get("external_ratings") or {}
            if ext.get("bangumi_name_cn") and ext["bangumi_name_cn"] not in alt_titles and ext["bangumi_name_cn"] != primary_title:
                alt_titles.append(ext["bangumi_name_cn"])

            header_parts: List[str] = []
            if alt_titles:
                header_parts.append(f"*{alt_titles[0]}*")

            meta_line: List[str] = []
            if vn.get("released"):
                meta_line.append(f"📅 {vn['released']}")
            devs = [d.get("name") for d in (vn.get("developers") or []) if d.get("name")]
            egs_brand = (vn.get("egs_details") or {}).get("brandname")
            if devs:
                meta_line.append(f"🏢 {', '.join(devs[:2])}")
            elif egs_brand:
                meta_line.append(f"🏢 {egs_brand}")
            if meta_line:
                header_parts.append(" • ".join(meta_line))

            if vn.get("description"):
                desc_md = convert_bbcode_to_markdown(vn["description"])
                header_parts.append(truncate_text(desc_md, 350))

            embed = discord.Embed(
                title=primary_title[:256],
                url=f"https://vndb.org/{vn_id}",
                description="\n\n".join(header_parts)[:4096] or None,
                color=discord.Color.blurple(),
            )

            if not is_content_nsfw(vn) and (vn.get("image") or {}).get("url"):
                embed.set_thumbnail(url=vn["image"]["url"])

            scores_val = self._format_scores(vn)
            if scores_val:
                embed.add_field(name="⭐ Scores", value=scores_val[:1024], inline=False)

            len_val = self._format_length_and_reading(vn)
            if len_val:
                embed.add_field(name="⏱️ Length & Reading Stats", value=len_val[:1024], inline=False)

            tags_val = self._format_tags_and_povs(vn)
            if tags_val:
                embed.add_field(name="🏷️ Tags & POVs", value=tags_val, inline=False)

            embed.set_footer(text=f"VNCR Multi-DB · {vn_id}")
            view = self._build_links_view(vn)
            await interaction.followup.send(embed=embed, view=view)
        except Exception as exc:
            logger.error("Error in /vn command for '%s': %s", query, exc, exc_info=True)
            await interaction.followup.send(
                "❌ An error occurred while looking up that visual novel.",
                ephemeral=True,
            )

    @vn_lookup.autocomplete("query")
    async def vn_lookup_autocomplete(self, interaction: discord.Interaction, current: str):
        q = (current or "").strip()
        if len(q) < 2:
            return []

        try:
            results = await self.media_searcher.search_visual_novel(q, limit=25)
        except Exception as exc:
            logger.warning("/vn autocomplete failed for '%s': %s", q, exc)
            return []

        choices: List[app_commands.Choice[str]] = []
        for vn in results:
            vn_id = vn.get("id")
            titles = vn.get("titles") or {}
            display_title = vn.get("display") or titles.get("primary")
            if not vn_id or not display_title:
                continue

            badges: List[str] = []
            if vn.get("released"):
                badges.append(str(vn["released"]))
            ext = vn.get("external_ratings") or {}
            consensus = ext.get("consensus_score")
            if consensus is not None:
                badges.append(f"★{float(consensus):.2f}")
            elif isinstance(vn.get("rating"), (int, float)) and vn["rating"] > 0:
                r = vn["rating"]
                badges.append(f"{(r / 10 if r > 10 else r):.1f}/10")

            suffix = f" [{vn_id}]"
            if badges:
                badge_str = f" — {' • '.join(badges)}"
                label = f"{display_title}{badge_str}{suffix}"
                if len(label) > 100:
                    avail = 100 - len(badge_str) - len(suffix) - 1
                    label = f"{display_title[:max(1, avail)]}…{badge_str}{suffix}"
            else:
                label = f"{display_title}{suffix}"
                if len(label) > 100:
                    label = f"{display_title[:100 - len(suffix) - 1]}…{suffix}"

            value = self.media_searcher.create_autocomplete_value(vn_id, "primary", source="vndb")
            choices.append(app_commands.Choice(name=label, value=value))

        return choices[:25]


async def setup(bot):
    await bot.add_cog(VNLookupCog(bot))
