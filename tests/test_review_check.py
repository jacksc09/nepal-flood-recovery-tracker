"""The review app's place check: merging repeats and splitting 'each' amounts.

Runs on synthetic entries in a temporary folder, never on the real ledger.
"""

import json

import pytest

import ledger
import review_ui


def entry(entry_id, money_id, place_id, geo_level, pcode, article_id):
    return {"entry_id": entry_id, "money_id": money_id, "status": "committed",
            "amount_text": "Rs 1 crore", "amount": "10000000", "currency": "NPR",
            "amount_basis": "total", "amount_npr": "10000000", "amount_usd": "65000",
            "donor_name": "Example Trust", "donor_as_written": "Example Trust",
            "recipient_as_written": None, "place_as_written": None,
            "geo_level": geo_level, "pcode": pcode, "place_id": place_id,
            "announced_on": "2026-09-01",
            "sources": [{"article_id": article_id, "url": f"https://example.org/{article_id}",
                         "outlet": "example", "lang": "en", "published_at": "2026-09-01",
                         "title": "t", "evidence_excerpt": "Rs 1 crore"}],
            "review": {"decision": "approved", "edited_fields": [], "reviewed_on": "2026-10-09"},
            "first_published_week": None}


@pytest.fixture
def store(tmp_path, monkeypatch):
    led = tmp_path / "ledger.jsonl"
    rows = [entry("L-000001", "M-000001", "NP0330", "district", "NP0330", "a1"),
            entry("L-000002", "M-000002", "NP0330", "district", "NP0330", "a2"),
            entry("L-000003", "M-000003", "CORRIDOR", "corridor", "CORRIDOR", "a3")]
    led.write_text("".join(json.dumps(r) + "\n" for r in rows))
    sugg = tmp_path / "place_suggestions.jsonl"
    sugg.write_text("".join(json.dumps(r) + "\n" for r in [
        {"entry_id": "L-000002", "geo_level": "district", "place_ids": ["NP0330"], "each": False,
         "same_as": "L-000001", "reason": "same gift", "model": "m"},
        {"entry_id": "L-000003", "geo_level": "district", "place_ids": ["NP0329", "NP0328"],
         "each": True, "same_as": None, "reason": "1 crore each", "model": "m"},
    ]))
    monkeypatch.setattr(ledger, "LEDGER_JSONL", led)
    for name, path in [("QUEUE", tmp_path / "queue.jsonl"), ("JOURNAL", tmp_path / "journal.jsonl"),
                       ("REVIEW_LOG", tmp_path / "review_log.csv"),
                       ("SCOPE_CSV", tmp_path / "fts_scope.csv"),
                       ("TRANSLATIONS", tmp_path / "translations.jsonl"),
                       ("PLACE_SUGGESTIONS", sugg),
                       ("LINK_SUGGESTIONS", tmp_path / "org_link_suggestions.jsonl"),
                       ("DUPLICATE_GROUPS", tmp_path / "duplicate_groups.jsonl")]:
        monkeypatch.setattr(review_ui, name, path)
    monkeypatch.setattr(review_ui.orgs, "ORGS_CSV", tmp_path / "orgs.csv")
    monkeypatch.setattr(review_ui.orgs, "ORG_AREAS", tmp_path / "org_areas.jsonl")
    return review_ui.Store()


def ids(store):
    return {e["entry_id"]: e for e in ledger.load_ledger()}


def test_both_suggestions_are_offered(store):
    assert {c["entry"]["entry_id"] for c in store.state()["checks"]} == {"L-000002", "L-000003"}


def test_accepting_a_repeat_merges_it_as_another_source(store):
    row = store.decide_check({"entry_id": "L-000002", "action": "accept"})
    assert row["decision"] == "merged" and row["entry_id"] == "L-000001"
    after = ids(store)
    assert "L-000002" not in after
    assert [s["article_id"] for s in after["L-000001"]["sources"]] == ["a1", "a2"]


def test_accepting_each_makes_one_entry_per_place(store):
    store.decide_check({"entry_id": "L-000003", "action": "accept"})
    after = ids(store)
    split = [e for e in after.values() if e["entry_id"] == "L-000003"
             or e["review"].get("split_from") == "L-000003"]
    assert sorted(e["place_id"] for e in split) == ["NP0328", "NP0329"]
    assert len({e["money_id"] for e in split}) == 2          # two separate gifts
    assert all(e["amount_npr"] == "10000000" for e in split)  # never divided


def test_keeping_the_current_place_changes_nothing(store):
    before = ids(store)
    store.decide_check({"entry_id": "L-000003", "action": "keep"})
    assert ids(store) == before
    assert store.state()["checks"][0]["entry"]["entry_id"] == "L-000002"


def test_a_check_is_decided_only_once(store):
    store.decide_check({"entry_id": "L-000003", "action": "keep"})
    with pytest.raises(ValueError):
        store.decide_check({"entry_id": "L-000003", "action": "accept"})


def test_a_split_never_duplicates_a_place_that_already_has_this_gift(tmp_path, monkeypatch, store):
    # L-000001 is already the Dhading entry for this gift; splitting L-000003
    # into Dhading + Rasuwa must add a source to L-000001, not a second Dhading entry.
    sugg = tmp_path / "place_suggestions.jsonl"
    sugg.write_text(json.dumps({"entry_id": "L-000003", "geo_level": "district",
                                "place_ids": ["NP0330", "NP0329"], "each": True, "same_as": None,
                                "reason": "each", "model": "m"}) + "\n")
    store.decide_check({"entry_id": "L-000003", "action": "accept"})
    after = ids(store)
    dhading = [e for e in after.values() if e["place_id"] == "NP0330"]
    assert len(dhading) == 2                       # L-000001 and L-000002 only
    assert "a3" in [s["article_id"] for s in after["L-000001"]["sources"]]
    rasuwa = [e for e in after.values() if e["place_id"] == "NP0329"]
    assert len(rasuwa) == 1 and rasuwa[0]["entry_id"] == "L-000003"


