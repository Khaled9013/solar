"""Five-field cron with bounded calendar search and explicit DST round-trip validation."""
from datetime import datetime, timedelta, timezone as utc_timezone
from zoneinfo import ZoneInfo


def field(spec, low, high, sunday=False):
    result = set()
    for item in spec.split(","):
        base, slash, step_text = item.partition("/")
        step = int(step_text) if slash else 1
        if not 1 <= step <= high + 1:
            raise ValueError("Invalid cron step")
        if base == "*":
            first, last = low, high
        elif "-" in base:
            first, last = (int(n) for n in base.split("-", 1))
        else:
            first = int(base)
            last = high if slash else first
        if not low <= first <= last <= high:
            raise ValueError("Cron value is outside its allowed range")
        result.update(range(first, last + 1, step))
    if sunday and 7 in result:
        result.remove(7)
        result.add(0)
    return result


def parse(expression):
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("Cron requires five fields: minute hour day month weekday")
    values = [field(parts[0], 0, 59), field(parts[1], 0, 23), field(parts[2], 1, 31),
              field(parts[3], 1, 12), field(parts[4], 0, 7, sunday=True)]
    return parts, values


def next_cron(expression, after, timezone="UTC"):
    parts, (minutes, hours, days, months, weekdays) = parse(expression)
    zone = ZoneInfo(timezone)
    if after.tzinfo is None:
        raise ValueError("Cron start must include a timezone")
    date = after.astimezone(zone).date()
    for _ in range(366 * 5):
        day, weekday = date.day in days, (date.weekday() + 1) % 7 in weekdays
        day_match = (day and weekday) if parts[2].startswith("*") or parts[4].startswith("*") else (day or weekday)
        if date.month in months and day_match:
            matches = []
            for hour in sorted(hours):
                for minute in sorted(minutes):
                    wall = datetime(date.year, date.month, date.day, hour, minute)
                    for fold in (0, 1):
                        candidate = wall.replace(tzinfo=zone, fold=fold).astimezone(utc_timezone.utc)
                        local = candidate.astimezone(zone)
                        if candidate > after and local.replace(tzinfo=None) == wall and local.fold == fold:
                            matches.append(candidate)
            if matches:
                return min(matches)
        date += timedelta(days=1)
    raise ValueError("Cron has no occurrence within the next five years")
