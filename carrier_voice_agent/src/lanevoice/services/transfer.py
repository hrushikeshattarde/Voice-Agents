"""Rep lookup + warm-transfer resolution (PRD §3 step 6b / §9.5)."""

from __future__ import annotations

from lanevoice.db.repository import Repository
from lanevoice.domain.models import Load, TransferResolution


class TransferService:
    def __init__(self, repo: Repository):
        self._repo = repo

    def resolve(self, load: Load) -> TransferResolution:
        rep = self._repo.get_rep(load.assigned_rep_id) if load.assigned_rep_id else None
        if rep and rep.available:
            return TransferResolution(rep=rep, is_fallback=False)

        # Never dead-air disconnect: fall back to any available rep.
        fallback = self._repo.available_rep(exclude_rep_id=load.assigned_rep_id)
        if fallback:
            return TransferResolution(rep=fallback, is_fallback=True)

        return TransferResolution(
            rep=None, is_fallback=True, note="voicemail_plus_callback_task"
        )
