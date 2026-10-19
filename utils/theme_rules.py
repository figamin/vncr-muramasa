# utils/theme_rules.py
"""Vendored copy of hikaru's rules_json validator (VN_Club_Bot/lib/themes.py).

The web console writes theme rules into HIKARU's DB, which hikaru's /nominate
gate reads. The console can't import hikaru's Python (only its DB volume is
mounted), so this file mirrors hikaru's contract to guarantee both sides agree
on the JSON shape. SOURCE OF TRUTH: VN_Club_Bot/lib/themes.py. If that changes,
update this file and bump RULES_SCHEMA_VERSION in lockstep.

Validation and summary only: evaluating a ruleset needs the VNDB lookups this
process has no path to, so evaluate_theme and tag_probes stay in hikaru.
"""
from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

RULES_SCHEMA_VERSION = 1
_TAG_ID_RE = re.compile(r"^g\d+$")
_PRODUCER_ID_RE = re.compile(r"^p\d+$")


# VNDB length enum labels (1-5). Used in reject copy + rules summary.
LENGTH_LABELS = {1: "Very short", 2: "Short", 3: "Medium", 4: "Long", 5: "Very long"}


# One VNDB tag probe: (tag_id, max_spoiler, min_level, include_children). The
# first three mirror the filter's own argument order; the fourth chooses
# between the `tag` filter, which credits a VN for a descendant of the tag,
# and `dtag`, which wants the tag applied to the VN itself.
TagProbe = tuple[str, int, float, bool]


# Each probe is its own VNDB round trip, so a ruleset's tag count is a direct
# latency cost on every nomination for that period.
MAX_TAG_PROBES = 12


# VNDB tag ratings run 0-3 and reflect how strongly voters think a tag applies.
# A floor near 1 admits VNs where the tag is barely a footnote, so the default
# asks for one voters broadly agreed on. Each tag can override it.
DEFAULT_MIN_TAG_RATING = 2.0


# Exclusion asks a different question than inclusion. "Must have Science
# Fiction" should mean a meaningfully-tagged VN, so it honours the rating
# floor and the spoiler preference. "Must not have <tag>" should catch the
# tag however faintly it applies, so it ignores both unless that tag was
# given its own floor.
_EXCLUDE_MAX_SPOILER = 2
_EXCLUDE_MIN_LEVEL = 0.0


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _validate_int_range(node: object, key: str, lo: Optional[int], hi: Optional[int]) -> dict:
    _require(isinstance(node, dict), f"{key} must be an object with min/max")
    out: dict = {}
    for bound in ("min", "max"):
        val = node.get(bound)
        if val is None:
            out[bound] = None
            continue
        _require(isinstance(val, int) and not isinstance(val, bool), f"{key}.{bound} must be an integer")
        if lo is not None:
            _require(val >= lo, f"{key}.{bound} must be >= {lo}")
        if hi is not None:
            _require(val <= hi, f"{key}.{bound} must be <= {hi}")
        out[bound] = val
    if out["min"] is not None and out["max"] is not None:
        _require(out["min"] <= out["max"], f"{key}.min must be <= {key}.max")
    return out


def _validate_rating(value: object, key: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool),
             f"{key} must be a number")
    _require(0.0 <= float(value) <= 3.0, f"{key} must be between 0 and 3")
    return float(value)


def _validate_id_list(node: object, key: str, id_re: re.Pattern,
                      *, per_entry_rating: bool = False) -> list[dict]:
    _require(isinstance(node, list), f"{key} must be a list")
    out = []
    for item in node:
        _require(isinstance(item, dict) and "id" in item, f"{key} entries need an id")
        item_id = item["id"]
        _require(isinstance(item_id, str) and bool(id_re.match(item_id)),
                 f"{key} id {item_id!r} is not valid")
        entry = {"id": item_id, "name": str(item.get("name") or item_id)}
        # Left absent rather than stamped with the bucket default, so a tag
        # that never had its own threshold keeps following the ruleset's.
        if per_entry_rating and item.get("min_rating") is not None:
            entry["min_rating"] = _validate_rating(item["min_rating"], f"{key} min_rating")
        out.append(entry)
    return out


def _validate_release_str(value: object, key: str) -> Optional[str]:
    if value is None:
        return None
    _require(isinstance(value, str), f"{key} must be a date string")
    _require(_parse_release_date(value) is not None, f"{key} {value!r} is not a valid date")
    return value


