"""Money parsing: the code, never the model, decides the number and currency."""

from decimal import Decimal

import pytest

import amounts


@pytest.mark.parametrize("text, value, currency", [
    # Nepali rupees, Devanagari digits and South Asian units
    ("रु ५ करोड", 50_000_000, "NPR"),
    ("रु. ५०,०००", 50_000, "NPR"),
    ("रू १ लाख", 100_000, "NPR"),
    ("रु ५ करोड ५० लाख", 55_000_000, "NPR"),
    ("२ अर्ब ५० करोड रुपैयाँ", 2_500_000_000, "NPR"),
    ("१ अरब रुपैयाँ", 1_000_000_000, "NPR"),
    ("रुपैयाँ ५ करोडको", 50_000_000, "NPR"),
    ("डेढ करोड रुपैयाँ", 15_000_000, "NPR"),
    ("साढे तीन करोड रुपैयाँ", 35_000_000, "NPR"),
    ("२० हजार रुपैयाँ", 20_000, "NPR"),
    # English-language Nepali press
    ("Rs 5 crore", 50_000_000, "NPR"),
    ("Rs 2.5 crore", 25_000_000, "NPR"),
    ("Rs 50 lakh", 5_000_000, "NPR"),
    ("Rs 1,00,000", 100_000, "NPR"),
    ("Rs 100,000 cash aid", 100_000, "NPR"),
    ("NRs 50,000", 50_000, "NPR"),
    ("Rs 1.2 billion", 1_200_000_000, "NPR"),
    ("NPR 723.67bn", 723_670_000_000, "NPR"),
    ("Rs 5 crore 20 lakh", 52_000_000, "NPR"),
    # Foreign currencies
    ("US$ 8.45 million", 8_450_000, "USD"),
    ("USD 1.02m", 1_020_000, "USD"),
    ("1 million US dollars", 1_000_000, "USD"),
    ("अमेरिकी डलर १० लाख", 1_000_000, "USD"),
    ("A$4,269", 4_269, "AUD"),
    ("€2 million", 2_000_000, "EUR"),
    ("£1m", 1_000_000, "GBP"),
    ("10 million yuan", 10_000_000, "CNY"),
    ("100 million yen", 100_000_000, "JPY"),
    ("IRs 5 crore", 50_000_000, "INR"),
    ("भारु ५ करोड", 50_000_000, "INR"),
    ("5 crore Indian rupees", 50_000_000, "INR"),
])
def test_parses_value_and_currency(text, value, currency):
    assert amounts.parse_amount(text) == (Decimal(value), currency)


@pytest.mark.parametrize("text, value", [
    ("$4,269", 4_269),             # could be US or Australian dollars
    ("$1 million", 1_000_000),
    ("10 lakh dollars", 1_000_000),
    ("१० लाख डलर", 1_000_000),
])
def test_a_bare_dollar_is_flagged_for_human_confirmation(text, value):
    got_value, currency = amounts.parse_amount(text)
    assert got_value == Decimal(value)
    assert currency == "USD?"
    assert amounts.needs_confirmation(currency)


def test_south_asian_units_without_a_currency_are_probably_rupees_but_flagged():
    assert amounts.parse_amount("५ करोड") == (Decimal(50_000_000), "NPR?")
    assert amounts.needs_confirmation("NPR?")
    assert not amounts.needs_confirmation("NPR")


@pytest.mark.parametrize("text", [
    "5 million",                      # no currency at all
    "relief materials",               # no number
    "Rs 5-10 crore",                  # a range is not one amount
    "Rs 10 crore and Rs 5 crore",     # two amounts
    "Rs 5 crore (US$ 330,000)",       # two currencies
    "Rs 0",                           # zero is not money given
    "¥10 million",                    # yen or yuan? can't tell
    "5 thousand",                     # English units, no currency
    "गुरु ५",                          # रु inside a word is not "Rs"
])
def test_refuses_what_it_cannot_parse_with_certainty(text):
    with pytest.raises(amounts.AmountError):
        amounts.parse_amount(text)
