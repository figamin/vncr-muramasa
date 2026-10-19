"""Move one Discord account's hikaru records to another account id.

Used by the web console when a member moves to a new Discord account. Works on
hikaru's SQLite database with the standard library driver so the whole
check, plan, snapshot, move and verify sequence runs in one thread inside one
transaction.

Guarantees:
- The only write is changing the owner column of whole rows (plus one journal
  row). Nothing is deleted or overwritten, so row counts, vote tallies and
  point totals are identical before and after.
- A row that cannot move without breaking a per-user rule (one log per VN per
  month, one ballot per cycle, one nomination per server and period, one
  settings row and one VNDB link per account) stays on the source account and
  is reported instead.
- Applying requires the token of a preview; a plan that no longer matches is
  refused, so what runs is exactly what was shown.
- Before committing, the transaction recomputes its fingerprints and rolls back
  on any difference.
- A user-id column, unique index or trigger this module does not know about
  makes it refuse, so a later schema change cannot be half-handled.
- Every transfer is journaled row by row in hikaru's own user_transfers table
  and can be undone precisely; a verified snapshot of the database is written
  before any change.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_SNOWFLAKE = 2**63 - 1
_SNOWFLAKE_RE = re.compile(r"^[1-9][0-9]{16,18}$")

# Seconds to wait for hikaru's own short write transactions to finish.
BUSY_TIMEOUT_S = 10.0
SNAPSHOTS_KEPT = 10
_UPDATE_CHUNK = 500

# (table, owner column, primary key, label). The only tables whose rows move.
MOVABLE = (
    ("reading_logs", "user_id", "log_id", "Reading logs"),
    ("vn_votes", "user_id", "id", "Votes"),
    ("vn_titles", "nominator_user_id", "id", "Nominations"),
)
MOVABLE_TABLES = tuple(t for t, _, _, _ in MOVABLE)

# (table, owner column, item key, label). Tables keyed by the owner itself, so
# a moved row changes its primary key. The item key names a row within one
# owner's rows and survives the move, which is what the journal records; None
# means one row per owner. Each is optional: older databases lack them.
USER_KEYED = (
    ("user_settings", "user_id", None, "Settings"),
    ("vndb_links", "user_id", "vndb_uid", "VNDB link"),
    ("vndb_ulist", "user_id", "vndb_id", "VNDB list entries"),
)
# The planner's one-row-per-key collision rule depends on these exact keys.
USER_KEYED_PK = {
    "user_settings": ("user_id",),
    "vndb_links": ("user_id",),
    "vndb_ulist": ("user_id", "vndb_id"),
}
# Journal id of the single row in a table whose item key is None.
SINGLE_ROW_ID = 1

# Columns that hold a user id but are deliberately left alone.
KEPT_USER_COLUMNS = {
    ("users", "discord_user_id"),       # username cache, refills for the new id
    ("guild_managers", "principal_id"),  # permission grant, re-added by hand
    ("guild_managers", "added_by_user_id"),
    ("guild_reply_settings", "updated_by"),  # audit field of a server setting
    ("theme_templates", "created_by_user_id"),
    ("theme_assignments", "created_by_user_id"),
    ("user_transfers", "from_user_id"),
    ("user_transfers", "to_user_id"),
    ("vn_nominations", "user_id"),       # legacy; must hold no rows for the source
}

# Columns whose names look user-related but hold no account id.
NOT_USER_ID_COLUMNS = {
    ("guild_managers", "principal_type"),
    ("reading_logs", "user_rating"),
    ("users", "user_name"),
    ("vndb_links", "vndb_uid"),       # VNDB account id, not a Discord one
    ("vndb_links", "vndb_username"),
    ("users", "user_tag"),
}

# Deliberately broad: a false positive only asks for a column to be classified
# above, while a miss could leave part of an account behind.
_USERISH_RE = re.compile(
    r"user|member|author|voter|nominator|owner|principal|discord|uid|reader|creator"
    r"|requester|recipient|_by$|_by_"
)

# Unique indexes on a movable table that include its owner column. The planner
# enforces each of these (more strictly than the index itself), so any other
# such index means the planner could miss a collision.
KNOWN_OWNER_UNIQUE = {
    "reading_logs": {("reward_month", "user_id", "vndb_id")},
    "vn_votes": {("cycle_id", "nomination_id", "user_id")},
    "vn_titles": {("end_month", "guild_id", "nominator_user_id", "start_month")},
    "user_settings": {("user_id",)},
    "vndb_links": {("user_id",)},
    "vndb_ulist": {("user_id", "vndb_id")},
}

_apply_lock = threading.Lock()


class TransferError(Exception):
    """A refusal or failure whose message is safe to show in the console."""


def parse_account_id(value) -> int:
    """Validate a Discord account id supplied as a string.

    Strings only: a JSON number above 2**53 has already lost precision in the
    browser, so accepting one could silently target a different account.
    """
    if not isinstance(value, str):
        raise TransferError("Account ids must be sent as strings.")
    value = value.strip()
    if not _SNOWFLAKE_RE.fullmatch(value):
        raise TransferError("Account ids are 17 to 19 digits with no leading zero.")
    parsed = int(value)
    if parsed > MAX_SNOWFLAKE:
        raise TransferError("Account id is out of range.")
    return parsed


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema guard
# ---------------------------------------------------------------------------

def _tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [r[0] for r in rows]


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _pk_columns(conn, table: str) -> tuple[str, ...]:
    rows = [r for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall() if r[5]]
    return tuple(r[1] for r in sorted(rows, key=lambda r: r[5]))


def _sources(conn) -> list[tuple[str, str, str, str, str]]:
    """(table, owner, key SQL, order SQL, label) for every table whose rows move.

    The key SQL selects the id the journal records for a row; the order SQL
    is a total order over the table's rows.
    """
    out = [(t, col, pk, pk, label) for t, col, pk, label in MOVABLE]
    present = set(_tables(conn))
    for t, col, key, label in USER_KEYED:
        if t in present:
            out.append((t, col, key or str(SINGLE_ROW_ID), ", ".join(USER_KEYED_PK[t]), label))
    return out


def check_schema(conn) -> None:
    """Refuse unless the database has exactly the shape this module handles."""
    tables = set(_tables(conn))
    if "user_transfers" not in tables:
        raise TransferError(
            "Hikaru's database has no user_transfers table yet. Deploy the hikaru "
            "update that adds it (restart hikaru), then try again."
        )
    for table, col, pk, _ in MOVABLE:
        if table not in tables:
            raise TransferError(f"Hikaru's database has no {table} table.")
        cols = _columns(conn, table)
        if col not in cols or pk not in cols:
            raise TransferError(f"{table} is missing {col} or {pk}; the schema has changed.")
        # Undo addresses rows by primary key, which is only safe while deleted
        # keys are never handed out again.
        create_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0] or ""
        if "AUTOINCREMENT" not in create_sql.upper():
            raise TransferError(f"{table} no longer uses AUTOINCREMENT keys; refusing.")
    for table, col, key, _ in USER_KEYED:
        if table not in tables:
            continue
        cols = _columns(conn, table)
        if col not in cols or (key is not None and key not in cols):
            raise TransferError(f"{table} is missing {col} or {key}; the schema has changed.")
        if _pk_columns(conn, table) != USER_KEYED_PK[table]:
            raise TransferError(f"{table} no longer has the primary key this tool expects; refusing.")

    unknown = []
    for table in sorted(tables):
        # Migration snapshot tables are never read by the bot.
        if table.endswith("_backup"):
            continue
        for col in _columns(conn, table):
            key = (table, col)
            if not _USERISH_RE.search(col.lower()):
                continue
            if key in NOT_USER_ID_COLUMNS or key in KEPT_USER_COLUMNS:
                continue
            if any(key == (t, c) for t, c, _, _ in MOVABLE + USER_KEYED):
                continue
            unknown.append(f"{table}.{col}")
    if unknown:
        raise TransferError(
            "Hikaru's database has user-related columns this tool does not know how "
            "to handle: " + ", ".join(unknown) + ". Update the transfer tool first."
        )

    sources = _sources(conn)
    for table, col, _, _, _ in sources:
        for idx in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
            # Primary keys count too: on the owner-keyed tables they are the rule.
            if not idx["unique"]:
                continue
            names = [r["name"] for r in conn.execute(f'PRAGMA index_info("{idx["name"]}")').fetchall()]
            if any(n is None for n in names):
                raise TransferError(
                    f"{table} has a unique expression index ({idx['name']}) this tool "
                    "cannot evaluate. Update the transfer tool first."
                )
            idx_cols = tuple(sorted(names))
            if col in idx_cols and idx_cols not in KNOWN_OWNER_UNIQUE[table]:
                raise TransferError(
                    f"{table} has a unique rule this tool does not know ({idx['name']}). "
                    "Update the transfer tool first."
                )

    source_tables = tuple(s[0] for s in sources)
    placeholders = ",".join("?" * len(source_tables))
    triggers = conn.execute(
        f"SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ({placeholders})",
        source_tables,
    ).fetchall()
    if triggers:
        raise TransferError(
            "Hikaru's database has triggers on the transferred tables; refusing."
        )

    for table, col, _, _, _ in sources:
        odd = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL AND typeof({col}) != 'integer'"
        ).fetchone()[0]
        if odd:
            raise TransferError(
                f"{table}.{col} has {odd} value(s) not stored as integers; "
                "they could be skipped silently, so the transfer refuses."
            )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _owned(conn, table: str, col: str, key: str, order: str, uid: int) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT *, {key} AS transfer_key FROM {table} WHERE {col} = ? ORDER BY {order}", (uid,)
    ).fetchall()


def _vn_title(conn, vndb_id):
    if not vndb_id:
        return None
    row = conn.execute(
        "SELECT title_en, title_ja FROM vndb_cache WHERE vndb_id = ?", (vndb_id,)
    ).fetchone()
    if row:
        return row["title_en"] or row["title_ja"]
    row = conn.execute(
        "SELECT title_cache FROM vn_titles WHERE vndb_id = ? AND title_cache IS NOT NULL LIMIT 1",
        (vndb_id,),
    ).fetchone()
    return row["title_cache"] if row else None


def _vndb_link(conn, uid: int):
    if "vndb_links" not in _tables(conn):
        return None
    row = conn.execute("SELECT vndb_uid FROM vndb_links WHERE user_id = ?", (uid,)).fetchone()
    return row[0] if row else None


def _sid(value):
    """Snowflakes leave as strings so the browser never rounds them."""
    return None if value is None else str(value)


def _describe(conn, table: str, row: sqlite3.Row) -> dict:
    if table == "reading_logs":
        return {
            "id": row["log_id"],
            "vndb_id": row["vndb_id"],
            "title": _vn_title(conn, row["vndb_id"]),
            "month": row["reward_month"],
            "points": row["points"],
            "reason": row["reward_reason"],
            "rating": row["user_rating"],
            # Rows from before per-user scales are out of 5.
            "rating_scale": row["rating_scale"] if "rating_scale" in row.keys() else 5,
            "has_comment": bool(row["comment"]),
            "guild_id": _sid(row["logged_in_guild"]),
        }
    if table == "vn_votes":
        nom = conn.execute(
            "SELECT vndb_id, title_cache FROM vn_titles WHERE id = ?", (row["nomination_id"],)
        ).fetchone()
        title = None
        if nom:
            title = nom["title_cache"] or _vn_title(conn, nom["vndb_id"])
        return {
            "id": row["id"],
            "cycle_id": row["cycle_id"],
            "guild_id": _sid(row["guild_id"]),
            "title": title,
        }
    if table == "user_settings":
        return {"id": row["transfer_key"], "rating_scale": row["rating_scale"]}
    if table == "vndb_links":
        return {
            "id": row["transfer_key"],
            "vndb_uid": row["vndb_uid"],
            "vndb_username": row["vndb_username"],
        }
    if table == "vndb_ulist":
        title = None
        if "vndb_vn_titles" in _tables(conn):
            t = conn.execute(
                "SELECT title, alttitle FROM vndb_vn_titles WHERE vndb_id = ?", (row["vndb_id"],)
            ).fetchone()
            if t:
                title = t["title"] or t["alttitle"]
        return {
            "id": row["transfer_key"],
            "vndb_id": row["vndb_id"],
            "title": title or _vn_title(conn, row["vndb_id"]),
            "vote": row["vote"],
        }
    return {
        "id": row["id"],
        "vndb_id": row["vndb_id"],
        "title": row["title_cache"] or _vn_title(conn, row["vndb_id"]),
        "guild_id": _sid(row["guild_id"]),
        "period": row["start_month"] if row["start_month"] == row["end_month"]
        else f"{row['start_month']} to {row['end_month']}",
        "status": row["status"],
    }


def build_plan(conn, src: int, dst: int, restrict: dict | None = None,
               mode: str = "transfer") -> dict:
    """Decide which of src's rows move to dst and which stay.

    ``restrict`` limits the candidates to given row ids per table (used by
    undo, which may only move back the rows a transfer moved). Candidates that
    src no longer owns are reported as missing.
    """
    if src == dst:
        raise TransferError("The two account ids are the same.")

    plan = {"mode": mode, "source_id": str(src), "target_id": str(dst), "tables": {}}
    move_ids: dict[str, list] = {}
    stay_ids: dict[str, list] = {}

    for table, col, key, order, label in _sources(conn):
        src_rows = _owned(conn, table, col, key, order, src)
        dst_rows = _owned(conn, table, col, key, order, dst)
        missing = []
        if restrict is not None:
            wanted = set(restrict.get(table, []))
            present = {r["transfer_key"] for r in src_rows}
            missing = sorted(wanted - present)
            src_rows = [r for r in src_rows if r["transfer_key"] in wanted]

        move, stay = [], []
        if table == "reading_logs":
            taken = {(r["vndb_id"], r["reward_month"]) for r in dst_rows if r["vndb_id"] is not None}
            conflict_of = {(r["vndb_id"], r["reward_month"]): r["log_id"]
                           for r in dst_rows if r["vndb_id"] is not None}
            for r in src_rows:
                key = (r["vndb_id"], r["reward_month"])
                if r["vndb_id"] is not None and key in taken:
                    d = _describe(conn, table, r)
                    d["why"] = "The target already has this VN logged for this month."
                    d["conflicts_with"] = conflict_of.get(key)
                    stay.append(d)
                else:
                    move.append(r)
                    if r["vndb_id"] is not None:
                        taken.add(key)
        elif table == "vn_votes":
            dst_cycles = {r["cycle_id"] for r in dst_rows}
            for r in src_rows:
                if r["cycle_id"] in dst_cycles:
                    d = _describe(conn, table, r)
                    d["why"] = "The target also voted in this cycle (one ballot per person)."
                    stay.append(d)
                else:
                    move.append(r)
        elif table in ("user_settings", "vndb_links"):
            why = ("The target already has its own settings." if table == "user_settings"
                   else "The target already has a linked VNDB account.")
            for r in src_rows:
                if dst_rows:
                    d = _describe(conn, table, r)
                    d["why"] = why
                    stay.append(d)
                else:
                    move.append(r)
        elif table == "vndb_ulist":
            # Hikaru syncs list entries only for the link on the same account,
            # so entries move only where they end up under the link they came
            # from: with their own link, or where neither account has one.
            src_link, dst_link = _vndb_link(conn, src), _vndb_link(conn, dst)
            link_moves = bool(move_ids.get("vndb_links"))
            why_all = None
            if src_link is not None and not link_moves:
                why_all = "Belongs to the source's VNDB link, which stays."
            elif src_link is None and dst_link is not None:
                why_all = "The target has its own VNDB link; this entry was not synced from it."
            dst_vns = {r["vndb_id"] for r in dst_rows}
            for r in src_rows:
                why = why_all
                if why is None and r["vndb_id"] in dst_vns:
                    why = "The target already has this VN in its VNDB list."
                if why:
                    d = _describe(conn, table, r)
                    d["why"] = why
                    stay.append(d)
                else:
                    move.append(r)
        else:
            # Hikaru reads a row with no guild as belonging to every guild, so a
            # NULL on either side collides with any guild for the same period.
            dst_periods = {(r["guild_id"], r["start_month"], r["end_month"]) for r in dst_rows}
            dst_windows = {(s, e) for _, s, e in dst_periods}
            dst_global = {(s, e) for g, s, e in dst_periods if g is None}

            def collides(r):
                window = (r["start_month"], r["end_month"])
                if r["guild_id"] is None:
                    return window in dst_windows
                return (r["guild_id"], *window) in dst_periods or window in dst_global

            for r in src_rows:
                if collides(r):
                    d = _describe(conn, table, r)
                    d["why"] = "The target also nominated for this server and period."
                    stay.append(d)
                else:
                    move.append(r)

        move_ids[table] = sorted(r["transfer_key"] for r in move)
        stay_ids[table] = sorted(d["id"] for d in stay)
        plan["tables"][table] = {
            "label": label,
            "move": [_describe(conn, table, r) for r in move],
            "stay": stay,
            "missing": missing,
            "target_existing": len(dst_rows),
        }

    if "vn_nominations" in _tables(conn):
        legacy = conn.execute(
            "SELECT COUNT(*) FROM vn_nominations WHERE user_id = ?", (src,)
        ).fetchone()[0]
        if legacy:
            raise TransferError(
                "The legacy vn_nominations table still holds rows for the source "
                "account. It should be empty after hikaru's migrations; refusing."
            )

    def manager_guilds(uid):
        rows = conn.execute(
            "SELECT guild_id FROM guild_managers WHERE principal_type = 'user' AND principal_id = ?",
            (uid,),
        ).fetchall()
        return [str(r[0]) for r in rows]

    plan["source_manager_guilds"] = manager_guilds(src)
    plan["target_manager_guilds"] = manager_guilds(dst)
    plan["move_count"] = sum(len(v) for v in move_ids.values())
    plan["stay_count"] = sum(len(v) for v in stay_ids.values())
    plan["points_moving"] = sum(d["points"] for d in plan["tables"]["reading_logs"]["move"])
    plan["_move_ids"] = move_ids
    plan["_stay_ids"] = stay_ids
    plan["token"] = _token(mode, src, dst, move_ids, stay_ids)

    warnings = []
    if plan["source_manager_guilds"]:
        warnings.append(
            f"The source account is a manager in {len(plan['source_manager_guilds'])} "
            "server(s). Manager grants are not moved; add the new account by hand if it should have them."
        )
    if move_ids["reading_logs"]:
        warnings.append(
            "Role rewards follow total points. The receiving account gets its role on hikaru's "
            "next check; an account left with no logs keeps whatever reward role it has until "
            "removed by hand, and one left with some logs is moved to the role its remaining "
            "points earn. Leaderboards and season placements are computed live and change with "
            "the moved points."
        )
    if move_ids["vn_votes"]:
        placeholders = ",".join("?" * len(move_ids["vn_votes"]))
        open_votes = conn.execute(
            f"SELECT COUNT(*) FROM vn_votes v JOIN vn_cycles c ON c.id = v.cycle_id "
            f"WHERE v.id IN ({placeholders}) AND c.phase != 'closed'",
            move_ids["vn_votes"],
        ).fetchone()[0]
        if open_votes:
            warnings.append(
                f"{open_votes} moving vote(s) belong to a cycle that is still open. Ask the "
                "member not to vote from either account until the transfer is done."
            )
    if plan["stay_count"] and mode == "transfer":
        warnings.append(
            f"{plan['stay_count']} row(s) stay on the source account because they collide "
            "with the target's own records. Review them below; nothing is deleted."
        )
    elif plan["stay_count"]:
        warnings.append(
            f"{plan['stay_count']} row(s) collide with records the original account has now. "
            "Undo is blocked until they are resolved."
        )
    missing = sum(len(t["missing"]) for t in plan["tables"].values())
    if missing:
        warnings.append(
            f"{missing} row(s) from the transfer no longer exist (deleted since) and are skipped."
        )
    plan["warnings"] = warnings
    return plan


def _token(mode, src, dst, move_ids, stay_ids) -> str:
    payload = json.dumps(
        {"mode": mode, "src": src, "dst": dst, "move": move_ids, "stay": stay_ids},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def public_plan(plan: dict) -> dict:
    return {k: v for k, v in plan.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _hash_rows(rows) -> str:
    h = hashlib.sha256()
    for row in rows:
        h.update(repr(list(row)).encode())
        h.update(b"\n")
    return h.hexdigest()


def fingerprint(conn, src: int, dst: int) -> dict:
    """Everything a transfer must leave unchanged."""
    fp = {}
    for table, col, _, order, _ in _sources(conn):
        cols = _columns(conn, table)
        owner_index = cols.index(col)
        fp[f"{table}.count"] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        fp[f"{table}.others"] = _hash_rows(conn.execute(
            f"SELECT * FROM {table} WHERE {col} IS NULL OR {col} NOT IN (?, ?) ORDER BY {order}",
            (src, dst),
        ))
        # The pair's rows with the owner blanked: content must not change.
        # Sorted after blanking because the owner can be part of the order.
        pair = [list(r) for r in conn.execute(
            f"SELECT * FROM {table} WHERE {col} IN (?, ?)", (src, dst),
        )]
        for values in pair:
            values[owner_index] = None
        fp[f"{table}.pair_content"] = _hash_rows(sorted(pair, key=repr))
    fp["vote_tally"] = [tuple(r) for r in conn.execute(
        "SELECT cycle_id, nomination_id, COUNT(*) FROM vn_votes GROUP BY 1, 2 ORDER BY 1, 2"
    )]
    fp["points_by_guild"] = [tuple(r) for r in conn.execute(
        "SELECT logged_in_guild, COUNT(*), SUM(points) FROM reading_logs GROUP BY 1 ORDER BY 1"
    )]
    fp["points_pair"] = tuple(conn.execute(
        "SELECT COUNT(*), SUM(points) FROM reading_logs WHERE user_id IN (?, ?)", (src, dst)
    ).fetchone())
    return fp


def _owned_ids(conn, uid: int) -> dict[str, set]:
    return {
        table: {r[0] for r in conn.execute(f"SELECT {key} FROM {table} WHERE {col} = ?", (uid,))}
        for table, col, key, _, _ in _sources(conn)
    }


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def take_snapshot(db_path: str, snapshot_dir: Path, label: str, expected_counts: dict) -> Path:
    """Copy the database with SQLite's backup API and verify the copy.

    Called while the caller holds the write lock, so the copy is exactly the
    state the change starts from.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    # Timestamp first so name order is time order for pruning.
    final = snapshot_dir / f"hikaru_pre_{stamp}_{label}.sqlite3"
    tmp = snapshot_dir / (final.name + ".tmp")
    source_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    try:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source_uri, uri=True, timeout=BUSY_TIMEOUT_S)) as src, \
                closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst)
            check = dst.execute("PRAGMA quick_check").fetchone()[0]
            counts = {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in expected_counts}
        if check != "ok" or counts != expected_counts:
            raise TransferError("The pre-change snapshot did not verify; nothing was changed.")
        os.replace(tmp, final)
    except TransferError:
        _discard(tmp)
        raise
    except Exception as exc:
        _discard(tmp)
        raise TransferError(f"Could not write the pre-change snapshot ({exc}); nothing was changed.")
    return final


