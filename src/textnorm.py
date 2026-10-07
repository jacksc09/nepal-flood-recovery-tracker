"""Normalise text before comparing it, so equal text compares equal.

News sites type the same words in different ways: invisible joiner
characters inside Nepali words, curly vs straight quotes, non-breaking
spaces, and Devanagari digits (५) vs ASCII digits (5). The checks that
guard the ledger ask "does this quote appear word for word in the stored
article?", so both sides go through norm() first.

norm() is for MATCHING only. Anything shown to a reader keeps its
original characters.
"""

import re
import unicodedata

# Characters that change nothing visible but break string comparison.
_INVISIBLE = {
    "​": None,  # zero-width space
    "‌": None,  # zero-width non-joiner (ZWNJ)
    "‍": None,  # zero-width joiner (ZWJ)
    "­": None,  # soft hyphen
    "﻿": None,  # byte-order mark / zero-width no-break space
}

_PUNCT = {
    "‘": "'", "’": "'", "‚": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
}

# Devanagari digits ० to ९ are U+0966 to U+096F.
_DIGITS = {0x0966 + i: str(i) for i in range(10)}

_TABLE = str.maketrans({**_INVISIBLE, **_PUNCT, **_DIGITS})
_SPACES = re.compile(r"\s+")


def norm(text: str) -> str:
    """NFC, strip invisible joiners, unify quotes/dashes/digits/whitespace."""
    text = unicodedata.normalize("NFC", text or "")
    text = text.translate(_TABLE)
    # \s matches the non-breaking space and other Unicode spaces too.
    return _SPACES.sub(" ", text).strip()


def contains(text: str, fragment: str) -> bool:
    """True if fragment appears in text once both are normalised.

    An empty fragment never counts as found: "" is technically inside
    every string, and an empty quote is no evidence of anything.
    """
    needle = norm(fragment)
    return bool(needle) and needle in norm(text)
