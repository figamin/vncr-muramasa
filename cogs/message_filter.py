"""
Message word-filter cog for muramasa.

Legitimate moderation tool: when a user's message matches an admin-configured
rule, the offending term is replaced with a chosen replacement, the original is
deleted, and the cleaned text is reposted under the author's name and avatar via
a channel webhook (which is what lets the repost keep the author's identity).
Every original is recorded to a mod-log channel and the DB so moderators keep
full visibility.

Rules (simple word or full regex) and settings are managed from the /manage_filters
command and the web console, and are stored in the shared SQLite DB. The cog
watches settings.rules_version and reloads its compiled rules when it changes,
so edits go live without a restart.

Fail-open by design: if the repost cannot be sent (missing permissions, rate
limit, oversized content), the original is left untouched. The original is only
deleted after a successful repost, so a failed repost never destroys a message.
"""

import asyncio
import logging
import re
from typing import Dict, List, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import Database
from utils.media_detect import message_has_visual_media
from utils.message_filter import (
    apply_rules, compile_rules, validate_pattern,
    author_marker as _author_marker,
    strip_author_marker as _strip_author_marker,
    defang_masked_links as _defang_masked_links,
)

logger = logging.getLogger(__name__)

WEBHOOK_NAME = "Muramasa Filter"
DISCORD_MESSAGE_LIMIT = 2000
MAX_WEBHOOK_FILES = 10
# Discord attachment flag bit for a spoiler (IS_SPOILER = 1 << 3, documented and
# stable). Checked directly as well, since not every discord.py version reads it.
ATTACHMENT_SPOILER_FLAG = 1 << 3
# Discord's elevated-MFA gate (error 60003). In a guild that requires 2FA for
# moderation the check is made against the bot OWNER's account, not the bot: bots
# cannot carry 2FA, so no permission the bot holds gets past it. Webhook management
# and deleting another member's message both sit behind this gate, which is why the
# filter stops wholesale when the owner's 2FA is off. Spelled out here so the log
# says what to fix instead of just relaying "Two factor is required for this
# operation".
MFA_REQUIRED_CODE = 60003
MFA_REQUIRED_HINT = (
    " — this server requires 2FA for moderation and the bot's owner has not enabled "
    "2FA on their Discord account; bots cannot satisfy that check themselves, so "
    "enable 2FA on the application owner's account"
)


def _mfa_hint(exc) -> str:
    """Actionable suffix for a 60003 error, empty for anything else."""
    return MFA_REQUIRED_HINT if getattr(exc, "code", None) == MFA_REQUIRED_CODE else ""

# Leading marker of the subtext reply line used for link-media reply reposts (see
# _build_reply_line). The edit modal keys off this to strip the line back out, so the
# two must stay in sync.
REPLY_LINE_LEAD = "-# [↪ reply]"
# Leading form of the reply EMBED's description, used to recognise the bot's own scaffolding when
# re-editing a repost. The second entry is the older shape, still baked into reposts sitting
# in channel history, so both must keep matching.
REPLY_EMBED_LEADS = ("[↪ reply](", "[reply to ")


# The invisible author marker (_author_marker / _strip_author_marker) lives in
# utils/message_filter.py so highlights and the purge preview can strip it too; it is
# imported above under these same private names. See that module for the full rationale
# (U+2800 label, angle-bracket URL, leading space) and why every place that re-displays
# repost content must strip it.


class RepostEditModal(discord.ui.Modal):
    """Modal shown when the original poster edits their reposted message. On submit the
    new text is re-run through the filter (so an edit can't reintroduce a filtered word)
    and the webhook message is updated in place."""

    def __init__(self, cog, message: discord.Message, content_override: Optional[str] = None):
        super().__init__(title="Edit your message")
        self.cog = cog
        self.message = message
        # content_override lets the caller seed the box with the latest content it wrote (so a
        # second edit doesn't revert the first) without a REST fetch on the un-deferrable modal path.
        default = content_override[:2000] if content_override is not None else self._prefill(message)
        self.body = discord.ui.TextInput(
            label="Message",
            style=discord.TextStyle.paragraph,
            default=default,
            max_length=2000,
            required=False,
        )
        self.add_item(self.body)

    @staticmethod
    def _prefill(message: discord.Message) -> str:
        """Text to seed the edit box with: the poster's own words, without the reply
        scaffolding the bot added. Two repost shapes carry that scaffolding:
          - link-media reply: a leading '-# [↪ reply](...)' subtext line + a reply-ping;
          - normal reply: a reply EMBED + a leading reply-ping.
        Strip whichever applies so an edit re-filters only the poster's text, not the
        quoted snippet or the ping. A non-reply repost's leading mention could be the
        poster's own, so leave it alone. The trailing author marker is always stripped."""
        content = _strip_author_marker(message.content or "")
        if content.startswith(REPLY_LINE_LEAD + "("):
            newline = content.find("\n")
            content = content[newline + 1:] if newline != -1 else ""
            return re.sub(r"^<@!?\d+>\s+", "", content, count=1)[:2000]
        # Match the bot's reply embed by its exact leading form, not a loose "reply to" substring
        # (an unfurled link preview could otherwise be mistaken for a reply and strip the
        # poster's own leading mention).
        is_reply = any((getattr(e, "description", "") or "").startswith(REPLY_EMBED_LEADS)
                       for e in message.embeds)
        if is_reply:
            content = re.sub(r"^<@!?\d+>\s+", "", content, count=1)
        return content[:2000]

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.apply_repost_edit(interaction, self.message, str(self.body))


