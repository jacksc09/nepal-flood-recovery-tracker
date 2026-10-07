"""Convert between currencies with Nepal Rastra Bank's published rates.

data/processed/fx_rates.csv (written by fetch_fx.py) holds NRB's buy and
sell rates in rupees. Two rules:
  * NRB quotes some currencies per 10 or per 100 units (JPY per 10, INR
    and KRW per 100). The `unit` column says which; rates here are always
    turned into rupees per ONE unit.
  * A date with no rate (a holiday such as Dashain or Tihar) uses the last
    rate published on or before that date, and reports which date that was.

The rate used is the middle of buy and sell.
"""

import bisect
import csv
from datetime import date
from decimal import Decimal

from config import PROCESSED

FX_CSV = PROCESSED / "fx_rates.csv"


class Rates:
    def __init__(self, rows: list):
        self._by_currency = {}   # currency -> (sorted dates, rates per one unit)
        table = {}
        for r in rows:
            mid = (Decimal(r["buy"]) + Decimal(r["sell"])) / 2 / Decimal(r["unit"])
            table.setdefault(r["currency"], {})[r["date"]] = mid
        for currency, by_day in table.items():
            days = sorted(by_day)
            self._by_currency[currency] = (days, [by_day[d] for d in days])

    @classmethod
    def load(cls, path=FX_CSV) -> "Rates":
        with open(path, newline="", encoding="utf-8") as f:
            return cls(list(csv.DictReader(f)))

    def npr_per_unit(self, currency: str, day: date) -> tuple:
        """(rupees per one unit, date of the rate used) on or before day."""
        if currency == "NPR":
            return Decimal(1), None
        if currency not in self._by_currency:
            raise LookupError(f"NRB publishes no rate for {currency}")
        days, mids = self._by_currency[currency]
        i = bisect.bisect_right(days, day.isoformat()) - 1
        if i < 0:
            raise LookupError(f"no {currency} rate on or before {day}")
        return mids[i], days[i]

    def convert(self, amount: Decimal, from_cur: str, to_cur: str, day: date) -> tuple:
        """(converted amount, rate date) - via rupees, at the day's mid rates."""
        if from_cur.endswith("?") or to_cur.endswith("?"):
            raise ValueError(f"currency not confirmed: {from_cur} -> {to_cur}")
        src, src_day = self.npr_per_unit(from_cur, day)
        dst, dst_day = self.npr_per_unit(to_cur, day)
        return amount * src / dst, src_day or dst_day
