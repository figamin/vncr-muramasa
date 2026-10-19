"""
Server structure backup cog.

/manage_server_backups snapshots a server's structure (settings, roles,
channels with permission overwrites, emojis, bans, member roles) to a local
JSON file and can restore it into any server the bot is in, which doubles as
a server-clone tool. Distinct from /manage_backups, which snapshots the
SQLite database.

create also attaches the JSON to the (ephemeral) reply so it can be saved
off-box; restore can take a backup_file attachment instead of an on-disk
backup, so a backup exported from one deployment can be restored on another.

Restoring onto a server run by a different set of bot accounts: member-type
permission overwrites reference bot user IDs from the source server. The
optional old->new map under config.json server_backup.bot_id_map (or the
id_map command parameter, which overrides it) rewrites those IDs on restore.
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import server_backup

logger = logging.getLogger(__name__)

PROGRESS_EDIT_INTERVAL = 2.5  # seconds between progress message edits
# Defensive cap on an uploaded backup read into memory to parse. Discord's
# own attachment limit is lower, so this only guards against a pathological file.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _format_id_map(id_map: dict) -> str:
    if not id_map:
        return "none"
    return "\n".join(f"`{old}` -> `{new}`" for old, new in id_map.items())


def _summary_lines(summary: dict) -> str:
    return (f"**Roles:** {summary['roles']}\n"
            f"**Categories:** {summary['categories']}\n"
            f"**Channels:** {summary['channels']}\n"
            f"**Member overwrites:** {summary['member_overwrites']}\n"
            f"**Emojis:** {summary['emojis']}\n"
            f"**Bans:** {summary['bans']}\n"
            f"**Members recorded:** {summary['members']}")


def _report_embed(report: server_backup.RestoreReport, backup_id: str) -> discord.Embed:
    ok = not report.errors
    embed = discord.Embed(
        title="✅ Server Restore Complete" if ok else "⚠️ Server Restore Finished With Errors",
        description=f"Restored from backup `{backup_id}`.",
        color=discord.Color.green() if ok else discord.Color.orange(),
        timestamp=datetime.now(),
    )
    embed.add_field(
        name="Restored",
        value=(f"**Roles:** {report.roles_created} created, {report.roles_skipped} kept\n"
               f"**Channels:** {report.channels_created} created, {report.channels_skipped} kept\n"
               f"**Threads:** {report.threads_created}\n"
               f"**Member overwrites:** {report.member_overwrites_applied}\n"
               f"**Emojis:** {report.emojis_created} created, {report.emojis_skipped} skipped\n"
               f"**Bans:** {report.bans_restored}\n"
               f"**Members updated:** {report.members_updated} "
               f"({report.members_missing} not in this server)"),
        inline=False,
    )
    if report.overwrites_skipped:
        shown = report.overwrites_skipped[:8]
        more = len(report.overwrites_skipped) - len(shown)
        value = "\n".join(shown) + (f"\n...and {more} more" if more > 0 else "")
        embed.add_field(name="Overwrites skipped (target not found)", value=value[:1024], inline=False)
    if report.errors:
        shown = report.errors[:8]
        more = len(report.errors) - len(shown)
        value = "\n".join(f"• {e}" for e in shown) + (f"\n...and {more} more (see logs)" if more > 0 else "")
        embed.add_field(name="Errors", value=value[:1024], inline=False)
    return embed


class ServerBackupCog(commands.Cog):
    """Full server structure backup, restore and cloning."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._restore_lock = asyncio.Lock()

    def _config_id_map(self) -> dict:
        raw = self.bot.config.get("server_backup", {}).get("bot_id_map", {})
        return {str(k): str(v) for k, v in raw.items()}

    async def backup_id_autocomplete(self, interaction: discord.Interaction, current: str):
        # Reading + parsing every backup file must not block the event loop.
        entries = await asyncio.to_thread(server_backup.list_backups)
        choices = []
        for entry in entries:
            if current and current.lower() not in entry["id"].lower():
                continue
            created = entry["created_at"][:16].replace("T", " ")
            label = f"{entry['id']} ({entry['guild_name']}, {created})"[:100]
            choices.append(app_commands.Choice(name=label, value=entry["id"]))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(
        name="manage_server_backups",
        description="[ADMIN] Back up or restore/clone the full server structure (roles, channels, permissions)",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(
        action="Action to perform",
        backup_id="Backup to use (for info, restore, delete)",
        backup_name="(create) Custom name for the backup file",
        backup_file="(restore) Restore from an uploaded .json backup instead of one on disk",
        clear_first="(restore) Wipe current roles/channels first. Off = only add what's missing (default: on)",
        id_map="(restore) Override bot ID remap, e.g. '111:222,333:444' (default: config bot_id_map)",
        restore_bans="(restore) Re-apply the banned-user list (default: on)",
        restore_members="(restore) Re-apply member roles and nicknames (default: on)",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="create", value="create"),
        app_commands.Choice(name="list", value="list"),
        app_commands.Choice(name="info", value="info"),
        app_commands.Choice(name="restore", value="restore"),
        app_commands.Choice(name="delete", value="delete"),
    ])
    @app_commands.autocomplete(backup_id=backup_id_autocomplete)
    async def manage_server_backups(self, interaction: discord.Interaction, action: str,
                                    backup_id: Optional[str] = None,
                                    backup_name: Optional[str] = None,
                                    backup_file: Optional[discord.Attachment] = None,
                                    clear_first: Optional[bool] = None,
                                    id_map: Optional[str] = None,
                                    restore_bans: Optional[bool] = None,
                                    restore_members: Optional[bool] = None):
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You need administrator permissions to use server backup commands.", ephemeral=True)
            return

        # Validate cheap preconditions before deferring so their errors reply
        # ephemerally (the first followup after a public defer is not ephemeral).
        try:
            if action in ("info", "delete"):
                if not backup_id:
                    raise ValueError("Please specify a backup_id.")
                server_backup.validate_backup_id(backup_id)
            if action == "restore":
                if not backup_id and backup_file is None:
                    raise ValueError("Provide a backup_id, or attach a backup_file to restore from.")
                if backup_id:
                    server_backup.validate_backup_id(backup_id)
                if backup_file is not None:
                    if not backup_file.filename.lower().endswith(".json"):
                        raise ValueError("The attached backup_file must be a .json file.")
                    if backup_file.size > MAX_UPLOAD_BYTES:
                        raise ValueError(f"The attached backup_file is too large "
                                         f"(max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
                if id_map:
                    server_backup.parse_id_map(id_map)
            if action == "create" and backup_name:
                server_backup.validate_backup_id(backup_name)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        # create attaches the backup JSON (member/ban data), so keep it private.
        await interaction.response.defer(ephemeral=(action in ("restore", "create")))

        try:
            if action == "create":
                await self._handle_create(interaction, backup_name)
            elif action == "list":
                await self._handle_list(interaction)
            elif action == "info":
                await self._handle_info(interaction, backup_id)
            elif action == "restore":
                await self._handle_restore(interaction, backup_id, backup_file, clear_first,
                                           id_map, restore_bans, restore_members)
            elif action == "delete":
                await self._handle_delete(interaction, backup_id)
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in server backup command: {e}", exc_info=True)
            await interaction.followup.send(f"❌ An error occurred: {e}", ephemeral=True)

    async def _handle_create(self, interaction: discord.Interaction, backup_name: Optional[str]):
        guild = interaction.guild
        data = await server_backup.create_backup(
            guild, created_by=interaction.user.id, backup_id=backup_name)
        summary = server_backup.summarize_backup(data)
        path = server_backup.backup_path(data["id"])
        file_size = await asyncio.to_thread(lambda: path.stat().st_size)
        size_mb = round(file_size / (1024 * 1024), 2)
        embed = discord.Embed(
            title="✅ Server Backup Created",
            description=f"Backup `{data['id']}` of **{guild.name}** saved ({size_mb} MB).",
            color=discord.Color.green(),
            timestamp=datetime.now(),
        )
        embed.add_field(name="Contents", value=_summary_lines(summary), inline=False)
        embed.add_field(
            name="Restore / clone",
            value="Run `/manage_server_backups action:restore` in the target server.",
            inline=False,
        )
        # Attach the JSON so it can be saved off-box, unless it exceeds the
        # server's upload limit; the copy on disk is authoritative either way.
        # ephemeral=True is required per-followup (it is NOT inherited from the
        # ephemeral defer); the file holds member IDs/nicks/ban reasons.
        if file_size <= guild.filesize_limit:
            file = await asyncio.to_thread(discord.File, str(path), f"{data['id']}.json")
            await interaction.followup.send(embed=embed, file=file, ephemeral=True)
        else:
            limit_mb = guild.filesize_limit // (1024 * 1024)
            embed.add_field(
                name="Download",
                value=f"Too large to attach here (>{limit_mb} MB). Saved on the bot host at "
                      f"`data/server_backups/{data['id']}.json`.",
                inline=False,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    async def _handle_list(self, interaction: discord.Interaction):
        entries = await asyncio.to_thread(server_backup.list_backups)
        if not entries:
            embed = discord.Embed(
                title="📁 Server Backups",
                description="No server backups found. Create one with `/manage_server_backups action:create`.",
                color=discord.Color.orange(),
            )
        else:
            embed = discord.Embed(
                title="📁 Server Backups",
                description=f"Found {len(entries)} backup(s)",
                color=discord.Color.blue(),
                timestamp=datetime.now(),
            )
            for i, entry in enumerate(entries[:10]):
                created = entry["created_at"][:16].replace("T", " ")
                embed.add_field(
                    name=f"{i + 1}. {entry['id']}",
                    value=f"**Server:** {entry['guild_name']}\n"
                          f"**Created:** {created} UTC\n"
                          f"**Size:** {entry['size_mb']} MB",
                    inline=True,
                )
            if len(entries) > 10:
                embed.add_field(name="Note", value=f"Showing 10 of {len(entries)} backups.", inline=False)
        await interaction.followup.send(embed=embed)

    async def _handle_info(self, interaction: discord.Interaction, backup_id: Optional[str]):
        if not backup_id:
            raise ValueError("Please specify a backup_id.")
        data = await asyncio.to_thread(server_backup.load_backup, backup_id)
        summary = server_backup.summarize_backup(data)
        embed = discord.Embed(
            title=f"📄 Server Backup `{data['id']}`",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )
        embed.add_field(
            name="Source",
            value=f"**Server:** {data.get('guild_name')} (`{data.get('guild_id')}`)\n"
                  f"**Created:** {data.get('created_at', '')[:16].replace('T', ' ')} UTC\n"
                  f"**Created by:** <@{data.get('created_by')}>",
            inline=False,
        )
        embed.add_field(name="Contents", value=_summary_lines(summary), inline=False)
        config_map = self._config_id_map()
        embed.add_field(
            name="Configured bot ID map",
            value=_format_id_map(config_map),
            inline=False,
        )
        await interaction.followup.send(embed=embed)

    async def _handle_delete(self, interaction: discord.Interaction, backup_id: Optional[str]):
        if not backup_id:
            raise ValueError("Please specify a backup_id.")
        if await asyncio.to_thread(server_backup.delete_backup, backup_id):
            embed = discord.Embed(
                title="🗑️ Server Backup Deleted",
                description=f"Backup `{backup_id}` was deleted.",
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="❌ Backup Not Found",
                description=f"No backup named `{backup_id}` exists.",
                color=discord.Color.red(),
            )
        await interaction.followup.send(embed=embed)

    async def _handle_restore(self, interaction: discord.Interaction, backup_id: Optional[str],
                              backup_file: Optional[discord.Attachment], clear_first: Optional[bool],
                              id_map: Optional[str],
                              restore_bans: Optional[bool], restore_members: Optional[bool]):
        if self._restore_lock.locked():
            raise ValueError("A server restore is already in progress. Wait for it to finish.")

        guild = interaction.guild
        if backup_file is not None:
            raw = await backup_file.read()
            try:
                # decode() runs inside the thread too, so a file near
                # MAX_UPLOAD_BYTES never blocks the event loop.
                data = await asyncio.to_thread(lambda: json.loads(raw.decode("utf-8")))
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise ValueError("Could not parse the attached file as JSON.")
            server_backup.validate_backup_structure(data)
            source_label = backup_file.filename
        else:
            data = await asyncio.to_thread(server_backup.load_backup, backup_id)
            server_backup.validate_backup_structure(data)
            source_label = backup_id
        effective_map = server_backup.parse_id_map(id_map) if id_map else self._config_id_map()
        options = server_backup.RestoreOptions(
            clear_first=clear_first if clear_first is not None else True,
            id_map=effective_map,
            restore_bans=restore_bans if restore_bans is not None else True,
            restore_member_data=restore_members if restore_members is not None else True,
        )
        summary = server_backup.summarize_backup(data)

        warnings = []
        if options.clear_first:
            warnings.append(f"⚠️ **All {len(guild.roles) - 1} roles and {len(guild.channels)} channels "
                            f"of {guild.name} will be DELETED first.**")
        else:
            warnings.append("Merge mode: existing roles/channels are kept, only missing ones are created.")
        if not guild.me.guild_permissions.administrator:
            warnings.append("⚠️ The bot does not have Administrator here; many steps will likely fail.")
        if "COMMUNITY" in guild.features:
            warnings.append("This is a community server; some channels/settings cannot be cleared or changed.")
        if data.get("guild_id") == str(guild.id):
            warnings.append("Restoring into the same server the backup was taken from.")

        embed = discord.Embed(
            title="⚠️ Confirm Server Restore",
            description=f"Restore backup `{source_label}` (from **{data.get('guild_name')}**) "
                        f"into **{guild.name}**?",
            color=discord.Color.red(),
        )
        embed.add_field(name="Backup contents", value=_summary_lines(summary), inline=False)
        embed.add_field(
            name="Options",
            value=f"**Clear first:** {'yes' if options.clear_first else 'no (merge)'}\n"
                  f"**Restore bans:** {'yes' if options.restore_bans else 'no'}\n"
                  f"**Restore member roles/nicks:** {'yes' if options.restore_member_data else 'no'}\n"
                  f"**Bot ID map:** {_format_id_map(options.id_map)}",
            inline=False,
        )
        embed.add_field(name="Notes", value="\n".join(warnings), inline=False)

        view = ServerRestoreConfirmView(self, source_label, data, options, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def run_restore(self, interaction: discord.Interaction, backup_id: str,
                          data: dict, options: server_backup.RestoreOptions):
        """Execute the restore with throttled progress edits and a DM fallback
        for the final report (the status message channel may be deleted by the
        clear phase)."""
        guild = interaction.guild
        state = {"last_edit": 0.0, "gone": False}

        async def progress(message: str):
            now = time.monotonic()
            if state["gone"] or now - state["last_edit"] < PROGRESS_EDIT_INTERVAL:
                return
            state["last_edit"] = now
            try:
                await interaction.edit_original_response(
                    embed=discord.Embed(
                        title="🔄 Server Restore In Progress",
                        description=message,
                        color=discord.Color.blue(),
                    ),
                    view=None,
                )
            except discord.HTTPException:
                state["gone"] = True

        async with self._restore_lock:
            logger.info("Server restore of %s into %s (%s) started by %s",
                        backup_id, guild.name, guild.id, interaction.user.id)
            try:
                report = await server_backup.restore_backup(guild, data, options, progress)
                final = _report_embed(report, backup_id)
            except Exception as exc:  # noqa: BLE001 - always deliver a final status
                logger.error("Server restore of %s crashed: %s", backup_id, exc, exc_info=True)
                final = discord.Embed(
                    title="❌ Server Restore Failed",
                    description=(f"Restore of `{backup_id}` stopped unexpectedly: {exc}\n\n"
                                 "The server may be partially restored. Check the logs."),
                    color=discord.Color.red(),
                    timestamp=datetime.now(),
                )

        try:
            await interaction.edit_original_response(embed=final, view=None)
        except discord.HTTPException:
            try:
                await interaction.user.send(embed=final)
            except discord.HTTPException:
                logger.error("Could not deliver restore report for %s anywhere", backup_id)


class ServerRestoreConfirmView(discord.ui.View):
    """Confirmation view for a destructive server restore."""

    def __init__(self, cog: ServerBackupCog, backup_id: str, data: dict,
                 options: server_backup.RestoreOptions, requester_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.backup_id = backup_id
        self.data = data
        self.options = options
        self.requester_id = requester_id
        # Guards against a double-click queuing a second destructive restore in
        # the window between the lock check and acquiring it.
        self._consumed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the admin who started this restore can use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Restore", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm_restore(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Sync check-and-set closes the double-click race before any await point.
        if self._consumed or self.cog._restore_lock.locked():
            await interaction.response.send_message(
                "❌ A server restore is already in progress.", ephemeral=True)
            return
        self._consumed = True
        # Views can outlive the command context; re-verify admin before a destructive write.
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member or not member.guild_permissions.administrator:
            self._consumed = False
            await interaction.response.send_message(
                "❌ You need administrator permissions to restore a server backup.", ephemeral=True)
            return

        await interaction.response.defer()
        for item in self.children:
            item.disabled = True
        self.stop()
        await self.cog.run_restore(interaction, self.backup_id, self.data, self.options)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_restore(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="❌ Restore Cancelled",
            description="Server restore was cancelled.",
            color=discord.Color.orange(),
        )
        for item in self.children:
            item.disabled = True
        self.stop()
        await interaction.response.edit_message(embed=embed, view=self)


async def setup(bot):
    await bot.add_cog(ServerBackupCog(bot))
