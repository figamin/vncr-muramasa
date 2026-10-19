"""Mass message purge engine for muramasa.

Deletion is delete-as-you-go over the guild-level `/guilds/{id}/messages/search` endpoint
(bots CAN use it; only the channel-level search 403s). Paging is by message ID, not offset:
search a page, delete its matches, then search for the next messages strictly OLDER than
the oldest seen so far (a max_id cursor for newest-first, min_id for oldest-first). Paging
to older ids means the just-deleted (newer) messages are excluded by the cursor regardless
of index state, so Discord's post-delete search-index lag can't blank the page; an offset-0
re-read would land where the deletes just happened and see a transient empty page. It also
sidesteps the ~5000 offset cap (offset is always 0). Discord documents
min_id/max_id as the sanctioned way to page a timestamp-sorted search past the offset
ceiling. A dated purge merges the cursor into the tighter bound and keeps the opposite
user bound, so it can never page out of the requested window.

An empty page is NOT trusted as "drained": Discord's search under-returns for reasons
unrelated to the purge's own deletes (cold/long-unaccessed messages, index incompleteness),
so the SAME cursor is re-queried a few times before concluding done (safe here: it can't
re-read the deletion zone). Search is best-effort: a run converges in one or a few passes, and a
re-run re-probes and continues. total_results is unreliable and is used only for the
confirm estimate + progress display, never to decide when to stop.

The confirm gate is fed by a fast one-page probe (total_results as an estimate + a preview
sample), not an exhaustive scan. The history walk is kept as a fallback for when search is
unavailable (endpoint blocked) or for filters search can't express: server-side search
covers author, channel, date, has-link/file and mentions, while `regex` and reply-based
directed-at route to the walk.

Pure, discord-free logic lives at module top so it is unit testable without
importing discord. The scan/delete
code that drives the Discord API imports discord lazily inside the methods that
need it.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from typing import Optional

from utils.message_filter import strip_author_marker

logger = logging.getLogger(__name__)

PREVIEW_LIMIT = 15
# Pacing for one-by-one deletion of >14-day messages.
#
# Routing these deletes through discord.py stalls ~118s after every ~42 deletes with ZERO
# 429s: discord.py PRE-EMPTIVELY honors the bucket Discord advertises to bots (~40 per
# ~120s) for old-message deletes. Real enforcement is far looser than that advertised
# bucket: fast raw requests draw only SMALL 429s (retry_after of a few seconds), never a
# long penalty.
#
# So old-message deletes go over raw HTTP with a reactive model: send at a set pace, honor
# a real 429's retry_after exactly, never pre-emptively sleep out an advertised reset. The
# pacer is bounded and quick to recover so a burst of 429s can only ease it off, never
# strand it at a ceiling it cannot climb back from.
# Discord's own headers put bot old-message deletes at **30 per 120s PER CHANNEL**
# (X-RateLimit-Limit: 30, Reset-After: ~120s), enforced as a token bucket refilling ~1 per
# 4s: burst 30, then one delete per ~3.4s retry_after. Pushing faster converts clean pacing
# into 429 waits of the same duration: identical throughput, plus ban risk. So each channel
# paces at the refill rate, and throughput scales by running channels in PARALLEL (each has
# its own bucket). A backlog concentrated in one channel is bound by that channel's
# refill rate.
DELETE_PACE_BASE = 4.0       # per channel: the measured bucket refill interval
DELETE_PACE_MIN = 3.4        # never hammer below the refill; 429s add nothing
DELETE_PACE_MAX = 10.0       # bounded so a backoff can never strand the purge
# AGGREGATE gate across all channels, and the one that matters most: old-delete 429s carry
# scope=user, i.e. Discord's old-delete throttle is substantially PER BOT ACCOUNT, not per
# channel, with an aggregate equilibrium around ~0.35-0.4/s. Pin the gate there so it hugs
# the ceiling without oscillating into 429 churn (backing off, creeping down and getting
# hit again wastes wall clock in forced waits). Channel parallelism still helps, but
# modestly: it fills the account-wide allowance smoothly rather than multiplying it.
GLOBAL_PACE_BASE = 2.8
GLOBAL_PACE_MIN = 2.4        # ~0.4/s aggregate ceiling
GLOBAL_PACE_MAX = 8.0
RAW_DELETE_MAX_RETRIES = 5
RAW_DELETE_TIMEOUT = 30
# A delete round-trip is normally well under a second. A markedly longer one means a quota
# refill window was waited out: recorded, but NOT treated as a reason to slow down.
DELETE_SLOW_AFTER = 1.2
DELETE_CLEAN_STREAK = 10     # recover promptly; discord.py is the real safety net
DELETE_WIDEN = 1.5           # ease off on a confirmed 429 (never double)
DELETE_NARROW = 0.9
SCAN_CONCURRENCY = 8         # channels scanned concurrently on the history-walk fallback
# Discord's delete quota is PER CHANNEL, so channels run in parallel: while one waits out its
# refill window the rest keep deleting. This is where throughput actually comes from. Well
# under Discord's ~50 req/s global ceiling.
DELETE_CHANNEL_WORKERS = 8
MAX_PENDING_DELETES = 400     # producer backpressure so the cursor can't race far ahead
# Rolling buffer of recently-deleted messages surfaced live in the web console.
DELETION_LOG_SIZE = 100
SNIPPET_LIMIT = 80

# Discord message search (guild endpoint, bot-usable) returns up to ~25 hits per page and
# rejects offsets past ~5000. Offset is NOT used: paging is by message id (a max_id cursor
# for newest-first, min_id for oldest-first). After deleting a page the next query asks for
# messages strictly OLDER than the oldest seen so far, so:
#   - offset is always 0 (Discord's ~5000 offset cap is irrelevant), and
#   - a just-deleted position is never re-read. An offset-0 re-read would land in the
#     post-delete index-lag zone (Discord briefly still shows the deleted messages and
#     momentarily reports total_results: 0), so the loop would see a transient empty page
#     and quit. A max_id cursor excludes the deleted (newer) messages by id regardless of
#     index state, so index lag on them can't blank the page. (Discord documents
#     min_id/max_id as the sanctioned way to page a timestamp-sorted search past the
#     offset ceiling.)
# A page can STILL under-return for reasons unrelated to the purge's own deletes:
# cold/long-unaccessed messages, or transient search index incompleteness. An empty page is
# therefore NOT proof the range is drained, so a bounded empty-page retry re-issues the
# SAME cursor a few times before concluding done. Retrying the same cursor is safe here (it
# can never re-read the deletion zone or double-delete). total_results is the most
# lag-corrupted field and is NEVER used to decide when to stop; it serves only the
# confirm-gate estimate and the live progress display.
SEARCH_MAX_RETRIES = 20          # per page, across 202 (indexing) + 429 (rate limit) waits
# Cancel is cooperative, so a purge parked in a long sleep would ignore Stop for the whole
# delay (the 202/429 wait alone reaches 30s). Sleep in slices this size instead, so a cancel
# lands almost immediately rather than whenever the wait happens to end.
CANCEL_POLL_STEP = 0.25
SEARCH_PAGE_DELAY = 1.0          # polite pacing between pages (429s are retried separately)
SEARCH_EMPTY_PAGE_RETRIES = 5    # re-query the same cursor this many times before "drained"
DISCORD_EPOCH_MS = 1420070400000
# default + reply; the numeric MessageType values search returns for deletable messages.
_DELETABLE_SEARCH_TYPES = {0, 19}

# Matches an http(s) URL anywhere in the message content.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass
class PurgeFilters:
    """Criteria for selecting messages to delete.

    Every field is optional; a purge can be scoped by author (`target_user_id`),
    by who it's directed at (`directed_at_user_id`), by date, or by content, in any
    combination. `after`/`before`/`include_nsfw` are applied while walking history
    and choosing channels, not by `message_matches`. A bad `regex` raises here (at
    parse time). The "don't match every message in the server" safety net is scope
    aware and lives in `purge_is_bounded`, not here, so a single-channel purge with
    no other filter is still allowed.
    """

    target_user_id: Optional[int] = None
    directed_at_user_id: Optional[int] = None  # posts that mention or reply to this user
    contains: Optional[str] = None
    regex: Optional[str] = None
    has_link: bool = False
    has_file: bool = False
    include_pinned: bool = False
    after: Optional[object] = None  # datetime, history-level
    before: Optional[object] = None  # datetime, history-level
    # On by default: a purge is meant to remove ALL of the target's messages, and silently
    # leaving NSFW channels behind would make a run look complete when it is not.
    include_nsfw: bool = True
    oldest_first: bool = False  # delete oldest matches first (subtle), vs newest first
    # Thread policy, scoped to PRIVATE threads only. A private thread is searchable only by
    # its members, so its contents are invisible to everyone else, yet a user's own bot or
    # quiz thread can hold thousands of trivial messages, and clearing those one at a time
    # spends the whole account-wide delete budget on content nobody can read. PUBLIC threads
    # are ordinary visible content and are always purged message-by-message, unaffected here.
    # delete_private_threads is ON by default: a target's own private thread is invisible to
    # the rest of the server, so paging through thousands of its messages spends the whole
    # account-wide delete budget on content nobody can read (hours for one large thread).
    delete_private_threads: bool = True   # target created it -> delete the thread in one call
    skip_private_threads: bool = False    # leave private threads alone entirely
    excluded_channel_ids: frozenset = frozenset()  # channel/thread ids never touched
    _regex: Optional[re.Pattern] = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._regex = re.compile(self.regex, re.IGNORECASE) if self.regex else None


def parse_channel_ids(value) -> frozenset:
    """Parse channel/thread ids out of free-form text: comma/space/newline separated,
    and channel mentions like <#123> also work. Extracts snowflake-length digit runs;
    None/blank -> empty set."""
    if not value:
        return frozenset()
    return frozenset(int(m) for m in re.findall(r"\d{15,20}", str(value)))


def filters_narrow_selection(filters: PurgeFilters) -> bool:
    """True if any filter field restricts which messages match. Excludes the
    modifiers (include_pinned/include_nsfw/oldest_first) that only broaden or
    reorder, never narrow."""
    return any((
        filters.target_user_id is not None,
        filters.directed_at_user_id is not None,
        filters.after is not None,
        filters.before is not None,
        bool(filters.contains),
        bool(filters.regex),
        filters.has_link,
        filters.has_file,
    ))


def purge_is_bounded(scope, filters: PurgeFilters) -> bool:
    """True if the purge can't match every message in the server. A single-channel
    scope is a bound on its own; a guild-wide scope needs at least one narrowing
    filter (author, directed-at, date, or content)."""
    if scope != "guild":
        return True
    return filters_narrow_selection(filters)


def message_snippet(message, limit: int = SNIPPET_LIMIT) -> str:
    """Short, display-safe summary of a message's content for previews/logs."""
    content = strip_author_marker(message.content)[:limit]
    if not content and message.attachments:
        return f"[{len(message.attachments)} attachment(s)]"
    return content


