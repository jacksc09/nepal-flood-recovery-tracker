"""Assemble one week's data file: everything the map shows for that week.

Used by publish_week.py (which writes the permanent, never-rewritten file
in docs/data/weeks/) and build_preview.py (which writes a local preview
that is never committed). Both build the week the same way:

  * meta    - which week, what kind (reconstructed / snapshot / late
              snapshot), when it was built
  * fts     - OCHA FTS: the appeal requirement and every in-scope flow
              first reported by the week's end (plan flows, plus flows
              outside the plan that Jack marked in scope)
  * ledger  - reviewed money entries announced by the week's end, with the
              totals from ledger.py (FTS is never added to these)
  * damage  - Copernicus EMS homes destroyed/damaged per municipality, from
              the latest product published by the week's end
  * news    - article counts per place (to date, and this week) and this
              week's headlines with links
  * donate  - each place's "Where to donate" list as it stood that week

Everything in a week file is dated on or before the week's end, so an
old week shows what was known then (or, for reconstructed weeks, what the
dated records say was known then).
"""

import csv
import json
from datetime import date, datetime
from decimal import Decimal

import articles
import ledger
import orgs
from config import (DOCS, EVENT_DATE, LAST_RECONSTRUCTED_WEEK, LATE_SNAPSHOT_DAYS,
                    MANIFESTS, MANUAL, NPT, PROCESSED)
from fetch_common import utc_stamp
from gazetteer import Gazetteer
from match_places import MATCHES_CSV
from weeks import week_bounds, week_end_date, week_of, week_start_date

PUBLIC_EXCERPT_WORDS = 25
PLACES_JSON = DOCS / "data" / "places.json"


def read_csv(path) -> list:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def first_words(text: str, n: int = PUBLIC_EXCERPT_WORDS) -> str:
    words = (text or "").split()
    return " ".join(words[:n]) + (" …" if len(words) > n else "")


def money(d: Decimal) -> float:
    """Decimals become plain numbers in JSON (whole rupees / dollars)."""
    return float(round(d))


def area_keys(gaz: Gazetteer, place_id: str) -> list:
    """A place and every larger area it belongs to: town -> municipality -> district."""
    p = gaz.places.get(place_id)
    if not p or not p.pcode.startswith("NP") or p.kind == "province":
        return []
    keys = [place_id] if place_id != p.pcode else []
    if len(p.pcode) == 9:
        keys.append(p.pcode)
    keys.append(p.pcode[:6])
    return keys


def week_kind(week: str, built_at: datetime) -> str:
    if week_start_date(week) <= week_start_date(LAST_RECONSTRUCTED_WEEK):
        return "reconstructed"
    days_late = (built_at.astimezone(NPT).date() - week_end_date(week)).days
    return "late_snapshot" if days_late > LATE_SNAPSHOT_DAYS else "snapshot"


# --- the parts ---------------------------------------------------------------------
def fts_part(end: date) -> dict:
    plan = json.loads((PROCESSED / "fts_plan.json").read_text())
    scope = {r["flow_id"]: r for r in read_csv(MANUAL / "fts_scope.csv")}
    flows, funded = [], {"paid": 0.0, "commitment": 0.0}
    pledges = out_of_plan = 0.0
    for f in read_csv(PROCESSED / "fts_flows.csv"):
        if f["first_reported_date"] > end.isoformat() or f["boundary"] != "incoming":
            continue
        in_plan = f["in_plan"] == "yes"
        if not in_plan and scope.get(f["flow_id"], {}).get("in_scope") != "yes":
            continue                       # out of plan and not (yet) judged in scope
        usd = float(f["amount_usd"] or 0)
        flows.append({k: f[k] for k in ("flow_id", "version_id", "status", "date",
                                        "first_reported_date", "source_org",
                                        "destination_org", "cluster")}
                     | {"amount_usd": usd, "in_plan": in_plan,
                        "scope_reason": scope.get(f["flow_id"], {}).get("reason", "")})
        if not in_plan:
            out_of_plan += usd
        elif f["status"] == "pledge":
            pledges += usd
        else:
            funded[f["status"]] = funded.get(f["status"], 0.0) + usd
    return {"plan_name": plan["name"], "plan_id": plan["plan_id"],
            "requirement_usd": plan["requirement_usd"],
            "funded_usd": funded | {"total": funded["paid"] + funded["commitment"]},
            "pledges_usd": pledges, "out_of_plan_in_scope_usd": out_of_plan,
            "pending_scope_decisions": sum(
                1 for f in read_csv(PROCESSED / "fts_flows.csv")
                if f["in_plan"] == "no" and f["flow_id"] not in scope),
            "flows": sorted(flows, key=lambda f: (f["first_reported_date"], f["flow_id"]))}


