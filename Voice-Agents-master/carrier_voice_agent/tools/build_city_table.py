"""Build the bundled US city -> coordinates table from GeoNames.

Run with:  uv run --with geonamescache --no-project python build_city_table.py

Source: GeoNames (https://www.geonames.org/), CC BY 4.0, via the `geonamescache`
package (MIT). Only US places are kept, and only the four fields the geocoder
needs, which turns a 33 MB worldwide dependency into a ~60 KB bundled file with
no runtime dependency at all.
"""

import csv
import gzip
import io
import pathlib
import sys

import geonamescache

OUT = pathlib.Path(sys.argv[1])

gc = geonamescache.GeonamesCache()
rows = []
for city in gc.get_cities().values():
    if city.get("countrycode") != "US":
        continue
    name = (city.get("name") or "").strip()
    state = (city.get("admin1code") or "").strip()
    if not name or len(state) != 2:
        continue
    try:
        lat = round(float(city["latitude"]), 4)
        lon = round(float(city["longitude"]), 4)
    except (TypeError, ValueError, KeyError):
        continue
    rows.append((name, state, f"{lat}", f"{lon}", str(int(city.get("population") or 0))))

# Biggest first, so a duplicate name resolves to the place a trucker means when
# they say "Springfield" with no state.
rows.sort(key=lambda r: (-int(r[4]), r[0], r[1]))

buffer = io.StringIO()
writer = csv.writer(buffer, lineterminator="\n")
writer.writerow(["name", "state", "lat", "lon", "population"])
writer.writerows(rows)
raw = buffer.getvalue().encode("utf-8")

OUT.parent.mkdir(parents=True, exist_ok=True)
with gzip.open(OUT, "wb", compresslevel=9) as fh:
    fh.write(raw)

print(f"{len(rows)} US places -> {OUT}")
print(f"  raw {len(raw)/1024:.0f} KB, gzipped {OUT.stat().st_size/1024:.0f} KB")
states = {r[1] for r in rows}
print(f"  {len(states)} states/territories")
for probe in ("Fort Wayne", "Sikeston", "Joliet", "Laredo", "Springfield"):
    hits = [r for r in rows if r[0] == probe]
    print(f"  {probe:<12} {len(hits):>2} match(es): "
          + ", ".join(f"{h[1]}({h[4]})" for h in hits[:4]))
