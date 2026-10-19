"""
Server structure backup and restore.

Python port of the discord-backup project (referred to below as the reference),
extended in three ways the reference lacks:
- member-type channel permission overwrites are saved and restored (the
  reference only keeps role overwrites), with an optional old->new ID remap so
  overwrites for bot accounts survive a clone onto a different set of bots
- member role assignments and nicknames are re-applied to users who are
  present in the target server
- restoring with clear_first=False runs in merge mode: roles/channels/emojis
  that already exist by name are kept, only missing ones are created

Structure only, no messages: settings, roles, categories/channels with
permission overwrites, threads, emojis, bans, member roles/nicks.
Backups are plain JSON files under data/server_backups/.
"""

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aiohttp
import discord

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
DEFAULT_BACKUP_DIR = Path("data/server_backups")
BACKUP_ID_RE = re.compile(r"^[\w-]+$")

MAX_EMOJI_BYTES = 256 * 1024
MAX_BANS = 1000

# Pacing between write calls, mirrors the reference. discord.py already sleeps
# on 429s; these keep a long restore from hammering the API in bursts.
ROLE_DELAY = 0.25
CHANNEL_DELAY = 0.25
EMOJI_DELAY = 0.5
BAN_DELAY = 0.5
MEMBER_DELAY = 0.25

ProgressFn = Callable[[str], Awaitable[None]]


# --------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------- #

MAX_BACKUP_ID_LEN = 64


def validate_backup_id(backup_id: str) -> str:
    """Reject IDs that could escape the backup directory or break autocomplete."""
    if not backup_id or not BACKUP_ID_RE.match(backup_id):
        raise ValueError(
            "Invalid backup ID: only letters, numbers, '-' and '_' are allowed."
        )
    if len(backup_id) > MAX_BACKUP_ID_LEN:
        raise ValueError(f"Backup ID too long (max {MAX_BACKUP_ID_LEN} characters).")
    return backup_id


def backup_path(backup_id: str, backup_dir: Optional[Path] = None) -> Path:
    validate_backup_id(backup_id)
    return Path(backup_dir or DEFAULT_BACKUP_DIR) / f"{backup_id}.json"


