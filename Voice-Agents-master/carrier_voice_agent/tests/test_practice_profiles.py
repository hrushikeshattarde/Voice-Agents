"""Practice profiles: the personas are DATA, edited by non-programmers.

A sales manager sharpening an objection in a TOML file must not be able to
take practice mode down in a way that surfaces mid-call. These tests hold the
loader to its contract: the shipped set always loads whole, and any malformed
file is refused at load time with the file and field named. The other contract
is the answer key: what the browser can see must never include the hidden
facts and triggers a rep is supposed to earn through discovery.
"""

from __future__ import annotations

import json

import pytest

from lanevoice.practice.profiles import _LIST_FIELDS, _STR_FIELDS, load_profiles, profile_cards

# The eight moods the feature shipped with. A rename or an added ninth is fine —
# this asserts the set stays deliberate, not accidental.
EXPECTED_IDS = {
    "brush_off", "rate_shopper", "burned_shipper", "busy_operator",
    "chatty_noncommitter", "skeptical_negotiator", "gatekeeper", "loyal_incumbent",
}

# What the browser is allowed to know before the call. Everything else in the
# profile is the answer key.
CARD_KEYS = {"id", "name", "persona_name", "title", "company", "vertical",
             "difficulty", "blurb", "win_condition"}


# A minimal valid profile for the negative tests. JSON scalars/arrays are valid
# TOML values, so json.dumps is the writer.
def _write(directory, stem: str, drop: str | None = None, **overrides):
    data = {f: f"{f} text" for f in _STR_FIELDS}
    data.update({f: [f"{f} item"] for f in _LIST_FIELDS})
    data.update({"id": stem, "difficulty": "easy"})
    data.update(overrides)
    if drop:
        del data[drop]
    body = "\n".join(f"{k} = {json.dumps(v)}" for k, v in data.items())
    (directory / f"{stem}.toml").write_text(body, encoding="utf-8")


# ------------------------------------------------------------ shipped set #
def test_the_shipped_set_loads_and_covers_the_eight_moods():
    profiles = load_profiles()
    assert set(profiles) == EXPECTED_IDS
    # Every difficulty tier is represented — a rep needs a path up.
    assert {p.difficulty for p in profiles.values()} == {"easy", "medium", "hard"}


def test_the_browser_cards_never_carry_the_answer_key():
    profiles = load_profiles()
    for card in profile_cards(profiles):
        assert set(card) == CARD_KEYS
    # And none of the hidden material leaks through a card VALUE either: spot-check
    # facts a rep is supposed to earn — the gatekeeper's real path in, Victor's
    # RFP, Tanya's season.
    flat = json.dumps(profile_cards(profiles)).lower()
    for secret in ("whitfield", "rfp", "roofing"):
        assert secret not in flat


def test_cards_are_ordered_easy_first():
    order = [c["difficulty"] for c in profile_cards(load_profiles())]
    rank = {"easy": 0, "medium": 1, "hard": 2}
    assert order == sorted(order, key=rank.__getitem__)


# --------------------------------------------------------------- validation #
def test_a_missing_field_is_refused_with_the_file_and_field_named(tmp_path):
    _write(tmp_path, "broken", drop="win_condition")
    with pytest.raises(ValueError, match=r"broken\.toml.*win_condition"):
        load_profiles(tmp_path)


def test_an_empty_hidden_facts_list_is_refused(tmp_path):
    _write(tmp_path, "hollow", hidden_facts=[])
    with pytest.raises(ValueError, match=r"hollow\.toml.*hidden_facts"):
        load_profiles(tmp_path)


def test_an_unknown_field_is_refused_as_a_probable_typo(tmp_path):
    # A typo'd `hangup_triggers` would otherwise mean a customer who never
    # hangs up — silently, which is the worst way.
    _write(tmp_path, "typo", hangup_trigers=["x"])
    with pytest.raises(ValueError, match=r"typo\.toml.*hangup_trigers"):
        load_profiles(tmp_path)


def test_an_unknown_difficulty_is_refused(tmp_path):
    _write(tmp_path, "odd", difficulty="brutal")
    with pytest.raises(ValueError, match=r"odd\.toml.*difficulty"):
        load_profiles(tmp_path)


def test_an_id_that_disagrees_with_the_filename_is_refused(tmp_path):
    # The filename is how humans find a profile; the id is how code does.
    # A copied file must not silently shadow the one it was copied from.
    _write(tmp_path, "copy_of_brush_off", id="brush_off")
    with pytest.raises(ValueError, match=r"copy_of_brush_off\.toml.*brush_off"):
        load_profiles(tmp_path)


def test_broken_toml_is_refused_with_the_file_named(tmp_path):
    (tmp_path / "mangled.toml").write_text("id = 'unclosed", encoding="utf-8")
    with pytest.raises(ValueError, match=r"mangled\.toml"):
        load_profiles(tmp_path)


def test_an_empty_profile_directory_is_refused(tmp_path):
    # No profiles is a packaging failure (the wheel dropped the data files),
    # and it must stop the dashboard at boot, not 404 at click time.
    with pytest.raises(ValueError, match="No practice profiles"):
        load_profiles(tmp_path)