def _discard(path: Path) -> None:
    """Best-effort removal that never masks the error being reported."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove %s", path.name)


def prune_snapshots(snapshot_dir: Path, keep: int | None = None, protect: Path | None = None) -> None:
    if keep is None:
        keep = SNAPSHOTS_KEPT
    try:
        files = sorted(snapshot_dir.glob("hikaru_pre_*.sqlite3"))
    except Exception:
        logger.exception("Could not list hikaru transfer snapshots")
        return
    for old in files[:-keep] if keep else files:
        if protect is not None and old.name == protect.name:
            continue
        try:
            old.unlink(missing_ok=True)
        except Exception:
            logger.warning("Could not remove old transfer snapshot %s", old.name)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _move(conn, move_ids: dict, src: int, dst: int) -> None:
    for table, col, key, _, _ in _sources(conn):
        ids = move_ids[table]
        for i in range(0, len(ids), _UPDATE_CHUNK):
            chunk = ids[i:i + _UPDATE_CHUNK]
            marks = ",".join("?" * len(chunk))
            cur = conn.execute(
                f"UPDATE {table} SET {col} = ? WHERE {col} = ? AND {key} IN ({marks})",
                (dst, src, *chunk),
            )
            if cur.rowcount != len(chunk):
                raise TransferError(f"{table}: expected to move {len(chunk)} row(s), moved {cur.rowcount}.")


def _run_locked(fn):
    if not _apply_lock.acquire(blocking=False):
        raise TransferError("Another transfer or undo is running. Try again in a moment.")
    try:
        return fn()
    finally:
        _apply_lock.release()


def _verified_move(conn, db_path, snapshot_dir, label, plan, src, dst, journal_write,
                   made: list) -> int:
    """Snapshot, move, write the journal and verify, inside the open transaction.

    The snapshot path is appended to ``made`` so the caller can remove it if the
    transaction does not commit.
    """
    move_ids = plan["_move_ids"]
    before = fingerprint(conn, src, dst)
    src_before, dst_before = _owned_ids(conn, src), _owned_ids(conn, dst)
    made.append(take_snapshot(
        db_path, snapshot_dir, label,
        {t: before[f"{t}.count"] for t in move_ids},
    ))

    changes_before = conn.total_changes
    _move(conn, move_ids, src, dst)
    journal_id = journal_write()

    after = fingerprint(conn, src, dst)
    if after != before:
        diff = sorted(k for k in before if before[k] != after.get(k))
        raise TransferError("Verification failed (" + ", ".join(diff) + "); rolled back.")
    src_after, dst_after = _owned_ids(conn, src), _owned_ids(conn, dst)
    for table in move_ids:
        moved = set(move_ids[table])
        if src_after[table] != src_before[table] - moved or dst_after[table] != dst_before[table] | moved:
            raise TransferError(f"Verification failed ({table} ownership); rolled back.")
    expected_changes = plan["move_count"] + 1
    if conn.total_changes - changes_before != expected_changes:
        raise TransferError("Verification failed (unexpected row changes); rolled back.")
    return journal_id


def _busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _in_write_transaction(db_path: str, body):
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = body(conn)
            conn.execute("COMMIT")
            return result
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.OperationalError as exc:
        if _busy(exc):
            raise TransferError("Hikaru's database is busy. Nothing was changed; try again.")
        raise
    finally:
        conn.close()


def _commit_with_snapshot(db_path: str, snapshot_dir: Path, body):
    """Run body in a locked write transaction; returns (result, snapshot path).

    A snapshot written for a change that was cleanly refused and rolled back is
    removed, since the live database still equals it. On any other error the
    snapshot is kept.
    """
    made: list[Path] = []
    try:
        result = _run_locked(lambda: _in_write_transaction(db_path, lambda conn: body(conn, made)))
    except TransferError:
        for path in made:
            _discard(path)
        raise
    snapshot = made[-1]
    prune_snapshots(snapshot_dir, protect=snapshot)
    return result, snapshot


def _in_read_transaction(db_path: str, body):
    try:
        with closing(connect(db_path)) as conn:
            conn.execute("BEGIN")
            try:
                return body(conn)
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        if _busy(exc):
            raise TransferError("Hikaru's database is busy. Try again.")
        raise


def preview_transfer(db_path: str, src: int, dst: int) -> dict:
    def body(conn):
        check_schema(conn)
        return public_plan(build_plan(conn, src, dst))
    return _in_read_transaction(db_path, body)


def apply_transfer(db_path: str, src: int, dst: int, token: str, snapshot_dir: Path) -> dict:
    def body(conn, made):
        check_schema(conn)
        plan = build_plan(conn, src, dst)
        if plan["token"] != token:
            raise TransferError("The records changed since the preview. Preview again before confirming.")
        if plan["move_count"] == 0:
            raise TransferError("There is nothing to move.")

        def journal_write():
            cur = conn.execute(
                "INSERT INTO user_transfers (from_user_id, to_user_id, moved_json, left_json) "
                "VALUES (?, ?, ?, ?)",
                (src, dst, json.dumps(plan["_move_ids"]), json.dumps(plan["_stay_ids"])),
            )
            return cur.lastrowid

        journal_id = _verified_move(
            conn, db_path, snapshot_dir, "transfer", plan, src, dst, journal_write, made)
        return plan, journal_id

    (plan, journal_id), snapshot = _commit_with_snapshot(db_path, snapshot_dir, body)
    logger.info(
        "Hikaru user transfer #%d applied: moved %d row(s), left %d",
        journal_id, plan["move_count"], plan["stay_count"],
    )
    result = public_plan(plan)
    result["transfer_id"] = journal_id
    result["snapshot"] = snapshot.name
    return result


def _load_journal(conn, transfer_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM user_transfers WHERE id = ?", (transfer_id,)).fetchone()
    if row is None:
        raise TransferError("No such transfer.")
    if row["undone_at"] is not None:
        raise TransferError("This transfer was already undone.")
    return row


def _undo_plan(conn, transfer_id: int) -> tuple[dict, sqlite3.Row]:
    check_schema(conn)
    row = _load_journal(conn, transfer_id)
    # Newest first: a later transfer can have carried some of these rows on to
    # a third account, and undoing out of order would strand them there.
    accounts = (row["from_user_id"], row["to_user_id"])
    later = conn.execute(
        "SELECT id FROM user_transfers WHERE id > ? AND undone_at IS NULL "
        "AND (from_user_id IN (?, ?) OR to_user_id IN (?, ?)) ORDER BY id DESC LIMIT 1",
        (transfer_id, *accounts, *accounts),
    ).fetchone()
    if later:
        raise TransferError(
            f"Transfer #{later[0]} is newer and involves the same account. Undo it first."
        )
    moved = json.loads(row["moved_json"])
    # Undo moves rows from the transfer's target back to its source.
    plan = build_plan(conn, row["to_user_id"], row["from_user_id"], restrict=moved,
                      mode=f"undo:{transfer_id}")
    plan["transfer_id"] = transfer_id
    return plan, row


def preview_undo(db_path: str, transfer_id: int) -> dict:
    return _in_read_transaction(db_path, lambda conn: public_plan(_undo_plan(conn, transfer_id)[0]))


def apply_undo(db_path: str, transfer_id: int, token: str, snapshot_dir: Path) -> dict:
    def body(conn, made):
        plan, row = _undo_plan(conn, transfer_id)
        if plan["token"] != token:
            raise TransferError("The records changed since the preview. Preview the undo again.")
        # All or nothing: a partly undone transfer could not be finished later,
        # because it is marked undone.
        if plan["stay_count"]:
            raise TransferError(
                f"{plan['stay_count']} row(s) cannot move back because the original account "
                "has matching records again. Resolve those first; nothing was changed."
            )
        if plan["move_count"] == 0:
            raise TransferError("Nothing can be moved back; the transfer stays as it is.")
        src, dst = row["to_user_id"], row["from_user_id"]

        def journal_write():
            undo_record = {
                "moved": plan["_move_ids"],
                "left": plan["_stay_ids"],
                "missing": {t: v["missing"] for t, v in plan["tables"].items()},
            }
            cur = conn.execute(
                "UPDATE user_transfers SET undone_at = CURRENT_TIMESTAMP, undo_json = ? "
                "WHERE id = ? AND undone_at IS NULL",
                (json.dumps(undo_record), transfer_id),
            )
            if cur.rowcount != 1:
                raise TransferError("This transfer was already undone.")
            return transfer_id

        _verified_move(
            conn, db_path, snapshot_dir, f"undo{transfer_id}", plan, src, dst, journal_write, made)
        return plan

    plan, snapshot = _commit_with_snapshot(db_path, snapshot_dir, body)
    logger.info(
        "Hikaru user transfer #%d undone: moved back %d row(s), left %d",
        transfer_id, plan["move_count"], plan["stay_count"],
    )
    result = public_plan(plan)
    result["snapshot"] = snapshot.name
    return result


def list_transfers(db_path: str, limit: int = 25) -> list[dict]:
    def body(conn):
        if "user_transfers" not in _tables(conn):
            return []
        out = []
        for r in conn.execute(
            "SELECT * FROM user_transfers ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall():
            moved = json.loads(r["moved_json"])
            left = json.loads(r["left_json"])
            out.append({
                "id": r["id"],
                "source_id": str(r["from_user_id"]),
                "target_id": str(r["to_user_id"]),
                "performed_at": r["performed_at"],
                "moved": {t: len(v) for t, v in moved.items()},
                "left": {t: len(v) for t, v in left.items()},
                "undone_at": r["undone_at"],
            })
        return out
    return _in_read_transaction(db_path, body)
