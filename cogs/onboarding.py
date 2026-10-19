"""In-server verification flow.

Members join freely and land in the limited role. Entry to the rest of the server is
requested from inside, where the public channels are already visible, rather than from a
join form that has to be answered before anything can be seen.

The flow is: entry button, modal, private thread, quiz, manual review. The quiz result is
recognised by the verification cog, which hands off here instead of granting roles when an
application is open; level progression for existing members is untouched by that.
"""

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands, tasks

from utils.database import (
    Database,
    format_approver_ids as _format_ids,
    parse_approver_ids as _parse_ids,
)
from utils.message_filter import defang_masked_links

ENTRY_BUTTON_ID = "onboarding:start"
APPROVE_PREFIX = "onboarding:approve:"
REJECT_PREFIX = "onboarding:reject:"
# These two carry no application id: the presser identifies the applicant and the
# thread identifies the case, so one registration serves every rejection ever posted.
REAPPLY_BUTTON_ID = "onboarding:reapply"
COMMAND_BUTTON_ID = "onboarding:command"

# The deck the entry quiz draws from, published so the quiz stays a test of effort
# rather than of recall.
WORD_LIST_URL = "https://vnclub.org/level1/"

# How often the native join-request queue is checked. discord.py has no parser for
# GUILD_JOIN_REQUEST_CREATE, so nothing is dispatched to listen for and polling is the
# only route. A minute is well inside what an applicant will tolerate and costs one
# request per guild per minute.
JOIN_REQUEST_POLL_SECONDS = 60
GUIDE_URL = "https://vnclub.org/guide/"
# The community's shared room on the kizuna texthooker, linked by /kizuna.
KIZUNA_ROOM_URL = "https://kizuna-texthooker-ui.app/rooms/vncr"
CONTACT_BUTTON_ID = "onboarding:contact"

CONFIRMED_ROLE = "Confirmed Reader"

# Held for as long as an application is open, from submitting the form until it is
# approved or rejected. Scoped to the whole of that rather than to the stretch before the
# quiz, so filtering the member list by it answers one question with one meaning: who is
# waiting on the moderators. Which stage they are at is a separate question the funnel
# counts answer.
APPLYING_ROLE = "Applying"

# Thread titles carry the id because a display name can change after the fact.
THREAD_NAME = "verify-{user_id}"

# How a closed review card reads, keyed by the status written to the row. Leaving the
# server is not a decision anyone made, so it carries no decider and a neutral colour:
# the review channel is the only record of who was turned down, and a card that reports
# a departure as a rejection puts someone in that record who does not belong there. An
# unrecognised status closes the card without claiming a cause for it.
CARD_OUTCOMES = {
    "approved": ("Approved", discord.Color.green(), "decider not recorded"),
    "rejected": ("Rejected", discord.Color.dark_red(), "decider not recorded"),
    "left": ("Closed", discord.Color.light_grey(), "the applicant left the server"),
}
CARD_OUTCOME_DEFAULT = ("Closed", discord.Color.light_grey(), "no longer open")

# The approval as the applicant reads it in their own thread. Shared, because access
# can also be granted from outside this flow, and someone let in by either route should
# read the same sentence.
APPROVED_OUTCOME = (
    "{mention} You are in. Welcome to VN Club.\n\n"
    "承認されました。VN Clubへようこそ！"
)

READING_QUESTION = "What are you reading in Japanese, or want to start?"
# Discord caps a modal label at 45 characters and a placeholder at 100, truncating
# either without warning. The labels are English because they have to stay under that
# cap and the field order makes them guessable; the Japanese rides in the placeholders,
# which have the room for it.
READING_LABEL = "What are you reading in Japanese?"
READING_PLACEHOLDER = (
    "What you're partway through, or want to start. "
    "今読んでいる作品、またはこれから読みたい作品を書いてください。"
)
# Plural, and paragraph-styled below: a single-line field reads as a request for exactly
# one link and discourages offering the rest.
PROFILE_LABEL = "Links showing your history, if any"
PROFILE_PLACEHOLDER = (
    "VNDB, EGS, AniList, MAL, Steam, a blog. As many as you like. "
    "読書・視聴履歴が分かるリンクがあれば記入してください。複数可。"
)