def _parse_release_date(s: Optional[str], *, end_of_period: bool = False) -> Optional[date]:
    """Parse a full or partial VNDB release string to a date. Returns None for
    TBA/None/garbage.

    A partial date names a span, and which end of it to take depends on the
    bound: "released after 2010" starts on 1 January, while "released before
    2000" runs to 31 December. Padding an upper bound to the first of the
    period would reject everything published during the year the manager
    named, and VNDB's own `released` filter reads it the same way.
    """
    if not isinstance(s, str):
        return None
    s = s.strip()
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$", s)
    if not m:
        return None
    year = int(m.group(1))
    if m.group(3):
        month, day = int(m.group(2)), int(m.group(3))
    elif m.group(2):
        month = int(m.group(2))
        day = monthrange(year, month)[1] if end_of_period else 1
    else:
        month, day = (12, 31) if end_of_period else (1, 1)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def validate_rules(rules: object) -> dict:
    """Validate + normalize a rules dict. Raises ValueError on any bad shape.
    Returns a canonical dict stamped with schema_version. An empty ruleset is
    allowed (gates nothing) and returns just the version."""
    _require(isinstance(rules, dict), "rules must be an object")
    allowed = {"schema_version", "length_rating", "character_count",
               "developers", "tags", "released", "nsfw_cover"}
    unknown = set(rules) - allowed
    _require(not unknown, f"unknown rule keys: {sorted(unknown)}")

    out: dict = {"schema_version": RULES_SCHEMA_VERSION}

    if "length_rating" in rules:
        out["length_rating"] = _validate_int_range(rules["length_rating"], "length_rating", 1, 5)
    if "character_count" in rules:
        out["character_count"] = _validate_int_range(rules["character_count"], "character_count", 0, None)
    if "developers" in rules:
        node = rules["developers"]
        _require(isinstance(node, dict), "developers must be an object")
        out["developers"] = {"any_of": _validate_id_list(node.get("any_of", []), "developers.any_of", _PRODUCER_ID_RE)}
    if "tags" in rules:
        node = rules["tags"]
        _require(isinstance(node, dict), "tags must be an object")
        tags_out = {
            "all_of": _validate_id_list(node.get("all_of", []), "tags.all_of",
                                        _TAG_ID_RE, per_entry_rating=True),
            "any_of": _validate_id_list(node.get("any_of", []), "tags.any_of",
                                        _TAG_ID_RE, per_entry_rating=True),
            "none_of": _validate_id_list(node.get("none_of", []), "tags.none_of",
                                         _TAG_ID_RE, per_entry_rating=True),
        }
        tags_out["min_rating"] = _validate_rating(
            node.get("min_rating", DEFAULT_MIN_TAG_RATING), "tags.min_rating")
        include_spoilers = node.get("include_spoilers", True)
        _require(isinstance(include_spoilers, bool), "tags.include_spoilers must be a boolean")
        tags_out["include_spoilers"] = include_spoilers
        include_children = node.get("include_children", True)
        _require(isinstance(include_children, bool), "tags.include_children must be a boolean")
        tags_out["include_children"] = include_children
        out["tags"] = tags_out
    if "released" in rules:
        node = rules["released"]
        _require(isinstance(node, dict), "released must be an object")
        out["released"] = {
            "after": _validate_release_str(node.get("after"), "released.after"),
            "before": _validate_release_str(node.get("before"), "released.before"),
        }
    if "nsfw_cover" in rules:
        val = rules["nsfw_cover"]
        _require(val in ("allow", "disallow"), "nsfw_cover must be 'allow' or 'disallow'")
        out["nsfw_cover"] = val

    return out


def include_probe(tags: dict, entry: dict) -> TagProbe:
    """The probe for a tag that must be present: the tag's own rating floor
    when it has one, else the ruleset's."""
    level = entry.get("min_rating")
    if level is None:
        level = tags["min_rating"]
    return (entry["id"], (2 if tags["include_spoilers"] else 0), float(level),
            tags.get("include_children", True))


def exclude_probe(entry: dict) -> TagProbe:
    """The probe for a banned tag. Any application counts unless the tag
    carries an explicit floor, and a descendant of the tag counts too: a
    theme that bans a subject bans its child tags too."""
    level = entry.get("min_rating")
    return (entry["id"], _EXCLUDE_MAX_SPOILER,
            _EXCLUDE_MIN_LEVEL if level is None else float(level), True)


