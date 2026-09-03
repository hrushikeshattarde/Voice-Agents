"""
Deadhead: how far the caller's truck is from the pickup.

This number gets SAID to a driver, which decides most of what is asserted here.
Two properties matter more than accuracy:

  * it is never stated precisely — the estimate is worth about ±10%, so it is
    always rounded and always hedged;
  * it is never stated at all when we cannot place the caller confidently.
    A state centroid, or the nearest big city to a small town, could be 200 miles
    out. Saying nothing is the honest answer and the agent handles it fine.

Calibration against real driving distances (Google Maps) is at the bottom. The
estimate runs consistently OVER, which is the safe direction: a driver told "about
275 miles" who drives 239 is pleasantly surprised, and the reverse is a complaint.
"""

import pytest

from lanevoice import geo

# Real pickup coordinates from live load 2535130 (Breckenridge, MN).
PICKUP_LAT, PICKUP_LON = 46.262721, -96.560291


# --------------------------------------------------------------------------- #
# Placing what the caller said
# --------------------------------------------------------------------------- #
def test_a_city_and_state_resolves():
    place = geo.locate("Fort Wayne, Indiana")
    assert place is not None
    assert place.label == "Fort Wayne, IN"
    assert place.lat == pytest.approx(41.13, abs=0.05)
    assert place.lon == pytest.approx(-85.13, abs=0.05)


def test_the_two_letter_form_resolves_too():
    """`extract_empty_location` emits "Dallas, TX" for a comma + abbreviation and
    "Dallas, Texas" for a spelled-out state. Both have to land."""
    assert geo.locate("Dallas, TX").label == "Dallas, TX"
    assert geo.locate("Dallas, Texas").label == "Dallas, TX"


def test_speech_to_text_abbreviations_still_match():
    """Truckers and Whisper both abbreviate. The expansion is applied to BOTH
    sides, so the caller's "st louis" meets the table's "St. Louis"."""
    assert geo.locate("ft wayne, indiana").label == "Fort Wayne, IN"
    assert geo.locate("St Louis, Missouri").label == "St. Louis, MO"
    assert geo.locate("Mt Pleasant, South Carolina").state == "SC"


def test_a_near_miss_still_matches():
    """Phone audio mangles names. A tight fuzzy match recovers the common cases."""
    assert geo.locate("Indianapolis, Indiana").label == "Indianapolis, IN"
    assert geo.locate("Philadelpia, Pennsylvania").state == "PA"


def test_a_state_run_onto_the_city_is_still_split():
    """The parser emits a comma today, but this comes from a caller's own words
    often enough that depending on punctuation would drop a placeable location."""
    assert geo.locate("Phoenix Arizona").label == "Phoenix, AZ"
    assert geo.locate("Indianapolis Indiana").label == "Indianapolis, IN"
    # Longest state name first, so this is West Virginia and not Virginia.
    assert geo.locate("Huntington West Virginia").state == "WV"


def test_a_bare_city_resolves_to_the_biggest_one():
    """A caller who names no state means the one everybody means. There are eight
    Springfields in the table; "Springfield" is Missouri."""
    assert geo.locate("Springfield").label == "Springfield, MO"
    assert geo.locate("Portland").label == "Portland, OR"
    assert geo.locate("Kansas City").label == "Kansas City, MO"


def test_the_state_still_wins_over_size():
    """...but if they DID name a state, that state decides."""
    assert geo.locate("Springfield, Illinois").label == "Springfield, IL"
    assert geo.locate("Portland, Maine").label == "Portland, ME"


# --------------------------------------------------------------------------- #
# Refusing to guess — the important half
# --------------------------------------------------------------------------- #
def test_a_state_alone_is_not_placeable():
    """State centroids are worthless for this: Indiana's is 150 miles from Gary.
    The agent says nothing about distance instead."""
    for spoken in ("Indiana", "Texas", "TX", "New Mexico"):
        assert geo.locate(spoken) is None, spoken


def test_a_town_below_the_tables_floor_is_not_placeable():
    """The table now holds every place of 1,000+ people, so Breckenridge, MN
    (~3,300) is in it — a live pickup that used to fall through. A hamlet below
    the floor still comes back as None: NOT the nearest town, which would be
    miles off and stated with total confidence."""
    assert geo.locate("Breckenridge, Minnesota").state == "MN"
    assert geo.locate("Klondike Corner, Ohio") is None


def test_a_name_shared_by_many_towns_takes_the_one_near_the_pickup():
    """Observed live: a caller empty in Columbia City, Indiana — 20 miles from a
    Fort Wayne pickup — and the only Columbia City the old table knew was in
    Washington state. With the pickup to steer by, the nearest wins; with no
    pickup, the most populous, as before."""
    fort_wayne = (41.1306, -85.1289)
    assert geo.locate("Columbia City", near=fort_wayne).state == "IN"
    assert geo.locate("Columbia City").state == "WA"
    assert geo.locate("Springfield", near=(39.78, -89.65)).state == "IL"
    assert geo.locate("Springfield").label == "Springfield, MO"
    # A state given by the caller still beats the pickup.
    assert geo.locate("Columbia City, Washington", near=fort_wayne).state == "WA"


def test_the_offices_towns_become_vocabulary():
    names = geo.region_keyterms("Fort Wayne, IN", miles=150, limit=60)
    assert names and names[0] == "Fort Wayne"                 # the back yard first
    assert {"Columbia City", "Huntington", "Auburn", "Decatur"} <= set(names)
    assert len(names) <= 60
    assert geo.region_keyterms("Nowhere at all", 150, 60) == []


def test_an_unrecognisable_place_is_not_placeable():
    for spoken in ("Nowhereville, Indiana", "asdfgh", "", None, "   "):
        assert geo.locate(spoken) is None, spoken


