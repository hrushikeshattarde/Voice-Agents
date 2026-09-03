# Bundled data

## `us_cities.csv.gz`

16,659 US places with coordinates — `name,state,lat,lon,population`, sorted
largest-population first so a bare "Springfield" with no pickup to steer by
resolves to the one a trucker most likely means.

Used by [`lanevoice/geo.py`](../geo.py) to turn a spoken "empty in Fort Wayne"
into a point, so the agent can tell a carrier roughly how far their truck is from
the pickup, and to give the recogniser the names of the towns around the office.
Bundled rather than fetched: a geocoding call on the critical path of a live
conversation is exactly the round trip this codebase avoids everywhere else, and
shipping the table keeps the test suite hermetic.

**Coverage:** every US place of 1,000 people or more. The first table stopped at
15,000 and missed the towns trucks actually empty in — Columbia City, Auburn and
Decatur around Fort Wayne among them — and a bare "Columbia City" then resolved
to the only one it knew, in Washington state, two thousand miles away. With the
whole country in, names repeat, so `geo.locate` takes the pickup's coordinates
and prefers the town nearest to it (see "region" in `geo.py`). A place below
1,000 people still comes back as None, and the agent says nothing about distance.

### Source and licence

GeoNames — <https://www.geonames.org/> — licensed **CC BY 4.0**
(<https://creativecommons.org/licenses/by/4.0/>).

Extracted from the GeoNames `cities1000` dump
(<https://download.geonames.org/export/dump/cities1000.zip>), US rows of feature
class P only, minus PPLX (sections of a city — Chicago's neighbourhoods are not
places a truck is empty in), five fields kept. Regenerate with the same filter when the dump is
refreshed; the file is written with a fixed mtime so the archive is reproducible.

Attribution is required by CC BY 4.0 and is given here and in `geo.py`'s module
docstring.
