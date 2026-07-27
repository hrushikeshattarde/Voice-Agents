"""Load lookup service (PRD §3 step 2)."""

from __future__ import annotations

from dataclasses import dataclass

from lanevoice.db.repository import Repository
from lanevoice.domain.models import Load


@dataclass(frozen=True)
class LoadLookup:
    found: bool
    available: bool
    load: Load | None


class LoadService:
    def __init__(self, repo: Repository):
        self._repo = repo

    def lookup(self, load_id: str) -> LoadLookup:
        load = self._repo.get_load(load_id)
        if load is None:
            return LoadLookup(found=False, available=False, load=None)
        return LoadLookup(found=True, available=load.is_open, load=load)

    def open_load_ids(self) -> list[str]:
        return [load.load_id for load in self._repo.open_loads()]
