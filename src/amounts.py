"""Turn an amount as written ("रु ५ करोड", "US$ 8.45 million") into a number.

This is the code that decides every figure in the ledger. The language
model only copies the amount text out of an article; this parser works out
the value and the currency, and a draft whose currency disagrees with the
parser is rejected. So the parser must be strict: when it cannot be sure,
it raises AmountError instead of guessing.

What it understands:
  * ASCII and Devanagari digits, with any comma grouping (1,00,000)
  * Nepali/South Asian units: सय (hundred), हजार (thousand), लाख/lakh
    (100,000), करोड/crore (10 million), अर्ब/arba (1 billion),
    खर्ब/kharba (100 billion)
  * English units: hundred, thousand/k, million/m/mn, billion/bn
  * Nepali number words before a unit (एक करोड, डेढ करोड = 1.5 crore,
    साढे तीन करोड = 3.5 crore)
  * compounds with units getting smaller (रु ५ करोड ५० लाख), which may
    end in a plain number below a hundreds/thousands unit
    (५५ लाख ५५ हजार ५५५ = 5,555,555)
  * a repeated number, which in Nepali means "each" (१०–१० हजार = 10
    thousand each), as opposed to a range (5-10 crore), which is refused
  * currencies: NPR, USD, AUD, CAD, EUR, GBP, CHF, CNY, JPY, INR

Two results carry a "?" and must be confirmed by a person in review:
  * "USD?" - a bare "$" or "dollars" (Nepali news also reports Australian
    and Canadian dollar fundraising)
  * "NPR?" - South Asian units with no currency named ("५ करोड")

Pure functions only.
"""

import re
from decimal import Decimal

from textnorm import norm


class AmountError(ValueError):
    """The text is not one unambiguous amount of money."""


_DEV = "ऀ-ॿ"   # the Devanagari block, for "not inside a word" checks

# (currency code, regex). Order matters: specific forms first, because each
# match is blanked out before the next pattern runs. "Indian rupees" must be
# taken as INR before the plain "rupees" pattern can call it NPR, and
# "US$"/"A$" before a bare "$".
_CURRENCY_PATTERNS = [
    ("INR", r"indian\s+rupees?|\birs\.?(?=[\s\d]|$)|\binr\b|भारु|भारतीय\s+रु\S*"),
    # Nepali spells the countries several ways (अष्ट्रेलियन, अस्ट्रेलियाली...).
    ("AUD", r"\bau?\$|\baud\b|australian\s+dollars?|(?:अष्ट्र|अस्ट्र|आस्ट्र|आष्ट्र)\S*\s+डलर\S*"),
    ("CAD", r"\bc\$|\bcad\b|canadian\s+dollars?|(?:क्यानेडि|क्यानडा|क्यानाडा)\S*\s+डलर\S*"),
    ("USD", r"\bus\s?\$|\busd\b|\bu\.?s\.?\s+dollars?|american\s+dollars?"
            r"|(?:अमेरिकी|अमेरिकन|युएस)\s+डलर\S*"),
    ("EUR", r"€|\beur\b|\beuros?\b|युरो\S*"),
    ("GBP", r"£|\bgbp\b|\bpounds?\s+sterling\b|\bpounds?\b|\bsterling\b|पाउन्ड\S*"),
    ("CNY", r"\byuan\b|\brmb\b|\bcny\b|\brenminbi\b|युआन\S*"),
    ("JPY", r"\byen\b|\bjpy\b|येन\S*"),
    ("CHF", r"\bchf\b|swiss\s+francs?|स्विस\s+फ्र\S*"),
    ("¥", r"¥"),   # used for both yen and yuan: refused below
    ("NPR", r"\bnrs\.?|\bnpr\b|\brs\.?(?=[\s\d]|$)"
            rf"|(?<![{_DEV}])(?:रु|रू)\.?(?=[\s\d]|$)"
            r"|रुप[ैि]\S*|\bnepali\s+rupees?|\brupees?\b"),
    ("USD?", r"\$|\bdollars?\b|डलर\S*"),
]
_CURRENCY_RES = [(code, re.compile(rx)) for code, rx in _CURRENCY_PATTERNS]

# Units. Nepali units are matched as a word START so case endings still
# match (करोडको = "of crore"); English units need a word boundary after.
_UNITS_NE = {"सय": 10**2, "हजार": 10**3, "लाख": 10**5, "करोड": 10**7,
             "अर्ब": 10**9, "अरब": 10**9, "खर्ब": 10**11, "खरब": 10**11}
_UNITS_EN = {"hundred": 10**2, "thousand": 10**3, "k": 10**3,
             "lakhs": 10**5, "lakh": 10**5, "lacs": 10**5, "lac": 10**5,
             "crores": 10**7, "crore": 10**7, "cr": 10**7,
             "arba": 10**9, "arab": 10**9, "kharba": 10**11, "kharab": 10**11,
             "millions": 10**6, "million": 10**6, "mn": 10**6, "m": 10**6,
             "billions": 10**9, "billion": 10**9, "bn": 10**9}