def _parse_timestamp(value) -> Optional[datetime]:
    """SQLite hands back naive UTC strings; callers need something comparable."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _utc_now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# The image the entry embed carries, by filename stem, in assets/other. Named separately
# from the welcome banners because those are picked at random and this one is deliberate.
ENTRY_BANNER_STEM = "hikaru_banner"


def _entry_banner() -> Optional[Path]:
    """The entry embed's image, if the file is present.

    Optional by design: with nothing there the embed simply carries no image, so the flow
    still works on a server that has not supplied one.
    """
    folder = Path(__file__).resolve().parent.parent / "assets" / "other"
    for ext in ("webp", "png", "jpg", "jpeg", "gif"):
        candidate = folder / f"{ENTRY_BANNER_STEM}.{ext}"
        if candidate.exists():
            return candidate
    return None


def _trim(text: Optional[str], limit: int) -> str:
    """Truncate before any escaping, so an escape cannot be split in half.

    Stripped before the empty test, not after: an answer of nothing but spaces satisfies a
    required modal field but leaves nothing to show, and an embed field with an empty value
    is rejected outright, which would lose the whole review card.
    """
    text = (text or "").strip()
    if not text:
        return "(blank)"
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _inert(text: Optional[str], limit: int = 900) -> str:
    """Applicant text lands in an embed a reviewer reads, so it must not resolve as a
    masked link or a mention. Truncation happens first, so a split cannot land inside an
    escape. Bold and italics are left alone: they are cosmetic, and escaping every marker
    would mangle the Japanese and kaomoji this text is mostly made of."""
    trimmed = _trim(text, limit)
    if trimmed == "(blank)":
        return trimmed
    return discord.utils.escape_mentions(defang_masked_links(trimmed))


class ApplicationModal(discord.ui.Modal, title="Join VN Club / 参加申請"):
    reading = discord.ui.TextInput(
        label=READING_LABEL,
        placeholder=READING_PLACEHOLDER,
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=600,
    )
    profile = discord.ui.TextInput(
        label=PROFILE_LABEL,
        placeholder=PROFILE_PLACEHOLDER,
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=600,
    )

    def __init__(self, cog: "OnboardingCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_application(
            interaction, str(self.reading.value), str(self.profile.value or "")
        )


class RejectReasonModal(discord.ui.Modal, title="Reject application"):
    note = discord.ui.TextInput(
        label="Reason (shown to the applicant)",
        placeholder="Optional. Say what would make a new application work.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400,
    )

    def __init__(self, cog: "OnboardingCog", app_id: int):
        super().__init__()
        self.cog = cog
        self.app_id = app_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.finish_rejection(interaction, self.app_id, str(self.note.value or ""))


class EntryView(discord.ui.View):
    """Lives on a persistent message, so it carries no per-user state."""

    def __init__(self, cog: Optional["OnboardingCog"] = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(discord.ui.Button(
            label="Learning guide",
            style=discord.ButtonStyle.link,
            url=GUIDE_URL,
        ))
        self.add_item(discord.ui.Button(
            label="Word list",
            style=discord.ButtonStyle.link,
            url=WORD_LIST_URL,
        ))

    @discord.ui.button(
        label="Start verification",
        # Green rather than blurple: it sits beside two grey link buttons and is the one
        # thing a new arrival has to press.
        style=discord.ButtonStyle.success,
        emoji="\U0001f4d6",
        custom_id=ENTRY_BUTTON_ID,
    )
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.cog or interaction.client.get_cog("OnboardingCog")
        if cog is None:
            await interaction.response.send_message(
                "Verification is not available right now. Please try again later.",
                ephemeral=True,
            )
            return
        await cog.handle_entry_click(interaction)


class ThreadHelpView(discord.ui.View):
    """Rides the pinned instructions so the two things a stuck applicant needs are always
    at the top of the thread, however far Kotoba has pushed the original message down."""

    def __init__(self, cog: Optional["OnboardingCog"] = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(discord.ui.Button(
            label="Show the command again",
            style=discord.ButtonStyle.primary,
            custom_id=COMMAND_BUTTON_ID,
        ))
        self.add_item(discord.ui.Button(
            label="Contact moderators",
            style=discord.ButtonStyle.secondary,
            custom_id=CONTACT_BUTTON_ID,
        ))
        self.add_item(discord.ui.Button(
            label="Word list",
            style=discord.ButtonStyle.link,
            url=WORD_LIST_URL,
        ))


class RejectedView(discord.ui.View):
    """Sits on the rejection message so the two useful next steps are one press away."""

    def __init__(self, cog: Optional["OnboardingCog"] = None, contacted: bool = False):
        super().__init__(timeout=None)
        self.cog = cog

        reapply = discord.ui.Button(
            label="Apply again",
            style=discord.ButtonStyle.primary,
            custom_id=REAPPLY_BUTTON_ID,
        )
        contact = discord.ui.Button(
            label="Contact moderators",
            style=discord.ButtonStyle.secondary,
            custom_id=CONTACT_BUTTON_ID,
            disabled=contacted,
        )
        self.add_item(reapply)
        self.add_item(contact)


class ReviewView(discord.ui.View):
    """One per application; the id travels in the custom_id so it survives a restart."""

    def __init__(self, app_id: int, decided: bool = False):
        super().__init__(timeout=None)
        self.app_id = app_id

        approve = discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"{APPROVE_PREFIX}{app_id}",
            disabled=decided,
        )
        reject = discord.ui.Button(
            label="Reject",
            style=discord.ButtonStyle.danger,
            custom_id=f"{REJECT_PREFIX}{app_id}",
            disabled=decided,
        )
        self.add_item(approve)
        self.add_item(reject)


class OnboardingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = Database()

    # ------------------------------------------------------------------
    # Native join requests
    #
    # Discord's own "Apply to Join" is kept on because it is the only Access setting
    # that gives a stranger a way in from the server tag: the three settings are
    # mutually exclusive and Discoverable is not available to every server. It requires at
    # least one question and a human decision, which would mean reviewing everyone
    # twice. Approving them automatically reduces it to the rules gate it is wanted for,
    # and leaves the real vetting to the in-server flow.
    # ------------------------------------------------------------------

    @tasks.loop(seconds=JOIN_REQUEST_POLL_SECONDS)
    async def auto_approve_join_requests(self):
        for guild in list(self.bot.guilds):
            try:
                settings = await self.db.get_onboarding_settings(guild.id)
                if not settings.get("auto_approve_join_requests"):
                    continue
                await self._drain_join_requests(guild)
            except Exception as exc:
                print(f"Onboarding: join-request sweep failed for {guild.id}: {exc}")

    @auto_approve_join_requests.before_loop
    async def before_auto_approve_join_requests(self):
        await self.bot.wait_until_ready()

    async def _drain_join_requests(self, guild: discord.Guild):
        """Approve everything currently submitted for one guild.

        Routed through bot.http so it inherits discord.py's rate limiting rather than
        raising a second HTTP stack. Neither endpoint is wrapped by the library, so the
        Route is built by hand.
        """
        listing = discord.http.Route(
            "GET", "/guilds/{guild_id}/requests", guild_id=guild.id
        )
        try:
            data = await self.bot.http.request(
                listing, params={"status": "SUBMITTED", "limit": 100}
            )
        except discord.Forbidden:
            # 403 here means the guild is not on Apply to Join, or the permission was
            # removed. Logged once per sweep rather than raising, since the flow works
            # without it.
            print(
                f"Onboarding: cannot read join requests for {guild.id}. "
                "Check Access is set to Apply to Join and the bot can kick members."
            )
            return
        except discord.HTTPException as exc:
            print(f"Onboarding: listing join requests failed for {guild.id}: {exc}")
            return

        pending = (data or {}).get("guild_join_requests") or []
        for entry in pending:
            request_id = entry.get("id")
            if not request_id:
                continue
            user = entry.get("user") or {}
            action = discord.http.Route(
                "PATCH", "/guilds/{guild_id}/requests/{request_id}",
                guild_id=guild.id, request_id=request_id,
            )
            try:
                await self.bot.http.request(action, json={"action": "APPROVED"})
                print(
                    f"Onboarding: auto-approved join request {request_id} "
                    f"for {user.get('id')} in {guild.id}"
                )
            except discord.HTTPException as exc:
                print(
                    f"Onboarding: could not approve join request {request_id} "
                    f"in {guild.id}: {exc}"
                )

    async def cog_load(self):
        # These ids are fixed, so one registration each answers every message carrying
        # them, in every guild, for as long as those messages exist.
        self.bot.add_view(EntryView(self))
        self.bot.add_view(RejectedView(self))
        self.bot.add_view(ThreadHelpView(self))
        await self._reattach_review_views()

    @commands.Cog.listener()
    async def on_ready(self):
        # Started here rather than in cog_load: a loop started before the gateway is up
        # can silently never tick. on_ready re-fires on RESUME, hence the guard.
        if not self.auto_approve_join_requests.is_running():
            self.auto_approve_join_requests.start()

    async def cog_unload(self):
        self.auto_approve_join_requests.cancel()

    async def _reattach_review_views(self):
        """Re-bind review buttons after a restart, or open cards stop responding."""
        for guild in self.bot.guilds:
            try:
                pending = await self.db.get_applications_by_status(
                    guild.id, "awaiting_review", limit=200
                )
            except Exception as exc:
                print(f"Onboarding: cannot load pending applications: {exc}")
                continue
            for app in pending:
                if app.get("review_message_id"):
                    self.bot.add_view(
                        ReviewView(app["id"]), message_id=app["review_message_id"]
                    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # Discord marks no account as "has Nitro", but several things only a subscriber can
    # own are readable, so the presence of any of them is strong evidence. Reported as
    # the individual observations rather than as a verdict, since each can also be a
    # lapsed subscription.
    @staticmethod
    def _nitro_signals(member: discord.Member, fetched: Optional[discord.User]) -> str:
        signals = []
        # Tested against None throughout rather than for truthiness: an Asset defines
        # __len__, so a bool() on one asks a question about its length instead of its
        # existence.
        avatar = member.avatar
        if avatar is None and fetched is not None:
            avatar = fetched.avatar
        if avatar is not None and avatar.is_animated():
            signals.append("animated avatar")
        if fetched is not None and fetched.banner is not None:
            signals.append("profile banner")
        decoration = getattr(member, "avatar_decoration", None)
        if decoration is None and fetched is not None:
            decoration = getattr(fetched, "avatar_decoration", None)
        if decoration is not None:
            signals.append("avatar decoration")
        if member.guild_avatar is not None:
            signals.append("per-server avatar")
        if member.premium_since is not None:
            stamp = discord.utils.format_dt(member.premium_since, style="R")
            signals.append(f"boosting since {stamp}")
        if not signals:
            return ""
        return "\n".join(f"\u2713 {s}" for s in signals)

    @staticmethod
    def _guild_tag(member: discord.Member) -> str:
        """The small server tag shown beside a name, when the library exposes it.

        Unrelated to this project's own `primary_guild` / `primary_guild_id`, which mean
        a member's home server for immersion scoping (cogs/immersion.py,
        immersion_users.primary_guild_id). Same word, different subject.

        Read defensively for two separate reasons. The attribute only exists from
        discord.py 2.6, so an older pin has to fall through silently. And where it does
        exist the container is never None: an account with no tag yields a default object
        whose fields are all empty, so the only sound test is on the tag itself. The id
        can be absent while a tag is displayed, so it cannot be used as the gate either.
        """
        primary = getattr(member, "primary_guild", None)
        if primary is None:
            return ""
        tag = getattr(primary, "tag", None)
        if not tag:
            return ""
        line = _inert(str(tag), 40)
        source = getattr(primary, "id", None)
        if source is not None:
            line += f" (server {source})"
        return line

    @staticmethod
    def _account_badges(member: discord.Member) -> str:
        """Every public flag the account carries.

        Discord serves an account's description and pronouns only to a user token, so
        neither can appear here; public flags are what a bot is given. They are listed in
        full rather than filtered, because a reviewer reads them as corroboration and the
        judgement of which one matters belongs to the reviewer.
        """
        flags = member.public_flags
        named = [
            ("spammer", "\u26a0\ufe0f flagged as spam by Discord"),
            ("staff", "Discord staff"),
            ("partner", "Discord partner"),
            ("discord_certified_moderator", "certified moderator"),
            ("hypesquad", "HypeSquad events"),
            ("hypesquad_bravery", "HypeSquad Bravery"),
            ("hypesquad_brilliance", "HypeSquad Brilliance"),
            ("hypesquad_balance", "HypeSquad Balance"),
            ("bug_hunter", "bug hunter"),
            ("bug_hunter_level_2", "bug hunter level 2"),
            ("early_supporter", "early supporter"),
            ("active_developer", "active developer"),
            ("verified_bot_developer", "early verified bot developer"),
            ("system", "system account"),
            ("team_user", "team account"),
            ("verified_bot", "verified bot"),
        ]
        marks = [label for attr, label in named if getattr(flags, attr, False)]
        return "\n".join(marks)

    @staticmethod
    def _presence(member: discord.Member) -> str:
        """Status and what they are doing, which the presences intent makes available."""
        bits = [str(member.status).replace("dnd", "do not disturb")]
        for activity in member.activities[:3]:
            name = getattr(activity, "name", None)
            if isinstance(activity, discord.CustomActivity):
                text = activity.name or ""
                if text:
                    bits.append(f"custom: {_inert(text, 80)}")
            elif name:
                verb = {
                    discord.ActivityType.playing: "playing",
                    discord.ActivityType.listening: "listening to",
                    discord.ActivityType.watching: "watching",
                    discord.ActivityType.streaming: "streaming",
                    discord.ActivityType.competing: "competing in",
                }.get(getattr(activity, "type", None), "")
                bits.append(f"{verb} {_inert(name, 60)}".strip())
        return "\n".join(bits)

    @staticmethod
    def _server_context(member: discord.Member) -> str:
        """Where they already stand in this server, and how far their reach extends."""
        bits = []
        if member.pending:
            bits.append("\u26a0\ufe0f has not completed membership screening")
        if member.is_timed_out():
            until = discord.utils.format_dt(member.timed_out_until, style="R")
            bits.append(f"\u26a0\ufe0f timed out until {until}")
        if member.nick:
            bits.append(f"nickname: {_inert(member.nick, 60)}")
        roles = [r.mention for r in member.roles if not r.is_default()]
        if roles:
            bits.append("roles: " + " ".join(roles[:10]))
        # Only counts guilds this bot is also in, so it is a floor rather than a total.
        mutual = len(member.mutual_guilds)
        if mutual > 1:
            bits.append(f"also in {mutual - 1} other server(s) this bot is in")
        return "\n".join(bits)

    async def _ensure_thread(
        self, channel: discord.TextChannel, member: discord.Member, app_id: int
    ) -> Optional[discord.Thread]:
        """The member's verification thread, reopened or rebuilt as needed.

        The thread is the only place an applicant and a moderator can talk before access
        is granted, and it is also where both outcomes are delivered, so a deleted one
        has to be replaceable at any point in the flow rather than only at the start.
        """
        thread = await self._find_existing_thread(channel, member)
        if thread is not None and (thread.archived or thread.locked):
            # A previous round closed it, so it has to be reopened to be written to.
            try:
                await thread.edit(archived=False, locked=False)
            except discord.HTTPException as exc:
                print(f"Onboarding: cannot reopen thread {thread.id}: {exc}")
                thread = None

        if thread is None:
            try:
                thread = await channel.create_thread(
                    name=THREAD_NAME.format(user_id=member.id),
                    type=discord.ChannelType.private_thread,
                    invitable=False,
                    auto_archive_duration=10080,
                    reason=f"Verification application {app_id}",
                )
            except discord.Forbidden:
                print(f"Onboarding: no permission to open a thread in {channel.id}")
                return None
            except discord.HTTPException as exc:
                print(f"Onboarding: thread creation failed for {app_id}: {exc}")
                return None

        await self.db.update_verification_application(app_id, thread_id=thread.id)
        try:
            await thread.add_user(member)
        except discord.HTTPException as exc:
            # Not on its own fatal, since the mention in the thread's first message adds
            # them too. Recorded because if neither lands, the applicant is pointed at a
            # private thread they cannot open and nothing else would say so.
            print(f"Onboarding: cannot add {member.id} to thread {thread.id}: {exc}")
        return thread

    def _is_own_private_thread(self, thread) -> bool:
        """Whether this is a thread the bot itself opened for verification.

        The thread name is derived from a public user id, so a name match alone would let
        anyone who can create a thread in the entry channel pre-claim one and have the
        bot post another member's answers into it. Ownership and privacy are what make
        the promise in the thread's own first message true.
        """
        return (
            isinstance(thread, discord.Thread)
            and thread.type is discord.ChannelType.private_thread
            and thread.owner_id == (self.bot.user.id if self.bot.user else None)
        )

    async def _find_existing_thread(
        self, channel: discord.TextChannel, member: discord.Member
    ) -> Optional[discord.Thread]:
        """A member's verification thread, if one is still around.

        The application row is not enough on its own: it is cleared when a decision is
        made, while the thread it names outlives it, so keying only on the row produces a
        second thread named identically to the first. The name is derived from the user
        id, which makes it the durable key, and the row is consulted first only because it
        is the cheaper lookup.
        """
        wanted = THREAD_NAME.format(user_id=member.id)

        latest = await self.db.get_latest_application(member.guild.id, member.id)
        if latest and latest.get("thread_id"):
            found = member.guild.get_channel_or_thread(latest["thread_id"])
            if self._is_own_private_thread(found):
                return found

        for thread in channel.threads:
            if thread.name == wanted and self._is_own_private_thread(thread):
                return thread

        try:
            async for thread in channel.archived_threads(private=True, joined=False, limit=100):
                if thread.name == wanted and self._is_own_private_thread(thread):
                    return thread
        except discord.HTTPException as exc:
            # Listing archived private threads needs Manage Threads. Without it the
            # active-thread scan above is all the coverage there is.
            print(f"Onboarding: cannot list archived threads: {exc}")

        return None

    async def _applying_role(
        self, guild: discord.Guild, create: bool = True
    ) -> Optional[discord.Role]:
        """Fetch the marker role, creating it once if the server has never had one.

        create is off wherever the marker is only ever dropped. A server that has no such
        role has nobody holding it, so making one there would add a role to the list for
        a member on their way out of the flow rather than into it.
        """
        role = discord.utils.get(guild.roles, name=APPLYING_ROLE)
        if role is not None or not create:
            return role
        try:
            return await guild.create_role(
                name=APPLYING_ROLE,
                # Colourless, matching the other verification roles. A colour here
                # would become the display colour for a member holding only role 0,
                # publishing a status the private thread exists to keep quiet.
                colour=discord.Colour.default(),
                reason="Onboarding: marks an application in progress",
            )
        except discord.HTTPException as exc:
            print(f"Onboarding: cannot create the {APPLYING_ROLE} role: {exc}")
            return None

    async def _set_applying(self, member: discord.Member, applying: bool):
        """Add or drop the marker. Never fatal: the flow works without it."""
        try:
            role = await self._applying_role(member.guild, create=applying)
            if role is None:
                return
            if applying and role not in member.roles:
                await member.add_roles(role, reason="Onboarding: application opened")
            elif not applying and role in member.roles:
                await member.remove_roles(role, reason="Onboarding: application closed")
        except discord.HTTPException as exc:
            print(f"Onboarding: cannot update the {APPLYING_ROLE} role: {exc}")

    def entry_view(self) -> "EntryView":
        """A fresh view for any message that should carry the entry button. The button's
        custom_id is fixed, so the registration made in cog_load answers every copy."""
        return EntryView(self)

    def _quiz_terms(self) -> str:
        """Pass mark and timing, read from the live quiz settings rather than repeated in
        prose, so retuning the command cannot leave the description behind. Empty when
        the quiz has no settings; the line it joins reads correctly without it."""
        cfg = getattr(self._verification_cog(), "LEVEL_1_QUIZ", None) or {}
        score = cfg.get("score_limit")
        missed = cfg.get("max_missed")
        seconds = (cfg.get("time_limit") or 0) // 1000
        if not score:
            return ""
        return f"Score **{score}**, at most {missed} mistakes, {seconds}s each."

    def _quiz_terms_ja(self) -> str:
        """The pass mark in Japanese, or nothing when the quiz has no settings.

        Worded as /quiz_verify already words the same three numbers, so one quiz is not
        described two ways. Empty rather than a placeholder when unset, since the line it
        sits in reads correctly without it.
        """
        cfg = getattr(self._verification_cog(), "LEVEL_1_QUIZ", None) or {}
        score = cfg.get("score_limit")
        if not score:
            return ""
        missed = cfg.get("max_missed")
        seconds = (cfg.get("time_limit") or 0) // 1000
        return f"合格点は{score}点、ミスは{missed}回まで、制限時間は1問{seconds}秒です。"

    async def entry_channel_id(self, guild_id: int) -> Optional[int]:
        """The entry channel as an int.

        config.json holds channel ids as strings and this table holds them as integers,
        so anything comparing the two has to coerce or it silently reports two identical
        channels as different.
        """
        settings = await self.db.get_onboarding_settings(guild_id)
        raw = settings.get("entry_channel_id")
        return int(raw) if raw else None

    def _verification_cog(self):
        return self.bot.get_cog("VerificationCog")

    def _entry_quiz_command(self) -> str:
        cog = self._verification_cog()
        return getattr(cog, "LEVEL_1_COMMAND", "k!quiz vncr1 8 mmq=6 atl=25")

    async def _cooldown_remaining(self, guild_id: int, user_id: int) -> Optional[datetime]:
        """When a previously rejected applicant becomes eligible again."""
        settings = await self.db.get_onboarding_settings(guild_id)
        days = int(settings.get("reapply_cooldown_days") or 0)
        if days <= 0:
            return None
        latest = await self.db.get_latest_application(guild_id, user_id)
        if not latest or latest.get("status") != "rejected":
            return None
        decided = _parse_timestamp(latest.get("decided_at"))
        if not decided:
            return None
        eligible = decided + timedelta(days=days)
        return eligible if eligible > datetime.now(timezone.utc) else None

    # ------------------------------------------------------------------
    # Applicant side
    # ------------------------------------------------------------------

    async def handle_entry_click(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Use this in the server.", ephemeral=True
            )
            return

        settings = await self.db.get_onboarding_settings(guild.id)
        if not settings.get("enabled"):
            await interaction.response.send_message(
                "Verification is closed at the moment. Please check back soon.",
                ephemeral=True,
            )
            return

        member = interaction.user
        if isinstance(member, discord.Member):
            if discord.utils.get(member.roles, name=CONFIRMED_ROLE):
                await interaction.response.send_message(
                    "You already have access. Nothing to do here.", ephemeral=True
                )
                return

        existing = await self.db.get_open_application(guild.id, member.id)
        if existing:
            thread_id = existing.get("thread_id")
            thread = guild.get_channel_or_thread(thread_id) if thread_id else None

            if isinstance(thread, discord.Thread):
                state = (
                    "waiting on the quiz"
                    if existing.get("status") == "awaiting_quiz"
                    else "waiting on a reviewer"
                )
                await interaction.response.send_message(
                    f"You already have an application open in {thread.mention}, "
                    f"currently {state}.",
                    ephemeral=True,
                )
                return

            # The thread is gone, deleted by hand or lost with its channel. Pointing at
            # it renders as an unknown channel and the button then refuses forever, so
            # the row is treated as recoverable rather than as a block.
            if existing.get("status") == "awaiting_review":
                # Rebuilt rather than explained away. Without a thread there is nowhere
                # for the applicant to reach a moderator, and nowhere for the decision
                # to be delivered when it comes: _post_outcome silently posts nothing.
                await interaction.response.defer(ephemeral=True, thinking=True)
                await self._rebuild_review_thread(interaction, existing)
                return
            # Still at the quiz with nowhere to take it: let them start over. The modal
            # reuses this row rather than opening a second one.

        eligible_at = await self._cooldown_remaining(guild.id, member.id)
        if eligible_at:
            stamp = discord.utils.format_dt(eligible_at, style="D")
            await interaction.response.send_message(
                f"You can apply again on {stamp}.", ephemeral=True
            )
            return

        await interaction.response.send_modal(ApplicationModal(self))

    async def _rebuild_review_thread(self, interaction: discord.Interaction, app: dict):
        """Give an application already in review somewhere to land again."""
        guild, member = interaction.guild, interaction.user
        settings = await self.db.get_onboarding_settings(guild.id)
        channel = guild.get_channel(settings.get("entry_channel_id") or 0)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "Verification is misconfigured. Please tell a moderator.", ephemeral=True
            )
            return

        thread = await self._ensure_thread(channel, member, app["id"])
        if thread is None:
            await interaction.followup.send(
                "I could not reopen your verification thread. Please tell a moderator.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Your application / 申請内容",
            description=(
                "This thread was recreated. Your application is still with the "
                "moderators and you do not need to take the quiz again.\n\n"
                "スレッドを作り直しました。申請は引き続き審査中ですので、クイズをもう一度"
                "受ける必要はありません。"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name=READING_QUESTION, value=_inert(app.get("reading_answer")), inline=False
        )
        embed.add_field(
            name="Links", value=_inert(app.get("profile_answer")), inline=False
        )
        embed.set_footer(text=f"Application #{app['id']}")
        try:
            await thread.send(
                content=member.mention, embed=embed, view=ThreadHelpView(self)
            )
        except discord.HTTPException as exc:
            print(f"Onboarding: cannot post into rebuilt thread {thread.id}: {exc}")

        await interaction.followup.send(
            f"Your application is still with the moderators. I have reopened your "
            f"thread so you can reach us there: {thread.mention}",
            ephemeral=True,
        )

    async def handle_application(
        self, interaction: discord.Interaction, reading: str, profile: str
    ):
        guild = interaction.guild
        member = interaction.user
        await interaction.response.defer(ephemeral=True, thinking=True)

        settings = await self.db.get_onboarding_settings(guild.id)
        entry_channel_id = settings.get("entry_channel_id")
        channel = guild.get_channel(entry_channel_id) if entry_channel_id else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "Verification is misconfigured. Please tell a moderator.", ephemeral=True
            )
            return

        # An application still at the quiz is updated in place. Anything else, including
        # a decided one being retried from the rejection message, opens a fresh row.
        existing = await self.db.get_open_application(guild.id, member.id)
        if existing and existing.get("status") == "awaiting_quiz":
            app_id = existing["id"]
            await self.db.update_verification_application(
                app_id,
                reading_answer=reading.strip(),
                profile_answer=profile.strip() or None,
                thread_id=None,
            )
        else:
            app_id = await self.db.create_verification_application(
                guild.id, member.id, reading.strip(), profile.strip() or None
            )
        if app_id is None:
            await interaction.followup.send(
                "Something went wrong opening your application. Please try again.",
                ephemeral=True,
            )
            return

        thread = await self._ensure_thread(channel, member, app_id)
        if thread is None:
            await interaction.followup.send(
                "I could not open your verification thread. Please tell a moderator.",
                ephemeral=True,
            )
            return

        if isinstance(member, discord.Member):
            await self._set_applying(member, True)

        verification = self._verification_cog()
        already = verification.get_highest_quiz_level(member.id) if verification else 0
        if already >= 1:
            # The quiz has been passed before and the record survives a departure, so
            # asking for it again tests nothing. Only the application is outstanding.
            await self.db.update_verification_application(app_id, thread_id=thread.id)
            app = await self.db.get_application_by_id(app_id)
            await self._advance_to_review(
                member, app, already,
                f"{member.mention} You have already passed level {already}, so there is "
                "no quiz this time. Your application is with the moderators and you "
                "will hear back in this thread.\n\n"
                f"すでにレベル{already}に合格しているため、今回クイズはありません。"
                "審査結果はこのスレッドでお知らせします。",
            )
            await interaction.followup.send(
                f"You have already passed level {already}, so no quiz is needed. Your "
                f"application is with the moderators: {thread.mention}",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Almost there / あと少し",
            description=(
                "One step left: a short vocab quiz. Kotoba runs it and only listens to "
                "you, so the command has to come from your own message.\n\n"
                "**1.** Copy the command in the message below (long-press on phone).\n"
                "**2.** Paste it here and send it.\n"
                "**3.** Type the reading of each word in kana or romaji. "
                f"{self._quiz_terms()} Unlimited tries.\n\n"
                "クイズはKotobaが出題します。コマンドはあなた自身のメッセージとして"
                "送る必要があります。\n"
                "**1.** 下のメッセージのコマンドをコピー（スマホは長押し）\n"
                "**2.** ここに貼り付けて送信\n"
                f"**3.** 読みをかな・ローマ字で入力。{self._quiz_terms_ja()}"
                "何度でも挑戦できます。\n\n"
                "Only you and the moderators can see this thread."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name=READING_QUESTION, value=_inert(reading), inline=False
        )
        embed.add_field(
            name="Links", value=_inert(profile) if profile.strip() else "(skipped)",
            inline=False,
        )
        embed.set_footer(text=f"Application #{app_id}")

        # Kotoba has to be present in a private thread to answer in it.
        kotoba_id = getattr(self._verification_cog(), "KOTOBA_ID", None)
        mention = f"<@{kotoba_id}> " if kotoba_id else ""
        instructions = await thread.send(
            content=f"{member.mention} {mention}".strip(),
            embed=embed,
            view=ThreadHelpView(self),
        )
        # Pinned so it stays reachable from the thread header. A quiz attempt costs
        # several Kotoba messages, so after a few tries the instructions are a long
        # scroll back on a phone.
        #
        # A reused thread would otherwise collect one pin per attempt, all of them the
        # same message with stale answers, so the bot's previous pins are cleared first.
        # Only the bot's own are touched: a moderator may have pinned something in here
        # deliberately. pins() is an async iterator from discord.py 2.6 onwards.
        try:
            async for pinned in thread.pins():
                if pinned.author.id == self.bot.user.id:
                    await pinned.unpin(reason="Superseded by a newer application")
        except discord.HTTPException as exc:
            print(f"Onboarding: cannot clear old pins in {thread.id}: {exc}")
        try:
            await instructions.pin()
        except discord.HTTPException as exc:
            print(f"Onboarding: cannot pin instructions in {thread.id}: {exc}")
        # Sent bare, as the whole of its own message. A phone's copy-message action takes
        # the message verbatim, so any surrounding text or code fence is copied with it
        # and has to be picked back out. The embed above carries the instructions, and in
        # a thread holding only these two messages it does not need styling to be found.
        await thread.send(self._entry_quiz_command())

        await interaction.followup.send(
            f"Your verification thread is ready: {thread.mention}", ephemeral=True
        )

    # ------------------------------------------------------------------
    # Quiz handoff
    # ------------------------------------------------------------------

    async def intercept_quiz_pass(self, member: discord.Member, level: int) -> bool:
        """Route an entry pass into review. True means roles were deliberately not granted.

        The test is whether the member is already IN, not what any application row says.
        Passing the quiz is not itself a way in: the command stays in the applicant's
        thread, the thread is deliberately left open after a decision, and the Kotoba
        listener is not restricted to any channel. Keying this off an open application
        would let every other state through to the grant, so a rejected applicant could
        re-run the quiz and admit themselves while their review card still read Rejected.
        Only an existing member climbing levels may reach the direct grant path.
        """
        if discord.utils.get(member.roles, name=CONFIRMED_ROLE) is not None:
            return False  # already a member, so this is level progression

        try:
            app = await self.db.get_open_application(member.guild.id, member.id)
        except Exception as exc:
            # Cannot prove they are entitled, so do not grant.
            print(f"Onboarding: application lookup failed, suppressing grant: {exc}")
            return True
        status = app.get("status") if app else None

        # With the flow off, or before it has been configured, this cog is inert and the
        # direct quiz path is the only way in. Without this the interception still fires
        # while the entry button refuses, which leaves a guild with no route to access at
        # all: the pass earns nothing and the button that would replace it is closed.
        #
        # Read after the lookup above, not before it. A failed settings read returns the
        # defaults, in which the flow is off, so asking first would turn a database fault
        # into a grant rather than into the refusal the lookup already gives.
        settings = await self.db.get_onboarding_settings(member.guild.id)
        if not settings.get("enabled"):
            return False

        if status == "awaiting_review":
            # The other quiz event already claimed this one; suppress the grant anyway.
            return True

        if status != "awaiting_quiz":
            # Rejected, left, or never applied. The pass earns nothing on its own, and
            # saying so beats silence: otherwise the bot looks broken to someone who
            # just watched themselves pass.
            await self._explain_pass_earns_nothing(member, level)
            return True

        await self._advance_to_review(
            member, app, level,
            f"Passed level {level}. {member.mention}, your application is with "
            "the moderators now. If it is approved you will get access to the "
            "rest of the server, and either way you will hear back in this thread.\n\n"
            f"レベル{level}に合格しました。申請はモデレーターの審査に進みます。"
            "結果はこのスレッドでお知らせします。",
        )
        return True

    async def _explain_pass_earns_nothing(self, member: discord.Member, level: int):
        """Tell someone their pass did not admit them, and where to go instead.

        Reached by a rejected or departed applicant re-running the quiz, so it must not
        read as an error. Delivered to their own thread when one survives, otherwise by
        DM, because the pass may have happened in any channel Kotoba can see.
        """
        settings = await self.db.get_onboarding_settings(member.guild.id)
        entry_id = settings.get("entry_channel_id")
        where = f" Start again in <#{entry_id}>." if entry_id else ""
        body = (
            f"{member.mention} Nice, level {level} passed. Access is not granted by the "
            f"quiz alone though, so this does not let you in on its own.{where}"
        )

        latest = await self.db.get_latest_application(member.guild.id, member.id)
        thread = None
        if latest and latest.get("thread_id"):
            thread = member.guild.get_channel_or_thread(latest["thread_id"])
        if isinstance(thread, discord.Thread):
            try:
                await thread.send(body)
                return
            except discord.HTTPException:
                pass
        try:
            await member.send(body)
        except discord.HTTPException:
            print(f"Onboarding: could not tell {member.id} their pass earns nothing")

    async def _advance_to_review(
        self, member: discord.Member, app: dict, level: int, thread_note: str
    ) -> bool:
        """Claim the application for review and tell both sides.

        Reached either by passing the entry quiz or by already holding a level, so the
        message that lands in the thread is supplied by the caller.
        """
        if not await self.db.claim_application_for_review(
            app["id"], level, _utc_now_sql()
        ):
            # One pass reaches the bot as two events, so the loser of that race belongs
            # here and has changed nothing. A failed write reads identically and leaves
            # someone who passed with no card and no message, so both are recorded.
            print(
                f"Onboarding: application #{app['id']} not moved to review for "
                f"{member.id}; another event holds it, or the write failed"
            )
            return False

        thread = None
        if app.get("thread_id"):
            thread = member.guild.get_channel_or_thread(app["thread_id"])
        if isinstance(thread, discord.Thread):
            try:
                await thread.send(thread_note)
            except discord.HTTPException as exc:
                print(
                    f"Onboarding: cannot tell {member.id} that application "
                    f"#{app['id']} reached review: {exc}"
                )
        else:
            # The card still posts, so the reviewers see the application; only the
            # applicant is left without the one message that says the quiz advanced them.
            print(
                f"Onboarding: no thread for application #{app['id']}, so {member.id} "
                "is not told the quiz advanced them"
            )

        await self._post_review_card(member, app["id"], level, thread)
        return True

    async def _post_review_card(
        self, member: discord.Member, app_id: int, level: int,
        thread: Optional[discord.Thread]
    ):
        settings = await self.db.get_onboarding_settings(member.guild.id)
        channel_id = settings.get("review_channel_id")
        channel = member.guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            print(f"Onboarding: no review channel set for guild {member.guild.id}")
            return

        app = await self.db.get_application_by_id(app_id) or {}
        created = _parse_timestamp(member.created_at)

        embed = discord.Embed(
            title=f"Application #{app_id}",
            # The mention is the whole point of this line: it is clickable, so the
            # reviewer reaches the profile without copying an id out of the footer.
            description=member.mention,
            color=discord.Color.gold(),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(
            name=READING_QUESTION, value=_inert(app.get("reading_answer")), inline=False
        )
        embed.add_field(
            name="Links", value=_inert(app.get("profile_answer")), inline=False
        )
        embed.add_field(name="Quiz", value=f"Passed level {level}", inline=True)
        embed.add_field(
            name="Account made",
            value=discord.utils.format_dt(member.created_at, style="R"),
            inline=True,
        )
        if member.joined_at:
            # Distance between joining and applying separates someone who looked around
            # from someone who arrived and pressed the button immediately.
            embed.add_field(
                name="Joined server",
                value=discord.utils.format_dt(member.joined_at, style="R"),
                inline=True,
            )
        if member.global_name and member.global_name != member.name:
            embed.add_field(name="Display name", value=_inert(member.global_name, 100),
                            inline=True)
        # Banner and decoration live only on a fetched user; the cached member never
        # carries them, and they are two of the Nitro tells, so this is worth the call.
        fetched = None
        try:
            fetched = await self.bot.fetch_user(member.id)
        except discord.HTTPException as exc:
            print(f"Onboarding: cannot fetch user {member.id}: {exc}")

        nitro = self._nitro_signals(member, fetched)
        if nitro:
            embed.add_field(name="Nitro signals", value=nitro, inline=True)
        tag = self._guild_tag(member)
        if tag:
            embed.add_field(name="Guild tag", value=tag, inline=True)
        badges = self._account_badges(member)
        if badges:
            embed.add_field(name="Account flags", value=badges, inline=True)
        presence = self._presence(member)
        if presence:
            embed.add_field(name="Presence", value=presence, inline=True)
        context = self._server_context(member)
        if context:
            embed.add_field(name="In this server", value=context[:1024], inline=False)
        if thread:
            embed.add_field(name="Thread", value=thread.mention, inline=True)
        embed.set_footer(text=f"User ID {member.id}")

        if fetched is not None and fetched.banner is not None:
            embed.set_image(url=fetched.banner.url)

        # Labelled with the reviewer role but never notifying with it. The card lands in a
        # channel reviewers already watch, so a notification per application is noise
        # rather than news. "Contact moderators" still pings, because that one is somebody
        # asking for a person.
        content = None
        mentions = discord.AllowedMentions.none()
        try:
            waiting = await self.db.get_applications_by_status(
                member.guild.id, "awaiting_review"
            )
            others = [row for row in waiting if row["id"] != app_id]
            if not others:
                settings = await self.db.get_onboarding_settings(member.guild.id)
                role_id = settings.get("moderator_role_id")
                role = member.guild.get_role(role_id) if role_id else None
                if role is not None:
                    content = f"{role.mention} an application is waiting."
        except Exception as exc:
            print(f"Onboarding: cannot decide whether to ping reviewers: {exc}")

        try:
            message = await channel.send(
                content=content, embed=embed, view=ReviewView(app_id),
                allowed_mentions=mentions,
            )
        except discord.HTTPException as exc:
            print(f"Onboarding: cannot post review card for {app_id}: {exc}")
            return
        await self.db.update_verification_application(
            app_id, review_message_id=message.id
        )

    # ------------------------------------------------------------------
    # Reviewer side
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        """Close an open application when its applicant leaves.

        Nothing else in the bot listens for a departure, so without this an applicant who
        joins, starts, and leaves stays in awaiting_quiz forever and is counted as a
        person still working through the flow. Raw, because a member who left is often no
        longer in cache by the time the event lands.
        """
        try:
            app = await self.db.get_open_application(payload.guild_id, payload.user.id)
            if not app:
                return
            # Conditional, like every other transition. An unguarded write here could
            # land after an approval that was already in flight and overwrite it, undoing
            # a decision whose roles, announcement and DM had all gone out.
            claimed = await self.db.claim_application_decision(
                app["id"], "left", 0, _utc_now_sql(), "Left the server",
                from_statuses=self.db.OPEN_STATUSES,
            )
            if claimed:
                guild = self.bot.get_guild(payload.guild_id)
                if guild is not None:
                    await self._finalise_card(guild, app, app["id"], "left", None)
        except Exception as exc:
            print(f"Onboarding: cannot close application on leave: {exc}")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Settle an application when the access role arrives from outside the flow.

        Granting Confirmed Reader is what an approval ultimately does, and it can also be
        done by an admin command or by hand on the member, which leaves the row, the
        marker role, the thread and the review card exactly as they were. The card
        records no decider here: the event carries the roles and not who changed them.
        """
        if before.roles == after.roles:
            return
        if discord.utils.get(after.roles, name=CONFIRMED_ROLE) is None:
            return
        if discord.utils.get(before.roles, name=CONFIRMED_ROLE) is not None:
            return
        try:
            await self.settle_external_grant(after)
        except Exception as exc:
            print(f"Onboarding: cannot settle application on role grant: {exc}")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if custom_id.startswith(APPROVE_PREFIX):
            await self._on_decision(interaction, custom_id[len(APPROVE_PREFIX):], True)
        elif custom_id.startswith(REJECT_PREFIX):
            await self._on_decision(interaction, custom_id[len(REJECT_PREFIX):], False)
        elif custom_id == REAPPLY_BUTTON_ID:
            await self.handle_entry_click(interaction)
        elif custom_id == CONTACT_BUTTON_ID:
            await self.handle_contact(interaction)
        elif custom_id == COMMAND_BUTTON_ID:
            await self.handle_resend_command(interaction)

    async def handle_resend_command(self, interaction: discord.Interaction):
        """Put the quiz command back at the bottom of the thread.

        Kotoba posts several messages per attempt, so after a few tries the original
        command is far out of reach on a phone. Sent as a new message rather than as an
        ephemeral one so it can still be tapped to copy.
        """
        thread = interaction.channel
        member = interaction.user
        if not isinstance(thread, discord.Thread) or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Use this inside your verification thread.", ephemeral=True
            )
            return
        if thread.name != THREAD_NAME.format(user_id=member.id):
            await interaction.response.send_message(
                "This is not your application thread.", ephemeral=True
            )
            return
        await interaction.response.send_message(self._entry_quiz_command())
        # The repost is bare so it copies clean. The reminder of what to do with it goes
        # to the presser alone, since pressing this button instead of sending the command
        # is the usual sign of not knowing that the command has to come from them.
        await interaction.followup.send(
            "Copy that message and send it here yourself. The quiz starts when Kotoba "
            "sees the command from you, not from the bot.\n"
            "そのメッセージをコピーして、ご自身で送信してください。"
            "あなたが送信したコマンドにだけKotobaが反応します。",
            ephemeral=True,
        )

    async def handle_contact(self, interaction: discord.Interaction):
        """Bring a moderator into the applicant's own thread."""
        thread = interaction.channel
        member = interaction.user
        if not isinstance(thread, discord.Thread) or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Use this inside your verification thread.", ephemeral=True
            )
            return

        # The thread is named for its applicant, so the name is the ownership test. A
        # moderator already in the thread has no need of the button.
        if thread.name != THREAD_NAME.format(user_id=member.id):
            await interaction.response.send_message(
                "This is not your application thread.", ephemeral=True
            )
            return

        settings = await self.db.get_onboarding_settings(interaction.guild.id)
        role_id = settings.get("moderator_role_id")
        role = interaction.guild.get_role(role_id) if role_id else None

        if role is not None:
            body = (
                f"{role.mention} {member.mention} would like to talk about their "
                "application."
            )
            mentions = discord.AllowedMentions(roles=[role], users=[member])
        else:
            # Without a configured role the thread is still the right place to wait.
            print(f"Onboarding: no moderator role set for guild {interaction.guild.id}")
            body = (
                f"{member.mention} Thanks, a moderator will pick this up here. Feel free "
                "to add anything else in the meantime."
            )
            mentions = discord.AllowedMentions(users=[member])

        await interaction.response.send_message(body, allowed_mentions=mentions)

        # One call is enough; the thread is now on their radar and stays there. Rebuilt
        # from the message so this works on whichever view carried the button.
        try:
            view = discord.ui.View.from_message(interaction.message)
            for child in view.children:
                if getattr(child, "custom_id", None) == CONTACT_BUTTON_ID:
                    child.disabled = True
            await interaction.message.edit(view=view)
        except (discord.HTTPException, ValueError):
            pass

    async def approver_ids(self, guild_id: int) -> list:
        settings = await self.db.get_onboarding_settings(guild_id)
        return _parse_ids(settings.get("approver_ids"))

    async def can_decide(self, guild_id: int, user_id: int) -> bool:
        """Whether one person may decide applications.

        A named list rather than a permission or a role. Both of those are held for
        unrelated reasons and change without anyone revisiting who should be admitting
        members, so a routine grant elsewhere would silently widen who can let strangers
        in. An empty list admits nobody rather than falling back to a permission, so a
        half-configured flow cannot quietly open itself; the list is managed by an
        administrator, who can always add themselves.
        """
        return user_id in await self.approver_ids(guild_id)

    async def _on_decision(
        self, interaction: discord.Interaction, raw_id: str, approve: bool
    ):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member) or not (
            await self.can_decide(guild.id, interaction.user.id)
        ):
            await interaction.response.send_message(
                "You are not on the approver list for applications.", ephemeral=True
            )
            return
        try:
            app_id = int(raw_id)
        except ValueError:
            return

        app = await self.db.get_application_by_id(app_id)
        if not app or app.get("status") != "awaiting_review":
            await interaction.response.send_message(
                "That application has already been decided.", ephemeral=True
            )
            return

        if approve:
            await self.finish_approval(interaction, app_id)
        else:
            await interaction.response.send_modal(RejectReasonModal(self, app_id))

    # ------------------------------------------------------------------
    # Decisions. The core takes no Interaction so the web console can call it;
    # the two Discord handlers below are adapters that report the result back.
    # ------------------------------------------------------------------

    async def approve_application(
        self, guild: discord.Guild, app_id: int, decided_by_id: int,
        decider_label: Optional[str] = None,
    ) -> tuple:
        """Grant access. Returns (ok, message) with the message meant for the decider."""
        app = await self.db.get_application_by_id(app_id)
        if not app:
            return False, f"Application #{app_id} does not exist."
        if app.get("status") != "awaiting_review":
            return False, f"Application #{app_id} has already been decided."
        if guild is None or int(app["guild_id"]) != guild.id:
            return False, "That application belongs to a different server."

        member = guild.get_member(app["user_id"])
        if member is None:
            await self.db.claim_application_decision(
                app_id, "left", decided_by_id, _utc_now_sql(), "Left before review"
            )
            # Otherwise the card keeps live buttons that can only ever answer
            # "already decided".
            await self._finalise_card(guild, app, app_id, "left", None)
            return False, "That member is no longer in the server."

        # Claimed before any role is touched, so two moderators deciding opposite ways
        # in the same window cannot both act: whichever loses the claim changes nothing.
        # A grant that fails afterwards puts the row back below, so losing the race
        # costs a retry rather than stranding the application.
        if not await self.db.claim_application_decision(
            app_id, "approved", decided_by_id, _utc_now_sql()
        ):
            return False, f"Application #{app_id} was decided by someone else first."

        passed = int(app.get("quiz_level") or 1)
        verification = self._verification_cog()
        granted = False
        failure = "Could not assign the roles. Check my role position and try again."
        if verification:
            # An applicant can already hold a level without holding Confirmed Reader:
            # restore_user_roles hands the numeric role back to any returning member with
            # tracked progress, but only grants Confirmed Reader to one recorded as
            # verified. Such a member applies, passes the entry quiz, and would otherwise
            # be written down to level 1 here, since _perform_role_updates strips every
            # other level role and set_highest_quiz_level overwrites the record. The
            # sequential path refuses that case outright; this one keeps the higher of
            # the two instead, because refusing would leave them without access.
            level = max(passed, verification.get_highest_quiz_level(member.id))
            try:
                await verification.ensure_roles_exist(guild)
                # _perform_role_updates treats a role handed to it as None as nothing to
                # do and still reports success, so an absent Confirmed Reader would grant
                # access to no channels while the applicant is told they are in and
                # recorded as verified. Resolved here so that case fails the grant and
                # releases the row below instead.
                confirmed_role = discord.utils.get(guild.roles, name=CONFIRMED_ROLE)
                if confirmed_role is None:
                    # create_role builds the role from the HTTP response but leaves the
                    # guild's role cache to the gateway event that follows, so a role
                    # created a moment ago can still be absent from it. The authoritative
                    # read keeps that window from reading as a role nobody can create.
                    try:
                        confirmed_role = discord.utils.get(
                            await guild.fetch_roles(), name=CONFIRMED_ROLE
                        )
                    except discord.HTTPException as exc:
                        print(f"Onboarding: could not list roles for {guild.id}: {exc}")
                if confirmed_role is None:
                    print(
                        f"Onboarding: {CONFIRMED_ROLE} does not exist and could not be "
                        f"created, so application #{app_id} cannot be approved"
                    )
                    failure = (
                        f"The {CONFIRMED_ROLE} role does not exist and I could not "
                        "create it. Add it, then approve again."
                    )
                else:
                    granted = await verification._perform_role_updates(
                        member,
                        discord.utils.get(guild.roles, name=str(level)),
                        discord.utils.get(guild.roles, name="0"),
                        confirmed_role,
                        level,
                    )
            except Exception as exc:
                # A raised grant is as much a failure as one that returns False, and both
                # have to reach the release below or the row stays claimed with no roles.
                print(f"Onboarding: role grant failed on application #{app_id}: {exc}")
        else:
            level = passed

        if not granted:
            # Released back into the queue. Left claimed, the row would read approved
            # while the member holds nothing, and no button could correct it.
            await self.db.update_verification_application(
                app_id, status="awaiting_review", decided_by=None,
                decided_at=None, decision_note=None,
            )
            return False, failure

        # Written only after the roles actually landed. Recording someone as verified on
        # a failed grant would let restore_user_roles admit them on a later rejoin
        # without anyone having approved them.
        if verification:
            verification.add_verified_user(member.id)
            verification.set_highest_quiz_level(member.id, level)
            verification.save_verification_data()

        await self._set_applying(member, False)
        await self._post_outcome(
            app, guild, APPROVED_OUTCOME.format(mention=member.mention)
        )
        # The direct quiz path announces the arrival, DMs the member and points them at
        # the next level, all from _process_verification, which the review handoff
        # bypasses. Approval is the moment those become true, so they are issued here
        # instead of at the pass.
        if verification is not None:
            try:
                # Only reads .guild off its first argument despite the parameter name,
                # so a Member serves and no Interaction is needed.
                await verification._send_welcome_announcement(
                    member, member, level, had_server_access_before=False
                )
            except Exception as exc:
                print(f"Onboarding: welcome announcement failed: {exc}")

            # The next level goes here rather than into the verification thread. That
            # thread exists to get someone in and to talk to a moderator; levelling is a
            # normal server activity that belongs in the channels everyone else uses.
            lines = [
                f"Your application to VN Club was approved. You have "
                f"completed Level {level} and now have access to the server.",
                f"VN Clubへの申請が承認されました。レベル{level}をクリアし、"
                "サーバーを利用できるようになりました。ようこそ！",
            ]
            # Kept to the commands. No channel tour: the channels a newcomer can be
            # pointed at are the ones they could already see before verifying, so naming
            # them at the moment they gain everything else reads backwards.
            #
            # English only. Logging, levels and profile cards are learner tooling, and a
            # Japanese summary of them serves nobody. The approval itself is bilingual
            # above, because the outcome matters to anyone.
            lines.append(
                "A few things the bot does:\n"
                "`/log_vn` to log what you read\n"
                "`/kizuna` to link your kizuna account so reading logs itself\n"
                "`/roles` to pick up optional roles\n"
                "`/help` for the rest\n\n"
                # Angle-bracketed so a DM does not carry a link preview.
                f"The VN Club kizuna room: <{KIZUNA_ROOM_URL}>"
            )

            nxt = self._next_level_command(level)
            if nxt:
                # A DM is outside the guild, so the channel is named as a mention that
                # resolves for the reader rather than as a bare instruction to find it.
                quiz_id = (await self.db.get_onboarding_settings(guild.id)).get(
                    "quiz_channel_id"
                )
                where = f"in <#{quiz_id}>" if quiz_id else "in the server"
                where_ja = f"<#{quiz_id}>" if quiz_id else "サーバー内"
                lines.append(
                    f"There are more levels if you want them. Copy the command below "
                    f"and send it {where} whenever you like."
                    f"\nこの先のレベルにも挑戦できます。次に進みたい場合は、下のコマンドを"
                    f"コピーして{where_ja}に送信してください。"
                )
            try:
                await member.send("\n\n".join(lines))
                if nxt:
                    # Its own message for the same reason as the entry command: a copied
                    # message carries everything in it, so the command travels alone.
                    await member.send(nxt)
            except discord.Forbidden:
                pass  # DMs closed, and the thread already carries the same news.
            except discord.HTTPException as exc:
                print(f"Onboarding: approval DM failed: {exc}")

        await self._finalise_card(
            guild, app, app_id, "approved", decider_label or f"<@{decided_by_id}>"
        )
        return True, f"Approved application #{app_id}."

    async def reject_application(
        self, guild: discord.Guild, app_id: int, decided_by_id: int, note: str,
        decider_label: Optional[str] = None,
    ) -> tuple:
        """Decline the application. Returns (ok, message) for the decider."""
        app = await self.db.get_application_by_id(app_id)
        if not app:
            return False, f"Application #{app_id} does not exist."
        # Rejection accepts a row still at the quiz as well. Nothing else can move one
        # out of that state, so without this an applicant who abandons the quiz stays in
        # the queue and holds the Applying role with no way for anyone to clear them.
        if app.get("status") not in self.db.OPEN_STATUSES:
            return False, f"Application #{app_id} has already been decided."
        if guild is None or int(app["guild_id"]) != guild.id:
            return False, "That application belongs to a different server."

        reason = (note or "").strip()
        if not await self.db.claim_application_decision(
            app_id, "rejected", decided_by_id, _utc_now_sql(), reason or None,
            from_statuses=self.db.OPEN_STATUSES,
        ):
            return False, f"Application #{app_id} was decided by someone else first."

        rejected_member = guild.get_member(app["user_id"])
        if rejected_member is not None:
            await self._set_applying(rejected_member, False)

        settings = await self.db.get_onboarding_settings(guild.id)
        days = int(settings.get("reapply_cooldown_days") or 0)
        when = (
            f"You are welcome to apply again in {days} days."
            if days > 0 else "You are welcome to apply again whenever you like."
        )
        # Addressed to the applicant, who is otherwise not told the outcome at all.
        who = f"{rejected_member.mention} " if rejected_member else ""
        # Assembled in blocks so the quoted reason cannot absorb the lines after it:
        # a blockquote runs to the end of its line, so the boilerplate has to start
        # its own paragraph rather than be concatenated on.
        blocks = [
            f"{who}Thanks for applying. We are not able to approve this one right now."
        ]
        if reason:
            blocks.append("> " + _inert(reason, 400).replace("\n", "\n> "))
        # Branched alongside the English above it. A fixed Japanese line here would tell a
        # Japanese-reading applicant to reapply whenever they like while the cooldown is
        # actively blocking the entry button.
        # Cushioned more than the English, deliberately. "Thanks for applying" and "right
        # now" soften the English on their own; a bare 承認できませんでした lands colder in
        # Japanese than the sentence it translates, so the thanks and 残念ながら carry that
        # weight instead. The buttons are named here too, since a reader following only
        # the Japanese would otherwise never be told they exist.
        when_ja = (
            f"{days}日後から再申請できます。" if days > 0
            else "いつでも再申請できます。"
        )
        closing = (
            "\n\nご申請ありがとうございました。残念ながら、今回は承認できませんでした。"
            f"{when_ja}\n下のボタンから、再申請するか、モデレーターにお問い合わせください。"
        )
        # The outcome is delivered into the thread and nowhere else. _post_outcome
        # reopens an archived thread and rebuilds a deleted one, so a missing thread is
        # not a reason to route this elsewhere, and the reply buttons only work inside a
        # guild thread anyway. The remaining undeliverable case is the bot being unable
        # to open a thread in the entry channel at all, which is reported to the decider
        # rather than papered over, since it stops every other applicant too.
        blocks.append(
            when
            + "\nUse the buttons below to send a new application, or to ask "
            "us about it."
            + closing
        )
        delivered = await self._post_outcome(
            app, guild, "\n\n".join(blocks), view=RejectedView(self)
        )
        await self._finalise_card(
            guild, app, app_id, "rejected", decider_label or f"<@{decided_by_id}>"
        )
        if not delivered:
            return True, (
                f"Rejected application #{app_id}, but I could not open their thread, "
                "so they have not been told. Check my thread permissions in the entry "
                "channel."
            )
        return True, f"Rejected application #{app_id}."

    async def settle_external_grant(
        self, member: discord.Member, decided_by_id: Optional[int] = None,
        decider_label: Optional[str] = None,
    ) -> Optional[int]:
        """Close an open application whose access was granted outside this flow.

        Confirmed Reader can be handed out by an admin command or by hand, which gives an
        applicant everything an approval gives them without touching the row behind it.
        Left alone they keep the marker role, their thread is never answered, and the
        review card stays pressable. The roles are in place by the time this runs, so the
        bookkeeping and the message into the thread are all that is left to do.

        decided_by_id is None where nothing names who acted, which a role change arriving
        on the gateway does not. Returns the application settled, or None when there was
        nothing open to settle.
        """
        guild = member.guild
        app = await self.db.get_open_application(guild.id, member.id)
        if app is None:
            # The marker can outlive the row it belongs to, and this is the one moment
            # someone holding it is known to be in rather than waiting.
            await self._set_applying(member, False)
            return None
        app_id = int(app["id"])
        # Conditional like every other transition here, so a decision already in flight
        # when the roles landed is not overwritten by this one.
        if not await self.db.claim_application_decision(
            app_id, "approved", decided_by_id, _utc_now_sql(),
            "Confirmed outside the application flow",
            from_statuses=self.db.OPEN_STATUSES,
        ):
            return None
        await self._set_applying(member, False)
        await self._post_outcome(
            app, guild, APPROVED_OUTCOME.format(mention=member.mention)
        )
        await self._finalise_card(guild, app, app_id, "approved", decider_label)
        return app_id

    async def finish_approval(self, interaction: discord.Interaction, app_id: int):
        """Discord adapter: defer, run the core, report."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        _, message = await self.approve_application(
            interaction.guild, app_id, interaction.user.id,
            decider_label=interaction.user.mention,
        )
        await interaction.followup.send(message, ephemeral=True)

    async def finish_rejection(
        self, interaction: discord.Interaction, app_id: int, note: str
    ):
        """Discord adapter: defer, run the core, report."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        _, message = await self.reject_application(
            interaction.guild, app_id, interaction.user.id, note,
            decider_label=interaction.user.mention,
        )
        await interaction.followup.send(message, ephemeral=True)

    def _next_level_command(self, level: int) -> Optional[str]:
        """The command for the level above this one, or None at the top.

        Nothing else in the server explains that levels continue past the entry quiz, so
        without this an approved member has no way to discover the rest of them.
        """
        verification = self._verification_cog()
        if verification is None:
            return None
        try:
            settings = verification.get_level_settings(level + 1)
        except Exception:
            return None  # Past the top level, so there is nothing to point at.
        return (settings or {}).get("command")

    async def _post_outcome(
        self, app: dict, guild: discord.Guild, message: str,
        view: Optional[discord.ui.View] = None,
    ):
        """Deliver the decision into the applicant's thread and leave it open.

        Nothing archives here. A rejection's buttons have to stay pressable, and either
        outcome can raise a question worth answering. The thread carries a seven day
        auto-archive, so it leaves the active list on its own once it goes quiet.
        """
        thread_id = app.get("thread_id")
        if guild is None:
            return False
        thread = guild.get_channel_or_thread(thread_id) if thread_id else None
        if not isinstance(thread, discord.Thread) or thread.archived or thread.locked:
            # A decision can land days after the thread auto-archived, and an archived
            # thread is dropped from the guild cache and cannot be written to until it
            # is reopened. A row can also name no thread at all, since the id is written
            # only once one opens. _ensure_thread covers both: it keys on the thread name
            # rather than on the row, reopens what it finds, and opens one when there is
            # nothing to find.
            settings = await self.db.get_onboarding_settings(guild.id)
            channel = guild.get_channel(settings.get("entry_channel_id") or 0)
            member = guild.get_member(app["user_id"])
            thread = None
            if isinstance(channel, discord.TextChannel) and member is not None:
                thread = await self._ensure_thread(channel, member, app["id"])
        if thread is None:
            print(
                f"Onboarding: no thread for application {app['id']}, "
                "outcome not delivered there"
            )
            return False
        try:
            if view is not None:
                await thread.send(message, view=view)
            else:
                await thread.send(message)
            return True
        except discord.HTTPException as exc:
            print(f"Onboarding: cannot post outcome to thread {thread.id}: {exc}")
            return False

    async def _finalise_card(
        self, guild: discord.Guild, app: dict, app_id: int, outcome: str,
        decider_label: Optional[str],
    ):
        """Grey out the buttons so a decided card cannot be pressed twice.

        outcome is the status written to the row, so the card and the funnel say the same
        thing about the same application. An applicant who left was not turned down by
        anyone, and a card that reads as a rejection puts a decision nobody made into the
        one place the review history is kept.

        decider_label is rendered as-is, so a Discord decider passes their mention and a
        console decider passes whatever names them. A bare user mention is literally
        "<@id>", which is why the id alone makes a serviceable default. It is None where
        no one decided, and the outcome then supplies the whole line.
        """
        message_id = app.get("review_message_id")
        if not message_id:
            return
        settings = await self.db.get_onboarding_settings(guild.id)
        channel = guild.get_channel(settings.get("review_channel_id") or 0)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException as exc:
            # A card left ungreyed reads as still waiting, which is the one thing the
            # review channel is relied on to show correctly.
            print(f"Onboarding: cannot read the review card for #{app_id}: {exc}")
            return
        verb, colour, unattributed = CARD_OUTCOMES.get(outcome, CARD_OUTCOME_DEFAULT)
        embed = message.embeds[0] if message.embeds else discord.Embed()
        embed.color = colour
        embed.add_field(
            name=verb,
            value=f"by {decider_label}" if decider_label else unattributed,
            inline=False,
        )
        try:
            await message.edit(embed=embed, view=ReviewView(app_id, decided=True))
        except discord.HTTPException as exc:
            print(f"Onboarding: cannot close the review card for #{app_id}: {exc}")

    # ------------------------------------------------------------------
    # Admin
    # ------------------------------------------------------------------

    @discord.app_commands.command(
        name="manage_onboarding",
        description="[ADMIN] Configure the in-server verification flow",
    )
    @discord.app_commands.default_permissions(administrator=True)
    @discord.app_commands.checks.has_permissions(administrator=True)
    @discord.app_commands.guild_only()
    @discord.app_commands.describe(
        action="What to do",
        channel="Entry channel to post the button in, or the review channel",
        role="Role pinged when a rejected applicant asks to talk",
        announcements="Linked in the welcome message as somewhere already open",
        events="Linked in the welcome message as somewhere already open",
        resources="Linked in the welcome message as somewhere already open",
        days="Days before a rejected applicant may apply again, 0 for none",
    )
    @discord.app_commands.choices(action=[
        discord.app_commands.Choice(name="Post entry message", value="post"),
        discord.app_commands.Choice(name="Set entry channel", value="entrychannel"),
        discord.app_commands.Choice(name="Set review channel", value="review"),
        discord.app_commands.Choice(name="Set moderator role", value="modrole"),
        discord.app_commands.Choice(name="Set quiz channel", value="quizchannel"),
        discord.app_commands.Choice(name="Auto-approve join requests: on", value="autoapprove_on"),
        discord.app_commands.Choice(name="Auto-approve join requests: off", value="autoapprove_off"),
        discord.app_commands.Choice(name="Set tour channels", value="tour"),
        discord.app_commands.Choice(name="Enable", value="enable"),
        discord.app_commands.Choice(name="Disable", value="disable"),
        discord.app_commands.Choice(name="Set reapply cooldown", value="cooldown"),
        discord.app_commands.Choice(name="Add approver", value="approver_add"),
        discord.app_commands.Choice(name="Remove approver", value="approver_remove"),
        discord.app_commands.Choice(name="List approvers", value="approvers"),
        discord.app_commands.Choice(name="Status and funnel", value="status"),
    ])
    async def manage_onboarding(
        self,
        interaction: discord.Interaction,
        action: str,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None,
        announcements: Optional[discord.TextChannel] = None,
        events: Optional[discord.TextChannel] = None,
        resources: Optional[discord.TextChannel] = None,
        days: Optional[int] = None,
        user: Optional[discord.Member] = None,
    ):
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True, thinking=True)

        if action in ("approver_add", "approver_remove"):
            if user is None:
                await interaction.followup.send(
                    "Pick a user with the `user` option.", ephemeral=True
                )
                return
            if user.bot:
                await interaction.followup.send("Bots cannot approve.", ephemeral=True)
                return
            current = _parse_ids(
                (await self.db.get_onboarding_settings(guild.id)).get("approver_ids")
            )
            if action == "approver_add":
                if user.id in current:
                    await interaction.followup.send(
                        f"{user.mention} can already approve.", ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                current.append(user.id)
                said = f"{user.mention} can now approve applications."
            else:
                if user.id not in current:
                    await interaction.followup.send(
                        f"{user.mention} is not on the approver list.", ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                current.remove(user.id)
                said = f"{user.mention} can no longer approve applications."
            await self.db.update_onboarding_settings(
                guild.id, approver_ids=_format_ids(current)
            )
            await interaction.followup.send(
                f"{said} {len(current)} approver(s) now.", ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if action == "approvers":
            current = _parse_ids(
                (await self.db.get_onboarding_settings(guild.id)).get("approver_ids")
            )
            if not current:
                await interaction.followup.send(
                    "Nobody can approve applications. Add someone with "
                    "`/manage_onboarding action:Add approver user:@them`.",
                    ephemeral=True,
                )
                return
            # Rendered as mentions so a departed or renamed approver is still identifiable,
            # with pings suppressed since this is a list rather than a summons.
            listed = "\n".join(f"<@{i}> `{i}`" for i in current)
            await interaction.followup.send(
                f"**Approvers ({len(current)})**\n{listed}", ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if action == "post":
            target = channel or interaction.channel
            if not isinstance(target, discord.TextChannel):
                await interaction.followup.send("Pick a text channel.", ephemeral=True)
                return
            # Deliberately short. This is the third time a newcomer meets the same
            # explanation: the join message covers it in both languages, and the rules
            # cover it again above wherever this is pinned. Its job here is the button and
            # one line saying what pressing it does. The thread it opens repeats the terms
            # at the point they matter, and the buttons carry the guide and the word list.
            embed = discord.Embed(
                title="Get full access",
                description=(
                    "Press the button below. You will get a private thread with two "
                    "short questions and a quick vocab quiz. Unlimited tries.\n\n"
                    "下のボタンを押すと、あなた専用のスレッドが作成されます。"
                    "簡単な質問2つと語彙クイズに答えるだけです。何度でも挑戦できます。"
                ),
                color=discord.Color.blurple(),
            )
            banner = _entry_banner()
            if banner:
                embed.set_image(url=f"attachment://{banner.name}")
            settings = await self.db.get_onboarding_settings(guild.id)
            entry_id = settings.get("entry_channel_id")
            if banner:
                message = await target.send(
                    embed=embed, view=EntryView(self),
                    file=discord.File(banner, filename=banner.name),
                )
            else:
                message = await target.send(embed=embed, view=EntryView(self))
            # The entry channel is where threads are opened, not merely where a copy of the
            # button sits. Claimed by the first post so a fresh setup takes one command,
            # and left alone afterwards: a second copy in a rules or welcome channel is a
            # normal thing to want, and it must not quietly move where applications are
            # handled. "Set entry channel" moves it deliberately.
            fields = {"entry_message_id": message.id}
            if not entry_id:
                fields["entry_channel_id"] = target.id
                entry_id = target.id
            if not await self.db.update_onboarding_settings(guild.id, **fields):
                await interaction.followup.send(
                    f"Posted in {target.mention}, but the settings write failed, so the "
                    "flow cannot see it. Check the bot log.", ephemeral=True
                )
                return
            elsewhere = (
                "" if entry_id == target.id
                else f"\nThreads are still opened in <#{entry_id}>, not here."
            )
            await interaction.followup.send(
                f"Entry message posted in {target.mention}.{elsewhere}", ephemeral=True
            )
            return

        if action == "entrychannel":
            if channel is None:
                await interaction.followup.send(
                    "Pick a channel with the `channel` option.", ephemeral=True
                )
                return
            await self.db.update_onboarding_settings(
                guild.id, entry_channel_id=channel.id
            )
            await interaction.followup.send(
                f"Applications will now open their threads in {channel.mention}. "
                "Existing copies of the button keep working and all point here.",
                ephemeral=True,
            )
            return

        if action == "review":
            if channel is None:
                await interaction.followup.send(
                    "Pass the channel to send review cards to.", ephemeral=True
                )
                return
            await self.db.update_onboarding_settings(
                guild.id, review_channel_id=channel.id
            )
            await interaction.followup.send(
                f"Review cards will go to {channel.mention}.", ephemeral=True
            )
            return

        if action in ("autoapprove_on", "autoapprove_off"):
            on = action == "autoapprove_on"
            await self.db.update_onboarding_settings(
                guild.id, auto_approve_join_requests=1 if on else 0
            )
            if on:
                note = (
                    "Discord's own join requests will be approved automatically within "
                    f"{JOIN_REQUEST_POLL_SECONDS}s. The native form becomes a rules gate; "
                    "vetting stays in this flow.\n\n"
                    "Needs Access set to **Apply to Join** in Server Settings. If it is "
                    "not, the sweep logs a warning and does nothing."
                )
            else:
                note = "Join requests will wait for a human again."
            await interaction.followup.send(note, ephemeral=True)
            return

        if action == "tour":
            # These live in config.json rather than in onboarding_settings because the
            # welcome message reads them from the guild config, alongside the other
            # channel keys that predate this cog.
            chosen = {
                "announcements_channel": announcements,
                "events_channel": events,
                "resources_channel": resources,
            }
            chosen = {key: ch for key, ch in chosen.items() if ch is not None}
            if not chosen:
                await interaction.followup.send(
                    "Pass at least one of announcements, events or resources.",
                    ephemeral=True,
                )
                return
            guild_config = self.bot.config["guilds"].setdefault(str(guild.id), {})
            for key, ch in chosen.items():
                guild_config[key] = str(ch.id)
            try:
                with io.open("config.json", "w", encoding="utf-8") as handle:
                    json.dump(self.bot.config, handle, indent=4, ensure_ascii=False)
            except OSError as exc:
                print(f"Onboarding: cannot write config.json: {exc}")
                await interaction.followup.send(
                    "I could not save that. Check my file permissions.", ephemeral=True
                )
                return
            named = ", ".join(ch.mention for ch in chosen.values())
            await interaction.followup.send(
                f"The welcome message will link {named}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if action == "quizchannel":
            if channel is None:
                await interaction.followup.send(
                    "Pass the channel where members run level quizzes.", ephemeral=True
                )
                return
            await self.db.update_onboarding_settings(
                guild.id, quiz_channel_id=channel.id
            )
            await interaction.followup.send(
                f"Approved members will be pointed at {channel.mention} for further "
                "levels.",
                ephemeral=True,
            )
            return

        if action == "modrole":
            if role is None:
                await interaction.followup.send(
                    "Pass the role to ping when an applicant asks to talk.",
                    ephemeral=True,
                )
                return
            await self.db.update_onboarding_settings(
                guild.id, moderator_role_id=role.id
            )
            await interaction.followup.send(
                f"{role.mention} will be pinged when a rejected applicant asks to talk.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if action in ("enable", "disable"):
            settings = await self.db.get_onboarding_settings(guild.id)
            if action == "enable" and not settings.get("entry_message_id"):
                await interaction.followup.send(
                    "Post the entry message first.", ephemeral=True
                )
                return
            if action == "enable" and not settings.get("review_channel_id"):
                await interaction.followup.send(
                    "Set a review channel first, or approvals have nowhere to land.",
                    ephemeral=True,
                )
                return
            if action == "enable" and not _parse_ids(settings.get("approver_ids")):
                await interaction.followup.send(
                    "Add at least one approver first, or applications will queue up with "
                    "nobody able to decide them: "
                    "`/manage_onboarding action:Add approver user:@them`.",
                    ephemeral=True,
                )
                return
            await self.db.update_onboarding_settings(
                guild.id, enabled=1 if action == "enable" else 0
            )
            await interaction.followup.send(
                f"Verification flow {action}d.", ephemeral=True
            )
            return

        if action == "cooldown":
            if days is None or days < 0:
                await interaction.followup.send(
                    "Pass a number of days, or 0 for no cooldown.", ephemeral=True
                )
                return
            await self.db.update_onboarding_settings(
                guild.id, reapply_cooldown_days=days
            )
            await interaction.followup.send(
                f"Rejected applicants may reapply after {days} days.", ephemeral=True
            )
            return

        settings = await self.db.get_onboarding_settings(guild.id)
        counts = await self.db.get_onboarding_funnel_counts(guild.id)
        embed = discord.Embed(title="Onboarding", color=discord.Color.blurple())
        embed.add_field(
            name="Enabled", value="yes" if settings.get("enabled") else "no", inline=True
        )
        entry = settings.get("entry_channel_id")
        review = settings.get("review_channel_id")
        embed.add_field(
            name="Entry", value=f"<#{entry}>" if entry else "not set", inline=True
        )
        welcome_raw = self.bot.config["guilds"].get(
            str(guild.id), {}
        ).get("welcome_channel")
        if welcome_raw and entry and int(welcome_raw) == int(entry):
            embed.add_field(
                name="\u26a0\ufe0f Channel clash",
                value=(
                    "Arrival and level-up announcements are set to the entry channel, so "
                    "only unverified members would see them. Move them with "
                    "`/manage_welcome action:set_channel`."
                ),
                inline=False,
            )
        embed.add_field(
            name="Review", value=f"<#{review}>" if review else "not set", inline=True
        )
        guild_config = self.bot.config["guilds"].get(str(guild.id), {})
        tour = " ".join(
            f"<#{guild_config[key]}>" if guild_config.get(key) else f"({word})"
            for key, word in (
                ("announcements_channel", "announcements"),
                ("events_channel", "events"),
                ("resources_channel", "resources"),
            )
        )
        embed.add_field(name="Welcome links", value=tour, inline=False)
        quizchan = settings.get("quiz_channel_id")
        embed.add_field(
            name="Quiz channel",
            value=f"<#{quizchan}>" if quizchan else "not set",
            inline=True,
        )
        modrole = settings.get("moderator_role_id")
        embed.add_field(
            name="Moderator role",
            value=(
                f"<@&{modrole}> (pings only)" if modrole else "not set"
            ),
            inline=True,
        )
        approvers = _parse_ids(settings.get("approver_ids"))
        embed.add_field(
            name=f"Approvers ({len(approvers)})",
            value=(
                " ".join(f"<@{i}>" for i in approvers[:10])
                + (" …" if len(approvers) > 10 else "")
                if approvers else "nobody, so no application can be decided"
            ),
            inline=False,
        )
        embed.add_field(
            name="Native auto-approve",
            value="on" if settings.get("auto_approve_join_requests") else "off",
            inline=True,
        )
        embed.add_field(
            name="Reapply cooldown",
            value=(
                f"{settings.get('reapply_cooldown_days')} days"
                if settings.get("reapply_cooldown_days") else "none"
            ),
            inline=True,
        )
        started = sum(counts.values())
        embed.add_field(
            name="Funnel",
            value=(
                f"started **{started}**\n"
                f"at the quiz **{counts['awaiting_quiz']}**\n"
                f"waiting on you **{counts['awaiting_review']}**\n"
                f"approved **{counts['approved']}**\n"
                f"rejected **{counts['rejected']}**\n"
                f"left the server **{counts.get('left', 0)}**"
            ),
            inline=False,
        )
        orphaned = await self.db.count_applications_without_card(guild.id)
        if orphaned:
            embed.add_field(
                name="\u26a0\ufe0f Waiting with no review card",
                value=(
                    f"**{orphaned}** application(s) passed the quiz but their card never "
                    "posted. Those applicants are waiting on a decision nobody can see. "
                    "Check the review channel is set and readable."
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(OnboardingCog(bot))