def ledger_part(end: date, built_at: datetime) -> dict:
    entries = ledger.apply_corrections(ledger.load_ledger(), ledger.load_corrections(),
                                       made_by=built_at.astimezone(NPT).date())
    t = ledger.totals_as_of(entries, end)

    def cell(c):
        return {col: {"npr": money(c[col]["npr"]), "usd": money(c[col]["usd"])}
                for col in ledger.COLUMNS} | {
            "n_entries": c["n_entries"],
            "also_reported_to_fts_npr": money(c["also_reported_to_fts_npr"]),
            "by_phase": {ph: {"npr": money(v["npr"]), "usd": money(v["usd"])}
                         for ph, v in c["by_phase"].items()}}

    public = []
    for e in entries:
        if e["announced_on"] > end.isoformat() or e["status"] == "retracted":
            continue
        public.append({
            **{k: e.get(k) for k in ("entry_id", "money_id", "status", "amount_text",
                                     "amount", "currency", "amount_basis", "unit_count",
                                     "amount_computed", "amount_npr", "amount_usd",
                                     "donor_name", "donor_type", "geo_level", "pcode",
                                     "place_id", "sector", "phase", "announced_on",
                                     "fts_flow_id", "first_published_week")},
            "fx_rate_date": (e.get("fx") or {}).get("rate_date"),
            "review": (e.get("review") or {}).get("decision"),
            "sources": [{"url": s["url"], "outlet": s["outlet"], "lang": s["lang"],
                         "title": s.get("title", ""), "published_at": s["published_at"],
                         "excerpt": first_words(s["evidence_excerpt"])}
                        for s in e["sources"]],
        })
    return {"areas": {k: cell(c) for k, c in t["areas"].items()},
            "not_location_specific": {k: cell(c) for k, c in t["not_location_specific"].items()},
            "per_unit_not_totalled": [e["entry_id"] for e in t["per_unit_not_totalled"]],
            "entries": sorted(public, key=lambda e: (e["announced_on"], e["entry_id"]))}


def damage_part(end: date, gaz: Gazetteer) -> dict:
    rows = read_csv(PROCESSED / "ems_damage.csv")
    latest = {}   # AOI -> latest product published by the week's end
    for r in rows:
        if r["product_date"] <= end.isoformat():
            if r["product_date"] >= latest.get(r["aoi"], ("", ""))[1]:
                latest[r["aoi"]] = (r["product"], r["product_date"])
    areas, products = {}, []
    for aoi, (product, day) in sorted(latest.items()):
        products.append({"aoi": aoi, "product": product, "product_date": day,
                         "name": next(r["aoi_name"] for r in rows if r["aoi"] == aoi)})
        for r in rows:
            if r["aoi"] != aoi or r["product"] != product or r["pcode"] == "outside":
                continue
            for key in area_keys(gaz, r["pcode"]):
                a = areas.setdefault(key, {"destroyed": 0, "damaged": 0, "possibly": 0})
                a["destroyed"] += int(r["residential_destroyed"])
                a["damaged"] += int(r["residential_damaged"])
                a["possibly"] += int(r["residential_possibly"])
    return {"areas": areas, "products": products,
            "source": "Copernicus EMS EMSR927 grading (CC BY 4.0)",
            "measure": "residential buildings, satellite grading"}


def news_part(week: str, gaz: Gazetteer) -> dict:
    store = articles.load()
    start, end = week_start_date(week).isoformat(), week_end_date(week).isoformat()
    cumulative, this_week, items = {}, {}, []
    totals = {"week": {"en": 0, "ne": 0}, "to_date": {"en": 0, "ne": 0}}
    for m in read_csv(MATCHES_CSV):
        if m["relevance"] not in ("title", "body"):
            continue
        a = store[m["article_id"]]
        day = a["published_at_utc"][:10] if a["date_precision"] == "date" else \
            datetime.fromisoformat(a["published_at_utc"].replace("Z", "+00:00")) \
            .astimezone(NPT).date().isoformat()
        if day < EVENT_DATE.isoformat() or day > end:
            continue
        keys = sorted({k for pid in m["place_ids"].split("|") if pid
                       for k in area_keys(gaz, pid)})
        lang = a["lang"]
        totals["to_date"][lang] += 1
        for k in keys:
            c = cumulative.setdefault(k, {"en": 0, "ne": 0})
            c[lang] += 1
        if day >= start:
            totals["week"][lang] += 1
            for k in keys:
                c = this_week.setdefault(k, {"en": 0, "ne": 0})
                c[lang] += 1
            items.append({"article_id": a["article_id"], "title": a["title"],
                          "url": a["url"], "outlet": a["outlet"], "lang": lang,
                          "published": day, "places": keys, "late": False})
    items.sort(key=lambda i: (i["published"], i["article_id"]), reverse=True)
    return {"totals": totals, "to_date": cumulative, "this_week": this_week, "items": items}


