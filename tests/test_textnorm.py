"""Text normalisation used for matching (never for display)."""

import textnorm


def test_zero_width_joiners_and_soft_hyphens_are_removed():
    assert textnorm.norm("रसुवा‍को") == "रसुवाको"
    assert textnorm.norm("रसुवा‌मा") == "रसुवामा"
    assert textnorm.norm("recon­struction") == "reconstruction"


def test_nfc_makes_both_spellings_of_a_nukta_letter_equal():
    precomposed = "क़"            # क़ as one code point
    decomposed = "क़"       # क + nukta
    assert textnorm.norm(precomposed) == textnorm.norm(decomposed)


def test_quotes_and_dashes_are_unified():
    assert textnorm.norm("“Rs 5 crore” – it’s") == '"Rs 5 crore" - it\'s'
    assert textnorm.norm("2026—27") == "2026-27"


def test_whitespace_collapses_to_single_spaces():
    assert textnorm.norm("  Rs 5\n\n crore\t ") == "Rs 5 crore"


def test_devanagari_digits_become_ascii():
    assert textnorm.norm("रु ५ करोड ९७४") == "रु 5 करोड 974"


def test_contains_normalises_both_sides():
    article = "प्रदेश सरकारले रु ५ करोड‍ दिने निर्णय गरेको छ ।"
    assert textnorm.contains(article, "रु 5 करोड")
    assert textnorm.contains(article, "रु ५  करोड")
    assert not textnorm.contains(article, "रु 6 करोड")
    assert not textnorm.contains(article, "")
