"""Load lookup service (PRD §3 step 2)."""

from __future__ import annotations

from dataclasses import dataclass

from lanevoice.db.repository import Repository
from lanevoice.domain.models import Load


@dataclass(frozen=True)
class LoadLookup:
    found: bool
    posted: bool
    available: bool   # open status
    load: Load | None


class LoadService:
    def __init__(self, repo: Repository):
        self._repo = repo

    def lookup(self, load_id: str) -> LoadLookup:
        load = self._repo.get_load(load_id)
        if load is None:
            return LoadLookup(found=False, posted=False, available=False, load=None)
        return LoadLookup(
            found=True, posted=load.is_posted, available=load.is_open, load=load
        )

    def open_load_ids(self) -> list[str]:
        return [load.load_id for load in self._repo.open_loads()]

    def open_loads_summary(self) -> str:
        """Real lanes for the open loads so the agent doesn't invent them.
        e.g. 'L1002 (Atlanta to Miami), L1003 (LA to Phoenix)'."""
        parts = []
        for load in self._repo.open_loads():
            origin = load.origin.split(",")[0]
            dest = load.destination.split(",")[0]
            parts.append(f"{load.load_id} ({origin} to {dest})")
        return ", ".join(parts)