# Number words. These only count when a unit follows ("एक करोड", "a
# million"), because on their own they are ordinary words (छ also means "is").
_WORDS = {"एक": "1", "दुई": "2", "तीन": "3", "चार": "4", "पाँच": "5",
          "पांच": "5", "छ": "6", "सात": "7", "आठ": "8", "नौ": "9",
          "दश": "10", "दस": "10", "डेढ": "1.5", "अढाई": "2.5", "अढाइ": "2.5",
          "a": "1", "one": "1", "two": "2", "three": "3", "four": "4",
          "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
          "ten": "10"}
# साढे X = X and a half, सवा X = X and a quarter, पौने X = X less a quarter.
# After NFC, साढ़े (with a nukta) is spelled with a separate nukta mark.
_MODIFIERS = {"साढे": "0.5", "साढ़े": "0.5", "सवा": "0.25", "पौने": "-0.25"}


def _alternation(words) -> str:
    # Longest first, so "millions" wins over "million" over "m".
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


_UNIT = (f"(?:{_alternation(_UNITS_NE)})"
         f"|(?:{_alternation(_UNITS_EN)})\\b")
_WORD_NE = [w for w in _WORDS if not w.isascii()]
_WORD_EN = [w for w in _WORDS if w.isascii()]
_QTY_RE = re.compile(
    rf"(?:(?<![{_DEV}])(?P<mod>{_alternation(_MODIFIERS)})\s*)?"
    r"(?:"
    rf"(?P<digits>\d+(?:,\d+)*(?:\.\d+)?)\s*(?P<unit1>{_UNIT})?"
    r"|"
    rf"(?P<word>(?<![{_DEV}])(?:{_alternation(_WORD_NE)})(?![{_DEV}])"
    rf"|\b(?:{_alternation(_WORD_EN)})\b)\s*(?P<unit2>{_UNIT})"
    r")"
)


def _unit_value(unit: str) -> int:
    for word, value in _UNITS_NE.items():
        if unit.startswith(word):
            return value
    return _UNITS_EN[unit]


def _find_currency(text: str) -> tuple:
    """Return (currency code or None, text with currency words blanked)."""
    found = set()
    for code, rx in _CURRENCY_RES:
        def blank(m, code=code):
            found.add(code)
            return " " * len(m.group(0))
        text = rx.sub(blank, text)
    if "¥" in found:
        raise AmountError("'¥' is used for both yen and yuan")
    if len(found) > 1:
        raise AmountError(f"more than one currency: {sorted(found)}")
    return (found.pop() if found else None), text


def parse_amount(text: str) -> tuple:
    """'रु ५ करोड' -> (Decimal('50000000'), 'NPR'). Raises AmountError."""
    low = norm(text).lower()
    currency, rest = _find_currency(low)
    # "10-10" or "5/5" (the same number twice) means "each": keep one.
    rest = re.sub(r"(\d+(?:[,.]\d+)*)\s*[-/]\s*\1(?![\d,.])", r"\1", rest)

    parts = []   # (number, unit value or None)
    for m in _QTY_RE.finditer(rest):
        if m.group("digits"):
            number = Decimal(m.group("digits").replace(",", ""))
            unit = m.group("unit1")
        else:
            number = Decimal(_WORDS[m.group("word")])
            unit = m.group("unit2")
        if m.group("mod"):
            number += Decimal(_MODIFIERS[m.group("mod")])
        parts.append((number, _unit_value(unit) if unit else None))

    if not parts:
        raise AmountError(f"no number found in {text!r}")
    if len(parts) > 1:
        # Only a compound like "५ करोड ५० लाख" (units getting smaller) is
        # one amount. Anything else is a range, a list, or a year.
        units = [u for _, u in parts]
        # The last part may be a plain number, but only after hundreds or
        # thousands and smaller than them: "५५ हजार ५५५", never "5 crore 2026".
        last_number, last_unit = parts[-1]
        if last_unit is None and units[-2] is not None and units[-2] <= 1000 \
                and last_number < units[-2]:
            units = units[:-1]
        if None in units or any(a <= b for a, b in zip(units, units[1:])):
            raise AmountError(f"more than one number (a range or a list?): {text!r}")

    value = sum((number * (unit or 1) for number, unit in parts), Decimal(0))
    if value <= 0:
        raise AmountError(f"not a positive amount: {text!r}")
    if value == value.to_integral_value():
        value = value.quantize(Decimal(1))

    if currency is None:
        if _is_south_asian(rest):
            currency = "NPR?"
        else:
            raise AmountError(f"no currency in {text!r}")
    return value, currency


def _is_south_asian(text: str) -> bool:
    """Lakh/crore/arba (or a Nepali unit word) mark South Asian money."""
    return bool(re.search(
        rf"{_alternation(_UNITS_NE)}|\b(?:lakhs?|lacs?|crores?|cr|arba|arab|kharba|kharab)\b",
        text))


def needs_confirmation(currency: str) -> bool:
    """True for a currency the parser could only guess ('USD?', 'NPR?')."""
    return currency.endswith("?")