def extra(tmp_path, monkeypatch, rows):
    path = tmp_path / "extra_suggestions.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(review_ui, "EXTRA_SUGGESTIONS", path)


def test_accepting_a_remove_takes_a_double_counting_total_out(tmp_path, monkeypatch, store):
    extra(tmp_path, monkeypatch, [{"check_id": "x1", "entry_id": "L-000003", "kind": "remove",
                                   "same_as": None, "parts": ["L-000001", "L-000002"],
                                   "reason": "total of its parts", "found_by": "test"}])
    assert "x1" in {c["check_id"] for c in store.state()["checks"]}
    row = store.decide_check({"check_id": "x1", "action": "accept"})
    assert row["decision"] == "removed"
    assert "L-000003" not in ids(store)


def test_accepting_a_stage_link_makes_one_money_stream(tmp_path, monkeypatch, store):
    extra(tmp_path, monkeypatch, [{"check_id": "x2", "entry_id": "L-000002", "kind": "stage",
                                   "same_as": "L-000001", "parts": [], "reason": "later stage",
                                   "found_by": "test"}])
    store.decide_check({"check_id": "x2", "action": "accept"})
    after = ids(store)
    assert after["L-000002"]["money_id"] == after["L-000001"]["money_id"]
    assert len(after["L-000002"]["sources"]) == 1          # not merged: both entries stay


def test_link_suggestions_are_shown_but_never_saved_or_checked(store):
    store.save_org_record({"name_en": "Example Trust", "type": "national_ngo",
                           "website": "https://example-trust.org.np"})
    review_ui.LINK_SUGGESTIONS.write_text("".join(json.dumps(r) + "\n" for r in [
        {"org_id": "O-example-trust", "website": "https://example-trust.org.np",
         "donate_url": "https://example-trust.org.np/give", "found_on": "https://example-trust.org.np",
         "note": "", "suggested_on": "2026-10-14"},
        {"name_en": "Example Relief Fund", "type": "government_fund",
         "website": "https://relief.example.gov.np", "donate_url": "https://wa.me/9779800000000",
         "found_on": "https://relief.example.gov.np", "note": "", "suggested_on": "2026-10-14"},
    ]))
    before = review_ui.orgs.ORGS_CSV.read_text()
    st = store.state()
    rec = next(r for r in st["org_records"] if r["org_id"] == "O-example-trust")
    assert rec["suggestions"][0]["donate_url"] == "https://example-trust.org.np/give"
    assert rec["suggestions"][0]["problems"] == []
    assert rec["donate_url"] == "" and rec["verified_on"] == ""
    [new] = st["new_org_suggestions"]
    assert new["name_en"] == "Example Relief Fund" and new["problems"]   # a WhatsApp link shows as refused
    assert review_ui.orgs.ORGS_CSV.read_text() == before                  # showing saves nothing

    entered = store.save_org_record({"org_id": "O-example-trust",
                                     "donate_url": "https://example-trust.org.np/give"})
    assert entered["verified_on"] == ""        # typing a link in is not checking it
    checked = store.save_org_record({"org_id": "O-example-trust",
                                     "donate_url": "https://example-trust.org.np/give",
                                     "confirm_verified": True})
    assert checked["verified_on"] and checked["link_status"] == "ok"


def queued(item_id):
    return {"item_id": item_id, "kind": "ledger",
            "draft": {"amount_text": "Rs 15,000", "amount_basis": "per_household",
                      "donor_name_en": "Government of Nepal", "evidence_excerpt": "Rs 15,000 per month"},
            "article": {"title": "t", "outlet": "example", "url": "https://example.org/a",
                        "published_at_utc": "2026-10-05T00:00:00Z"}}


def test_a_duplicate_group_rejects_only_the_ticked_pending_copies(store):
    review_ui.QUEUE.write_text("".join(json.dumps(queued(f"2026-W41:a{n}:e1")) + "\n" for n in range(4)))
    review_ui.DUPLICATE_GROUPS.write_text(json.dumps(
        {"group_id": "G-1", "label": "one allowance", "reason": "2",
         "item_ids": [f"2026-W41:a{n}:e1" for n in range(4)]}) + "\n")
    s = review_ui.Store()
    before = ledger.LEDGER_JSONL.read_text()
    s.decide_ledger({"item_id": "2026-W41:a0:e1", "action": "reject", "reason": "2"})   # decided by hand earlier
    [g] = s.duplicate_groups({})
    assert [i["item_id"] for i in g["items"]] == ["2026-W41:a1:e1", "2026-W41:a2:e1", "2026-W41:a3:e1"]
    with pytest.raises(ValueError):
        s.reject_group({"group_id": "G-1", "item_ids": ["2026-W41:a1:e1"], "reason": "9"})
    r = s.reject_group({"group_id": "G-1", "item_ids": ["2026-W41:a1:e1", "2026-W41:a2:e1"], "reason": "2"})
    assert r["rejected"] == 2
    log = {row["item_id"]: row for row in review_ui.read_csv(review_ui.REVIEW_LOG)}
    assert log["2026-W41:a1:e1"]["decision"] == "rejected"
    assert log["2026-W41:a1:e1"]["reason"] == "duplicate of an existing entry"
    assert "2026-W41:a3:e1" not in log                                   # unticked: still pending
    assert [i["item_id"] for i in s.duplicate_groups({})[0]["items"]] == ["2026-W41:a3:e1"]
    assert ledger.LEDGER_JSONL.read_text() == before                     # rejections never touch the ledger
