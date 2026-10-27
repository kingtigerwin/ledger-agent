from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class InvalidTimeZone(ValueError):
    """The UI sent a time zone that isn't an IANA name."""


def today_in(time_zone: str, now: datetime) -> date:
    """The customer's calendar date at `now`, so "today" in their message means the right day."""
    try:
        zone = ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise InvalidTimeZone(f"unknown time zone: {time_zone!r}") from err
    return now.astimezone(zone).date()
