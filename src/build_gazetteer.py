"""Build data/gazetteer/places.csv: every corridor place, in English and Nepali.

Sources:
  * OCHA COD-AB (HDX cod-ab-npl): pcodes, English names, centroids. It has
    no Nepali names.
  * BIPAD, the government disaster portal: Nepali names (title_ne). Its
    spellings differ from COD's for about a fifth of the municipalities.
  * data/manual/ (hand-edited, committed):
      gazetteer_overrides.csv  COD <-> BIPAD pairs that need a person's check
      admin_extras.csv         extra spellings and needs_context flags
      towns.csv                towns, rivers and landmarks (geocode_towns.py
                               fills in their coordinates)

How COD and BIPAD are joined: a BIPAD municipality is paired with the COD
municipality whose polygon contains BIPAD's centroid. If the two names
also agree, the pair is accepted automatically. If they disagree, the pair
is written to gazetteer_overrides.csv with checked_by_hand=no for Jack to
confirm (he sets yes). The build always prints how many are unchecked.

Run from the repo root:  .venv/bin/python src/build_gazetteer.py
"""

import csv
import difflib
import json
import re

import pandas as pd

from config import (CORE_DISTRICTS, CORRIDOR_DISTRICTS, GAZETTEER, HDX_COD_AB, MANUAL,
                    RAW)
from fetch_common import append_manifest, get, hdx_resources, raw_record, utc_stamp
from gazetteer import Gazetteer, is_protected_area

COD_XLSX = RAW / "cod" / "npl_admin_boundaries.xlsx"
BIPAD_DIR = RAW / "bipad"
BIPAD_API = "https://bipadportal.gov.np/api/v1/{level}/"
OVERRIDES = MANUAL / "gazetteer_overrides.csv"
EXTRAS = MANUAL / "admin_extras.csv"
TOWNS = MANUAL / "towns.csv"

CORRIDOR_PROVINCES = ["NP03", "NP04", "NP05"]   # Bagmati, Gandaki, Lumbini
OVERRIDE_FIELDS = ["level", "cod_pcode", "cod_name", "bipad_id", "bipad_title_en",
                   "bipad_title_ne", "evidence", "checked_by_hand", "checked_on", "note"]
PLACE_FIELDS = ["place_id", "kind", "pcode", "parent_pcode", "name_en", "name_ne",
                "aliases_en", "aliases_ne", "lat", "lon", "corridor_role",
                "needs_context", "ne_source", "notes"]


# --- downloads (skipped when the raw file is already there) ----------------
def ensure_cod_xlsx() -> None:
    if COD_XLSX.exists():
        return
    res = next(r for r in hdx_resources(HDX_COD_AB)
               if r["name"] == "npl_admin_boundaries.xlsx")
    COD_XLSX.parent.mkdir(parents=True, exist_ok=True)
    COD_XLSX.write_bytes(get(res["url"], timeout=120).content)
    append_manifest("cod_ab", {
        "fetched_at_utc": utc_stamp(), "endpoint": res["url"], "hdx_package": HDX_COD_AB,
        "hdx_last_modified": res.get("last_modified"), "licence": "CC BY-IGO",
        "raw": [raw_record(COD_XLSX)]})


def ensure_bipad() -> None:
    paths = {level: BIPAD_DIR / f"{level}.json"
             for level in ("province", "district", "municipality")}
    if all(p.exists() for p in paths.values()):
        return
    BIPAD_DIR.mkdir(parents=True, exist_ok=True)
    for level, path in paths.items():
        # limit=1000 returns every row in one page (774 municipalities).
        resp = get(BIPAD_API.format(level=level), params={"limit": 1000, "format": "json"})
        path.write_bytes(resp.content)
    append_manifest("bipad", {
        "fetched_at_utc": utc_stamp(), "endpoint": BIPAD_API,
        "params": {"limit": 1000, "format": "json"},
        "note": "used for Nepali names (title_ne) only",
        "raw": [raw_record(p) for p in paths.values()]})


# --- helpers ---------------------------------------------------------------
def simple(name: str) -> str:
    """Lower-case letters only: 'Binayi Tribeni' -> 'binayitribeni'."""
    return re.sub(r"[^a-z]", "", name.lower())


def read_csv(path) -> list:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def bipad_rows(level: str) -> list:
    return json.loads((BIPAD_DIR / f"{level}.json").read_text())["results"]


