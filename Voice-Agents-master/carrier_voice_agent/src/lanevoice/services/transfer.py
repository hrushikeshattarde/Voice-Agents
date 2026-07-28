"""Rep lookup + warm-transfer resolution (PRD §3 step 6b / §9.5)."""

from __future__ import annotations

from lanevoice.db.repository import Repository
from lanevoice.domain.models import Load, TransferResolution


class TransferService:
    def __init__(self, repo: Repository):
        self._repo = repo

    def resolve(self, load: Load | None) -> TransferResolution:
        """The rep to hand this call to.

        `load` is optional because a call can need a person before it has a load:
        the caller asks for one, or the board is unreachable and we cannot look
        one up. "I don't know which load this is about" is never a reason to hang
        up on somebody, so a call with no load simply skips the assigned-rep step
        and takes whoever is free.
        """
        assigned = load.assigned_rep_id if load else None
        rep = self._repo.get_rep(assigned) if assigned else None
        if rep and rep.available:
            return TransferResolution(rep=rep, is_fallback=False)

        # Never dead-air disconnect: fall back to any available rep.
        fallback = self._repo.available_rep(exclude_rep_id=assigned)
        if fallback:
            return TransferResolution(rep=fallback, is_fallback=True)

        return TransferResolution(
            rep=None, is_fallback=True, note="voicemail_plus_callback_task"
        )
