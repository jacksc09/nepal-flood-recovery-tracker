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


def test_a_listing_needs_evidence():
    assert orgs.listing_problems(listing()) == []
    no_evidence = dict(listing(), evidence={})
    assert orgs.listing_problems(no_evidence)
    long_quote = listing(excerpt=" ".join(["word"] * 26))
    assert orgs.listing_problems(long_quote)
    trimmed = listing(excerpt=" ".join(["word"] * 25) + " …")   # as the review app stores a cut quote
    assert orgs.listing_problems(trimmed) == []


def test_a_link_needs_a_hand_check_and_a_live_safe_page():
    assert orgs.link_problems(TRUST) == []
    assert orgs.link_problems(dict(TRUST, verified_on=""))
    assert orgs.link_problems(dict(TRUST, link_status="dead"))
    assert orgs.link_problems(dict(TRUST, donate_url="https://www.gofundme.com/f/x"))


WEEK_END = date(2026, 10, 11)
CHAINS = {"NP0329": ["NP0329"], "NP0329402": ["NP0329402", "NP0329"],
          "NP0329401": ["NP0329401", "NP0329"], "T-syafrubesi": ["T-syafrubesi", "NP0329402", "NP0329"],
          "NP0330": ["NP0330"]}


def block(place_id, listings, orgs_by_id):
    return orgs.donate_block(place_id, listings, orgs_by_id, WEEK_END, CHAINS)


def test_an_org_with_evidence_is_listed_before_its_link_is_checked():
    b = block("NP0329402", [listing()], {"O-trust": dict(TRUST, verified_on=""), "O-nrcs": NRCS})
    assert b["working_scope"] == "here"
    [item] = b["working"]
    assert item["org_id"] == "O-trust" and item["evidence_date"] == "2026-09-20"
    assert "donate_url" not in item and "verified_on" not in item
    assert [i["org_id"] for i in b["national"]] == ["O-nrcs"]    # nothing here to give through yet


def test_a_checked_link_is_shown_and_the_national_block_steps_aside():
    b = block("NP0329402", [listing()], {"O-trust": TRUST, "O-nrcs": NRCS})
    assert b["working"][0]["donate_url"] == "https://example-trust.org.np/give"
    assert b["working"][0]["verified_on"] == "2026-10-08"
    assert b["national"] == []


@pytest.mark.parametrize("org", [dict(TRUST, link_status="dead"),
                                 dict(TRUST, donate_url="https://wa.me/9779800000000")])
def test_a_dead_or_unsafe_link_is_hidden_but_the_org_stays_listed(org):
    b = block("NP0329402", [listing()], {"O-trust": org, "O-nrcs": NRCS})
    assert [i["org_id"] for i in b["working"]] == ["O-trust"]
    assert "donate_url" not in b["working"][0]
    assert [i["org_id"] for i in b["national"]] == ["O-nrcs"]


def test_evidence_counts_upward_only():
    town = listing(place_id="T-syafrubesi")
    by_id = {"O-trust": TRUST}
    assert [i["place_id"] for i in block("NP0329", [town], by_id)["working"]] == ["T-syafrubesi"]
    assert block("NP0329402", [town], by_id)["working_scope"] == "here"
    district_only = listing(place_id="NP0329")
    assert block("NP0329", [district_only], by_id)["working_scope"] == "here"


def test_a_place_with_no_evidence_of_its_own_shows_its_district_labelled():
    b = block("NP0329401", [listing(place_id="NP0329402")], {"O-trust": TRUST})
    assert b["working_scope"] == "district" and b["working_area"] == "NP0329"
    town = block("T-syafrubesi", [listing(place_id="NP0329402")], {"O-trust": TRUST})
    assert town["working_scope"] == "municipality" and town["working_area"] == "NP0329402"


def test_evidence_after_the_week_end_is_not_shown():
    b = block("NP0329402", [listing(day="2026-10-12")], {"O-trust": TRUST, "O-nrcs": NRCS})
    assert b["working"] == [] and b["working_scope"] == "none"


def test_old_evidence_shows_last_seen():
    old = block("NP0329402", [listing(day="2026-07-01")], {"O-trust": TRUST})
    assert old["working"][0]["freshness"] == "last_seen"
    fresh = block("NP0329402", [listing()], {"O-trust": TRUST})
    assert fresh["working"][0]["freshness"] == "active"


def test_a_place_with_no_listing_gets_the_labelled_national_fallback():
    b = block("NP0330", [], {"O-trust": TRUST, "O-nrcs": NRCS})
    assert b["working"] == [] and b["working_scope"] == "none"
    assert [i["org_id"] for i in b["national"]] == ["O-nrcs"]
    assert "not specific to this place" in b["national_label"]
    assert orgs.DISCLAIMER in b["disclaimer"]
