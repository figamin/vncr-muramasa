"""
LJTA (Learn Japanese Through Anime) - Commands Override
Customizes messages and branding for the anime learning community.
This version does NOT inherit command decorators to avoid conflicts.
"""

import discord
from discord import app_commands
from discord.ext import commands
import json
import logging
import os
import random
import sys
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from cogs.immersion import ANIME_CLASS_ROLE_DEFINITIONS  # noqa: E402

class CommandsCog(commands.Cog):
    """LJTA Commands - General bot commands with anime branding"""
    
    def __init__(self, bot):
        self.bot = bot
        # Use anime bot's assets
        self.MOTIVATION_DIR = Path(__file__).parent.parent / 'assets' / 'motivation'
    
    # ============= ADMIN COMMANDS =============
    
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
                             channel: discord.TextChannel = None):
        if action == "set_channel":
            await self._welcome_set_channel(interaction, channel)
        else:
            await self._welcome_test(interaction)

    async def _welcome_set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if channel is None:
            await interaction.response.send_message("Pick a channel for `set_channel`.", ephemeral=True)
            return
        try:
            guild_id = str(interaction.guild_id)
            self.bot.config['guilds'].setdefault(guild_id, {})['welcome_channel'] = str(channel.id)
            self.bot.save_config()
            await interaction.response.send_message(f"Welcome channel set to {channel.mention}", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in manage_welcome set_channel: {e}")
            await interaction.response.send_message("An error occurred!", ephemeral=True)

    async def _welcome_test(self, interaction: discord.Interaction):
        try:
            # Defer response first since send_welcome_message can take time (loading GIF, etc.)
            await interaction.response.defer(ephemeral=True)
            events_cog = self.bot.get_cog('EventsCog')
            if events_cog:
                await events_cog.send_welcome_message(interaction.user, interaction.guild)
                await interaction.followup.send("Welcome message triggered!", ephemeral=True)
            else:
                await interaction.followup.send("Events cog not found!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in manage_welcome test: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("An error occurred!", ephemeral=True)
            else:
                await interaction.followup.send("An error occurred!", ephemeral=True)

    @app_commands.command(name="role_distribution", description="View anime class role distribution")
    @app_commands.guild_only()
    async def role_distribution(self, interaction: discord.Interaction):
        """Provide a snapshot of anime class role coverage for this server."""
        try:
            await interaction.response.defer()

            guild = interaction.guild
            if guild is None:
                await interaction.followup.send("This command must be used inside a server.", ephemeral=True)
                return

            members = [member for member in guild.members if not member.bot]
            total_members = len(members)
            if total_members == 0:
                await interaction.followup.send("No members to report on yet.", ephemeral=True)
                return

            guild_config = (self.bot.config.get("guilds") or {}).get(str(guild.id), {})
            definitions = guild_config.get("class_roles", {}).get("definitions") or ANIME_CLASS_ROLE_DEFINITIONS

            class_names = [definition.get("name") for definition in definitions if definition.get("name")]
            class_roles = {
                name: discord.utils.get(guild.roles, name=name)
                for name in class_names
            }

            class_counts: Dict[str, int] = {name: 0 for name in class_names}
            for member in members:
                for name, role in class_roles.items():
                    if role and role in member.roles:
                        class_counts[name] += 1

            total_with_class = sum(class_counts.values())
            total_without_class = max(total_members - total_with_class, 0)

            # Count verified vs non-verified members
            verified_role = discord.utils.get(guild.roles, name="Verified")
            verified_count = 0
            non_verified_count = 0
            for member in members:
                if verified_role and verified_role in member.roles:
                    verified_count += 1
                else:
                    non_verified_count += 1

            def _format_entry(name: str, count: int) -> str:
                percentage = (count / total_members * 100) if total_members else 0.0
                noun = "member" if count == 1 else "members"
                return f"**{name}**: {count:,} {noun} ({percentage:.1f}%)"

            class_lines = [
                _format_entry(name, class_counts[name])
                for name in class_names
                if class_counts.get(name, 0) > 0
            ]

            if not class_lines:
                class_lines.append("No members currently hold anime class roles.")

            if total_without_class > 0:
                class_lines.append(_format_entry("No Class", total_without_class))

            # Format verification stats
            verification_lines = [
                _format_entry("Verified", verified_count),
                _format_entry("Not Verified", non_verified_count)
            ]

            embed = discord.Embed(
                title="Anime Class Role Distribution",
                description=f"Server: **{guild.name}**",
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(
                name="👥 Members Tracked",
                value=f"{total_members:,} humans",
                inline=False
            )
            embed.add_field(
                name="✅ Verification Status",
                value="\n".join(verification_lines),
                inline=False
            )
            embed.add_field(
                name="🏷️ Class Roles",
                value="\n".join(class_lines),
                inline=False
            )
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")

            await interaction.followup.send(embed=embed)

        except Exception as exc:
            logger.error("Error in role_distribution command: %s", exc)
            await interaction.followup.send("An error occurred while generating the report.", ephemeral=True)
    
    # ============= LJTA PUBLIC COMMANDS =============
    
    @app_commands.command(name="guide", description="Share the Japanese learning guide")
    async def guide(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📚 Japanese Learning Guide",
            description=(
                "**Complete Japanese Learning Guide:**\n"
                "https://donkuri.github.io/learn-japanese/guide/\n\n"
                "This comprehensive guide covers everything you need to know about learning Japanese through immersion, "
                "including how to use anime effectively for language learning!"
            ),
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="LJTA - Learn Japanese Through Anime")
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="ankileaderboard", description="Instructions to join the LJTA Anki leaderboard")
    async def anki_leaderboard(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Join the LJTA Anki Leaderboard!",
            description=(
                "Boost your Japanese anime learning by joining our **LJTA Anki Leaderboard**! "
                "Track your progress and compete with fellow learners.\n\n"
                "**How to join:**\n"
                "1. Download the [Anki Leaderboard add-on](https://ankiweb.net/shared/info/41708974).\n"
                "2. Open Anki, go to the leaderboard, and search for the group named **LJTA**.\n"
                f"3. Enter the password: **{os.getenv('SAKURA_ANKI_LEADERBOARD_PASSWORD', 'ask a moderator')}**.\n"
                "4. Start reviewing and climb the ranks!\n\n"
                "Good luck on your Japanese learning journey! 🎌"
            ),
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="LJTA - Learn Japanese Through Anime")
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="resources", description="Get helpful anime learning resources")
    async def resources(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Anime Learning Resources",
            description=(
                "Check out these comprehensive Japanese learning resources:\n"
                "https://github.com/donkuri/japanese-resources\n\n"
                "Find tools, guides, and materials to help you on your Japanese learning journey!"
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="LJTA - Learn Japanese Through Anime")
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="motivation", description="Get motivational content for your anime learning journey")
    async def motivation(self, interaction: discord.Interaction):
        try:
            if not os.path.exists(self.MOTIVATION_DIR):
                logger.error(f"Motivation directory not found at {self.MOTIVATION_DIR}")
                await interaction.response.send_message("Sorry, the motivation directory is missing! Contact an admin.")
                return

            # Get all files
            motivation_files = [f for f in os.listdir(self.MOTIVATION_DIR) 
                              if os.path.isfile(os.path.join(self.MOTIVATION_DIR, f))]
            
            if not motivation_files:
                logger.warning(f"No motivation files found in {self.MOTIVATION_DIR}")
                await interaction.response.send_message("Sorry, no motivational content is available! Contact an admin.")
                return

            # Randomly select a file
            selected_file = random.choice(motivation_files)
            file_path = os.path.join(self.MOTIVATION_DIR, selected_file)
            
            file = discord.File(file_path, filename=selected_file)
            embed = discord.Embed(
                title="Motivation Boost!",
                description=f"Keep up the great work learning Japanese!",
                color=discord.Color.purple(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text="LJTA - Learn Japanese Through Anime")
            await interaction.response.send_message(embed=embed, file=file)
        except Exception as e:
            logger.error(f"Error in /motivation command: {e}")
            await interaction.response.send_message("An error occurred while sending the motivational content. Try again later!")
    
    @app_commands.command(name="help", description="Get help with all LJTA commands")
    @app_commands.describe(
        category="Specific category to get help for (optional)"
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="logging", value="logging"),
        app_commands.Choice(name="utility", value="utility"),
        app_commands.Choice(name="fun", value="fun")
    ])
    async def help_command(self, interaction: discord.Interaction,
                           category: app_commands.Choice[str] = None):
        """LJTA help command with anime branding"""
        try:
            await interaction.response.defer()
            
            if category:
                # Category-specific help
                embed = discord.Embed(
                    title=f"📖 Help: {category.name.title()}",
                    color=discord.Color.blue()
                )

                if category.value == "logging":
                    embed.description = "Time-focused immersion tracking for reading and listening."
                    embed.add_field(
                        name="📝 Core Logging",
                        value=(
                            "• `/log_manual`: Manual activity logging with full customization\n"
                            "• `/log_video`: Log video watching with automatic duration detection\n"
                            "• `/log_vn`: Log visual novel reading with optional character counts\n"
                            "• `/log_manga`: Log manga reading with optional page counts\n"
                            "• `/log_book`: Log book reading with optional page counts\n"
                            "• `/log_anime`: Log anime watching with episode count\n"
                            "• `/log_game`: Log game playing time"
                        ),
                        inline=False
                    )
                    embed.add_field(
                        name="📊 Statistics & History",
                        value=(
                            "• `/log_details`: View detailed information about a specific log entry\n"
                            "• `/log_history`: View activity history with interactive pagination\n"
                            "• `/log_library`: View library of all logged titles with aggregate stats\n"
                            "• `/log_stats`: Interactive statistics dashboard with filters and visualizations\n"
                            "• `/log_heatmap`: View your activity heatmap calendar\n"
                            "• `/profile`: Generate an immersion profile card\n"
                            "• `/log_leaderboard`: View user leaderboard for different timeframes and media types\n"
                            "• `/log_server_leaderboard`: View server comparison leaderboard\n"
                            "• `/log_overview`: View server-wide and global immersion statistics"
                        ),
                        inline=False
                    )
                    embed.add_field(
                        name="💾 Data Management",
                        value=(
                            "• `/log_export`: Export activities to compressed JSONL file\n"
                            "• `/log_import`: Import activities or manage import history"
                        ),
                        inline=False
                    )
                    embed.add_field(
                        name="🎯 Goals & Configuration",
                        value=(
                            "• `/log_goal`: Create, view, or remove immersion goals\n"
                            "• `/log_config`: Configure personal immersion tracking settings"
                        ),
                        inline=False
                    )
                    embed.add_field(
                        name="🔧 Utilities",
                        value=(
                            "• `/log_undo`: Undo the last activity or specific activity by ID"
                        ),
                        inline=False
                    )
                    embed.add_field(
                        name="💡 Tips",
                        value="Times like '30m', '1h30m', or just '90' (minutes). Use `/help` for more info.",
                        inline=False
                    )

                elif category.value == "utility":
                    embed.description = "Helpful utility and resource commands"
                    embed.add_field(
                        name="🛠️ Utility Commands",
                        value=(
                            "• `/guide`: Complete Japanese learning guide\n"
                            "• `/resources`: Japanese learning resources\n"
                            "• `/ankileaderboard`: Join LJTA Anki leaderboard"
                        ),
                        inline=False
                    )

                elif category.value == "fun":
                    embed.description = "Motivational and entertainment commands"
                    embed.add_field(
                        name="🎨 Fun Commands",
                        value=(
                            "• `/motivation`: Get motivational content\n"
                            "More fun commands coming soon!"
                        ),
                        inline=False
                    )


                embed.set_footer(text="LJTA • Unified Help")
                await interaction.followup.send(embed=embed)
                
            else:
                # Main help overview
                embed = discord.Embed(
                    title="🤖 LJTA Help Center",
                    description="Welcome to **Learn Japanese Through Anime**! Here's an overview of available features.\n"
                                "Use `/help category:<name>` to browse a specific category.",
                    color=discord.Color.blue()
                )
                embed.add_field(
                    name="Logging",
                    value="Complete anime tracking system for watching time and progress\n"
                          "Use `/help category:logging` to browse commands",
                    inline=True
                )
                embed.add_field(
                    name="Utility",
                    value="Resource links and helpful tools\n"
                          "Use `/help category:utility` for details",
                    inline=True
                )
                embed.add_field(
                    name="Fun",
                    value="Motivational content and entertainment\n"
                          "Use `/help category:fun` for details",
                    inline=True
                )
                embed.add_field(
                    name="Server Snapshot",
                    value="Keep tabs on class coverage with `/role_distribution`",
                    inline=False
                )
                embed.add_field(
                    name="📋 Quick Start",
                    value=(
                        "• Log anime: `/log_anime name:\"Death Note\" episodes:1`\n"
                        "• View stats: `/log_stats`\n"
                        "• View profile: `/profile`\n"
                        "• View your library: `/log_library`\n"
                        "• Learning guide: `/guide`"
                    ),
                    inline=False
                )
                embed.set_footer(text="LJTA • Unified Help")
                await interaction.followup.send(embed=embed)
        
        except Exception as e:
            logger.error(f"Error in help command: {e}")
            await interaction.followup.send("An error occurred while generating help. Please try again.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(CommandsCog(bot))