def release_window_phrase(rel: dict) -> str:
    """The release rule as a phrase. One-sided windows are the common case,
    and "any to 2000" reads like a placeholder leaked into the copy."""
    after, before = rel.get("after"), rel.get("before")
    if after and before:
        return f"{after} to {before}"
    if after:
        return f"{after} or later"
    return f"{before} or earlier"


def rules_conflicts(rules: dict) -> list[str]:
    """Rules that are valid but can never be satisfied, or that cost more than
    they're worth. Reported by the editors at save time only. Not raised from
    validate_rules: the gate validates on read, so a stored ruleset must stay
    loadable rather than blocking every nomination for the period."""
    problems: list[str] = []
    tags = rules.get("tags")
    if tags:
        excluded = {t["id"] for t in tags["none_of"]}
        for bucket in ("all_of", "any_of"):
            both = [t["name"] for t in tags[bucket] if t["id"] in excluded]
            if both:
                problems.append(
                    f"{', '.join(both)}: required and excluded at the same time, "
                    "so nothing can pass.")
        total = sum(len(tags[b]) for b in ("all_of", "any_of", "none_of"))
        if total > MAX_TAG_PROBES:
            problems.append(
                f"{total} tags is over the {MAX_TAG_PROBES} this can check per "
                "nomination; trim the list.")
    return problems


def rules_summary(rules: dict) -> str:
    """One-line-per-constraint human summary for dashboards + reject context."""
    parts: list[str] = []
    lr = rules.get("length_rating")
    if lr:
        lo = LENGTH_LABELS.get(lr.get("min"), "any") if lr.get("min") is not None else "any"
        hi = LENGTH_LABELS.get(lr.get("max"), "any") if lr.get("max") is not None else "any"
        parts.append(f"Length: {lo} to {hi}")
    cc = rules.get("character_count")
    if cc:
        lo = f"{cc['min']:,}" if cc.get("min") is not None else "any"
        hi = f"{cc['max']:,}" if cc.get("max") is not None else "any"
        parts.append(f"Characters: {lo} to {hi}")
    dev = rules.get("developers")
    if dev and dev.get("any_of"):
        parts.append("Developer: " + ", ".join(d["name"] for d in dev["any_of"]))
    tags = rules.get("tags")
    if tags:
        def inc_label(t: dict) -> str:
            return f"{t['name']} ({include_probe(tags, t)[2]:g}+)"

        def exc_label(t: dict) -> str:
            level = exclude_probe(t)[2]
            return f"{t['name']} ({level:g}+)" if level else t["name"]

        if tags["all_of"]:
            parts.append("Must have: " + ", ".join(inc_label(t) for t in tags["all_of"]))
        if tags["any_of"]:
            parts.append("Any of: " + ", ".join(inc_label(t) for t in tags["any_of"]))
        if tags["none_of"]:
            parts.append("Excludes: " + ", ".join(exc_label(t) for t in tags["none_of"]))
        # Both settings scope how a required tag is matched, so they only
        # describe a ruleset that requires one. An exclusion sets its own
        # scope and is unaffected by either.
        if tags["all_of"] or tags["any_of"]:
            if not tags["include_spoilers"]:
                parts.append("Spoiler tags don't count toward required tags")
            # Worth stating either way: the same tag name covers a set several
            # times larger with child tags than without, and the reader cannot
            # tell which they are looking at from the tag alone.
            parts.append(
                "Child tags count toward required tags"
                if tags.get("include_children", True)
                else "Child tags don't count: required tags must be applied directly")
    rel = rules.get("released")
    if rel and (rel.get("after") or rel.get("before")):
        parts.append(f"Released: {release_window_phrase(rel)}")
    if rules.get("nsfw_cover") == "disallow":
        parts.append("No NSFW covers")
    return "\n".join(parts) if parts else "No restrictions."


# evaluate_theme fails closed, so its list mixes "breaks the rule" with "the
# answer was unavailable". Both stop a nomination, but only the first is the
# VN's fault, and a caller that reports on someone else's pick has to be able
# to tell them apart. Every unverifiable reason starts with this.
UNVERIFIED_PREFIX = "Could not verify"


def is_unverified(reason: str) -> bool:
    return reason.startswith(UNVERIFIED_PREFIX)


def all_unverified(reasons: list[str]) -> bool:
    """True when nothing here is an actual rule breach, only missing answers."""
    return bool(reasons) and all(is_unverified(r) for r in reasons)
