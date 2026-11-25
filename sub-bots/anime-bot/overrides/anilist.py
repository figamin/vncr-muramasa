"""
LJTA (Learn Japanese Through Anime) - AniList link (anime bot only)

Members link their AniList account with /anilist link. The syncing itself is the same as
for MyAnimeList and lives in list_sync.py: /log_anime adds the episodes to their AniList
list, episodes they mark as watched on AniList are logged here (up to the daily cap), and
undo works both ways for synced logs. A member can link AniList or MyAnimeList, not both.

Setup: the bot's AniList API client (made at https://anilist.co/settings/developer) with
its Redirect URL set to https://anilist.co/api/v2/oauth/pin. Its Client ID goes in
DEFAULT_CLIENT_ID below (it's public: it's in every login link); ANILIST_CLIENT_ID in .env
overrides it. No client secret is used: members approve the bot on AniList, AniList shows
them a code (their login), and they paste it into the bot. Nothing else to set up.
AniList logins last a year and can't be renewed by the bot: after that, members are told
to /anilist link again.
"""

import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from .list_sync import DAILY_CAP_HOURS, FALLBACK_EPISODE_MINUTES, ListSync, now_utc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SETTINGS - edit here. (The log channel, daily cap and log/cap/removal messages are
# shared with MyAnimeList: see list_sync.py.)
# ---------------------------------------------------------------------------
DEFAULT_CLIENT_ID = "57862"   # the bot's AniList API client ID (public: it's in every login link)

# Shown (only to them) when someone logs anime while their AniList link has expired.
EXPIRED_MEMBER_NOTICE = (
    "⚠️ Your AniList link has expired, so this log wasn't added to your AniList list and new "
    "episodes on AniList aren't being logged. Use `/anilist link` to reconnect."
)
REQUESTS_PER_MINUTE = 30   # AniList's limit (normally 90; 30 while their API is "degraded")

AUTH_URL = "https://anilist.co/api/v2/oauth/authorize"
GRAPHQL_URL = "https://graphql.anilist.co"
LINK_TIMEOUT_MINUTES = 15
PAGE_SIZE = 50   # most recently updated entries checked each time (AniList's page maximum)

LIST_ENTRY_FIELDS = "id mediaId status progress score repeat"
RECENT_QUERY = """
query ($userId: Int, $perPage: Int) {
  Page(page: 1, perPage: $perPage) {
    mediaList(userId: $userId, type: ANIME, sort: UPDATED_TIME_DESC) {
      %s media { duration title { romaji english native } }
    }
  }
}""" % LIST_ENTRY_FIELDS
WHOLE_LIST_QUERY = """
query ($userId: Int, $chunk: Int) {
  MediaListCollection(userId: $userId, type: ANIME, chunk: $chunk, perChunk: 500) {
    hasNextChunk
    lists { entries { mediaId progress } }
  }
}"""
ENTRY_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) { episodes mediaListEntry { %s } }
}""" % LIST_ENTRY_FIELDS
DELETE_MUTATION = "mutation ($id: Int) { DeleteMediaListEntry(id: $id) { deleted } }"


def token_expiry(token: str) -> str:
    """When an AniList login runs out: read from the token itself (a JWT), else a year."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return datetime.fromtimestamp(int(data["exp"]), timezone.utc).isoformat()
    except Exception:
        return (now_utc() + timedelta(days=365)).isoformat()


def token_from_paste(pasted: str) -> str:
    """The code AniList showed, or (if they pasted an address) the access_token in it."""
    pasted = (pasted or "").strip()
    if "access_token=" in pasted:
        parsed = urlparse(pasted)
        for part in (parsed.fragment, parsed.query, pasted.split("#", 1)[-1]):
            value = parse_qs(part).get("access_token")
            if value:
                return value[0].strip()
    return "".join(pasted.split())   # codes copied from a narrow screen can wrap onto lines


