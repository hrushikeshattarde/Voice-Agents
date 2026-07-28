"""
Turning stored values into the words a rep actually says.

Everything here exists because a fact that reads fine in a database reads like a
machine over a phone line. "2026-07-31" is the clearest example: spoken verbatim
it comes out as "two thousand twenty-six dash zero seven dash thirty-one".
"""

from __future__ import annotations

import datetime


def _ordinal(day: int) -> str:
    if 11 <= day <= 13:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }"


def spoken_date(value: str | None, today: object | None = None) -> str | None:
    """Render an ISO date the way it gets said out loud on a call.

    Today and tomorrow are named as such — that is what a carrier needs to hear
    first, because it decides whether they can even take the load. Inside the
    coming week it's the weekday alone ("Friday"); past that, weekday plus date
    ("Friday, July 31st"). A yesterday-or-older date is still rendered in full so
    a stale posting sounds obviously stale instead of quietly plausible.

    Anything we can't parse comes back unchanged. A malformed date in the feed
    should read oddly, not take the call down mid-sentence.
    """
    if not value:
        return None
    try:
        when = datetime.date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return str(value)

    if isinstance(today, datetime.datetime):
        today = today.date()
    if not isinstance(today, datetime.date):
        today = datetime.date.today()

    delta = (when - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    full = f"{when.strftime('%A')}, {when.strftime('%B')} {_ordinal(when.day)}"
    if 2 <= delta <= 6:
        return when.strftime("%A")
    return full


def spell_digits(value: str) -> str:
    """Read a reference number back one digit at a time.

    Confirming "2519181" as "two million five hundred..." is useless to a caller
    checking it against a posting; they need it grouped as digits, the way the
    number was read to them in the first place.
    """
    return "-".join(ch for ch in str(value) if ch.isalnum())
