"""
Customer profiles — who the rep is pitching to, and what mood they're in.

Each profile is one TOML file in `data/profiles/`, editable without touching
code: a sales manager can sharpen an objection or add a ninth mood by editing
text. That convenience is also the risk — these files feed a system prompt, so
a missing field would surface as a customer with no mood, mid-practice-call,
with nothing in the logs naming the broken file. The loader therefore validates
everything up front and refuses to start with the file and field named.

The split between what the browser sees (`card()`) and what stays server-side
is deliberate: hidden facts, objections and the hang-up triggers are the answer
key. A rep who can read the customer's insides isn't practicing discovery.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

_PROFILE_DIR = Path(__file__).parent / "data" / "profiles"

_DIFFICULTIES = ("easy", "medium", "hard")

# Single-string fields the persona prompt is built from. All required: an empty
# disposition is a customer with no mood, an empty opening_line a call nobody
# answers.
_STR_FIELDS = (
    "id", "name", "persona_name", "title", "company", "vertical", "difficulty",
    "blurb", "disposition", "speech_style", "opening_line", "win_condition",
    "rubric_focus",
)
# List-of-string fields. Also required and non-empty — a Burned Shipper with no
# objections is just a shipper.
_LIST_FIELDS = ("hidden_facts", "objections", "warms_to", "hangup_triggers")


@dataclass(frozen=True)
class CustomerProfile:
    """One customer mood, fully loaded and validated."""

    id: str
    name: str                       # "The Brush-off" — the mood, shown on the card
    persona_name: str               # "Dale Hutchins" — who answers the phone
    title: str
    company: str
    vertical: str                   # freight context: "steel & building products — flatbed"
    difficulty: str                 # easy | medium | hard
    blurb: str                      # card text: what this mood practices
    disposition: str                # the mood itself, in prompt-ready prose
    speech_style: str
    opening_line: str               # how they answer the phone, verbatim
    win_condition: str              # what "the rep earned it" means for this profile
    rubric_focus: str               # what the judge weighs extra for this mood (phase 2)
    hidden_facts: tuple[str, ...]   # pain the rep can uncover with good discovery
    objections: tuple[str, ...]
    warms_to: tuple[str, ...]
    hangup_triggers: tuple[str, ...]

    def card(self) -> dict:
        """What the profile picker in the browser gets.

        The coaching answer key — hidden_facts, objections, warm-up and hang-up
        triggers, speech_style — never leaves the server. The win_condition IS
        shown: the rep should know what they're playing for, just not how the
        customer will resist getting there.
        """
        return {
            "id": self.id,
            "name": self.name,
            "persona_name": self.persona_name,
            "title": self.title,
            "company": self.company,
            "vertical": self.vertical,
            "difficulty": self.difficulty,
            "blurb": self.blurb,
            "win_condition": self.win_condition,
        }


def load_profiles(directory: str | Path | None = None) -> dict[str, CustomerProfile]:
    """Every profile in `directory` (default: the shipped set), keyed by id.

    Raises ValueError naming the file and field on the first problem — this is
    meant to stop the dashboard at startup, not to be caught and papered over.
    """
    directory = Path(directory) if directory is not None else _PROFILE_DIR
    files = sorted(directory.glob("*.toml"))
    if not files:
        raise ValueError(f"No practice profiles found in {directory}")
    profiles: dict[str, CustomerProfile] = {}
    for path in files:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path.name}: not valid TOML — {exc}") from exc
        profile = _validate(path, data)
        if profile.id in profiles:
            raise ValueError(f"{path.name}: duplicate profile id {profile.id!r}")
        profiles[profile.id] = profile
    return profiles


def profile_cards(profiles: dict[str, CustomerProfile]) -> list[dict]:
    """Picker order: easy first, then alphabetical — a rep's natural path up."""
    rank = {d: i for i, d in enumerate(_DIFFICULTIES)}
    ordered = sorted(profiles.values(), key=lambda p: (rank[p.difficulty], p.name))
    return [p.card() for p in ordered]


def _validate(path: Path, data: dict) -> CustomerProfile:
    for field in _STR_FIELDS:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path.name}: {field!r} is required and must be non-empty text")
    for field in _LIST_FIELDS:
        value = data.get(field)
        if (not isinstance(value, list) or not value
                or not all(isinstance(v, str) and v.strip() for v in value)):
            raise ValueError(
                f"{path.name}: {field!r} is required and must be a non-empty list of text")
    # An unknown key is almost certainly a typo'd known one — and a typo'd
    # `hangup_triggers` would mean a customer who never hangs up, silently.
    known = set(_STR_FIELDS) | set(_LIST_FIELDS)
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{path.name}: unknown field(s) {sorted(unknown)} — "
                         f"expected only {sorted(known)}")
    if data["difficulty"] not in _DIFFICULTIES:
        raise ValueError(f"{path.name}: difficulty must be one of {_DIFFICULTIES}, "
                         f"got {data['difficulty']!r}")
    # The filename is how humans find a profile; the id is how code does. Forcing
    # them equal means a copied file can't silently shadow the one it was copied
    # from.
    if data["id"] != path.stem:
        raise ValueError(f"{path.name}: id {data['id']!r} must match the filename")
    return CustomerProfile(
        **{f: data[f].strip() for f in _STR_FIELDS},
        **{f: tuple(v.strip() for v in data[f]) for f in _LIST_FIELDS},
    )
