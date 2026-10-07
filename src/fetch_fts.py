"""Fetch national funding for the flood from OCHA's Financial Tracking Service.

FTS records humanitarian funding reported by donors and agencies. Every
flow is tagged to "Nepal" as a whole, never to a district, so this is the
tracker's NATIONAL track. It is never added to the place-by-place ledger.

Two queries (both keyless):
  1. the flash appeal (plan 1637): its requirement, and every flow to it;
  2. every 2026 flow to Nepal, to find flows reported OUTSIDE the appeal
     since 26 Aug. Whether each of those belongs to this flood is a human
     decision, recorded in data/manual/fts_scope.csv through the review UI.

Built-in check: the incoming, non-pledge plan flows must add up exactly to
the fundingTotal FTS reports for the plan. If they don't, the script stops.

Outputs:
  data/raw/fts/<UTC stamp>/      verbatim API responses (gitignored)
  data/processed/fts_flows.csv   one row per flow (plan + out-of-plan candidates)
  data/processed/fts_plan.json   requirement and totals as fetched
  data/manifests/fts.jsonl       one line per run

Run from the repo root:  .venv/bin/python src/fetch_fts.py
Re-process a saved run:  .venv/bin/python src/fetch_fts.py --from-raw data/raw/fts/<stamp>
"""

import argparse
import csv
import json
from pathlib import Path

from config import EVENT_DATE, FTS_API, FTS_PLAN_ID, MANUAL, PROCESSED, RAW
from fetch_common import append_manifest, get, raw_record, utc_now, utc_stamp

FLOWS_CSV = PROCESSED / "fts_flows.csv"
PLAN_JSON = PROCESSED / "fts_plan.json"
SCOPE_CSV = MANUAL / "fts_scope.csv"
FIELDS = ["flow_id", "version_id", "date", "decision_date", "first_reported_date",
          "created_at", "updated_at", "amount_usd", "original_amount", "original_currency",
          "status", "boundary", "parent_flow_id", "source_org", "destination_org",
          "cluster", "in_plan", "keywords", "description"]


def objects(flow: dict, side: str, kind: str) -> list:
    return [o["name"] for o in flow.get(f"{side}Objects") or [] if o["type"] == kind]


