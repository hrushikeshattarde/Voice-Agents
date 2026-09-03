"""
The rep directory — who a call gets handed to when the agent can't finish it.

Reps are a name, a phone and an availability flag, kept in a TOML file the desk
edits (`REPS_FILE`, `reps.toml` next to the database by default):

    [[reps]]
    id = "jsmith"
    name = "Jordan Smith"
    phone = "+12605551234"
    available = true

Same editable-data-file pattern as the practice managers, same validation
posture: a malformed entry is refused at load with the field named, because the
failure it prevents — a caller told they are being put through to somebody who
does not exist — is exactly what the sample reps used to do. Until this file
existed the transfer list was three invented people from the seed data, and a
live carrier was told "let me get you over to Sarah Chen".

The FILE is the source of truth for the `reps` table: when it exists, the table
is replaced with its contents at every worker and dashboard start. When it does
not exist the table is left alone — empty on a live deployment, which the agent
handles honestly ("no rep is free right now, I've logged a callback").
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from lanevoice.domain.models import Rep
from lanevoice.logging_config import get_logger

if TYPE_CHECKING:
    from lanevoice.db.database import Database

logger = get_logger(__name__)


def load_reps(path: str | Path) -> list[Rep] | None:
    """The reps listed in `path`, or None when there is no such file.

    None and an empty list mean different things to `sync_reps`: no file leaves
    the table untouched, an empty file clears it.
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path.name}: not valid TOML — {exc}") from exc
    reps: list[Rep] = []
    seen: set[str] = set()
    for entry in data.get("reps") or []:
        if not isinstance(entry, dict):
            raise ValueError(f"{path.name}: every [[reps]] entry must be a table")
        for field in ("id", "name", "phone"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{path.name}: rep {field!r} is required and must be non-empty text")
        phone = entry["phone"].strip()
        digits = phone.lstrip("+")
        if not digits.isdigit() or len(digits) < 10:
            # A transfer target that cannot be dialled is worse than none.
            raise ValueError(f"{path.name}: {phone!r} does not look like a phone "
                             "number (use digits, e.g. +12605551234)")
        available = entry.get("available", True)
        if not isinstance(available, bool):
            raise ValueError(f"{path.name}: rep 'available' must be true or false")
        rep = Rep(rep_id=entry["id"].strip(), name=entry["name"].strip(),
                  phone=phone, available=available)
        if rep.rep_id in seen:
            raise ValueError(f"{path.name}: duplicate rep id {rep.rep_id!r}")
        seen.add(rep.rep_id)
        reps.append(rep)
    return reps


def sync_reps(db: Database, reps: list[Rep] | None) -> None:
    """Make the `reps` table match the directory file. None = no file = no change."""
    if reps is None:
        return
    conn = db.connect()
    try:
        conn.execute("DELETE FROM reps")
        conn.executemany(
            "INSERT INTO reps (rep_id, name, phone, available) VALUES (?,?,?,?)",
            [(r.rep_id, r.name, r.phone, int(r.available)) for r in reps])
        conn.commit()
    finally:
        conn.close()
    logger.info("rep directory: %d rep%s (%d available)", len(reps),
                "" if len(reps) == 1 else "s", sum(r.available for r in reps))
