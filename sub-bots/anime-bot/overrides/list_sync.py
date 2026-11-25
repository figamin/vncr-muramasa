"""
LJTA (Learn Japanese Through Anime) - shared engine for anime list sites (anime bot only)

Not a cog by itself: MalCog (mal.py) and AniListCog (anilist.py) both build on ListSync,
which does everything that's the same for both sites:
- Discord -> site: every /log_anime adds those episodes to that anime on the member's list.
- Site -> Discord: every few minutes, new episodes they mark as watched on the site are
  logged as normal anime logs and posted in the log channel, looking the same as /log_anime.
  At most DAILY_CAP_HOURS a day come in this way. When a member reaches the cap they're
  pinged once, and further updates that day are dropped (not logged or posted, ever).
- Undo works both ways, only for logs synced while linked: undoing such a log in Discord
  takes its episodes back off the site, and removing episodes on the site undoes (or
  shortens) the synced logs from the last UNDO_DAYS days. Logs from before linking never
  affect the other side.
A member can link only one of the sites at a time, so episodes are never imported twice.

Each site's cog fills in the site-specific parts: logging in, and reading/changing one
member's list (fetch_recent, get_entry, save_entry, delete_entry, ...).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
import discord
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SETTINGS (both MyAnimeList and AniList) - edit IDs, limits and messages here.
# In the messages, {service} is the site's name ("MyAnimeList" / "AniList") and
# {short} its short name ("MAL" / "AniList").
# ---------------------------------------------------------------------------
LOG_CHANNEL_ID = 1415741464545329272   # site -> Discord logs and cap notices go here
DAILY_CAP_HOURS = 5                   # max anime time per member per day (UTC) from a site
POLL_MINUTES = 10                     # how often linked lists are checked
FALLBACK_EPISODE_MINUTES = 24         # when the site doesn't know an episode's length
UNDO_DAYS = 7                         # removing episodes on the site undoes synced logs this recent

# Posted once when a member reaches the cap. {user} pings them; {hours} is the cap.
CAP_MESSAGE = (
    "{user} you've reached your daily {short} cap limit of {hours} hours. "
    "No more {service} updates will be logged for you today."
)
# Shown above each imported log so the channel shows whose log it is (no ping).
IMPORT_HEADER = "{user} logged on {service}"
# Posted (no ping) when episodes removed on the site undo or shorten synced logs; {changes} is one line per log.
REMOVAL_MESSAGE = "{user} removed episodes on {service}, so these logs changed:\n{changes}"
# When someone tries to link a second site.
ONE_SITE_MESSAGE = (
    "You've already linked {other}. Only one list site can be linked at a time, so unlink it "
    "first with `{unlink}`."
)

SITE_COGS = {"MalCog": "/mal unlink", "AniListCog": "/anilist unlink"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class ListSync:
    """Mixed into each site's cog (class XCog(ListSync, commands.Cog)). The cog's __init__
    must call ListSync.__init__(self, bot, db_path), and its init_db must call
    create_sync_tables. Entries passed between here and the site's code are dicts:
        {"id", "title", "watched", "status", "score", "rewatching", "minutes", "list_id"}
    """

    SERVICE = ""          # shown to members, e.g. "MyAnimeList"
    SHORT = ""            # e.g. "MAL"
    PREFIX = ""           # table names ({PREFIX}_snapshot, _daily, _sync) and log tag
    SOURCE = ""           # meta "source" of imported logs, and the log builder's source
    WATCHING = ""         # the site's "watching" status
    COMPLETED = ""        # the site's "completed" status

    def __init__(self, bot, db_path: str):
        self.bot = bot
        self.db_path = db_path
        self.lock = asyncio.Lock()   # one sync at a time, so pushes, undos and imports never cross
        self.push_tasks: Dict[str, asyncio.Task] = {}   # user_id -> their latest Discord -> site push

    @property
    def tag(self) -> str:
        return f"[{self.SHORT}]"

    def text(self, template: str, **kwargs) -> str:
        return template.format(service=self.SERVICE, short=self.SHORT, **kwargs)

    # ---------------- site-specific parts (each site's cog fills these in) ----------------

    def sync_ready(self, guild_id) -> bool:
        """Syncing works for logs made in this server."""
        raise NotImplementedError

    def app_ok(self) -> bool:
        """The bot's connection to the site works at all."""
        raise NotImplementedError

    async def poll_ready(self) -> bool:
        """Called before each check of the linked lists; False skips it."""
        return self.app_ok()

    def anime_id_of(self, meta: Dict[str, Any]) -> Optional[int]:
        """The site's id for the anime of a Discord log (from the log's meta)."""
        raise NotImplementedError

    async def get_link(self, user_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    async def all_links(self) -> List[Dict[str, Any]]:
        """Working links (not broken)."""
        raise NotImplementedError

    async def fetch_recent(self, link: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """The member's most recently updated list entries, newest first. None on failure."""
        raise NotImplementedError

    async def get_entry(self, link: Dict[str, Any], anime_id: int) -> Tuple[bool, Optional[Dict[str, Any]], int]:
        """(read ok?, their entry or None if not on their list, the anime's episode count or 0)."""
        raise NotImplementedError

    async def save_entry(self, link: Dict[str, Any], anime_id: int, watched: int, status: Optional[str]) -> bool:
        """Set episodes watched (and the status, unless None)."""
        raise NotImplementedError

    async def delete_entry(self, link: Dict[str, Any], anime_id: int, entry: Dict[str, Any]) -> bool:
        raise NotImplementedError

    async def import_name(self, anime_id: int, title: str) -> str:
        """What to give the anime log builder for an imported anime (ideally its AniList page)."""
        raise NotImplementedError

    # ---------------- one site per member ----------------

    async def other_site_message(self, user_id: str) -> Optional[str]:
        """If this member has linked another site, the message telling them to unlink it."""
        for cog_name, unlink in SITE_COGS.items():
            cog = self.bot.get_cog(cog_name)
            if cog is None or cog is self or not isinstance(cog, ListSync):
                continue
            if await cog.get_link(user_id):
                return ONE_SITE_MESSAGE.format(other=cog.SERVICE, unlink=unlink)
        return None

    # ---------------- storage ----------------

    async def create_sync_tables(self, db):
        p = self.PREFIX
        await db.executescript(f"""
            CREATE TABLE IF NOT EXISTS {p}_snapshot (
                user_id TEXT NOT NULL,
                anime_id INTEGER NOT NULL,
                episodes INTEGER NOT NULL,
                PRIMARY KEY (user_id, anime_id)
            );
            CREATE TABLE IF NOT EXISTS {p}_daily (
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                seconds INTEGER NOT NULL DEFAULT 0,
                capped INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            );
            -- Discord logs synced with the site while linked, so an undo on one side can be
            -- mirrored on the other. The log's episodes on the site are start_ep+1 .. end_ep.
            CREATE TABLE IF NOT EXISTS {p}_sync (
                activity_id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                anime_id INTEGER NOT NULL,
                start_ep INTEGER NOT NULL,
                end_ep INTEGER NOT NULL,
                direction TEXT NOT NULL,      -- 'to_list' (logged in Discord) or 'from_list'
                prev_status TEXT,             -- to_list: status before (NULL = wasn't on their list)
                created_at TEXT NOT NULL,
                undone INTEGER NOT NULL DEFAULT 0
            );
        """)

    async def forget_user(self, db, user_id: str):
        """On linking (a new starting point) or unlinking: drop their sync state."""
        await db.execute(f"DELETE FROM {self.PREFIX}_snapshot WHERE user_id = ?", (user_id,))
        await db.execute(f"DELETE FROM {self.PREFIX}_sync WHERE user_id = ?", (user_id,))

    async def get_snapshot(self, user_id: str) -> Dict[int, int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(f"SELECT anime_id, episodes FROM {self.PREFIX}_snapshot WHERE user_id = ?", (user_id,)) as cur:
                return {int(a): int(e) for a, e in await cur.fetchall()}

    async def set_snapshot(self, user_id: str, anime_id: int, episodes: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"INSERT INTO {self.PREFIX}_snapshot (user_id, anime_id, episodes) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, anime_id) DO UPDATE SET episodes = excluded.episodes",
                (user_id, anime_id, episodes),
            )
            await db.commit()

    async def get_daily(self, user_id: str, day: str) -> Tuple[int, bool]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(f"SELECT seconds, capped FROM {self.PREFIX}_daily WHERE user_id = ? AND day = ?", (user_id, day)) as cur:
                row = await cur.fetchone()
                return (int(row[0]), bool(row[1])) if row else (0, False)

    async def set_daily(self, user_id: str, day: str, seconds: int, capped: bool):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"INSERT INTO {self.PREFIX}_daily (user_id, day, seconds, capped) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, day) DO UPDATE SET seconds = excluded.seconds, capped = excluded.capped",
                (user_id, day, seconds, int(capped)),
            )
            await db.commit()

    async def add_sync(self, activity_id: int, user_id: str, anime_id: int, start: int, end: int,
                       direction: str, prev_status: Optional[str] = None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"INSERT OR REPLACE INTO {self.PREFIX}_sync (activity_id, user_id, anime_id, start_ep, end_ep, "
                "direction, prev_status, created_at, undone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (int(activity_id), user_id, int(anime_id), start, end, direction, prev_status, now_utc().isoformat()),
            )
            await db.commit()

    async def get_sync(self, activity_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(f"SELECT * FROM {self.PREFIX}_sync WHERE activity_id = ? AND undone = 0", (int(activity_id),)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def recent_syncs(self, user_id: str, anime_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Synced logs from the last UNDO_DAYS days that aren't undone, top episodes first."""
        cutoff = (now_utc() - timedelta(days=UNDO_DAYS)).isoformat()
        query = f"SELECT * FROM {self.PREFIX}_sync WHERE user_id = ? AND undone = 0 AND created_at >= ?"
        args: list = [user_id, cutoff]
        if anime_id is not None:
            query += " AND anime_id = ?"
            args.append(int(anime_id))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query + " ORDER BY end_ep DESC, activity_id DESC", args) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def _sync_db(self, sql: str, args: tuple):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(sql.replace("{sync}", f"{self.PREFIX}_sync"), args)
            await db.commit()

    # ---------------- Discord -> site ----------------

    @commands.Cog.listener()
    async def on_anime_activity_logged(self, activity, source: str):
        """After /log_anime: add the episodes to the member's list on the site."""
        if source != "command":
            return
        # Remembered so /log_anime's expiry notice can wait for this update to finish
        self.push_tasks[str(activity.user_id)] = asyncio.current_task()
        if not self.sync_ready(getattr(activity, "guild_id", None)):
            return
        link = await self.get_link(str(activity.user_id))
        if not link or link["broken"]:
            return
        meta = activity.meta or {}
        anime_id = self.anime_id_of(meta)
        episodes = int(meta.get("episodes") or 0)
        if not anime_id or episodes <= 0:
            return
        await self.push_to_list(link, int(anime_id), episodes, getattr(activity, "id", None),
                                complete=bool(meta.get("logged_as_complete")))

    async def push_to_list(self, link: Dict[str, Any], anime_id: int, episodes: int,
                           activity_id: Optional[int] = None, *, complete: bool = False):
        """Add `episodes` to this anime on the member's list (watching, or completed at the
        last episode; `complete` = logged with "complete", so straight to completed). Shows
        already completed (or being rewatched) there are left alone. Remembers which of the
        site's episodes the log added, so undoing it on either side can be mirrored."""
        user_id = link["user_id"]
        try:
            async with self.lock:
                ok, current, total = await self.get_entry(link, anime_id)
                if not ok:
                    logger.warning(f"{self.tag} Couldn't read anime {anime_id} for {user_id}")
                    return
                current = current or {}
                if current.get("status") == self.COMPLETED or current.get("rewatching"):
                    return   # already finished there: leave rewatches to the member
                before = int(current.get("watched") or 0)
                watched = before + episodes
                new_status = self.WATCHING
                if total and (complete or watched >= total):   # total 0 = unknown (still airing)
                    watched, new_status = total, self.COMPLETED
                if not await self.save_entry(link, anime_id, watched, new_status):
                    logger.warning(f"{self.tag} Couldn't update anime {anime_id} for {user_id}")
                    return
                # So the site -> Discord check doesn't log these episodes a second time. Only this
                # change is counted, so episodes marked on the site since the last check still get logged.
                snapshot = (await self.get_snapshot(user_id)).get(int(anime_id), 0)
                await self.set_snapshot(user_id, int(anime_id), snapshot + watched - before)
                if activity_id and watched > before:
                    await self.add_sync(activity_id, user_id, int(anime_id), before, watched,
                                        "to_list", current.get("status"))
        except Exception as e:
            logger.exception(f"{self.tag} Sync to {self.SERVICE} failed for {user_id}: {e}")

    # ---------------- undo, both ways ----------------

    @commands.Cog.listener()
    async def on_activity_undone(self, activity_id: int, user_id: str):
        """A log was undone in Discord (Undo button or /log_undo). If it was synced with the
        site while linked, take its episodes back off the member's list there too. Logs that
        were never synced (e.g. from before linking) leave the site alone."""
        try:
            async with self.lock:
                row = await self.get_sync(activity_id)
                if row is None:
                    return
                await self._sync_db("UPDATE {sync} SET undone = 1 WHERE activity_id = ?", (int(activity_id),))
                link = await self.get_link(row["user_id"])
                if not link or link["broken"] or not self.app_ok():
                    return
                await self.remove_from_list(link, row)
        except Exception as e:
            logger.exception(f"{self.tag} Undo on {self.SERVICE} failed for log {activity_id}: {e}")

    async def remove_from_list(self, link: Dict[str, Any], row: Dict[str, Any]):
        """Take one synced log's episodes back off the member's list (call inside the lock)."""
        user_id, anime_id = link["user_id"], int(row["anime_id"])
        count = int(row["end_ep"]) - int(row["start_ep"])
        if count <= 0:
            return
        ok, current, _ = await self.get_entry(link, anime_id)
        if not ok:
            logger.warning(f"{self.tag} Couldn't read anime {anime_id} for {user_id} to undo")
            return
        if current:   # (if it's no longer on their list, there's nothing to take off)
            watched = int(current.get("watched") or 0)
            new_watched = max(0, watched - count)
            if row["direction"] == "to_list" and row["prev_status"] is None and new_watched == 0 \
                    and not current.get("score"):
                # The log had added the anime to their list: take it off again
                ok = await self.delete_entry(link, anime_id, current)
            else:
                status = None
                if row["direction"] == "to_list" and row["prev_status"] and new_watched == int(row["start_ep"]):
                    status = row["prev_status"]   # back to how it was before the log
                elif current.get("status") == self.COMPLETED and new_watched < watched:
                    status = self.WATCHING
                ok = await self.save_entry(link, anime_id, new_watched, status)
            if not ok:
                logger.warning(f"{self.tag} Couldn't undo anime {anime_id} for {user_id}")
                return
            # Only our change, so the site -> Discord check doesn't see it as a removal there
            snapshot = (await self.get_snapshot(user_id)).get(anime_id, 0)
            await self.set_snapshot(user_id, anime_id, max(0, snapshot - (watched - new_watched)))
        # Synced logs above this one move down on their list
        await self._sync_db(
            "UPDATE {sync} SET start_ep = MAX(start_ep - ?, 0), end_ep = MAX(end_ep - ?, 0) "
            "WHERE user_id = ? AND anime_id = ? AND undone = 0 AND start_ep >= ?",
            (count, count, user_id, anime_id, int(row["end_ep"])),
        )

    async def undo_removed(self, link: Dict[str, Any], immersion, anime_id: int, watched: int) -> List[str]:
        """Episodes above `watched` were removed on the site: undo the synced logs they belonged
        to, or shorten a log that lost only some (call inside the lock). Logs from before
        linking, or synced more than UNDO_DAYS days ago, are left alone. Returns a line per log."""
        user_id, changes = link["user_id"], []
        for row in await self.recent_syncs(user_id, anime_id):
            if int(row["end_ep"]) <= watched:
                break   # top episodes first, so the rest are all still on their list
            activity = await immersion.activity_repo.get_activity_by_id(row["activity_id"])
            if activity is None or str(activity.user_id) != user_id:
                await self._sync_db("UPDATE {sync} SET undone = 1 WHERE activity_id = ?", (row["activity_id"],))
                continue   # already gone from Discord
            episodes = int((activity.meta or {}).get("episodes") or 0)
            keep = 0 if watched <= int(row["start_ep"]) else episodes - (int(row["end_ep"]) - watched)
            if keep <= 0:
                # A plain delete, not _undo_activity: that would take the episodes off the site again
                await immersion.activity_repo.delete_activity(activity.id, user_id)
                await self._sync_db("UPDATE {sync} SET undone = 1 WHERE activity_id = ?", (activity.id,))
                changes.append(f"Undid log `{activity.id}`: {activity.name} ({episodes} episodes)")
            else:
                duration = round((activity.duration or 0) * keep / episodes)
                async with aiosqlite.connect(immersion.db.db_path) as db:
                    await db.execute(
                        "UPDATE immersion_activities SET duration = ?, "
                        "meta = json_set(COALESCE(meta, '{}'), '$.episodes', ?) WHERE id = ? AND deleted_at IS NULL",
                        (duration, keep, activity.id),
                    )
                    await db.commit()
                immersion.activity_repo.invalidate_user_cache(activity.user_id, activity.guild_id)
                await self._sync_db("UPDATE {sync} SET end_ep = ? WHERE activity_id = ?", (watched, activity.id))
                changes.append(f"Shortened log `{activity.id}`: {activity.name} ({episodes} → {keep} episodes)")
        if changes:
            try:
                await immersion.goal_service.refresh_user_goals(user_id)
            except Exception as e:
                logger.warning(f"{self.tag} Goal refresh after removal on {self.SERVICE} failed for {user_id}: {e}")
        return changes

    # ---------------- site -> Discord ----------------

    @tasks.loop(minutes=POLL_MINUTES)
    async def poll(self):
        if not await self.poll_ready():
            return
        channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if channel is None:
            logger.warning(f"{self.tag} Log channel {LOG_CHANNEL_ID} not found; {self.SERVICE} -> Discord paused")
            return
        if not self.sync_ready(channel.guild.id):
            return   # not connected (or expired) in the server that has the log channel
        for link in await self.all_links():
            try:
                await self.import_from_list(link, channel)
            except Exception as e:
                logger.exception(f"{self.tag} Import failed for {link['user_id']}: {e}")

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    async def import_from_list(self, link: Dict[str, Any], channel: discord.TextChannel):
        """Log new episodes the member marked on the site, up to the daily cap; undo synced
        logs whose episodes were removed there."""
        guild = channel.guild
        member = guild.get_member(int(link["user_id"]))
        immersion = self.bot.get_cog("ImmersionCog")
        if immersion is None:
            return
        imported, changes = False, []
        async with self.lock:
            entries = await self.fetch_recent(link)
            if entries is None:
                return
            snapshot = await self.get_snapshot(link["user_id"])
            day = now_utc().date().isoformat()
            used, capped = await self.get_daily(link["user_id"], day)
            banned = member is not None and hasattr(immersion, "_is_banned_from_logging") \
                and immersion._is_banned_from_logging(member)

            for entry in reversed(entries):   # oldest change first, so the cap applies in order
                anime_id = int(entry["id"])
                watched = int(entry.get("watched") or 0)
                before = snapshot.get(anime_id, 0)   # not seen before = added to the list since linking
                if watched == before:
                    continue
                # Recorded first: whatever happens next, these episodes are never logged twice
                await self.set_snapshot(link["user_id"], anime_id, watched)
                if watched < before:
                    # Episodes removed there: undo the synced logs they came from (not while
                    # rewatching, when the count starts over)
                    if not entry.get("rewatching"):
                        changes += await self.undo_removed(link, immersion, anime_id, watched)
                    continue
                new_episodes = watched - before
                if member is None or member.bot or banned or capped:
                    continue

                minutes_each = max(1, int(entry.get("minutes") or FALLBACK_EPISODE_MINUTES))
                room = DAILY_CAP_HOURS * 3600 - used
                fits = min(new_episodes, room // (minutes_each * 60))
                if fits > 0:
                    title = entry.get("title") or f"{self.SHORT} anime {anime_id}"
                    name = await self.import_name(anime_id, title)
                    activity_id, embed, view, goals, progress_file, error = await immersion._log_anime_core(
                        link["user_id"], str(guild.id), name, fits, minutes_each, now_utc(), None,
                        extra_meta={"source": self.SOURCE, f"{self.PREFIX}_anime_id": anime_id},
                        use_official_duration=False, source=self.SOURCE, fallback_name=title,
                    )
                    if error:
                        logger.info(f"{self.tag} Skipped {anime_id} for {link['user_id']}: {error}")
                        continue
                    await self.add_sync(activity_id, link["user_id"], anime_id, before, before + fits, "from_list")
                    # What was actually logged (fewer episodes if it hit the anime's episode count)
                    logged = await immersion.activity_repo.get_activity_by_id(activity_id)
                    used += logged.duration if logged is not None else fits * minutes_each * 60
                    imported = True
                    await self.set_daily(link["user_id"], day, used, False)
                    kwargs = {"embed": embed, "view": view}
                    if progress_file:
                        kwargs["file"] = progress_file
                    await channel.send(self.text(IMPORT_HEADER, user=member.mention),
                                       allowed_mentions=discord.AllowedMentions.none(), **kwargs)
                    if goals:
                        await channel.send(embed=immersion._build_goal_completion_embed(goals),
                                           allowed_mentions=discord.AllowedMentions.none())
                if fits < new_episodes or used >= DAILY_CAP_HOURS * 3600:
                    capped = True
                    await self.set_daily(link["user_id"], day, used, True)
                    await channel.send(
                        self.text(CAP_MESSAGE, user=member.mention, hours=DAILY_CAP_HOURS),
                        allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[member]),
                    )

            # Anime taken off their list don't show up in it at all: check recently synced ones
            seen = {int(entry["id"]) for entry in entries}
            for anime_id in sorted({int(r["anime_id"]) for r in await self.recent_syncs(link["user_id"])} - seen):
                ok, current, _ = await self.get_entry(link, anime_id)
                if ok and current is None:
                    await self.set_snapshot(link["user_id"], anime_id, 0)
                    changes += await self.undo_removed(link, immersion, anime_id, 0)

        if changes:
            mention = member.mention if member is not None else f"<@{link['user_id']}>"
            await channel.send(self.text(REMOVAL_MESSAGE, user=mention, changes="\n".join(changes))[:2000],
                               allowed_mentions=discord.AllowedMentions.none())

        if (imported or changes) and member is not None:
            # Class roles, and the penalty/reward checks, follow the new total
            try:
                self.bot.dispatch("anime_log_changed", guild, member)
                await immersion._sync_roles_for_member(member, channel, source=f"{self.PREFIX}_import")
            except Exception as e:
                logger.warning(f"{self.tag} Role sync after import failed for {member}: {e}")
