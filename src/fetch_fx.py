"""Fetch Nepal Rastra Bank exchange rates from just before the flood until today.

Ledger amounts are shown in both rupees and US dollars, each converted at
the rate on the day the money was announced (see fx.py). NRB's forex API
is keyless. Each run re-fetches the whole window (a few KB a day), saves
the raw pages, and rewrites data/processed/fx_rates.csv.

Run from the repo root:  .venv/bin/python src/fetch_fx.py
"""

import csv
import json
from datetime import timedelta

from config import EVENT_DATE, NPT, NRB_FOREX_API, PROCESSED, RAW
from fetch_common import append_manifest, get, raw_record, utc_now, utc_stamp
from fx import FX_CSV, Rates

START = EVENT_DATE - timedelta(days=7)   # a margin for "last rate on or before"
PER_PAGE = 100


def main() -> None:
    now = utc_now()
    today = now.astimezone(NPT).date()
    run_dir = RAW / "fx" / now.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    rows, page, pages = [], 1, 1
    while page <= pages:
        params = {"page": page, "per_page": PER_PAGE,
                  "from": START.isoformat(), "to": today.isoformat()}
        resp = get(NRB_FOREX_API, params=params)
        (run_dir / f"page_{page}.json").write_bytes(resp.content)
        body = resp.json()
        pages = body["pagination"]["pages"]
        for day in body["data"]["payload"]:
            for r in day["rates"]:
                rows.append({"date": day["date"], "currency": r["currency"]["iso3"],
                             "unit": r["currency"]["unit"], "buy": r["buy"], "sell": r["sell"]})
        page += 1
    append_manifest("fx", {"fetched_at_utc": utc_stamp(now), "endpoint": NRB_FOREX_API,
                           "params": {"from": START.isoformat(), "to": today.isoformat()},
                           "raw": [raw_record(p) for p in sorted(run_dir.iterdir())]})

    rows.sort(key=lambda r: (r["date"], r["currency"]))
    PROCESSED.mkdir(parents=True, exist_ok=True)
    with open(FX_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "currency", "unit", "buy", "sell"])
        w.writeheader()
        w.writerows(rows)

    # --- sanity checks ---------------------------------------------------
    days = sorted({r["date"] for r in rows})
    print(f"saved {len(rows)} rates for {len(days)} days ({days[0]} to {days[-1]})")
    missing = [d for d in (START + timedelta(n) for n in range((today - START).days + 1))
               if d.isoformat() not in days]
    print(f"days with no NRB rate (covered by the last earlier rate): "
          f"{[d.isoformat() for d in missing] or 'none'}")
    usd = next(r for r in rows if r["date"] == EVENT_DATE.isoformat() and r["currency"] == "USD")
    print(f"spot check USD on {EVENT_DATE}: buy {usd['buy']} / sell {usd['sell']} "
          f"(expected 152.37 / 152.97: {'OK' if (usd['buy'], usd['sell']) == ('152.37', '152.97') else 'CHECK'})")
    jpy = Rates(rows).npr_per_unit("JPY", EVENT_DATE)[0]
    print(f"JPY quoted per {next(r['unit'] for r in rows if r['currency'] == 'JPY')} -> "
          f"{jpy:.4f} NPR per yen")


if __name__ == "__main__":
    main()