def pair(level, cod_pcode, cod_name, bipad, evidence, overrides, new_overrides):
    """Return the BIPAD row to use for a COD unit, consulting the override file.

    A name+location agreement is accepted automatically. Anything else
    goes through gazetteer_overrides.csv, where Jack's checked row wins.
    """
    agreed = bipad is not None and simple(bipad["title_en"]) == simple(cod_name)
    if agreed and "centroid" in evidence:
        return bipad, "auto (name and location agree)"
    row = overrides.get(cod_pcode)
    if row is None:
        row = {"level": level, "cod_pcode": cod_pcode, "cod_name": cod_name,
               "bipad_id": bipad["id"] if bipad else "",
               "bipad_title_en": bipad["title_en"] if bipad else "",
               "bipad_title_ne": bipad["title_ne"] if bipad else "",
               "evidence": evidence, "checked_by_hand": "no", "checked_on": "", "note": ""}
        new_overrides.append(row)
    source = ("override (checked by hand)" if row["checked_by_hand"] == "yes"
              else "override (NOT yet checked by hand)")
    return {"id": row["bipad_id"], "title_en": row["bipad_title_en"],
            "title_ne": row["bipad_title_ne"]}, source


def main() -> None:
    ensure_cod_xlsx()
    ensure_bipad()
    from geo import municipality_at   # needs build_boundaries.py to have run

    xl = pd.ExcelFile(COD_XLSX)
    adm1, adm2, adm3 = (xl.parse(s) for s in ("npl_admin1", "npl_admin2", "npl_admin3"))
    overrides = {r["cod_pcode"]: r for r in read_csv(OVERRIDES)}
    extras = {r["pcode"]: r for r in read_csv(EXTRAS)}
    new_overrides = []
    places = []

    def add(place_id, kind, pcode, parent, name_en, name_ne, lat, lon, role, ne_source,
            aliases_en=(), aliases_ne=(), needs_context=False, notes=""):
        extra = extras.get(pcode if kind != "town" else "", {})
        aliases_en = [a for a in (*aliases_en, *extra.get("aliases_en", "").split("|"))
                      if a and a != name_en]
        aliases_ne = [a for a in (*aliases_ne, *extra.get("aliases_ne", "").split("|"))
                      if a and a != name_ne]
        needs = needs_context or extra.get("needs_context") == "yes"
        places.append({
            "place_id": place_id, "kind": kind, "pcode": pcode, "parent_pcode": parent,
            "name_en": name_en, "name_ne": name_ne,
            "aliases_en": "|".join(dict.fromkeys(aliases_en)),
            "aliases_ne": "|".join(dict.fromkeys(aliases_ne)),
            "lat": f"{lat:.5f}" if lat is not None else "",
            "lon": f"{lon:.5f}" if lon is not None else "",
            "corridor_role": role, "needs_context": "yes" if needs else "no",
            "ne_source": ne_source, "notes": " | ".join(n for n in (notes, extra.get("note", "")) if n)})

    # --- provinces -------------------------------------------------------
    bipad_prov = {simple(p["title_en"]): p for p in bipad_rows("province")}
    for _, r in adm1[adm1["adm1_pcode"].isin(CORRIDOR_PROVINCES)].iterrows():
        b = bipad_prov.get(simple(r["adm1_name"]))
        b, src = pair("province", r["adm1_pcode"], r["adm1_name"], b,
                      "name only (provinces have no centroid check)", overrides, new_overrides)
        add(r["adm1_pcode"], "province", r["adm1_pcode"], "NP", r["adm1_name"], b["title_ne"],
            r["center_lat"], r["center_lon"], "province", src,
            aliases_en=[f"{r['adm1_name']} Province"])

    # --- districts -------------------------------------------------------
    bipad_dist = bipad_rows("district")
    by_name = {simple(d["title_en"]): d for d in bipad_dist}
    for _, r in adm2[adm2["adm2_pcode"].isin(CORRIDOR_DISTRICTS)].iterrows():
        b = by_name.get(simple(r["adm2_name"]))
        evidence = "name"
        if b is None:
            close = difflib.get_close_matches(simple(r["adm2_name"]), by_name, n=1, cutoff=0.6)
            b = by_name[close[0]] if close else None
            evidence = f"difflib suggestion ({close[0] if close else 'none'})"
        else:
            evidence = "name; centroid"   # districts are big; names are distinctive
        b, src = pair("district", r["adm2_pcode"], r["adm2_name"], b, evidence,
                      overrides, new_overrides)
        role = "core" if r["adm2_pcode"] in CORE_DISTRICTS else "downstream"
        aliases = [b["title_en"]] if b.get("title_en") else []
        add(r["adm2_pcode"], "district", r["adm2_pcode"], r["adm1_pcode"], r["adm2_name"],
            b["title_ne"], r["center_lat"], r["center_lon"], role, src, aliases_en=aliases)

    # --- municipalities --------------------------------------------------
    # Which BIPAD municipality's centroid lies inside each COD polygon?
    inside = {}
    for m in bipad_rows("municipality"):
        lon, lat = m["centroid"]["coordinates"]
        hit = municipality_at(lon, lat)
        if hit:
            inside.setdefault(hit["pcode"], []).append(m)
    district_names = {simple(n) for n in CORRIDOR_DISTRICTS.values()} | {"chitwan", "tanahun"}
    province_names = {simple(n) for n in adm1["adm1_name"]}
    for _, r in adm3[adm3["adm2_pcode"].isin(CORRIDOR_DISTRICTS)].iterrows():
        pcode = r["adm3_pcode"]
        if is_protected_area(pcode):
            continue
        hits = inside.get(pcode, [])
        b = hits[0] if len(hits) == 1 else None
        evidence = (f"centroid inside ({len(hits)} BIPAD centroids)" if hits
                    else "no BIPAD centroid inside")
        b, src = pair("municipality", pcode, r["adm3_name"], b, evidence,
                      overrides, new_overrides)
        role = "core" if r["adm2_pcode"] in CORE_DISTRICTS else "downstream"
        name_en, name_ne = r["adm3_name"], b["title_ne"]
        aliases_en = [b["title_en"]] if b.get("title_en") else []
        notes = ""
        # A municipality named like its district or province (Gorkha, Gandaki)
        # only matches when written with its type, so "Gorkha" stays the district.
        if simple(name_en) in district_names | province_names:
            kind_en = "Municipality" if pcode[6] == "3" else "Rural Municipality"
            kind_ne = "नगरपालिका" if pcode[6] == "3" else "गाउँपालिका"
            aliases_en = [f"{name_en} {kind_en}"]
            name_ne_plain, name_ne = name_ne, f"{name_ne} {kind_ne}"
            name_en = f"{name_en} {kind_en}"
            notes = f"plain '{r['adm3_name']}'/'{name_ne_plain}' is left to the district/province"
        add(pcode, "municipality", pcode, r["adm2_pcode"], name_en, name_ne,
            r["center_lat"], r["center_lon"], role, src, aliases_en=aliases_en, notes=notes)

    # --- towns, rivers, landmarks (hand-written) ------------------------
    for t in read_csv(TOWNS):
        lat = float(t["lat"]) if t["lat"] else None
        lon = float(t["lon"]) if t["lon"] else None
        role = "core"
        if t["pcode"] and t["pcode"][:6] in CORRIDOR_DISTRICTS and t["pcode"][:6] not in CORE_DISTRICTS:
            role = "downstream"
        add(t["place_id"], t["kind"], t["pcode"], t["pcode"][:6] if t["pcode"].startswith("NP") else "",
            t["name_en"], t["name_ne"], lat, lon, role,
            "towns.csv (checked by hand)" if t["checked_by_hand"] == "yes"
            else "towns.csv (NOT yet checked by hand)",
            aliases_en=t["aliases_en"].split("|"), aliases_ne=t["aliases_ne"].split("|"),
            needs_context=t["needs_context"] == "yes", notes=t["notes"])

    # --- write -----------------------------------------------------------
    if new_overrides:
        all_rows = list(overrides.values()) + new_overrides
        with open(OVERRIDES, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=OVERRIDE_FIELDS)
            w.writeheader()
            w.writerows(all_rows)
    GAZETTEER.parent.mkdir(parents=True, exist_ok=True)
    with open(GAZETTEER, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PLACE_FIELDS)
        w.writeheader()
        w.writerows(places)

    # --- sanity checks ---------------------------------------------------
    kinds = {}
    for p in places:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(f"places written: {len(places)} {kinds}")
    munis = [p for p in places if p["kind"] == "municipality"]
    with_ne = sum(1 for p in munis if p["name_ne"])
    print(f"corridor municipalities (5xx protected areas excluded): {len(munis)}; "
          f"with a Nepali name: {with_ne} ({with_ne / len(munis):.0%})")
    assert with_ne == len(munis), "a municipality has no Nepali name"
    all_overrides = read_csv(OVERRIDES)
    unchecked = [r for r in all_overrides if r["checked_by_hand"] != "yes"]
    print(f"override pairs: {len(all_overrides)} ({len(new_overrides)} new this run); "
          f"awaiting Jack's hand check: {len(unchecked)}")
    no_point = [p["place_id"] for p in places if p["kind"] == "town" and not p["lat"]]
    if no_point:
        print(f"towns with no coordinates yet ({len(no_point)}): run src/geocode_towns.py")

    gaz = Gazetteer.load()
    for text, expect in [("Gosaikunda", "NP0329402"), ("गोसाईकुण्डमा", "NP0329402"),
                         ("रसुवाको", "NP0329"), ("Meghang", "NP0328401"),
                         ("Gorkha", "NP0436"), ("Syabrubesi", "NP0329402")]:
        got = [m.pcode for m in gaz.match(text)]
        print(f"  spot check {text!r} -> {got} ({'OK' if got == [expect] else 'CHECK'})")
    shared = {}
    for p in places:
        for a in [p["name_en"], *p["aliases_en"].split("|")]:
            if a:
                shared.setdefault(a.lower(), set()).add(p["place_id"])
    clashes = {a: ids for a, ids in shared.items() if len(ids) > 1}
    print(f"spellings shared by more than one place (resolved by district context): "
          f"{dict(sorted(clashes.items()))}")


if __name__ == "__main__":
    main()
