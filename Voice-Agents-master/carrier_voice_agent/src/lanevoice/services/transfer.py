"""Rep lookup + warm-transfer resolution (PRD §3 step 6b / §9.5)."""

from __future__ import annotations

from lanevoice.db.repository import Repository
from lanevoice.domain.models import Load, TransferResolution
from lanevoice.logging_config import get_logger

logger = get_logger(__name__)


class TransferService:
    def __init__(self, repo: Repository):
        self._repo = repo

    def resolve(self, load: Load | None) -> TransferResolution:
        """The rep to hand this call to.

        First choice is always the load's own carrier sales rep — the person it is
        assigned to in the system of record (`Load.assigned_rep_id`). That is who a
        caller asking for "the rep on this load" means, and who already knows the
        freight.

        `load` is optional because a call can need a person before it has a load:
        the caller asks for one, or the board is unreachable and we cannot look
        one up. "I don't know which load this is about" is never a reason to hang
        up on somebody, so a call with no load simply skips the assigned-rep step
        and takes whoever is free.

        This resolves ONE rep and is not retried. A rep whose phone we ring and who
        doesn't pick up is not replaced with a different person — the carrier asked
        for the rep on their load, and being passed around three strangers is not
        what they asked for. That case is a callback instead; see
        `CarrierSalesAgent.transfer_declined`.

        Every fallback carries the reason in `note` and the load's real owner in
        `assigned_rep`, so the handoff the carrier gets and the handoff the desk
        reads about afterwards are the same event.
        """
        assigned = load.assigned_rep_id if load else None
        rep = self._repo.get_rep(assigned) if assigned else None
        if rep and rep.available:
            return TransferResolution(rep=rep, is_fallback=False, assigned_rep=rep)

        if rep is not None:
            logger.info(
                "Load %s belongs to %s but they cannot take the call%s — falling "
                "back to an available rep.", load.load_id if load else "n/a",
                rep.name or rep.rep_id,
                " (no dialable number on their record)" if not rep.phone else "")
        why = (
            "assigned_rep_has_no_number" if rep is not None and not rep.phone
            else "assigned_rep_unavailable" if rep is not None
            # The load names a rep and we could not resolve them — a failed user
            # lookup. Told apart from a load that names nobody, because one is an
            # outage to chase and the other is a load that needs assigning.
            else "assigned_rep_not_found" if assigned
            else "load_has_no_assigned_rep" if load is not None
            else "no_load_identified"
        )

        # Never dead-air disconnect: somebody who is free, rather than nobody.
        fallback = self._repo.available_rep(exclude_rep_id=assigned)
        if fallback is not None:
            return TransferResolution(
                rep=fallback, is_fallback=True, note=why, assigned_rep=rep)

        return TransferResolution(
            rep=None, is_fallback=True, note="voicemail_plus_callback_task",
            assigned_rep=rep,
        )
