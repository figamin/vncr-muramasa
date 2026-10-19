"""
Message word-filter engine for muramasa.

Pure and discord-free so the matching/replacement logic can be unit-tested and
so the cog and the web console share one implementation. Both the slash command
and the console validate patterns through validate_pattern before storing, and
the cog compiles rules through compile_rules, so a broken regex can never reach
the live message pipeline: it is skipped, never raised.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---- Invisible author marker (shared: the filter cog, highlights, purge previews) ----
# A webhook repost is authored by the webhook, not the member, so Discord search
# and the purge (both key off the real author) can't find it by user. The cog
# appends this INVISIBLE masked link carrying the poster's id at the end of the
# repost content, after one space: the id sits in the URL (so searching that id in
# Discord surfaces the repost and the purge can find + delete it) while the link
# renders as nothing. It stays invisible four ways: the label is U+2800 (the one
# blank char Discord accepts as a valid masked-link label), the URL is angle-
# bracketed so it can't unfurl a link embed, the host is off Discord because the
# client draws a profile card for any discord.com/users link regardless of the
# brackets, and the leading space stops a trailing URL from swallowing it. The
# host uses the reserved .invalid TLD so the link is plainly a tag and never
# resolves to a real page; the id is its own path segment so search tokenizes it.
# strip_author_marker removes it wherever repost content is re-displayed (edit
# box, reply quotes, highlights, purge previews).
# ---- Masked-link defanging (shared: the filter cog, onboarding) ----


def defang_masked_links(text: str) -> str:
    """Break masked-link markdown [label](url) so it can't render as a clickable link whose
    label hides the destination. A zero-width space between ']' and '(' is enough and, unlike
    escaping every bracket, leaves standalone brackets, code spans, and kaomoji untouched,
    which matters wherever the text is Japanese. Idempotent (the split pair no longer
    matches), so re-applying on an edit can't accumulate corruption."""
    return re.sub(r"\]\(", "]​(", text)


AUTHOR_MARKER_LABEL = "⠀"  # Braille pattern blank


def author_marker(author_id: int) -> str:
    return f"[{AUTHOR_MARKER_LABEL}](<https://author.invalid/{author_id}>)"


# Matches the trailing marker plus the space that precedes it. Also matches the
# discord.com/users form, which older reposts still carry.
AUTHOR_MARKER_RE = re.compile(
    r"\s*\[[^\]]*\]\(<https://(?:author\.invalid|discord\.com/users)/\d+>\)\s*$")


def strip_author_marker(content: Optional[str]) -> str:
    return AUTHOR_MARKER_RE.sub("", content or "")


# Hard cap applied to a message before rules run. Discord already caps message
# length far below this, so it never trims real content; it only bounds work if
# a pathological pattern meets an unusually large paste.
MAX_CONTENT_LENGTH = 8000

# Regex matching runs synchronously on the bot's event loop and Python's `re` has
# no timeout, so a pattern with catastrophic backtracking would freeze the
# whole bot. The common shape is rejected at rule-creation time: a quantifier
# applied to a group that itself contains an unbounded quantifier, e.g. (a+)+,
# (a*)*, (\w+)+, (.*)*. This is a heuristic (exact detection is undecidable),
# but it blocks the shapes admins actually type by accident. Only regex-mode rules
# are checked; simple-mode patterns are escaped so they cannot backtrack.
MAX_PATTERN_LENGTH = 1000
_NESTED_QUANTIFIER = re.compile(r"\([^()]*[*+][^()]*\)\s*[*+{]")


@dataclass
class CompiledRule:
    rule_id: int
    pattern: "re.Pattern[str]"
    replacement: str


def plural_forms(word: str) -> List[str]:
    """Return a simple word plus its common English plural, longest first so the
    plural (longer) form is preferred over the singular prefix in an alternation.
    Handles the frequent cases: consonant+y -> ies (story -> stories), s/x/z/ch/sh
    -> es (box -> boxes), everything else -> s (cat -> cats). Irregular plurals
    (leaf/leaves, person/people) are not generated; use regex mode or add the form
    explicitly for those."""
    forms = [word]
    lw = word.lower()
    if len(lw) >= 2 and lw.endswith("y") and lw[-2] not in "aeiou":
        forms.append(word[:-1] + "ies")
    elif lw.endswith(("s", "x", "z", "ch", "sh")):
        forms.append(word + "es")
    else:
        forms.append(word + "s")
    return sorted(set(forms), key=len, reverse=True)