def save_backup_file(data: dict, backup_dir: Optional[Path] = None) -> Path:
    path = backup_path(data["id"], backup_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then atomically replace, so a crash mid-write cannot
    # corrupt or destroy an existing same-name backup.
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def validate_backup_structure(data) -> dict:
    """Reject anything that isn't a server backup before a destructive restore
    can run on it (e.g. an unrelated .json a user uploaded by mistake)."""
    if not isinstance(data, dict):
        raise ValueError("File is not a JSON object.")
    if not isinstance(data.get("channels"), dict) or not isinstance(data.get("roles"), list):
        raise ValueError("File does not look like a server backup (missing channels/roles).")
    return data


def load_backup(backup_id: str, backup_dir: Optional[Path] = None) -> dict:
    path = backup_path(backup_id, backup_dir)
    if not path.exists():
        raise FileNotFoundError(f"No backup named '{backup_id}' found.")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def delete_backup(backup_id: str, backup_dir: Optional[Path] = None) -> bool:
    path = backup_path(backup_id, backup_dir)
    if not path.exists():
        return False
    path.unlink()
    return True


def list_backups(backup_dir: Optional[Path] = None) -> list:
    """Summaries of all stored backups, newest first."""
    directory = Path(backup_dir or DEFAULT_BACKUP_DIR)
    if not directory.exists():
        return []
    entries = []
    for path in directory.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries.append({
                "id": data.get("id", path.stem),
                "guild_name": data.get("guild_name", "unknown"),
                "guild_id": data.get("guild_id"),
                "created_at": data.get("created_at", ""),
                "size_mb": round(path.stat().st_size / (1024 * 1024), 2),
            })
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping unreadable backup file %s: %s", path, exc)
    entries.sort(key=lambda e: e["created_at"], reverse=True)
    return entries


def summarize_backup(data: dict) -> dict:
    """Item counts for embeds and confirmation prompts."""
    categories = data.get("channels", {}).get("categories", [])
    others = data.get("channels", {}).get("others", [])
    channel_count = len(others) + sum(len(c.get("children", [])) for c in categories)
    member_overwrites = 0
    for holder in categories + others + [c for cat in categories for c in cat.get("children", [])]:
        for ow in holder.get("overwrites", []):
            if ow.get("type") == "member":
                member_overwrites += 1
    return {
        "roles": len(data.get("roles", [])),
        "categories": len(categories),
        "channels": channel_count,
        "member_overwrites": member_overwrites,
        "emojis": len(data.get("emojis", [])),
        "bans": len(data.get("bans", [])),
        "members": len(data.get("members", [])),
    }


def parse_id_map(raw: str) -> dict:
    """Parse an 'old:new,old:new' string into a str->str ID map."""
    id_map = {}
    if not raw or not raw.strip():
        return id_map
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(":")
        if len(parts) != 2 or not parts[0].strip().isdigit() or not parts[1].strip().isdigit():
            raise ValueError(
                f"Invalid id_map entry '{pair}'. Expected 'old_id:new_id' pairs "
                "separated by commas, e.g. '111:222,333:444'."
            )
        id_map[parts[0].strip()] = parts[1].strip()
    return id_map


def remap_id(raw_id: str, id_map: dict) -> str:
    return id_map.get(str(raw_id), str(raw_id))


# --------------------------------------------------------------------- #
# Backup creation
# --------------------------------------------------------------------- #

async def _read_asset(asset) -> Optional[str]:
    """Read a discord Asset into base64, or None on failure."""
    if asset is None:
        return None
    try:
        return base64.b64encode(await asset.read()).decode("ascii")
    except discord.HTTPException as exc:
        logger.warning("Failed to read asset %s: %s", asset, exc)
        return None


def _serialize_overwrites(channel) -> list:
    entries = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        if isinstance(target, discord.Role):
            entries.append({
                "type": "role",
                "name": target.name,
                "allow": str(allow.value),
                "deny": str(deny.value),
            })
        elif isinstance(target, (discord.Member, discord.User)):
            entries.append({
                "type": "member",
                "id": str(target.id),
                "name": str(target),
                "bot": bool(getattr(target, "bot", False)),
                "allow": str(allow.value),
                "deny": str(deny.value),
            })
        # Unresolvable targets (deleted roles/members) are skipped.
    return entries


def _serialize_roles(guild) -> list:
    roles = []
    # Highest first, matching the reference; sequential creation in this order
    # reproduces the original stacking.
    for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if role.managed:
            continue
        roles.append({
            "name": role.name,
            "color": role.colour.value,
            "hoist": role.hoist,
            "permissions": str(role.permissions.value),
            "mentionable": role.mentionable,
            "position": role.position,
            "is_everyone": role.is_default(),
        })
    return roles


def _serialize_threads(channel) -> list:
    threads = []
    for thread in getattr(channel, "threads", []):
        threads.append({
            "name": thread.name,
            "auto_archive_duration": thread.auto_archive_duration,
            "slowmode_delay": thread.slowmode_delay,
            "locked": thread.locked,
        })
    return threads


def _serialize_channel(channel) -> Optional[dict]:
    base = {
        "name": channel.name,
        "position": channel.position,
        "overwrites": _serialize_overwrites(channel),
        "parent": channel.category.name if channel.category else None,
    }
    if isinstance(channel, discord.TextChannel):
        base.update({
            "kind": "text",
            "topic": channel.topic,
            "nsfw": channel.nsfw,
            "slowmode_delay": channel.slowmode_delay,
            "is_news": channel.is_news(),
            "default_auto_archive_duration": channel.default_auto_archive_duration,
            "threads": _serialize_threads(channel),
        })
    elif isinstance(channel, discord.VoiceChannel):
        base.update({
            "kind": "voice",
            "bitrate": channel.bitrate,
            "user_limit": channel.user_limit,
        })
    elif isinstance(channel, discord.StageChannel):
        base.update({
            "kind": "stage",
            "topic": channel.topic,
            "bitrate": channel.bitrate,
            "user_limit": channel.user_limit,
        })
    elif isinstance(channel, discord.ForumChannel):
        base.update({
            "kind": "forum",
            "topic": channel.topic,
            "nsfw": channel.nsfw,
            "slowmode_delay": channel.slowmode_delay,
            "default_auto_archive_duration": channel.default_auto_archive_duration,
            "tags": [
                {
                    "name": tag.name,
                    "moderated": tag.moderated,
                    # Custom emoji IDs would not exist on the target server.
                    "emoji": str(tag.emoji) if tag.emoji and tag.emoji.id is None else None,
                }
                for tag in channel.available_tags
            ],
        })
    else:
        logger.warning("Skipping unsupported channel type %s (%s)", type(channel).__name__, channel.name)
        return None
    return base


def _serialize_channels(guild) -> dict:
    channels = {"categories": [], "others": []}
    for category in sorted(guild.categories, key=lambda c: c.position):
        cat_data = {
            "name": category.name,
            "overwrites": _serialize_overwrites(category),
            "children": [],
        }
        # Sort by raw position for the true display order. category.channels
        # groups text before voice, which loses a voice-above-text layout.
        for child in sorted(category.channels, key=lambda c: c.position):
            data = _serialize_channel(child)
            if data:
                cat_data["children"].append(data)
        channels["categories"].append(cat_data)
    top_level = [c for c in guild.channels
                 if c.category is None and not isinstance(c, (discord.CategoryChannel, discord.Thread))]
    for channel in sorted(top_level, key=lambda c: c.position):
        data = _serialize_channel(channel)
        if data:
            channels["others"].append(data)
    return channels


async def _serialize_emojis(guild) -> list:
    emojis = []
    for emoji in guild.emojis:
        entry = {"name": emoji.name or "unknown", "animated": emoji.animated}
        try:
            raw = await emoji.read()
            if len(raw) <= MAX_EMOJI_BYTES:
                entry["base64"] = base64.b64encode(raw).decode("ascii")
            else:
                entry["url"] = str(emoji.url)
        except discord.HTTPException:
            entry["url"] = str(emoji.url)
        emojis.append(entry)
    return emojis


async def _serialize_bans(guild) -> list:
    bans = []
    try:
        async for entry in guild.bans(limit=MAX_BANS):
            bans.append({"id": str(entry.user.id), "reason": entry.reason or "No reason provided"})
    except discord.HTTPException as exc:
        logger.warning("Could not fetch bans for backup: %s", exc)
    return bans


def _serialize_members(guild) -> list:
    members = []
    for member in guild.members:
        role_names = [r.name for r in member.roles if not r.is_default() and not r.managed]
        members.append({
            "id": str(member.id),
            "name": str(member),
            "nick": member.nick,
            "bot": member.bot,
            "roles": role_names,
        })
    return members


async def create_backup(guild: discord.Guild, *, created_by: int,
                        backup_id: Optional[str] = None,
                        backup_dir: Optional[Path] = None) -> dict:
    """Snapshot a guild's structure and write it to a JSON file."""
    if backup_id:
        validate_backup_id(backup_id)
    else:
        backup_id = f"server_{guild.id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    widget_channel = guild.widget_channel
    data = {
        "format_version": FORMAT_VERSION,
        "id": backup_id,
        "guild_id": str(guild.id),
        "guild_name": guild.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": str(created_by),
        "settings": {
            "name": guild.name,
            "icon_base64": await _read_asset(guild.icon),
            "banner_base64": await _read_asset(guild.banner),
            "splash_base64": await _read_asset(guild.splash),
            "verification_level": guild.verification_level.value,
            "default_notifications": guild.default_notifications.value,
            "explicit_content_filter": guild.explicit_content_filter.value,
            "afk": ({"channel_name": guild.afk_channel.name, "timeout": guild.afk_timeout}
                    if guild.afk_channel else None),
            "system_channel_name": guild.system_channel.name if guild.system_channel else None,
            "widget": {
                "enabled": bool(getattr(guild, "widget_enabled", False)),
                "channel_name": widget_channel.name if widget_channel else None,
            },
        },
        "roles": _serialize_roles(guild),
        "channels": _serialize_channels(guild),
        "emojis": await _serialize_emojis(guild),
        "bans": await _serialize_bans(guild),
        "members": _serialize_members(guild),
    }
    # Serializing multi-MB base64 blobs to disk must not block the event loop.
    await asyncio.to_thread(save_backup_file, data, backup_dir)
    logger.info("Created server backup %s for guild %s (%s)", backup_id, guild.name, guild.id)
    return data


# --------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------- #

@dataclass
class RestoreOptions:
    clear_first: bool = True
    id_map: dict = field(default_factory=dict)
    restore_bans: bool = True
    restore_member_data: bool = True


@dataclass
class RestoreReport:
    cleared: bool = False
    settings_restored: bool = False
    roles_created: int = 0
    roles_skipped: int = 0
    channels_created: int = 0
    channels_skipped: int = 0
    threads_created: int = 0
    member_overwrites_applied: int = 0
    overwrites_skipped: list = field(default_factory=list)
    emojis_created: int = 0
    emojis_skipped: int = 0
    bans_restored: int = 0
    members_updated: int = 0
    members_missing: int = 0
    errors: list = field(default_factory=list)

    def error(self, message: str):
        logger.warning("Restore: %s", message)
        if len(self.errors) < 50:
            self.errors.append(message)


async def _notify(progress: Optional[ProgressFn], message: str):
    if progress is None:
        return
    try:
        await progress(message)
    except Exception:
        # Progress reporting must never break the restore (the status message
        # channel may have been deleted by the clear phase).
        pass


async def _resolve_member(guild, raw_id: str, id_map: dict):
    try:
        member_id = int(remap_id(raw_id, id_map))
    except ValueError:
        return None
    member = guild.get_member(member_id)
    if member is None:
        try:
            member = await guild.fetch_member(member_id)
        except discord.HTTPException:
            member = None
    return member


async def build_overwrites(guild, entries: list, role_map: dict,
                           id_map: dict, report: RestoreReport) -> dict:
    """Turn stored overwrite entries into a discord.py overwrites dict.

    Role overwrites resolve by name against role_map; member overwrites
    resolve by ID after applying the old->new bot ID remap. Targets that
    do not exist on the target guild are recorded and skipped.
    """
    overwrites = {}
    for entry in entries:
        try:
            pair = discord.PermissionOverwrite.from_pair(
                discord.Permissions(int(entry["allow"])),
                discord.Permissions(int(entry["deny"])),
            )
        except (KeyError, ValueError, TypeError):
            report.error(f"Malformed overwrite entry skipped: {entry}")
            continue
        if entry.get("type") == "role":
            role = role_map.get(entry.get("name"))
            if role is not None:
                overwrites[role] = pair
            else:
                report.overwrites_skipped.append(f"role '{entry.get('name')}'")
        else:
            member = await _resolve_member(guild, entry.get("id", ""), id_map)
            if member is not None:
                # Two source IDs can remap to one target (id_map collision); the
                # last write would silently win, so note it rather than miscount.
                if member in overwrites:
                    report.overwrites_skipped.append(
                        f"member '{entry.get('name', entry.get('id'))}' (duplicate target after remap)")
                else:
                    overwrites[member] = pair
                    report.member_overwrites_applied += 1
            else:
                report.overwrites_skipped.append(f"member '{entry.get('name', entry.get('id'))}'")
    return overwrites


async def clear_guild(guild: discord.Guild, report: RestoreReport,
                      progress: Optional[ProgressFn] = None):
    """Delete roles, channels, emojis, webhooks and bans, and reset settings."""
    await _notify(progress, "Clearing existing roles...")
    for role in list(guild.roles):
        if role.is_default() or role.managed or not role.is_assignable():
            continue
        try:
            await role.delete(reason="Server restore: clearing guild")
        except discord.HTTPException as exc:
            report.error(f"Could not delete role '{role.name}': {exc}")

    await _notify(progress, "Clearing existing channels...")
    for channel in list(guild.channels):
        try:
            await channel.delete(reason="Server restore: clearing guild")
        except discord.HTTPException as exc:
            report.error(f"Could not delete channel '{channel.name}': {exc}")

    await _notify(progress, "Clearing emojis, webhooks and bans...")
    for emoji in list(guild.emojis):
        try:
            await emoji.delete(reason="Server restore: clearing guild")
        except discord.HTTPException as exc:
            report.error(f"Could not delete emoji '{emoji.name}': {exc}")
    try:
        for webhook in await guild.webhooks():
            try:
                await webhook.delete(reason="Server restore: clearing guild")
            except discord.HTTPException:
                pass
    except discord.HTTPException as exc:
        report.error(f"Could not list webhooks: {exc}")
    try:
        async for ban in guild.bans(limit=MAX_BANS):
            try:
                await guild.unban(ban.user, reason="Server restore: clearing guild")
            except discord.HTTPException:
                pass
    except discord.HTTPException as exc:
        report.error(f"Could not clear bans: {exc}")

    try:
        kwargs = {
            "icon": None,
            "banner": None,
            "splash": None,
            "afk_channel": None,
            "afk_timeout": 300,
            "system_channel": None,
            "default_notifications": discord.NotificationLevel.only_mentions,
        }
        if "COMMUNITY" not in guild.features:
            kwargs["verification_level"] = discord.VerificationLevel.none
            kwargs["explicit_content_filter"] = discord.ContentFilter.disabled
        await guild.edit(reason="Server restore: clearing guild", **kwargs)
    except discord.HTTPException as exc:
        report.error(f"Could not reset guild settings: {exc}")
    try:
        await guild.edit_widget(enabled=False, channel=None)
    except discord.HTTPException:
        pass
    report.cleared = True


async def _fetch_url_bytes(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
    return None


async def _restore_settings(guild, data: dict, report: RestoreReport):
    """Each setting is applied in its own edit (like the reference's separate
    calls) so one field the target guild rejects cannot void the rest."""
    settings = data.get("settings", {})

    async def _edit(desc, **kwargs) -> bool:
        try:
            await guild.edit(reason="Server restore", **kwargs)
            return True
        except (discord.HTTPException, ValueError, TypeError) as exc:
            report.error(f"Could not restore {desc}: {exc}")
            return False

    # Each field is its own edit: a rejected icon (e.g. animated icon without the
    # boost feature) must not also drop the name, which would have succeeded alone.
    if settings.get("name"):
        report.settings_restored = await _edit("server name", name=settings["name"])
    if settings.get("icon_base64"):
        try:
            icon = base64.b64decode(settings["icon_base64"])
        except ValueError:
            report.error("Invalid stored icon image, skipping")
        else:
            await _edit("icon", icon=icon)
    if settings.get("default_notifications") is not None:
        try:
            level = discord.NotificationLevel(settings["default_notifications"])
        except ValueError:
            level = None
        if level is not None:
            await _edit("default notification level", default_notifications=level)

    # Banner and splash need boost features on the target guild.
    for key, attr, feature in (("banner_base64", "banner", "BANNER"),
                               ("splash_base64", "splash", "INVITE_SPLASH")):
        if settings.get(key) and feature in guild.features:
            try:
                image = base64.b64decode(settings[key])
            except ValueError:
                report.error(f"Invalid stored {attr} image, skipping")
                continue
            await _edit(attr, **{attr: image})

    if settings.get("verification_level") is not None:
        try:
            level = discord.VerificationLevel(settings["verification_level"])
        except ValueError:
            level = None
        if level is not None:
            await _edit("verification level", verification_level=level)
    # Same rule as the reference: the filter is only changeable on community guilds.
    if "COMMUNITY" in guild.features and settings.get("explicit_content_filter") is not None:
        try:
            content_filter = discord.ContentFilter(settings["explicit_content_filter"])
        except ValueError:
            content_filter = None
        if content_filter is not None:
            await _edit("explicit content filter", explicit_content_filter=content_filter)


async def _restore_roles(guild, data: dict, options: RestoreOptions,
                         report: RestoreReport, progress: Optional[ProgressFn]) -> dict:
    """Create missing roles; returns a name -> Role map for overwrites."""
    role_map = {}
    try:
        # The gateway cache can lag right after clear_guild deleted roles;
        # fetch fresh so deleted roles don't leak into the map.
        current_roles = await guild.fetch_roles()
    except discord.HTTPException:
        current_roles = list(guild.roles)
    for role in current_roles:
        role_map.setdefault(role.name, role)

    roles = data.get("roles", [])
    total = len(roles)
    for i, role_data in enumerate(roles, 1):
        name = role_data.get("name", "")[:100]
        try:
            permissions = discord.Permissions(int(role_data.get("permissions", 0)))
        except (TypeError, ValueError):
            permissions = discord.Permissions.none()
        if role_data.get("is_everyone"):
            # Merge mode fills gaps; it must not rewrite the target's @everyone.
            if not options.clear_first:
                report.roles_skipped += 1
                continue
            try:
                await guild.default_role.edit(permissions=permissions, reason="Server restore")
            except discord.HTTPException as exc:
                report.error(f"Could not update @everyone permissions: {exc}")
            continue
        existing = role_map.get(name)
        # In merge mode (and for clear survivors) reuse same-name roles.
        if existing is not None and not existing.is_default():
            report.roles_skipped += 1
            continue
        try:
            role = await guild.create_role(
                name=name,
                permissions=permissions,
                colour=discord.Colour(role_data.get("color", 0)),
                hoist=bool(role_data.get("hoist", False)),
                mentionable=bool(role_data.get("mentionable", False)),
                reason="Server restore",
            )
            role_map[name] = role
            report.roles_created += 1
        except (discord.HTTPException, TypeError, ValueError) as exc:
            # TypeError/ValueError guard a malformed color/field in an uploaded backup.
            report.error(f"Could not create role '{name}': {exc}")
        await _notify(progress, f"Restoring roles ({i}/{total})...")
        await asyncio.sleep(ROLE_DELAY)
    role_map["@everyone"] = guild.default_role
    return role_map


def _find_existing_channel(guild, category, name: str, kind: str):
    kind_types = {
        "text": discord.TextChannel,
        "voice": discord.VoiceChannel,
        "stage": discord.StageChannel,
        "forum": discord.ForumChannel,
    }
    expected = kind_types.get(kind)
    pool = category.channels if category else [c for c in guild.channels if c.category is None]
    for channel in pool:
        if channel.name == name and expected is not None and type(channel) is expected:
            return channel
    return None


async def _restore_channel(guild, channel_data: dict, category, role_map: dict,
                           options: RestoreOptions, report: RestoreReport,
                           position: Optional[int] = None):
    name = channel_data.get("name", "unnamed")
    kind = channel_data.get("kind", "text")
    if not options.clear_first and _find_existing_channel(guild, category, name, kind):
        report.channels_skipped += 1
        return
    overwrites = await build_overwrites(guild, channel_data.get("overwrites", []),
                                        role_map, options.id_map, report)
    channel = None
    # Explicit position reproduces the source order (incl. voice above text);
    # without it Discord auto-places each new channel and the order drifts.
    pos = {} if position is None else {"position": position}
    # Announcement-channel threads must be created as news_thread, but only once
    # the channel has actually been converted to a news channel.
    thread_type = discord.ChannelType.public_thread
    try:
        if kind == "text":
            channel = await guild.create_text_channel(
                name,
                category=category,
                topic=channel_data.get("topic"),
                nsfw=bool(channel_data.get("nsfw", False)),
                slowmode_delay=channel_data.get("slowmode_delay") or 0,
                default_auto_archive_duration=channel_data.get("default_auto_archive_duration") or 1440,
                overwrites=overwrites,
                reason="Server restore",
                **pos,
            )
            if channel_data.get("is_news") and "NEWS" in guild.features:
                try:
                    channel = await channel.edit(type=discord.ChannelType.news)
                    thread_type = discord.ChannelType.news_thread
                except discord.HTTPException as exc:
                    report.error(f"Could not convert '{name}' to announcement channel: {exc}")
        elif kind == "voice":
            channel = await guild.create_voice_channel(
                name,
                category=category,
                bitrate=min(channel_data.get("bitrate") or 64000, int(guild.bitrate_limit)),
                user_limit=channel_data.get("user_limit") or 0,
                overwrites=overwrites,
                reason="Server restore",
                **pos,
            )
        elif kind == "stage":
            # discord.py cannot set a stage channel's topic at rest (it is the
            # live-stage instance topic, set via create_instance), so it is dropped.
            if channel_data.get("topic"):
                report.error(f"Stage channel '{name}' topic could not be restored (not settable at rest)")
            channel = await guild.create_stage_channel(
                name,
                category=category,
                bitrate=min(channel_data.get("bitrate") or 64000, int(guild.bitrate_limit)),
                user_limit=channel_data.get("user_limit") or 0,
                overwrites=overwrites,
                reason="Server restore",
                **pos,
            )
        elif kind == "forum":
            tags = [
                discord.ForumTag(
                    name=t["name"][:20],
                    moderated=bool(t.get("moderated", False)),
                    emoji=discord.PartialEmoji.from_str(t["emoji"]) if t.get("emoji") else None,
                )
                for t in channel_data.get("tags", [])[:20]
                if t.get("name")
            ]
            channel = await guild.create_forum(
                name,
                category=category,
                topic=channel_data.get("topic"),
                nsfw=bool(channel_data.get("nsfw", False)),
                slowmode_delay=channel_data.get("slowmode_delay") or 0,
                default_auto_archive_duration=channel_data.get("default_auto_archive_duration") or 1440,
                available_tags=tags,
                overwrites=overwrites,
                reason="Server restore",
                **pos,
            )
        else:
            report.error(f"Unsupported channel kind '{kind}' for '{name}', skipped")
            return
    except (discord.HTTPException, TypeError, ValueError) as exc:
        # Never let one bad channel abort a restore that already wiped the guild.
        report.error(f"Could not create channel '{name}': {exc}")
        return
    report.channels_created += 1

    for thread_data in channel_data.get("threads", []):
        try:
            await channel.create_thread(
                name=thread_data.get("name", "thread"),
                type=thread_type,
                auto_archive_duration=thread_data.get("auto_archive_duration") or 1440,
                reason="Server restore",
            )
            report.threads_created += 1
        except discord.HTTPException as exc:
            report.error(f"Could not recreate thread '{thread_data.get('name')}' in '{name}': {exc}")
    await asyncio.sleep(CHANNEL_DELAY)


async def _restore_channels(guild, data: dict, role_map: dict, options: RestoreOptions,
                            report: RestoreReport, progress: Optional[ProgressFn]):
    channels = data.get("channels", {})
    categories = channels.get("categories", [])
    others = channels.get("others", [])
    total = len(categories) + len(others) + sum(len(c.get("children", [])) for c in categories)
    done = 0
    # Explicit positions preserve the source layout. Categories are indexed
    # among themselves; each category's children and the top-level channels are
    # indexed within their own group (matching how the backup was serialized).
    for cat_index, cat_data in enumerate(categories):
        cat_name = cat_data.get("name", "unnamed")
        category = None
        if not options.clear_first:
            category = discord.utils.get(guild.categories, name=cat_name)
        if category is None:
            overwrites = await build_overwrites(guild, cat_data.get("overwrites", []),
                                                role_map, options.id_map, report)
            try:
                category = await guild.create_category(cat_name, overwrites=overwrites,
                                                       position=cat_index, reason="Server restore")
                report.channels_created += 1
                await asyncio.sleep(CHANNEL_DELAY)
            except discord.HTTPException as exc:
                report.error(f"Could not create category '{cat_name}': {exc}")
        else:
            report.channels_skipped += 1
        done += 1
        for child_index, child in enumerate(cat_data.get("children", [])):
            if category is not None:
                await _restore_channel(guild, child, category, role_map, options, report,
                                       position=child_index)
            done += 1
            await _notify(progress, f"Restoring channels ({done}/{total})...")
    for other_index, channel_data in enumerate(others):
        await _restore_channel(guild, channel_data, None, role_map, options, report,
                               position=other_index)
        done += 1
        await _notify(progress, f"Restoring channels ({done}/{total})...")


async def _restore_channel_settings(guild, data: dict, report: RestoreReport):
    """AFK, system channel and widget, resolved by channel name."""
    settings = data.get("settings", {})
    kwargs = {}
    afk = settings.get("afk")
    if afk:
        afk_channel = discord.utils.get(guild.voice_channels, name=afk.get("channel_name"))
        if afk_channel:
            kwargs["afk_channel"] = afk_channel
            kwargs["afk_timeout"] = afk.get("timeout", 300)
    system_name = settings.get("system_channel_name")
    if system_name:
        system_channel = discord.utils.get(guild.text_channels, name=system_name)
        if system_channel:
            kwargs["system_channel"] = system_channel
    if kwargs:
        try:
            await guild.edit(reason="Server restore", **kwargs)
        except discord.HTTPException as exc:
            report.error(f"Could not restore AFK/system channel: {exc}")
    widget = settings.get("widget") or {}
    if widget.get("enabled"):
        # A widget channel may be any channel type; only pass one if it resolves,
        # otherwise enable the widget without clearing an existing channel.
        widget_channel = (discord.utils.get(guild.channels, name=widget["channel_name"])
                          if widget.get("channel_name") else None)
        try:
            if widget_channel is not None:
                await guild.edit_widget(enabled=True, channel=widget_channel)
            else:
                await guild.edit_widget(enabled=True)
        except discord.HTTPException as exc:
            report.error(f"Could not restore widget settings: {exc}")


async def _restore_emojis(guild, data: dict, options: RestoreOptions,
                          report: RestoreReport, progress: Optional[ProgressFn]):
    existing_names = {e.name for e in guild.emojis}
    # Static and animated emojis have separate slot caps (emoji_limit each).
    used = {
        False: sum(1 for e in guild.emojis if not e.animated),
        True: sum(1 for e in guild.emojis if e.animated),
    }
    emojis = data.get("emojis", [])
    total = len(emojis)
    async with aiohttp.ClientSession() as session:
        for i, emoji_data in enumerate(emojis, 1):
            name = (emoji_data.get("name") or "unknown")[:32]
            animated = bool(emoji_data.get("animated", False))
            if used[animated] >= guild.emoji_limit:
                report.emojis_skipped += 1
                continue
            if not options.clear_first and name in existing_names:
                report.emojis_skipped += 1
                continue
            image = None
            if emoji_data.get("base64"):
                try:
                    image = base64.b64decode(emoji_data["base64"])
                except ValueError:
                    pass
            elif emoji_data.get("url"):
                image = await _fetch_url_bytes(session, emoji_data["url"])
            if not image:
                report.error(f"No usable image for emoji '{name}', skipped")
                continue
            try:
                await guild.create_custom_emoji(name=name, image=image, reason="Server restore")
                report.emojis_created += 1
                used[animated] += 1
            except (discord.HTTPException, ValueError) as exc:
                report.error(f"Could not create emoji '{name}': {exc}")
            await _notify(progress, f"Restoring emojis ({i}/{total})...")
            await asyncio.sleep(EMOJI_DELAY)


async def _restore_bans(guild, data: dict, report: RestoreReport,
                        progress: Optional[ProgressFn]):
    bans = data.get("bans", [])
    total = len(bans)
    for i, ban in enumerate(bans, 1):
        try:
            # delete_message_seconds=0: guild.ban defaults to purging 1 day of
            # the user's messages, which would violate this tool's structure-only,
            # no-messages scope for anyone still present in the target.
            await guild.ban(discord.Object(id=int(ban["id"])),
                            reason=ban.get("reason") or "Restored from backup",
                            delete_message_seconds=0)
            report.bans_restored += 1
        except (discord.HTTPException, KeyError, TypeError, ValueError) as exc:
            report.error(f"Could not restore ban for {ban.get('id')}: {exc}")
        await _notify(progress, f"Restoring bans ({i}/{total})...")
        await asyncio.sleep(BAN_DELAY)


async def _restore_member_data(guild, data: dict, role_map: dict, options: RestoreOptions,
                               report: RestoreReport, progress: Optional[ProgressFn]):
    members = data.get("members", [])
    total = len(members)
    for i, entry in enumerate(members, 1):
        member = await _resolve_member(guild, entry.get("id", ""), options.id_map)
        if member is None:
            report.members_missing += 1
            continue
        wanted = [role_map[n] for n in entry.get("roles", []) if n in role_map]
        to_add = [r for r in wanted if r.is_assignable() and r not in member.roles]
        nick = entry.get("nick")
        changed = False
        try:
            if to_add:
                await member.add_roles(*to_add, reason="Server restore", atomic=False)
                changed = True
            if nick and member.nick != nick and member.id != guild.owner_id:
                try:
                    await member.edit(nick=nick, reason="Server restore")
                    changed = True
                except discord.Forbidden:
                    pass
        except discord.HTTPException as exc:
            report.error(f"Could not restore data for member {entry.get('name', entry.get('id'))}: {exc}")
        if changed:
            report.members_updated += 1
            await asyncio.sleep(MEMBER_DELAY)
        await _notify(progress, f"Restoring member roles ({i}/{total})...")


async def _run_phase(report: RestoreReport, name: str, coro):
    """Run one restore phase, containing any unexpected error so a later phase
    (and the final report) still runs even after the guild was already wiped."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - a phase must never abort the whole restore
        logger.exception("Server restore phase '%s' failed", name)
        report.error(f"Phase '{name}' failed: {exc}")
        return None


async def restore_backup(guild: discord.Guild, data: dict, options: RestoreOptions,
                         progress: Optional[ProgressFn] = None) -> RestoreReport:
    """Restore a backup into a guild. Sequential and rate-limit friendly.

    With clear_first the guild is wiped first (reference behavior); without
    it, same-name roles/channels/emojis are kept and only missing ones are
    created, so it fills gaps instead of duplicating. Guild-level settings
    (name, icon, @everyone) are only applied in clear_first mode.

    Each phase is exception-contained: because clear_first deletes everything
    up front, a crash in one phase must not strand the guild half-restored with
    no report, so failures are recorded and the remaining phases still run.
    """
    report = RestoreReport()
    if options.clear_first:
        await _run_phase(report, "clear", clear_guild(guild, report, progress))
        await _notify(progress, "Restoring server settings...")
        await _run_phase(report, "settings", _restore_settings(guild, data, report))

    role_map = await _run_phase(report, "roles",
                                _restore_roles(guild, data, options, report, progress))
    if role_map is None:
        role_map = {"@everyone": guild.default_role}
    await _run_phase(report, "channels",
                     _restore_channels(guild, data, role_map, options, report, progress))
    if options.clear_first:
        # AFK/system/widget point at guild identity; merge mode stays additive.
        await _run_phase(report, "channel settings",
                         _restore_channel_settings(guild, data, report))
    await _run_phase(report, "emojis",
                     _restore_emojis(guild, data, options, report, progress))
    if options.restore_bans:
        await _run_phase(report, "bans", _restore_bans(guild, data, report, progress))
    if options.restore_member_data:
        await _run_phase(report, "member data",
                         _restore_member_data(guild, data, role_map, options, report, progress))
    logger.info(
        "Server restore into %s (%s) finished: %d roles, %d channels created, %d errors",
        guild.name, guild.id, report.roles_created, report.channels_created, len(report.errors),
    )
    return report