def record_capped(buffer: list, entry, cap: int) -> list:
    """Append entry to buffer, keeping only the most recent `cap` items."""
    buffer.append(entry)
    if len(buffer) > cap:
        del buffer[:len(buffer) - cap]
    return buffer


def _message_directed_at(message, user_id: int) -> bool:
    """True if the message mentions `user_id` or is a reply to one of their messages.

    Pure attribute reads: `raw_mentions` (covers @-mentions and reply-pings) and the
    resolved reply reference. A reply whose original was deleted has no resolvable
    author, so it can't match via the reply path.
    """
    if user_id in (getattr(message, "raw_mentions", None) or []):
        return True
    ref = getattr(message, "reference", None)
    resolved = getattr(ref, "resolved", None) if ref is not None else None
    author = getattr(resolved, "author", None)
    return author is not None and getattr(author, "id", None) == user_id


def message_matches(message, filters: PurgeFilters) -> bool:
    """True if `message` should be deleted under `filters`. Pure; reads attributes only."""
    if filters.target_user_id is not None and message.author.id != filters.target_user_id:
        return False
    if filters.directed_at_user_id is not None and not _message_directed_at(message, filters.directed_at_user_id):
        return False
    if message.pinned and not filters.include_pinned:
        return False
    if filters.contains and filters.contains.lower() not in (message.content or "").lower():
        return False
    if filters._regex is not None and not filters._regex.search(message.content or ""):
        return False
    if filters.has_link and not _URL_RE.search(message.content or ""):
        return False
    if filters.has_file and not message.attachments:
        return False
    return True


