"""Ledger totals: stage columns, upward-only roll-up, and what never gets added."""

from datetime import date
from decimal import Decimal

import ledger

CR = 10_000_000   # one crore


def entry(entry_id, money_id, status, crore, pcode="NP0329402", place_id=None,
          geo_level="municipality", on="2026-09-10", basis="total", computed=False,
          fts=None):
    return {"entry_id": entry_id, "money_id": money_id, "status": status,
            "amount_npr": str(crore * CR), "amount_usd": str(crore * CR // 150),
            "amount_basis": basis, "amount_computed": computed, "geo_level": geo_level,
            "pcode": pcode, "place_id": place_id or pcode, "announced_on": on,
            "fts_flow_id": fts}


WEEK_END = date(2026, 9, 13)


def npr(cell, col):
    return cell[col]["npr"]


def test_stage_columns_use_the_largest_amount_at_each_stage():
    t = ledger.totals_as_of([entry("L1", "M1", "announced", 10),
                             entry("L2", "M1", "committed", 8)], WEEK_END)
    cell = t["areas"]["NP0329402"]
    assert (npr(cell, "promised"), npr(cell, "committed"), npr(cell, "disbursed")) == \
        (Decimal(10 * CR), Decimal(8 * CR), Decimal(0))


def test_a_partial_disbursement_does_not_shrink_what_was_committed():
    t = ledger.totals_as_of([entry("L1", "M1", "committed", 10),
                             entry("L2", "M1", "disbursed", 3)], WEEK_END)
    cell = t["areas"]["NP0329402"]
    assert (npr(cell, "promised"), npr(cell, "committed"), npr(cell, "disbursed")) == \
        (Decimal(10 * CR), Decimal(10 * CR), Decimal(3 * CR))


def test_separate_money_streams_add_up():
    t = ledger.totals_as_of([entry("L1", "M1", "committed", 10),
                             entry("L2", "M2", "committed", 5)], WEEK_END)
    assert npr(t["areas"]["NP0329402"], "committed") == Decimal(15 * CR)


def test_roll_up_goes_upward_only():
    t = ledger.totals_as_of([
        entry("L1", "M1", "committed", 10, pcode="NP0329402"),            # municipality
        entry("L2", "M2", "committed", 4, pcode="NP0329", geo_level="district"),
        entry("L3", "M3", "committed", 1, pcode="NP0329402", place_id="T-syafrubesi"),
    ], WEEK_END)
    areas = t["areas"]
    assert npr(areas["NP0329"], "committed") == Decimal(15 * CR)       # 10 + 4 + 1
    assert npr(areas["NP0329402"], "committed") == Decimal(11 * CR)    # never gets the 4
    assert npr(areas["T-syafrubesi"], "committed") == Decimal(1 * CR)
    assert areas["NP0329"]["n_entries"] == 3


def test_per_household_amounts_without_a_total_are_listed_not_added():
    t = ledger.totals_as_of([entry("L1", "M1", "disbursed", 1, basis="per_household"),
                             entry("L2", "M2", "disbursed", 2, basis="per_household",
                                   computed=True)], WEEK_END)
    assert npr(t["areas"]["NP0329402"], "disbursed") == Decimal(2 * CR)
    assert [e["entry_id"] for e in t["per_unit_not_totalled"]] == ["L1"]


def test_corridor_and_national_money_is_not_location_specific():
    t = ledger.totals_as_of([
        entry("L1", "M1", "committed", 7, pcode="CORRIDOR", geo_level="corridor"),
        entry("L2", "M2", "committed", 2, pcode="NATIONAL", geo_level="national"),
    ], WEEK_END)
    assert t["areas"] == {}
    assert npr(t["not_location_specific"]["corridor"], "committed") == Decimal(7 * CR)
    assert npr(t["not_location_specific"]["national"], "committed") == Decimal(2 * CR)


def test_only_money_announced_by_the_week_end_counts():
    t = ledger.totals_as_of([entry("L1", "M1", "committed", 10, on="2026-09-14")], WEEK_END)
    assert t["areas"] == {}


def test_corrections_apply_and_a_retraction_removes_an_entry():
    entries = [entry("L1", "M1", "committed", 10), entry("L2", "M2", "committed", 5)]
    corrections = [
        {"correction_id": "C1", "entry_id": "L1", "field": "amount_npr",
         "new": str(9 * CR), "corrected_on": "2026-10-01"},
        {"correction_id": "C2", "entry_id": "L2", "field": "status",
         "new": "retracted", "corrected_on": "2026-10-02"},
    ]
    fixed = ledger.apply_corrections(entries, corrections)
    t = ledger.totals_as_of(fixed, WEEK_END)
    assert npr(t["areas"]["NP0329402"], "committed") == Decimal(9 * CR)
    # the originals are untouched: corrections never rewrite history
    assert entries[0]["amount_npr"] == str(10 * CR)
    # and a correction made later than a given date can be left out
    early = ledger.apply_corrections(entries, corrections, made_by=date(2026, 9, 30))
    assert early == entries


def test_an_fts_link_is_reported_alongside_never_added_or_subtracted():
    t = ledger.totals_as_of([entry("L1", "M1", "committed", 10, fts="400013")], WEEK_END)
    cell = t["areas"]["NP0329402"]
    assert npr(cell, "committed") == Decimal(10 * CR)
    assert cell["also_reported_to_fts_npr"] == Decimal(10 * CR)
    assert not any("fts" in key and "also" not in key for key in cell)


def test_committed_money_splits_by_phase_and_adds_up():
    relief = dict(entry("L1", "M1", "announced", 6), phase="relief")
    relief_firm = dict(entry("L2", "M1", "committed", 5), phase="reconstruction")   # firmest decides
    rebuild = dict(entry("L3", "M2", "committed", 3), phase="reconstruction")
    pledge = dict(entry("L4", "M3", "pledged", 9), phase="recovery")               # not committed
    cell = ledger.totals_as_of([relief, relief_firm, rebuild, pledge], WEEK_END)["areas"]["NP0329402"]
    by = {ph: v["npr"] for ph, v in cell["by_phase"].items()}
    assert by == {"relief": 0, "recovery": 0, "reconstruction": Decimal(8 * CR), "unspecified": 0}
    assert sum(by.values()) == npr(cell, "committed")