def test_a_loose_similar_name_is_rejected():
    """The fuzzy cutoff is tight on purpose. Fort Payne, Alabama is a real place
    365 miles from Fort Wayne — and, at ~14,000 people, below this table's floor.
    So it must come back as None or as somewhere in Alabama. What it must NEVER do
    is resolve to Fort Wayne, which is the kind of near-miss that has the agent
    confidently discussing the wrong state."""
    payne = geo.locate("Fort Payne, Alabama")
    assert payne is None or payne.state == "AL"
    assert geo.locate("Fort Wayne, Indiana").label == "Fort Wayne, IN"


# --------------------------------------------------------------------------- #
# Saying it out loud
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("miles,expected", [
    (18, "about 20 miles"),
    (47, "about 50 miles"),
    (93, "about 90 miles"),
    (152, "about 150 miles"),
    (267, "about 275 miles"),
    (410, "about 400 miles"),
    (812, "about 800 miles"),
])
def test_distances_are_rounded_for_speech(miles, expected):
    """Never "97 miles". The estimate cannot support that precision, and a figure
    said to the mile is one a driver will hold us to."""
    assert geo.spoken_miles(miles) == expected


def test_a_trivial_distance_is_words_not_a_number():
    """"About 10 miles" invites an argument about nothing."""
    for miles in (0, 3, 12, 14.9):
        assert geo.spoken_miles(miles) == "right around the corner from the pickup"


def test_nothing_is_said_for_an_unknown_distance():
    assert geo.spoken_miles(None) is None
    assert geo.spoken_miles(-1) is None


def test_no_spoken_figure_is_ever_exact():
    """Sweeping the range: every output either rounds to a 5 or is the words."""
    for miles in range(0, 1200, 7):
        spoken = geo.spoken_miles(miles)
        assert spoken is not None
        if spoken.startswith("about"):
            assert int(spoken.split()[1]) % 5 == 0, miles


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
def test_the_whole_phrase_from_a_spoken_place():
    assert geo.deadhead_phrase("Fargo, North Dakota", PICKUP_LAT, PICKUP_LON) \
        == "about 50 miles"


def test_a_pickup_with_no_coordinates_yields_nothing():
    """Real load records carry a city with null lat/lon. Inventing a distance for
    those is exactly what this returns None to avoid."""
    assert geo.deadhead_phrase("Fort Wayne, Indiana", None, None) is None
    assert geo.deadhead_phrase("Fort Wayne, Indiana", PICKUP_LAT, None) is None


def test_an_unplaceable_caller_yields_nothing():
    assert geo.deadhead_phrase("Minnesota", PICKUP_LAT, PICKUP_LON) is None
    assert geo.deadhead_phrase(None, PICKUP_LAT, PICKUP_LON) is None


def test_the_road_factor_scales_the_estimate():
    chicago = geo.locate("Chicago, Illinois")
    milwaukee = geo.locate("Milwaukee, Wisconsin")
    straight = geo.haversine_miles(chicago.lat, chicago.lon,
                                   milwaukee.lat, milwaukee.lon)

    scaled = geo.deadhead_miles(chicago, milwaukee.lat, milwaukee.lon,
                                road_factor=1.5)
    assert scaled == pytest.approx(straight * 1.5, rel=0.01)
    # A factor below 1 would claim driving is shorter than flying, so it clamps.
    assert geo.deadhead_miles(chicago, milwaukee.lat, milwaukee.lon,
                              road_factor=0.5) == pytest.approx(straight, rel=0.01)


# --------------------------------------------------------------------------- #
# Calibration against reality
# --------------------------------------------------------------------------- #
# (from, to, real driving miles per Google Maps)
_KNOWN = [
    ("Fort Wayne, Indiana", "Indianapolis, Indiana", 125),
    ("Chicago, Illinois", "Milwaukee, Wisconsin", 92),
    ("Dallas, Texas", "Houston, Texas", 239),
    ("Los Angeles, California", "Phoenix, Arizona", 373),
    ("Atlanta, Georgia", "Nashville, Tennessee", 250),
]


@pytest.mark.parametrize("origin,destination,real", _KNOWN)
def test_the_estimate_is_within_a_quarter_of_reality(origin, destination, real):
    """Measured 3–15% over on these five, mean 8.8%. The bound is deliberately
    loose — this test guards against a broken factor or a swapped lat/lon, not
    against the inherent error of a straight line."""
    start, end = geo.locate(origin), geo.locate(destination)
    estimate = geo.deadhead_miles(start, end.lat, end.lon)
    assert estimate == pytest.approx(real, rel=0.25), f"{origin} -> {destination}"


@pytest.mark.parametrize("origin,destination,real", _KNOWN)
def test_the_estimate_errs_long_rather_than_short(origin, destination, real):
    """The safe direction. A driver told "about 275" who drives 239 is pleased; one
    told "about 200" who drives 239 is not, and may have priced the load on it."""
    start, end = geo.locate(origin), geo.locate(destination)
    assert geo.deadhead_miles(start, end.lat, end.lon) >= real * 0.98


def test_haversine_is_symmetric_and_zero_at_a_point():
    a, b = geo.locate("Chicago, Illinois"), geo.locate("Dallas, Texas")
    there = geo.haversine_miles(a.lat, a.lon, b.lat, b.lon)
    back = geo.haversine_miles(b.lat, b.lon, a.lat, a.lon)
    assert there == pytest.approx(back)
    assert geo.haversine_miles(a.lat, a.lon, a.lat, a.lon) == pytest.approx(0)


def test_the_bundled_table_is_actually_bundled():
    """A packaging change that dropped the data file would otherwise show up as
    "the agent stopped mentioning distance", with no error anywhere."""
    assert geo._CITIES.loaded_count > 3000