class PasteCodeModal(discord.ui.Modal, title="Finish linking AniList"):
    code = discord.ui.TextInput(
        label="The code AniList showed you",
        style=discord.TextStyle.paragraph,
        placeholder="Paste the whole code here",
        max_length=4000,
    )

    def __init__(self, cog: "AniListCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.finish_link(interaction, str(self.code.value))


class LinkView(discord.ui.View):
    def __init__(self, cog: "AniListCog", auth_url: str):
        super().__init__(timeout=LINK_TIMEOUT_MINUTES * 60)
        self.cog = cog
        # "1. Open AniList" first, then "2. Paste the code"
        self.remove_item(self.paste)
        self.add_item(discord.ui.Button(label="1. Open AniList", style=discord.ButtonStyle.link, url=auth_url))
        self.add_item(self.paste)

    @discord.ui.button(label="2. Paste the code", style=discord.ButtonStyle.primary)
    async def paste(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PasteCodeModal(self.cog))


class AniListCog(ListSync, commands.Cog):
    """Two-way AniList sync for the anime bot (the shared part is ListSync)"""

    SERVICE, SHORT, PREFIX, SOURCE = "AniList", "AniList", "anilist", "anilist"
    WATCHING, COMPLETED = "CURRENT", "COMPLETED"

    anilist = app_commands.Group(name="anilist", description="Link your AniList account", guild_only=True)

    def __init__(self, bot):
        # The main data/ folder (next to vnbot.db), which Docker keeps between rebuilds
        data_dir = Path(__file__).resolve().parents[3] / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        ListSync.__init__(self, bot, str(data_dir / "anime_anilist.db"))
        self.session: Optional[aiohttp.ClientSession] = None
        self.rate_lock = asyncio.Lock()
        self.next_request_at = 0.0
        self.request_gap = 60 / REQUESTS_PER_MINUTE + 0.1   # seconds between requests

    @property
    def client_id(self) -> str:
        return os.getenv("ANILIST_CLIENT_ID", "").strip() or DEFAULT_CLIENT_ID

    async def cog_load(self):
        await self.init_db()
        self.poll.start()

    async def cog_unload(self):
        self.poll.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    async def init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS anilist_links (
                    user_id TEXT PRIMARY KEY,
                    anilist_user_id INTEGER UNIQUE,
                    anilist_name TEXT,
                    token TEXT,
                    expires_at TEXT,
                    linked_at TEXT,
                    broken INTEGER NOT NULL DEFAULT 0
                );
            """)
            await self.create_sync_tables(db)
            await db.commit()

    # ---------------- storage ----------------

    async def get_link(self, user_id: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM anilist_links WHERE user_id = ?", (user_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def all_links(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM anilist_links WHERE broken = 0") as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def mark_broken(self, link: Dict[str, Any]):
        link["broken"] = 1
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE anilist_links SET broken = 1 WHERE user_id = ?", (link["user_id"],))
            await db.commit()
        logger.warning(f"[AniList] Login for {link['user_id']} expired; they need to /anilist link again")

    # ---------------- HTTP ----------------

    async def _send(self, payload: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        try:
            async with self.session.post(GRAPHQL_URL, json=payload, headers=headers) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = None
                return resp.status, body if isinstance(body, dict) else {}, dict(resp.headers)
        except Exception as e:
            logger.warning(f"[AniList] Request failed: {e!r}")
            return 0, {}, {}

    async def _gql(self, query: str, variables: Optional[Dict[str, Any]] = None,
                   token: Optional[str] = None) -> Tuple[int, Dict[str, Any], List[Dict[str, Any]]]:
        """One GraphQL request, spaced out to stay under AniList's rate limit. When AniList
        says to slow down (429), waits as long as it asks (up to a minute) and tries once more."""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {"query": query, "variables": variables or {}}
        status, body = 0, {}
        for attempt in range(2):
            async with self.rate_lock:
                wait = self.next_request_at - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.next_request_at = time.monotonic() + self.request_gap
                status, body, resp_headers = await self._send(payload, headers)
                if status == 429 and attempt == 0:
                    retry = min(max(int(resp_headers.get("Retry-After") or 60), 1), 65)
                    self.next_request_at = time.monotonic() + retry
                    logger.warning(f"[AniList] Rate limited; waiting {retry}s")
                    continue
            break
        return status, body.get("data") or {}, body.get("errors") or []

    async def _api(self, link: Dict[str, Any], query: str, variables: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        """A request as this member. A login AniList no longer accepts (or that's past its
        expiry) marks their link broken, so they're told to link again."""
        if link.get("broken"):
            return 401, {}
        if link.get("expires_at") and datetime.fromisoformat(link["expires_at"]) <= now_utc():
            await self.mark_broken(link)
            return 401, {}
        status, data, errors = await self._gql(query, variables, link.get("token"))
        messages = " ".join(str(e.get("message", "")) for e in errors).lower()
        if status in (400, 401) and ("token" in messages or "unauthorized" in messages):
            await self.mark_broken(link)
            return 401, {}
        if errors and status == 200:
            status = 500   # an answer with errors in it isn't one to act on
        return status, data

    # ---------------- what list_sync.ListSync needs from AniList ----------------

    def sync_ready(self, guild_id) -> bool:
        return bool(self.client_id)

    def app_ok(self) -> bool:
        return bool(self.client_id)

    def anime_id_of(self, meta: Dict[str, Any]) -> Optional[int]:
        return meta.get("anilist_id") or (meta.get("anilist") or {}).get("id")

    @staticmethod
    def _entry(raw: Dict[str, Any], anime_id: int) -> Dict[str, Any]:
        return {"id": int(anime_id), "watched": int(raw.get("progress") or 0), "status": raw.get("status"),
                "score": raw.get("score") or 0, "rewatching": raw.get("status") == "REPEATING",
                "list_id": raw.get("id")}

    async def fetch_recent(self, link: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        status, data = await self._api(link, RECENT_QUERY, {"userId": link["anilist_user_id"], "perPage": PAGE_SIZE})
        if status != 200:
            logger.warning(f"[AniList] Couldn't read the list of {link['user_id']}: {status}")
            return None
        recent = []
        for raw in ((data.get("Page") or {}).get("mediaList") or []):
            media = raw.get("media") or {}
            titles = media.get("title") or {}
            entry = self._entry(raw, raw["mediaId"])
            entry["title"] = titles.get("romaji") or titles.get("english") or titles.get("native")
            entry["minutes"] = int(media.get("duration") or 0) or FALLBACK_EPISODE_MINUTES
            recent.append(entry)
        return recent

    async def fetch_whole_list(self, user: Dict[str, Any], token: str) -> Optional[Dict[int, int]]:
        """Every anime on their list -> episodes watched (the starting point when linking)."""
        link = {"user_id": "linking", "token": token, "broken": 0}
        progress: Dict[int, int] = {}
        chunk = 1
        while True:
            status, data = await self._api(link, WHOLE_LIST_QUERY, {"userId": user["id"], "chunk": chunk})
            if status != 200:
                return None
            collection = data.get("MediaListCollection") or {}
            for group in collection.get("lists") or []:
                for raw in group.get("entries") or []:   # (an anime can be in several lists)
                    progress[int(raw["mediaId"])] = int(raw.get("progress") or 0)
            if not collection.get("hasNextChunk"):
                return progress
            chunk += 1

    async def get_entry(self, link: Dict[str, Any], anime_id: int) -> Tuple[bool, Optional[Dict[str, Any]], int]:
        status, data = await self._api(link, ENTRY_QUERY, {"id": int(anime_id)})
        media = data.get("Media") if status == 200 else None
        if not media:
            logger.warning(f"[AniList] Couldn't read AniList anime {anime_id} for {link['user_id']}: {status}")
            return False, None, 0
        raw = media.get("mediaListEntry")
        return True, (self._entry(raw, anime_id) if raw else None), int(media.get("episodes") or 0)

    async def save_entry(self, link: Dict[str, Any], anime_id: int, watched: int, status: Optional[str]) -> bool:
        if status:
            mutation = ("mutation ($mediaId: Int, $progress: Int, $status: MediaListStatus) { "
                        "SaveMediaListEntry(mediaId: $mediaId, progress: $progress, status: $status) { id } }")
            variables = {"mediaId": int(anime_id), "progress": int(watched), "status": status}
        else:
            mutation = ("mutation ($mediaId: Int, $progress: Int) { "
                        "SaveMediaListEntry(mediaId: $mediaId, progress: $progress) { id } }")
            variables = {"mediaId": int(anime_id), "progress": int(watched)}
        code, data = await self._api(link, mutation, variables)
        return code == 200 and bool(data.get("SaveMediaListEntry"))

    async def delete_entry(self, link: Dict[str, Any], anime_id: int, entry: Dict[str, Any]) -> bool:
        if not entry.get("list_id"):
            return False
        code, data = await self._api(link, DELETE_MUTATION, {"id": int(entry["list_id"])})
        return code == 200 and bool((data.get("DeleteMediaListEntry") or {}).get("deleted"))

    async def import_name(self, anime_id: int, title: str) -> str:
        return f"https://anilist.co/anime/{int(anime_id)}"   # the log builder reads AniList pages

    # ---------------- /log_anime notice ----------------

    async def log_notice(self, guild, member) -> Optional[str]:
        """After someone uses /log_anime: a notice for them if their AniList link expired."""
        user_id = str(member.id)
        link = await self.get_link(user_id)
        if not link:
            return None
        task = self.push_tasks.get(user_id)
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=15)   # let this log's AniList update finish first
        link = await self.get_link(user_id)
        if link and link["broken"]:
            return EXPIRED_MEMBER_NOTICE
        return None

    # ---------------- linking ----------------

    def login_url(self) -> str:
        return f"{AUTH_URL}?{urlencode({'client_id': self.client_id, 'response_type': 'token'})}"

    @anilist.command(name="link", description="Link your AniList account to sync anime logs both ways")
    async def anilist_link(self, interaction: discord.Interaction):
        if not self.client_id:
            await interaction.response.send_message(
                "AniList linking isn't set up yet (the bot's AniList client ID is missing).", ephemeral=True)
            return
        other = await self.other_site_message(str(interaction.user.id))
        if other:
            await interaction.response.send_message(other, ephemeral=True)
            return
        text = (
            "Link your AniList account\n"
            "1. Press Open AniList, log in if asked, and approve the bot.\n"
            "2. AniList then shows you a long code. Copy all of it.\n"
            "3. Press Paste the code and paste it.\n\n"
            "Keep the code to yourself: it's your AniList login for the bot.\n"
            "Once linked, your `/log_anime` logs are added to your AniList list, and episodes you mark "
            f"as watched on AniList are logged here (up to {DAILY_CAP_HOURS} hours a day)."
        )
        await interaction.response.send_message(text, view=LinkView(self, self.login_url()), ephemeral=True)

    async def complete_link(self, user_id: str, pasted: str) -> Tuple[bool, str]:
        """Finish linking from the code the member pasted. Returns (linked?, message for the
        member). Only the member sees the message; it names their AniList account."""
        other = await self.other_site_message(user_id)
        if other:
            return False, other
        token = token_from_paste(pasted)
        if len(token) < 20:
            return False, "That doesn't look like the code AniList showed you. Please start again with `/anilist link`."
        status, data, _ = await self._gql("query { Viewer { id name } }", token=token)
        viewer = data.get("Viewer") if status == 200 else None
        if not viewer:
            return False, "AniList didn't accept that code. Please start again with `/anilist link`."
        # Starting point: what's already on their list is NOT logged, only what changes from now on
        progress = await self.fetch_whole_list(viewer, token)
        if progress is None:
            return False, "Couldn't read your AniList list. Please try `/anilist link` again later."

        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT user_id FROM anilist_links WHERE anilist_user_id = ?", (viewer["id"],)) as cur:
                    row = await cur.fetchone()
                if row and row[0] != user_id:
                    return False, "That AniList account is already linked to another Discord account."
                await self.forget_user(db, user_id)   # a new link starts fresh
                await db.execute(
                    "INSERT OR REPLACE INTO anilist_links (user_id, anilist_user_id, anilist_name, token, expires_at, "
                    "linked_at, broken) VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (user_id, viewer["id"], viewer.get("name"), token, token_expiry(token), now_utc().isoformat()),
                )
                await db.executemany(
                    "INSERT INTO anilist_snapshot (user_id, anime_id, episodes) VALUES (?, ?, ?)",
                    [(user_id, anime_id, episodes) for anime_id, episodes in progress.items()],
                )
                await db.commit()

        logger.info(f"[AniList] Discord user {user_id} linked AniList account {viewer.get('name')} ({viewer['id']})")
        return True, (
            f"✅ Linked to AniList account {viewer.get('name')}. Your existing list ({len(progress)} anime) "
            "isn't logged; only episodes you mark as watched from now on."
        )

    async def finish_link(self, interaction: discord.Interaction, pasted: str):
        """Discord side of linking: everything here is only shown to the member."""
        await interaction.response.defer(ephemeral=True)
        _, message = await self.complete_link(str(interaction.user.id), pasted)
        await interaction.followup.send(message, ephemeral=True)

    @anilist.command(name="unlink", description="Unlink your AniList account")
    async def anilist_unlink(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute("DELETE FROM anilist_links WHERE user_id = ?", (user_id,))
                await self.forget_user(db, user_id)
                await db.commit()
                removed = cur.rowcount
        await interaction.response.send_message(
            "Unlinked. Nothing syncs with AniList any more." if removed else "You don't have an AniList account linked.",
            ephemeral=True,
        )

    @anilist.command(name="status", description="See your AniList link and today's AniList time")
    async def anilist_status(self, interaction: discord.Interaction):
        link = await self.get_link(str(interaction.user.id))
        if not link:
            await interaction.response.send_message("No AniList account linked. Use `/anilist link`.", ephemeral=True)
            return
        seconds, capped = await self.get_daily(link["user_id"], now_utc().date().isoformat())
        lines = [f"Linked to {link['anilist_name']}."]
        if link["broken"]:
            lines.append("⚠️ The AniList login expired, so nothing syncs. Please `/anilist link` again.")
        elif link.get("expires_at"):
            lines.append(f"The login lasts until {datetime.fromisoformat(link['expires_at']):%d %B %Y}; "
                         "after that, `/anilist link` again.")
        if not self.client_id:
            lines.append("AniList syncing isn't set up in the bot (its AniList client ID is missing).")
        lines.append(
            f"Logged from AniList today: {seconds / 3600:.1f}h of {DAILY_CAP_HOURS}h"
            + (" (cap reached; more AniList updates today are ignored)." if capped else ".")
        )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot):
    await bot.add_cog(AniListCog(bot))
