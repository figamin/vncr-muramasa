import io
import json
import logging
import traceback
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import Database

logger = logging.getLogger(__name__)

IGNORED_APP_ERRORS = (
    app_commands.CheckFailure,
    app_commands.CommandAlreadyRegistered,
    app_commands.CommandNotFound,
    app_commands.CommandSignatureMismatch,
    app_commands.CommandOnCooldown,
    app_commands.MissingPermissions,
    app_commands.BotMissingPermissions,
    app_commands.MissingRole,
    app_commands.MissingAnyRole,
    app_commands.NoPrivateMessage,
)

IGNORED_COMMAND_ERRORS = (
    commands.CheckFailure,
    commands.CommandNotFound,
    commands.CommandOnCooldown,
    commands.MissingPermissions,
    commands.BotMissingPermissions,
    commands.MissingRole,
    commands.MissingAnyRole,
    commands.NoPrivateMessage,
)


class ErrorLoggingCog(commands.Cog):
    """Cog providing configurable error logging to Discord channels."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = Database()
        self._previous_tree_error_handler = None

    async def cog_load(self):
        await self.db.initialize()
        self._previous_tree_error_handler = self.bot.tree.on_error
        self.bot.tree.on_error = self._app_command_error_wrapper
        logger.info("Error logging cog loaded")

    async def cog_unload(self):
        if self.bot.tree.on_error == self._app_command_error_wrapper:
            self.bot.tree.on_error = self._previous_tree_error_handler
        logger.info("Error logging cog unloaded")

    async def _app_command_error_wrapper(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        handled = await self._handle_interaction_error(interaction, error)

        if self._previous_tree_error_handler and self._previous_tree_error_handler != self._app_command_error_wrapper:
            try:
                await self._previous_tree_error_handler(interaction, error)
            except Exception as secondary_error:
                logger.error("Error invoking previous app command error handler: %s", secondary_error)

        if not handled:
            logger.debug("App command error ignored by logging cog: %s", error)

    def _should_ignore(self, error: Exception) -> bool:
        original = getattr(error, "original", error)
        return isinstance(original, (*IGNORED_APP_ERRORS, *IGNORED_COMMAND_ERRORS))

    async def _handle_interaction_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> bool:
        if self._should_ignore(error):
            return False

        await self._notify_interaction_user(interaction)
        await self._log_error(
            guild=interaction.guild,
            origin_channel=interaction.channel,
            user=interaction.user,
            command_name=self._get_interaction_command_name(interaction),
            options=self._get_interaction_options(interaction),
            error=error,
        )
        return True

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if self._should_ignore(error):
            return

        await self._notify_prefix_user(ctx)
        if ctx.command:
            command_display = f"{ctx.prefix}{ctx.command.qualified_name}" if ctx.prefix else ctx.command.qualified_name
        else:
            command_display = ctx.message.content.split("\n", 1)[0]

        message_preview = ctx.message.content
        if len(message_preview) > 800:
            message_preview = message_preview[:797] + "..."

        await self._log_error(
            guild=ctx.guild,
            origin_channel=ctx.channel,
            user=ctx.author,
            command_name=command_display,
            options={"message": message_preview},
            error=error,
        )

    async def _notify_interaction_user(self, interaction: discord.Interaction):
        message = "❌ Something went wrong while running this command. The admins have been notified."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception as notify_error:
            logger.debug("Failed to notify interaction user about error: %s", notify_error)

    async def _notify_prefix_user(self, ctx: commands.Context):
        try:
            await ctx.send("❌ Something went wrong while running this command. The admins have been notified.")
        except Exception as notify_error:
            logger.debug("Failed to notify prefix command user about error: %s", notify_error)

    def _get_interaction_command_name(self, interaction: discord.Interaction) -> str:
        if interaction.command:
            return interaction.command.qualified_name
        data = interaction.data or {}
        return data.get("name", "unknown_command")

    def _get_interaction_options(self, interaction: discord.Interaction) -> Optional[dict]:
        data = interaction.data or {}
        options = data.get("options")
        if not options:
            return None
        try:
            return {opt.get("name"): opt.get("value") for opt in options}
        except Exception:
            return data

    async def _log_error(
        self,
        guild: Optional[discord.Guild],
        origin_channel: Optional[discord.abc.GuildChannel],
        user: discord.abc.User,
        command_name: str,
        options: Optional[dict],
        error: Exception,
    ):
        if not guild:
            logger.warning("Error logging skipped because no guild was associated with the command.")
            return

        channel_record = await self.db.get_guild_error_log_channel(guild.id)
        if not channel_record:
            logger.info("No error log channel configured for guild %s", guild.id)
            return

        log_channel = guild.get_channel(channel_record["channel_id"])
        if log_channel is None:
            try:
                log_channel = await guild.fetch_channel(channel_record["channel_id"])
            except Exception as fetch_error:
                logger.error("Configured error log channel %s not accessible: %s", channel_record["channel_id"], fetch_error)
                return

        embed = discord.Embed(
            title="🚨 Command Error Encountered",
            color=discord.Color.red(),
            timestamp=datetime.utcnow()
        )

        embed.add_field(name="Command", value=command_name or "Unknown", inline=False)
        embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
        if origin_channel:
            if isinstance(origin_channel, discord.TextChannel):
                location = f"{origin_channel.mention} (`{origin_channel.id}`)"
            else:
                location = f"{origin_channel} (`{origin_channel.id}`)"
        else:
            location = "Direct Message or Unknown"
        embed.add_field(name="Channel", value=location, inline=False)

        original = getattr(error, "original", error)
        embed.add_field(name="Error Type", value=original.__class__.__name__, inline=True)
        embed.add_field(name="Error Message", value=str(original) or "(no message)", inline=False)

        if options:
            formatted_options = json.dumps(options, indent=2, ensure_ascii=False)
            if len(formatted_options) > 1024:
                formatted_options = formatted_options[:1021] + "..."
            embed.add_field(name="Command Options", value=f"```json\n{formatted_options}\n```", inline=False)

        stack_trace = "".join(traceback.format_exception(type(original), original, original.__traceback__))
        trace_file = None
        if stack_trace:
            trace_bytes = stack_trace.encode("utf-8", errors="ignore")
            trace_file = discord.File(io.BytesIO(trace_bytes), filename=f"error_{guild.id}_{int(datetime.utcnow().timestamp())}.log")
            if len(stack_trace) <= 1000:
                embed.add_field(name="Traceback", value=f"```\n{stack_trace}\n```", inline=False)
            else:
                embed.add_field(name="Traceback", value="Traceback attached as file.", inline=False)

        try:
            if trace_file:
                await log_channel.send(embed=embed, file=trace_file)
            else:
                await log_channel.send(embed=embed)
        except Exception as send_error:
            logger.error("Failed to send error log message: %s", send_error)

    @app_commands.command(name="manage_logs", description="[ADMIN] Set or remove the bot error-log channel")
    @app_commands.describe(
        action="What to do.",
        channel="(set_channel) Channel that should receive error logs.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="set_channel", value="set_channel"),
        app_commands.Choice(name="remove_channel", value="remove_channel"),
    ])
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def manage_logs(self, interaction: discord.Interaction, action: str,
                          channel: Optional[discord.TextChannel] = None):
        if action == "set_channel":
            await self._logs_set_channel(interaction, channel)
        else:
            await self._logs_remove_channel(interaction)

    async def _logs_set_channel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel]):
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member or not member.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
            return

        if channel is None:
            await interaction.response.send_message("❌ Pick a channel for `set_channel`.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            success = await self.db.set_error_log_channel(interaction.guild.id, channel.id)
        except Exception as db_error:
            logger.error("Error storing logs channel: %s", db_error)
            await interaction.followup.send("❌ Failed to store the logs channel. Please try again.", ephemeral=True)
            return

        if success:
            embed = discord.Embed(
                title="✅ Error Log Channel Set",
                description=f"Bot errors will now be reported in {channel.mention}.",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(
                name="What you'll receive",
                value="• Command name and user\n• Full error message\n• Stack trace attachment",
                inline=False
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send("❌ Failed to set the error log channel.", ephemeral=True)

    async def _logs_remove_channel(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member or not member.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            removed = await self.db.remove_error_log_channel(interaction.guild.id)
        except Exception as db_error:
            logger.error("Error removing logs channel: %s", db_error)
            await interaction.followup.send("❌ Failed to remove the logs channel. Please try again.", ephemeral=True)
            return

        if removed:
            await interaction.followup.send("✅ Error log channel configuration removed.", ephemeral=True)
        else:
            await interaction.followup.send("ℹ️ No error log channel was configured for this server.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ErrorLoggingCog(bot))
