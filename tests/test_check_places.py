"""Cleaning up place-check suggestions before they reach the reviewer."""

import check_places


def entry(entry_id, place_id, day="2026-08-26", amount="10000000", donor="Government of Nepal"):
    return {"entry_id": entry_id, "donor_name": donor, "amount_npr": amount,
            "announced_on": day, "geo_level": "district", "place_id": place_id}


def sug(entry_id, place_ids, same_as=None):
    return {"entry_id": entry_id, "geo_level": "district", "place_ids": place_ids,
            "each": len(place_ids) > 1, "same_as": same_as, "reason": "r", "model": "m"}


def test_entries_already_split_by_place_are_not_split_again_and_repeat_within_their_place():
    entries = {e["entry_id"]: e for e in [
        entry("L-1", "NP0329"), entry("L-2", "NP0328"),          # article 1: Rasuwa, Nuwakot
        entry("L-3", "NP0329"), entry("L-4", "NP0328")]}         # article 2: the same gifts
    suggestions = {s["entry_id"]: s for s in [
        sug("L-1", ["NP0329", "NP0328"]), sug("L-2", ["NP0329", "NP0328"], "L-1"),
        sug("L-3", ["NP0329", "NP0328"], "L-1"), sug("L-4", ["NP0329", "NP0328"], "L-1")]}
    fixed = check_places.reconcile(suggestions, entries, order=["L-1", "L-2", "L-3", "L-4"])
    assert [fixed[i]["each"] for i in ("L-1", "L-2", "L-3", "L-4")] == [False] * 4
    assert fixed["L-1"]["same_as"] is None and fixed["L-2"]["same_as"] is None
    assert fixed["L-3"]["same_as"] == "L-1"       # Rasuwa repeat -> the Rasuwa entry
    assert fixed["L-4"]["same_as"] == "L-2"       # Nuwakot repeat -> the Nuwakot entry, never Rasuwa


def test_a_repeat_must_be_in_the_same_place():
    entries = {"L-1": entry("L-1", "NP0329"), "L-2": entry("L-2", "NP0330")}
    suggestions = {"L-1": sug("L-1", ["NP0329"]), "L-2": sug("L-2", ["NP0330"], "L-1")}
    fixed = check_places.reconcile(suggestions, entries, order=["L-1", "L-2"])
    assert fixed["L-2"]["same_as"] is None
