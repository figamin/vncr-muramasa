"""
Media flag / review cog for muramasa.

When the message-filter pipeline reposts a non-exempt member's visual media, the
repost's ⋯ menu (in message_filter's RepostMenuView) offers a ✅ / 🚩 review vote to
anyone; clicking it calls on_vote_item here, which records the vote directly and
replaces the ephemeral menu with the result. Every 🚩 flag posts a heads-up to the
mod-logs channel naming who flagged it, so mods can weigh each flagger's judgment.
Log-only: nothing is auto-deleted or actioned.

Reposts made before the single-menu change carry the old ✅/🚩 buttons, which route
to on_vote (an ephemeral Confirm/Cancel, then finalize_vote) and keep working across
restarts. on_vote_item and finalize_vote share the same record + dedup + report path.

Members can exempt themselves from media reposting with /filter; admins manage the
same list via /manage_mediareview and the web console. The opt-out only suppresses
the media path: a message that trips a word-filter rule is still cleaned and reposted.
"""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import Database
from utils.media_detect import message_has_visual_media

logger = logging.getLogger(__name__)


class MediaFlagConfirmView(discord.ui.View):
    """Ephemeral confirm/cancel shown after a flag button is clicked, so a misclick
    doesn't record a vote. The vote is only committed if the user confirms. Transient
    (created per click, only the clicker sees it), so it needs no persistence."""

    def __init__(self, cog, item: dict, vote: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.item = item
        self.vote = vote
        self.message = None  # set by the caller so on_timeout can disable the buttons

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content="⌛ Confirmation expired. Click the button again to vote.", view=self
                )
            except Exception:
                pass

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.finalize_vote(interaction, self.item, self.vote)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


