"""Mass message purge: delete matching messages, with a confirm gate.

The engine (utils/purge_service.py) previews with a fast one-page search estimate, then
on confirm deletes and re-searches continuously (delete-as-you-go) so one run clears the
whole set with no result-size limit; it falls back to walking channel history when search
can't serve the filter. This cog handles start + confirm + on-demand status. The web
console is the live monitor.
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from utils.purge_service import (
    PurgeJobManager,
    PurgeFilters,
    PurgeBusyError,
    PurgeStatus,
    parse_purge_date,
    parse_channel_ids,
)

logger = logging.getLogger(__name__)

# How often to refresh the ephemeral scan-progress message (seconds).
SCAN_PROGRESS_INTERVAL = 3

ChannelArg = Union[
    discord.TextChannel, discord.VoiceChannel, discord.Thread, discord.ForumChannel
]


def format_seconds(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


class MessagePurgeCog(commands.Cog):
    """Slash commands for the mass message purge feature."""

    def __init__(self, bot):
        self.bot = bot
        self.manager = PurgeJobManager(bot=bot)
        # Expose the manager so the web console (separate thread) can drive it.
        bot.purge_manager = self.manager

    # ------------------------------------------------------------------ helpers

    def _resolve_target(self, user: Optional[discord.User], user_id: Optional[str]):
        """Return (target_id:int, display:str) or (None, None). Accepts users who left."""
        if user is not None:
            return user.id, f"{user.mention} ({user.name})"
        if user_id and user_id.strip().isdigit():
            tid = int(user_id.strip())
            return tid, f"<@{tid}> ({tid})"
        return None, None

    @staticmethod
    def _selection_display(target_display, directed_display) -> str:
        """Human phrase for what a purge targets: 'by X', 'directed at Y', both, or a
        generic phrase when there is no user filter (scope/date/content only)."""
        if target_display and directed_display:
            return f"by {target_display} directed at {directed_display}"
        if target_display:
            return f"by {target_display}"
        if directed_display:
            return f"directed at {directed_display}"
        return "matching your filters"

    def _scanning_embed(self, status: dict, selection) -> discord.Embed:
        embed = discord.Embed(
            title="🔍 Checking how many messages match…",
            description=(
                f"Estimating messages {selection}. This is quick: deletion starts as soon "
                "as you confirm and works through them live (no full pre-scan)."
            ),
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        if status.get("current_channel"):
            embed.add_field(name="Currently checking", value=status["current_channel"], inline=False)
        embed.set_footer(text="A preview with a confirm button appears in a moment.")
        return embed

    def _preview_embed(self, job, selection, channel) -> discord.Embed:
        flt = job.filters
        estimate = getattr(job, "is_estimate", False)
        scope_txt = channel.mention if channel else "Entire server (all channels + threads)"
        count_txt = f"about **{job.matched}**" if estimate else f"**{job.matched}**"
        embed = discord.Embed(
            title="⚠️ Confirm message purge",
            description=(
                f"About to delete {count_txt} message(s) {selection}.\n"
                "This is **irreversible**. Review the preview, then confirm."
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Scope", value=scope_txt, inline=False)
        embed.add_field(name="Est. matches" if estimate else "Matched", value=str(job.matched), inline=True)
        embed.add_field(name="Est. delete time", value=f"~{format_seconds(job.estimated_seconds)}", inline=True)
        if estimate:
            embed.add_field(
                name="How it runs",
                value=("The count is Discord's search estimate. On confirm, messages are "
                       "deleted and re-searched by ID until none remain (no result-size "
                       "limit). Discord's search can under-return on very large/old sets, "
                       "so a re-run may be needed to catch the last stragglers."),
                inline=False)

        active_filters = []
        # Show the full UTC instant, not just the date: a bare date is 00:00 UTC, so
        # a `before` filter excludes the whole of that UTC day, which is easy to miss
        # for admins who read Discord in local time.
        if flt.after:
            active_filters.append(f"after {flt.after:%Y-%m-%d %H:%M} UTC")
        if flt.before:
            active_filters.append(f"before {flt.before:%Y-%m-%d %H:%M} UTC")
        if flt.contains:
            active_filters.append(f"contains `{flt.contains}`")
        if flt.regex:
            active_filters.append(f"regex `{flt.regex}`")
        if flt.has_link:
            active_filters.append("has link")
        if flt.has_file:
            active_filters.append("has file")
        if flt.include_pinned:
            active_filters.append("incl. pinned")
        # NSFW is included by default, so the noteworthy case is it being turned OFF.
        if not flt.include_nsfw:
            active_filters.append("excluding NSFW channels")
        if flt.excluded_channel_ids:
            active_filters.append(f"excluding {len(flt.excluded_channel_ids)} channel(s)")
        if active_filters:
            embed.add_field(name="Filters", value=", ".join(active_filters), inline=False)
        embed.add_field(name="Delete order",
                        value="oldest first (subtle)" if flt.oldest_first else "newest first",
                        inline=False)
        # Spell out the thread policy: deleting a private thread wholesale also removes
        # anyone else's messages inside it, and it is on by default, so it must be visible
        # on the confirm gate rather than buried in an option nobody re-reads.
        if flt.skip_private_threads:
            thread_note = "Private threads are left untouched."
        elif flt.delete_private_threads and flt.target_user_id is not None:
            thread_note = ("Private threads **created by this user will be deleted whole** "
                           "(one call instead of thousands), which also removes anyone "
                           "else's messages inside them. Public threads are purged "
                           "message-by-message as normal.")
        else:
            thread_note = "All threads are purged message-by-message."
        embed.add_field(name="Threads", value=thread_note, inline=False)

        if job.preview:
            lines = []
            for p in job.preview[:8]:
                snippet = (p["snippet"] or "").replace("\n", " ")
                lines.append(f"`{p['timestamp'][:16]}` #{p['channel']}: {snippet[:80]}")
            embed.add_field(name="Sample", value="\n".join(lines)[:1024], inline=False)

        embed.set_footer(text="Preview expires in ~15 minutes")
        return embed

    def _status_embed(self, status: dict) -> discord.Embed:
        state = status.get("status", PurgeStatus.IDLE)
        if state == PurgeStatus.IDLE:
            return discord.Embed(
                title="🧹 Purge status",
                description="No purge job is running.",
                color=discord.Color.blue(),
            )
        colors = {
            PurgeStatus.SCANNING: discord.Color.gold(),
            PurgeStatus.AWAITING: discord.Color.orange(),
            PurgeStatus.DELETING: discord.Color.red(),
            PurgeStatus.COMPLETED: discord.Color.green(),
            PurgeStatus.CANCELLED: discord.Color.greyple(),
            PurgeStatus.FAILED: discord.Color.dark_red(),
            PurgeStatus.EXPIRED: discord.Color.greyple(),
        }
        embed = discord.Embed(
            title="🧹 Purge status",
            description=f"**State:** {state}",
            color=colors.get(state, discord.Color.blue()),
            timestamp=datetime.now(timezone.utc),
        )
        who = []
        if status.get("target_user_id"):
            who.append(f"by <@{status['target_user_id']}>")
        if status.get("directed_at_user_id"):
            who.append(f"directed at <@{status['directed_at_user_id']}>")
        embed.add_field(name="Target", value=" ".join(who) or "any author", inline=True)
        embed.add_field(name="Scope", value=status.get("scope", "?"), inline=True)
        embed.add_field(name="Scanned", value=f"{status['messages_scanned']} / {status['channels_scanned']} ch", inline=True)
        embed.add_field(name="Matched", value=str(status["matched"]), inline=True)
        embed.add_field(name="Deleted", value=str(status["deleted"]), inline=True)
        embed.add_field(name="Failed", value=str(status["failed"]), inline=True)
        if status.get("current_channel"):
            embed.add_field(name="Current", value=status["current_channel"], inline=False)
        if status.get("error"):
            embed.add_field(name="Error", value=str(status["error"])[:1024], inline=False)
        return embed

    async def _run_delete_safe(self, job_id):
        try:
            await self.manager.run_delete(job_id)
        except Exception as e:
            logger.error("Purge delete task %s failed: %s", job_id, e)

    # ------------------------------------------------------------------ commands

    @app_commands.command(name="manage_purge", description="[ADMIN] Mass-delete messages by author, target, channel, date or content (dry-run first)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.choices(action=[
        app_commands.Choice(name="start - preview messages to delete", value="start"),
        app_commands.Choice(name="status - show the current/last job", value="status"),
        app_commands.Choice(name="cancel - stop the running job", value="cancel"),
    ])
    @app_commands.describe(
        action="What to do (default: start a new purge)",
        user="[start] Only delete messages by this author",
        user_id="[start] Author user ID, for an author who left the server",
        directed_at="[start] Delete posts that mention or reply to this user (any author)",
        directed_at_id="[start] Like directed_at, by user ID (for someone who left)",
        channel="[start] Limit to one channel (default: entire server incl. threads)",
        after="[start] Only messages after this UTC date (YYYY-MM-DD = 00:00 UTC that day)",
        before="[start] Only messages before this UTC date (YYYY-MM-DD = 00:00 UTC, so that day is excluded)",
        contains="[start] Messages containing this word/phrase (fast); use regex for mid-word substrings",
        regex="[start] Regex match (case-insensitive; slower full-history scan)",
        has_link="[start] Only messages containing a link",
        has_file="[start] Only messages with an attachment",
        include_pinned="[start] Also delete pinned messages",
        include_nsfw="[start] Scan NSFW channels (ON by default; set false to skip them)",
        oldest_first="[start] Delete oldest matches first (more subtle), instead of newest first",
        exclude_channels="[start] Channel/thread IDs to never touch (comma-separated)",
        delete_private_threads="[start] If the target created a PRIVATE thread, delete the whole thread (ON by default; one call vs thousands, but also removes others' messages in it)",
        skip_private_threads="[start] Leave private threads alone (their contents are invisible to non-members anyway)",
    )
    async def purge(
        self,
        interaction: discord.Interaction,
        action: str = "start",
        user: Optional[discord.User] = None,
        user_id: Optional[str] = None,
        directed_at: Optional[discord.User] = None,
        directed_at_id: Optional[str] = None,
        channel: Optional[ChannelArg] = None,
        after: Optional[str] = None,
        before: Optional[str] = None,
        contains: Optional[str] = None,
        regex: Optional[str] = None,
        has_link: bool = False,
        has_file: bool = False,
        include_pinned: bool = False,
        include_nsfw: bool = True,
        oldest_first: bool = False,
        exclude_channels: Optional[str] = None,
        delete_private_threads: bool = True,
        skip_private_threads: bool = False,
    ):
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need administrator permissions.", ephemeral=True)
            return

        if action == "status":
            await interaction.response.send_message(
                embed=self._status_embed(self.manager.get_status()), ephemeral=True
            )
            return
        if action == "cancel":
            await self._do_cancel(interaction)
            return

        # action == "start": dry-run scan, then a confirm gate
        target_id, target_display = self._resolve_target(user, user_id)
        directed_id, directed_display = self._resolve_target(directed_at, directed_at_id)

        try:
            after_dt = parse_purge_date(after)
            before_dt = parse_purge_date(before)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        # A purge must be bounded so it can never match every message in the server.
        # A specific channel is a bound on its own; a server-wide scope needs at least
        # one narrowing filter (author, directed-at, date, or content).
        has_bound = channel is not None or any((
            target_id is not None, directed_id is not None,
            after_dt is not None, before_dt is not None,
            contains, regex, has_link, has_file,
        ))
        if not has_bound:
            await interaction.response.send_message(
                "❌ Add at least one filter so the purge is bounded: a `channel`, a "
                "`user`/`directed_at`, an `after`/`before` date, or a content filter "
                "(`contains`, `regex`, `has_link`, `has_file`).",
                ephemeral=True,
            )
            return
        selection_display = self._selection_display(target_display, directed_display)

        try:
            filters = PurgeFilters(
                target_user_id=target_id,
                directed_at_user_id=directed_id,
                contains=contains,
                regex=regex,
                has_link=has_link,
                has_file=has_file,
                include_pinned=include_pinned,
                after=after_dt,
                before=before_dt,
                include_nsfw=include_nsfw,
                oldest_first=oldest_first,
                excluded_channel_ids=parse_channel_ids(exclude_channels),
                delete_private_threads=delete_private_threads,
                skip_private_threads=skip_private_threads,
            )
        except re.error as e:
            await interaction.response.send_message(f"❌ Invalid regex: {e}", ephemeral=True)
            return
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        scope = channel.id if channel else "guild"
        await interaction.response.defer(ephemeral=True)
        if self.manager.is_busy():
            await interaction.edit_original_response(
                content="❌ A purge is already running. Use `/manage_purge action:status`.")
            return

        # Run the scan in the background and live-update the ephemeral with progress,
        # so the user sees activity instead of a silent "thinking" state.
        scan_task = asyncio.create_task(self.manager.run_dry_run(
            scope=scope, guild_id=interaction.guild_id, filters=filters,
            requested_by=interaction.user.id, requested_via="slash"))
        await interaction.edit_original_response(
            embed=self._scanning_embed(self.manager.get_status(), selection_display))
        while not scan_task.done():
            await asyncio.sleep(SCAN_PROGRESS_INTERVAL)
            status = self.manager.get_status()
            if status.get("status") == PurgeStatus.SCANNING:
                try:
                    await interaction.edit_original_response(
                        embed=self._scanning_embed(status, selection_display))
                except discord.HTTPException:
                    pass

        try:
            job = await scan_task
        except PurgeBusyError:
            await interaction.edit_original_response(
                content="❌ A purge is already running. Use `/manage_purge action:status`.", embed=None)
            return
        except Exception as e:
            logger.error("Purge dry-run failed: %s", e)
            await interaction.edit_original_response(content=f"❌ Scan failed: {e}", embed=None)
            return

        if job.status == PurgeStatus.CANCELLED:
            await interaction.edit_original_response(content="🛑 Purge cancelled.", embed=None)
            return

        if job.matched == 0:
            self.manager.abandon(job.id)
            await interaction.edit_original_response(
                content="No matching messages found. Nothing to delete.", embed=None)
            return

        embed = self._preview_embed(job, selection_display, channel)
        view = PurgeConfirmView(self, job.id, interaction.user.id)
        await interaction.edit_original_response(embed=embed, view=view)

    async def _do_cancel(self, interaction: discord.Interaction):
        status = self.manager.get_status()
        job_id = status.get("id")
        state = status.get("status")
        if state == PurgeStatus.AWAITING:
            ok = self.manager.abandon(job_id)
        elif state in (PurgeStatus.SCANNING, PurgeStatus.DELETING):
            ok = self.manager.cancel(job_id)
        else:
            ok = False
        msg = "🛑 Cancellation requested." if ok else "Nothing is running to cancel."
        await interaction.response.send_message(msg, ephemeral=True)


class PurgeConfirmView(discord.ui.View):
    """Confirm/Cancel gate shown after a dry-run, restricted to the requester."""

    def __init__(self, cog: MessagePurgeCog, job_id, requester_id):
        super().__init__(timeout=900)  # ~ preview TTL
        self.cog = cog
        self.job_id = job_id
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the admin who started this purge can use these buttons.", ephemeral=True
            )
            return False
        return True

    def _disable(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Confirm delete", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if not self.cog.manager.can_confirm(self.job_id):
            self._disable()
            await interaction.edit_original_response(
                content="Preview expired or no longer valid. Run `/manage_purge` again.", view=self
            )
            return
        interaction.client.loop.create_task(self.cog._run_delete_safe(self.job_id))
        self._disable()
        embed = discord.Embed(
            title="🧹 Purge started",
            description=(
                "Deletion is running in the background. Track it with `/manage_purge action:status` "
                "or the web console. Use `/manage_purge action:cancel` to stop."
            ),
            color=discord.Color.red(),
        )
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.cog.manager.abandon(self.job_id)
        self._disable()
        embed = discord.Embed(
            title="Purge cancelled",
            description="No messages were deleted.",
            color=discord.Color.greyple(),
        )
        await interaction.response.edit_message(embed=embed, view=self)


async def setup(bot):
    await bot.add_cog(MessagePurgeCog(bot))
