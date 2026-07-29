"""
Who takes a handed-over call, and what gets dialled to reach them (PRD §9.5).

`TransferService` answers the first question from the load; `telephony.transfer`
answers the second. They are tested together because the failure they exist to
prevent spans both: a carrier told to hold for the rep on their load, and then
either sat in silence or connected to somebody else's desk.
"""

import pytest

from lanevoice.domain.models import (
    Load,
    LoadStatus,
    Rep,
    TransferResolution,
)
from lanevoice.services import TransferService
from lanevoice.telephony.transfer import dial_plan, dial_target


def _load(rep_id):
    return Load(
        load_id="L9001", origin="Chicago, IL", destination="Dallas, TX",
        pickup_date="2026-08-03", equipment="Dry Van", weight_lbs=42000,
        open_rate=2000, ceiling_rate=2500, fraud_low_rate=1400,
        assigned_rep_id=rep_id, status=LoadStatus.OPEN,
    )


class _Reps:
    """A repository stub holding just the two things a transfer reads."""

    def __init__(self, reps=(), free=()):
        self._reps = {r.rep_id: r for r in reps}
        self._free = [free] if isinstance(free, Rep) else list(free)
        self.excluded = None

    def get_rep(self, rep_id):
        return self._reps.get(rep_id)

    def available_rep(self, exclude_rep_id=None):
        self.excluded = exclude_rep_id
        free = [r for r in self._free if r.rep_id != exclude_rep_id]
        return free[0] if free else None


OWNER = Rep("2423", "Lucas Piqueras", "+13123007447", True,
            title="Carrier Account Manager", extension="8754")
FREE = Rep("R02", "Mike Torres", "+15551110102", True)


# --------------------------------------------------------------------------- #
# Who takes the call
# --------------------------------------------------------------------------- #
def test_the_loads_own_rep_takes_the_call():
    resolution = TransferService(_Reps([OWNER], free=FREE)).resolve(_load("2423"))
    assert resolution.rep is OWNER
    assert resolution.is_fallback is False
    assert resolution.assigned_rep is OWNER


def test_a_load_with_no_assigned_rep_falls_back_to_whoever_is_free():
    repo = _Reps(free=FREE)
    resolution = TransferService(repo).resolve(_load(None))
    assert resolution.rep is FREE
    assert resolution.is_fallback is True
    assert resolution.assigned_rep is None
    assert resolution.note == "load_has_no_assigned_rep"


def test_an_unreachable_owner_still_gets_the_carrier_to_a_person():
    """The owner is recorded either way: they are who has to ring the carrier
    back, and a fallback handoff that loses their name loses that."""
    unreachable = Rep("2423", "Lucas Piqueras", "", False)
    repo = _Reps([unreachable], free=FREE)
    resolution = TransferService(repo).resolve(_load("2423"))
    assert resolution.rep is FREE
    assert resolution.assigned_rep is unreachable
    assert resolution.note == "assigned_rep_has_no_number"
    # And the fallback search does not hand the call back to the same person.
    assert repo.excluded == "2423"


def test_a_rep_the_system_of_record_lost_does_not_end_the_call():
    """`get_rep` returning None is the shape of a failed user lookup. A carrier is
    on the line, so it resolves to somebody rather than to nothing — and says which
    of the two things went wrong, because an outage and an unassigned load are
    chased by different people."""
    resolution = TransferService(_Reps(free=FREE)).resolve(_load("2423"))
    assert resolution.rep is FREE
    assert resolution.note == "assigned_rep_not_found"


def test_a_call_with_no_load_can_still_reach_a_person():
    """Asking for a rep before saying which load, or a dead board: neither is a
    reason to hang up on somebody."""
    resolution = TransferService(_Reps(free=FREE)).resolve(None)
    assert resolution.rep is FREE
    assert resolution.note == "no_load_identified"


def test_nobody_free_is_a_callback_never_a_disconnect():
    resolution = TransferService(_Reps()).resolve(_load(None))
    assert resolution.rep is None
    assert resolution.note == "voicemail_plus_callback_task"


# --------------------------------------------------------------------------- #
# What gets dialled
# --------------------------------------------------------------------------- #
def test_a_reps_number_becomes_a_tel_destination():
    assert dial_target(OWNER) == "tel:+13123007447"


def test_an_extension_does_not_travel_in_the_destination():
    """It cannot: a SIP transfer takes a single address. The switchboard is what
    gets dialled and the extension stays on the record for the note."""
    assert "8754" not in dial_target(OWNER)
    assert OWNER.spoken_phone.endswith("ext 8754")


@pytest.mark.parametrize("rep", [
    None,
    Rep("R09", "No Number", "", False),
])
def test_there_is_nothing_to_dial_for_a_rep_with_no_number(rep):
    assert dial_target(rep) is None


def test_a_resolution_with_nobody_in_it_dials_nothing():
    assert dial_target(TransferResolution(rep=None, is_fallback=True).rep) is None


# --------------------------------------------------------------------------- #
# Resolution happens once
# --------------------------------------------------------------------------- #
def test_the_same_rep_is_resolved_every_time_for_the_same_load():
    """There is no round robin. A rep who doesn't pick up is not swapped for a
    stranger — the carrier asked for the rep on their load, and that case becomes a
    callback (`CarrierSalesAgent.transfer_declined`) rather than another phone
    ringing."""
    repo = _Reps([OWNER], free=[FREE])
    service = TransferService(repo)
    assert service.resolve(_load("2423")).rep is OWNER
    assert service.resolve(_load("2423")).rep is OWNER


# --------------------------------------------------------------------------- #
# Reaching a rep who sits behind an extension
#
# Transport Pro records the office number as "312-300-7447 ext8754". Dialling only
# the base number reaches a switchboard, so the extension has to travel some other
# way — and which way depends on what answers the call.
# --------------------------------------------------------------------------- #
def test_dtmf_mode_dials_the_number_then_sends_the_extension():
    """For an auto-attendant: the full number, then the digits once it picks up."""
    assert dial_plan(OWNER, "dtmf") == ("+13123007447", "8754")


def test_sip_user_mode_dials_the_extension_itself():
    """For a trunk pointed at your own phone system: `sip:8754@your-pbx`, and the
    PBX rings that desk. No IVR timing to get wrong."""
    assert dial_plan(OWNER, "sip_user") == ("8754", "")


def test_off_mode_reaches_the_switchboard_and_says_so():
    assert dial_plan(OWNER, "off") == ("+13123007447", "")


@pytest.mark.parametrize("mode", ["dtmf", "sip_user", "off"])
def test_a_rep_with_no_extension_dials_the_same_in_every_mode(mode):
    assert dial_plan(FREE, mode) == ("+15551110102", "")


def test_an_unknown_mode_falls_back_to_sending_the_extension():
    """A typo in WHISPER_EXTENSION_MODE must not silently drop the extension and
    put every transfer through to a switchboard."""
    assert dial_plan(OWNER, "typo") == ("+13123007447", "8754")
    assert dial_plan(OWNER, "") == ("+13123007447", "8754")


def test_there_is_nothing_to_dial_without_a_number_or_an_extension():
    assert dial_plan(None, "dtmf") == ("", "")
    assert dial_plan(Rep("R09", "No Number", "", False), "dtmf") == ("", "")
