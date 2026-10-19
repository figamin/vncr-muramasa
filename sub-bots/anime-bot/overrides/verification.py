"""
LJTA (Learn Japanese Through Anime) - Verification Cog
Simple hiragana quiz verification for server access.
"""

import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import json
import re
import requests
from pathlib import Path


class VerificationCog(commands.Cog):
    """Verification cog for anime bot - simple hiragana quiz"""

    # Kotoba bot ID
    KOTOBA_ID = 251239170058616833

    # Quiz command for verification
    QUIZ_COMMAND = "k!quiz ljtakana 8 hardcore nd mmq=4 dauq=1 font=1 size=100 atl=16"

    # Required quiz settings
    REQUIRED_DECKS = {"ljtakana"}
    DECK_DISPLAY_NAME = "LJTA Verification Quiz"
    REQUIRED_SCORE = 8
    REQUIRED_TIME_LIMIT = 16000  # 16 seconds in ms
    REQUIRED_MAX_MISSED = 4

    def __init__(self, bot):
        self.bot = bot
        self.data_dir = Path(__file__).parent.parent / 'data'
        self.data_dir.mkdir(exist_ok=True)
        self.verification_data_file = self.data_dir / 'verification_data.json'
        self.verification_data = self.load_verification_data()

    def load_verification_data(self) -> dict:
        """Load verification data from file"""
        if self.verification_data_file.exists():
            try:
                with open(self.verification_data_file, 'r') as f:
                    data = json.load(f)
                    # Ensure verified_users is a list of strings
                    if isinstance(data.get("verified_users"), list):
                        data["verified_users"] = [str(uid) for uid in data["verified_users"]]
                    else:
                        data["verified_users"] = []
                    return data
            except (json.JSONDecodeError, FileNotFoundError) as e:
                print(f"[Anime Verification] Error loading verification data: {e}")
        return {"verified_users": []}

    def save_verification_data(self):
        """Save verification data to file"""
        try:
            with open(self.verification_data_file, 'w') as f:
                json.dump(self.verification_data, f, indent=2)
        except Exception as e:
            print(f"[Anime Verification] Error saving verification data: {e}")

    def is_user_verified(self, user_id: int) -> bool:
        """Check if a user is verified (in data)"""
        return str(user_id) in self.verification_data["verified_users"]

    def remove_verified_user(self, user_id: int):
        """Remove a user from the verified list"""
        user_id_str = str(user_id)
        if user_id_str in self.verification_data["verified_users"]:
            self.verification_data["verified_users"].remove(user_id_str)
            self.save_verification_data()

    def add_verified_user(self, user_id: int):
        """Add a user to the verified list"""
        user_id_str = str(user_id)
        if user_id_str not in self.verification_data["verified_users"]:
            self.verification_data["verified_users"].append(user_id_str)
            self.save_verification_data()

    async def ensure_verified_role_exists(self, guild: discord.Guild) -> discord.Role:
        """Ensure the Verified role exists, create if needed"""
        verified_role = discord.utils.get(guild.roles, name="Verified")
        if not verified_role:
            try:
                verified_role = await guild.create_role(
                    name="Verified",
                    color=discord.Color.default(),
                    reason="Verification system setup"
                )
                print(f"[Anime Verification] Created 'Verified' role in guild {guild.name}")
            except discord.Forbidden:
                print(f"[Anime Verification] Cannot create 'Verified' role in guild {guild.name} - insufficient permissions")
                return None
        return verified_role

    async def send_verification_announcement(self, guild: discord.Guild, member: discord.Member):
        """Send verification success announcement to the appropriate channel"""
        # Reload config from disk to pick up any web console changes
        if hasattr(self.bot, 'reload_config'):
            self.bot.reload_config()

        guild_config = self.bot.config.get('guilds', {}).get(str(guild.id), {})

        # Use class_announcements_channel, fall back to welcome_channel
        channel_id = (
            guild_config.get("class_announcements_channel")
            or guild_config.get("welcome_channel")
        )

        if channel_id:
            channel = guild.get_channel(int(channel_id))
            if channel:
                try:
                    await channel.send(f"🎉 Welcome to the server, {member.mention}! You've been verified.")
                except discord.Forbidden:
                    print(f"[Anime Verification] Cannot send to announcement channel in {guild.name}")

    async def complete_verification(self, message: discord.Message, member: discord.Member):
        """Complete the verification process for a user"""
        try:
            # Check if already verified
            if self.is_user_verified(member.id):
                await message.channel.send(f"{member.mention}, you're already verified!")
                return

            # Ensure role exists
            verified_role = await self.ensure_verified_role_exists(message.guild)
            if not verified_role:
                await message.channel.send("Error: Could not create or find the Verified role.")
                return

            # Assign role
            if verified_role not in member.roles:
                try:
                    await member.add_roles(verified_role, reason="Passed verification quiz")
                except discord.Forbidden:
                    await message.channel.send("Error: Cannot assign the Verified role - insufficient permissions.")
                    return

            # Save to data
            self.add_verified_user(member.id)

            # Send announcement
            await self.send_verification_announcement(message.guild, member)

            print(f"[Anime Verification] Verified user {member.name} ({member.id}) in {message.guild.name}")

        except Exception as e:
            print(f"[Anime Verification] Error completing verification for {member.name}: {e}")

    async def check_quiz_start(self, message: discord.Message):
        """Check if a user started the verification quiz"""
        # Only process messages from actual users, not bots
        if message.author.bot:
            return

        # Parse the quiz command
        command_parts = message.content.split()
        if len(command_parts) < 3:
            return

        # Check if it matches the verification quiz command pattern: k!quiz ljtakana 8
        if command_parts[0] != 'k!quiz':
            return

        deck = command_parts[1] if len(command_parts) > 1 else ""
        score_target = command_parts[2] if len(command_parts) > 2 else ""

        # Check for ljtakana deck with score 8
        if deck == 'ljtakana' and score_target == '8':
            # Check for key parameters
            has_hardcore = 'hardcore' in command_parts
            has_nd = 'nd' in command_parts
            has_mmq4 = any('mmq=4' in part for part in command_parts)
            has_atl16 = any('atl=16' in part for part in command_parts)

            if has_hardcore and has_nd and has_mmq4 and has_atl16:
                if self.is_user_verified(message.author.id):
                    await message.channel.send(
                        f"🔄 Verification quiz started by {message.author.mention} - **You're already verified!**"
                    )
                else:
                    await message.channel.send(
                        f"🎯 **VERIFICATION QUIZ** started by {message.author.mention}!\n"
                        f"**Good luck!** 🍀 Remember: unlimited tries, no cooldown!"
                    )

    async def check_quiz_failure(self, message: discord.Message, report_data: dict):
        """Send failure message when ljtakana quiz requirements not met"""
        try:
            # First check if this is even a ljtakana quiz - only the verification deck gets a response
            report_decks = {deck.get('shortName', '') for deck in report_data.get('decks', [])}
            report_decks_lower = {d.lower() for d in report_decks if d}
            required_decks_lower = {d.lower() for d in self.REQUIRED_DECKS}

            # If it's not a ljtakana quiz, don't respond at all
            if not report_decks_lower.intersection(required_decks_lower):
                print(f"[Anime Verification] Not a ljtakana quiz ({report_decks}), ignoring")
                return

            # Get the participant
            participants = report_data.get('participants', [])
            if len(participants) != 1:
                return

            user_id = int(participants[0]['discordUser']['id'])
            member = message.guild.get_member(user_id)
            if not member:
                return

            # Already verified?
            if self.is_user_verified(user_id):
                return

            # Figure out what went wrong
            scores = report_data.get('scores', [])
            score = scores[0].get('score', 0) if scores else 0
            settings = report_data.get('settings', {})

            issues = []
            if score < self.REQUIRED_SCORE:
                issues.append(f"Score too low: got {score}, need {self.REQUIRED_SCORE}")
            if settings.get('answerTimeLimitInMs', 0) != self.REQUIRED_TIME_LIMIT:
                issues.append(f"Wrong time limit: use `atl=16`")
            if settings.get('maxMissedQuestions', 0) != self.REQUIRED_MAX_MISSED:
                issues.append(f"Wrong max missed: use `mmq=4`")

            if issues:
                issues_text = "\n".join(f"• {issue}" for issue in issues)
                await message.channel.send(
                    f"❌ Verification quiz by {member.mention} didn't meet requirements:\n{issues_text}\n\n"
                    f"Use `/quiz_verify` to see the correct command."
                )

        except Exception as e:
            print(f"[Anime Verification] Error in check_quiz_failure: {e}")

    async def check_quiz_ended_failure(self, message: discord.Message, embed: discord.Embed):
        """Check if this was an LJTA verification quiz that ended without completion"""
        try:
            embed_title = embed.title or ""
            embed_text = embed.description or ""

            # Check for quiz ended indicators
            if "Quiz Ended" not in embed_title and "Game Ended" not in embed_title:
                return

            # Look for LJTA deck indicators
            full_text = f"{embed_title} {embed_text}".lower()
            if "ljta" not in full_text and "ljtakana" not in full_text:
                return

            # Try to find the user who took the quiz from the embed
            # Kotoba embeds usually mention the user
            user_mention_match = re.search(r'<@!?(\d+)>', embed_text)
            if user_mention_match:
                user_id = int(user_mention_match.group(1))
                member = message.guild.get_member(user_id)
            else:
                member = None

            if member and not self.is_user_verified(member.id):
                await message.channel.send(
                    f"❌ Quiz ended by {member.mention}. Don't give up! You have unlimited tries.\n"
                    f"Use `/quiz_verify` to see the command again."
                )
        except Exception as e:
            print(f"[Anime Verification] Error in check_quiz_ended_failure: {e}")

    def check_quiz_settings_match(self, report_data: dict) -> bool:
        """Check if the quiz report matches the required ljtakana settings"""
        try:
            print("[Anime Verification] === CHECKING QUIZ SETTINGS ===")

            # Check decks - mirror main bot: get shortName, case-insensitive subset check
            report_decks = {deck.get('shortName', '') for deck in report_data.get('decks', [])}
            print(f"[Anime Verification] Report decks: {report_decks}, Required: {self.REQUIRED_DECKS}")

            # Case-insensitive comparison
            report_decks_lower = {d.lower() for d in report_decks if d}
            required_decks_lower = {d.lower() for d in self.REQUIRED_DECKS}

            # Check if report decks match required (either direction for single deck)
            if not report_decks_lower.issubset(required_decks_lower) and not required_decks_lower.issubset(report_decks_lower):
                print("[Anime Verification] FAILED: Deck mismatch")
                return False

            # Check score
            scores = report_data.get('scores', [])
            if not scores:
                print("[Anime Verification] FAILED: No scores found")
                return False

            score = scores[0].get('score', 0)
            print(f"[Anime Verification] Score: {score}, Required: >= {self.REQUIRED_SCORE}")
            if score < self.REQUIRED_SCORE:
                print("[Anime Verification] FAILED: Score too low")
                return False

            # Check settings
            settings = report_data.get('settings', {})
            print(f"[Anime Verification] Settings: {settings}")

            # Check time limit
            time_limit = settings.get('answerTimeLimitInMs', 0)
            print(f"[Anime Verification] Time limit: {time_limit}, Required: {self.REQUIRED_TIME_LIMIT}")
            if time_limit != self.REQUIRED_TIME_LIMIT:
                print("[Anime Verification] FAILED: Time limit mismatch")
                return False

            # Check max missed questions
            max_missed = settings.get('maxMissedQuestions', 0)
            print(f"[Anime Verification] Max missed: {max_missed}, Required: {self.REQUIRED_MAX_MISSED}")
            if max_missed != self.REQUIRED_MAX_MISSED:
                print("[Anime Verification] FAILED: Max missed mismatch")
                return False

            print("[Anime Verification] === ALL CHECKS PASSED ===")
            return True

        except Exception as e:
            print(f"[Anime Verification] Error checking quiz settings: {e}")
            return False

    async def process_quiz_report(self, message: discord.Message, report_link: str):
        """Process a quiz report from Kotoba"""
        try:
            print(f"[Anime Verification] Processing quiz report: {report_link}")

            # Extract report ID from the link
            match = re.search(r'game_reports/([^)]*)\)', report_link)
            if not match:
                print("[Anime Verification] Failed to extract report ID from link")
                return

            report_id = match.group(1)
            print(f"[Anime Verification] Report ID: {report_id}")

            # report_id is interpolated into the URL path, so reject anything outside
            # the known report-ID charset rather than encoding it.
            if not re.fullmatch(r'[A-Za-z0-9_-]+', report_id):
                print("[Anime Verification] Invalid report ID format, skipping fetch")
                return

            # Fetch the quiz report from Kotoba API (off the event loop, with timeout)
            url = f'https://kotobaweb.com/api/game_reports/{report_id}'
            try:
                response = await asyncio.to_thread(requests.get, url, timeout=10)
            except requests.RequestException as e:
                print(f"[Anime Verification] Request to Kotoba failed: {e}")
                return
            if response.status_code != 200:
                print(f"[Anime Verification] Failed to fetch quiz report: {response.status_code}")
                return

            report_data = response.json()
            print("[Anime Verification] Successfully fetched quiz report")

            # Check single participant
            participants = report_data.get('participants', [])
            if len(participants) != 1:
                print("[Anime Verification] Quiz has multiple participants, skipping")
                return

            # Get user who took the quiz
            user_id = int(participants[0]['discordUser']['id'])
            member = message.guild.get_member(user_id)
            if not member:
                print(f"[Anime Verification] Member {user_id} not found in guild")
                return

            print(f"[Anime Verification] Quiz taker: {member.name}")

            # Check if settings match
            if not self.check_quiz_settings_match(report_data):
                print("[Anime Verification] Quiz settings don't match requirements")
                await self.check_quiz_failure(message, report_data)
                return

            # Complete verification
            await self.complete_verification(message, member)

        except Exception as e:
            print(f"[Anime Verification] Error processing quiz report: {e}")
            import traceback
            traceback.print_exc()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Monitor for Kotoba quiz starts and completions"""
        # Check for quiz start commands from users (not Kotoba)
        if message.author.id != self.KOTOBA_ID and message.content.startswith('k!quiz'):
            await self.check_quiz_start(message)
            return

        # Only process other messages from Kotoba bot
        if message.author.id != self.KOTOBA_ID:
            return

        # Check for embeds
        if not message.embeds:
            return

        # Look for "View a report" links in embed fields and description
        report_links = []
        quiz_ended_embed = None
        for embed in message.embeds:
            embed_title = embed.title or ""

            # Track if this is a Quiz Ended embed
            if "Quiz Ended" in embed_title or "Game Ended" in embed_title:
                quiz_ended_embed = embed

            # Check embed description for report links
            if embed.description and 'View a report' in embed.description:
                for line in embed.description.split('\n'):
                    if 'View a report' in line:
                        report_links.append(line)
                        print(f"[Anime Verification] Found report link in description: {line}")

            # Check embed fields
            for field in embed.fields:
                if 'View a report' in (field.value or ''):
                    report_links.append(field.value)
                    print(f"[Anime Verification] Found report link in field: {field.value}")
                elif 'game_reports' in (field.value or ''):
                    report_links.append(field.value)
                    print(f"[Anime Verification] Found report link (alt): {field.value}")

        # When report links are present, process them (success or failure determined by report)
        if report_links:
            for link in report_links:
                await self.process_quiz_report(message, link)
            return

        # No report links - if this was a Quiz Ended embed, it's a failure/stop
        if quiz_ended_embed:
            await self.check_quiz_ended_failure(message, quiz_ended_embed)

    async def get_user_anime_class_info(self, member: discord.Member):
        """Get user's anime class information for welcome message"""
        try:
            immersion_cog = self.bot.get_cog("ImmersionCog")
            if not immersion_cog:
                return None

            # Get anime hours
            user_id = str(member.id)
            anime_seconds = await immersion_cog.activity_repo.sum_activity_duration(
                user_id,
                media_type="anime"
            )
            anime_hours = (anime_seconds or 0) / 3600

            # Use anime class definitions
            from cogs.immersion import ANIME_CLASS_ROLE_DEFINITIONS

            # Find current class
            current_class = "人間"  # Default base class
            for definition in ANIME_CLASS_ROLE_DEFINITIONS:
                if anime_hours >= definition["min_hours"]:
                    current_class = definition["name"]

            return {
                "class_name": current_class,
                "anime_hours": anime_hours
            }
        except Exception as e:
            print(f"[Anime Verification] Error getting anime class info for {member.name}: {e}")
            return None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Restore Verified role for returning verified users"""
        if member.bot:
            return

        # Reload config from disk to pick up any web console changes
        if hasattr(self.bot, 'reload_config'):
            self.bot.reload_config()

        if self.is_user_verified(member.id):
            verified_role = discord.utils.get(member.guild.roles, name="Verified")
            if verified_role and verified_role not in member.roles:
                try:
                    await member.add_roles(verified_role, reason="Restored verification on rejoin")
                    print(f"[Anime Verification] Restored 'Verified' role to returning user {member.name}")
                except discord.Forbidden:
                    print(f"[Anime Verification] Cannot restore 'Verified' role to {member.name}")
                    return

            # Get anime class info
            class_info = await self.get_user_anime_class_info(member)

            # Send welcome back message to class announcements channel
            guild_id = str(member.guild.id)
            guild_config = self.bot.config.get('guilds', {}).get(guild_id, {})
            # Use class_announcements_channel (same as class change announcements)
            channel_id = guild_config.get('class_announcements_channel') or guild_config.get('welcome_channel')

            if channel_id:
                welcome_channel = member.guild.get_channel(int(channel_id))
                if welcome_channel:
                    try:
                        welcome_parts = [f"🔄 Welcome back, {member.mention}!"]
                        welcome_parts.append("Your **Verified** role has been restored.")

                        if class_info:
                            welcome_parts.append(f"Your **{class_info['class_name']}** class role has been restored ({class_info['anime_hours']:.1f}h anime time).")

                        welcome_back_text = " ".join(welcome_parts)
                        await welcome_channel.send(welcome_back_text)
                    except discord.Forbidden:
                        pass
                    except Exception as e:
                        print(f"[Anime Verification] Error sending welcome back message: {e}")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Track when Verified role is manually added or removed"""
        # Check if roles actually changed
        if before.roles == after.roles:
            return

        verified_role = discord.utils.get(after.guild.roles, name="Verified")
        if not verified_role:
            return

        had_verified = verified_role in before.roles
        has_verified = verified_role in after.roles
        is_in_data = self.is_user_verified(after.id)

        if has_verified and not had_verified and not is_in_data:
            # Gained Verified role manually - add to data
            self.add_verified_user(after.id)
            print(f"[Anime Verification] Auto-added {after.name} to verified list (gained Verified role)")
        elif not has_verified and had_verified and is_in_data:
            # Lost Verified role manually - remove from data
            self.remove_verified_user(after.id)
            print(f"[Anime Verification] Auto-removed {after.name} from verified list (lost Verified role)")

    @app_commands.command(name="quiz_verify", description="Show how to get verified")
    async def quiz_verify(self, interaction: discord.Interaction):
        """Show verification instructions"""
        embed = discord.Embed(
            title="How to Get Verified",
            description="Complete a hiragana quiz to verify and get server access!",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="Quiz Command",
            value=f"```{self.QUIZ_COMMAND}```",
            inline=False
        )
        embed.add_field(
            name="Requirements",
            value=(
                "• **Score**: Reach 8 points\n"
                "• **Max missed**: 4 questions\n"
                "• **Time limit**: 16 seconds per question\n"
                "• **Deck**: Hiragana"
            ),
            inline=False
        )
        embed.add_field(
            name="What happens",
            value=(
                "1. Copy and paste the quiz command above\n"
                "2. Complete the quiz and reach 8 points\n"
                "3. You'll automatically get the **Verified** role\n"
                "4. Enjoy full server access!"
            ),
            inline=False
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="quiz_progress", description="Check your verification status")
    async def quiz_progress(self, interaction: discord.Interaction, user: discord.Member = None):
        """Show verification status"""
        if user is None:
            user = interaction.user

        is_verified = self.is_user_verified(user.id)
        verified_role = discord.utils.get(interaction.guild.roles, name="Verified")
        has_role = verified_role in user.roles if verified_role else False

        if is_verified or has_role:
            embed = discord.Embed(
                title=f"✅ Verification Status - {user.display_name}",
                description="You are verified!",
                color=discord.Color.green()
            )
            embed.add_field(name="Status", value="Verified", inline=True)
        else:
            embed = discord.Embed(
                title=f"❓ Verification Status - {user.display_name}",
                description="You are not yet verified.",
                color=discord.Color.orange()
            )
            embed.add_field(name="Status", value="Not Verified", inline=True)
            embed.add_field(
                name="How to Verify",
                value=f"Use `/quiz_verify` to see the verification quiz command.",
                inline=False
            )

        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(VerificationCog(bot))