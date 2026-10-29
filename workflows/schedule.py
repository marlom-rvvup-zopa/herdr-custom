"""Cron-style schedules with same-day catch-up, in local time."""

from datetime import datetime, timedelta

FIELD_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
TICK_GRACE = timedelta(minutes=2)


def parse_field(text, low, high):
    values = set()
    for part in text.split(","):
        rng, _, step = part.partition("/")
        step = int(step) if step else 1
        if rng == "*":
            start, end = low, high
        elif "-" in rng:
            start, end = (int(x) for x in rng.split("-"))
        else:
            start = end = int(rng)
            if step != 1:
                end = high
        if step < 1 or start < low or end > high or start > end:
            raise ValueError("invalid cron field %r (allowed %d-%d)" % (text, low, high))
        values.update(range(start, end + 1, step))
    return values


def parse(expression):
    """Five fields: minute hour day-of-month month day-of-week (0 or 7 is Sunday)."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron expression needs 5 fields, got %r" % expression)
    sets = [parse_field(text, *bounds) for text, bounds in zip(fields, FIELD_RANGES)]
    sets[4] = {v % 7 for v in sets[4]}
    return sets, fields[2] != "*", fields[4] != "*"


def matches(expression, moment):
    (minute, hour, dom, month, dow), dom_restricted, dow_restricted = parse(expression)
    weekday = (moment.weekday() + 1) % 7
    if dom_restricted and dow_restricted:
        day_ok = moment.day in dom or weekday in dow
    else:
        day_ok = moment.day in dom and weekday in dow
    return moment.minute in minute and moment.hour in hour and moment.month in month and day_ok


def due_slot(trigger, now, last_fired):
    """The latest scheduled minute that has not fired yet, or None.

    Without catch_up_until a slot only counts for two minutes after it passes. With
    catch_up_until "HH:MM" a missed slot from earlier the same day still fires until then.
    """
    now = now.replace(second=0, microsecond=0)
    midnight = now.replace(hour=0, minute=0)
    cutoff = trigger.get("catch_up_until")
    if cutoff:
        hour, minute = (int(x) for x in cutoff.split(":"))
        if now > now.replace(hour=hour, minute=minute):
            return None
        earliest = midnight
    else:
        earliest = max(midnight, now - TICK_GRACE)
    slot = now
    while slot >= earliest:
        if matches(trigger["schedule"], slot):
            return None if last_fired and slot <= last_fired else slot
        slot -= timedelta(minutes=1)
    return None


def next_slot(expression, now):
    """The first scheduled minute strictly after now, or None within a year."""
    (minutes, hours, _, _, _), _, _ = parse(expression)
    now = now.replace(second=0, microsecond=0)
    for offset in range(367):
        day = now.replace(hour=0, minute=0) + timedelta(days=offset)
        if not matches(expression, day.replace(hour=min(hours), minute=min(minutes))):
            continue
        for hour in sorted(hours):
            for minute in sorted(minutes):
                slot = day.replace(hour=hour, minute=minute)
                if slot > now:
                    return slot
    return None
