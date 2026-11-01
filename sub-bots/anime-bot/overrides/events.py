"""
LJTA (Learn Japanese Through Anime) - Events Override
Customizes welcome messages and events for the anime learning community.
"""

import discord
from discord.ext import commands
import sys
from pathlib import Path

# Import parent events cog
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from cogs.events import EventsCog as ParentEventsCog

class EventsCog(ParentEventsCog):
    """Extended events cog with LJTA-specific welcome messages"""
    
    async def send_welcome_message(self, member: discord.Member, guild: discord.Guild):
        """Send LJTA welcome message to new members with level quiz info"""
        # Reload config from disk to pick up any web console changes
        if hasattr(self.bot, 'reload_config'):
            self.bot.reload_config()

        guild_id = str(guild.id)
        guild_config = self.bot.config['guilds'].get(guild_id, {})

        welcome_channel_id = guild_config.get('welcome_channel')
        if not welcome_channel_id:
            return

        channel = guild.get_channel(int(welcome_channel_id))
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        # Create welcome embed
        embed = discord.Embed(
            title="Welcome to Learn Japanese Through Anime! 🎌",
            description=(
                "We're a community dedicated to learning Japanese through anime immersion. "
                "Whether you're just starting out or already on your journey, you'll find helpful resources and fellow learners here!"
            ),
            color=0xFF69B4  # Pink color for anime theme
        )

        # Level quizzes field
        embed.add_field(
            name="📚 Show off your level with our quizzes:",
            value=(
                "• Six Kotoba quizzes: N5, N4, N3, N2, N1 and Insane (N0)\n"
                "• Each one you pass gives you its level role\n"
                "• ✅ Unlimited attempts - No cooldown between tries!\n"
                "• Use `/quizzes` to see all the commands"
            ),
            inline=False
        )

        # Set the GIF as the image
        gif_path = Path(__file__).parent.parent / "assets" / "motivation" / "sakura-anki.gif"

        try:
            # Send with the GIF attached and member mention
            if gif_path.exists():
                file = discord.File(gif_path, filename="sakura-anki.gif")
                embed.set_image(url="attachment://sakura-anki.gif")
                await channel.send(content=f"{member.mention}", file=file, embed=embed)
            else:
                await channel.send(content=f"{member.mention}", embed=embed)
        except discord.Forbidden:
            pass
    
    async def send_join_notification(self, member: discord.Member, guild: discord.Guild):
        """Send simple join notification"""
        # Reload config from disk to pick up any web console changes
        if hasattr(self.bot, 'reload_config'):
            self.bot.reload_config()

        guild_id = str(guild.id)
        welcome_channel_id = self.bot.config['guilds'].get(guild_id, {}).get('welcome_channel')
        if not welcome_channel_id:
            return

        channel = guild.get_channel(int(welcome_channel_id))
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        # Simple LJTA join notification
        message = f"👋 {member.mention} joined the server!"

        try:
            await channel.send(message)
        except discord.Forbidden:
            pass

    async def assign_reader_role(self, member: discord.Member):
        """Assign the default '人間' role to a new member (LJTA override)"""
        try:
            # Get or create the "人間" role instead of "Reader"
            human_role = discord.utils.get(member.guild.roles, name="人間")
            
            if not human_role:
                try:
                    human_role = await member.guild.create_role(
                        name="人間",
                        color=discord.Color.default(),  # Default color
                        reason="Default role for all server members (LJTA)"
                    )
                    print(f"Created '人間' role in guild {member.guild.name}")
                except discord.Forbidden:
                    print(f"Cannot create '人間' role in guild {member.guild.name} - insufficient permissions")
                    return
            
            # Assign the 人間 role to the new member
            if human_role and human_role not in member.roles:
                try:
                    await member.add_roles(human_role, reason="Default role assignment on join (LJTA)")
                    print(f"Assigned '人間' role to {member.name} in {member.guild.name}")
                except discord.Forbidden:
                    print(f"Cannot assign '人間' role to {member.name} - insufficient permissions")
                    
        except Exception as e:
            print(f"Error assigning 人間 role to {member.name}: {e}")

async def setup(bot):
    await bot.add_cog(EventsCog(bot))
