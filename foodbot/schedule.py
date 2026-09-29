from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def utcnow():
    return datetime.now(UTC)


def stamp(dt):
    return dt.astimezone(UTC).isoformat()


def parse(value):
    return datetime.fromisoformat(value)


def local_slot(now, settings, days=0):
    local = now.astimezone(ZoneInfo(settings.timezone))
    hour, minute = map(int, settings.checkin_time.split(":"))
    target = datetime.combine(local.date() + timedelta(days=days),
                              datetime.min.time(), ZoneInfo(settings.timezone))
    target = target.replace(hour=hour, minute=minute)
    # Normalize nonexistent local times (spring DST jump) to a real instant.
    return target.astimezone(UTC)


def next_weekly(now, settings):
    local = now.astimezone(ZoneInfo(settings.timezone))
    days = (settings.day - local.weekday()) % 7
    target = local_slot(now, settings, days)
    return target if target > now else local_slot(now, settings, days + 7)


def next_daily(now, settings):
    target = local_slot(now, settings)
    return target if target > now else local_slot(now, settings, 1)


def is_quiet(now, settings):
    hour = now.astimezone(ZoneInfo(settings.timezone)).hour
    start, end = settings.quiet_start, settings.quiet_end
    return start <= hour < end if start < end else hour >= start or hour < end


def pretty(value, settings):
    return parse(value).astimezone(ZoneInfo(settings.timezone)).strftime("%a, %b %d at %I:%M %p %Z")
