"""The ledger: reviewed money announcements, and the rules for totalling them.

data/ledger/ledger.jsonl holds one approved entry per line (written only by
review_ui.py when Jack approves an item). Once an entry has appeared in a
published week it never changes again; data/ledger/corrections.jsonl
(append-only) records every later fix, and apply_corrections() replays them.

The totalling rules, which keep the numbers honest:
  1. Stages are columns, never added together. Each stream of money
     (money_id) is counted once per column, at the LARGEST amount it has
     reached at that stage or a firmer one:
        Promised  = any stage (announced, pledged, committed, disbursed)
        Committed = committed or disbursed
        Disbursed = disbursed
     So 10 crore committed and later 3 crore disbursed shows 10 / 10 / 3.
  2. Roll-up goes upward only: a town's money also counts for its
     municipality and district; a district-level entry counts for the
     district alone and is never split among its municipalities.
  3. Province-, corridor- and national-level money sits in a separate
     "not location-specific" bucket.
  4. Per-household or per-person amounts are not added unless a total
     was computed from a count given in the evidence.
  5. OCHA FTS money is never added to anything here. A link to an FTS
     flow is reported alongside ("also reported to FTS"), nothing more.
  6. What the money is for: each cell also splits its Committed column by
     phase (relief / recovery / reconstruction), taking each stream's phase
     from its firmest entry, so the split always adds up to Committed.
"""

import copy
import json
from datetime import date
from decimal import Decimal

from config import LEDGER_DIR

LEDGER_JSONL = LEDGER_DIR / "ledger.jsonl"
CORRECTIONS_JSONL = LEDGER_DIR / "corrections.jsonl"

STAGES = ["announced", "pledged", "committed", "disbursed"]   # weakest -> firmest
COLUMNS = {"promised": "announced", "committed": "committed", "disbursed": "disbursed"}
UNLOCATED_LEVELS = ("province", "corridor", "national")
PHASES = ["relief", "recovery", "reconstruction", "unspecified"]
REBUILDING_PHASES = ("recovery", "reconstruction")


def _read_jsonl(path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def load_ledger(path=None) -> list:
    # The path is looked up when called (not when this file loads), so a test
    # can point it at a temporary folder.
    return _read_jsonl(path or LEDGER_JSONL)


def load_corrections(path=None) -> list:
    return _read_jsonl(path or CORRECTIONS_JSONL)


def apply_corrections(entries: list, corrections: list, made_by: date = None) -> list:
    """Entries with corrections replayed in order (originals left untouched).

    made_by: only replay corrections made on or before this date, which
    rebuilds the ledger exactly as it stood on that day.
    """
    out = {e["entry_id"]: copy.deepcopy(e) for e in entries}
    for c in sorted(corrections, key=lambda c: (c["corrected_on"], c["correction_id"])):
        if made_by and c["corrected_on"] > made_by.isoformat():
            continue
        if c["entry_id"] in out:
            out[c["entry_id"]][c["field"]] = c["new"]
    return [out[e["entry_id"]] for e in entries]


def _area_keys(e: dict) -> list:
    """Every area an entry counts towards, most specific first."""
    keys = []
    if e.get("place_id") and e["place_id"] != e["pcode"]:
        keys.append(e["place_id"])                    # a town or landmark
    if len(e["pcode"]) == 9:
        keys.append(e["pcode"])                       # its municipality
    keys.append(e["pcode"][:6])                       # its district
    return keys


def _empty_cell() -> dict:
    cell = {col: {"npr": Decimal(0), "usd": Decimal(0)} for col in COLUMNS}
    cell.update(n_entries=0, also_reported_to_fts_npr=Decimal(0),
                by_phase={ph: {"npr": Decimal(0), "usd": Decimal(0)} for ph in PHASES})
    return cell


def _rank(status: str) -> int:
    return STAGES.index(status)


def totals_as_of(entries: list, week_end: date) -> dict:
    """Totals per area for everything announced on or before week_end."""
    live = [e for e in entries
            if e["status"] != "retracted" and e["announced_on"] <= week_end.isoformat()]
    per_unit = [e for e in live
                if e["amount_basis"] != "total" and not e.get("amount_computed")]
    countable = [e for e in live if e not in per_unit]

    streams = {}
    for e in countable:
        streams.setdefault(e["money_id"], []).append(e)

    areas, unlocated = {}, {}
    for money in streams.values():
        # Where a stream counts: its firmest (then latest) entry decides.
        lead = max(money, key=lambda e: (_rank(e["status"]), e["announced_on"], e["entry_id"]))
        if lead["geo_level"] in UNLOCATED_LEVELS:
            cells = [unlocated.setdefault(lead["geo_level"], _empty_cell())]
        else:
            cells = [areas.setdefault(k, _empty_cell()) for k in _area_keys(lead)]
        values = {}
        for col, threshold in COLUMNS.items():
            reached = [e for e in money if _rank(e["status"]) >= _rank(threshold)]
            if reached:
                top = max(reached, key=lambda e: Decimal(e["amount_npr"]))
                values[col] = (Decimal(top["amount_npr"]), Decimal(top["amount_usd"]))
        in_fts = any(e.get("fts_flow_id") for e in money)
        phase = lead.get("phase") if lead.get("phase") in PHASES else "unspecified"
        for cell in cells:
            for col, (n, u) in values.items():
                cell[col]["npr"] += n
                cell[col]["usd"] += u
            if "committed" in values:
                cell["by_phase"][phase]["npr"] += values["committed"][0]
                cell["by_phase"][phase]["usd"] += values["committed"][1]
            cell["n_entries"] += len(money)
            if in_fts:
                cell["also_reported_to_fts_npr"] += values["promised"][0]
    return {"areas": areas, "not_location_specific": unlocated,
            "per_unit_not_totalled": per_unit, "n_entries": len(live)}
