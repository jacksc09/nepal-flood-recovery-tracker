"""'Where to donate' listings: strict link rules, evidence, freshness, fallback."""

from datetime import date

import pytest

import orgs

NRCS = {"org_id": "O-nrcs", "name_en": "Nepal Red Cross Society", "type": "red_cross_movement",
        "website": "https://nrcs.org", "donate_url": "https://nrcs.org/donate",
        "donate_domain": "nrcs.org", "verified_on": "2026-10-08", "link_status": "ok",
        "national_fallback": "yes"}
TRUST = {"org_id": "O-trust", "name_en": "Example Trust", "type": "national_ngo",
         "website": "https://example-trust.org.np", "donate_url": "https://example-trust.org.np/give",
         "donate_domain": "example-trust.org.np", "verified_on": "2026-10-08",
         "link_status": "ok", "national_fallback": "no"}


def listing(org_id="O-trust", place_id="NP0329402", day="2026-09-20", excerpt="volunteers handed out tents"):
    return {"org_id": org_id, "place_id": place_id,
            "evidence": {"url": "https://www.onlinekhabar.com/2026/09/1", "kind": "article",
                         "date": day, "excerpt": excerpt}}


def test_a_good_official_link_passes():
    assert orgs.url_problems("https://nrcs.org/donate", NRCS) == []
    assert orgs.url_problems("https://www.nrcs.org/donate", NRCS) == []


@pytest.mark.parametrize("url", [
    "http://nrcs.org/donate",                         # not https
    "https://www.gofundme.com/f/help-rasuwa",         # crowdfunding
    "https://www.facebook.com/nrcs/posts/1",          # social media
    "https://wa.me/9779800000000",                    # WhatsApp
    "https://bit.ly/3xyz",                            # link shortener hides the target
    "https://nrcs.org/qr-code.png",                   # a QR / image, not a page
    "https://nrcs.org.donate-now.com/give",           # look-alike domain
    "https://evil.example/nrcs.org",                  # the name only in the path
])
def test_unsafe_links_are_refused(url):
    assert orgs.url_problems(url, NRCS)


def test_the_approved_domain_must_be_the_orgs_own_or_official():
    assert orgs.domain_problems(TRUST) == []
    assert orgs.domain_problems(dict(TRUST, donate_domain="paypal.me")) != []
    pm_fund = {"website": "https://pmo.gov.np", "donate_domain": "pmo.gov.np"}
    assert orgs.domain_problems(pm_fund) == []


def test_a_listing_needs_evidence_and_a_verified_link():
    assert orgs.listing_problems(listing(), TRUST) == []
    assert orgs.listing_problems(listing(), dict(TRUST, verified_on=""))
    no_evidence = dict(listing(), evidence={})
    assert orgs.listing_problems(no_evidence, TRUST)
    long_quote = listing(excerpt=" ".join(["word"] * 26))
    assert orgs.listing_problems(long_quote, TRUST)


def test_a_dead_link_is_hidden():
    shown = orgs.donate_block("NP0329402", [listing()], {"O-trust": dict(TRUST, link_status="dead"),
                                                          "O-nrcs": NRCS}, date(2026, 10, 11))
    assert shown["kind"] == "national_fallback"


def test_old_evidence_shows_last_seen():
    block = orgs.donate_block("NP0329402", [listing(day="2026-07-01")],
                              {"O-trust": TRUST, "O-nrcs": NRCS}, date(2026, 10, 11))
    assert block["kind"] == "local"
    assert block["items"][0]["freshness"] == "last_seen"
    fresh = orgs.donate_block("NP0329402", [listing()], {"O-trust": TRUST, "O-nrcs": NRCS},
                              date(2026, 10, 11))
    assert fresh["items"][0]["freshness"] == "active"


def test_a_place_with_no_local_listing_gets_the_labelled_national_fallback():
    block = orgs.donate_block("NP0330", [], {"O-trust": TRUST, "O-nrcs": NRCS}, date(2026, 10, 11))
    assert block["kind"] == "national_fallback"
    assert [i["org_id"] for i in block["items"]] == ["O-nrcs"]
    assert "not specific to this place" in block["label"]
    assert orgs.DISCLAIMER in block["disclaimer"]