def to_row(flow: dict) -> dict:
    day = lambda v: (v or "")[:10]
    plans = [o["id"] for o in flow.get("destinationObjects") or [] if o["type"] == "Plan"]
    return {
        "flow_id": flow["id"], "version_id": flow.get("versionId"),
        "date": day(flow.get("date")), "decision_date": day(flow.get("decisionDate")),
        "first_reported_date": day(flow.get("firstReportedDate")),
        "created_at": flow.get("createdAt", ""), "updated_at": flow.get("updatedAt", ""),
        "amount_usd": flow.get("amountUSD"), "original_amount": flow.get("originalAmount") or "",
        "original_currency": flow.get("originalCurrency") or "",
        "status": flow.get("status"), "boundary": flow.get("boundary"),
        "parent_flow_id": flow.get("parentFlowId") or "",
        "source_org": "; ".join(objects(flow, "source", "Organization")),
        "destination_org": "; ".join(objects(flow, "destination", "Organization")),
        "cluster": "; ".join(objects(flow, "destination", "Cluster")),
        "in_plan": "yes" if str(FTS_PLAN_ID) in plans else "no",
        "keywords": "; ".join(flow.get("keywords") or []),
        "description": " ".join((flow.get("description") or "").split()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-raw", type=Path, help="re-process a saved run directory")
    args = parser.parse_args()

    if args.from_raw:
        run_dir = args.from_raw
        print(f"re-processing {run_dir}")
    else:
        stamp = utc_now()
        run_dir = RAW / "fts" / stamp.strftime("%Y%m%dT%H%M%SZ")
        run_dir.mkdir(parents=True, exist_ok=True)
        calls = {
            "plan.json": (f"{FTS_API}/plan/id/{FTS_PLAN_ID}", None),
            "plan_flows.json": (f"{FTS_API}/fts/flow", {"planId": FTS_PLAN_ID}),
            "country_flows.json": (f"{FTS_API}/fts/flow",
                                   {"countryISO3": "NPL", "year": EVENT_DATE.year}),
        }
        for name, (url, params) in calls.items():
            (run_dir / name).write_bytes(get(url, params=params).content)
        append_manifest("fts", {
            "fetched_at_utc": utc_stamp(stamp),
            "endpoints": {k: {"url": u, "params": p} for k, (u, p) in calls.items()},
            "licence": "FTS data: free to use with attribution to OCHA FTS",
            "raw": [raw_record(run_dir / n) for n in calls]})

    plan = json.loads((run_dir / "plan.json").read_text())["data"]
    plan_data = json.loads((run_dir / "plan_flows.json").read_text())["data"]
    country = json.loads((run_dir / "country_flows.json").read_text())["data"]["flows"]

    # --- built-in check: flows add up to FTS's own total ---------------------
    incoming = [f for f in plan_data["flows"]
                if f["boundary"] == "incoming" and f["status"] != "pledge"]
    flow_sum = sum(f["amountUSD"] for f in incoming)
    funding_total = plan_data["incoming"]["fundingTotal"]
    print(f"plan {FTS_PLAN_ID}: {plan['planVersion']['name']}")
    print(f"  requirement: ${plan['revisedRequirements']:,}")
    print(f"  incoming non-pledge flows: {len(incoming)}, sum ${flow_sum:,} "
          f"vs fundingTotal ${funding_total:,}")
    assert flow_sum == funding_total, "plan flows do not add up to fundingTotal - investigate"
    print(f"  reconciliation OK; funded {funding_total / plan['revisedRequirements']:.1%}")

    rows = {f["id"]: to_row(f) for f in plan_data["flows"]}
    out_of_plan = []
    for f in country:
        if f["id"] in rows:
            continue
        row = to_row(f)
        if row["in_plan"] == "yes" or row["date"] < EVENT_DATE.isoformat():
            continue
        if row["boundary"] != "incoming":
            continue
        rows[f["id"]] = row
        out_of_plan.append(row)

    PROCESSED.mkdir(parents=True, exist_ok=True)
    with open(FLOWS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: (r["first_reported_date"], r["flow_id"])))
    PLAN_JSON.write_text(json.dumps({
        "plan_id": FTS_PLAN_ID, "name": plan["planVersion"]["name"],
        "requirement_usd": plan["revisedRequirements"],
        "funding_total_usd": funding_total, "pledge_total_usd": plan_data["incoming"]["pledgeTotal"],
        "fetched_from": str(run_dir.name)}, indent=2) + "\n")

    # --- out-of-plan flows: which still need a scope decision? ---------------
    decided = set()
    if SCOPE_CSV.exists():
        with open(SCOPE_CSV, newline="", encoding="utf-8") as f:
            decided = {r["flow_id"] for r in csv.DictReader(f)}
    pending = [r for r in out_of_plan if r["flow_id"] not in decided]
    total = sum(float(r["amount_usd"] or 0) for r in out_of_plan)
    print(f"out-of-plan incoming flows to Nepal since {EVENT_DATE}: {len(out_of_plan)} "
          f"(${total:,.0f}); awaiting a scope decision: {len(pending)}")
    for r in sorted(out_of_plan, key=lambda r: -float(r["amount_usd"] or 0)):
        print(f"  {r['flow_id']:>7} {r['date']} ${float(r['amount_usd'] or 0):>12,.0f} "
              f"{r['status']:<10} {r['source_org'][:28]:<28} -> {r['destination_org'][:30]}")
    print(f"saved {len(rows)} flows to {FLOWS_CSV.relative_to(PROCESSED.parent.parent)}")


if __name__ == "__main__":
    main()