class MediaFlagCog(commands.Cog):
    """Community flag → mod heads-up. Owns the buttons, votes, report, and config.
    Never reposts (the message-filter pipeline does that and attaches the view)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = Database()
        # Cache of (settings, excluded_role_ids) for the per-message should_attach check,
        # reloaded only when the media-flag config version bumps.
        self._attach_cfg = None
        self._attach_cfg_version = -1

    async def cog_load(self):
        await self.db.initialize()
        # The ✅/🚩 review vote is offered by the message-filter cog's repost menu (new reposts
        # via on_vote_item; pre-menu reposts via the old mediaflag:safe/flag buttons -> on_vote).
        logger.info("Media flag cog loaded")

    async def should_attach(self, message: discord.Message) -> bool:
        """Called by the filter pipeline on every message: should this repost carry the
        flag buttons? Has visual media + enabled + poster not opted out and not in an
        excluded role. Config is cached and reloaded only when it changes (no
        per-message DB read)."""
        if not message_has_visual_media(message):
            return False
        settings, excluded, opted_out = await self._attach_config()
        if not settings.get("enabled"):
            return False
        member = message.author
        if member.id in opted_out:
            return False
        if excluded and isinstance(member, discord.Member) and {r.id for r in member.roles} & excluded:
            return False
        return True

    async def _attach_config(self):
        """(settings, excluded_role_ids, opted_out_user_ids) for should_attach, cached until
        the media-flag config version bumps (on enable/disable, an excluded-role change, or
        an opt-out change)."""
        version = self.db.get_media_flag_config_version()
        if self._attach_cfg is None or version != self._attach_cfg_version:
            settings = await self.db.get_media_flag_settings()
            excluded = set(await self.db.get_media_flag_excluded_roles())
            opted_out = set(await self.db.get_media_flag_optout_user_ids())
            self._attach_cfg = (settings, excluded, opted_out)
            self._attach_cfg_version = version
        return self._attach_cfg

    async def register_item(self, source_message: discord.Message, reposted_message: discord.Message):
        """Record a reposted media item so its button clicks can be tracked."""
        try:
            await self.db.add_media_flag_item(
                source_message.channel.id, source_message.id,
                reposted_message.channel.id, reposted_message.id,
                source_message.author.id,
            )
        except Exception as exc:
            logger.warning("Failed to register media flag item: %s", exc)

    async def on_vote(self, interaction: discord.Interaction, vote: str):
        """A flag button was clicked: validate, then ask for confirmation. The vote is
        not recorded until the user confirms (see finalize_vote)."""
        item = await self.db.get_media_flag_item_by_repost(interaction.message.id)
        if not item:
            await interaction.response.send_message("This item is no longer tracked.", ephemeral=True)
            return
        if await self._flag_is_ignored(interaction.user, vote):
            await interaction.response.send_message("Noted, but your flag isn't counted.", ephemeral=True)
            return
        prompt = "🚩 Flag this media for the mods?" if vote == "flag" else "✅ Mark this media as safe?"
        view = MediaFlagConfirmView(self, item, vote)
        await interaction.response.send_message(prompt, view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except Exception:
            pass

    async def finalize_vote(self, interaction: discord.Interaction, item: dict, vote: str):
        """Confirmed: record the vote and ack the clicker. A flag posts a mod-log
        heads-up naming the flagger, but only the first time this user flags this item,
        so toggling a vote (flag -> safe -> flag) can't spam the log channel."""
        await self.db.record_media_flag_vote(item["id"], interaction.user.id, vote)
        await interaction.response.edit_message(
            content="🚩 Flagged for the mods." if vote == "flag" else "✅ Marked safe.", view=None
        )
        if vote == "flag" and await self.db.claim_media_flag_report(item["id"], interaction.user.id):
            counts = await self.db.count_media_flag_votes(item["id"])
            settings = await self.db.get_media_flag_settings()
            await self._post_flag_report(interaction, item, counts, settings)

    async def on_vote_item(self, interaction: discord.Interaction, item: dict, vote: str):
        """Record a media-review vote straight from a repost's ephemeral action menu. The
        menu-open is already the deliberate step, so there's no extra confirm (unlike the legacy
        on_vote button flow). Runs the ignored-role check that on_vote does up front, then defers
        to finalize_vote so the record + edit + one-report-per-flagger dedup stays single-sourced."""
        if await self._flag_is_ignored(interaction.user, vote):
            await interaction.response.edit_message(content="Noted, but your flag isn't counted.", view=None)
            return
        await self.finalize_vote(interaction, item, vote)

    async def _flag_is_ignored(self, member, vote: str) -> bool:
        """True if a 'flag' vote shouldn't count because the member holds an ignored role. 'safe'
        always counts. Single-sourced so on_vote and on_vote_item can't drift on the rule."""
        if vote != "flag" or not isinstance(member, discord.Member):
            return False
        ignored = set(await self.db.get_media_flag_ignored_roles())
        return bool(ignored and {r.id for r in member.roles} & ignored)

    async def _post_flag_report(self, interaction: discord.Interaction, item: dict,
                                counts: dict, settings: dict):
        """Post a fresh mod-log heads-up for this single flag, naming the flagger."""
        log_channel_id = settings.get("log_channel_id")
        if not log_channel_id:
            return
        guild = interaction.guild
        channel = guild.get_channel(int(log_channel_id)) if guild else None
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(log_channel_id))
            except Exception as exc:
                logger.warning("Media flag log channel %s not accessible: %s", log_channel_id, exc)
                return
        embed = self._build_report_embed(interaction, item, counts)
        try:
            await channel.send(embed=embed)
        except Exception as exc:
            logger.warning("Failed to post media flag report: %s", exc)

    def _build_report_embed(self, interaction: discord.Interaction, item: dict, counts: dict) -> discord.Embed:
        jump = f"https://discord.com/channels/{interaction.guild_id}/{item['repost_channel_id']}/{item['repost_message_id']}"
        flagger = interaction.user
        embed = discord.Embed(title="🚩 Media flagged for review", color=discord.Color.orange(),
                              timestamp=discord.utils.utcnow())
        embed.add_field(name="Flagged by", value=f"{flagger.mention} ({flagger})", inline=False)
        embed.add_field(name="Poster", value=f"<@{item['author_id']}> ({item['author_id']})", inline=False)
        embed.add_field(name="Where", value=f"<#{item['source_channel_id']}> · [jump to media]({jump})", inline=False)
        embed.add_field(name="Votes so far", value=f"🚩 {counts['flag']} · ✅ {counts['safe']}", inline=False)
        return embed

    async def _ensure_admin(self, interaction: discord.Interaction) -> bool:
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member or not member.guild_permissions.administrator:
            msg = "❌ You need administrator permissions to use this command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return False
        return True

    @app_commands.command(name="filter", description="Choose whether your images, GIFs and videos get reposted for review")
    @app_commands.describe(action="Opt out, opt back in, or check your current setting.")
    @app_commands.choices(action=[
        app_commands.Choice(name="opt_out", value="opt_out"),
        app_commands.Choice(name="opt_in", value="opt_in"),
        app_commands.Choice(name="status", value="status"),
    ])
    @app_commands.guild_only()
    async def media_optout(self, interaction: discord.Interaction, action: str):
        """Self-service opt-out. Members manage only their own setting here; the list
        itself is managed by admins via /manage_mediareview and the web console."""
        user_id = interaction.user.id
        opted_out = await self.db.is_media_flag_opted_out(user_id)
        if action == "opt_out":
            if opted_out:
                msg = "You're already opted out. Your media isn't reposted for review."
            else:
                await self.db.add_media_flag_optout_user(user_id, added_by="self")
                msg = ("✅ Opted out. Your images, GIFs and videos stay as you posted them.\n"
                       "-# The word filter is separate: a message with a filtered word is still cleaned and reposted.")
        elif action == "opt_in":
            if not opted_out:
                msg = "You're not opted out, so there's nothing to undo."
            else:
                await self.db.remove_media_flag_optout_user(user_id)
                msg = "✅ Opted back in. Your media can be reposted for review again."
        elif action == "status":
            msg = ("**Opted out.** Your media isn't reposted for review. Use `/filter action:opt_in` to undo."
                   if opted_out else
                   "**Opted in** (the default). Your media can be reposted for review. Use `/filter action:opt_out` to stop that.")
        else:
            msg = "❌ Unknown action."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="manage_mediareview", description="[ADMIN] Manage media flag/review")
    @app_commands.describe(
        action="What to do.",
        channel="(set_log_channel) Channel that receives flag heads-ups.",
        role="(exclude_role/include_role) poster role exempt from reposting; (ignore_role/unignore_role) role whose flags don't count.",
        user="(optout_user/optin_user) member whose media is exempt from reposting.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="enable", value="enable"),
        app_commands.Choice(name="disable", value="disable"),
        app_commands.Choice(name="set_log_channel", value="set_log_channel"),
        app_commands.Choice(name="exclude_role", value="exclude_role"),
        app_commands.Choice(name="include_role", value="include_role"),
        app_commands.Choice(name="ignore_role", value="ignore_role"),
        app_commands.Choice(name="unignore_role", value="unignore_role"),
        app_commands.Choice(name="optout_user", value="optout_user"),
        app_commands.Choice(name="optin_user", value="optin_user"),
        app_commands.Choice(name="list_optouts", value="list_optouts"),
        app_commands.Choice(name="status", value="status"),
    ])
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def manage_mediareview(self, interaction: discord.Interaction, action: str,
                                 channel: Optional[discord.TextChannel] = None,
                                 role: Optional[discord.Role] = None,
                                 user: Optional[discord.User] = None):
        if not await self._ensure_admin(interaction):
            return
        if action == "enable":
            await self.db.set_media_flag_enabled(True)
            await interaction.response.send_message("✅ Media review enabled.", ephemeral=True)
        elif action == "disable":
            await self.db.set_media_flag_enabled(False)
            await interaction.response.send_message("✅ Media review disabled.", ephemeral=True)
        elif action == "set_log_channel":
            if channel is None:
                await interaction.response.send_message("❌ Provide a `channel`.", ephemeral=True); return
            await self.db.set_media_flag_log_channel(channel.id)
            await interaction.response.send_message(f"✅ Flag heads-ups will go to {channel.mention}.", ephemeral=True)
        elif action in ("exclude_role", "include_role", "ignore_role", "unignore_role"):
            if role is None:
                await interaction.response.send_message("❌ Provide a `role`.", ephemeral=True); return
            if action == "exclude_role":
                await self.db.add_media_flag_excluded_role(role.id, added_by=str(interaction.user.id))
                msg = f"✅ {role.mention} is exempt from media reposting."
            elif action == "include_role":
                await self.db.remove_media_flag_excluded_role(role.id)
                msg = f"✅ {role.mention} is no longer exempt."
            elif action == "ignore_role":
                await self.db.add_media_flag_ignored_role(role.id, added_by=str(interaction.user.id))
                msg = f"✅ Flags from {role.mention} will be ignored."
            else:
                await self.db.remove_media_flag_ignored_role(role.id)
                msg = f"✅ Flags from {role.mention} count again."
            await interaction.response.send_message(msg, ephemeral=True)
        elif action in ("optout_user", "optin_user"):
            if user is None:
                await interaction.response.send_message("❌ Provide a `user`.", ephemeral=True); return
            if action == "optout_user":
                await self.db.add_media_flag_optout_user(user.id, added_by=str(interaction.user.id))
                msg = f"✅ {user.mention}'s media is no longer reposted for review."
            else:
                await self.db.remove_media_flag_optout_user(user.id)
                msg = f"✅ {user.mention}'s media can be reposted for review again."
            await interaction.response.send_message(msg, ephemeral=True)
        elif action == "list_optouts":
            rows = await self.db.get_media_flag_optout_users()
            if not rows:
                body = "Nobody has opted out."
            else:
                # Mentions only, capped: a long list would otherwise blow the 2000-char limit.
                shown = rows[:40]
                body = "\n".join(f"<@{r['user_id']}> ({r['user_id']})" for r in shown)
                if len(rows) > len(shown):
                    body += f"\n… and {len(rows) - len(shown)} more"
            await interaction.response.send_message(
                f"**Opted out of media reposting ({len(rows)}):**\n{body}", ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())
        elif action == "status":
            s = await self.db.get_media_flag_settings()
            excl = await self.db.get_media_flag_excluded_roles()
            ign = await self.db.get_media_flag_ignored_roles()
            opt = await self.db.get_media_flag_optout_user_ids()
            lc = f"<#{s['log_channel_id']}>" if s.get("log_channel_id") else "(none)"
            await interaction.response.send_message(
                f"**Media review:** {'on' if s['enabled'] else 'off'} · every 🚩 flag is logged · log {lc}\n"
                f"Excluded poster roles: {len(excl)} · Ignored flagger roles: {len(ign)} · "
                f"Opted-out members: {len(opt)}", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Unknown action.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MediaFlagCog(bot))
