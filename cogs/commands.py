import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
import json
import logging
import os
import random

logger = logging.getLogger(__name__)

class CommandsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.MOTIVATION_DIR = os.path.join('assets', 'motivation')

    async def resolve_user(self, user_input: str = None, member: discord.Member = None):
        """
        Resolve a user from various input formats.
        
        Args:
            user_input: User ID string or username
            member: Discord member object
            
        Returns:
            Tuple of (user_id: str, user_display: str, user_object: discord.User or None)
        """
        if member:
            # Direct member object provided
            return str(member.id), f"{member.mention} ({member.name})", member
        
        if not user_input:
            return None, None, None
        
        # Try to parse as user ID (numeric string)
        if user_input.isdigit():
            user_id = user_input
            try:
                user_obj = await self.bot.fetch_user(int(user_id))
                return user_id, f"{user_obj.mention} ({user_obj.name})", user_obj
            except discord.NotFound:
                # User doesn't exist, but blacklisting by ID is still allowed
                return user_id, f"User ID: {user_id} (not found)", None
            except Exception as e:
                logger.warning(f"Error fetching user {user_id}: {e}")
                return user_id, f"User ID: {user_id}", None
        
        # Try to parse as username - search through the bot's cached users
        user_input_lower = user_input.lower()
        for user in self.bot.users:
            if user.name.lower() == user_input_lower or (user.global_name and user.global_name.lower() == user_input_lower):
                return str(user.id), f"{user.mention} ({user.name})", user
        
        # Username not found in cache
        return None, None, None

    @app_commands.command(name="manage_welcome", description="[ADMIN] Set the welcome channel or send a test welcome")
    @app_commands.describe(action="What to do.", channel="(set_channel) Channel for welcome messages.")
    @app_commands.choices(action=[
        app_commands.Choice(name="set_channel", value="set_channel"),
        app_commands.Choice(name="test", value="test"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def manage_welcome(self, interaction: discord.Interaction, action: str,
                             channel: Optional[discord.TextChannel] = None):
        if action == "set_channel":
            await self._welcome_set_channel(interaction, channel)
        else:
            await self._welcome_test(interaction)

    async def _welcome_set_channel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel]):
        if channel is None:
            await interaction.response.send_message("Pick a channel for `set_channel`.", ephemeral=True)
            return
        try:
            guild_id = str(interaction.guild_id)
            self.bot.config['guilds'].setdefault(guild_id, {})['welcome_channel'] = str(channel.id)
            with open('config.json', 'w', encoding='utf-8') as f:
                json.dump(self.bot.config, f, indent=4, ensure_ascii=False)
            note = ""
            onboarding = self.bot.get_cog("OnboardingCog")
            if onboarding is not None:
                entry_id = await onboarding.entry_channel_id(interaction.guild_id)
                if entry_id == channel.id:
                    note = (
                        "\n\n\u26a0\ufe0f This is also the onboarding entry channel, so quiz "
                        "level-ups would be announced where only unverified members are "
                        "looking. Point this at a channel the whole server reads."
                    )
            await interaction.response.send_message(
                f"Welcome channel set to {channel.mention}{note}", ephemeral=True
            )
        except discord.Forbidden:
            logger.error(f"Bot lacks permission to write config or send messages in {interaction.guild_id}")
            await interaction.response.send_message("I don't have permission to perform this action. Please check my permissions.", ephemeral=True)

    async def _welcome_test(self, interaction: discord.Interaction):
        try:
            # Defer response first since send_welcome_message can take time (loading GIF, etc.)
            await interaction.response.defer(ephemeral=True)
            # force, because whoever is testing this holds a quiz level and would
            # otherwise be skipped as a returning member. The reason is reported rather
            # than swallowed: several conditions skip the greeting silently, and claiming
            # a send that did not happen sends the tester looking in the wrong place.
            skipped = await self.bot.get_cog('EventsCog').send_welcome_message(
                interaction.user, interaction.guild, force=True
            )
            await interaction.followup.send(
                "Welcome message triggered!" if skipped is None
                else f"Not sent: {skipped}",
                ephemeral=True,
            )
        except discord.Forbidden:
            logger.error(f"Bot lacks permission to send welcome message in {interaction.guild_id}")
            if not interaction.response.is_done():
                await interaction.response.send_message("I don't have permission to send welcome messages. Please check my permissions.", ephemeral=True)
            else:
                await interaction.followup.send("I don't have permission to send welcome messages. Please check my permissions.", ephemeral=True)

    @app_commands.command(name="manage_verification", description="[ADMIN] Set the verification channel or enable/disable verification")
    @app_commands.describe(action="What to do.", channel="(set_channel) Channel for verification welcome messages.")
    @app_commands.choices(action=[
        app_commands.Choice(name="set_channel", value="set_channel"),
        app_commands.Choice(name="enable", value="enable"),
        app_commands.Choice(name="disable", value="disable"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def manage_verification(self, interaction: discord.Interaction, action: str,
                                  channel: Optional[discord.TextChannel] = None):
        try:
            guild_id = str(interaction.guild_id)
            guild_conf = self.bot.config['guilds'].setdefault(guild_id, {})
            if action == "set_channel":
                if channel is None:
                    await interaction.response.send_message("Pick a channel for `set_channel`.", ephemeral=True)
                    return
                guild_conf['verification_channel'] = str(channel.id)
                msg = f"Verification channel set to {channel.mention}"
            else:
                guild_conf['verification_enabled'] = (action == "enable")
                msg = f"Verification system {'enabled' if action == 'enable' else 'disabled'}"
            with open('config.json', 'w', encoding='utf-8') as f:
                json.dump(self.bot.config, f, indent=4, ensure_ascii=False)
            await interaction.response.send_message(msg, ephemeral=True)
        except discord.Forbidden:
            logger.error(f"Bot lacks permission to write config or send messages in {interaction.guild_id}")
            await interaction.response.send_message("I don't have permission to perform this action. Please check my permissions.", ephemeral=True)

    @app_commands.command(name="guide", description="Share the Japanese learning guide")
    async def guide(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="The Guide",
            description="Check out [the guide](https://vnclub.org/guide/) to start your Japanese immersion journey with visual novels",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="VNCR")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="manage_readers", description="[ADMIN] Confirm or unconfirm a user's reader (server) access")
    @app_commands.describe(
        action="Confirm grants the Confirmed Reader role; unconfirm removes it.",
        user="The member to confirm or unconfirm.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="confirm", value="confirm"),
        app_commands.Choice(name="unconfirm", value="unconfirm"),
    ])
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def manage_readers(self, interaction: discord.Interaction, action: str, user: discord.Member):
        # Both branches edit roles and, on a confirm, settle the application behind them,
        # which is several round trips more than an interaction token stays valid for.
        await interaction.response.defer(thinking=True)
        if action == "confirm":
            await self._readers_confirm(interaction, user)
        else:
            await self._readers_unconfirm(interaction, user)

    async def _settle_onboarding(self, interaction: discord.Interaction, user: discord.Member):
        """Close any application this member still has open, and say so for the reply.

        Granting Confirmed Reader is the same outcome an approval delivers. Without this
        the applicant keeps the marker role, their thread is never answered, and the
        review card still offers a decision that has already been made.
        """
        onboarding = self.bot.get_cog("OnboardingCog")
        if onboarding is None:
            return ""
        try:
            settled = await onboarding.settle_external_grant(
                user, interaction.user.id, interaction.user.mention
            )
        except Exception as e:
            logger.error(f"Could not settle the onboarding application for {user.id}: {e}")
            return ""
        return f" Application #{settled} closed." if settled else ""

    async def _readers_confirm(self, interaction: discord.Interaction, user: discord.Member):
        try:
            role_name = "Confirmed Reader"
            role = discord.utils.get(interaction.guild.roles, name=role_name)
            if not role:
                try:
                    role = await interaction.guild.create_role(
                        name=role_name,
                        color=discord.Color.green(),
                        reason="Created for VN learning community"
                    )
                except discord.Forbidden:
                    logger.error(f"Bot lacks permission to create roles in {interaction.guild_id}")
                    await interaction.followup.send("I don't have permission to create roles. Please check my permissions.")
                    return

            if role in user.roles:
                # The role can have been added while the bot was offline, in which case
                # no role change was ever dispatched and the application behind it is
                # still open.
                closed = await self._settle_onboarding(interaction, user)
                await interaction.followup.send(
                    f"{user.mention} already has the {role_name} role.{closed}"
                )
                return

            # Record them as verified without inventing a quiz level. Level 0 is no
            # longer used as a placeholder, so a manually confirmed member keeps whatever
            # progress is tracked (usually none) and gains no rank role until they pass a
            # quiz themselves.
            verification_cog = self.bot.get_cog('VerificationCog')
            if verification_cog:
                verification_cog.add_verified_user(user.id)

            await user.add_roles(role, reason=f"Promoted to {role_name} by {interaction.user.name}")

            # Before the slower role edit below, so the decider is recorded here rather
            # than by the role listener, which sees the change but not who made it.
            closed = await self._settle_onboarding(interaction, user)

            # Remove Unconfirmed Reader if they have it (roles are mutually exclusive)
            unconfirmed_role = discord.utils.get(interaction.guild.roles, name="Unconfirmed Reader")
            if unconfirmed_role and unconfirmed_role in user.roles:
                await user.remove_roles(unconfirmed_role, reason="Confirmed - removing Unconfirmed Reader")

            await interaction.followup.send(
                f"Promoted {user.mention} to {role_name}!{closed}"
            )
        except discord.Forbidden:
            logger.error(f"Bot lacks permission to assign roles in {interaction.guild_id}")
            await interaction.followup.send("I don't have permission to assign roles. Please check my permissions.")

    async def _readers_unconfirm(self, interaction: discord.Interaction, user: discord.Member):
        try:
            role_name = "Confirmed Reader"
            role = discord.utils.get(interaction.guild.roles, name=role_name)
            if not role:
                await interaction.followup.send(f"No {role_name} role exists.")
                return

            if role not in user.roles:
                await interaction.followup.send(f"{user.mention} doesn't have the {role_name} role.")
                return

            # Get verification cog to update quiz data
            verification_cog = self.bot.get_cog('VerificationCog')
            if verification_cog:
                user_id_str = str(user.id)
                
                # Remove from verified users list so they won't get Confirmed Reader restored when rejoining
                if user_id_str in verification_cog.verification_data["verified_users"]:
                    verification_cog.verification_data["verified_users"].remove(user_id_str)
                    verification_cog.save_verification_data()

            await user.remove_roles(role, reason=f"Removed {role_name} by {interaction.user.name}")

            # Add Unconfirmed Reader since they no longer have Confirmed Reader
            unconfirmed_role = discord.utils.get(interaction.guild.roles, name="Unconfirmed Reader")
            if unconfirmed_role and unconfirmed_role not in user.roles:
                await user.add_roles(unconfirmed_role, reason="Deconfirmed - adding Unconfirmed Reader")

            await interaction.followup.send(f"Removed {role_name} from {user.mention}.")
        except discord.Forbidden:
            logger.error(f"Bot lacks permission to remove roles in {interaction.guild_id}")
            await interaction.followup.send("I don't have permission to remove roles. Please check my permissions.")

    @app_commands.command(name="ankileaderboard", description="Instructions to join the VNCR Anki leaderboard")
    async def ankileaderboard(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Join the VNCR Anki Leaderboard!",
            description=(
                "Boost your Japanese VN learning by joining our **VNCR Anki Leaderboard**! "
                "Track your vocabulary progress, compete with friends, and stay motivated.\n\n"
                "**How to Join**:\n"
                "1. Install the [Anki Leaderboard Addon](https://ankiweb.net/shared/info/175794613).\n"
                "2. Open Anki, go to the leaderboard, and search for the group named **VNCR**.\n"
                f"3. Enter the password: **{os.getenv('ANKI_LEADERBOARD_PASSWORD', 'ask a moderator')}**.\n"
                "4. Start reviewing and climb the ranks!\n\n"
            ),
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="VNCR")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="resources", description="Share the VN Club Resources site")
    async def resources(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="VN Club Resources",
            description="Find useful resources for visual novel learning at [VN Club Resources](https://vnclub.org)",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="VNCR")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="getvns", description="Get links to download visual novels")
    async def getvns(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Get Visual Novels",
            description="Looking for visual novels? Check out the [sources page](https://vnclub.org/sources/) for links and info.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="VNCR")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="motivation", description="Get motivational content for your VN learning journey")
    @app_commands.checks.cooldown(1, 3.0)
    async def motivation(self, interaction: discord.Interaction):
        try:
            logger.info(f"Motivation command invoked by user {interaction.user.id} at {discord.utils.utcnow()}")
            
            # Defer immediately to prevent interaction timeout
            await interaction.response.defer()
            
            logger.info(f"Successfully deferred response for user {interaction.user.id}")
            
            if not os.path.exists(self.MOTIVATION_DIR):
                logger.error(f"Motivation directory not found at {self.MOTIVATION_DIR}")
                await interaction.followup.send("Sorry, the motivation directory is missing! Contact an admin.")
                return

            # Get all files in the motivation directory
            motivation_files = [f for f in os.listdir(self.MOTIVATION_DIR) 
                              if os.path.isfile(os.path.join(self.MOTIVATION_DIR, f))]
            
            if not motivation_files:
                logger.warning(f"No motivation files found in {self.MOTIVATION_DIR}")
                await interaction.followup.send("Sorry, no motivational content is available! Contact an admin.")
                return

            # Randomly select a file
            selected_file = random.choice(motivation_files)
            file_path = os.path.join(self.MOTIVATION_DIR, selected_file)
            
            logger.info(f"Loading motivation file: {selected_file}")
            
            # Create Discord file object
            file = discord.File(file_path, filename=selected_file)
            embed = discord.Embed(
                title="Motivation Boost!",
                description=f"Attached: {selected_file}",
                color=discord.Color.purple(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text="VNCR")
            await interaction.followup.send(embed=embed, file=file)
            
            logger.info(f"Successfully sent motivation content to user {interaction.user.id}")
        except discord.NotFound:
            # Interaction expired before a response was sent - log and ignore
            logger.warning(f"Motivation command interaction expired for user {interaction.user.id}")
        except Exception as e:
            logger.error(f"Error in /motivation command: {e}", exc_info=True)
            try:
                # Try to send error message if possible
                if interaction.response.is_done():
                    await interaction.followup.send("An error occurred while sending the motivational content. Try again later!")
                else:
                    await interaction.response.send_message("An error occurred while sending the motivational content. Try again later!")
            except discord.NotFound:
                # Interaction expired; nothing more to do
                logger.warning(f"Could not send error message for motivation command - interaction expired")

    @motivation.error
    async def motivation_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"Please wait {error.retry_after:.1f}s before using this again.",
                ephemeral=True
            )
        else:
            raise error

    @app_commands.command(name="help", description="Get help with all VNCR commands")
    @app_commands.describe(
        category="Specific category to get help for (optional)",
        command="Specific command to get detailed help for (optional)"
    )
    async def help_command(self, interaction: discord.Interaction,
                           category: str = None,
                           command: str = None):
        """Unified help for muramasa's commands"""
        try:
            await interaction.response.defer()

            immersion_commands_info, vn_commands_info = self._get_help_command_data()

            # If specific command requested, show detailed help
            if command:
                value = command.strip().lower().lstrip('/')
                if value == "log":
                    value = "log_manual"
                cmd_info = immersion_commands_info.get(value) or vn_commands_info.get(value)
                if not cmd_info and not value.startswith("vn"):
                    value_with_prefix = f"vn{value}"
                    cmd_info = vn_commands_info.get(value_with_prefix)
                    if cmd_info:
                        value = value_with_prefix
                if not cmd_info:
                    await interaction.followup.send("Command not found.", ephemeral=True)
                    return
                
                # Check if command is admin-only and user lacks permissions
                if cmd_info.get("admin_only", False) and not interaction.user.guild_permissions.administrator:
                    await interaction.followup.send("❌ You need administrator permissions to view this command.", ephemeral=True)
                    return

                embed = discord.Embed(
                    title=f"📖 Help: /{value}",
                    description=cmd_info["description"],
                    color=discord.Color.blue()
                )
                embed.add_field(name="📝 Usage", value=f"`{cmd_info['usage']}`", inline=False)
                embed.add_field(name="📋 Details", value=cmd_info["details"], inline=False)
                if cmd_info.get("examples"):
                    examples_text = "\n".join([f"`{ex}`" for ex in cmd_info["examples"]])
                    embed.add_field(name="💡 Examples", value=examples_text, inline=False)
                if cmd_info.get("admin_only"):
                    embed.add_field(name="🔒 Permissions", value="This command requires administrator permissions.", inline=False)
                embed.set_footer(text="VNCR • Unified Help")
                await interaction.followup.send(embed=embed)
                return

            # If category provided, show category help
            if category:
                display_name = category.replace('_', ' ').title()
                embed = discord.Embed(
                    title=f"📖 Help: {display_name}",
                    color=discord.Color.blue()
                )

                if category == "logging":
                    embed.description = "Time-focused immersion tracking for reading and listening."
                    # Group by category
                    groups = {}
                    for name, info in immersion_commands_info.items():
                        groups.setdefault(info["category"], []).append((name, info))
                    for cat, items in groups.items():
                        value_lines = [f"• `/{n}`: {i['description']}" for n, i in items]
                        embed.add_field(name=cat, value="\n".join(value_lines), inline=False)
                    embed.add_field(
                        name="💡 Tips",
                        value="Times like '30m', '1h30m', or just '90' (minutes). Use `/help command:<name>` for details.",
                        inline=False
                    )

                elif category == "admin":
                    # Check if user has admin permissions
                    if not interaction.user.guild_permissions.administrator:
                        await interaction.followup.send("❌ You need administrator permissions to view admin commands.", ephemeral=True)
                        return
                    
                    embed.description = "Administrative commands for server management and bot configuration."
                    
                    # Collect all admin commands
                    admin_commands = []
                    for name, info in vn_commands_info.items():
                        if info.get("admin_only", False):
                            admin_commands.append((name, info))
                    
                    # Categorize admin commands
                    vn_pool_cmds = [f"• `/{n}`: {i['description']}" for n, i in admin_commands if n.startswith('vn')]
                    verification_cmds = [f"• `/{n}`: {i['description']}" for n, i in admin_commands if n in ['manage_readers', 'manage_verification', 'manage_quiz', 'manage_quiz_backups']]
                    server_cmds = [f"• `/{n}`: {i['description']}" for n, i in admin_commands if n in ['manage_welcome', 'manage_sticky', 'manage_highlights', 'manage_roles']]
                    system_cmds = [f"• `/{n}`: {i['description']}" for n, i in admin_commands if n in ['manage_stop', 'manage_blacklist', 'manage_backups', 'manage_logs', 'manage_purge', 'manage_class_roles']]

                    if vn_pool_cmds:
                        embed.add_field(name="📚 VN Pool Management", value="\n".join(vn_pool_cmds), inline=False)
                    if verification_cmds:
                        embed.add_field(name="✅ Verification System", value="\n".join(verification_cmds), inline=False)
                    if server_cmds:
                        embed.add_field(name="👋 Welcome & Server", value="\n".join(server_cmds), inline=False)
                    if system_cmds:
                        embed.add_field(name="⚙️ System & Maintenance", value="\n".join(system_cmds), inline=False)
                    
                    embed.add_field(
                        name="🔒 Permissions",
                        value="All admin commands require Administrator permissions.",
                        inline=False
                    )
                    embed.add_field(
                        name="💡 Tips",
                        value="Use `/help command:<name>` for details and examples.",
                        inline=False
                    )

                elif category == "utility":
                    embed.description = "Helpful utility and resource commands"
                    embed.add_field(
                        name="🛠️ Utility Commands",
                        value="`/ankileaderboard`: Join the VNCR Anki leaderboard\n"
                              "`/resources`: Access VN Club resource site\n"
                              "`/getvns`: Get links to download visual novels\n"
                              "`/guide`: Share the Japanese learning guide",
                        inline=False
                    )

                elif category == "fun":
                    embed.description = "Motivational and entertainment commands"
                    embed.add_field(
                        name="🎨 Fun Commands",
                        value="`/motivation`: Get motivational content for VN learning\n"
                              "More fun commands coming soon!",
                        inline=False
                    )

                embed.set_footer(text="VNCR • Unified Help")
                await interaction.followup.send(embed=embed)
                return

            # Otherwise, overview
            embed = discord.Embed(
                title="🤖 VNCR Help Center",
                description="Welcome to VNCR! Here's an overview of available features.\n"
                            "Use `/help category:<name>` to browse a category or `/help command:<name>` for a command.",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="Logging",
                value="Complete activity logging system for reading and listening\n"
                      "Use `/help category:logging` to browse commands",
                inline=True
            )
            embed.add_field(
                name="Utility",
                value="Resource links and helpful tools\nUse `/help category:utility` for details",
                inline=True
            )
            embed.add_field(
                name="Fun",
                value="Motivational content and entertainment\nUse `/help category:fun` for details",
                inline=True
            )
            embed.add_field(
                name="📋 Quick Start",
                value="• Log activity: `/log_vn ...`\n"
                      "• Auto-log from a texthooker: `/kizuna action:link`\n"
                      "• Optional roles and event pings: `/roles`\n"
                      "• View your profile: `/profile`\n"
                      "• View your library: `/log_library`\n"
                      "• Detailed help: `/help command:<name>`",
                inline=False
            )
            embed.set_footer(text="VNCR • Unified Help")
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in /help command: {e}")
            await interaction.followup.send("An error occurred while generating help information.", ephemeral=True)

    @help_command.autocomplete("category")
    async def help_category_autocomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete for category parameter - filters admin for non-admins"""
        categories = [
            app_commands.Choice(name="Logging", value="logging"),
            app_commands.Choice(name="Utility", value="utility"),
            app_commands.Choice(name="Fun", value="fun"),
        ]
        
        # Only show admin category to administrators
        if interaction.user.guild_permissions.administrator:
            categories.append(app_commands.Choice(name="Admin", value="admin"))
        
        # Filter by current input
        if current:
            current_lower = current.lower()
            categories = [c for c in categories if current_lower in c.name.lower()]
        
        return categories

    @help_command.autocomplete("command")
    async def help_command_autocomplete(self, interaction: discord.Interaction, current: str):
        immersion_commands_info, vn_commands_info = self._get_help_command_data()
        combined = list(immersion_commands_info.items()) + list(vn_commands_info.items())
        
        # Filter out admin commands for non-admins
        if not interaction.user.guild_permissions.administrator:
            combined = [
                (name, info)
                for name, info in combined
                if not info.get("admin_only", False)
            ]

        query = (current or "").strip().lower()
        if query:
            combined = [
                (name, info)
                for name, info in combined
                if query in name.lower() or query in info["description"].lower()
            ]

        combined.sort(key=lambda item: item[0])

        results = []
        for name, info in combined[:25]:
            display = f"/{name}"
            results.append(app_commands.Choice(name=display, value=name))

        return results

    def _get_help_command_data(self):
        if hasattr(self, "_help_command_data_cache"):
            return self._help_command_data_cache

        immersion_commands_info = {
            "log_manual": {
                "category": "📝 Core Logging",
                "usage": "/log_manual activity_type:<type> content:<name> duration:<time>",
                "description": "Manual activity logging with full customization",
                "details": "Log any immersion activity with custom duration. Supports reading and listening activities with various media types.",
                "examples": [
                    "/log_manual activity_type:reading content:\"Yotsuba&!\" duration:\"30m\"",
                    "/log_manual activity_type:listening content:\"Podcast\" duration:\"1h\" date:\"2025-01-01 14:30\""
                ],
                "admin_only": False
            },
            "log_video": {
                "category": "📝 Core Logging",
                "usage": "/log_video url:<youtube_url> OR name:<video_name> duration:<time>",
                "description": "Log video watching with automatic duration detection",
                "details": "Automatically detects duration from YouTube URLs or manually specify video name and duration.",
                "examples": [
                    "/log_video url:https://youtube.com/watch?v=example",
                    "/log_video name:\"Japanese Tutorial\" duration:\"25m\""
                ],
                "admin_only": False
            },
            "log_vn": {
                "category": "📝 Core Logging",
                "usage": "/log_vn name:<vn_name> [duration:<time>] [characters:<count>]",
                "description": "Log visual novel reading with optional character counts",
                "details": "Supply duration to prioritize time tracking. If you also include character counts, the embed will show your actual reading speed.",
                "examples": [
                    "/log_vn name:\"Steins;Gate\" duration:\"45m\" characters:5000",
                    "/log_vn name:\"Clannad\" duration:\"30m\""
                ],
                "admin_only": False
            },
            "log_manga": {
                "category": "📝 Core Logging",
                "usage": "/log_manga name:<manga_name> [duration:<time>] [pages:<count>]",
                "description": "Log manga reading with optional page counts",
                "details": "Provide both duration and page count to display reading speed, or log just the time spent.",
                "examples": [
                    "/log_manga name:\"One Piece\" duration:\"40m\" pages:20",
                    "/log_manga name:\"Death Note\" duration:\"25m\""
                ],
                "admin_only": False
            },
            "log_book": {
                "category": "📝 Core Logging",
                "usage": "/log_book name:<book_name> [duration:<time>] [pages:<count>]",
                "description": "Log book reading with optional page counts",
                "details": "Tracks your reading time and, when possible, reports the pace based on pages and duration provided.",
                "examples": [
                    "/log_book name:\"Norwegian Wood\" duration:\"60m\" pages:50",
                    "/log_book name:\"Kitchen\" duration:\"30m\""
                ],
                "admin_only": False
            },
            "log_anime": {
                "category": "📝 Core Logging",
                "usage": "/log_anime name:<anime_name> episodes:<count>",
                "description": "Log anime watching with episode count",
                "details": "Automatically calculates time based on standard episode length (24 minutes) or specify custom duration.",
                "examples": [
                    "/log_anime name:\"Your Name\" episodes:1 episode_duration:106",
                    "/log_anime name:\"Attack on Titan\" episodes:2"
                ],
                "admin_only": False
            },
            "log_game": {
                "category": "📝 Core Logging",
                "usage": "/log_game name:<game_name> duration:<time> [activity_type:<reading/listening>]",
                "description": "Log game playing time",
                "details": "Track time spent playing games. Defaults to reading activity but can be changed to listening for audio-heavy games. Supports Japanese and English titles with autocomplete.",
                "examples": [
                    "/log_game name:\"Final Fantasy VII\" duration:2h",
                    "/log_game name:\"スーパーマリオ64\" duration:1h30m activity_type:reading"
                ],
                "admin_only": False
            },
            "log_history": {
                "category": "📊 Statistics & History",
                "usage": "/log_history [user:<@user>] [page:<number>] [activity_type:<reading/listening>] [media_type:<type>]",
                "description": "View activity history with interactive pagination",
                "details": "Browse through your or another user's activity history. Filter by activity or media type to drill into specific logs.",
                "examples": [
                    "/log_history",
                    "/log_history user:@username page:2",
                    "/log_history activity_type:reading media_type:visual_novel"
                ],
                "admin_only": False
            },
            "log_library": {
                "category": "📊 Statistics & History",
                "usage": "/log_library [user:<@user>] [media_type:<type>] [activity_type:<type>] [sort_by:<option>] [page:<number>]",
                "description": "View library of all logged titles",
                "details": "Browse all unique titles you've logged with aggregate statistics (total time, log count, last logged date). Filter by media/activity type and sort by duration, count, recent, or alphabetically. Perfect for tracking what media you've consumed.",
                "examples": [
                    "/log_library",
                    "/log_library media_type:visual_novel sort_by:duration",
                    "/log_library activity_type:reading sort_by:recent",
                    "/log_library user:@username sort_by:alphabetical"
                ],
                "admin_only": False
            },
            "log_stats": {
                "category": "📊 Statistics & History",
                "usage": "/log_stats [user:<@user>] [days:<number>] [media_type:<type>] [activity_type:<type>]",
                "description": "Interactive statistics dashboard with filters",
                "details": "Comprehensive stats viewer with multiple pages: Overview (cumulative progress chart), Breakdown (media distribution pie chart), and Timeline (daily activity chart). Filter by media type, activity type, time period, and metric (duration/characters/pages/episodes).",
                "examples": [
                    "/log_stats",
                    "/log_stats days:30 media_type:visual_novel",
                    "/log_stats user:@username activity_type:reading"
                ],
                "admin_only": False
            },
            "log_heatmap": {
                "category": "📊 Statistics & History",
                "usage": "/log_heatmap [user:<@user>] [media_type:<type>] [activity_type:<type>] [metric:<metric>]",
                "description": "View your activity heatmap calendar",
                "details": "Displays a visual heatmap of your activity over the last 9 months. Filter by media type, activity type, and metric. Navigate through different time periods using the navigation buttons.",
                "examples": [
                    "/log_heatmap",
                    "/log_heatmap media_type:visual_novel",
                    "/log_heatmap user:@username metric:characters"
                ],
                "admin_only": False
            },
            "profile": {
                "category": "📊 Statistics & History",
                "usage": "/profile [user:<@user>]",
                "description": "Generate an immersion profile card",
                "details": "Summarizes VN class progress, quiz level, lifetime immersion totals, and VN completion stats in one embed.",
                "examples": [
                    "/profile",
                    "/profile user:@username"
                ],
                "admin_only": False
            },
            "log_leaderboard": {
                "category": "📊 Statistics & History",
                "usage": "/log_leaderboard [timeframe:<period>] [media_type:<type>]",
                "description": "View user leaderboard for different timeframes and media types",
                "details": "Compare your immersion time with other server members.",
                "examples": [
                    "/log_leaderboard timeframe:week",
                    "/log_leaderboard timeframe:month media_type:anime",
                    "/log_leaderboard media_type:book"
                ],
                "admin_only": False
            },
            "log_server_leaderboard": {
                "category": "📊 Statistics & History",
                "usage": "/log_server_leaderboard [timeframe:<period>] [media_type:<type>] [page:<number>]",
                "description": "View server comparison leaderboard",
                "details": "Compare total immersion time across different servers. Shows which servers have the most active communities.",
                "examples": [
                    "/log_server_leaderboard",
                    "/log_server_leaderboard timeframe:month",
                    "/log_server_leaderboard media_type:anime page:2"
                ],
                "admin_only": False
            },
            "log_overview": {
                "category": "📊 Statistics & History",
                "usage": "/log_overview [timeframe:<period>] [metric:<type>]",
                "description": "View server-wide and global immersion statistics",
                "details": "Displays comprehensive statistics with interactive charts showing activity timeline, immersion overview, media distribution, and top users/titles. Toggle between server-wide and global views, filter by timeframe, and switch between different metrics (duration, characters, pages, episodes).",
                "examples": [
                    "/log_overview",
                    "/log_overview timeframe:month",
                    "/log_overview metric:characters timeframe:week"
                ],
                "admin_only": False
            },
            "log_export": {
                "category": "💾 Data Management",
                "usage": "/log_export",
                "description": "Export activities to compressed JSONL file",
                "details": "Exports all your activities to a downloadable file. Rate limited to once per 10 minutes.",
                "examples": ["/log_export"],
                "admin_only": False
            },
            "log_goal": {
                "category": "🎯 Goals & Configuration",
                "usage": "/log_goal action:<create/list/delete> [parameters...]",
                "description": "Create, view, or remove immersion goals",
                "details": "Set daily, weekly, or monthly goals. Media filters must align with their activity type (e.g., Video → Listening, Manga → Reading). Use `/log_goal action:delete name:<goal name>` to remove a goal.",
                "examples": [
                    "/log_goal action:create name:\"Daily VN\" target:90 period:daily activity_type:reading media_type:visual_novel",
                    "/log_goal action:delete name:\"Daily VN\"",
                    "/log_goal action:list"
                ],
                "admin_only": False
            },
            "log_config": {
                "category": "🎯 Goals & Configuration",
                "usage": "/log_config setting:<name> value:<value>",
                "description": "Configure personal immersion tracking settings",
                "details": "Set your timezone for localized timestamps.",
                "examples": [
                    "/log_config setting:timezone value:Asia/Tokyo"
                ],
                "admin_only": False
            },
            "log_details": {
                "category": "📊 Statistics & History",
                "usage": "/log_details activity_id:<id>",
                "description": "View detailed information about a specific log entry",
                "details": "Shows comprehensive details about any activity log including user, metadata, comments, and API information for supported media types.",
                "examples": [
                    "/log_details activity_id:12345"
                ],
                "admin_only": False
            },
            "log_undo": {
                "category": "🔧 Utilities",
                "usage": "/log_undo [activity_id:<id>]",
                "description": "Undo the last activity or specific activity by ID",
                "details": "Remove accidentally logged activities.",
                "examples": [
                    "/log_undo",
                    "/log_undo activity_id:123"
                ],
                "admin_only": False
            }
        }

        vn_commands_info = {
            "manage_welcome": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_welcome action:<set_channel|test> [channel:<#channel>]",
                "description": "Set the welcome channel or send a test welcome",
                "details": "set_channel configures which channel receives welcome messages; test sends a sample welcome.",
                "examples": [
                    "/manage_welcome action:set_channel channel:#welcome",
                    "/manage_welcome action:test"
                ],
                "admin_only": True
            },
            "manage_verification": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_verification action:<set_channel|enable|disable> [channel:<#channel>]",
                "description": "Set the verification channel or enable/disable verification",
                "details": "set_channel sets the verification welcome channel; enable/disable toggles the verification system.",
                "examples": [
                    "/manage_verification action:set_channel channel:#verification",
                    "/manage_verification action:enable",
                    "/manage_verification action:disable"
                ],
                "admin_only": True
            },
            "manage_readers": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_readers action:<confirm|unconfirm> user:<@user>",
                "description": "Confirm or unconfirm a user's reader (server) access",
                "details": "confirm grants the Confirmed Reader role (server access); unconfirm removes it.",
                "examples": [
                    "/manage_readers action:confirm user:@username",
                    "/manage_readers action:unconfirm user:@username"
                ],
                "admin_only": True
            },
            "manage_quiz": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_quiz action:<sync|restore|setlevel> [user:<@user>] [level:<n>]",
                "description": "Sync verification data, restore a user's roles, or set their quiz level",
                "details": "sync reconciles quiz data with Discord roles; restore reapplies a returning user's roles; setlevel overrides a user's highest quiz level.",
                "examples": [
                    "/manage_quiz action:sync",
                    "/manage_quiz action:restore user:@username",
                    "/manage_quiz action:setlevel user:@username level:5"
                ],
                "admin_only": True
            },
            "manage_quiz_backups": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_quiz_backups action:<create|list|status|restore> [backup_name:<name>]",
                "description": "Manage verification data backups",
                "details": "Create, list, check, or restore backups of the verification data.",
                "examples": [
                    "/manage_quiz_backups action:create",
                    "/manage_quiz_backups action:list"
                ],
                "admin_only": True
            },
            "manage_stop": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_stop",
                "description": "Emergency stop for the bot",
                "details": "Immediately shuts down the bot. Use only in emergencies.",
                "examples": [
                    "/manage_stop"
                ],
                "admin_only": True
            },
            "manage_blacklist": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_blacklist action:<add|remove|check|list> [user:<@user>] [reason:<text>]",
                "description": "Manage the user blacklist for leaderboards and roles",
                "details": "Add, remove, check, or list users in the blacklist. Blacklisted users are excluded from leaderboards and automated role assignments.",
                "examples": [
                    "/manage_blacklist action:add user:@username reason:\"Cheating\"",
                    "/manage_blacklist action:remove user:@username",
                    "/manage_blacklist action:check user:@username",
                    "/manage_blacklist action:list"
                ],
                "admin_only": True
            },
            "manage_backups": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_backups action:<create|list|status|restore|set_channel|remove_channel> [backup_name:<name>] [channel:<#channel>]",
                "description": "Database backup management and backup channel configuration",
                "details": "Create, list, check, or restore database backups; set_channel/remove_channel configure the channel that receives automated backups.",
                "examples": [
                    "/manage_backups action:create",
                    "/manage_backups action:list",
                    "/manage_backups action:set_channel channel:#backups"
                ],
                "admin_only": True
            },
            "manage_sticky": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_sticky action:<set|remove|list> [message:<text>] [channel:<#channel>]",
                "description": "Manage sticky messages that stay at the bottom of a channel",
                "details": "set creates a message that is reposted at the bottom whenever a new message arrives; remove clears it; list shows all sticky messages.",
                "examples": [
                    "/manage_sticky action:set message:\"Welcome! Please read the rules.\"",
                    "/manage_sticky action:remove channel:#general",
                    "/manage_sticky action:list"
                ],
                "admin_only": True
            },
            "manage_highlights": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_highlights action:<setup|status>",
                "description": "Configure or view the highlights feature",
                "details": "setup configures the highlights channel and reaction threshold; status shows the current configuration.",
                "examples": [
                    "/manage_highlights action:setup",
                    "/manage_highlights action:status"
                ],
                "admin_only": True
            },
            "manage_logs": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_logs action:<set_channel|remove_channel> [channel:<#channel>]",
                "description": "Set or remove the bot error-log channel",
                "details": "set_channel routes bot error logs to a channel; remove_channel stops them.",
                "examples": [
                    "/manage_logs action:set_channel channel:#bot-logs",
                    "/manage_logs action:remove_channel"
                ],
                "admin_only": True
            },
            "manage_purge": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_purge user:<@user> [...filters]",
                "description": "Mass-delete a user's messages (dry-run preview first)",
                "details": "Previews matching messages before deleting. Use the filters to scope by channel or time.",
                "examples": [
                    "/manage_purge user:@username"
                ],
                "admin_only": True
            },
            "manage_class_roles": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_class_roles",
                "description": "Force refresh of VN class and ranking roles",
                "details": "Recomputes and reassigns immersion class/ranking roles for all members.",
                "examples": [
                    "/manage_class_roles"
                ],
                "admin_only": True
            },
            "manage_roles": {
                "category": "⚙️ Admin Commands",
                "usage": "/manage_roles action:<add|remove|list> [role:<@role>] [emoji:<emoji>] [description:<text>]",
                "description": "Manage the optional roles offered by /roles",
                "details": "add registers an existing role (with optional emoji/description) as a self-assignable option; remove unregisters it; list shows the current options.",
                "examples": [
                    "/manage_roles action:add role:@Movie emoji:🎬 description:\"Movie night pings\"",
                    "/manage_roles action:list"
                ],
                "admin_only": True
            },
        }

        self._help_command_data_cache = (immersion_commands_info, vn_commands_info)
        return self._help_command_data_cache

    @app_commands.command(name="manage_stop", description="[ADMIN] Emergency stop for the bot (admin only)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        try:
            logger.info(f"Muramasa emergency stop initiated by {interaction.user.name} ({interaction.user.id})")
            await interaction.response.send_message("Shutting down Muramasa...", ephemeral=True)
            await self.bot.close()
        except discord.app_commands.errors.MissingPermissions:
            logger.debug(f"User {interaction.user.id} attempted /stop without Administrator permission")
            await interaction.response.send_message("You need Administrator permission to use this command!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error during /stop command: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Failed to shut down: {str(e)}", ephemeral=True)

    @app_commands.command(name="manage_blacklist", description="[ADMIN] Manage the user blacklist for leaderboards and roles")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(
        action="Action to perform",
        user="User to blacklist/unblacklist/check (mention, ID, or username)",
        user_id="User ID (alternative to user parameter)",
        reason="Reason for blacklisting (optional)"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add to blacklist", value="add"),
        app_commands.Choice(name="Remove from blacklist", value="remove"),
        app_commands.Choice(name="Check if blacklisted", value="check"),
        app_commands.Choice(name="List all blacklisted", value="list")
    ])
    async def blacklist(self, interaction: discord.Interaction, action: str, 
                       user: discord.Member = None, user_id: str = None, reason: str = None):
        """Manage the user blacklist"""
        try:
            await interaction.response.defer(ephemeral=True)
            
            # Get database from bot
            if not hasattr(self.bot, 'cogs'):
                await interaction.followup.send("❌ Bot is not properly initialized.", ephemeral=True)
                return
            
            immersion_cog = self.bot.get_cog('ImmersionCog')
            if not immersion_cog or not hasattr(immersion_cog, 'db'):
                await interaction.followup.send("❌ Immersion tracking system not available.", ephemeral=True)
                return
            
            db = immersion_cog.db
            
            if action == "add":
                # Resolve user from either parameter
                resolved_id, user_display, user_obj = await self.resolve_user(user_id, user)
                
                if not resolved_id:
                    await interaction.followup.send(
                        "❌ You must specify a user to blacklist using either:\n"
                        "• `user` parameter (mention/select a member)\n"
                        "• `user_id` parameter (user ID or username)\n\n"
                        "Examples:\n"
                        "• `/blacklist action:add user:@username`\n"
                        "• `/blacklist action:add user_id:123456789012345678`\n"
                        "• `/blacklist action:add user_id:username`",
                        ephemeral=True
                    )
                    return
                
                # Check if already blacklisted
                is_blacklisted = await db.is_user_blacklisted(resolved_id)
                if is_blacklisted:
                    await interaction.followup.send(f"ℹ️ {user_display} is already blacklisted.", ephemeral=True)
                    return
                
                # Add to blacklist
                success = await db.add_user_to_blacklist(
                    user_id=resolved_id,
                    blacklisted_by=str(interaction.user.id),
                    reason=reason
                )
                
                if success:
                    embed = discord.Embed(
                        title="🚫 User Blacklisted",
                        description=f"{user_display} has been added to the blacklist.",
                        color=discord.Color.red(),
                        timestamp=discord.utils.utcnow()
                    )
                    embed.add_field(name="User ID", value=resolved_id, inline=True)
                    embed.add_field(name="Blacklisted By", value=interaction.user.mention, inline=True)
                    if reason:
                        embed.add_field(name="Reason", value=reason, inline=False)
                    embed.add_field(
                        name="Effects",
                        value="• Will not appear in leaderboards\n"
                              "• Cannot earn ranking roles (魔王, 四天王, etc.)\n"
                              "• Class role locked at 魑魅魍魎\n"
                              "• Can still view own stats",
                        inline=False
                    )
                    embed.set_footer(text="Syncing roles across all servers...")
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    
                    # Trigger role sync for ALL guilds, not just the current one
                    synced_guilds = 0
                    for guild in self.bot.guilds:
                        try:
                            await immersion_cog.sync_reward_roles_for_guild(guild, source="blacklist_add")
                            synced_guilds += 1
                        except Exception as sync_error:
                            logger.error(f"Failed to sync roles in {guild.name} after blacklist add: {sync_error}")
                    
                    logger.info(f"Role sync triggered in {synced_guilds} guilds after blacklisting user {resolved_id}")
                    if synced_guilds == 0:
                        await interaction.followup.send("⚠️ User blacklisted, but role sync failed. Use `/manage_class_roles` manually.", ephemeral=True)
                else:
                    await interaction.followup.send("❌ Failed to add user to blacklist.", ephemeral=True)
            
            elif action == "remove":
                # Resolve user from either parameter
                resolved_id, user_display, user_obj = await self.resolve_user(user_id, user)
                
                if not resolved_id:
                    await interaction.followup.send(
                        "❌ You must specify a user to remove from blacklist using either:\n"
                        "• `user` parameter (mention/select a member)\n"
                        "• `user_id` parameter (user ID or username)\n\n"
                        "Examples:\n"
                        "• `/blacklist action:remove user:@username`\n"
                        "• `/blacklist action:remove user_id:123456789012345678`\n"
                        "• `/blacklist action:remove user_id:username`",
                        ephemeral=True
                    )
                    return
                
                # Remove from blacklist
                success = await db.remove_user_from_blacklist(resolved_id)
                
                if success:
                    embed = discord.Embed(
                        title="✅ User Removed from Blacklist",
                        description=f"{user_display} has been removed from the blacklist.",
                        color=discord.Color.green(),
                        timestamp=discord.utils.utcnow()
                    )
                    embed.add_field(name="User ID", value=resolved_id, inline=True)
                    embed.add_field(name="Removed By", value=interaction.user.mention, inline=True)
                    embed.add_field(
                        name="Effects",
                        value="• Will now appear in leaderboards\n"
                              "• Can earn ranking roles again\n"
                              "• Class role will update based on VN time",
                        inline=False
                    )
                    embed.set_footer(text="Syncing roles across all servers...")
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    
                    # Trigger role sync for ALL guilds, not just the current one
                    synced_guilds = 0
                    for guild in self.bot.guilds:
                        try:
                            await immersion_cog.sync_reward_roles_for_guild(guild, source="blacklist_remove")
                            synced_guilds += 1
                        except Exception as sync_error:
                            logger.error(f"Failed to sync roles in {guild.name} after blacklist remove: {sync_error}")
                    
                    logger.info(f"Role sync triggered in {synced_guilds} guilds after removing user {resolved_id} from blacklist")
                    if synced_guilds == 0:
                        await interaction.followup.send("⚠️ User removed from blacklist, but role sync failed. Use `/manage_class_roles` manually.", ephemeral=True)
                else:
                    await interaction.followup.send(f"ℹ️ {user_display} was not on the blacklist.", ephemeral=True)
            
            elif action == "check":
                # Resolve user from either parameter
                resolved_id, user_display, user_obj = await self.resolve_user(user_id, user)
                
                if not resolved_id:
                    await interaction.followup.send(
                        "❌ You must specify a user to check using either:\n"
                        "• `user` parameter (mention/select a member)\n"
                        "• `user_id` parameter (user ID or username)\n\n"
                        "Examples:\n"
                        "• `/blacklist action:check user:@username`\n"
                        "• `/blacklist action:check user_id:123456789012345678`\n"
                        "• `/blacklist action:check user_id:username`",
                        ephemeral=True
                    )
                    return
                
                entry = await db.get_blacklist_entry(resolved_id)
                
                if entry:
                    # Parse datetime
                    try:
                        blacklisted_at = discord.utils.parse_time(entry['blacklisted_at'])
                    except:
                        blacklisted_at = None
                    
                    embed = discord.Embed(
                        title="🚫 User is Blacklisted",
                        description=f"{user_display} is currently on the blacklist.",
                        color=discord.Color.red(),
                        timestamp=discord.utils.utcnow()
                    )
                    embed.add_field(name="User ID", value=entry['user_id'], inline=True)
                    
                    # Try to fetch who blacklisted them
                    try:
                        blacklister = await self.bot.fetch_user(int(entry['blacklisted_by']))
                        embed.add_field(name="Blacklisted By", value=blacklister.mention, inline=True)
                    except:
                        embed.add_field(name="Blacklisted By", value=entry['blacklisted_by'], inline=True)
                    
                    if blacklisted_at:
                        embed.add_field(name="Date", value=discord.utils.format_dt(blacklisted_at, style='F'), inline=False)
                    
                    if entry['reason']:
                        embed.add_field(name="Reason", value=entry['reason'], inline=False)
                    
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    embed = discord.Embed(
                        title="✅ User is Not Blacklisted",
                        description=f"{user_display} is not on the blacklist.",
                        color=discord.Color.green(),
                        timestamp=discord.utils.utcnow()
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
            
            elif action == "list":
                blacklisted_users = await db.get_all_blacklisted_users()
                
                if not blacklisted_users:
                    embed = discord.Embed(
                        title="📋 Blacklist",
                        description="No users are currently blacklisted.",
                        color=discord.Color.blue(),
                        timestamp=discord.utils.utcnow()
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return
                
                embed = discord.Embed(
                    title="📋 Blacklisted Users",
                    description=f"Total: {len(blacklisted_users)} user(s)",
                    color=discord.Color.orange(),
                    timestamp=discord.utils.utcnow()
                )
                
                for entry in blacklisted_users[:10]:  # Limit to 10 to avoid embed limits
                    try:
                        user_obj = await self.bot.fetch_user(int(entry['user_id']))
                        user_display = f"{user_obj.mention} ({user_obj.name})"
                    except:
                        user_display = f"<@{entry['user_id']}>"
                    
                    field_value = f"**ID:** {entry['user_id']}"
                    if entry['reason']:
                        field_value += f"\n**Reason:** {entry['reason']}"
                    
                    try:
                        blacklisted_at = discord.utils.parse_time(entry['blacklisted_at'])
                        field_value += f"\n**Date:** {discord.utils.format_dt(blacklisted_at, style='d')}"
                    except:
                        pass
                    
                    embed.add_field(name=user_display, value=field_value, inline=False)
                
                if len(blacklisted_users) > 10:
                    embed.set_footer(text=f"Showing 10 of {len(blacklisted_users)} blacklisted users")
                
                await interaction.followup.send(embed=embed, ephemeral=True)
                
        except discord.app_commands.errors.MissingPermissions:
            logger.debug(f"User {interaction.user.id} attempted /blacklist without Administrator permission")
            await interaction.response.send_message("You need Administrator permission to use this command!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in /blacklist command: {e}")
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ An error occurred: {str(e)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ An error occurred: {str(e)}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(CommandsCog(bot))