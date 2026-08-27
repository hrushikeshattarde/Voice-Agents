"""
Account managers — who a rep's practice report gets mailed to.

Same editable-data-file pattern as the customer profiles: a sales manager adds
themselves by editing `data/managers.toml`, no code change. And the same
validation posture: a malformed entry is refused at load with the field named,
because the failure it prevents — a report silently mailed to a typo — is the
kind nobody notices until a quarter of feedback has gone to nowhere.

Unlike the profiles, the FILE ITSELF is optional: a desk that hasn't set up
managers yet gets an empty roster and a dashboard that says so, not a crash.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

_MANAGERS_FILE = Path(__file__).parent / "data" / "managers.toml"


@dataclass(frozen=True)
class AccountManager:
    id: str
    name: str
    email: str

    def card(self) -> dict:
        return {"id": self.id, "name": self.name, "email": self.email}


def load_managers(path: str | Path | None = None) -> dict[str, AccountManager]:
    """Managers keyed by id; empty when the file is missing or lists none."""
    path = Path(path) if path is not None else _MANAGERS_FILE
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path.name}: not valid TOML — {exc}") from exc
    managers: dict[str, AccountManager] = {}
    for entry in data.get("managers") or []:
        if not isinstance(entry, dict):
            raise ValueError(f"{path.name}: every [[managers]] entry must be a table")
        for field in ("id", "name", "email"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{path.name}: manager {field!r} is required and must be "
                    "non-empty text")
        email = entry["email"].strip()
        if "@" not in email or " " in email:
            # Not full RFC validation — just the typo class that would mail a
            # quarter of reports to nowhere.
            raise ValueError(f"{path.name}: {email!r} does not look like an "
                             "email address")
        manager = AccountManager(id=entry["id"].strip(), name=entry["name"].strip(),
                                 email=email.lower())
        if manager.id in managers:
            raise ValueError(f"{path.name}: duplicate manager id {manager.id!r}")
        managers[manager.id] = manager
    return managers
