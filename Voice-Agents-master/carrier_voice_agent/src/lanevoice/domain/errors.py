"""Failures the conversation layer has to react to, independent of the backend."""

from __future__ import annotations


class SourceUnavailable(RuntimeError):
    """The system of record could not answer, so we do not know the answer.

    Raised by a repository instead of returning "not found", because the agent
    says those two things very differently: "there's no load 1303369 on the
    board" is a statement of fact, and making it because an API timed out is
    telling a carrier something untrue about their freight.

    The conversation layer catches this and hands the call to a rep, which is the
    honest outcome when we cannot see our own board.
    """


class LoadOutOfScope(RuntimeError):
    """The load exists, but it is another office's freight — not this desk's.

    Raised by a scoped repository instead of returning "not found", because the
    two deserve different calls. A number that finds nothing is usually a
    mishear or a stale posting, so the agent asks for another number (up to a
    cap). Another office's load is DECIDED: no retry can change whose desk it
    is, so the agent thanks the caller and ends the call on the first hit —
    observed live, the collapse into "not found" sent a caller hunting through
    their posting for a different number that could never have worked.
    """