class RepostView(discord.ui.View):
    """Persistent controls on every webhook repost. Edit/Delete are usable only by the
    original poster (resolved via the repost_authors table, which only the bot writes, so
    it always names the real poster). The ✅/🚩 media-review buttons are present only on media
    reposts and delegate to the media-flag cog. One instance is registered on boot to
    route clicks by custom_id; per-message instances just render the right buttons."""

    def __init__(self, cog, *, include_media: bool):
        super().__init__(timeout=None)
        self.cog = cog
        if not include_media:
            for item in list(self.children):
                if getattr(item, "custom_id", None) in ("mediaflag:safe", "mediaflag:flag"):
                    self.remove_item(item)

    @discord.ui.button(emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="repost:edit")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.on_repost_edit(interaction)

    @discord.ui.button(emoji="🗑️", style=discord.ButtonStyle.secondary, custom_id="repost:delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.on_repost_delete(interaction)

    @discord.ui.button(emoji="✅", style=discord.ButtonStyle.secondary, custom_id="mediaflag:safe")
    async def safe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._media_vote(interaction, "safe")

    @discord.ui.button(emoji="🚩", style=discord.ButtonStyle.secondary, custom_id="mediaflag:flag")
    async def flag(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._media_vote(interaction, "flag")

    async def _media_vote(self, interaction: discord.Interaction, vote: str):
        mediaflag = interaction.client.get_cog("MediaFlagCog")
        if mediaflag is None:
            await interaction.response.send_message("Media review is unavailable right now.", ephemeral=True)
            return
        await mediaflag.on_vote(interaction, vote)


class RepostMenuView(discord.ui.View):
    """The single control on every webhook repost: one muted menu button, identical on media
    and non-media reposts. Clicking it opens a PRIVATE (ephemeral) menu so the channel stays
    uncluttered; the menu is built per click, so the poster gets Edit/Delete and a media repost
    also shows the review vote to anyone. One instance is registered on boot to route clicks."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="⋯", style=discord.ButtonStyle.secondary, custom_id="repost:menu")
    async def menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.on_repost_menu(interaction)


class RepostActionView(discord.ui.View):
    """The private, per-click action menu behind a repost's menu button. Only the clicker sees
    it. It holds the repost message so its buttons act on that message even though they live on
    the ephemeral reply. Transient (re-created on every menu click), so it needs no persistence.
    Labels are fine here since the ephemeral is private and never clutters the channel."""

    def __init__(self, cog, repost: discord.Message, *, is_owner: bool, media_item: Optional[dict]):
        super().__init__(timeout=180)
        self.cog = cog
        self.repost = repost
        self.media_item = media_item
        self.message = None  # the ephemeral, set by the caller so on_timeout can disable it
        if is_owner:
            self._add("✏️ Edit", "edit", discord.ButtonStyle.secondary)
            self._add("🗑️ Delete", "delete", discord.ButtonStyle.danger)
        if media_item is not None:
            self._add("✅ Safe", "safe", discord.ButtonStyle.secondary)
            self._add("🚩 Flag", "flag", discord.ButtonStyle.secondary)

    def _add(self, label: str, action: str, style: discord.ButtonStyle):
        button = discord.ui.Button(label=label, style=style)

        async def callback(interaction: discord.Interaction, action=action):
            await self.cog.on_repost_action(interaction, self, action)

        button.callback = callback
        self.add_item(button)

    async def on_timeout(self):
        # After the timeout the buttons are no longer routed; disable them and say so, so a
        # stale click gets a clear note instead of Discord's silent "interaction failed".
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(content="This menu expired. Click ⋯ again.", view=self)
            except Exception:
                pass


class MessageFilterCog(commands.Cog):
    """Forum-style word filter with webhook repost and an audit log."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = Database()
        # Compiled rules + exempt roles + excluded channels, refreshed when rules_version changes.
        self._compiled = []
        self._excluded_roles: Set[int] = set()
        self._excluded_channels: Set[int] = set()
        self._rules_version: Optional[int] = None
        self._reload_lock = asyncio.Lock()
        # Per-channel webhook cache and creation locks.
        self._webhooks: Dict[int, discord.Webhook] = {}
        self._webhook_locks: Dict[int, asyncio.Lock] = {}
        # Channels already warned about (missing perms), so each is logged once.
        self._perm_warned: Set[int] = set()
        # repost_message_id -> latest content written via an edit, so a second edit from the same
        # still-open menu prefills fresh (not the stale menu snapshot) with no REST fetch. Bounded.
        self._recent_edits: Dict[int, str] = {}

    async def cog_load(self):
        await self.db.initialize()
        try:
            await self._reload_rules(force=True)
        except Exception as exc:
            logger.error("Failed initial message filter load: %s", exc)
        # New reposts carry the single menu button; register its router. The multi-button
        # RepostView stays registered too, because older reposts in channel history still carry
        # its ✏️/🗑️/✅/🚩 buttons (routed by those custom_ids). RETIREMENT: safe to drop RepostView + the
        # truly-legacy handlers (on_repost_edit/on_repost_delete/on_vote/MediaFlagConfirmView) only
        # once no pre-menu repost is still reachable in channel history. repost_authors/
        # media_flag_items rows are never purged and a pinned/old repost keeps its baked-in buttons
        # indefinitely, so gate on channel-history age, not just elapsed time. Keep finalize_vote:
        # the new menu vote (on_vote_item) still delegates to it.
        self.bot.add_view(RepostMenuView(self))
        self.bot.add_view(RepostView(self, include_media=True))
        logger.info("Message filter cog loaded")

    # ------------------------------------------------------------------ #
    # Rule cache
    # ------------------------------------------------------------------ #

    async def _reload_rules(self, settings: Optional[dict] = None, force: bool = False):
        """Recompile enabled rules and reload exempt roles. Cheap and only runs
        when rules_version changed (or on first load)."""
        async with self._reload_lock:
            if settings is None:
                settings = await self.db.get_message_filter_settings()
            version = settings.get("rules_version")
            if not force and version == self._rules_version:
                return
            rows = await self.db.get_message_filter_rules(enabled_only=True)
            self._compiled = compile_rules(rows)
            self._excluded_roles = set(await self.db.get_message_filter_excluded_roles())
            self._excluded_channels = set(await self.db.get_message_filter_excluded_channels())
            self._rules_version = version

    # ------------------------------------------------------------------ #
    # Message pipeline
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not await self._should_process(message):
            return

        settings = await self.db.get_message_filter_settings()
        # Keep the rule/role/channel cache fresh (cheap; reloads only when the version changed).
        if settings.get("rules_version") != self._rules_version:
            await self._reload_rules(settings)

        # Excluded channels: skip the entire pipeline (word filter AND media review) here.
        if self._is_channel_excluded(message.channel):
            return

        # Text filter: clean matched terms, only if the filter is enabled and the
        # author isn't exempt. matched stays empty when the filter is off.
        cleaned, matched = message.content, []
        if settings.get("enabled") and not self._author_is_exempt(message.author):
            cleaned, matched = apply_rules(message.content, self._compiled)
            if cleaned == message.content:
                matched = []

        # A forum/media post's opening message cannot be replaced by a repost: deleting it
        # leaves the post headed by "original message deleted". It skips media review, and
        # a word-filter hit there is logged for mods and left in place.
        if self._is_post_starter(message):
            if matched:
                await self._log_action(message, cleaned, matched, settings, kept_post_starter=True)
            return

        # Media flag: should this message's media be reposted with flag buttons?
        # Guarded so a media-flag error can never abort the text-filter path below
        # (which would leave a banned message live and undeleted).
        mediaflag = self.bot.get_cog("MediaFlagCog")
        attach_media = False
        if mediaflag is not None:
            try:
                attach_media = await mediaflag.should_attach(message)
            except Exception as exc:
                logger.warning("Media flag should_attach failed; skipping buttons: %s", exc)

        if not matched and not attach_media:
            return

        try:
            await self._process_message(message, cleaned, matched, settings, attach_media, mediaflag)
        except Exception as exc:
            logger.error("Message pipeline failed to process a message: %s", exc, exc_info=True)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Re-run the pipeline when an edit changes the text (a flagged term added
        # after the fact) OR when media first appears. Link media (Tenor/klipy)
        # arrives as an embed that resolves via a message edit shortly after the
        # message posts, so this edit hook is how link-based media gets caught.
        text_changed = before.content != after.content
        media_appeared = (not message_has_visual_media(before)) and message_has_visual_media(after)
        if not text_changed and not media_appeared:
            return
        await self.on_message(after)

    async def _should_process(self, message: discord.Message) -> bool:
        if message.guild is None:
            return False
        if message.author.bot:
            return False
        if message.webhook_id is not None:
            # Skip all webhook posts, which also keeps the bot's own reposts from being reprocessed.
            return False
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return False
        if (not (message.content and message.content.strip())
                and not message.attachments and not message.embeds):
            # Nothing to clean and no media to review.
            return False
        return True

    def _author_is_exempt(self, author) -> bool:
        if not self._excluded_roles:
            return False
        role_ids = {role.id for role in getattr(author, "roles", [])}
        return bool(role_ids & self._excluded_roles)

    def _is_channel_excluded(self, channel) -> bool:
        """True if this channel (or, for a thread, its parent) is on the excluded list,
        so the whole pipeline is skipped there."""
        if not self._excluded_channels:
            return False
        if channel.id in self._excluded_channels:
            return True
        parent_id = getattr(channel, "parent_id", None)
        return parent_id is not None and parent_id in self._excluded_channels

    @staticmethod
    def _is_post_starter(message: discord.Message) -> bool:
        """True for the opening message of a forum or media channel post, which shares
        its thread's id. Messages in ordinary threads never do: a thread started from a
        message keeps that message in the parent channel."""
        channel = message.channel
        return isinstance(channel, discord.Thread) and message.id == channel.id

    async def _process_message(self, message: discord.Message, cleaned: str, matched: List[int],
                               settings: dict, attach_media: bool, mediaflag):
        parent, thread = self._resolve_channel(message)
        if parent is None:
            return

        me = message.guild.me
        if me is None:
            return
        if not (parent.permissions_for(me).manage_webhooks
                and message.channel.permissions_for(me).manage_messages):
            self._warn_missing_perms(message.channel)
            return

        reply_embed = self._build_reply_embed(message)
        allowed = self._build_allowed_mentions(message)
        has_attachments = bool(message.attachments)
        body = cleaned if matched else message.content

        # Link-only media (no attachment) survives just via its URL in the text. If
        # cleaning wiped that text entirely, keep the original so the flagged media
        # stays visible to review.
        if attach_media and matched and not has_attachments and not body.strip():
            body = message.content

        # Nothing meaningful to carry (message cleaned to nothing, no media/attachment):
        # just remove + log. Judged on the body BEFORE the reply ping, so an all-filtered
        # reply is deleted rather than reposted as a bare mention.
        if not body.strip() and not has_attachments:
            if await self._safe_delete(message):
                if matched:
                    await self._log_action(message, cleaned, matched, settings)
            return

        # Every repost carries one muted menu button; the actions (Edit/Delete for the poster,
        # the ✅/🚩 review vote for media) live in the private menu it opens, not on the message.
        view = RepostMenuView(self)

        # Neutralize masked-link markdown in the poster's body: a webhook repost renders
        # [label](url) as a clickable link that hides its destination, but the member's own
        # message showed it inert, so the repost should too (a bare URL keeps its visible
        # destination). Same hardening as the edit path and the reply-context line.
        content = _defang_masked_links(body)
        replied_mention = await self._replied_mention(message)
        if replied_mention:
            content = f"{replied_mention} {content}".strip()

        # Link-based media (GIF-picker klipy/tenor, etc.) shows up only via Discord's
        # auto-unfurl of the URL in the content, but passing a reply EMBED suppresses that
        # unfurl. So for link media (no re-uploaded attachment) fold the reply context into
        # a subtext line and send no embed, letting the media render. Attachment media
        # re-uploads as a file regardless, so those keep the embed.
        if reply_embed is not None and attach_media and not has_attachments:
            reply_line = self._build_reply_line(message)
            content = f"{reply_line}\n{content}" if reply_line and content else (reply_line or content)
            reply_embed = None

        try:
            sent = await self._repost(message, content, reply_embed, thread, parent, allowed, view=view)
        except (discord.Forbidden, discord.HTTPException) as exc:
            # Fail open: the first chunk never posted, so leave the original untouched.
            logger.warning("Repost failed in #%s (%s)%s; leaving original.",
                           getattr(message.channel, "name", message.channel.id), exc,
                           _mfa_hint(exc))
            return

        # A persistent RepostMenuView is already registered globally in cog_load, so the menu
        # button keeps routing; stop this per-message instance so webhook.send()'s automatic
        # per-message view registration doesn't leak a view-store entry (a timeout=None view
        # otherwise never gets an auto-cleanup pass).
        view.stop()

        # Link the repost to its original poster BEFORE the delete/log network round
        # trips, so a reaction/reply/flag-click on the now-visible repost can resolve it
        # (highlights, reply-ping, and flag tracking all key off these rows).
        if sent is not None:
            await self.db.add_repost_author(sent.id, message.author.id)
            if attach_media:
                await mediaflag.register_item(message, sent)

        # Repost is committed (at least the first chunk posted), so always delete now.
        deleted = await self._safe_delete(message)
        if not deleted:
            logger.warning(
                "Reposted a copy in #%s but could not delete the original; a duplicate is visible "
                "and needs manual cleanup.", getattr(message.channel, "name", message.channel.id),
            )
        if matched:
            await self._log_action(message, cleaned, matched, settings, delete_failed=not deleted)

    # ------------------------------------------------------------------ #
    # Webhook repost
    # ------------------------------------------------------------------ #

    def _resolve_channel(self, message: discord.Message) -> Tuple[Optional[discord.abc.GuildChannel], Optional[discord.Thread]]:
        channel = message.channel
        if isinstance(channel, discord.Thread):
            return channel.parent, channel
        return channel, None

    async def _get_webhook(self, parent: discord.abc.GuildChannel) -> discord.Webhook:
        cached = self._webhooks.get(parent.id)
        if cached is not None:
            return cached
        lock = self._webhook_locks.setdefault(parent.id, asyncio.Lock())
        async with lock:
            cached = self._webhooks.get(parent.id)
            if cached is not None:
                return cached
            for hook in await parent.webhooks():
                if hook.token and hook.user and self.bot.user and hook.user.id == self.bot.user.id:
                    self._webhooks[parent.id] = hook
                    return hook
            hook = await parent.create_webhook(name=WEBHOOK_NAME, reason="Muramasa message filter")
            self._webhooks[parent.id] = hook
            return hook

    async def _repost(self, message: discord.Message, cleaned: str,
                      reply_embed: Optional[discord.Embed], thread: Optional[discord.Thread],
                      parent: discord.abc.GuildChannel, allowed: discord.AllowedMentions, *,
                      view: Optional[discord.ui.View] = None):
        username = self._webhook_username(message.author)
        avatar_url = message.author.display_avatar.url
        # Append the invisible author marker to every chunk so each piece is findable/purgeable
        # by the original poster's id. The space before it stops a trailing URL (GIF-picker link)
        # from swallowing the marker. Split with headroom so chunk + space + marker stays under the
        # message limit; an empty (media-only) chunk carries just the marker.
        marker = _author_marker(message.author.id)
        chunks = self._split(cleaned, DISCORD_MESSAGE_LIMIT - len(marker) - 1) or [""]
        chunks = [(c + " " + marker) if c else marker for c in chunks]
        sent = None

        # Send the FIRST chunk (it carries the attachments + reply embed). This is
        # the ONLY send retried on a stale webhook (404): once a chunk is posted
        # it cannot be un-posted, so retrying a later chunk from the top would
        # duplicate the first chunk and its attachments. Files are rebuilt per
        # attempt because a File's buffer is consumed once sent. If this raises,
        # the caller fails open and leaves the original in place.
        # Two failure modes get a single bounded retry here: a stale webhook (404 -> refetch)
        # and a Discord-rejected username override (400 -> resend under the poster's id, so a
        # member's chosen nickname can never keep the filter from cleaning their message).
        # Anything else propagates and the caller fails open.
        refetched = False
        username_retried = False
        while True:
            webhook = await self._get_webhook(parent)
            files = await self._rebuild_files(message)
            try:
                sent = await self._send_chunk(webhook, chunks[0], username, avatar_url,
                                              allowed, thread, reply_embed, files, view=view)
                break
            except discord.NotFound:
                self._webhooks.pop(parent.id, None)
                if refetched:
                    raise
                refetched = True
            except discord.HTTPException as exc:
                if getattr(exc, "status", None) == 400 and not username_retried:
                    username = str(message.author.id)
                    username_retried = True
                    continue
                raise

        # The first chunk is posted, so the repost is committed and the caller will
        # delete the original. Remaining chunks are best-effort: a failure here must
        # NOT propagate, or the caller would fail open and leave the original next
        # to the already-posted copy.
        for chunk in chunks[1:]:
            try:
                webhook = await self._get_webhook(parent)
                await self._send_chunk(webhook, chunk, username, avatar_url,
                                       allowed, thread, None, None)
            except Exception as exc:
                logger.warning("Message filter: could not post a continuation chunk in #%s: %s",
                               getattr(message.channel, "name", message.channel.id), exc)
        return sent

    async def _send_chunk(self, webhook: discord.Webhook, content: str, username: str, avatar_url: str,
                          allowed: discord.AllowedMentions, thread: Optional[discord.Thread],
                          reply_embed: Optional[discord.Embed], files: Optional[List[discord.File]], *,
                          view: Optional[discord.ui.View] = None):
        kwargs = dict(username=username, avatar_url=avatar_url, allowed_mentions=allowed, wait=True)
        if thread is not None:
            kwargs["thread"] = thread
        if reply_embed is not None:
            kwargs["embeds"] = [reply_embed]
        if files:
            kwargs["files"] = files
        if view is not None:
            kwargs["view"] = view
        if content:
            kwargs["content"] = content
        elif not files and reply_embed is None:
            # Nothing else to carry the message; a zero-width space keeps it valid.
            kwargs["content"] = "​"
        return await webhook.send(**kwargs)

    async def _rebuild_files(self, message: discord.Message) -> List[discord.File]:
        files: List[discord.File] = []
        limit = getattr(message.guild, "filesize_limit", 0) or 0
        for attachment in message.attachments[:MAX_WEBHOOK_FILES]:
            try:
                if limit and attachment.size and attachment.size > limit:
                    continue
                files.append(await attachment.to_file(spoiler=self._attachment_is_spoiler(attachment)))
            except Exception as exc:
                logger.warning("Could not re-upload attachment %s: %s", attachment.filename, exc)
        return files

    def _attachment_is_spoiler(self, attachment: discord.Attachment) -> bool:
        # Discord marks spoilers by filename prefix and by an attachment flag (e.g.
        # GIF-picker media), and older discord.py versions only read the prefix.
        # Honor all three, or the repost would show a spoilered image un-blurred:
        #   1. is_spoiler(), 2. a .spoiler property if present,
        #   3. the raw IS_SPOILER flag bit.
        if attachment.is_spoiler():
            return True
        flags = getattr(attachment, "flags", None)
        if flags is None:
            return False
        named = getattr(flags, "spoiler", None)
        if named is not None:
            return bool(named)
        return bool(getattr(flags, "value", 0) & ATTACHMENT_SPOILER_FLAG)

    def _build_reply_embed(self, message: discord.Message) -> Optional[discord.Embed]:
        ref = message.reference
        if ref is None:
            return None
        resolved = ref.resolved
        if not isinstance(resolved, discord.Message):
            return None
        # Strip the bot's own trailing author marker first: the replied-to message may
        # itself be one of its reposts, and the marker would otherwise surface as
        # visible "[ ](url)" junk in the quoted reply context (defang exposes it).
        snippet = _strip_author_marker(resolved.content or "").replace("\n", " ").strip()
        if len(snippet) > 100:
            snippet = snippet[:99] + "…"
        snippet = _defang_masked_links(snippet)
        # The jump link is a short bracketed chip rather than a caption-length label: as a
        # label it reads as plain text and the context does not look clickable.
        # Same shape as _build_reply_line, so the two renderers stay recognisably one thing.
        name = _defang_masked_links(resolved.author.display_name)  # sits after the [.](.) label, not inside it
        text = f"[↪ reply]({resolved.jump_url}) to {name}"
        if snippet:
            text += f": {snippet}"
        return discord.Embed(description=text, color=discord.Color.greyple())

    def _build_reply_line(self, message: discord.Message) -> Optional[str]:
        """Reply context as a subtext line, for reposts that can't use the embed (an embed
        suppresses Discord's auto-unfurl of a media link in the content). This lands in
        message CONTENT, so it is defended four ways: escape_mentions on the whole line so a
        replied-to name/snippet can't ping bystanders; _defang_masked_links on the name/snippet
        so neither can carry a masked link; the fixed 'reply' label keeps the jump link's text
        bot-controlled; and any URL in the quoted snippet is angle-bracket wrapped so it can't
        unfurl a second preview next to the media. The deliberate reply-ping is added separately."""
        ref = message.reference
        if ref is None:
            return None
        resolved = ref.resolved
        if not isinstance(resolved, discord.Message):
            return None
        # Strip the bot's own trailing author marker first (the replied-to message may be
        # one of its reposts) so it doesn't surface as "[ ](url)" junk in the quote.
        snippet = _strip_author_marker(resolved.content or "").replace("\n", " ").strip()
        if len(snippet) > 100:
            snippet = snippet[:99] + "…"
        snippet = _defang_masked_links(snippet)
        # Exclude the truncation ellipsis (…) from the URL match so a boundary URL doesn't
        # pull it inside the <> wrap.
        snippet = re.sub(r"https?://[^\s…]+", lambda m: f"<{m.group(0)}>", snippet)
        name = _defang_masked_links(resolved.author.display_name)  # sits after the [.](.) label, not inside it
        line = f"{REPLY_LINE_LEAD}({resolved.jump_url}) to {name}"
        if snippet:
            line += f": {snippet}"
        return discord.utils.escape_mentions(line)

    @staticmethod
    def _reply_ping_requested(message: discord.Message) -> bool:
        """Whether the replier left the reply's ping toggle ON. A repost is not a native reply
        (webhook sends reject message_reference), so the ping only survives as a mention the
        bot prepends, and prepending it unconditionally overrides a deliberate toggle-off. Received
        messages carry no mention_author flag; Discord instead adds the replied-to author to
        `mentions` only when the ping was on, so an id there that the content never mentions is
        exactly that toggle. Always False when the target is webhook-authored, whose toggle
        Discord never reports either way."""
        return bool({user.id for user in message.mentions} - set(message.raw_mentions))

    async def _replied_mention(self, message: discord.Message) -> Optional[str]:
        """Mention string to prepend for a reply ping, or None. Skips self-replies, replies
        whose ping the replier turned off, and bot/webhook targets, which have no real user
        to notify.

        The bot's own reposts fall in that last group, so replying to one never pings the person it
        stands in for. A webhook-targeted reply arrives identical whether the toggle was on or
        off, so the choice can only be guessed, and guessing towards a ping leaves the replier
        no way to take back a notification they declined. Anyone who does want to notify can
        @ them, which pings normally and survives the repost."""
        ref = message.reference
        if ref is None:
            return None
        resolved = ref.resolved
        if not isinstance(resolved, discord.Message):
            return None
        if not self._reply_ping_requested(message):
            return None
        author = resolved.author
        if author is None or resolved.webhook_id is not None or getattr(author, "bot", False):
            return None
        if author.id == message.author.id:
            return None
        return f"<@{author.id}>"

    def _build_allowed_mentions(self, message: discord.Message) -> discord.AllowedMentions:
        # Mirror the author's mention permissions: the repost may ping @everyone or
        # roles only if the author could.
        try:
            can_ping_all = message.channel.permissions_for(message.author).mention_everyone
        except Exception:
            can_ping_all = False
        return discord.AllowedMentions(everyone=can_ping_all, roles=can_ping_all, users=True)

    def _webhook_username(self, author) -> str:
        # Discord rejects a webhook username override that contains "clyde" or "discord"
        # (case-insensitive) with a 400, but a member's nickname has no such limit. Strip
        # those substrings, or the repost send fails and the filter falls open (leaving the
        # message it should have cleaned in place). _repost also retries a 400 under the
        # poster's id as a backstop for anything else Discord rejects.
        name = re.sub(r"clyde|discord", "", author.display_name or author.name or "", flags=re.IGNORECASE)
        name = name[:80].strip()
        return name or "user"

    def _split(self, text: str, size: int = DISCORD_MESSAGE_LIMIT) -> List[str]:
        if not text:
            return []
        return [text[i:i + size] for i in range(0, len(text), size)]

    async def _safe_delete(self, message: discord.Message) -> bool:
        try:
            await message.delete()
            return True
        except discord.NotFound:
            return True
        except discord.Forbidden as exc:
            logger.warning("Filtered a message but lacked Manage Messages to delete it in #%s%s",
                           getattr(message.channel, "name", message.channel.id),
                           _mfa_hint(exc) or ".")
            return False
        except discord.HTTPException as exc:
            logger.warning("Failed to delete filtered message: %s", exc)
            return False

    def _warn_missing_perms(self, channel):
        if channel.id in self._perm_warned:
            return
        self._perm_warned.add(channel.id)
        logger.warning(
            "Message filter needs Manage Webhooks + Manage Messages in #%s; skipping until granted.",
            getattr(channel, "name", channel.id),
        )

    # ------------------------------------------------------------------ #
    # Mod-log
    # ------------------------------------------------------------------ #

    async def _log_action(self, message: discord.Message, cleaned: str,
                          matched: List[int], settings: dict, delete_failed: bool = False,
                          kept_post_starter: bool = False):
        try:
            await self.db.add_message_filter_log(
                message.channel.id, message.id, message.author.id,
                message.content, cleaned, matched,
            )
        except Exception as exc:
            logger.warning("Failed to record message filter log row: %s", exc)

        channel = await self._resolve_log_channel(settings, message.guild)
        if channel is None:
            return

        embed = discord.Embed(
            title="🧹 Message filtered",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Author", value=f"{message.author.mention} ({message.author.id})", inline=False)
        embed.add_field(name="Channel", value=getattr(message.channel, "mention", str(message.channel.id)), inline=False)
        embed.add_field(name="Original", value=self._clip(message.content), inline=False)
        embed.add_field(name="Cleaned", value=self._clip(cleaned) or "(empty)", inline=False)
        embed.add_field(name="Rules matched", value=", ".join(str(m) for m in matched) or "-", inline=False)
        if delete_failed:
            embed.color = discord.Color.red()
            embed.add_field(
                name="⚠️ Original NOT deleted",
                value="The cleaned copy was posted but the original could not be removed; a duplicate is visible and needs manual cleanup.",
                inline=False,
            )
        if kept_post_starter:
            embed.color = discord.Color.red()
            embed.add_field(
                name="⚠️ Left in place",
                value="This opens a forum post, so it was not reposted or deleted. Edit or remove it manually if needed.",
                inline=False,
            )
        try:
            await channel.send(embed=embed)
        except Exception as exc:
            logger.warning("Failed to send message filter log embed: %s", exc)

    def _clip(self, text: str, limit: int = 1000) -> str:
        text = text or ""
        return text if len(text) <= limit else text[:limit - 1] + "…"

    async def _resolve_log_channel(self, settings: dict, guild):
        """The configured mod-log channel object, or None if unset/unreachable."""
        log_channel_id = settings.get("log_channel_id")
        if not log_channel_id:
            return None
        channel = guild.get_channel(int(log_channel_id)) if guild else None
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(log_channel_id))
            except Exception as exc:
                logger.warning("Message filter log channel %s not accessible: %s", log_channel_id, exc)
                return None
        return channel

    # ------------------------------------------------------------------ #
    # Repost controls (poster-only Edit / Delete)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _actor_timed_out(user) -> bool:
        """A Discord timeout blocks sending/editing messages and slash commands but NOT button
        or modal interactions, so these handlers must self-enforce it, or a timed-out member
        could keep rewriting/deleting their reposts while supposedly silenced."""
        return isinstance(user, discord.Member) and user.is_timed_out()

    async def _can_manage_repost(self, interaction: discord.Interaction, repost: discord.Message) -> bool:
        """True only if the actor may edit/delete this repost: the original poster (per
        repost_authors, written server-side) and not under a Discord timeout. Rejects with an
        ephemeral otherwise. Shared by the ephemeral action menu and the legacy direct buttons."""
        author_id = await self.db.get_repost_author(repost.id)
        if author_id is None or author_id != interaction.user.id:
            await interaction.response.send_message(
                "Only the person who posted this message can do that.", ephemeral=True)
            return False
        if self._actor_timed_out(interaction.user):
            await interaction.response.send_message("You can't do that while timed out.", ephemeral=True)
            return False
        return True

    async def on_repost_menu(self, interaction: discord.Interaction):
        """The single menu button was clicked: open a private, per-user action menu for this
        repost. The poster gets Edit/Delete; a media repost shows the review vote to anyone.
        Everyone gets the poster's id, which is the only way to search a repost back to its
        author (Discord's from: search sees the webhook, not the person)."""
        repost = interaction.message
        author_id = await self.db.get_repost_author(repost.id)
        is_owner = author_id is not None and author_id == interaction.user.id
        media_item = await self.db.get_media_flag_item_by_repost(repost.id)
        header = self._poster_lookup_note(author_id)
        if not is_owner and media_item is None:
            await interaction.response.send_message(
                header or "There's nothing you can do on this message.", ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())
            return
        view = RepostActionView(self, repost, is_owner=is_owner, media_item=media_item)
        content = f"{header}\nWhat would you like to do?" if header else "What would you like to do?"
        await interaction.response.send_message(content, view=view, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
        try:
            view.message = await interaction.original_response()
        except Exception:
            pass

    @staticmethod
    def _poster_lookup_note(author_id: Optional[int]) -> Optional[str]:
        """Who posted this, plus the id in a copyable code span. Reposts are sent by a webhook,
        so Discord's `from:` search cannot find them; searching the raw id can, via the marker
        every repost carries. None for a repost predating the repost_authors table."""
        if author_id is None:
            return None
        return (f"Posted by <@{author_id}>\n"
                f"-# Search `{author_id}` to find their messages (`from:` search can't see reposts).")

    async def on_repost_action(self, interaction: discord.Interaction, view: RepostActionView, action: str):
        """A button inside the ephemeral action menu was clicked; act on view.repost. A terminal
        action stop()s the view so its 180s timeout can't later overwrite the result message with
        the "expired" note. Edit opens a modal and leaves the menu usable, so it is NOT stopped
        (its timeout then correctly retires the stale menu)."""
        repost = view.repost
        if action == "edit":
            if not await self._can_manage_repost(interaction, repost):
                return
            # Seed from the latest content the bot last wrote (if any), so a second edit from the same
            # still-open menu doesn't revert the prior one. A modal can't be deferred, so this must
            # not do a REST fetch first; the cache gives fresh content without one.
            await interaction.response.send_modal(
                RepostEditModal(self, repost, content_override=self._recent_edits.get(repost.id)))
        elif action == "delete":
            if not await self._can_manage_repost(interaction, repost):
                return
            try:
                await repost.delete()
            except discord.NotFound:
                pass  # already gone counts as deleted (matches _safe_delete)
            except Exception as exc:
                logger.warning("Failed to delete repost on poster request: %s", exc)
                await interaction.response.edit_message(content="❌ Couldn't delete the message.", view=None)
                view.stop()
                return
            await interaction.response.edit_message(content="🗑️ Deleted.", view=None)
            view.stop()
        elif action in ("safe", "flag"):
            mediaflag = self.bot.get_cog("MediaFlagCog")
            if mediaflag is None or view.media_item is None:
                await interaction.response.edit_message(content="Media review is unavailable right now.", view=None)
            else:
                await mediaflag.on_vote_item(interaction, view.media_item, action)
            view.stop()

    async def on_repost_edit(self, interaction: discord.Interaction):
        # Legacy direct Edit button, still present on older reposts in channel history.
        if not await self._can_manage_repost(interaction, interaction.message):
            return
        await interaction.response.send_modal(RepostEditModal(self, interaction.message))

    async def on_repost_delete(self, interaction: discord.Interaction):
        # Legacy direct Delete button, still present on older reposts in channel history.
        if not await self._can_manage_repost(interaction, interaction.message):
            return
        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass  # already gone counts as deleted (matches _safe_delete and the menu delete branch)
        except Exception as exc:
            logger.warning("Failed to delete repost on poster request: %s", exc)
            await interaction.response.send_message("❌ Couldn't delete the message.", ephemeral=True)
            return
        await interaction.response.send_message("🗑️ Deleted.", ephemeral=True)

    async def apply_repost_edit(self, interaction: discord.Interaction, message: discord.Message, new_content: str):
        # Defense in depth: re-verify ownership on the modal submit (a separate interaction).
        author_id = await self.db.get_repost_author(message.id)
        if author_id is None or author_id != interaction.user.id:
            await interaction.response.send_message(
                "Only the person who posted this message can edit it.", ephemeral=True)
            return
        if self._actor_timed_out(interaction.user):
            await interaction.response.send_message("You can't edit messages while timed out.", ephemeral=True)
            return

        # Ack now: fetching a cold webhook plus the edit can outrun the 3s interaction
        # window, and the rest of this method reports back via followup.
        await interaction.response.defer(ephemeral=True)

        # Everything after the defer runs under one guard so any failure (a DB read, a cold
        # webhook fetch, the edit) still reports back, rather than leaving a stuck spinner.
        try:
            # Re-run the filter so an edit can't slip a filtered word back in. Exempt authors
            # are skipped too, matching the post-time pipeline (their posts aren't filtered).
            settings = await self.db.get_message_filter_settings()
            if settings.get("rules_version") != self._rules_version:
                await self._reload_rules(settings)
            cleaned, matched = new_content, []
            if settings.get("enabled") and not self._author_is_exempt(interaction.user):
                cleaned, matched = apply_rules(new_content, self._compiled)
                if cleaned == new_content:
                    matched = []
            # Neutralize masked-link markdown [label](url): a webhook message renders it as a
            # clickable link whose label hides the destination (a normal user message shows it
            # inert), so the repost would otherwise show a link with a hidden destination. Bare
            # URLs keep their visible destination. Mirrors how the reply-context line is already hardened.
            cleaned = _defang_masked_links(cleaned)

            # Re-tag with the author marker so edits stay findable/purgeable by the poster's id.
            marked = f"{cleaned} {_author_marker(author_id)}" if cleaned else _author_marker(author_id)
            parent, thread = self._resolve_channel(message)
            webhook = await self._get_webhook(parent)
            await webhook.edit_message(
                message.id,
                content=marked,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
                thread=thread if thread is not None else discord.utils.MISSING,
            )
        except Exception as exc:
            logger.warning("Failed to edit repost: %s", exc)
            await interaction.followup.send("❌ Couldn't update the message.", ephemeral=True)
            return

        # Remember what was just written so a re-edit from a still-open menu prefills fresh. Bounded
        # and LRU: pop-before-insert moves a re-edited repost to the newest slot so a hot one can't
        # be evicted while its menu is still open; evict the oldest once it grows past the cap.
        self._recent_edits.pop(message.id, None)
        self._recent_edits[message.id] = cleaned
        if len(self._recent_edits) > 512:
            self._recent_edits.pop(next(iter(self._recent_edits)))

        # Always "Updated", whether or not a term was cleaned; every edit is audit-logged
        # for the mods.
        await interaction.followup.send("✅ Updated.", ephemeral=True)
        await self._log_edit(message, new_content, cleaned, matched, settings, author_id)

    async def _log_edit(self, message, original, cleaned, matched, settings, author_id):
        """Audit EVERY repost edit to the DB, so a content change is always traceable even
        when it trips no filter rule (an edit is the one way a member pushes text through the
        bot's webhook, which Discord AutoMod and the audit log don't see). Post a mod-log
        embed only when the edit reintroduced a filtered word, to keep the channel signal-heavy."""
        try:
            await self.db.add_message_filter_log(
                message.channel.id, message.id, author_id, original, cleaned, matched)
        except Exception as exc:
            logger.warning("Failed to record repost-edit log row: %s", exc)
        if not matched:
            return
        channel = await self._resolve_log_channel(settings, message.guild)
        if channel is None:
            return
        embed = discord.Embed(title="✏️ Repost edit re-filtered", color=discord.Color.orange(),
                              timestamp=discord.utils.utcnow())
        embed.add_field(name="Author", value=f"<@{author_id}> ({author_id})", inline=False)
        embed.add_field(name="Channel", value=getattr(message.channel, "mention", str(message.channel.id)), inline=False)
        embed.add_field(name="Edited to", value=self._clip(original), inline=False)
        embed.add_field(name="Cleaned", value=self._clip(cleaned) or "(empty)", inline=False)
        embed.add_field(name="Rules matched", value=", ".join(str(m) for m in matched) or "-", inline=False)
        try:
            await channel.send(embed=embed)
        except Exception as exc:
            logger.warning("Failed to send repost-edit log embed: %s", exc)

    # ------------------------------------------------------------------ #
    # Admin slash command (/manage_filters)
    # ------------------------------------------------------------------ #

    async def _ensure_admin(self, interaction: discord.Interaction) -> bool:
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member or not member.guild_permissions.administrator:
            msg = "❌ You need administrator permissions to use this command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return False
        return True

    @app_commands.command(name="manage_filters", description="[ADMIN] Manage the message word-filter")
    @app_commands.describe(
        action="What to do (add, list, enable, set_log_channel, etc.).",
        pattern="(add) The word/phrase to match, or a regex if the regex option is on.",
        replacement="(add) What each match becomes. Leave empty to delete the matched word.",
        rule_id="(remove/enable_rule/disable_rule) The rule's id number, shown by action:list.",
        channel="(set_log_channel) Log channel; (exclude_channel/include_channel) skip the whole pipeline (filter + media review) in this channel.",
        role="(exclude_role/include_role) Members with this role are never filtered.",
        text="(test) Sample text to preview what the current rules would do to it.",
        regex="(add) Treat the pattern as a regular expression instead of literal text.",
        ignore_case="(add) Match regardless of capitalization. Default on.",
        whole_word="(add) Match the word only on its own, not inside bigger words. Simple mode; weak for Japanese.",
        match_plurals="(add) Also match the plural form (story -> stories). Simple mode only.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="remove", value="remove"),
        app_commands.Choice(name="list", value="list"),
        app_commands.Choice(name="enable_rule", value="enable_rule"),
        app_commands.Choice(name="disable_rule", value="disable_rule"),
        app_commands.Choice(name="set_log_channel", value="set_log_channel"),
        app_commands.Choice(name="enable", value="enable"),
        app_commands.Choice(name="disable", value="disable"),
        app_commands.Choice(name="exclude_role", value="exclude_role"),
        app_commands.Choice(name="include_role", value="include_role"),
        app_commands.Choice(name="exclude_channel", value="exclude_channel"),
        app_commands.Choice(name="include_channel", value="include_channel"),
        app_commands.Choice(name="test", value="test"),
    ])
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def manage_filters(
        self,
        interaction: discord.Interaction,
        action: str,
        pattern: Optional[str] = None,
        replacement: Optional[str] = None,
        rule_id: Optional[int] = None,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None,
        text: Optional[str] = None,
        regex: Optional[bool] = False,
        ignore_case: Optional[bool] = True,
        whole_word: Optional[bool] = False,
        match_plurals: Optional[bool] = False,
    ):
        if not await self._ensure_admin(interaction):
            return
        if action == "add":
            await self._mf_add(interaction, pattern, replacement or "", bool(regex),
                               bool(ignore_case), bool(whole_word), bool(match_plurals))
        elif action == "remove":
            await self._mf_remove(interaction, rule_id)
        elif action == "list":
            await self._mf_list(interaction)
        elif action == "enable_rule":
            await self._mf_set_rule_enabled(interaction, rule_id, True)
        elif action == "disable_rule":
            await self._mf_set_rule_enabled(interaction, rule_id, False)
        elif action == "set_log_channel":
            await self._mf_setlog(interaction, channel)
        elif action == "enable":
            await self._mf_set_enabled(interaction, True)
        elif action == "disable":
            await self._mf_set_enabled(interaction, False)
        elif action == "exclude_role":
            await self._mf_role(interaction, role, exempt=True)
        elif action == "include_role":
            await self._mf_role(interaction, role, exempt=False)
        elif action == "exclude_channel":
            await self._mf_channel(interaction, channel, exclude=True)
        elif action == "include_channel":
            await self._mf_channel(interaction, channel, exclude=False)
        elif action == "test":
            await self._mf_test(interaction, text)
        else:
            await interaction.response.send_message("❌ Unknown action.", ephemeral=True)

    async def _mf_add(self, interaction, pattern, replacement, regex, ignore_case, whole_word, match_plurals):
        if not pattern:
            await interaction.response.send_message("❌ Provide a `pattern` for the add action.", ephemeral=True)
            return
        ok, error = validate_pattern(pattern, regex, replacement, ignore_case, whole_word, match_plurals)
        if not ok:
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return
        rule_id = await self.db.add_message_filter_rule(
            pattern, replacement, is_regex=regex, ignore_case=ignore_case,
            whole_word=whole_word, match_plurals=match_plurals, created_by=str(interaction.user.id),
        )
        if rule_id is None:
            await interaction.response.send_message("❌ Failed to add the rule.", ephemeral=True)
            return
        await self._reload_rules(force=True)
        mode = "regex" if regex else "simple"
        settings = await self.db.get_message_filter_settings()
        hint = "" if settings.get("enabled") else "\n⚠️ The filter is currently **off**. Run `/manage_filters action:enable`."
        await interaction.response.send_message(
            f"✅ Added {mode} rule `#{rule_id}`: `{pattern}` → `{replacement}`{hint}", ephemeral=True
        )

    async def _mf_remove(self, interaction, rule_id):
        if rule_id is None:
            await interaction.response.send_message("❌ Provide a `rule_id` for the remove action.", ephemeral=True)
            return
        removed = await self.db.delete_message_filter_rule(rule_id)
        await self._reload_rules(force=True)
        if removed:
            await interaction.response.send_message(f"✅ Removed rule `#{rule_id}`.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ No rule with id `{rule_id}`.", ephemeral=True)

    async def _mf_set_rule_enabled(self, interaction, rule_id, enabled):
        if rule_id is None:
            await interaction.response.send_message("❌ Provide a `rule_id`.", ephemeral=True)
            return
        updated = await self.db.update_message_filter_rule(rule_id, enabled=enabled)
        await self._reload_rules(force=True)
        if updated:
            state = "enabled" if enabled else "disabled"
            await interaction.response.send_message(f"✅ Rule `#{rule_id}` {state}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ No rule with id `{rule_id}`.", ephemeral=True)

    async def _mf_list(self, interaction):
        rules = await self.db.get_message_filter_rules()
        settings = await self.db.get_message_filter_settings()
        if not rules:
            state = "on" if settings.get("enabled") else "off"
            await interaction.response.send_message(
                f"No rules yet. Global filter is **{state}**. Add one with `/manage_filters action:add`.", ephemeral=True
            )
            return
        lines = []
        for r in rules[:40]:
            mode = "regex" if r["is_regex"] else "simple"
            flags = []
            if r["ignore_case"]:
                flags.append("i")
            if r["whole_word"] and not r["is_regex"]:
                flags.append("w")
            if r.get("match_plurals") and not r["is_regex"]:
                flags.append("p")
            state = "✅" if r["enabled"] else "🚫"
            flag_str = f" [{''.join(flags)}]" if flags else ""
            lines.append(f"{state} `#{r['id']}` ({mode}{flag_str}) `{r['pattern']}` → `{r['replacement']}`")
        header = f"**Global filter: {'on' if settings.get('enabled') else 'off'}** · {len(rules)} rule(s)"
        if len(rules) > 40:
            lines.append(f"... and {len(rules) - 40} more")
        await interaction.response.send_message(header + "\n" + "\n".join(lines), ephemeral=True)

    async def _mf_setlog(self, interaction, channel):
        if channel is None:
            await interaction.response.send_message("❌ Provide a `channel` for set_log_channel.", ephemeral=True)
            return
        await self.db.set_message_filter_log_channel(channel.id)
        await interaction.response.send_message(
            f"✅ Filtered originals will be logged to {channel.mention}.", ephemeral=True
        )

    async def _mf_set_enabled(self, interaction, enabled):
        await self.db.set_message_filter_enabled(enabled)
        await self._reload_rules(force=True)
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(f"✅ Message filter {state}.", ephemeral=True)

    async def _mf_role(self, interaction, role, exempt):
        if role is None:
            await interaction.response.send_message("❌ Provide a `role`.", ephemeral=True)
            return
        if exempt:
            await self.db.add_message_filter_excluded_role(role.id, added_by=str(interaction.user.id))
            await self._reload_rules(force=True)
            await interaction.response.send_message(f"✅ {role.mention} is now exempt from filtering.", ephemeral=True)
            return
        removed = await self.db.remove_message_filter_excluded_role(role.id)
        await self._reload_rules(force=True)
        if removed:
            await interaction.response.send_message(f"✅ {role.mention} is no longer exempt.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ {role.mention} was not exempt.", ephemeral=True)

    async def _mf_channel(self, interaction, channel, exclude):
        if channel is None:
            await interaction.response.send_message("❌ Provide a `channel`.", ephemeral=True)
            return
        if exclude:
            await self.db.add_message_filter_excluded_channel(channel.id, added_by=str(interaction.user.id))
            await self._reload_rules(force=True)
            await interaction.response.send_message(
                f"✅ {channel.mention} is now excluded (no filtering or media review here).", ephemeral=True)
            return
        removed = await self.db.remove_message_filter_excluded_channel(channel.id)
        await self._reload_rules(force=True)
        if removed:
            await interaction.response.send_message(f"✅ {channel.mention} is no longer excluded.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ {channel.mention} was not excluded.", ephemeral=True)

    async def _mf_test(self, interaction, text):
        if not text:
            await interaction.response.send_message("❌ Provide `text` for the test action.", ephemeral=True)
            return
        rows = await self.db.get_message_filter_rules(enabled_only=True)
        compiled = compile_rules(rows)
        cleaned, matched = apply_rules(text, compiled)
        if not matched:
            await interaction.response.send_message("No rule matched. The message would be left unchanged.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"**Rules matched:** {', '.join(str(m) for m in matched)}\n"
            f"**Original:** {self._clip(text, 800)}\n"
            f"**Cleaned:** {self._clip(cleaned, 800) or '(empty)'}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MessageFilterCog(bot))
