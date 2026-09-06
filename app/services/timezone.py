from datetime import datetime
from zoneinfo import ZoneInfo

SA_TIMEZONE = ZoneInfo("Africa/Johannesburg")


def local_now():
    """Current South African wall-clock time stored as naive ISO text."""
    return datetime.now(SA_TIMEZONE).replace(tzinfo=None)


def local_now_iso(timespec="seconds"):
    return local_now().isoformat(timespec=timespec)


def parse_iso_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone(SA_TIMEZONE).replace(tzinfo=None)
    return parsed


def display_local_datetime(value):
    parsed = parse_iso_datetime(value)
    if parsed:
        return parsed.strftime("%Y-%m-%d  %H:%M")
    text = str(value or "").strip()
    if not text:
        return "—"
    return text.replace("T", "  ")


def display_local_date(value):
    parsed = parse_iso_datetime(value)
    if parsed:
        return parsed.date().isoformat()
    text = str(value or "").strip()
    return text[:10] if text else "—"
