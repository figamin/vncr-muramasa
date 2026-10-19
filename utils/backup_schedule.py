"""Shared scheduling utilities for deterministic backup runs."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Tuple

BACKUP_INTERVAL_HOURS: int = 4
BACKUP_INTERVAL: timedelta = timedelta(hours=BACKUP_INTERVAL_HOURS)
BACKUP_RUN_TIMES: Tuple[time, ...] = tuple(time(hour=hour) for hour in range(0, 24, BACKUP_INTERVAL_HOURS))

def next_backup_run_after(reference: datetime) -> datetime:
    """Return the next scheduled backup run strictly after ``reference``.

    The calculation assumes backups occur every ``BACKUP_INTERVAL_HOURS`` starting at
    midnight. The original timezone information of ``reference`` is preserved.
    """

    tzinfo = reference.tzinfo
    date = reference.date()

    for slot in BACKUP_RUN_TIMES:
        candidate = datetime.combine(date, slot)
        if tzinfo is not None and slot.tzinfo is None:
            candidate = candidate.replace(tzinfo=tzinfo)
        if candidate > reference:
            return candidate

    next_day = date + timedelta(days=1)
    candidate = datetime.combine(next_day, BACKUP_RUN_TIMES[0])
    if tzinfo is not None and BACKUP_RUN_TIMES[0].tzinfo is None:
        candidate = candidate.replace(tzinfo=tzinfo)
    return candidate
