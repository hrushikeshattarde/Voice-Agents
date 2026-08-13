# Bundled data

## `us_cities.csv.gz`

3,407 US places with coordinates — `name,state,lat,lon,population`, sorted
largest-population first so a bare "Springfield" resolves to the one a trucker
most likely means.

Used by [`lanevoice/geo.py`](../geo.py) to turn a spoken "empty in Fort Wayne"
into a point, so the agent can tell a carrier roughly how far their truck is from
the pickup. Bundled rather than fetched: a geocoding call on the critical path of
a live conversation is exactly the round trip this codebase avoids everywhere
else, and shipping the table keeps the test suite hermetic.

**Coverage:** US places over ~15,000 population. A truck empty in a smaller town
will not match, and `geo.locate` returns None — the agent then says nothing about
distance rather than guessing at the nearest big city.

### Source and licence

GeoNames — <https://www.geonames.org/> — licensed **CC BY 4.0**
(<https://creativecommons.org/licenses/by/4.0/>).

Extracted from the [`geonamescache`](https://pypi.org/project/geonamescache/)
package (MIT), which bundles the GeoNames `cities15000` dataset. Only US rows and
the five fields above are kept, which turns a 33 MB worldwide dependency into a
56 KB file with no runtime dependency.

Attribution is required by CC BY 4.0 and is given here and in `geo.py`'s module
docstring.

### Rebuilding

```bash
uv run --with geonamescache --no-project python tools/build_city_table.py src/lanevoice/data/us_cities.csv.gz
```

Worth doing if coverage ever needs to widen (swap `cities15000` for `cities5000`
upstream) — not on any schedule, since city coordinates do not move.
