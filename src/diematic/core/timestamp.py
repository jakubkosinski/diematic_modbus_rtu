"""Building a timestamp from the separate date and time registers of a regulator."""

from __future__ import annotations

from datetime import datetime, tzinfo


def compose_timestamp(
    year: int, month: int, day: int, hour: int, minute: int, timezone: tzinfo
) -> datetime | None:
    """The moment described by the registers, or None if they do not form a valid date.

    The regulator keeps the year as two digits, which are read as 20xx.
    """
    try:
        return datetime(2000 + year, month, day, hour, minute, tzinfo=timezone)
    except ValueError:
        return None
