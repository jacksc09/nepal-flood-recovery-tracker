"""Find corridor place names in English and Nepali text.

The gazetteer (data/gazetteer/places.csv, built by build_gazetteer.py) lists
every district, municipality, town and river of the flood corridor with
its spellings in both languages. This module answers two questions:

  match(text)                  which places does this article mention?
  resolve(name, article_text)  which place is "Kalika" in THIS article?

The pcode of a ledger entry always comes from here, never from the
language model.

Matching rules:
  * English: case-insensitive, whole words ("Rasuwa's" matches Rasuwa,
    "Gorkhapatra" - a newspaper - does not match Gorkha).
  * Nepali: the name must start a word, but anything may follow it,
    because case endings attach to the word (रसुवाको, रसुवामा).
  * Where two spellings overlap, the longest wins ("Trishuli Bazar" the
    town, not "Trishuli" the river).
  * Some names are only trusted with context (needs_context): the Bhote
    Koshi (there is a second one in Sindhupalchok), Kalika (several
    municipalities share it)... These count only if the article also
    names a place in the same district.
  * A name shared by places in different districts is resolved by which
    district the article names; if it can't be, it stays unresolved.
  * Protected-area pseudo-units (pcode ending 5xx) are never matched.
"""

import csv
import re
from dataclasses import dataclass, field

from config import CORE_DISTRICTS, CORRIDOR_PCODE, GAZETTEER
from textnorm import norm

# Nepali words that begin with a place name but are not that place.
FALSE_FRIENDS_NE = ["गोरखापत्र"]   # Gorkhapatra, the state newspaper

# Devanagari letters and vowel signs (not the danda or digits).
_NE_LETTER = r"ऀ-ॣॱ-ॿ"

# How specific each kind of place is, for resolve().
_SPECIFICITY = {"town": 4, "landmark": 3, "municipality": 2, "district": 1,
                "province": 0, "river": -1}


@dataclass
class Place:
    place_id: str
    kind: str              # province | district | municipality | town | river | landmark
    pcode: str             # the admin unit it belongs to (or CORRIDOR)
    parent_pcode: str
    name_en: str
    name_ne: str
    aliases_en: list
    aliases_ne: list
    lat: float
    lon: float
    corridor_role: str     # core | downstream | province
    needs_context: bool


@dataclass
class Match:
    alias: str             # the gazetteer spelling that matched
    start: int             # offsets in norm(text)
    end: int
    candidates: list       # Places this spelling could mean
    place_id: str = None   # set when exactly one candidate survives
    pcode: str = None
    notes: list = field(default_factory=list)


def is_protected_area(pcode: str) -> bool:
    """NP0328597 (Langtang National Park) and similar pseudo-units."""
    return len(pcode) == 9 and pcode.startswith("NP") and pcode[6] == "5"


def _district(pcode: str):
    return pcode[:6] if pcode.startswith("NP") and len(pcode) >= 6 else None


def _alternation(keys) -> str:
    return "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))


class Gazetteer:
    def __init__(self, places: list):
        self.places = {p.place_id: p for p in places
                       if not (p.kind == "municipality" and is_protected_area(p.pcode))}
        self._en, self._ne = {}, {}
        for p in self.places.values():
            for alias in {p.name_en, *p.aliases_en} - {""}:
                self._en.setdefault(norm(alias).lower(), []).append(p)
            for alias in {p.name_ne, *p.aliases_ne} - {""}:
                self._ne.setdefault(norm(alias), []).append(p)
        self._en_re = re.compile(
            rf"(?<![A-Za-z0-9])({_alternation(self._en)})(?![A-Za-z0-9])", re.IGNORECASE)
        self._ne_re = re.compile(rf"(?<![{_NE_LETTER}])({_alternation(self._ne)})")

    @classmethod
    def load(cls, path=GAZETTEER) -> "Gazetteer":
        places = []
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                places.append(Place(
                    place_id=row["place_id"], kind=row["kind"], pcode=row["pcode"],
                    parent_pcode=row["parent_pcode"], name_en=row["name_en"],
                    name_ne=row["name_ne"],
                    aliases_en=[a for a in row["aliases_en"].split("|") if a],
                    aliases_ne=[a for a in row["aliases_ne"].split("|") if a],
                    lat=float(row["lat"]) if row["lat"] else None,
                    lon=float(row["lon"]) if row["lon"] else None,
                    corridor_role=row["corridor_role"],
                    needs_context=row["needs_context"] == "yes"))
        return cls(places)

    # --- step 1: every spelling found, before any context rules ----------
    def _raw_matches(self, text: str) -> list:
        found = []
        for m in self._en_re.finditer(text):
            found.append(Match(m.group(1), m.start(), m.end(), self._en[m.group(1).lower()]))
        for m in self._ne_re.finditer(text):
            word = re.match(rf"[{_NE_LETTER}]+", text[m.start():]).group(0)
            if any(word.startswith(ff) for ff in FALSE_FRIENDS_NE):
                continue
            found.append(Match(m.group(1), m.start(), m.end(), self._ne[m.group(1)]))
        return sorted(found, key=lambda m: m.start)

    @staticmethod
    def _anchors(raw: list) -> set:
        """Districts named unambiguously (by a place that needs no context)."""
        anchors = set()
        for m in raw:
            if len(m.candidates) == 1 and not m.candidates[0].needs_context:
                d = _district(m.candidates[0].pcode)
                if d:
                    anchors.add(d)
        return anchors

    @staticmethod
    def _fits_context(p: Place, anchors: set) -> bool:
        if p.pcode == CORRIDOR_PCODE:
            return bool(anchors & set(CORE_DISTRICTS))
        return _district(p.pcode) in anchors

    def _settle(self, m: Match, anchors: set):
        """Apply the context rules to one match; None means drop it."""
        if len(m.candidates) == 1 and not m.candidates[0].needs_context:
            m.place_id, m.pcode = m.candidates[0].place_id, m.candidates[0].pcode
            return m
        fitting = [p for p in m.candidates if self._fits_context(p, anchors)]
        if len(fitting) == 1:
            m.place_id, m.pcode = fitting[0].place_id, fitting[0].pcode
            return m
        if not fitting and all(p.needs_context for p in m.candidates):
            return None              # e.g. "Bhote Koshi" with no corridor district
        m.notes.append("ambiguous: " + ", ".join(p.place_id for p in m.candidates))
        return m                     # kept, but unresolved

    # --- public ------------------------------------------------------------
    def match(self, text: str) -> list:
        """Places mentioned in text, after the context rules."""
        raw = self._raw_matches(norm(text))
        anchors = self._anchors(raw)
        return [s for m in raw if (s := self._settle(m, anchors))]

    def resolve(self, name: str, article_text: str) -> list:
        """Candidate place_ids for a place name as written in an article.

        One id means resolved. Several mean a person must choose (review
        shows a dropdown). An empty list means the name is not a corridor
        place at all.
        """
        anchors = self._anchors(self._raw_matches(norm(article_text)))
        options = []
        for m in self._raw_matches(norm(name)):
            fitting = [p for p in m.candidates if self._fits_context(p, anchors)]
            if len(m.candidates) == 1 and not m.candidates[0].needs_context:
                fitting = m.candidates
            options.extend(fitting or m.candidates)
        if not options:
            return []
        best = max(_SPECIFICITY.get(p.kind, 0) for p in options)
        return sorted({p.place_id for p in options if _SPECIFICITY.get(p.kind, 0) == best})

