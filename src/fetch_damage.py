"""Building damage per municipality from the Copernicus EMS satellite grading.

Copernicus Emergency Management Service activation EMSR927 graded every
building it could see in four areas of interest (AOIs) along the river:
01 Syapru Besi, 02 Timure, 03 Bidur, 05 Phosretar (AOI 04 was not
published). Some AOIs have an "initial" product and a later "monitoring"
product; the later one replaces the earlier one, they are never added.

For each product this script downloads (from HDX, CC BY):
  * the xlsx summary table (the official totals), and
  * the GeoPackage with one point per damaged building.
It then puts every damaged residential building into its municipality by
point-in-polygon (geo.py), so damage is attributed by location, not by
the AOI's name. The per-municipality counts must add up to the xlsx
totals, or the script stops.

A product's date is the day it was published on HDX (the resource's
last_modified date): that is when the figure became known.

Outputs:
  data/processed/ems_damage.csv       aoi, product, product_date, pcode, counts
  data/processed/ems_aoi_summary.csv  the xlsx totals per AOI and product
  data/manifests/ems.jsonl

Run from the repo root:  .venv/bin/python src/fetch_damage.py
"""

import csv
import re
import sqlite3
import struct

import openpyxl
from shapely import wkb

from config import HDX_EMS, PROCESSED, RAW
from fetch_common import append_manifest, get, hdx_resources, raw_record, utc_stamp
from geo import municipality_at

EMS_DIR = RAW / "ems"
DAMAGE_CSV = PROCESSED / "ems_damage.csv"
SUMMARY_CSV = PROCESSED / "ems_aoi_summary.csv"
NAME_RE = re.compile(r"npl_copernicus_ems_aoi(\d+)_grading_(initial|monitoring\d+)\.(gpkg|xlsx)$")
LICENCE = "CC BY 4.0, © European Union, Copernicus Emergency Management Service"
GRADES = {"Destroyed": "destroyed", "Damaged": "damaged", "Possibly damaged": "possibly"}


def gpkg_point(blob: bytes):
    """GeoPackage geometry blob -> shapely geometry (skip the GP header)."""
    flags = blob[3]
    envelope = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(flags >> 1) & 0b111]
    return wkb.loads(blob[8 + envelope:])


def read_xlsx_summary(path) -> dict:
    """The 'Grading' sheet's headline rows."""
    ws = openpyxl.load_workbook(path, data_only=True)["Grading"]
    out = {"locality": "", "res_destroyed": 0, "res_damaged": 0, "res_possibly": 0,
           "res_total_affected": 0, "res_total_in_aoi": 0,
           "pop_affected": "", "pop_in_aoi": "", "bridges_destroyed": 0}
    for row in ws.iter_rows(values_only=True):
        cells = [c for c in row if c is not None]
        if not cells:
            continue
        if isinstance(cells[0], str) and cells[0].startswith("EMSR927 AOI:"):
            out["locality"] = cells[0]
        if "Residential Buildings" in cells:
            nums = [c for c in cells if isinstance(c, (int, float))]
            (out["res_destroyed"], out["res_damaged"], out["res_possibly"],
             out["res_total_affected"], out["res_total_in_aoi"]) = nums[:5]
        if "Inhabitants" in cells:
            nums = [c for c in cells if isinstance(c, (int, float))]
            out["pop_affected"], out["pop_in_aoi"] = nums[:2]
        if "Bridges and elevated highways" in cells:
            out["bridges_destroyed"] = [c for c in cells if isinstance(c, (int, float))][0]
    return out


def count_by_municipality(path) -> dict:
    """{pcode or 'outside': {destroyed, damaged, possibly}} for residential points."""
    counts = {}
    db = sqlite3.connect(path)
    # The layer is called builtUpP_v1, builtUpP_v2... after the product version.
    (table,) = db.execute("SELECT table_name FROM gpkg_contents "
                          "WHERE table_name LIKE 'builtUpP_v%'").fetchone()
    for blob, obj_type, grade in db.execute(
            f"SELECT geom, obj_type, damage_gra FROM {table}"):
        if not obj_type.startswith("11") or grade not in GRADES:
            continue          # residential buildings only; skip "No visible damage"
        point = gpkg_point(blob)
        muni = municipality_at(point.x, point.y)
        key = muni["pcode"] if muni else "outside"
        c = counts.setdefault(key, {"destroyed": 0, "damaged": 0, "possibly": 0})
        c[GRADES[grade]] += 1
    db.close()
    return counts


