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
        """Send LJTA welcome message to new members with verification info"""
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

        # Verification Instructions field
        embed.add_field(
            name="📝 To access the rest of the server, complete a quick hiragana quiz:",
            value=(
                "**How it works:**\n"
                "• A character will appear (e.g., あ, き)\n"
                "• Type the correct reading in romaji (e.g., 'a', 'ki')\n"
                "• You have 16 seconds per question\n"
                "• Reach 8 correct answers to pass (max 4 mistakes allowed)"
            ),
            inline=False
        )

        # Important Info field
        embed.add_field(
            name="Important Info:",
            value=(
                "• 🎯 Deck: ljtakana (Hiragana)\n"
                "• ✅ Unlimited attempts - No cooldown between tries!\n"
                "• 📚 Follow [this guide](https://donkuri.github.io/learn-japanese/guide/) and you'll pass quickly!\n"
                "• 🏆 **Passing the quiz will unlock access to all channels, events, and our helpful community!**"
            ),
            inline=False
        )

        # Command section field
        embed.add_field(
            name="Copy and paste the command below exactly as it is:",
            value=" ",
            inline=False
        )

        # Set the GIF as the image
        gif_path = Path(__file__).parent.parent / "assets" / "motivation" / "sakura-anki.gif"

        # The quiz command in a separate message for easy copying
        command_message = "k!quiz ljtakana 8 hardcore nd mmq=4 dauq=1 font=1 size=100 atl=16"

        try:
            # Send with the GIF attached and member mention
            if gif_path.exists():
                file = discord.File(gif_path, filename="sakura-anki.gif")
                embed.set_image(url="attachment://sakura-anki.gif")
                await channel.send(content=f"{member.mention}", file=file, embed=embed)
            else:
                await channel.send(content=f"{member.mention}", embed=embed)

            # Send command in separate message for easy copying
            await channel.send(command_message)
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