def build_pattern(pattern: str, is_regex: bool, ignore_case: bool, whole_word: bool,
                  match_plurals: bool = False) -> "re.Pattern[str]":
    """Compile one rule. Simple rules are escaped and optionally wrapped in word
    boundaries; regex rules are compiled verbatim so capture groups work. When
    match_plurals is set (simple mode only), the pattern also matches the word's
    plural form."""
    flags = re.IGNORECASE if ignore_case else 0
    if is_regex:
        return re.compile(pattern, flags)
    if match_plurals:
        body = "(?:" + "|".join(re.escape(f) for f in plural_forms(pattern)) + ")"
    else:
        body = re.escape(pattern)
    if whole_word:
        body = rf"\b{body}\b"
    return re.compile(body, flags)


def compile_rules(rows: Iterable[Dict[str, Any]]) -> List[CompiledRule]:
    """Compile rule rows (dicts from the DB) into CompiledRule objects. A row
    whose pattern will not compile is logged and skipped so one bad rule cannot
    take down the whole filter."""
    compiled: List[CompiledRule] = []
    for row in rows:
        try:
            pattern = build_pattern(
                row["pattern"],
                bool(row.get("is_regex")),
                bool(row.get("ignore_case", 1)),
                bool(row.get("whole_word", 0)),
                bool(row.get("match_plurals", 0)),
            )
        except re.error as exc:
            logger.warning("Skipping message filter rule %s (bad pattern): %s", row.get("id"), exc)
            continue
        compiled.append(CompiledRule(
            rule_id=int(row["id"]),
            pattern=pattern,
            replacement=row.get("replacement") or "",
        ))
    return compiled


def apply_rules(content: str, compiled: Iterable[CompiledRule]) -> Tuple[str, List[int]]:
    """Apply each compiled rule in order. Returns the cleaned content and the ids
    of rules that changed something (empty list means the message is untouched)."""
    if not content:
        return content or "", []
    result = content[:MAX_CONTENT_LENGTH]
    matched: List[int] = []
    for rule in compiled:
        try:
            new_result, count = rule.pattern.subn(rule.replacement, result)
        except re.error as exc:
            logger.warning("Message filter rule %s failed while applying: %s", rule.rule_id, exc)
            continue
        if count and new_result != result:
            matched.append(rule.rule_id)
            result = new_result
    return result, matched


def validate_pattern(pattern: str, is_regex: bool, replacement: str,
                     ignore_case: bool = True, whole_word: bool = False,
                     match_plurals: bool = False) -> Tuple[bool, Optional[str]]:
    """Check a rule before it is stored. Compiles the pattern and runs a trial
    substitution so invalid replacement templates (e.g. a backreference to a
    group that does not exist) are rejected too, not just malformed patterns."""
    if not pattern:
        return False, "Pattern cannot be empty."
    try:
        compiled = build_pattern(pattern, is_regex, ignore_case, whole_word, match_plurals)
    except re.error as exc:
        return False, f"Invalid regex pattern: {exc}"
    if is_regex:
        if len(pattern) > MAX_PATTERN_LENGTH:
            return False, f"Pattern is too long (max {MAX_PATTERN_LENGTH} characters)."
        if _NESTED_QUANTIFIER.search(pattern):
            return False, (
                "This regex has a nested quantifier (like (x+)+ or (.*)* ) that can cause catastrophic "
                "backtracking and freeze the bot. Rewrite it without a quantifier inside a quantified group."
            )
    try:
        # The replacement template is validated against the pattern here even
        # though the sample does not match, so bad group refs are caught.
        compiled.sub(replacement or "", "muramasa filter validation sample")
    except re.error as exc:
        return False, f"Invalid replacement: {exc}"
    return True, None