def main() -> None:
    EMS_DIR.mkdir(parents=True, exist_ok=True)
    products = {}   # (aoi, product) -> {"gpkg": res, "xlsx": res}
    for res in hdx_resources(HDX_EMS):
        m = NAME_RE.match(res["name"])
        if m:
            products.setdefault((m.group(1), m.group(2)), {})[m.group(3)] = res

    used, downloaded, damage_rows, summary_rows = [], [], [], []
    for (aoi, product), files in sorted(products.items()):
        stamp = files["xlsx"]["last_modified"][:10]
        local = {}
        for ext, res in files.items():
            path = EMS_DIR / f"aoi{aoi}_{product}_{stamp}.{ext}"
            if not path.exists():
                path.write_bytes(get(res["url"], timeout=300).content)
                downloaded.append(path.name)
            used.append(raw_record(path))
            local[ext] = path
        summary = read_xlsx_summary(local["xlsx"])
        counts = count_by_municipality(local["gpkg"])

        # --- built-in check: points add up to the official table ----------
        for grade in ("destroyed", "damaged", "possibly"):
            pts = sum(c[grade] for c in counts.values())
            assert pts == summary[f"res_{grade}"], (
                f"AOI {aoi} {product}: {pts} {grade} points vs {summary[f'res_{grade}']} in xlsx")
        url = files["xlsx"]["url"]
        summary_rows.append({"aoi": aoi, "product": product, "product_date": stamp,
                             **summary, "source_url": url, "licence": LICENCE})
        for pcode, c in sorted(counts.items()):
            damage_rows.append({"aoi": aoi, "aoi_name": re.sub(r"\s*Grading.*$", "", summary["locality"].split("AOI: ")[-1]),
                                "product": product, "product_date": stamp, "pcode": pcode,
                                "residential_destroyed": c["destroyed"],
                                "residential_damaged": c["damaged"],
                                "residential_possibly": c["possibly"],
                                "source_url": url, "licence": LICENCE})

    # Every run is logged with ALL the raw files it used (not only the new
    # downloads), so the manifest always describes the processed output.
    append_manifest("ems", {"fetched_at_utc": utc_stamp(), "hdx_package": HDX_EMS,
                            "licence": LICENCE, "downloaded_this_run": downloaded,
                            "raw": used})
    PROCESSED.mkdir(parents=True, exist_ok=True)
    for path, rows in ((DAMAGE_CSV, damage_rows), (SUMMARY_CSV, summary_rows)):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    # --- sanity checks ---------------------------------------------------
    print(f"products: {len(products)} ({len(downloaded)} files downloaded this run)")
    for s in summary_rows:
        print(f"  AOI {s['aoi']} {s['product']:<11} {s['product_date']}  "
              f"{s['locality'].split('AOI: ')[-1]:<36} destroyed {s['res_destroyed']:>5}, "
              f"damaged {s['res_damaged']:>4}, possibly {s['res_possibly']:>4} "
              f"(points reconcile: OK)")
    from gazetteer import Gazetteer
    names = {p.place_id: p.name_en for p in Gazetteer.load().places.values()}
    print("residential buildings destroyed by municipality (latest product per AOI):")
    latest = {}
    for s in summary_rows:
        latest[s["aoi"]] = max(latest.get(s["aoi"], ""), s["product"])
    by_pcode = {}
    for r in damage_rows:
        if r["product"] == latest[r["aoi"]]:
            by_pcode[r["pcode"]] = by_pcode.get(r["pcode"], 0) + r["residential_destroyed"]
    for pcode, n in sorted(by_pcode.items(), key=lambda kv: -kv[1]):
        print(f"  {pcode:<10} {names.get(pcode, pcode):<22} {n:>5}")


if __name__ == "__main__":
    main()
