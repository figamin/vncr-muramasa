
import asyncio
import discord
from discord.ext import commands
import json
import logging
import os
import random
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

class EventsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bump_reminders = {}  # Store active reminders
        self.reminders_file = os.path.join('data', 'bump_reminders.json')
        self.bot.loop.create_task(self.load_and_restore_reminders())

        self.dissoku_reminders = {}  # Store active Dissoku reminders
        self.dissoku_reminders_file = os.path.join('data', 'dissoku_reminders.json')
        self.bot.loop.create_task(self.load_and_restore_dissoku_reminders())
    
    async def load_and_restore_reminders(self):
        """Load existing reminders and restore active ones on bot startup"""
        await self.bot.wait_until_ready()  # Wait for bot to be fully ready
        
        # Ensure data directory exists
        os.makedirs('data', exist_ok=True)
        
        # Load existing reminders from file
        if os.path.exists(self.reminders_file):
            try:
                with open(self.reminders_file, 'r') as f:
                    saved_reminders = json.load(f)
                
                current_time = datetime.utcnow()
                
                for reminder_id, reminder_data in saved_reminders.items():
                    reminder_time = datetime.fromisoformat(reminder_data['reminder_time'])
                    
                    # Only restore reminders that haven't expired yet
                    if reminder_time > current_time:
                        time_until_reminder = (reminder_time - current_time).total_seconds()
                        if time_until_reminder > 0:
                            # Schedule the reminder
                            self.bot.loop.create_task(
                                self.schedule_bump_reminder_with_id(
                                    reminder_id,
                                    reminder_data['channel_id'],
                                    reminder_data['guild_id'],
                                    time_until_reminder
                                )
                            )
                            self.bump_reminders[reminder_id] = reminder_data
                            print(f"Restored bump reminder for channel {reminder_data['channel_id']} in {time_until_reminder:.0f} seconds")
                
                # Save cleaned up reminders (only active ones are now in self.bump_reminders)
                self.save_reminders()
                
            except Exception as e:
                print(f"Error loading bump reminders: {e}")

    async def load_and_restore_dissoku_reminders(self):
        """Load existing Dissoku reminders and restore active ones on bot startup"""
        await self.bot.wait_until_ready()

        os.makedirs('data', exist_ok=True)

        if os.path.exists(self.dissoku_reminders_file):
            try:
                with open(self.dissoku_reminders_file, 'r') as f:
                    saved_reminders = json.load(f)

                current_time = datetime.utcnow()

                for reminder_id, reminder_data in saved_reminders.items():
                    reminder_time = datetime.fromisoformat(reminder_data['reminder_time'])

                    if reminder_time > current_time:
                        time_until_reminder = (reminder_time - current_time).total_seconds()
                        if time_until_reminder > 0:
                            self.bot.loop.create_task(
                                self.schedule_dissoku_reminder_with_id(
                                    reminder_id,
                                    reminder_data['channel_id'],
                                    reminder_data['guild_id'],
                                    time_until_reminder
                                )
                            )
                            self.dissoku_reminders[reminder_id] = reminder_data
                            print(f"Restored Dissoku reminder for channel {reminder_data['channel_id']} in {time_until_reminder:.0f} seconds")

                self.save_dissoku_reminders()

            except Exception as e:
                print(f"Error loading Dissoku reminders: {e}")

    def save_reminders(self):
        """Save current reminders to file"""
        try:
            with open(self.reminders_file, 'w') as f:
                json.dump(self.bump_reminders, f, indent=2, default=str)
        except Exception as e:
            print(f"Error saving bump reminders: {e}")

    def save_dissoku_reminders(self):
        """Save current Dissoku reminders to file"""
        try:
            with open(self.dissoku_reminders_file, 'w') as f:
                json.dump(self.dissoku_reminders, f, indent=2, default=str)
        except Exception as e:
            print(f"Error saving Dissoku reminders: {e}")

    def generate_reminder_id(self, channel_id: int, guild_id: int):
        """Generate unique reminder ID"""
        return f"{guild_id}_{channel_id}_{int(datetime.utcnow().timestamp())}"

    def generate_dissoku_reminder_id(self, channel_id: int, guild_id: int):
        """Generate unique Dissoku reminder ID"""
        return f"dissoku_{guild_id}_{channel_id}_{int(datetime.utcnow().timestamp())}"

    async def schedule_bump_reminder_with_id(self, reminder_id: str, channel_id: int, guild_id: int, delay: float):
        """Schedule a bump reminder with a specific ID"""
        await asyncio.sleep(delay)
        
        # Check if reminder still exists (in case it was cancelled)
        if reminder_id not in self.bump_reminders:
            return
        
        try:
            guild = self.bot.get_guild(guild_id)
            if guild:
                channel = guild.get_channel(channel_id)
                if channel and isinstance(channel, discord.TextChannel):
                    await channel.send("It's time to bump the server again! Use `/bump` to keep us visible on DISBOARD.")
                else:
                    print(f"Channel {channel_id} not found for bump reminder {reminder_id}")
            else:
                print(f"Guild {guild_id} not found for bump reminder {reminder_id}")
        except Exception as e:
            print(f"Error sending bump reminder {reminder_id}: {e}")
        finally:
            # Remove the reminder from active list and save
            if reminder_id in self.bump_reminders:
                del self.bump_reminders[reminder_id]
                self.save_reminders()

    async def schedule_bump_reminder(self, channel: discord.TextChannel):
        """Schedule a bump reminder and store it persistently"""
        reminder_id = self.generate_reminder_id(channel.id, channel.guild.id)
        reminder_time = datetime.utcnow() + timedelta(hours=2)
        
        # Store reminder data
        reminder_data = {
            'channel_id': channel.id,
            'guild_id': channel.guild.id,
            'reminder_time': reminder_time.isoformat(),
            'created_at': datetime.utcnow().isoformat()
        }
        
        self.bump_reminders[reminder_id] = reminder_data
        self.save_reminders()
        
        # Schedule the actual reminder
        self.bot.loop.create_task(
            self.schedule_bump_reminder_with_id(
                reminder_id,
                channel.id,
                channel.guild.id,
                7200  # 2 hours
            )
        )
        
        print(f"Scheduled bump reminder {reminder_id} for channel {channel.id} at {reminder_time}")

    async def schedule_dissoku_reminder_with_id(self, reminder_id: str, channel_id: int, guild_id: int, delay: float):
        """Schedule a Dissoku reminder with a specific ID"""
        await asyncio.sleep(delay)

        if reminder_id not in self.dissoku_reminders:
            return

        try:
            guild = self.bot.get_guild(guild_id)
            if guild:
                channel = guild.get_channel(channel_id)
                if channel and isinstance(channel, discord.TextChannel):
                    await channel.send("It's time to up the server again! Use `/up` to keep us visible on ディス速.")
                else:
                    print(f"Channel {channel_id} not found for Dissoku reminder {reminder_id}")
            else:
                print(f"Guild {guild_id} not found for Dissoku reminder {reminder_id}")
        except Exception as e:
            print(f"Error sending Dissoku reminder {reminder_id}: {e}")
        finally:
            if reminder_id in self.dissoku_reminders:
                del self.dissoku_reminders[reminder_id]
                self.save_dissoku_reminders()

    async def schedule_dissoku_reminder(self, channel: discord.TextChannel):
        """Schedule a Dissoku reminder and store it persistently"""
        reminder_id = self.generate_dissoku_reminder_id(channel.id, channel.guild.id)
        reminder_time = datetime.utcnow() + timedelta(hours=2)

        reminder_data = {
            'channel_id': channel.id,
            'guild_id': channel.guild.id,
            'reminder_time': reminder_time.isoformat(),
            'created_at': datetime.utcnow().isoformat()
        }

        self.dissoku_reminders[reminder_id] = reminder_data
        self.save_dissoku_reminders()

        self.bot.loop.create_task(
            self.schedule_dissoku_reminder_with_id(
                reminder_id,
                channel.id,
                channel.guild.id,
                7200  # 2 hours
            )
        )

        print(f"Scheduled Dissoku reminder {reminder_id} for channel {channel.id} at {reminder_time}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Only respond to DISBOARD bot's bump success message
        is_disboard_bump = False
        if message.author.id == 302050872383242240:
            # Check plain text (legacy, just in case)
            if "Bump done!" in message.content:
                is_disboard_bump = True
            # Check embeds for the bump message
            for embed in message.embeds:
                if (
                    (embed.title and "Bump done!" in embed.title)
                    or (embed.description and "Bump done!" in embed.description)
                ):
                    is_disboard_bump = True
                    break
        if is_disboard_bump:
            if isinstance(message.channel, discord.TextChannel):
                try:
                    await message.channel.send("Server bumped, sending reminder in two hours.")
                except discord.Forbidden:
                    pass
                await self.schedule_bump_reminder(message.channel)

        # Check for Dissoku (ディス速) up success message
        # Dissoku uses deferred slash command responses: the message arrives empty
        # and the content is filled in afterward without triggering edit events,
        # so the message is re-fetched after a short delay to get the actual content.
        if message.author.id == 761562078095867916:
            if not message.content and len(message.embeds) == 0:
                for _ in range(5):
                    await asyncio.sleep(3)
                    try:
                        message = await message.channel.fetch_message(message.id)
                    except Exception:
                        pass
                    if message.embeds or message.content:
                        break
            is_dissoku_up = False
            search_text = "をアップしたよ"
            if search_text in message.content:
                is_dissoku_up = True
            for embed in message.embeds:
                if search_text in (embed.title or ""):
                    is_dissoku_up = True
                if search_text in (embed.description or ""):
                    is_dissoku_up = True
                if embed.footer and search_text in (embed.footer.text or ""):
                    is_dissoku_up = True
                if embed.author and search_text in (embed.author.name or ""):
                    is_dissoku_up = True
                for field in embed.fields:
                    if search_text in (field.name or "") or search_text in (field.value or ""):
                        is_dissoku_up = True
            if is_dissoku_up:
                if isinstance(message.channel, discord.TextChannel):
                    try:
                        await message.channel.send("Server upped on Dissoku, sending reminder in two hours.")
                    except discord.Forbidden:
                        pass
                    await self.schedule_dissoku_reminder(message.channel)

        # Handle forum thread pin/unpin feature
        await self.handle_forum_thread_pin(message)
    
    async def handle_forum_thread_pin(self, message: discord.Message):
        """
        Handle pinning and unpinning messages in forum threads.
        
        Thread creators can:
        - Reply to a message and ping the bot to pin that message
        - Reply to a pinned message and ping the bot to unpin it
        
        The bot will delete the user's ping message after processing.
        """
        # Ignore bot messages
        if message.author.bot:
            return
        
        # Check if message is in a forum thread
        if not isinstance(message.channel, discord.Thread):
            return
        
        # Check if the thread is from a forum channel
        if message.channel.parent is None or not isinstance(message.channel.parent, discord.ForumChannel):
            return
        
        # Check if the message mentions the bot
        if self.bot.user not in message.mentions:
            return
        
        # Check if the message author is the thread creator
        if message.author.id != message.channel.owner_id:
            return
        
        # Check if the message is a reply to another message
        if message.reference is None or message.reference.message_id is None:
            return
        
        try:
            # Get the referenced message
            referenced_message = await message.channel.fetch_message(message.reference.message_id)
            
            # Check if the referenced message is already pinned
            try:
                if referenced_message.pinned:
                    # Unpin the message
                    await referenced_message.unpin()
                    print(f"Unpinned message {referenced_message.id} in thread {message.channel.name} (ID: {message.channel.id})")
                else:
                    # Pin the message
                    await referenced_message.pin()
                    print(f"Pinned message {referenced_message.id} in thread {message.channel.name} (ID: {message.channel.id})")
            except discord.Forbidden:
                print(f"Bot lacks permissions to pin/unpin messages in thread {message.channel.name}")
            
            # Always try to delete the user's ping message (even if pin/unpin failed)
            try:
                await message.delete()
                print(f"Deleted ping message {message.id} from {message.author.name}")
            except discord.Forbidden:
                print(f"Bot lacks permissions to delete messages in thread {message.channel.name}")
            except Exception as delete_error:
                print(f"Error deleting ping message: {delete_error}")
            
        except discord.NotFound:
            print(f"Referenced message not found in thread {message.channel.name}")
            try:
                await message.delete()
            except:
                pass
        except Exception as e:
            print(f"Error handling forum thread pin: {e}")
            try:
                await message.delete()
            except:
                pass

    # Named rather than positional so the sentence still reads correctly when a server
    # has only configured some of them.
    TOUR_CHANNELS = (
        ("announcements_channel", "announcements"),
        ("events_channel", "events"),
        ("resources_channel", "resources"),
    )

    def _tour_links(self, guild: discord.Guild) -> str:
        """The channels worth pointing a newcomer at, as links where possible.

        Falls back to the plain words for any channel a server has not configured, so an
        unconfigured install still reads naturally rather than showing a broken mention
        or an empty list.
        """
        config = self.bot.config["guilds"].get(str(guild.id), {})
        parts = []
        for key, word in self.TOUR_CHANNELS:
            raw = config.get(key)
            channel = guild.get_channel(int(raw)) if raw else None
            parts.append(channel.mention if channel is not None else word)
        if len(parts) > 1:
            return ", ".join(parts[:-1]) + " and " + parts[-1]
        return parts[0]

    async def _onboarding_ready(self, guild: discord.Guild) -> bool:
        """Whether the in-server flow is switched on AND has somewhere to run.

        A loaded cog is not the same as a live flow. The cog ships enabled=0 until an
        admin configures it, so treating its presence as readiness hands a new arrival a
        button that can only answer "verification is closed", while withholding the quiz
        command that is still the real way in.
        """
        onboarding = self.bot.get_cog("OnboardingCog")
        if onboarding is None:
            return False
        try:
            settings = await onboarding.db.get_onboarding_settings(guild.id)
            return bool(settings.get("enabled") and settings.get("entry_channel_id"))
        except Exception as exc:
            print(f"Welcome: cannot read onboarding settings: {exc}")
            return False

    async def _welcome_target(self, guild: discord.Guild):
        """Where the greeting belongs.

        The onboarding entry channel wins when that flow is configured, because the
        button on this message opens a thread in that channel and the two would
        otherwise sit in different places. The verification channel remains the target
        for a server still on the older flow.
        """
        onboarding = self.bot.get_cog("OnboardingCog")
        if onboarding is not None and await self._onboarding_ready(guild):
            try:
                settings = await onboarding.db.get_onboarding_settings(guild.id)
                channel = guild.get_channel(settings["entry_channel_id"])
                if isinstance(channel, discord.TextChannel):
                    return channel
                print(
                    f"Welcome: onboarding entry channel "
                    f"{settings['entry_channel_id']} is missing or not a text channel"
                )
            except Exception as exc:
                print(f"Welcome: cannot read onboarding settings: {exc}")

        configured = self.bot.config['guilds'].get(str(guild.id), {}).get('verification_channel')
        if not configured:
            print(f"Welcome: no verification_channel configured for guild {guild.id}")
            return None
        channel = guild.get_channel(int(configured))
        if not isinstance(channel, discord.TextChannel):
            print(f"Welcome: verification_channel {configured} is missing or not a text channel")
            return None
        return channel

    async def send_welcome_message(self, member: discord.Member, guild: discord.Guild,
                                   force: bool = False):
        """Send full welcome message with verification info.

        Returns None when the greeting was sent, or a short reason when it was
        deliberately skipped. Several conditions skip it silently, and a caller testing
        the message needs to be told which one it hit rather than shown a success it
        did not get.

        force lifts only the returning-member guard, which anyone testing this will trip
        by definition: they hold a level themselves.
        """
        guild_id = str(guild.id)
        guild_config = self.bot.config['guilds'].get(guild_id, {})

        # Check if verification is enabled for this guild
        if not guild_config.get('verification_enabled', False):
            print(f"Welcome: verification is disabled for guild {guild_id}")
            return "verification is disabled for this server"

        # A returning member who already holds a level is greeted by the verification
        # cog, which restores their roles and says welcome back. Greeting them again here
        # would hand them a Start verification button that can only tell them they are
        # already in.
        #
        # The test is on the tracked level being above zero, matching the condition that
        # welcome-back itself fires on. A zero entry is not a returning member: every
        # member present at a restart is written in at zero by
        # assign_level_0_to_untracked_users, so skipping on the key alone would silence
        # both greetings and leave an ordinary arrival with no way in at all.
        #
        # Read from the tracked data rather than from their roles, because the role
        # restore runs in a parallel listener and may not have landed yet.
        verification = self.bot.get_cog("VerificationCog")
        if verification is not None and not force:
            try:
                if verification.get_highest_quiz_level(member.id) > 0:
                    print(f"Welcome: {member.id} already holds a level, greeting skipped")
                    return ("they already hold a quiz level, so the greeting is skipped "
                            "for them as a returning member")
            except Exception as exc:
                print(f"Welcome: cannot check returning status: {exc}")

        channel = await self._welcome_target(guild)
        if channel is None:
            return "no welcome channel is configured, or the bot cannot see it"

        # Create welcome embed
        embed = discord.Embed(
            title="Welcome to The VN Club!",
            description=(
                "We're a community of Japanese learners who read visual novels in "
                "Japanese. Have a look around first: "
                f"{self._tour_links(guild)} are already open to you.\n\n"
                "When you're ready, press **Start verification** below. You'll get a "
                "private thread that only you and the moderators can see, with two "
                "short questions and a short vocab quiz drawn from the 100 most "
                "common words. **Unlimited tries, no cooldown.**\n\n"
                "You don't need to be far along. Beginners are welcome.\n\n"
                "VN Clubへようこそ。\n"
                "参加するには、下の「Start verification」ボタンを押してください。\n"
                "専用のスレッドが作成されるので、そこで簡単な質問2つと語彙クイズに答えてください。\n"
                "クイズは漢字の読みをローマ字またはかなで入力する形式です。何度でも挑戦できます。"
            ),
            color=0xFF4500  # Orange-red color
        )

        # Pick a random banner
        banners_dir = Path(__file__).parent.parent / "assets" / "other"
        banners = list(banners_dir.glob("banner*.webp"))
        banner = random.choice(banners) if banners else None

        # The button opens the application and the quiz command is issued inside the
        # resulting thread. Until the flow is switched on there is no thread to issue it
        # in, so the message falls back to carrying the command itself. Gated on the
        # flow being live rather than on the cog existing: while the cog is loaded but
        # not yet configured, a new arrival must still get the command route in.
        onboarding = self.bot.get_cog("OnboardingCog")
        flow_ready = await self._onboarding_ready(guild)
        view = onboarding.entry_view() if flow_ready else None

        try:
            if banner:
                file = discord.File(banner, filename=banner.name)
                embed.set_image(url=f"attachment://{banner.name}")
                await channel.send(
                    content=f"{member.mention}", file=file, embed=embed, view=view
                )
            else:
                await channel.send(content=f"{member.mention}", embed=embed, view=view)

            if not flow_ready:
                verification = self.bot.get_cog("VerificationCog")
                command = getattr(verification, "LEVEL_1_COMMAND", None)
                if command:
                    await channel.send(command)
        except discord.Forbidden:
            print(f"Welcome: no permission to post in #{channel.name}")
        except discord.HTTPException as exc:
            print(f"Welcome: Discord refused the message in #{channel.name}: {exc}")
            return f"Discord refused the message in #{channel.name}"
        return None

    async def send_join_notification(self, member: discord.Member, guild: discord.Guild):
        """Send simple join notification to general channel"""
        guild_id = str(guild.id)
        welcome_channel_id = self.bot.config['guilds'].get(guild_id, {}).get('welcome_channel')
        if not welcome_channel_id:
            return

        channel = guild.get_channel(int(welcome_channel_id))
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        # Simple join notification
        message = f"👋 {member.mention} joined the server!"

        try:
            await channel.send(message)
        except discord.Forbidden:
            pass

    async def assign_reader_role(self, member: discord.Member):
        """Assign the default 'Reader' role to a new member"""
        try:
            # Get or create the "Reader" role
            reader_role = discord.utils.get(member.guild.roles, name="Reader")
            
            if not reader_role:
                try:
                    reader_role = await member.guild.create_role(
                        name="Reader",
                        color=discord.Color.default(),  # Default color
                        reason="Default role for all server members"
                    )
                    print(f"Created 'Reader' role in guild {member.guild.name}")
                except discord.Forbidden:
                    print(f"Cannot create 'Reader' role in guild {member.guild.name} - insufficient permissions")
                    return
            
            # Assign the Reader role to the new member
            if reader_role and reader_role not in member.roles:
                try:
                    await member.add_roles(reader_role, reason="Default role assignment on join")
                    print(f"Assigned 'Reader' role to {member.name} in {member.guild.name}")
                except discord.Forbidden:
                    print(f"Cannot assign 'Reader' role to {member.name} - insufficient permissions")

            # Assign "Unconfirmed Reader" role to new members
            # (will be removed by verification cog when they get confirmed)
            unconfirmed_role = discord.utils.get(member.guild.roles, name="Unconfirmed Reader")
            confirmed_role = discord.utils.get(member.guild.roles, name="Confirmed Reader")
            if unconfirmed_role and unconfirmed_role not in member.roles:
                # Check if user is a known verified user (prevents race condition with verification cog)
                is_known_verified = False
                verification_cog = self.bot.get_cog("VerificationCog")
                if verification_cog:
                    user_id_str = str(member.id)
                    is_known_verified = user_id_str in verification_cog.verification_data.get("verified_users", {})

                # Only assign if they don't have Confirmed Reader AND aren't a known verified user
                if not is_known_verified and (not confirmed_role or confirmed_role not in member.roles):
                    try:
                        await member.add_roles(unconfirmed_role, reason="Unconfirmed user on join")
                        print(f"Assigned 'Unconfirmed Reader' role to {member.name} in {member.guild.name}")
                    except discord.Forbidden:
                        print(f"Cannot assign 'Unconfirmed Reader' role to {member.name} - insufficient permissions")

        except Exception as e:
            print(f"Error assigning Reader role to {member.name}: {e}")

    async def sync_class_roles_for_member(self, member: discord.Member):
        """Sync class roles for a specific member when they join"""
        try:
            # Get the immersion cog
            immersion_cog = self.bot.get_cog("ImmersionCog")
            if not immersion_cog:
                print(f"[CLASS SYNC] ImmersionCog not found, skipping class role sync for {member.name}")
                return
            
            # Wait for verification cog to finish its work first
            # Verification cog's on_member_join runs in parallel
            await asyncio.sleep(3)
            
            # Ensure the guild is chunked so all members are cached
            if not member.guild.chunked:
                try:
                    print(f"[CLASS SYNC] Chunking guild {member.guild.name} for member {member.name}")
                    await member.guild.chunk(cache=True)
                except Exception as chunk_exc:
                    print(f"[CLASS SYNC] Failed to chunk guild for {member.name}: {chunk_exc}")
            
            # Verify the member is actually in the guild members list
            if member not in member.guild.members:
                print(f"[CLASS SYNC] WARNING: Member {member.name} not found in guild.members after chunking!")
                # Try one more time after a longer delay
                await asyncio.sleep(2)
                if not member.guild.chunked:
                    await member.guild.chunk(cache=True)
            
            # Get user's activity hours to log for debugging
            try:
                activity_repo = immersion_cog.activity_repo
                # Try VN first, then anime as fallback for different bot types
                vn_seconds = await activity_repo.sum_activity_duration(
                    str(member.id), 
                    media_type="visual_novel"
                )
                anime_seconds = await activity_repo.sum_activity_duration(
                    str(member.id), 
                    media_type="anime"
                )
                total_hours = ((vn_seconds or 0) + (anime_seconds or 0)) / 3600
                print(f"[CLASS SYNC] Member {member.name} has {total_hours:.2f} total activity hours")
            except Exception as stat_exc:
                print(f"[CLASS SYNC] Could not fetch activity hours for {member.name}: {stat_exc}")
            
            # Trigger a full role sync for the guild (this will include the new member)
            # source="member_join" marks the sync as triggered by a join event
            print(f"[CLASS SYNC] Starting role sync for {member.name} in {member.guild.name}")
            stats = await immersion_cog.sync_reward_roles_for_guild(member.guild, source="member_join")
            print(f"[CLASS SYNC] Completed role sync for {member.name}: {stats}")
            
        except Exception as e:
            print(f"[CLASS SYNC] Error syncing class roles for {member.name}: {e}")
            import traceback
            traceback.print_exc()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        # Assign default "Reader" role to all new members
        await self.assign_reader_role(member)
        
        # Let verification cog handle quiz level restoration first
        # (It has its own on_member_join listener that runs in parallel)
        # A small delay lets it complete
        await asyncio.sleep(1)
        
        # Now sync class roles for the new member (in case they have existing activity hours)
        # This happens after verification role restore to avoid conflicts
        await self.sync_class_roles_for_member(member)
        
        # Send full welcome message with verification info to quiz channel
        # (Only for NEW users - returning users get welcome back message from verification cog)
        await self.send_welcome_message(member, member.guild)
        
        # Note: The verification cog will send "Welcome back" messages for returning users
        # and will handle level role restoration

async def setup(bot):
    await bot.add_cog(EventsCog(bot))
