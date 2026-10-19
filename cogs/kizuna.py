"""Kizuna texthooker integration.

Listens for kizuna webhook posts in any channel, parses
its log embed, looks up the linked discord user, and auto-logs the activity by
calling ImmersionCog.build_vn_log. The bot replies to the kizuna message with
the same "Visual Novel Activity Logged!" embed produced by /log_vn.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Set

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import Database

logger = logging.getLogger(__name__)

_KIZUNA_TITLE_RE = re.compile(
    r"^(?P<user>.+?)\s+read\s+(?P<chars>[\d,]+)\s+characters\s+of\s+(?P<title>.+)$",
    re.IGNORECASE,
)
_VNDB_URL_RE = re.compile(r"https?://vndb\.org/(v\d+)", re.IGNORECASE)
_DURATION_HMS_RE = re.compile(r"^\s*(\d{1,3}):(\d{1,2}):(\d{1,2})\s*$")
_DURATION_MS_RE = re.compile(r"^\s*(\d{1,3}):(\d{1,2})\s*$")


def _parse_hms(value: str) -> int:
    m = _DURATION_HMS_RE.match(value)
    if m:
        h, mn, s = (int(g) for g in m.groups())
        return h * 3600 + mn * 60 + s
    m = _DURATION_MS_RE.match(value)
    if m:
        mn, s = (int(g) for g in m.groups())
        return mn * 60 + s
    return 0


def _parse_kizuna_embed(embed: discord.Embed) -> Optional[Dict[str, Any]]:
    """Parse a kizuna VN log embed. Returns None for non-VN or unrecognised embeds."""
    title = (embed.title or "").strip()
    if not title:
        return None
    m = _KIZUNA_TITLE_RE.match(title)
    if not m:
        return None

    kizuna_username = m.group("user").strip()
    vn_title = m.group("title").strip()
    try:
        characters: Optional[int] = int(m.group("chars").replace(",", ""))
    except ValueError:
        characters = None

    vndb_id: Optional[str] = None
    haystacks: List[str] = [embed.description or ""]
    for field in embed.fields:
        haystacks.append(field.value or "")
    for hay in haystacks:
        mm = _VNDB_URL_RE.search(hay)
        if mm:
            vndb_id = mm.group(1).lower()
            break

    duration_seconds = 0
    for field in embed.fields:
        if (field.name or "").strip().lower() == "duration":
            duration_seconds = _parse_hms((field.value or "").strip())
            break

    return {
        "kizuna_username": kizuna_username,
        "vn_title": vn_title,
        "vndb_id": vndb_id,
        "characters": characters,
        "duration_seconds": duration_seconds,
    }


class KizunaCog(commands.Cog):
    """Auto-log VN activity from kizuna texthooker webhook posts and manage
    discord-account ↔ kizuna-username links."""

    def __init__(self, bot):
        self.bot = bot
        self.db = Database()
        # Per-bot-run dedup so the unlinked-user hint posts at most once per
        # kizuna username. Reset on cog reload (acceptable trade-off).
        self._unlinked_hint_seen: Set[str] = set()

    @app_commands.command(
        name="kizuna",
        description="Manage your kizuna texthooker auto-log link",
    )
    @app_commands.describe(
        action="What to do (link, unlink, or list your linked kizuna usernames)",
        kizuna_username="Kizuna username (required for link / unlink)",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="link", value="link"),
        app_commands.Choice(name="unlink", value="unlink"),
        app_commands.Choice(name="list", value="list"),
    ])
    async def kizuna(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        kizuna_username: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        action_value = action.value
        if action_value == "list":
            await self._do_list(interaction)
        elif action_value == "link":
            await self._do_link(interaction, kizuna_username)
        elif action_value == "unlink":
            await self._do_unlink(interaction, kizuna_username)

    @kizuna.autocomplete("kizuna_username")
    async def _kizuna_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        # Useful primarily for unlink; harmless for link.
        names = await self.db.list_kizuna_links_for_user(str(interaction.user.id))
        cur = (current or "").strip().casefold()
        if cur:
            names = [n for n in names if cur in n]
        return [app_commands.Choice(name=n, value=n) for n in names[:25]]

    async def _do_link(self, interaction: discord.Interaction, kizuna_username: Optional[str]) -> None:
        normalized = (kizuna_username or "").strip()
        if not normalized:
            await interaction.followup.send(
                "❌ `kizuna_username` is required for the link action.",
                ephemeral=True,
            )
            return

        # A kizuna username comes from a single-line text field, so only reject
        # line breaks / control characters and absurdly long values. Any normal
        # display name (quotes, symbols, non-Latin text) is fine.
        if len(normalized) > 200 or any(ord(c) < 0x20 for c in normalized):
            await interaction.followup.send(
                "❌ That kizuna username isn't valid (it has line breaks or is too "
                "long). Enter it on a single line.",
                ephemeral=True,
            )
            return

        user_id_str = str(interaction.user.id)
        existing = await self.db.get_discord_user_by_kizuna(normalized)
        if existing == user_id_str:
            await interaction.followup.send(
                f"ℹ️ Kizuna user **{normalized}** is already linked to your account.",
                ephemeral=True,
            )
            return
        if existing is not None and existing != user_id_str:
            await interaction.followup.send(
                f"❌ Kizuna user **{normalized}** is already linked to another discord account. "
                f"Ask a server admin if you believe this is wrong.",
                ephemeral=True,
            )
            return

        ok = await self.db.add_kizuna_link(normalized, user_id_str)
        if not ok:
            await interaction.followup.send(
                f"❌ Could not link kizuna user **{normalized}**. Please try again.",
                ephemeral=True,
            )
            return

        self._unlinked_hint_seen.discard(normalized.casefold())
        await interaction.followup.send(
            f"✅ Linked kizuna user **{normalized}** to your account. Future kizuna logs "
            f"will auto-log here too.",
            ephemeral=True,
        )

    async def _do_unlink(self, interaction: discord.Interaction, kizuna_username: Optional[str]) -> None:
        normalized = (kizuna_username or "").strip()
        if not normalized:
            await interaction.followup.send(
                "❌ `kizuna_username` is required for the unlink action.",
                ephemeral=True,
            )
            return

        ok = await self.db.remove_kizuna_link(normalized, str(interaction.user.id))
        if not ok:
            await interaction.followup.send(
                f"❌ No kizuna user **{normalized}** is linked to your account.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ Unlinked kizuna user **{normalized}** from your account.",
            ephemeral=True,
        )

    async def _do_list(self, interaction: discord.Interaction) -> None:
        names = await self.db.list_kizuna_links_for_user(str(interaction.user.id))
        if not names:
            await interaction.followup.send(
                "You have no linked kizuna usernames. Use `/kizuna action:link kizuna_username:<name>` to start auto-logging.",
                ephemeral=True,
            )
            return
        listing = "\n".join(f"• `{n}`" for n in names)
        await interaction.followup.send(
            f"**Your linked kizuna usernames:**\n{listing}",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Only webhook posts are parsed; a kizuna-shaped embed from a member or
        # another bot is ignored.
        if message.webhook_id is None:
            return
        if not message.embeds:
            return
        parsed = _parse_kizuna_embed(message.embeds[0])
        if not parsed:
            return
        await self._handle_kizuna_log(message, parsed)

    async def _handle_kizuna_log(self, message: discord.Message, parsed: Dict[str, Any]) -> None:
        kizuna_username = parsed["kizuna_username"]
        kizuna_key = kizuna_username.casefold()
        discord_user_id = await self.db.get_discord_user_by_kizuna(kizuna_key)

        if not discord_user_id:
            try:
                await message.add_reaction("❓")
            except (discord.Forbidden, discord.HTTPException):
                pass
            if kizuna_key not in self._unlinked_hint_seen:
                self._unlinked_hint_seen.add(kizuna_key)
                try:
                    await message.reply(
                        f"No discord account is linked to kizuna user **{kizuna_username}**. "
                        f"Run `/kizuna action:link kizuna_username:{kizuna_username}` to start auto-logging.",
                        mention_author=False,
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning("Could not send unlinked-kizuna hint: %s", exc)
            return

        guild = message.guild
        if guild is None:
            return

        immersion_cog = self.bot.get_cog("ImmersionCog")
        if immersion_cog is None:
            logger.error("ImmersionCog not loaded; cannot auto-log kizuna webhook")
            return

        vn_lookup = parsed.get("vndb_id") or parsed.get("vn_title")
        if not vn_lookup:
            return

        try:
            activity_id, embed, view, completed_goals, progress_file = await immersion_cog.build_vn_log(
                user_id=str(discord_user_id),
                guild_id=str(guild.id),
                name=vn_lookup,
                characters=parsed.get("characters"),
                duration_seconds=parsed.get("duration_seconds") or 0,
                activity_date=message.created_at,
                comment=None,
                extra_meta={
                    "kizuna_message_id": str(message.id),
                    "kizuna_username": kizuna_username,
                },
            )
        except ValueError as e:
            try:
                await message.add_reaction("⚠️")
            except (discord.Forbidden, discord.HTTPException):
                pass
            try:
                await message.reply(f"⚠️ Could not auto-log: {e}", mention_author=False)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        except Exception as e:
            logger.error("Error auto-logging kizuna webhook: %s", e, exc_info=True)
            return

        try:
            if progress_file:
                await message.reply(embed=embed, view=view, mention_author=False, file=progress_file)
            else:
                await message.reply(embed=embed, view=view, mention_author=False)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Failed to reply to kizuna webhook: %s", exc)
            return

        if completed_goals:
            try:
                goal_embed = immersion_cog._build_goal_completion_embed(completed_goals)
                await message.channel.send(embed=goal_embed)
            except Exception as exc:
                logger.warning("Failed to send goal completion notification: %s", exc)

        member = guild.get_member(int(discord_user_id))
        if member is not None:
            try:
                await immersion_cog._sync_roles_for_member(
                    member=member,
                    channel=message.channel,
                    source="activity_log",
                )
            except Exception as exc:
                logger.warning("kizuna role sync failed: %s", exc)


async def setup(bot):
    await bot.add_cog(KizunaCog(bot))
