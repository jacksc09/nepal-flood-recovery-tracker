"""The mechanical gate between a drafted line and human review."""

import copy
import json
from decimal import Decimal

import pytest

import check_drafts
from extract_prompt import EXAMPLES

TEXTS = {ex["output"]["article_id"]: ex["article"] for ex in EXAMPLES}
IDS = set(TEXTS)


def run(output: dict, ids=IDS, texts=TEXTS):
    return check_drafts.check_line(json.dumps(output, ensure_ascii=False), ids, texts.get)


def example(n: int) -> dict:
    return copy.deepcopy(EXAMPLES[n]["output"])


@pytest.mark.parametrize("n", range(len(EXAMPLES)))
def test_every_worked_example_passes_its_own_checks(n):
    result = run(example(n))
    assert result.errors == []


def test_the_code_computes_amount_and_currency():
    result = run(example(2))
    first = result.entries[0]
    assert first["amount"] == Decimal(50_000_000) and first["currency"] == "NPR"
    assert result.entries[1]["amount"] == Decimal(15_000_000)


def test_an_invented_excerpt_is_rejected():
    out = example(0)
    out["entries"][0]["evidence_excerpt"] = ("The Example Relief Trust will provide Rs 2.5 crore "
                                             "to Gosaikunda.")
    assert any("not found in the article" in e for e in run(out).errors)


def test_a_changed_digit_is_rejected():
    out = example(0)
    out["entries"][0]["amount_text"] = "Rs 3.5 crore"
    assert any("amount_text" in e for e in run(out).errors)


def test_a_currency_the_parser_disagrees_with_is_rejected():
    out = example(0)
    out["entries"][0]["currency_hint"] = "USD"
    assert any("currency" in e for e in run(out).errors)


def test_a_bare_dollar_must_be_left_unknown():
    out = example(1)
    out["entries"][1]["currency_hint"] = "USD"
    assert any("currency" in e for e in run(out).errors)
    ok = run(example(1))
    assert ok.errors == [] and ok.entries[1]["currency"] == "USD?"


def test_a_place_or_donor_missing_from_the_article_is_rejected():
    out = example(0)
    out["entries"][0]["place_as_written"] = "Syabrubesi"
    assert any("place_as_written" in e for e in run(out).errors)
    out = example(0)
    out["entries"][0]["donor_as_written"] = "World Bank"
    assert any("donor_as_written" in e for e in run(out).errors)


def test_a_unit_count_must_come_from_the_excerpt():
    out = example(1)
    out["entries"][0]["unit_count_text"] = "500 families"
    assert any("unit_count_text" in e for e in run(out).errors)


def test_an_overlong_excerpt_is_rejected():
    long_text = " ".join(["word"] * 50) + " Rs 5 crore"
    out = {"article_id": "x", "orgs": [], "entries": [dict(example(0)["entries"][0],
            evidence_excerpt=long_text, amount_text="Rs 5 crore",
            place_as_written=None, donor_as_written="word")]}
    result = run(out, ids={"x"}, texts={"x": long_text})
    assert any("words" in e for e in result.errors)


def test_money_for_another_disaster_is_dropped_not_rejected():
    out = example(0)
    out["entries"][0]["is_this_flood"] = False
    result = run(out)
    assert result.errors == [] and result.entries == [] and len(result.dropped) == 1


def test_an_unknown_article_or_a_bad_enum_is_rejected():
    out = example(3)
    out["article_id"] = "not-in-this-packet"
    assert run(out).errors
    out = example(0)
    out["entries"][0]["status"] = "promised"
    assert run(out).errors


def test_one_line_per_article():
    lines = [json.dumps(example(3)), json.dumps(example(3))]
    report = check_drafts.check_packet_lines(lines, {"example4"}, TEXTS.get)
    assert report.valid == {"example4"}
    assert any("more than once" in e for _, e in report.errors)
    assert check_drafts.check_packet_lines([], {"example4"}, TEXTS.get).missing == {"example4"}
