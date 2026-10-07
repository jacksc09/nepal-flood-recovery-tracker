"""Fill in coordinates for the towns in data/manual/towns.csv from OpenStreetMap.

For each town with no lat/lon yet, this asks OpenStreetMap's Nominatim
search (keyless; at most one request per second, as its usage policy
asks) and keeps the first result that falls inside a corridor
municipality. The municipality is then found by point-in-polygon against
the COD-AB boundaries, never guessed from the name.

Every filled row keeps checked_by_hand=no: Jack confirms each point on a
map before setting it to yes. Raw responses are saved to data/raw/nominatim/
and the run is logged in data/manifests/nominatim.jsonl.

Run from the repo root:  .venv/bin/python src/geocode_towns.py
Then re-run:             .venv/bin/python src/build_gazetteer.py
"""

import csv
import json

from config import CORRIDOR_DISTRICTS, MANUAL
from fetch_common import append_manifest, get, raw_record, save_raw, utc_stamp
from geo import municipality_at

TOWNS = MANUAL / "towns.csv"
NOMINATIM = "https://nominatim.openstreetmap.org/search"


def main() -> None:
    with open(TOWNS, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fields = list(rows[0].keys())
    raw_files, filled = [], 0
    for row in rows:
        if row["kind"] != "town" or row["lat"]:
            continue
        # Try the main spelling first, then each alias, until one lands
        # inside a corridor municipality.
        chosen = None
        spellings = [row["name_en"], *[a for a in row["aliases_en"].split("|") if a]]
        for n, spelling in enumerate(spellings):
            params = {"q": f"{spelling}, Nepal", "format": "jsonv2",
                      "countrycodes": "np", "limit": 5, "addressdetails": 0}
            resp = get(NOMINATIM, params=params)
            path = save_raw(f"nominatim/{row['place_id']}_{n}.json", resp.content)
            raw_files.append(raw_record(path))
            for hit in resp.json():
                muni = municipality_at(float(hit["lon"]), float(hit["lat"]))
                if muni and muni["kind"] == "municipality":
                    chosen = (hit, muni)
                    break
            if chosen:
                break
        if not chosen:
            print(f"  {row['name_en']}: no result inside the corridor - fill by hand")
            continue
        hit, muni = chosen
        row["lat"], row["lon"] = f"{float(hit['lat']):.5f}", f"{float(hit['lon']):.5f}"
        row["pcode"] = muni["pcode"]
        row["coord_source"] = f"OSM Nominatim {hit['osm_type']}/{hit['osm_id']} ({hit.get('type')})"
        filled += 1
        print(f"  {row['name_en']:<16} -> {muni['name_en']} ({muni['pcode']}, "
              f"{CORRIDOR_DISTRICTS.get(muni['pcode'][:6])}) via {hit['osm_type']} "
              f"{hit.get('type')}: {hit['display_name'][:70]}")

    with open(TOWNS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    if raw_files:
        append_manifest("nominatim", {"fetched_at_utc": utc_stamp(), "endpoint": NOMINATIM,
                                      "licence": "ODbL (OpenStreetMap contributors)",
                                      "raw": raw_files})
    print(f"filled {filled} towns; all remain checked_by_hand=no until Jack confirms them")


if __name__ == "__main__":
    main()
