"""Operations dashboard — a HappyRobot-style web UI over the call audit trail.

Serves the local SQLite audit database (calls, offers, notes, transfers) plus a
browser playground that drives the real `CarrierSalesAgent` — the same class the
phone worker runs, through the same repository path. Stdlib HTTP only: the
dashboard must start with `make demo`'s dependencies, no keys and no installs.
"""

from lanevoice.dashboard.queries import DashboardQueries
from lanevoice.dashboard.sessions import SessionManager

__all__ = ["DashboardQueries", "SessionManager"]
