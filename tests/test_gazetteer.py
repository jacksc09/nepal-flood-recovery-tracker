"""Place matching: which corridor places does a news text mention?"""

import pytest

from gazetteer import Gazetteer, Place


def place(place_id, pcode, en, ne="", aliases_en=(), aliases_ne=(), kind="municipality",
          role="core", needs_context=False):
    return Place(place_id=place_id, kind=kind, pcode=pcode, parent_pcode=pcode[:6],
                 name_en=en, name_ne=ne, aliases_en=list(aliases_en),
                 aliases_ne=list(aliases_ne), lat=None, lon=None,
                 corridor_role=role, needs_context=needs_context)


@pytest.fixture
def gaz():
    return Gazetteer([
        place("NP0329", "NP0329", "Rasuwa", "रसुवा", kind="district"),
        place("NP0436", "NP0436", "Gorkha", "गोरखा", kind="district", role="downstream"),
        place("NP0329402", "NP0329402", "Gosaikunda", "गोसाईकुण्ड",
              aliases_ne=["गोसाइँकुण्ड"]),
        place("NP0329404", "NP0329404", "Kalika", "कालिका", needs_context=True),
        place("NP0335404", "NP0335404", "Kalika", "कालिका", role="downstream",
              needs_context=True),
        place("NP0335", "NP0335", "Chitawan", "चितवन", aliases_en=["Chitwan"],
              kind="district", role="downstream"),
        place("T-syafrubesi", "NP0329402", "Syafrubesi", "स्याफ्रुबेसी",
              aliases_en=["Syabrubesi", "Syapru Besi"], kind="town"),
        place("T-bidur", "NP0328301", "Trishuli Bazar", "त्रिशूली बजार", kind="town"),
        place("R-trishuli", "CORRIDOR", "Trishuli", "त्रिशूली", kind="river"),
        place("R-bhotekoshi", "CORRIDOR", "Bhote Koshi", "भोटेकोशी", kind="river",
              needs_context=True),
        place("NP0328597", "NP0328597", "Langtang", "लाङटाङ"),   # a 5xx national park
    ])


def ids(matches):
    return sorted(m.place_id for m in matches if m.place_id)


def test_english_matches_whole_words_only(gaz):
    assert ids(gaz.match("Rasuwa's farmers")) == ["NP0329"]
    assert ids(gaz.match("Gorkhapatra reported")) == []
    assert ids(gaz.match("the GORKHA district")) == ["NP0436"]


def test_nepali_matches_with_case_endings(gaz):
    assert ids(gaz.match("रसुवाको बाढी")) == ["NP0329"]
    assert ids(gaz.match("रसुवामा")) == ["NP0329"]
    assert ids(gaz.match("गोसाइँकुण्ड गाउँपालिकाले")) == ["NP0329402"]


def test_nepali_does_not_match_inside_a_word_or_a_false_friend(gaz):
    assert ids(gaz.match("पूर्वरसुवा")) == []
    assert ids(gaz.match("गोरखापत्र दैनिक")) == []


def test_matching_ignores_invisible_joiners(gaz):
    assert ids(gaz.match("रसु‍वाको")) == ["NP0329"]


def test_longest_alias_wins(gaz):
    found = gaz.match("flooding at Trishuli Bazar")
    assert ids(found) == ["T-bidur"]
    assert ids(gaz.match("the Trishuli river")) == ["R-trishuli"]


def test_town_spelling_variants_resolve_to_the_town(gaz):
    for text in ["Syabrubesi", "Syapru Besi", "स्याफ्रुबेसीमा"]:
        found = gaz.match(text)
        assert ids(found) == ["T-syafrubesi"]
        assert found[0].pcode == "NP0329402"


def test_bhote_koshi_needs_a_corridor_district_alongside(gaz):
    assert ids(gaz.match("Bhote Koshi flood in Sindhupalchok")) == []
    assert ids(gaz.match("Bhote Koshi flood in Rasuwa")) == ["NP0329", "R-bhotekoshi"]


def test_ambiguous_name_resolved_by_the_district_mentioned(gaz):
    found = [m for m in gaz.match("Kalika rural municipality, Rasuwa") if m.alias == "Kalika"]
    assert found[0].place_id == "NP0329404"
    found = [m for m in gaz.match("Kalika, Chitwan") if m.alias == "Kalika"]
    assert found[0].place_id == "NP0335404"


def test_ambiguous_name_without_a_district_is_dropped(gaz):
    assert ids(gaz.match("Kalika temple")) == []


def test_protected_area_pseudo_units_are_never_matched(gaz):
    assert ids(gaz.match("Langtang National Park, Rasuwa")) == ["NP0329"]


def test_resolve_a_single_place_name_in_its_article(gaz):
    article = "Rasuwa: the Kalika rural municipality received Rs 5 crore."
    assert gaz.resolve("Kalika", article) == ["NP0329404"]
    assert gaz.resolve("Kalika", "Kalika received Rs 5 crore.") == ["NP0329404", "NP0335404"]
    assert gaz.resolve("Pokhara", article) == []
