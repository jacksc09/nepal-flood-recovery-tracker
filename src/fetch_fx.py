"""Fetch Nepal Rastra Bank exchange rates from just before the flood until today.

Ledger amounts are shown in both rupees and US dollars, each converted at
the rate on the day the money was announced (see fx.py). NRB's forex API
is keyless. Each run re-fetches the whole window (a few KB a day), saves
the raw pages, and rewrites data/processed/fx_rates.csv.

Currencies NRB does not publish (New Zealand dollars, so far) are crossed
through the euro with the European Central Bank's reference rates (keyless):
rupees per euro (NRB) divided by that currency per euro (ECB, the last
ECB rate on or before the day). Each row's `source` column says which.

Run from the repo root:  .venv/bin/python src/fetch_fx.py
"""

import csv
import json
import xml.etree.ElementTree as ET
from datetime import timedelta
from decimal import Decimal

from config import EVENT_DATE, NPT, NRB_FOREX_API, PROCESSED, RAW
from fetch_common import append_manifest, get, raw_record, utc_now, utc_stamp
from fx import FX_CSV, Rates

START = EVENT_DATE - timedelta(days=7)   # a margin for "last rate on or before"
PER_PAGE = 100
ECB_90D = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
CROSS_CURRENCIES = ["NZD", "CZK", "NOK"]  # needed by the ledger, not published by NRB
NRB_SOURCE = "Nepal Rastra Bank"


def parse_ecb(xml_bytes: bytes) -> dict:
    """{date: {currency: units per euro}} from the ECB reference-rate XML."""
    out = {}
    for cube in ET.fromstring(xml_bytes).iter():
        if cube.get("time"):
            out[cube.get("time")] = {c.get("currency"): c.get("rate") for c in cube}
    return out


def cross_rows(nrb_rows: list, ecb: dict, currencies: list) -> list:
    """Rupees per unit of each currency, via NRB's euro rate and the ECB's."""
    ecb_days = sorted(ecb)
    out = []
    for r in nrb_rows:
        if r["currency"] != "EUR":
            continue
        earlier = [d for d in ecb_days if d <= r["date"]]
        if not earlier:
            continue
        eur_mid = (Decimal(r["buy"]) + Decimal(r["sell"])) / 2 / Decimal(r["unit"])
        for cur in currencies:
            per_eur = ecb[earlier[-1]].get(cur)
            if per_eur:
                npr = (eur_mid / Decimal(per_eur)).quantize(Decimal("0.0001"))
                out.append({"date": r["date"], "currency": cur, "unit": "1", "buy": str(npr),
                            "sell": str(npr),
                            "source": f"ECB reference rate via EUR (ECB {earlier[-1]})"})
    return out


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
    resp = get(ECB_90D)
    (run_dir / "ecb_90d.xml").write_bytes(resp.content)
    for r in rows:
        r["source"] = NRB_SOURCE
    rows += cross_rows(rows, parse_ecb(resp.content), CROSS_CURRENCIES)
    append_manifest("fx", {"fetched_at_utc": utc_stamp(now), "endpoint": NRB_FOREX_API,
                           "cross_rates": ECB_90D,
                           "params": {"from": START.isoformat(), "to": today.isoformat()},
                           "raw": [raw_record(p) for p in sorted(run_dir.iterdir())]})

    rows.sort(key=lambda r: (r["date"], r["currency"]))
    PROCESSED.mkdir(parents=True, exist_ok=True)
    with open(FX_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "currency", "unit", "buy", "sell", "source"])
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
    nzd = [r for r in rows if r["currency"] == "NZD"]
    print(f"NZD rows crossed through the euro (ECB): {len(nzd)}"
          + (f"; {nzd[-1]['date']}: {nzd[-1]['buy']} NPR per NZ$" if nzd else ""))
    print(f"JPY quoted per {next(r['unit'] for r in rows if r['currency'] == 'JPY')} -> "
          f"{jpy:.4f} NPR per yen")


if __name__ == "__main__":
    main()