def donate_part(end: date, gaz: Gazetteer) -> dict:
    listings, by_id = orgs.load_areas(), orgs.load_orgs()
    chains = {pid: area_keys(gaz, pid) for pid in gaz.places}
    out = {}
    for pid, keys in chains.items():
        if not keys:
            continue
        out[pid] = orgs.donate_block(pid, listings, by_id, end, chains)
    return out


def review_stats(built_at: datetime) -> dict:
    stats = {"drafted": 0, "approved": 0, "edited": 0, "rejected": 0, "added_as_source": 0,
             "org_listings": 0}
    queue = (ledger.LEDGER_DIR.parent / "review" / "queue.jsonl")
    if queue.exists():
        stats["drafted"] = sum(1 for l in queue.read_text(encoding="utf-8").splitlines()
                               if l and json.loads(l)["kind"] == "ledger")
    for r in read_csv(ledger.LEDGER_DIR / "review_log.csv"):
        if r["reviewed_on"] > built_at.astimezone(NPT).date().isoformat():
            continue
        if r["kind"] == "ledger":
            stats[r["decision"]] = stats.get(r["decision"], 0) + 1
        elif r["kind"] == "org" and r["decision"] == "approved":
            stats["org_listings"] += 1
    return stats


def fetch_runs() -> dict:
    """The latest fetch run id per source, so a week file cites its data."""
    runs = {}
    for path in sorted(MANIFESTS.glob("*.jsonl")):
        lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]
        if lines:
            runs[path.stem] = {"run_id": lines[-1]["run_id"],
                               "fetched_at_utc": lines[-1].get("fetched_at_utc")}
    return runs


def build_week(week: str, built_at: datetime, preview: bool = False) -> dict:
    gaz = Gazetteer.load()
    start, end = week_bounds(week)
    kind = week_kind(week, built_at)
    return {
        "meta": {"week": week, "start_npt": start.isoformat(), "end_npt": end.isoformat(),
                 "kind": kind, "preview": preview, "built_at": utc_stamp(built_at),
                 "reconstructed_on": (built_at.astimezone(NPT).date().isoformat()
                                      if kind == "reconstructed" else None),
                 "data_cutoff": end.date().isoformat(), "fetch_runs": fetch_runs(),
                 "review_stats": review_stats(built_at),
                 "open_week": week == week_of(built_at)},
        "fts": fts_part(end.date()),
        "ledger": ledger_part(end.date(), built_at),
        "damage": damage_part(end.date(), gaz),
        "news": news_part(week, gaz),
        "donate": donate_part(end.date(), gaz),
        "context": read_csv(MANUAL / "context.csv"),
    }


def summary(week_data: dict) -> dict:
    """The few numbers index.json keeps per week, for the timeline."""
    led = week_data["ledger"]
    districts = {k: v for k, v in led["areas"].items() if len(k) == 6}
    located = {col: sum(v[col]["usd"] for v in districts.values()) for col in ledger.COLUMNS}
    unlocated = {col: sum(v[col]["usd"] for v in led["not_location_specific"].values())
                 for col in ledger.COLUMNS}
    rebuilding = sum(v["by_phase"][ph]["usd"]
                     for v in list(districts.values()) + list(led["not_location_specific"].values())
                     for ph in ledger.REBUILDING_PHASES)
    return {"fts_funded_usd": week_data["fts"]["funded_usd"]["total"],
            "fts_out_of_plan_in_scope_usd": week_data["fts"]["out_of_plan_in_scope_usd"],
            "requirement_usd": week_data["fts"]["requirement_usd"],
            "ledger_usd": {col: located[col] + unlocated[col] for col in ledger.COLUMNS},
            "ledger_rebuilding_usd": rebuilding,
            "ledger_entries": len(led["entries"]),
            "articles_week": week_data["news"]["totals"]["week"],
            "homes_destroyed": sum(v["destroyed"] for k, v in week_data["damage"]["areas"].items()
                                   if len(k) == 6)}


def write_places() -> int:
    """docs/data/places.json: names, kinds and bubble positions (not week data)."""
    gaz = Gazetteer.load()
    out = []
    for p in gaz.places.values():
        keys = area_keys(gaz, p.place_id)
        if not keys or p.lat is None:
            continue
        level = {"district": "district", "municipality": "municipality"}.get(p.kind, "settlement")
        out.append({"id": p.place_id, "kind": p.kind, "level": level,
                    "name_en": p.name_en, "name_ne": p.name_ne,
                    "lat": p.lat, "lon": p.lon, "district": p.pcode[:6],
                    "municipality": p.pcode if len(p.pcode) == 9 else None,
                    "role": p.corridor_role})
    PLACES_JSON.parent.mkdir(parents=True, exist_ok=True)
    PLACES_JSON.write_text(json.dumps(sorted(out, key=lambda p: p["id"]), ensure_ascii=False,
                                      separators=(",", ":")) + "\n", encoding="utf-8")
    return len(out)