def to_snowflake(dt) -> int:
    """Discord snowflake for a datetime, for search min_id/max_id date bounds."""
    return (int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22


def _base_search_params(filters: "PurgeFilters", channel_ids) -> list:
    """The non-paging, non-date query params shared by the probe and the cursor loop.

    `contains` is sent as the `content` term so the search narrows it server-side and
    stays fast; it is word-tokenized (not substring), and a client-side substring re-check
    tightens the hits. `regex` is NOT sent (search has no regex) and routes to the history
    walk. `channel_ids` is None for a whole-guild search, or a list to scope.
    """
    params = []
    if filters.target_user_id is not None:
        params.append(("author_id", str(filters.target_user_id)))
    for cid in (channel_ids or []):
        params.append(("channel_id", str(cid)))
    if filters.has_link:
        params.append(("has", "link"))
    if filters.has_file:
        params.append(("has", "file"))
    if filters.contains:
        params.append(("content", filters.contains))
    if filters.directed_at_user_id is not None:
        params.append(("mentions", str(filters.directed_at_user_id)))
    if filters.include_nsfw:
        params.append(("include_nsfw", "true"))
    params.append(("sort_by", "timestamp"))
    params.append(("sort_order", "asc" if filters.oldest_first else "desc"))
    return params


def build_search_params(filters: "PurgeFilters", channel_ids, offset: int) -> list:
    """Offset-paged query params for the guild search endpoint. Used by the fast confirm
    probe (offset 0, one page for the estimate); the delete loop uses build_cursor_params."""
    params = _base_search_params(filters, channel_ids)
    if filters.after is not None:
        params.append(("min_id", str(to_snowflake(filters.after))))
    if filters.before is not None:
        params.append(("max_id", str(to_snowflake(filters.before))))
    params.append(("offset", str(offset)))
    return params


def build_cursor_params(filters: "PurgeFilters", channel_ids, cursor) -> list:
    """Id-cursor-paged query params for the delete loop: page by message id, not offset.

    `cursor` is the boundary snowflake from the previous page (its oldest id for
    newest-first, its newest id for oldest-first), or None for the first page. It is MERGED
    into the tighter of the user's date bounds (never appended as a second min_id/max_id,
    whose duplicate-key behavior is undefined), and the OPPOSITE user bound is ALWAYS kept.
    That keeps a dated purge inside its window: without the retained floor/ceiling a desc
    purge with an `after` filter would page right past the floor and delete out-of-window
    history (an irreversible scope violation).
      newest-first (desc): max_id = min(before, cursor); min_id = after (kept as the floor)
      oldest-first  (asc): min_id = max(after,  cursor); max_id = before (kept as the ceiling)
    """
    params = _base_search_params(filters, channel_ids)
    after_snow = to_snowflake(filters.after) if filters.after is not None else None
    before_snow = to_snowflake(filters.before) if filters.before is not None else None
    if filters.oldest_first:
        lower = after_snow
        if cursor is not None:
            lower = cursor if lower is None else max(lower, cursor)
        min_id, max_id = lower, before_snow
    else:
        upper = before_snow
        if cursor is not None:
            upper = cursor if upper is None else min(upper, cursor)
        min_id, max_id = after_snow, upper
    if min_id is not None:
        params.append(("min_id", str(min_id)))
    if max_id is not None:
        params.append(("max_id", str(max_id)))
    params.append(("offset", "0"))
    return params


def extract_hits(data: dict) -> list:
    """The actually-matched messages from a search response. `messages` is a list of
    context groups; the searched-for message in each carries `hit: true`."""
    hits = []
    for group in (data.get("messages") or []):
        for msg in group:
            if isinstance(msg, dict) and msg.get("hit"):
                hits.append(msg)
    return hits


def search_result_matches(msg: dict, filters: "PurgeFilters") -> bool:
    """Client-side filter for a search hit (a raw message dict). Everything a filter
    can express server-side (author, mentions, date, has-link/file, the `content`
    term) is already applied by the search, so a hit is a real match; this only
    drops what search returns but must still not be deleted: non-deletable message types
    and (unless include_pinned) pinned messages. `contains` is NOT re-checked against
    msg.content: search indexes embeds too, so a term hit can have empty content.
    `regex` never reaches here (it routes to the history walk)."""
    if msg.get("type") not in _DELETABLE_SEARCH_TYPES:
        return False
    if msg.get("pinned") and not filters.include_pinned:
        return False
    return True


def search_snippet(msg: dict, limit: int = SNIPPET_LIMIT) -> str:
    """Short display-safe summary of a search hit for previews/logs."""
    content = strip_author_marker(msg.get("content"))[:limit]
    if not content and msg.get("attachments"):
        return f"[{len(msg['attachments'])} attachment(s)]"
    return content


def plan_search_page(hits, filters: "PurgeFilters"):
    """Split one page of raw search hits into (to_delete, skipped).

    `to_delete` is the hits that should and can be deleted; `skipped` is everything else the
    search returned that must NOT be deleted (non-deletable type, pinned when not included,
    an excluded channel, or a malformed hit). The cursor advances past BOTH lists (it is
    computed over every hit id on the page), so skipped messages are paged over, not
    re-read. Every hit lands in exactly one list.
    """
    to_delete, skipped = [], []
    for msg in hits:
        try:
            ch_id = int(msg["channel_id"])
            int(msg["id"])
        except (KeyError, ValueError, TypeError):
            skipped.append(msg)          # can't act on it, but the cursor still pages past it
            continue
        if ch_id in filters.excluded_channel_ids:
            skipped.append(msg)
            continue
        if not search_result_matches(msg, filters):
            skipped.append(msg)
            continue
        to_delete.append(msg)
    return to_delete, skipped


def hit_ids(hits) -> list:
    """The parseable integer message ids on a page of raw search hits (malformed ids
    dropped). The cursor is computed over these, so a page of only-malformed hits yields
    an empty list and the loop treats it as unproductive rather than crashing on min()."""
    ids = []
    for msg in hits:
        try:
            ids.append(int(msg["id"]))
        except (KeyError, ValueError, TypeError):
            pass
    return ids


class AdaptivePacer:
    """One shared, self-tuning interval between old-message deletes.

    Discord hides the >14-day delete limit from rate-limit headers, so the only signal is
    TIMING: a delete call that runs much longer than a normal round-trip means
    discord.py silently slept off a 429 inside it. `observe(seconds)` feeds each call's
    duration back in; the interval widens on a throttled call and narrows again only after
    a streak of clean ones (a single fast call must not undo a backoff, or the pace
    oscillates straight back into the limit). Pure and clock-free so it is unit testable.
    """

    def __init__(self, base=DELETE_PACE_BASE, minimum=DELETE_PACE_MIN, maximum=DELETE_PACE_MAX,
                 slow_after=DELETE_SLOW_AFTER, clean_streak=DELETE_CLEAN_STREAK,
                 widen=DELETE_WIDEN, narrow=DELETE_NARROW):
        self.interval = float(base)
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self.slow_after = float(slow_after)
        self.clean_streak = int(clean_streak)
        self._widen = float(widen)
        self._narrow = float(narrow)
        self.throttle_events = 0     # confirmed 429s: the pace WAS too fast
        self.bucket_waits = 0        # long calls with no 429: discord.py waiting out the bucket
        self.bucket_wait_seconds = 0.0
        self._clean = 0

    def observe(self, seconds: float) -> None:
        """Record how long one delete call took.

        A long call here does NOT mean the pace is too fast. discord.py reads Discord's
        rate-limit headers and sleeps out the bucket BEFORE sending, so a call can block for
        minutes with ZERO 429s logged. That bucket resets on Discord's own schedule no matter
        how slowly requests are sent, so widening the interval in response would only waste
        the capacity Discord does grant. Only a confirmed 429 (`penalize`) widens.
        """
        if seconds >= self.slow_after:
            self.bucket_waits += 1
            self.bucket_wait_seconds += seconds
            self._clean = 0          # don't speed up immediately after a stall either
            return
        self._clean += 1
        if self._clean >= self.clean_streak:
            self._clean = 0
            self.interval = max(self.interval * self._narrow, self.minimum)

    def penalize(self, retry_after: float = 0.0) -> None:
        """Widen on a CONFIRMED rate limit, however fast the call was. discord.py retries a
        429 internally in ~0.3-0.7s and reports it only to its logger, so without this the
        pacer scores a throttled call as clean and speeds up into a longer Discord penalty.

        `retry_after` (seconds Discord asked to wait) scales the response: a 0.3s retry is
        a nudge, a 60s+ one means the bot is deep in a penalty bucket and must back off far
        more than one step, or the whole purge is spent re-earning it.
        """
        self.throttle_events += 1
        self._clean = 0
        step = self._widen
        if retry_after and retry_after > 1.0:
            # Ride at least the pace Discord is demanding, not a fraction of it.
            step = max(step, min(retry_after, self.maximum) / max(self.interval, 0.01))
        self.interval = min(self.interval * step, self.maximum)

    def stats(self) -> dict:
        return {
            "interval": round(self.interval, 2),
            "throttle_events": self.throttle_events,
            "bucket_waits": self.bucket_waits,
            "bucket_wait_seconds": round(self.bucket_wait_seconds, 1),
        }


async def sleep_unless_cancelled(job, seconds: float, step: float = CANCEL_POLL_STEP) -> bool:
    """Sleep, but wake as soon as the job is cancelled.

    A plain asyncio.sleep would hold a cancelled purge for the full delay, and these waits
    run to 30s, which makes Stop look broken. Slicing the wait costs nothing and bounds cancel
    latency to `step`. Returns True if it slept the whole duration, False if cancel cut it.
    """
    remaining = float(seconds)
    while remaining > 0:
        if job is not None and job.cancel_requested:
            return False
        chunk = min(step, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk
    return True


class ChannelDeleter:
    """Runs one delete worker per channel, each with its own pace.

    Discord's message-delete quota is PER CHANNEL, so a single shared gate would let one
    channel running dry stall the entire purge while every other channel sat idle. Here
    each channel gets its own queue, its own AdaptivePacer and its own task, so exhausted
    channels wait while the others keep deleting.

    The search producer hands matches over and carries on paging instead of blocking on
    deletes, bounded by MAX_PENDING_DELETES so the cursor cannot race arbitrarily far ahead.
    """

    def __init__(self, manager, guild, job, now, session=None, headers=None):
        self.manager = manager
        self.guild = guild
        self.job = job
        self.now = now
        self.session = session
        self.headers = headers
        self._queues: dict = {}
        self._pacers: dict = {}
        self._tasks: list = []
        self._slots = asyncio.Semaphore(DELETE_CHANNEL_WORKERS)
        self._closed = False
        self.pending = 0

    @property
    def active_channels(self) -> int:
        return len(self._queues)

    def aggregate_stats(self) -> dict:
        """Pace stats rolled up across every channel worker."""
        pacers = list(self._pacers.values())
        if not pacers:
            return {"interval": 0.0, "throttle_events": 0, "bucket_waits": 0,
                    "bucket_wait_seconds": 0.0, "channels": 0}
        return {
            "interval": round(sum(p.interval for p in pacers) / len(pacers), 2),
            "throttle_events": sum(p.throttle_events for p in pacers),
            "bucket_waits": sum(p.bucket_waits for p in pacers),
            "bucket_wait_seconds": round(sum(p.bucket_wait_seconds for p in pacers), 1),
            "channels": len(pacers),
        }

    async def submit(self, matches) -> None:
        for m in matches:
            self._queue_for(m.channel_id).put_nowait(m)
            self.pending += 1
        while self.pending >= MAX_PENDING_DELETES and not self.job.cancel_requested:
            await asyncio.sleep(0.2)

    def _queue_for(self, channel_id):
        queue = self._queues.get(channel_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[channel_id] = queue
            self._pacers[channel_id] = AdaptivePacer()
            self._tasks.append(asyncio.create_task(self._worker(channel_id, queue)))
        return queue

    async def _take(self, queue, limit=100):
        """Wait briefly for work, then grab everything already queued (up to `limit`) so a
        run of recent messages can still bulk-delete 100 at a time."""
        items = []
        try:
            items.append(await asyncio.wait_for(queue.get(), timeout=0.5))
        except (asyncio.TimeoutError, TimeoutError):
            return items
        while len(items) < limit:
            try:
                items.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return items

    async def _worker(self, channel_id, queue):
        pacer = self._pacers[channel_id]
        gate = DeleteGate(pacer)
        while True:
            if self.job.cancel_requested:
                break
            batch = await self._take(queue)
            if batch:
                async with self._slots:
                    try:
                        await self.manager._delete_channel(
                            self.guild, channel_id, batch, self.job, self.now, gate,
                            self.session, self.headers)
                    except Exception as e:      # one bad channel must not kill the purge
                        logger.warning("Purge %s: channel %s delete batch failed: %s",
                                       self.job.id, channel_id, e)
                        self.job.failed += len(batch)
                self.pending -= len(batch)
            elif self._closed:
                break

    async def close(self) -> None:
        """Stop accepting work and wait for every channel to finish draining."""
        self._closed = True
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


class RateLimitWatcher(logging.Handler):
    """Feeds discord.py's 429s into a job's pacer.

    discord.py handles a 429 by sleeping and retrying inside the call: it raises nothing and
    returns normally, so the ONLY place the throttle surfaces is its own logger
    ("We are being rate limited. ... Retrying in 0.30 seconds."). Timing can't substitute:
    a 429 absorbed in 0.3s is indistinguishable from a healthy call. Attach for the duration
    of the delete phase and always detach in a finally.
    """

    LOGGER_NAME = "discord.http"
    # "...responded with 429. Retrying in 0.30 seconds." gives the severity of the throttle.
    _RETRY_AFTER = re.compile(r"retrying in ([\d.]+) second", re.IGNORECASE)

    def __init__(self, job):
        super().__init__(level=logging.WARNING)
        self.job = job

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return
        if "rate limited" not in message.lower():
            return
        retry_after = 0.0
        found = self._RETRY_AFTER.search(message)
        if found:
            try:
                retry_after = float(found.group(1))
            except ValueError:
                retry_after = 0.0
        self.job.note_rate_limited(retry_after)

    def __enter__(self):
        logging.getLogger(self.LOGGER_NAME).addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger(self.LOGGER_NAME).removeHandler(self)
        return False


class DeleteGate:
    """Serializes old-message deletes across ALL channels onto one shared adaptive interval.

    The point is that the aggregate delete rate becomes the controlled quantity. If each
    channel slept independently, N concurrent channels would multiply the real rate and
    trip Discord's hidden >14-day limit. Here every old-message delete queues for its turn
    behind one interval, so concurrency does not change throughput; the pacer does. The
    lock is created lazily so it binds to the running event loop, not import time.
    """

    def __init__(self, pacer: AdaptivePacer):
        self.pacer = pacer
        self._lock = None
        self._next_at = 0.0

    async def wait_turn(self, job=None) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            delay = self._next_at - time.monotonic()
            if delay > 0:
                if job is not None:
                    job.t_pace += delay
                await sleep_unless_cancelled(job, delay)
            self._next_at = time.monotonic() + self.pacer.interval


def next_cursor(ids, oldest_first: bool) -> int:
    """The boundary snowflake to page past next: the NEWEST id seen for oldest-first (paging
    upward), else the OLDEST id seen (paging downward). `ids` must be non-empty."""
    return max(ids) if oldest_first else min(ids)


def cursor_progressed(prev, new, oldest_first: bool) -> bool:
    """True if the boundary moved strictly past the previous one in the paging direction.
    False means the page didn't advance (only the boundary re-appeared, the inclusive-bound
    case, or a transient under-return), so the loop retries then stops rather than looping
    forever on the same id."""
    if prev is None:
        return True
    return new > prev if oldest_first else new < prev


# Discord refuses to bulk-delete messages older than 14 days. A small safety margin
# sends messages near the boundary down the slower one-by-one path instead of
# failing a bulk call.
BULK_DELETE_MAX_AGE = timedelta(days=14)


@dataclass
class MatchedMessage:
    """A single message selected for deletion (minimal, no full Message object held)."""

    channel_id: int
    message_id: int
    created_at: object  # datetime
    snippet: str = ""


def partition_by_age(matches, now, cutoff: timedelta = BULK_DELETE_MAX_AGE):
    """Split matches into (recent, old) by age. Recent (< cutoff) can be bulk deleted."""
    recent, old = [], []
    boundary = now - cutoff
    for m in matches:
        (recent if m.created_at > boundary else old).append(m)
    return recent, old


def chunked(seq, size):
    """Yield successive `size`-length chunks of `seq`."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def parse_purge_date(value):
    """Parse a date as UTC. Accepts YYYY-MM-DD, 'YYYY-MM-DD HH:MM', and the ISO
    'YYYY-MM-DDTHH:MM[:SS]' form an <input type=datetime-local> sends. None/blank ->
    None; unparseable -> ValueError."""
    if not value or not str(value).strip():
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Bad date '{value}'. Use YYYY-MM-DD or 'YYYY-MM-DD HH:MM' (UTC).")


def build_delete_plan(matches, now, oldest_first: bool = False,
                      cutoff: timedelta = BULK_DELETE_MAX_AGE):
    """Order deletions and group bulk-eligible (recent) messages into 100-chunks.

    Returns a list of ("bulk", [MatchedMessage,...]) and ("single", MatchedMessage),
    in the order they should be deleted. Recent (< cutoff) use bulk; old use single.
    oldest_first deletes oldest->newest (subtle); default deletes newest->oldest.
    """
    recent, old = partition_by_age(matches, now, cutoff)
    recent = sorted(recent, key=lambda x: x.created_at)
    old = sorted(old, key=lambda x: x.created_at)
    plan = []
    if oldest_first:
        plan.extend(("single", x) for x in old)                  # oldest individual first
        plan.extend(("bulk", c) for c in chunked(recent, 100))   # then recent bulk
    else:
        plan.extend(("bulk", c) for c in chunked(list(reversed(recent)), 100))  # newest bulk first
        plan.extend(("single", x) for x in reversed(old))        # then older individual
    return plan


class SearchUnavailable(Exception):
    """Raised when the guild search endpoint can't be used (403/blocked or exhausted
    retries), so the scan falls back to walking history."""


class PurgeBusyError(Exception):
    """Raised when a new purge is requested while one is already running."""


class PurgeStateError(Exception):
    """Raised when confirming/acting on a job that is not in the right state."""


class PurgeStatus:
    SCANNING = "scanning"
    AWAITING = "awaiting_confirmation"
    DELETING = "deleting"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    EXPIRED = "expired"
    IDLE = "idle"


BLOCKING_STATUSES = {PurgeStatus.SCANNING, PurgeStatus.AWAITING, PurgeStatus.DELETING}
TERMINAL_STATUSES = {
    PurgeStatus.COMPLETED, PurgeStatus.CANCELLED, PurgeStatus.FAILED, PurgeStatus.EXPIRED,
}


@dataclass
class PurgeJob:
    id: str
    requested_by: int
    requested_via: str          # "slash" | "console"
    scope: object               # "guild" or a channel id (int)
    guild_id: int
    filters: PurgeFilters
    status: str = PurgeStatus.SCANNING
    channels_scanned: int = 0
    messages_scanned: int = 0
    matched: int = 0
    deleted: int = 0
    failed: int = 0
    current_channel: Optional[str] = None
    preview: list = field(default_factory=list)
    matches: list = field(default_factory=list)
    recent_deletions: list = field(default_factory=list)  # rolling live delete log
    server_total: int = 0        # the search's total_results, kept as the MAX ever seen: a
                                 # confirm-gate estimate and progress denominator only, NEVER
                                 # a stop signal (it is unreliable and reads 0 post-delete)
    is_estimate: bool = False    # matched/server_total is a search estimate, not an exact scan
    scan_mode: str = "search"    # "search" (fast probe + cursor delete loop) | "walk" (history)
    estimated_seconds: float = 0.0
    # Where the wall clock actually goes. Discord hides delete throttling (discord.py sleeps
    # it off inside the call and logs nothing), so t_delete vs t_pace is the only way to see
    # whether the purge is rate limited or just self-pacing.
    t_search: float = 0.0        # seconds inside search requests
    t_delete: float = 0.0        # seconds inside delete API calls (includes absorbed 429 sleeps)
    t_pace: float = 0.0          # seconds deliberately waited between old-message deletes
    t_wait: float = 0.0          # seconds waiting on page settle / index-building / rate limits
    delete_started_mono: float = 0.0
    # Updated on every successful deletion. The status endpoint derives "seconds since
    # progress" from it, so a stall is visible even while blocked inside a delete
    # call that cannot update any activity string (Discord's penalties run 2+ minutes).
    last_progress_mono: float = 0.0
    rate_limit_hits: int = 0
    penalty_seconds: float = 0.0   # wall clock spent in Discord-imposed waits; the cost of going too fast
    bucket_waits: int = 0          # long calls with no 429: Discord's own refill windows
    bucket_wait_seconds: float = 0.0
    deleter: object = None         # the running ChannelDeleter, for per-channel pace stats
    unarchived: dict = field(default_factory=dict)  # thread id -> unarchive outcome (once per job)
    deleted_threads: set = field(default_factory=set)  # removed wholesale; skip their queued msgs
    threads_deleted: int = 0
    skipped_in_threads: int = 0
    current_activity: str = ""
    # The GUILD-WIDE aggregate limiter. Every channel worker passes through this in addition
    # to its own per-channel gate, and it is what note_rate_limited() tunes, so 429 feedback
    # reaches something the workers actually consult.
    pacer: AdaptivePacer = field(default_factory=lambda: AdaptivePacer(
        base=GLOBAL_PACE_BASE, minimum=GLOBAL_PACE_MIN, maximum=GLOBAL_PACE_MAX,
        # Re-approach the account-wide ceiling very gently: diving back after a backoff
        # produces oscillating 429 churn.
        narrow=0.97, clean_streak=15))
    gate: object = None
    created_at: object = None
    confirmed_at: object = None
    finished_at: object = None
    preview_expires_at: object = None
    cancel_requested: bool = False
    error: Optional[str] = None

    def __post_init__(self):
        # One gate per job, sharing this job's pacer across every channel it deletes from.
        if self.gate is None:
            self.gate = DeleteGate(self.pacer)

    def note_rate_limited(self, retry_after: float = 0.0) -> None:
        """A confirmed 429 (from discord.py's logger). Back the shared pace off immediately,
        scaled by the retry_after Discord returned."""
        self.rate_limit_hits += 1
        self.penalty_seconds += max(retry_after, 0.0)
        self.pacer.penalize(retry_after)

    def note_progress(self) -> None:
        self.last_progress_mono = time.monotonic()

    @property
    def target_user_id(self) -> Optional[int]:
        return self.filters.target_user_id

    @property
    def directed_at_user_id(self) -> Optional[int]:
        return self.filters.directed_at_user_id

    def progress_stats(self) -> dict:
        """Live throughput derived from the delete phase: rate, ETA and where time went.
        Uses a monotonic start so it is unaffected by clock changes."""
        elapsed = (time.monotonic() - self.delete_started_mono) if self.delete_started_mono else 0.0
        done = self.deleted + self.failed
        rate = (done / elapsed) if (elapsed > 1 and done) else 0.0
        remaining = max(0, (self.server_total or 0) - done)
        # Derived live, so a freeze shows up even while blocked inside a delete call
        # that can't update anything itself.
        stalled = (time.monotonic() - self.last_progress_mono) if self.last_progress_mono else 0.0
        return {
            "elapsed_seconds": round(elapsed, 1),
            "rate_per_sec": round(rate, 2),
            "seconds_per_message": round(1 / rate, 2) if rate else 0.0,
            "eta_seconds": round(remaining / rate) if rate else 0,
            "seconds_since_progress": round(stalled, 1),
            "rate_limit_hits": self.rate_limit_hits,
            "penalty_seconds": round(self.penalty_seconds, 1),
            "bucket_waits": self.bucket_waits,
            "bucket_wait_seconds": round(self.bucket_wait_seconds, 1),
            "delete_channels": getattr(self.deleter, "active_channels", 0),
            "pending_deletes": getattr(self.deleter, "pending", 0),
            "threads_deleted": self.threads_deleted,
            "skipped_in_threads": self.skipped_in_threads,
            "timing": {
                "search": round(self.t_search, 1),
                "delete": round(self.t_delete, 1),
                "pacing": round(self.t_pace, 1),
                "waiting": round(self.t_wait, 1),
            },
            "pacer": (self.deleter.aggregate_stats() if self.deleter is not None
                      else self.pacer.stats()),
        }

    def to_status_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "current_activity": self.current_activity,
            **self.progress_stats(),
            "target_user_id": self.target_user_id,
            "directed_at_user_id": self.directed_at_user_id,
            "scope": "guild" if self.scope == "guild" else str(self.scope),
            "guild_id": self.guild_id,
            "requested_by": self.requested_by,
            "requested_via": self.requested_via,
            "channels_scanned": self.channels_scanned,
            "excluded_count": len(self.filters.excluded_channel_ids),
            "messages_scanned": self.messages_scanned,
            "matched": self.matched,
            "deleted": self.deleted,
            "failed": self.failed,
            "server_total": self.server_total,
            "is_estimate": self.is_estimate,
            "current_channel": self.current_channel,
            "preview": list(self.preview),
            "recent_deletions": list(self.recent_deletions),
            "estimated_seconds": self.estimated_seconds,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "preview_expires_at": (
                self.preview_expires_at.isoformat() if self.preview_expires_at else None
            ),
        }


class PurgeJobManager:
    """Owns the single active purge job and drives scan/delete.

    The discord-driven scan/delete are injectable so the state machine is testable
    without discord. Defaults are the real `_discord_scan` / `_discord_delete`.
    """

    def __init__(self, bot=None, scan_impl=None, delete_impl=None, clock=None,
                 preview_ttl: timedelta = timedelta(minutes=15)):
        self.bot = bot
        self._scan_impl = scan_impl or self._discord_scan
        self._delete_impl = delete_impl or self._discord_delete
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.preview_ttl = preview_ttl
        self.active_job: Optional[PurgeJob] = None
        self.last_job: Optional[PurgeJob] = None
        self._counter = 0

    def _now(self):
        return self._clock()

    def _retire(self):
        if self.active_job and self.active_job.status in TERMINAL_STATUSES:
            self.last_job = self.active_job
            self.active_job = None

    def _expire_active_if_needed(self):
        job = self.active_job
        if (job and job.status == PurgeStatus.AWAITING and job.preview_expires_at
                and self._now() > job.preview_expires_at):
            job.status = PurgeStatus.EXPIRED
            job.finished_at = self._now()
            self._retire()

    def is_busy(self) -> bool:
        self._expire_active_if_needed()
        return self.active_job is not None and self.active_job.status in BLOCKING_STATUSES

    async def run_dry_run(self, scope, guild_id, filters: PurgeFilters,
                          requested_by, requested_via) -> PurgeJob:
        if not purge_is_bounded(scope, filters):
            raise ValueError(
                "Refusing an unbounded purge: a server-wide purge needs at least one "
                "filter (author, directed-at user, date, or content). Limit it to a "
                "channel or add a filter."
            )
        if self.is_busy():
            raise PurgeBusyError("A purge job is already running.")
        self._retire()
        self._counter += 1
        job = PurgeJob(
            id=f"purge-{self._counter}",
            requested_by=requested_by,
            requested_via=requested_via,
            scope=scope,
            guild_id=guild_id,
            filters=filters,
            status=PurgeStatus.SCANNING,
            created_at=self._now(),
        )
        self.active_job = job
        logger.info("Purge %s: scanning (target=%s scope=%s by=%s)",
                    job.id, job.target_user_id, job.scope, requested_by)
        try:
            await self._scan_impl(job)
        except Exception as e:
            job.status = PurgeStatus.FAILED
            job.error = str(e)
            job.finished_at = self._now()
            self._retire()
            logger.error("Purge %s scan failed: %s", job.id, e)
            raise
        if job.cancel_requested:  # cancelled mid-scan: don't present a stale preview
            job.status = PurgeStatus.CANCELLED
            job.finished_at = self._now()
            self._retire()
            logger.info("Purge %s: cancelled during scan", job.id)
            return job
        if job.scan_mode == "walk":
            recent, old = partition_by_age(job.matches, self._now())
            # Old messages delete one-by-one at the paced rate; recent go out in fast bulk chunks.
            job.estimated_seconds = round(
                len(old) * (DELETE_PACE_BASE + 0.25) + len(recent) * 0.02, 1)
        else:
            # Search probe: the estimated total is known but not each message's age yet, so
            # ballpark it. Recent messages bulk-delete fast; only >14-day ones hit the paced
            # rate. This is a rough upper-ish figure, shown with a "~".
            job.estimated_seconds = round(job.matched * DELETE_PACE_BASE, 1)
        job.status = PurgeStatus.AWAITING
        job.preview_expires_at = self._now() + self.preview_ttl
        logger.info("Purge %s: %s matches, awaiting confirmation", job.id, job.matched)
        return job

    async def run_delete(self, job_id) -> PurgeJob:
        self._expire_active_if_needed()
        job = self.active_job
        if not job or job.id != job_id:
            raise PurgeStateError("No matching pending purge to confirm.")
        if job.status != PurgeStatus.AWAITING:
            raise PurgeStateError(f"Job is {job.status}, not awaiting confirmation.")
        job.status = PurgeStatus.DELETING
        job.confirmed_at = self._now()
        job.delete_started_mono = time.monotonic()   # baseline for live rate + ETA
        job.last_progress_mono = job.delete_started_mono
        logger.info("Purge %s: deleting %s messages", job.id, job.matched)
        try:
            # Watch discord.py's logger for the whole delete phase: it absorbs 429s silently
            # and reports them nowhere else, so this is what keeps the pacer honest.
            with RateLimitWatcher(job):
                await self._delete_impl(job)
        except Exception as e:
            job.status = PurgeStatus.FAILED
            job.error = str(e)
            job.finished_at = self._now()
            self._retire()
            logger.error("Purge %s delete failed: %s", job.id, e)
            raise
        job.status = PurgeStatus.CANCELLED if job.cancel_requested else PurgeStatus.COMPLETED
        job.finished_at = self._now()
        self._retire()
        logger.info("Purge %s: %s (deleted=%s failed=%s)",
                    job.id, job.status, job.deleted, job.failed)
        return job

    def cancel(self, job_id) -> bool:
        """Cooperatively stop a running scan/delete; honored at the next checkpoint."""
        job = self.active_job
        if job and job.id == job_id and job.status in BLOCKING_STATUSES:
            job.cancel_requested = True
            return True
        return False

    def can_confirm(self, job_id) -> bool:
        """True if `job_id` is the active job and still awaiting confirmation."""
        self._expire_active_if_needed()
        job = self.active_job
        return bool(job and job.id == job_id and job.status == PurgeStatus.AWAITING)

    def abandon(self, job_id) -> bool:
        """Drop an unconfirmed preview without deleting anything, freeing the slot."""
        self._expire_active_if_needed()
        job = self.active_job
        if job and job.id == job_id and job.status == PurgeStatus.AWAITING:
            job.status = PurgeStatus.CANCELLED
            job.finished_at = self._now()
            self._retire()
            return True
        return False

    def get_status(self) -> dict:
        self._expire_active_if_needed()
        job = self.active_job or self.last_job
        if job is None:
            return {"status": PurgeStatus.IDLE}
        return job.to_status_dict()

    # ------------------------------------------------------------------
    # Discord-driven scan/delete. Imported lazily; verified live, not in unit tests.
    # ------------------------------------------------------------------

    @staticmethod
    def _search_eligible(filters: PurgeFilters) -> bool:
        """Whether the fast guild-search scan can serve these filters.

        `contains` is served server-side via the search `content` term, so it stays
        fast. `regex` (no server-side equivalent) and reply-based `directed_at` (the
        `mentions` param misses silent replies) route to the unbounded history walk:
        served client-side over a superset, the ~5000 search offset cap would silently
        miss matches beyond the newest ~5000 in scope. Everything else scopes
        server-side, so hits are real matches and the cap + re-run model holds.
        """
        return not (filters.regex or filters.directed_at_user_id is not None)

    async def _discord_scan(self, job: PurgeJob):
        """Probe phase. For search-eligible filters this is a FAST estimate, not a full
        scan: one search page per channel batch gives total_results plus a sample preview,
        so the confirm gate appears in seconds. The real work happens in run_delete, which
        deletes each page and re-searches the freed index (delete-as-you-go). regex and
        reply-based directed-at can't be expressed server-side, so they still walk history
        to collect an exact match set up front."""
        guild = self.bot.get_guild(job.guild_id) if self.bot else None
        if guild is None:
            raise RuntimeError(f"Guild {job.guild_id} is not available to the bot.")
        if self._search_eligible(job.filters):
            try:
                await self._search_probe(guild, job)
                job.scan_mode = "search"
                job.is_estimate = True
                job.current_channel = None
                return
            except SearchUnavailable as e:
                logger.warning("Purge %s: search unavailable (%s); falling back to history walk",
                               job.id, e)
                # Reset partial search progress so the walk starts clean.
                job.matches.clear()
                job.preview.clear()
                job.matched = 0
                job.messages_scanned = 0
                job.channels_scanned = 0
                job.server_total = 0
        job.scan_mode = "walk"
        job.is_estimate = False
        await self._walk_scan(guild, job)
        job.current_channel = None

    def _search_http(self, guild):
        """(session, url, headers) for guild-search requests. Raises SearchUnavailable
        if the bot HTTP token isn't available. Caller must `async with session`."""
        import aiohttp  # noqa: F401

        token = getattr(getattr(self.bot, "http", None), "token", None)
        if not token:
            raise SearchUnavailable("bot HTTP token not available")
        headers = {"Authorization": f"Bot {token}",
                   "User-Agent": "DiscordBot (muramasa purge, 1.0)"}
        url = f"https://discord.com/api/v10/guilds/{guild.id}/messages/search"
        return aiohttp.ClientSession(), url, headers

    async def _search_probe(self, guild, job: PurgeJob):
        """Fast estimate for the confirm gate: one search page per channel batch. Sums
        total_results into job.server_total (the ~count) and fills a sample preview. Does
        NOT walk the whole result set. Raises SearchUnavailable to trigger the walk
        fallback. One search per batch already covers each channel's threads + forum
        posts server-side, so excluded channels are never touched."""
        batches = await self._search_channel_batches(guild, job)
        job.channels_scanned = sum(len(b) for b in batches if b)
        session, url, headers = self._search_http(guild)
        estimate = 0
        async with session:
            for channel_ids in batches:
                if job.cancel_requested:
                    break
                params = build_search_params(job.filters, channel_ids, 0)
                data = await self._search_page(session, url, headers, params, job)
                estimate += int(data.get("total_results") or 0)
                if len(job.preview) < PREVIEW_LIMIT:
                    self._collect_preview(job, extract_hits(data), guild)
        job.server_total = estimate
        job.matched = estimate  # displayed as "~estimate" (job.is_estimate is True)

    @staticmethod
    def _collect_preview(job: PurgeJob, hits, guild):
        """Add up to PREVIEW_LIMIT deletable sample hits to the confirm preview."""
        for msg in hits:
            if len(job.preview) >= PREVIEW_LIMIT:
                return
            to_del, _ = plan_search_page([msg], job.filters)
            if not to_del:
                continue
            ch = guild.get_channel_or_thread(int(msg["channel_id"]))
            job.preview.append({
                "channel": getattr(ch, "name", None) or str(msg.get("channel_id")),
                "timestamp": msg.get("timestamp"),
                "snippet": search_snippet(msg),
            })

    async def _search_delete(self, guild, job: PurgeJob):
        """Delete-as-you-go over the search index, one channel batch at a time. Each batch
        pages by message id (see _search_delete_batch): delete a page, then search for the
        next messages OLDER than the oldest seen so far. Raises SearchUnavailable if the
        endpoint stops mid-run (deletions so far stand; a re-run re-probes and continues)."""
        batches = await self._search_channel_batches(guild, job)
        session, url, headers = self._search_http(guild)
        now = self._now()
        async with session:
            # Producer/consumer: the cursor keeps paging while per-channel workers delete, so
            # a channel waiting out its quota does not stall the whole purge.
            deleter = ChannelDeleter(self, guild, job, now, session, headers)
            job.deleter = deleter
            try:
                for channel_ids in batches:
                    if job.cancel_requested:
                        break
                    await self._search_delete_batch(session, url, headers, channel_ids,
                                                    guild, job, now, deleter)
            finally:
                job.current_activity = "finishing queued deletions"
                await deleter.close()

    async def _search_delete_batch(self, session, url, headers, channel_ids, guild, job, now,
                                   deleter=None):
        """One channel batch's id-cursor delete loop.

        `cursor` is the boundary snowflake to page past: None on the first page, then the
        oldest id seen (newest-first) or newest id seen (oldest-first). Each iteration:
        search id-beyond-cursor, delete that page's matches, advance the cursor past the
        WHOLE page (deletable and skipped alike), repeat. Because the cursor only moves to
        messages not yet reached, the just-deleted (newer) ones are excluded by id no
        matter what the search index is doing, so post-delete index lag can't false-stop
        the loop.

        A page that returns no hits, no parseable ids, or fails to advance the cursor is
        treated as UNPRODUCTIVE: it may be the true end of the range, or a transient search
        under-return (cold/long-unaccessed messages, index incompleteness). The SAME cursor
        is re-issued up to SEARCH_EMPTY_PAGE_RETRIES times before concluding the batch is
        drained. Re-querying the same cursor can never re-read the deletion zone or
        double-delete, so the retry is free of the hazard an offset loop would have.
        """
        filters = job.filters
        cursor = None
        stale = 0
        while not job.cancel_requested:
            params = build_cursor_params(filters, channel_ids, cursor)
            data = await self._search_page(session, url, headers, params, job)
            # server_total stays anchored at the probe's reading. total_results is the most
            # lag-corrupted field there is: it reads 0 right after deletes, over-reports for
            # hours via phantom entries for recently-deleted messages, and drifts page to
            # page. Mid-run readings therefore neither lower it (that would end the loop
            # early) nor raise it (ratcheting the worst over-report would inflate the
            # estimate). It is display only and NEVER decides the stop.
            hits = extract_hits(data)
            ids = hit_ids(hits)
            advanced = bool(ids) and cursor_progressed(cursor, next_cursor(ids, filters.oldest_first), filters.oldest_first)
            if not advanced:
                if job.cancel_requested:
                    break
                # Empty / all-malformed / non-advancing page: retry the same cursor a few
                # times (the range may just be transiently cold) before calling it drained.
                if stale < SEARCH_EMPTY_PAGE_RETRIES:
                    stale += 1
                    backoff = SEARCH_PAGE_DELAY * stale  # mild backoff to let it warm
                    job.current_activity = (f"empty page, re-checking in {backoff:.0f}s "
                                            f"({stale}/{SEARCH_EMPTY_PAGE_RETRIES})")
                    job.t_wait += backoff
                    await sleep_unless_cancelled(job, backoff)
                    continue
                break
            stale = 0
            to_delete, _skipped = plan_search_page(hits, filters)
            job.messages_scanned += len(hits)
            if to_delete:
                matches = [self._to_matched(m) for m in to_delete]
                job.matched += len(matches)
                if deleter is not None:
                    # Hand off and keep paging; the channel workers delete in parallel.
                    await deleter.submit(matches)
                else:
                    await self._delete_channel_group(guild, matches, job, now)
            # Advance past the whole page (deletable + skipped): the next search asks for
            # ids strictly beyond this, so undeletable messages are paged over, never re-read.
            cursor = next_cursor(ids, filters.oldest_first)
            job.t_wait += SEARCH_PAGE_DELAY
            await sleep_unless_cancelled(job, SEARCH_PAGE_DELAY)

    @staticmethod
    def _to_matched(msg: dict) -> MatchedMessage:
        """MatchedMessage from a validated search hit (already passed plan_search_page).
        An unparseable timestamp falls back to the epoch so the message takes the safe
        paced one-by-one delete path instead of being wrongly bulk-deleted."""
        try:
            created = datetime.fromisoformat(msg["timestamp"])
        except (KeyError, ValueError, TypeError):
            created = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return MatchedMessage(int(msg["channel_id"]), int(msg["id"]), created, search_snippet(msg))

    async def _delete_channel_group(self, guild, matches, job, now):
        """Delete a set of matches grouped by channel, each channel paced independently.
        Used by the walk path and wherever no ChannelDeleter is running."""
        by_channel: dict = {}
        for m in matches:
            by_channel.setdefault(m.channel_id, []).append(m)
        gates = {cid: DeleteGate(AdaptivePacer()) for cid in by_channel}
        slots = asyncio.Semaphore(DELETE_CHANNEL_WORKERS)

        async def _one(cid, group):
            async with slots:
                await self._delete_channel(guild, cid, group, job, now, gates[cid])

        await asyncio.gather(*(_one(cid, g) for cid, g in by_channel.items()),
                             return_exceptions=True)

    async def _raw_delete_message(self, session, headers, channel_id, message_id, job):
        """Delete one message over raw HTTP: send at a set pace, honor a real 429's
        retry_after exactly, and never pre-emptively sleep out the bucket Discord
        advertises (~40/120s for bot old-message deletes; routing through discord.py
        honors it and freezes ~118s per ~42 deletes, while real enforcement is only
        small sub-4s 429s even far above this pace).

        Logs the X-RateLimit headers whenever the advertised budget runs out or a 429 lands,
        so the advertised-vs-enforced gap stays observable in the container log.

        Returns "deleted" | "gone" | "forbidden" | "cancelled" | "failed".
        """
        import aiohttp  # noqa: F401

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
        for _ in range(RAW_DELETE_MAX_RETRIES):
            if job.cancel_requested:
                return "cancelled"
            try:
                async with session.delete(
                        url, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=RAW_DELETE_TIMEOUT)) as resp:
                    if resp.headers.get("X-RateLimit-Remaining") == "0":
                        logger.info(
                            "Purge %s: advertised delete budget exhausted (limit=%s reset_after=%ss)"
                            ", continuing reactively",
                            job.id, resp.headers.get("X-RateLimit-Limit"),
                            resp.headers.get("X-RateLimit-Reset-After"))
                    if resp.status in (200, 204):
                        return "deleted"
                    if resp.status == 404:
                        return "gone"          # already deleted; not a failure
                    if resp.status == 403:
                        return "forbidden"
                    if resp.status == 429:
                        body = await self._safe_json(resp)
                        retry_after = float(body.get("retry_after") or 1.0)
                        job.note_rate_limited(retry_after)
                        job.current_activity = f"rate limited, waiting {retry_after:.1f}s"
                        logger.info("Purge %s: 429 on delete in %s (retry_after=%.1fs, "
                                    "global=%s scope=%s); pace now %.1fs",
                                    job.id, channel_id, retry_after,
                                    resp.headers.get("X-RateLimit-Global"),
                                    resp.headers.get("X-RateLimit-Scope"),
                                    job.pacer.interval)
                        job.t_wait += retry_after
                        await sleep_unless_cancelled(job, retry_after)
                        continue
                    if resp.status == 400:
                        body = await self._safe_json(resp)
                        # 50083 = the message lives in an ARCHIVED thread; deletes are
                        # rejected until it is unarchived. Unarchive once and retry; if the
                        # thread cannot be unarchived, remember that so the rest of its
                        # queued messages fail fast instead of burning a pace slot each.
                        if body.get("code") == 50083:
                            if await self._ensure_unarchived(session, headers, channel_id, job):
                                continue
                            return "failed"
                        logger.warning("Purge %s: delete %s got HTTP 400: %s",
                                       job.id, message_id, str(body)[:120])
                        return "failed"
                    if resp.status >= 500:
                        await sleep_unless_cancelled(job, 1.0)
                        continue
                    text = (await resp.text())[:120]
                    logger.warning("Purge %s: delete %s got HTTP %s: %s",
                                   job.id, message_id, resp.status, text)
                    return "failed"
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.info("Purge %s: delete request error (%s); retrying", job.id, e)
                await sleep_unless_cancelled(job, 1.0)
        return "failed"

    async def _ensure_unarchived(self, session, headers, channel_id, job) -> bool:
        """Unarchive a thread so its messages can be deleted, once per thread per job.
        Returns True if the thread is (now) active; False if it can't be unarchived (e.g.
        a locked thread without permission), in which case the caller fails fast."""
        import aiohttp  # noqa: F401

        cached = job.unarchived.get(channel_id)
        if cached is not None:
            return cached
        ok = False
        try:
            async with session.patch(
                    f"https://discord.com/api/v10/channels/{channel_id}",
                    json={"archived": False},
                    headers={**headers, "Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=RAW_DELETE_TIMEOUT)) as resp:
                ok = resp.status == 200
                if ok:
                    logger.info("Purge %s: unarchived thread %s to delete inside it",
                                job.id, channel_id)
                else:
                    logger.warning("Purge %s: cannot unarchive thread %s (HTTP %s); "
                                   "skipping its messages", job.id, channel_id, resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("Purge %s: unarchive of %s failed (%s)", job.id, channel_id, e)
        job.unarchived[channel_id] = ok
        return ok

    async def _search_page(self, session, url, headers, params, job: PurgeJob) -> dict:
        """One search request with retry on 202 (index building) and 429 (rate limit).
        403/other statuses raise SearchUnavailable to trigger the walk fallback."""
        import aiohttp  # noqa: F401

        for _ in range(SEARCH_MAX_RETRIES):
            if job.cancel_requested:
                return {"messages": [], "total_results": job.server_total}
            try:
                started = time.monotonic()
                async with session.get(url, params=params, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        job.t_search += time.monotonic() - started
                        return data
                    if resp.status in (202, 429):
                        job.t_search += time.monotonic() - started
                        body = await self._safe_json(resp)
                        wait_s = min(max(float(body.get("retry_after") or 1.0), 0.5), 30.0)
                        if resp.status == 429:
                            job.current_activity = f"search rate limited, waiting {wait_s:.0f}s"
                            logger.warning("Purge %s: search rate limited, waiting %.1fs",
                                           job.id, wait_s)
                        else:
                            job.current_activity = f"Discord indexing, waiting {wait_s:.0f}s"
                            logger.info("Purge %s: guild search index building, waiting %.1fs",
                                        job.id, wait_s)
                        job.t_wait += wait_s
                        await sleep_unless_cancelled(job, wait_s)
                        continue
                    if resp.status == 403:
                        raise SearchUnavailable("guild search returned 403")
                    text = (await resp.text())[:200]
                    raise SearchUnavailable(f"guild search HTTP {resp.status}: {text}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # Transient network blip or per-request timeout: retry a few times
                # before degrading to a full history walk (which discards collected
                # matches). asyncio.TimeoutError is not an aiohttp.ClientError, so it
                # must be caught explicitly or a hung request would fail the whole job.
                logger.info("Purge %s: search request error (%s); retrying", job.id, e)
                await sleep_unless_cancelled(job, 1.0)
        raise SearchUnavailable("guild search exhausted retries (network, indexing, or rate limited)")

    @staticmethod
    async def _safe_json(resp) -> dict:
        try:
            return await resp.json()
        except Exception:
            return {}

    async def _search_channel_batches(self, guild, job: PurgeJob):
        """Channel-id batches to scope the search. A channel_id search already covers that
        channel's own threads and forum posts server-side, so threads are never enumerated
        (enumerating them makes a thread-heavy guild grind to a halt). Guild scope with
        no exclusions searches the whole guild (a single None batch); with exclusions it scopes
        to every non-category channel MINUS the excluded ones, so excluding a channel also
        excludes all of its threads. Chunked to Discord's 500-channel-per-search cap."""
        import discord  # noqa: F401

        excluded = job.filters.excluded_channel_ids
        if job.scope != "guild":
            cid = int(job.scope)
            return [] if cid in excluded else [[cid]]
        if not excluded:
            return [None]  # whole-guild search; include_nsfw controls NSFW channels
        ids = [c.id for c in guild.channels
               if not isinstance(c, discord.CategoryChannel) and c.id not in excluded]
        return [ids[i:i + 500] for i in range(0, len(ids), 500)]

    async def _walk_scan(self, guild, job: PurgeJob):
        """Fallback scan: walk channel history when search is unavailable."""
        channels = await self._resolve_channels(guild, job)
        if not channels:
            return
        # Scan channels/threads concurrently (separate rate-limit buckets). A single
        # channel's own history is read sequentially: discord.py serializes requests
        # per channel bucket, so splitting one channel into parallel reads would not
        # help. The win is across channels (whole-server, channels-with-threads).
        sem = asyncio.Semaphore(SCAN_CONCURRENCY)
        await asyncio.gather(*(self._scan_channel(ch, job, sem) for ch in channels))

    async def _scan_channel(self, channel, job, sem):
        import discord  # noqa: F401

        filters = job.filters
        async with sem:
            if not job.cancel_requested:
                job.current_channel = _channel_label(channel)
                try:
                    async for message in channel.history(limit=None, after=filters.after,
                                                         before=filters.before):
                        if job.cancel_requested:
                            break
                        job.messages_scanned += 1
                        if not _is_deletable_type(message):
                            continue
                        if not message_matches(message, filters):
                            continue
                        job.matches.append(MatchedMessage(
                            channel.id, message.id, message.created_at, message_snippet(message)))
                        job.matched += 1
                        if len(job.preview) < PREVIEW_LIMIT:
                            job.preview.append(_preview_entry(message, channel))
                except discord.Forbidden:
                    logger.info("Purge %s: no access to %s, skipping",
                                job.id, _channel_label(channel))
                except discord.HTTPException as e:
                    logger.warning("Purge %s: history read failed for %s: %s",
                                   job.id, _channel_label(channel), e)
        job.channels_scanned += 1

    async def _discord_delete(self, job: PurgeJob):
        guild = self.bot.get_guild(job.guild_id) if self.bot else None
        if guild is None:
            raise RuntimeError(f"Guild {job.guild_id} is not available to the bot.")
        if job.scan_mode == "search":
            # The probe only estimated the total; now find and delete the real matches
            # page by page, counting matched/deleted along the way (no pre-collected list).
            job.matched = 0
            job.is_estimate = False
            await self._search_delete(guild, job)
            job.current_channel = None
            return
        # Walk path: matches were collected exhaustively during the scan; delete them.
        # Channels delete concurrently, each paced by its own per-channel quota.
        await self._delete_channel_group(guild, job.matches, job, self._now())
        job.current_channel = None

    async def _delete_channel(self, guild, channel_id, matches, job, now, gate=None,
                              session=None, headers=None):
        """Delete a batch of one channel's matches. `gate` paces THIS channel only (the
        quota is per channel); `session`/`headers` enable the raw-HTTP delete path for
        >14-day messages. Without a session it falls back to discord.py deletes."""
        import discord  # noqa: F401

        if job.cancel_requested:
            return
        if channel_id in job.deleted_threads:
            # Thread already removed wholesale; its remaining queued messages are gone too.
            job.skipped_in_threads += len(matches)
            return
        channel = guild.get_channel_or_thread(channel_id) or self.bot.get_channel(channel_id)
        if channel is None:
            # Search can surface messages in channels/threads not in cache (e.g.
            # archived threads); resolve those with a fetch before giving up.
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                channel = None
        label = _channel_label(channel) if channel is not None else str(channel_id)
        job.current_channel = label
        if channel is not None and isinstance(channel, discord.Thread):
            if await self._apply_thread_policy(channel, channel_id, label, matches, job):
                return
        for kind, payload in build_delete_plan(matches, now, job.filters.oldest_first):
            if job.cancel_requested:
                break
            if kind == "single" or len(payload) == 1:
                items = payload if kind == "bulk" else [payload]
                await self._delete_individually(channel, channel_id, label, items, job, gate, session, headers)
                continue
            if channel is None:      # bulk needs the channel object; fall back per-message
                await self._delete_individually(channel, channel_id, label, payload, job, gate, session, headers)
                continue
            partials = [channel.get_partial_message(m.message_id) for m in payload]
            try:
                job.current_activity = f"bulk deleting {len(partials)} in {label}"
                started = time.monotonic()
                await channel.delete_messages(partials)
                job.t_delete += time.monotonic() - started
                base = job.deleted
                job.deleted += len(partials)
                for k, m in enumerate(payload):
                    self._log_deletion(job, label, m, base + k + 1)
            except (discord.NotFound, discord.HTTPException) as e:
                logger.warning("Purge %s: bulk delete fell back to individual: %s", job.id, e)
                await self._delete_individually(channel, channel_id, label, payload, job, gate, session, headers)

    @staticmethod
    def _log_deletion(job: PurgeJob, channel_label, mm, index):
        job.note_progress()
        # `index` is the running 1-based deletion count, shown as [n/~total] in the console.
        record_capped(job.recent_deletions, {
            "index": index,
            "channel": channel_label,
            "timestamp": mm.created_at.isoformat() if mm.created_at else None,
            "snippet": mm.snippet,
            "id": str(mm.message_id),
        }, DELETION_LOG_SIZE)

    async def _apply_thread_policy(self, channel, channel_id, label, matches, job) -> bool:
        """Decide how a PRIVATE thread's matches are handled. Returns True when this batch is
        fully dealt with here (skipped, or removed along with the thread) and no per-message
        deleting should follow.

        Only private threads are eligible: they are searchable solely by their members, so
        their contents are invisible to the rest of the server. Public threads are normal
        visible content and always fall through to per-message deleting. Removing the thread
        is one API call instead of thousands of paced deletes, but it also takes anyone
        else's messages inside it, which is why it only applies to threads the target created
        and can be switched off.
        """
        import discord  # noqa: F401

        filters = job.filters
        if channel.type is not discord.ChannelType.private_thread:
            return False                    # public thread: purge its messages as usual
        if filters.skip_private_threads:
            job.skipped_in_threads += len(matches)
            return True
        owns_it = (filters.delete_private_threads
                   and filters.target_user_id is not None
                   and channel.owner_id == filters.target_user_id)
        if not owns_it:
            return False
        try:
            await channel.delete()
        except discord.HTTPException as e:
            logger.warning("Purge %s: could not delete thread %s (%s); deleting its "
                           "messages individually instead", job.id, label, e)
            return False
        job.deleted_threads.add(channel_id)
        job.threads_deleted += 1
        job.deleted += len(matches)
        job.note_progress()
        self._log_deletion(job, label, MatchedMessage(
            channel_id, channel_id, None,
            f"[removed the whole private thread: {len(matches)}+ messages in one call]"),
            job.deleted)
        logger.info("Purge %s: deleted private thread %s (created by the target) instead of "
                    "paging through its messages", job.id, label)
        return True

    async def _delete_individually(self, channel, channel_id, label, matches, job: PurgeJob,
                                   gate=None, session=None, headers=None):
        """One-by-one deletes for this channel, paced by ITS own gate plus the guild-wide
        aggregate gate (the per-channel gate alone cannot stop N channels multiplying into a
        rate Discord rejects, and the global gate is what 429 feedback tunes).

        >14-day messages go over raw HTTP when a session is supplied: routing them through
        discord.py pre-emptively sleeps out the advertised bot bucket (~40 deletes per
        ~120s, seen as a ~118s freeze per ~42 deletes with zero 429s), while real
        enforcement is far looser. The raw path reacts to 429s and never pre-emptively
        waits. Recent messages are bulk-delete fallbacks, on discord.py.
        """
        import discord  # noqa: F401

        gate = gate or job.gate
        pacer = gate.pacer
        boundary = self._now() - BULK_DELETE_MAX_AGE
        for m in matches:
            if job.cancel_requested:
                break
            old = bool(m.created_at and m.created_at <= boundary)
            if old:
                job.current_activity = f"pacing {pacer.interval:.1f}s in {label}"
                await gate.wait_turn(job)
                if job.gate is not None and job.gate is not gate:
                    await job.gate.wait_turn(job)
                if job.cancel_requested:
                    break          # cancelled while waiting for a turn: don't spend the delete
            job.current_activity = f"deleting in {label}"
            started = time.monotonic()
            if old and session is not None:
                outcome = await self._raw_delete_message(
                    session, headers, channel_id, m.message_id, job)
                if outcome == "deleted":
                    job.deleted += 1
                    self._log_deletion(job, label, m, job.deleted)
                elif outcome in ("forbidden", "failed"):
                    job.failed += 1
                # "gone" = already deleted, "cancelled" = stopping; neither is a failure
            elif channel is None:
                job.failed += 1
                continue
            else:
                try:
                    await channel.get_partial_message(m.message_id).delete()
                    job.deleted += 1
                    self._log_deletion(job, label, m, job.deleted)
                except discord.NotFound:
                    pass  # already gone, not a failure
                except discord.Forbidden:
                    job.failed += 1
                except discord.HTTPException as e:
                    job.failed += 1
                    logger.warning("Purge %s: delete failed for %s: %s", job.id, m.message_id, e)
            call_seconds = time.monotonic() - started
            job.t_delete += call_seconds
            if old:
                # Records long calls without slowing down for them: a long call with no 429
                # is a quota refill window, which a slower pace cannot avoid (see observe).
                before = pacer.bucket_waits
                pacer.observe(call_seconds)
                if pacer.bucket_waits > before:
                    job.bucket_waits += 1
                    job.bucket_wait_seconds += call_seconds

    async def _resolve_channels(self, guild, job: PurgeJob) -> list:
        filters = job.filters
        excluded = filters.excluded_channel_ids
        if job.scope != "guild":
            ch = guild.get_channel_or_thread(int(job.scope))
            result = []
            if ch is not None:
                if _is_messageable(ch):
                    result.append(ch)
                result.extend(await self._all_threads(ch))
            return [c for c in result
                    if c.id not in excluded and _scannable(c, guild, filters)]

        result = []
        parents = (
            list(guild.text_channels)
            + list(getattr(guild, "voice_channels", []))
            + list(getattr(guild, "forums", []))
        )
        for parent in parents:
            if parent.id in excluded:
                continue  # excluded channel: skip it and all its threads
            if _is_messageable(parent) and _scannable(parent, guild, filters):
                result.append(parent)
            for thread in await self._all_threads(parent):
                if thread.id not in excluded and _scannable(thread, guild, filters):
                    result.append(thread)
        return result

    async def _all_threads(self, channel) -> list:
        import discord  # noqa: F401

        found = {}
        for thread in getattr(channel, "threads", []) or []:
            found[thread.id] = thread
        archived = getattr(channel, "archived_threads", None)
        if archived is None:
            return list(found.values())
        # Public archived threads: supported by text, voice, and forum channels.
        try:
            async for thread in archived(limit=None):
                found[thread.id] = thread
        except (discord.Forbidden, discord.HTTPException):
            pass
        # Private archived threads exist only on TextChannel; ForumChannel.archived_threads()
        # has no `private` kwarg, so only request them there.
        if isinstance(channel, discord.TextChannel):
            try:
                async for thread in archived(limit=None, private=True):
                    found[thread.id] = thread
            except (discord.Forbidden, discord.HTTPException):
                pass
        return list(found.values())


def _is_deletable_type(message) -> bool:
    import discord  # noqa: F401

    return message.type in (discord.MessageType.default, discord.MessageType.reply)


def _is_messageable(channel) -> bool:
    import discord  # noqa: F401

    return isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread))


def _scannable(channel, guild, filters: PurgeFilters) -> bool:
    if channel is None:
        return False
    is_nsfw = getattr(channel, "is_nsfw", None)
    if not filters.include_nsfw and callable(is_nsfw) and is_nsfw():
        return False
    perms = channel.permissions_for(guild.me)
    return perms.read_message_history


def _channel_label(channel) -> str:
    name = getattr(channel, "name", None)
    return f"#{name}" if name else str(getattr(channel, "id", "?"))


def _preview_entry(message, channel) -> dict:
    return {
        "channel": getattr(channel, "name", str(getattr(channel, "id", "?"))),
        "timestamp": message.created_at.isoformat(),
        "snippet": message_snippet(message),
    }
