"""Exchange rates: NRB quotes some currencies per 10 or per 100 units."""

from datetime import date
from decimal import Decimal

import pytest

import fx

ROWS = [
    {"date": "2026-10-01", "currency": "USD", "unit": "1", "buy": "152.00", "sell": "152.60"},
    {"date": "2026-10-05", "currency": "USD", "unit": "1", "buy": "153.00", "sell": "153.60"},
    {"date": "2026-10-01", "currency": "JPY", "unit": "10", "buy": "9.61", "sell": "9.65"},
    {"date": "2026-10-01", "currency": "INR", "unit": "100", "buy": "160.00", "sell": "160.15"},
]


@pytest.fixture
def rates():
    return fx.Rates(ROWS)


def test_mid_rate_per_single_unit(rates):
    assert rates.npr_per_unit("USD", date(2026, 10, 1)) == (Decimal("152.30"), "2026-10-01")
    assert rates.npr_per_unit("JPY", date(2026, 10, 1)) == (Decimal("0.963"), "2026-10-01")
    assert rates.npr_per_unit("INR", date(2026, 10, 1)) == (Decimal("1.60075"), "2026-10-01")


def test_a_holiday_gap_uses_the_last_rate_on_or_before(rates):
    # 2-4 Oct have no rate (e.g. Dashain): use 1 Oct and say so.
    assert rates.npr_per_unit("USD", date(2026, 10, 3)) == (Decimal("152.30"), "2026-10-01")
    assert rates.npr_per_unit("USD", date(2026, 10, 5))[1] == "2026-10-05"


def test_no_rate_before_the_first_one(rates):
    with pytest.raises(LookupError):
        rates.npr_per_unit("USD", date(2026, 9, 30))


def test_npr_is_always_one(rates):
    assert rates.npr_per_unit("NPR", date(2026, 1, 1)) == (Decimal(1), None)


def test_convert_npr_to_usd(rates):
    usd, rate_date = rates.convert(Decimal(152_300_000), "NPR", "USD", date(2026, 10, 1))
    assert usd == Decimal(1_000_000)
    assert rate_date == "2026-10-01"


def test_unconfirmed_currencies_are_not_converted(rates):
    with pytest.raises(ValueError):
        rates.convert(Decimal(100), "USD?", "NPR", date(2026, 10, 1))


def test_a_currency_nrb_does_not_publish_is_crossed_through_the_euro():
    import fetch_fx
    nrb = [{"date": "2026-10-05", "currency": "EUR", "unit": "1", "buy": "178.60", "sell": "178.60"}]
    ecb = {"2026-10-02": {"NZD": "1.9000"}}          # Friday: ECB publishes no weekend rates
    rows = fetch_fx.cross_rows(nrb, ecb, ["NZD"])
    assert len(rows) == 1 and rows[0]["currency"] == "NZD" and rows[0]["date"] == "2026-10-05"
    assert rows[0]["source"].startswith("ECB")
    assert fx.Rates(nrb + rows).npr_per_unit("NZD", date(2026, 10, 5))[0] == Decimal("94.0")
