"""Cut the corridor's district and municipality outlines out of the OCHA COD-AB.

The national boundary file is 60 MB zipped, far too big for a web page.
This script runs once (and again only if COD-AB changes). It:
  1. downloads npl_admin_boundaries.geojson.zip from HDX if it is not
     already in data/raw/cod/ (and records it in data/manifests/cod_ab.jsonl);
  2. keeps the eight corridor districts and their municipalities;
  3. writes them unsimplified to data/raw/cod/corridor_full.geojson, used
     for point-in-polygon checks (geo.py);
  4. simplifies and rounds them into docs/data/boundaries.geojson for the
     map (target: under 500 KB).

Municipality pcodes whose last three digits start with 5 are protected
areas (national parks), not real municipalities. They are kept as outlines
with kind "protected_area" so the map has no holes, but they never get a
bubble and the gazetteer never matches them.

Run from the repo root:  .venv/bin/python src/build_boundaries.py
"""

import json
import zipfile

from shapely.geometry import mapping, shape
from shapely.validation import explain_validity

from config import CORE_DISTRICTS, CORRIDOR_DISTRICTS, DOCS, HDX_COD_AB, RAW
from fetch_common import append_manifest, get, hdx_resources, raw_record, utc_stamp

ZIP_PATH = RAW / "cod" / "npl_admin_boundaries.geojson.zip"
FULL_OUT = RAW / "cod" / "corridor_full.geojson"
PUBLIC_OUT = DOCS / "data" / "boundaries.geojson"

# Simplification tolerance in degrees (0.001 degrees is roughly 100 m here).
# Bubbles sit on top of faint outlines, so small slivers are acceptable.
TOLERANCE = {"district": 0.0015, "municipality": 0.0012}
DECIMALS = 4      # about 11 m


def ensure_zip() -> None:
    if ZIP_PATH.exists():
        return
    res = next(r for r in hdx_resources(HDX_COD_AB)
               if r["name"] == "npl_admin_boundaries.geojson.zip")
    print(f"downloading {res['name']} ({int(res.get('size') or 0) / 1e6:.0f} MB)...")
    ZIP_PATH.parent.mkdir(parents=True, exist_ok=True)
    ZIP_PATH.write_bytes(get(res["url"], timeout=600).content)
    append_manifest("cod_ab", {
        "fetched_at_utc": utc_stamp(), "endpoint": res["url"],
        "hdx_package": HDX_COD_AB, "hdx_last_modified": res.get("last_modified"),
        "licence": "CC BY-IGO", "raw": [raw_record(ZIP_PATH)]})


def kind_of(pcode: str) -> str:
    """NP0329402 -> 'municipality'; NP0328597 -> 'protected_area'."""
    return "protected_area" if pcode[6] == "5" else "municipality"


def round_coords(geom: dict) -> dict:
    def r(coords):
        if isinstance(coords[0], (int, float)):
            return [round(coords[0], DECIMALS), round(coords[1], DECIMALS)]
        return [r(c) for c in coords]
    return {"type": geom["type"], "coordinates": r(geom["coordinates"])}


def main() -> None:
    ensure_zip()
    z = zipfile.ZipFile(ZIP_PATH)
    adm2 = json.loads(z.read("npl_admin2.geojson"))["features"]
    adm3 = json.loads(z.read("npl_admin3.geojson"))["features"]

    full, public = [], []
    for f in adm2:
        p = f["properties"]
        if p["adm2_pcode"] not in CORRIDOR_DISTRICTS:
            continue
        props = {"pcode": p["adm2_pcode"], "level": "district", "kind": "district",
                 "name_en": p["adm2_name"], "parent_pcode": p["adm1_pcode"],
                 "district": p["adm2_name"],
                 "role": "core" if p["adm2_pcode"] in CORE_DISTRICTS else "downstream",
                 "center": [round(p["center_lon"], 5), round(p["center_lat"], 5)]}
        full.append({"type": "Feature", "properties": props, "geometry": f["geometry"]})
    for f in adm3:
        p = f["properties"]
        if p["adm2_pcode"] not in CORRIDOR_DISTRICTS:
            continue
        props = {"pcode": p["adm3_pcode"], "level": "municipality",
                 "kind": kind_of(p["adm3_pcode"]),
                 "name_en": p["adm3_name"], "parent_pcode": p["adm2_pcode"],
                 "district": p["adm2_name"],
                 "role": "core" if p["adm2_pcode"] in CORE_DISTRICTS else "downstream",
                 "center": [round(p["center_lon"], 5), round(p["center_lat"], 5)]}
        full.append({"type": "Feature", "properties": props, "geometry": f["geometry"]})

    FULL_OUT.write_text(json.dumps({"type": "FeatureCollection", "features": full}))

    invalid = 0
    for f in full:
        geom = shape(f["geometry"])
        if not geom.is_valid:
            invalid += 1
            print(f"  note: {f['properties']['pcode']} invalid in COD-AB "
                  f"({explain_validity(geom)}); repaired with buffer(0)")
            geom = geom.buffer(0)
        simple = geom.simplify(TOLERANCE[f["properties"]["level"]], preserve_topology=True)
        assert simple.is_valid and not simple.is_empty, f["properties"]["pcode"]
        public.append({"type": "Feature", "properties": f["properties"],
                       "geometry": round_coords(mapping(simple))})

    PUBLIC_OUT.parent.mkdir(parents=True, exist_ok=True)
    body = {"type": "FeatureCollection",
            "meta": {"source": "OCHA COD-AB Nepal (HDX cod-ab-npl), v02",
                     "licence": "CC BY-IGO",
                     "note": "simplified for display; protected areas are outlines only"},
            "features": public}
    PUBLIC_OUT.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")))

    # --- sanity checks ---------------------------------------------------
    by_kind = {}
    for f in full:
        k = f["properties"]["kind"]
        by_kind[k] = by_kind.get(k, 0) + 1
    print(f"features: {by_kind}")
    assert by_kind.get("district") == len(CORRIDOR_DISTRICTS), "a corridor district is missing"
    print(f"invalid source geometries repaired: {invalid}")
    size_kb = PUBLIC_OUT.stat().st_size / 1024
    print(f"public boundaries: {PUBLIC_OUT.relative_to(DOCS.parent)} = {size_kb:.0f} KB "
          f"({'OK' if size_kb < 500 else 'OVER the 500 KB target'})")
    print(f"full-detail corridor file: {FULL_OUT.stat().st_size / 1e6:.1f} MB (gitignored)")

    # Point-in-polygon spot checks (coordinates from OpenStreetMap).
    from geo import municipality_at
    for name, lon, lat, expect in [("Dhunche", 85.2953, 28.1063, "Gosaikunda"),
                                   ("Bidur (Trishuli Bazar)", 85.1576, 27.8983, "Bidur")]:
        hit = municipality_at(lon, lat)
        got = hit["name_en"] if hit else None
        print(f"  spot check {name}: {got} ({'OK' if got == expect else 'CHECK'})")


if __name__ == "__main__":
    main()
