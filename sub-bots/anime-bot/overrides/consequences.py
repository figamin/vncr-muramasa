"""
LJTA (Learn Japanese Through Anime) - Anime Consequences Cog
Monthly penalty and reward roles based on logged anime time.

- On the 1st of each month, members who logged under 4 hours of anime in the
  month that just ended get the penalty role, one person per second. Once they
  all have it, one message (with the image) pings the role in the penalty
  channel. No DMs are sent. Logging 1 hour of anime removes the role.
- Logging 15+ hours of anime in a month gives the reward role and an
  announcement. It makes you immune to the penalty and is removed on the 1st of
  the second month after you earned it (earned in October -> removed 1 December).
- Members who first joined after the 15th aren't judged for that first month.

Months are calendar months. The month-end check runs shortly after midnight UTC
on the 1st. A log counts toward the month of the date it was logged for.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SETTINGS - edit IDs, thresholds and messages here.
# ---------------------------------------------------------------------------
PENALTY_ROLE_ID = 1566279338410778715
REWARD_ROLE_ID = 1566279534943408339
PENALTY_CHANNEL_ID = 1566282395752071230       # where the penalty role "locks" people
ANNOUNCEMENT_CHANNEL_ID = 1415732490815016972  # where rewards are announced

PENALTY_THRESHOLD_HOURS = 4   # logging less than this in a month -> penalty
RELEASE_HOURS = 1             # logging this much after the penalty -> role removed
REWARD_THRESHOLD_HOURS = 15   # logging this much in a month -> reward role
LATE_JOIN_DAY = 15            # joined after this day of a month -> not judged that month

# Posted once in the penalty channel after everyone for the month has the role,
# with the image. {role} pings the penalty role; {channel} links the penalty channel.
PENALTY_MESSAGE = (
    "{role} Oh no! It looks you haven't been logging any anime and as punishment have been locked in "
    "{channel} ! You must log some anime using the /log_anime command to regain access to the "
    "rest of the server!"
)
PENALTY_IMAGE = Path(__file__).parent.parent / "assets" / "consequences" / "psych-ward.jpg"

# {user} pings the member.
REWARD_ANNOUNCEMENT = "{user} got a 💮 for logging over 15 hours of anime this month!"

CHECK_INTERVAL_MINUTES = 10   # how often the background check runs

# Month-end penalty roles go out one person at a time. ~400 people at 1 per second
# takes about 7 minutes; the penalty channel message is posted after the last one.
PENALTY_ROLE_INTERVAL_SECONDS = 1


# ---------------------------------------------------------------------------
# Month helpers. Months are "YYYY-MM" strings, which also sort correctly.
# ---------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def month_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def add_months(month: str, n: int) -> str:
    year, mon = (int(part) for part in month.split("-"))
    index = year * 12 + (mon - 1) + n
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_start(month: str) -> datetime:
    year, mon = (int(part) for part in month.split("-"))
    return datetime(year, mon, 1, tzinfo=timezone.utc)


def month_name(month: str) -> str:
    """'2026-10' -> 'October 2026'"""
    return month_start(month).strftime("%B %Y")


def db_timestamp(dt: datetime) -> str:
    """Same format SQLite's CURRENT_TIMESTAMP uses for created_at."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def hours(seconds: int) -> str:
    return f"{seconds / 3600:.1f}h"


