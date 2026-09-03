"""
Where the agent's loads and carriers come from.

One function, so the worker, the demo and the tests all resolve `DATA_SOURCE`
the same way and nothing else in the codebase has to know which backend is live.

    DATA_SOURCE=transportpro   the live Public API (needs TRANSPORT_PRO_*)
    DATA_SOURCE=sqlite         the offline seed data

The local SQLite database is opened either way: with Transport Pro it holds the
call audit trail (calls, offers, notes, handoffs) and the warm-transfer rep list,
neither of which the Public API has an endpoint for.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from lanevoice.db import Database, Repository
from lanevoice.logging_config import get_logger
from lanevoice.settings import Settings, get_settings

if TYPE_CHECKING:
    from lanevoice.integrations.transportpro import TransportProRepository

logger = get_logger(__name__)


def open_database(settings: Settings | None = None) -> Database:
    """The local database, ready for this deployment's mode.

    Offline (DATA_SOURCE=sqlite) it carries the sample board the demo and the
    tests run against. With Transport Pro as the system of record it is an audit
    trail and a rep directory and NOTHING else: no sample rows are written, and
    any left behind by an earlier offline start or an old `lanevoice-initdb` are
    removed — they are how a live carrier came to be told "let me get you over to
    Sarah Chen", an invented rep. The rep directory is then loaded from
    REPS_FILE when that file exists (relative paths sit next to the database).
    """
    from lanevoice.db.seed import purge_seed
    from lanevoice.reps import load_reps, sync_reps

    settings = settings or get_settings()
    live = settings.uses_transport_pro

    db = Database(settings.db_path)
    db.init(seed=not live)
    if live:
        removed = purge_seed(db)
        if removed:
            logger.warning(
                "removed sample rows left in %s by an offline start: %s — a live "
                "deployment carries no sample loads, carriers or reps", settings.db_path,
                removed)

    reps_path = Path(settings.reps_file)
    if not reps_path.is_absolute():
        reps_path = Path(settings.db_path).resolve().parent / reps_path
    reps = load_reps(reps_path)
    if reps is not None:
        sync_reps(db, reps)
    elif live:
        logger.info(
            "no rep directory at %s: handoffs go to the load's carrier sales rep as "
            "Transport Pro lists them. Copy reps.toml.example to reps.toml to give a "
            "rep a direct number (Transport Pro often has only the office line and an "
            "extension) or to add a fallback pool for loads with no rep.", reps_path)
    return db


def build_repository(
    settings: Settings | None = None,
) -> Repository | TransportProRepository:
    """The repository the conversation layer should use, per `DATA_SOURCE`.

    In Transport Pro mode the real board is the source and the local database is
    the audit trail plus the rep directory — see `open_database` for what is,
    and is not, written into it.
    """
    settings = settings or get_settings()
    live = settings.uses_transport_pro

    db = open_database(settings)
    audit = Repository(db)

    if not live:
        logger.info("Data source: local SQLite seed data (%s)", settings.db_path)
        return audit

    # Imported here so the offline path never needs httpx or a base URL.
    from lanevoice.integrations.transportpro import (
        TransportProClient,
        TransportProRepository,
    )

    settings.require("transport_pro_url", "transport_pro_username",
                     "transport_pro_password")
    client = TransportProClient(settings)
    logger.info("Data source: Transport Pro at %s (audit trail in %s)",
                settings.transport_pro_url, settings.db_path)

    # Two optional enrichments, each independently switched on by having its
    # credentials present. Both are absent by default, and their absence is a
    # DEGRADATION rather than an error — see the log lines, which say plainly what
    # the agent will not be able to do without them.
    highway = None
    if settings.uses_highway:
        from lanevoice.integrations.highway import HighwayClient

        highway = HighwayClient(settings)
        logger.info("Highway enabled: carrier qualifications and cargo insurance "
                    "limits will be checked against %s", settings.highway_api_url)
    else:
        logger.warning(
            "HIGHWAY_API_TOKEN is not set. Transport Pro's carrier_status carries "
            "no classification list, so per-load qualification checks (Critical "
            "Cargo and the like) will fall back to whatever HappyRobot reports, or "
            "be skipped entirely.")

    happyrobot = None
    if settings.uses_happyrobot:
        from lanevoice.integrations.transportpro.happyrobot import HappyRobotClient

        happyrobot = HappyRobotClient(settings)
        logger.info("HappyRobot enabled: bookings will produce a real carrier "
                    "booking link, and PASS carriers can be sent a Highway invite.")
    else:
        logger.warning(
            "HAPPYROBOT_URL / HAPPYROBOT_TOKEN are not set. Agreed rates will be "
            "LOGGED as offers for a rep to action — the agent cannot issue a "
            "booking link, so no call can end in the carrier actually holding the "
            "load.")

    return TransportProRepository(client, audit, settings,
                                  highway=highway, happyrobot=happyrobot)