class ConsequencesCog(commands.Cog):
    """Monthly anime penalty/reward roles for the anime bot"""

    def __init__(self, bot):
        self.bot = bot
        # The main data/ folder (next to vnbot.db), which Docker keeps between rebuilds
        self.data_dir = Path(__file__).resolve().parents[3] / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "anime_consequences.json"
        self.data = self.load_data()
        self.lock = asyncio.Lock()

    async def cog_load(self):
        self.monthly_check.start()
        self.penalty_sender.start()

    async def cog_unload(self):
        self.monthly_check.cancel()
        self.penalty_sender.cancel()

    # ---------------- saved data ----------------
    #
    # Per server:
    #   enabled        - system on/off
    #   first_month    - first month that can be judged
    #   last_rollover  - month whose start has been processed
    #   penalized      - {user_id: time the penalty role was given (UTC)}
    #   rewards        - {user_id: {"earned": month, "expires": month}} (active reward roles)
    #   reward_months  - {user_id: last month a reward was given}, so it is given at most
    #                    once a month even if the role is removed (by a mod or the expiry test)
    #   first_joined   - {user_id: first join time seen by the bot}
    #   penalty_queue  - {"month": judged month, "users": [user_id, ...] still to penalize,
    #                    "given": how many got the role, "announced": message posted?}
    #   penalty_months - {user_id: month they were last penalized for}, so nobody is
    #                    penalized twice for the same month

    def load_data(self) -> dict:
        if self.data_file.exists():
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data.get("guilds"), dict):
                    return data
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"[Consequences] Error loading data: {e}")
        return {"guilds": {}}

    def save_data(self):
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            logger.error(f"[Consequences] Error saving data: {e}")

    def guild_state(self, guild: discord.Guild) -> dict:
        state = self.data["guilds"].get(str(guild.id))
        if state is None:
            now = now_utc()
            current = month_key(now)
            state = {
                "enabled": True,
                # Same fairness rule as for new members: if the system starts
                # after the 15th, the first month isn't judged.
                "first_month": current if now.day <= LATE_JOIN_DAY else add_months(current, 1),
                "last_rollover": current,
            }
            self.data["guilds"][str(guild.id)] = state
        for key in ("penalized", "rewards", "reward_months", "first_joined", "penalty_queue", "penalty_months"):
            state.setdefault(key, {})
        return state

    def target_guilds(self) -> List[discord.Guild]:
        """Only servers that have the penalty role (other servers are ignored)."""
        return [g for g in self.bot.guilds if g.get_role(PENALTY_ROLE_ID)]

    # ---------------- database ----------------

    def db_path(self) -> str:
        immersion = self.bot.get_cog("ImmersionCog")
        if immersion is not None and getattr(immersion, "db", None) is not None:
            return immersion.db.db_path
        return str(Path(__file__).resolve().parent.parent.parent.parent / "data" / "vnbot.db")

    async def anime_seconds_in_month(self, month: str, user_id: Optional[str] = None) -> Dict[str, int]:
        """Anime seconds per user for logs dated in the given month."""
        sql = (
            "SELECT user_id, SUM(duration) FROM immersion_activities "
            "WHERE media_type = 'anime' AND deleted_at IS NULL AND date >= ? AND date < ?"
        )
        params = [f"{month}-01", f"{add_months(month, 1)}-01"]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " GROUP BY user_id"
        async with aiosqlite.connect(self.db_path()) as db:
            async with db.execute(sql, params) as cursor:
                return {str(row[0]): int(row[1] or 0) async for row in cursor}

    async def anime_logs_created_since(self, since: str, user_id: Optional[str] = None) -> List[Tuple[str, str, int]]:
        """(user_id, created_at, seconds) for anime logs added at or after `since`."""
        sql = (
            "SELECT user_id, created_at, duration FROM immersion_activities "
            "WHERE media_type = 'anime' AND deleted_at IS NULL AND created_at >= ?"
        )
        params = [since]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        async with aiosqlite.connect(self.db_path()) as db:
            async with db.execute(sql, params) as cursor:
                return [(str(r[0]), str(r[1]), int(r[2] or 0)) async for r in cursor]

    # ---------------- rules ----------------

    def first_join(self, state: dict, member: discord.Member) -> Optional[datetime]:
        dates = []
        recorded = state["first_joined"].get(str(member.id))
        if recorded:
            try:
                dates.append(datetime.fromisoformat(recorded))
            except ValueError:
                pass
        if member.joined_at:
            dates.append(member.joined_at)
        return min(dates) if dates else None

    def immunity_reason(self, state: dict, member: discord.Member, month: str) -> Optional[str]:
        """Why this member can't get the penalty for `month`, or None."""
        reward = state["rewards"].get(str(member.id))
        if reward and reward.get("expires", "") > month:
            return f"you have the reward role (earned in {month_name(reward['earned'])}, removed on 1 {month_name(reward['expires'])})"
        reward_role = member.guild.get_role(REWARD_ROLE_ID)
        if reward_role and reward_role in member.roles:
            return "you have the reward role"
        joined = self.first_join(state, member)
        year, mon = (int(part) for part in month.split("-"))
        cutoff = datetime(year, mon, LATE_JOIN_DAY + 1, tzinfo=timezone.utc)
        if joined and joined >= cutoff:
            return f"you joined on {joined:%Y-%m-%d}, after the {LATE_JOIN_DAY}th, so this month doesn't count"
        return None

    # ---------------- actions ----------------

    def penalty_file(self) -> Optional[discord.File]:
        if PENALTY_IMAGE.exists():
            return discord.File(PENALTY_IMAGE, filename=PENALTY_IMAGE.name)
        return None

    async def post_penalty_message(self, guild: discord.Guild, test_member: Optional[discord.Member] = None) -> Optional[str]:
        """Post the penalty message with the image in the penalty channel, pinging the
        penalty role. For an admin test, it pings only that admin instead of the role.
        Returns an error message on failure."""
        channel = guild.get_channel(PENALTY_CHANNEL_ID)
        role = guild.get_role(PENALTY_ROLE_ID)
        if channel is None or role is None:
            return "the penalty channel or role doesn't exist"
        text = PENALTY_MESSAGE.format(role=role.mention, channel=f"<#{PENALTY_CHANNEL_ID}>")
        if test_member is None:
            mentions = discord.AllowedMentions(everyone=False, users=False, roles=[role])
        else:
            text = f"{test_member.mention} (test, the role isn't pinged) {text}"
            mentions = discord.AllowedMentions(everyone=False, roles=False, users=[test_member])
        try:
            await channel.send(text, file=self.penalty_file(), allowed_mentions=mentions)
        except discord.HTTPException as e:
            logger.error(f"[Consequences] Can't post the penalty message: {e}")
            return f"I couldn't post in the penalty channel ({e})."
        return None

    async def give_penalty_role(self, state: dict, member: discord.Member, reason: str) -> Optional[str]:
        """Give the penalty role and record it. Returns an error message on failure."""
        role = member.guild.get_role(PENALTY_ROLE_ID)
        if role is None:
            return "the penalty role doesn't exist"
        try:
            await member.add_roles(role, reason=reason)
        except discord.HTTPException as e:
            logger.error(f"[Consequences] Can't give penalty role to {member}: {e}")
            return f"I couldn't give the penalty role ({e}). Check my role is above it and I have Manage Roles."
        state["penalized"][str(member.id)] = db_timestamp(now_utc())
        self.save_data()
        logger.info(f"[Consequences] Penalized {member} ({member.id}): {reason}")
        return None

    async def release(self, state: dict, member: discord.Member) -> Optional[str]:
        role = member.guild.get_role(PENALTY_ROLE_ID)
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason=f"Logged {RELEASE_HOURS}h of anime")
            except discord.HTTPException as e:
                logger.error(f"[Consequences] Can't remove penalty role from {member}: {e}")
                return f"I couldn't remove the penalty role ({e})."
        state["penalized"].pop(str(member.id), None)
        self.save_data()
        logger.info(f"[Consequences] Released {member} ({member.id})")
        return None

    async def grant_reward(self, state: dict, member: discord.Member, month: str) -> Optional[str]:
        role = member.guild.get_role(REWARD_ROLE_ID)
        if role is None:
            return "the reward role doesn't exist"
        if role not in member.roles:
            try:
                await member.add_roles(role, reason=f"Logged {REWARD_THRESHOLD_HOURS}h+ of anime in {month}")
            except discord.HTTPException as e:
                logger.error(f"[Consequences] Can't give reward role to {member}: {e}")
                return f"I couldn't give the reward role ({e})."
        state["rewards"][str(member.id)] = {"earned": month, "expires": add_months(month, 2)}
        state["reward_months"][str(member.id)] = month
        self.save_data()
        channel = member.guild.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if channel:
            try:
                await channel.send(REWARD_ANNOUNCEMENT.format(user=member.mention))
            except discord.HTTPException as e:
                logger.warning(f"[Consequences] Can't post reward announcement: {e}")
        logger.info(f"[Consequences] Rewarded {member} ({member.id}) for {month}")
        return None

    async def remove_reward(self, state: dict, guild: discord.Guild, user_id: str) -> Optional[str]:
        member = guild.get_member(int(user_id))
        role = guild.get_role(REWARD_ROLE_ID)
        if member and role and role in member.roles:
            try:
                await member.remove_roles(role, reason="Anime reward role expired")
            except discord.HTTPException as e:
                logger.error(f"[Consequences] Can't remove reward role from {member}: {e}")
                return f"I couldn't remove the reward role ({e})."
        state["rewards"].pop(user_id, None)
        self.save_data()
        return None

    # ---------------- checks ----------------

    async def check_releases(self, guild: discord.Guild, state: dict, only: Optional[discord.Member] = None):
        """Remove the penalty role from anyone who has logged enough anime since getting it."""
        penalized = state["penalized"]
        if only is not None:
            penalized = {k: v for k, v in penalized.items() if k == str(only.id)}
        if not penalized:
            return
        rows = await self.anime_logs_created_since(
            min(penalized.values()), user_id=str(only.id) if only else None
        )
        logged: Dict[str, int] = {}
        for user_id, created_at, seconds in rows:
            since = penalized.get(user_id)
            if since and created_at >= since:
                logged[user_id] = logged.get(user_id, 0) + seconds

        role = guild.get_role(PENALTY_ROLE_ID)
        stale_before = db_timestamp(datetime.fromtimestamp(now_utc().timestamp() - 600, timezone.utc))
        for user_id, since in list(penalized.items()):
            member = guild.get_member(int(user_id))
            if member is None:
                continue  # left the server; keep the record so rejoining doesn't clear it
            if role not in member.roles and since < stale_before:
                state["penalized"].pop(user_id, None)  # a moderator removed it
                self.save_data()
                continue
            if logged.get(user_id, 0) >= RELEASE_HOURS * 3600:
                await self.release(state, member)

    async def check_rewards(self, guild: discord.Guild, state: dict, only: Optional[discord.Member] = None):
        """Give the reward role to anyone who reached the threshold this month."""
        month = month_key(now_utc())
        totals = await self.anime_seconds_in_month(month, user_id=str(only.id) if only else None)
        for user_id, seconds in totals.items():
            if seconds < REWARD_THRESHOLD_HOURS * 3600:
                continue
            if state["reward_months"].get(user_id) == month:
                continue  # already rewarded this month
            member = guild.get_member(int(user_id))
            if member is None or member.bot:
                continue
            await self.grant_reward(state, member, month)

    def penalty_skip_reason(self, state: dict, member: Optional[discord.Member], month: str,
                            totals: Dict[str, int]) -> Optional[str]:
        """Why this member shouldn't get the penalty for `month`, or None."""
        if member is None:
            return "left the server"
        if member.bot:
            return "bot"
        role = member.guild.get_role(PENALTY_ROLE_ID)
        if role in member.roles:
            return "already has the penalty role"
        if state["penalty_months"].get(str(member.id)) == month:
            return "already penalized for this month"
        if self.immunity_reason(state, member, month):
            return "immune"
        if totals.get(str(member.id), 0) >= PENALTY_THRESHOLD_HOURS * 3600:
            return "logged enough"
        return None

    async def judge_month(self, guild: discord.Guild, state: dict, month: str):
        """Month-end: queue the members who logged too little in `month`.
        penalty_sender then gives the role one person at a time, and posts the
        penalty channel message once everyone has it."""
        totals = await self.anime_seconds_in_month(month)
        if not guild.chunked:
            await guild.chunk(cache=True)
        queue = [
            str(member.id) for member in guild.members
            if self.penalty_skip_reason(state, member, month, totals) is None
        ]
        # A new month's batch replaces anything left over
        state["penalty_queue"] = {"month": month, "users": queue, "given": 0, "announced": False}
        self.save_data()
        logger.info(f"[Consequences] {guild.name}: judged {month}, queued {len(queue)} member(s)")

    async def send_next_penalty(self, guild: discord.Guild, state: dict):
        """Give the penalty role to the next person in the queue (one per call).
        When the queue is done, post the penalty channel message once."""
        if not state["enabled"]:
            return  # turning the system off also pauses the queue
        queue = state["penalty_queue"]
        month = queue.get("month")
        users = queue.get("users") or []

        if not users:
            if queue.get("given") and not queue.get("announced"):
                # Marked first, so a crash or restart can never post it twice
                queue["announced"] = True
                self.save_data()
                await self.post_penalty_message(guild)
                logger.info(f"[Consequences] {guild.name}: posted the penalty message for {month} ({queue['given']} people)")
            return

        while users:
            user_id = users.pop(0)
            member = guild.get_member(int(user_id))
            # Re-check: they may have left, been released, backdated logs into the month, etc.
            totals = await self.anime_seconds_in_month(month, user_id)
            if self.penalty_skip_reason(state, member, month, totals) is not None:
                self.save_data()
                continue
            error = await self.give_penalty_role(
                state, member, f"Logged under {PENALTY_THRESHOLD_HOURS}h of anime in {month}"
            )
            if error is None:
                state["penalty_months"][user_id] = month
                queue["given"] = queue.get("given", 0) + 1
            self.save_data()
            return

    @tasks.loop(seconds=PENALTY_ROLE_INTERVAL_SECONDS)
    async def penalty_sender(self):
        for guild in self.target_guilds():
            try:
                state = self.guild_state(guild)
                async with self.lock:
                    await self.send_next_penalty(guild, state)
            except Exception as e:
                logger.exception(f"[Consequences] Penalty sender failed in {guild.name}: {e}")

    @penalty_sender.before_loop
    async def before_penalty_sender(self):
        await self.bot.wait_until_ready()

    async def rollover(self, guild: discord.Guild, state: dict):
        """Runs once when a new month starts: judge last month, then expire rewards."""
        current = month_key(now_utc())
        if state["last_rollover"] >= current:
            return
        judged = add_months(current, -1)
        if state["enabled"] and judged >= state["first_month"]:
            await self.judge_month(guild, state, judged)
        # Rewards are removed after judging, so a reward still protects its last month
        for user_id, reward in list(state["rewards"].items()):
            if reward.get("expires", "") <= current:
                await self.remove_reward(state, guild, user_id)
        state["last_rollover"] = current
        self.save_data()

    def record_join_dates(self, guild: discord.Guild, state: dict):
        """Remember everyone's join date, so leaving and rejoining after the 15th
        doesn't count as a new member's first month."""
        changed = False
        for member in guild.members:
            if not member.bot and member.joined_at and str(member.id) not in state["first_joined"]:
                state["first_joined"][str(member.id)] = member.joined_at.isoformat()
                changed = True
        if changed:
            self.save_data()

    def record_existing_roles(self, guild: discord.Guild, state: dict):
        """Rebuild missing records from roles people already have (e.g. given by hand
        while the bot was offline, or if the data file was lost), so a penalty can
        still be lifted by logging and a reward still expires."""
        penalty = guild.get_role(PENALTY_ROLE_ID)
        reward = guild.get_role(REWARD_ROLE_ID)
        month = month_key(now_utc())
        changed = False
        for member in guild.members:
            user_id = str(member.id)
            if penalty in member.roles and user_id not in state["penalized"]:
                state["penalized"][user_id] = db_timestamp(now_utc())
                changed = True
            if reward and reward in member.roles and user_id not in state["rewards"]:
                state["rewards"][user_id] = {"earned": month, "expires": add_months(month, 2)}
                changed = True
        if changed:
            self.save_data()

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def monthly_check(self):
        for guild in self.target_guilds():
            try:
                state = self.guild_state(guild)
                self.record_join_dates(guild, state)
                self.record_existing_roles(guild, state)
                await self.rollover(guild, state)
                async with self.lock:
                    await self.check_releases(guild, state)
                    if state["enabled"]:
                        await self.check_rewards(guild, state)
            except Exception as e:
                logger.exception(f"[Consequences] Check failed in {guild.name}: {e}")

    @monthly_check.before_loop
    async def before_monthly_check(self):
        await self.bot.wait_until_ready()

    # ---------------- events ----------------

    @commands.Cog.listener()
    async def on_anime_log_changed(self, guild: discord.Guild, user):
        """Sent by the immersion cog after someone logs (or undoes a log)."""
        if guild is None or guild.get_role(PENALTY_ROLE_ID) is None:
            return
        member = guild.get_member(user.id)
        if member is None or member.bot:
            return
        try:
            state = self.guild_state(guild)
            async with self.lock:
                await self.check_releases(guild, state, only=member)
                if state["enabled"]:
                    await self.check_rewards(guild, state, only=member)
        except Exception as e:
            logger.exception(f"[Consequences] Log check failed for {member}: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or member.guild.get_role(PENALTY_ROLE_ID) is None:
            return
        state = self.guild_state(member.guild)
        user_id = str(member.id)
        if user_id not in state["first_joined"]:
            state["first_joined"][user_id] = (member.joined_at or now_utc()).isoformat()
            self.save_data()
        # Leaving and rejoining doesn't get rid of the penalty, or lose the reward
        roles = []
        if user_id in state["penalized"]:
            roles.append(member.guild.get_role(PENALTY_ROLE_ID))
        reward = state["rewards"].get(user_id)
        if reward and reward.get("expires", "") > month_key(now_utc()):
            roles.append(member.guild.get_role(REWARD_ROLE_ID))
        roles = [r for r in roles if r]
        if roles:
            try:
                await member.add_roles(*roles, reason="Restored anime penalty/reward role on rejoin")
            except discord.HTTPException as e:
                logger.warning(f"[Consequences] Can't restore roles to {member}: {e}")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Keep the saved data in step with roles a moderator adds or removes by hand."""
        if before.roles == after.roles or after.guild.get_role(PENALTY_ROLE_ID) is None:
            return
        state = self.guild_state(after.guild)
        user_id = str(after.id)
        changed = False

        penalty = after.guild.get_role(PENALTY_ROLE_ID)
        if penalty in after.roles and penalty not in before.roles and user_id not in state["penalized"]:
            state["penalized"][user_id] = db_timestamp(now_utc())
            changed = True
        elif penalty in before.roles and penalty not in after.roles and user_id in state["penalized"]:
            state["penalized"].pop(user_id)
            changed = True

        reward = after.guild.get_role(REWARD_ROLE_ID)
        if reward:
            if reward in after.roles and reward not in before.roles and user_id not in state["rewards"]:
                month = month_key(now_utc())
                state["rewards"][user_id] = {"earned": month, "expires": add_months(month, 2)}
                changed = True
            elif reward in before.roles and reward not in after.roles and user_id in state["rewards"]:
                state["rewards"].pop(user_id)
                changed = True

        if changed:
            self.save_data()

    # ---------------- admin commands ----------------

    @staticmethod
    def is_admin(interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.administrator)

    @app_commands.command(name="manage_consequences", description="[ADMIN] Turn the monthly anime penalty/reward system on or off")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(action="What to do")
    @app_commands.choices(action=[
        app_commands.Choice(name="Show status", value="status"),
        app_commands.Choice(name="Enable", value="enable"),
        app_commands.Choice(name="Disable", value="disable"),
    ])
    async def manage_consequences(self, interaction: discord.Interaction, action: app_commands.Choice[str]):
        if not self.is_admin(interaction):
            await interaction.response.send_message("You need administrator permission for this.", ephemeral=True)
            return
        if interaction.guild.get_role(PENALTY_ROLE_ID) is None:
            await interaction.response.send_message("This server doesn't have the penalty role, so the system doesn't run here.", ephemeral=True)
            return

        state = self.guild_state(interaction.guild)
        if action.value in ("enable", "disable"):
            state["enabled"] = action.value == "enable"
            self.save_data()

        next_check = month_start(add_months(month_key(now_utc()), 1))
        embed = discord.Embed(
            title="Anime consequences",
            description="On" if state["enabled"] else "Off",
            color=discord.Color.green() if state["enabled"] else discord.Color.dark_grey(),
        )
        embed.add_field(name="Next month-end check", value=f"<t:{int(next_check.timestamp())}:F>", inline=False)
        embed.add_field(name="First month judged", value=month_name(state["first_month"]), inline=True)
        embed.add_field(name="Penalized now", value=str(len(state["penalized"])), inline=True)
        embed.add_field(name="Reward holders", value=str(len(state["rewards"])), inline=True)

        queue = state["penalty_queue"]
        waiting = len(queue.get("users") or [])
        if waiting:
            done_at = now_utc().timestamp() + waiting * PENALTY_ROLE_INTERVAL_SECONDS
            queue_text = (
                f"{waiting} people still to get the penalty role, one every "
                f"{PENALTY_ROLE_INTERVAL_SECONDS}s (done around <t:{int(done_at)}:t>). "
                "The penalty channel message is posted after the last one."
            )
        elif queue.get("month"):
            queue_text = (
                f"{month_name(queue['month'])}: {queue.get('given', 0)} penalized; penalty channel message "
                + ("posted." if queue.get("announced") else "not needed (nobody penalized)." if not queue.get("given") else "about to be posted.")
            )
        else:
            queue_text = "Nothing yet"
        embed.add_field(name="Month-end penalty queue", value=queue_text, inline=False)
        embed.set_footer(text=(
            "While off: no new penalties or rewards, and the penalty queue pauses. People who already "
            "have the penalty can still remove it by logging, and reward roles still expire."
        ))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="test_consequence", description="[ADMIN] Test the anime penalty/reward events on yourself")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(event="Which event to test (default: penalty check)")
    @app_commands.choices(event=[
        app_commands.Choice(name=f"Penalty check (under {PENALTY_THRESHOLD_HOURS}h this month)", value="penalty"),
        app_commands.Choice(name=f"Release check ({RELEASE_HOURS}h logged since the penalty)", value="release"),
        app_commands.Choice(name=f"Reward check ({REWARD_THRESHOLD_HOURS}h+ this month)", value="reward"),
        app_commands.Choice(name="Reward expiry", value="expiry"),
    ])
    async def test_consequence(self, interaction: discord.Interaction, event: Optional[app_commands.Choice[str]] = None):
        if not self.is_admin(interaction):
            await interaction.response.send_message("You need administrator permission for this.", ephemeral=True)
            return
        guild = interaction.guild
        if guild.get_role(PENALTY_ROLE_ID) is None:
            await interaction.response.send_message("This server doesn't have the penalty role, so the system doesn't run here.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        member = interaction.user
        state = self.guild_state(guild)
        user_id = str(member.id)
        month = month_key(now_utc())
        choice = event.value if event else "penalty"
        note = "" if state["enabled"] else "\n(The system is currently off; tests still run.)"

        async with self.lock:
            if choice == "penalty":
                penalty_role = guild.get_role(PENALTY_ROLE_ID)
                seconds = (await self.anime_seconds_in_month(month, user_id)).get(user_id, 0)
                immune = self.immunity_reason(state, member, month)
                if penalty_role in member.roles:
                    reply = "You already have the penalty role. Try the release check."
                elif immune:
                    reply = f"No penalty: {immune}. (Logged {hours(seconds)} this month.) Use the reward expiry test to drop the reward role first."
                elif seconds >= PENALTY_THRESHOLD_HOURS * 3600:
                    reply = f"✅ Safe: you've logged {hours(seconds)} of anime this month (the minimum is {PENALTY_THRESHOLD_HOURS}h)."
                else:
                    # Same role and message as the month-end run, but the message pings only
                    # this admin, not the role. Not counted towards the once-a-month rule.
                    error = await self.give_penalty_role(state, member, "Penalty test by an admin")
                    if not error:
                        error = await self.post_penalty_message(guild, test_member=member)
                    reply = error or (
                        f"You've logged {hours(seconds)} this month, under {PENALTY_THRESHOLD_HOURS}h, so you got "
                        f"the penalty role, and the penalty channel message was posted pinging only you. "
                        f"Log {RELEASE_HOURS}h of anime to remove it."
                    )

            elif choice == "release":
                since = state["penalized"].get(user_id)
                if since is None:
                    reply = "You don't have the penalty. Run the penalty check first."
                else:
                    rows = await self.anime_logs_created_since(since, user_id)
                    seconds = sum(s for _, created, s in rows if created >= since)
                    if seconds >= RELEASE_HOURS * 3600:
                        error = await self.release(state, member)
                        reply = error or f"✅ You've logged {hours(seconds)} since the penalty, so the role was removed."
                    else:
                        reply = (
                            f"You've logged {seconds // 60} min of anime since the penalty; "
                            f"{RELEASE_HOURS * 60} min are needed to remove it."
                        )

            elif choice == "reward":
                seconds = (await self.anime_seconds_in_month(month, user_id)).get(user_id, 0)
                if seconds >= REWARD_THRESHOLD_HOURS * 3600:
                    error = await self.grant_reward(state, member, month)
                    reply = error or (
                        f"✅ You've logged {hours(seconds)} this month: you got the reward role and the "
                        f"announcement was posted. It will be removed on 1 {month_name(add_months(month, 2))}."
                    )
                else:
                    reply = f"You've logged {hours(seconds)} this month; the reward needs {REWARD_THRESHOLD_HOURS}h."

            else:  # expiry
                reward_role = guild.get_role(REWARD_ROLE_ID)
                if user_id not in state["rewards"] and not (reward_role and reward_role in member.roles):
                    reply = "You don't have the reward role. Run the reward check first."
                else:
                    error = await self.remove_reward(state, guild, user_id)
                    reply = error or "✅ The reward role was removed, as it would be when it expires. You're no longer immune."

        await interaction.followup.send(reply + note, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ConsequencesCog(bot))
