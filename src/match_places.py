"""Decide which articles are about the flood corridor, and which mention money.

Searching for "रसुवा" returns hundreds of articles a week, most of them
not about the flood. This funnel narrows them down mechanically:

  1. places    - which corridor places the headline and text name
                 (gazetteer.py, with its context rules)
  2. relevance - "title": a corridor place in the headline and a flood word
                           anywhere;
                 "body":  no place in the headline, but at least two place
                           mentions in the text and a flood word;
                 "weak":  a place but no flood word, or a single passing mention;
                 "none":  no corridor place at all.
  3. money     - for title/body articles with stored text: does the text
                 contain an amount (a currency or a lakh/crore unit next to
                 a number)? Those are the "money candidates" that go into
                 drafting packets.

Only title/body articles appear in the map's news lists. Only money
candidates published on or after the flood reach drafting.

Output: data/processed/article_matches.csv (one row per article).

Run from the repo root:  .venv/bin/python src/match_places.py [--sample 2026-W35]
"""

import argparse
import csv
import random
import re

import articles
from config import EVENT_DATE, FLOOD_TERMS, PROCESSED
from gazetteer import Gazetteer
from textnorm import norm

MATCHES_CSV = PROCESSED / "article_matches.csv"
FIELDS = ["article_id", "published_week", "lang", "place_ids", "pcodes", "unresolved",
          "title_places", "body_mentions", "flood_terms", "relevance",
          "money_hits", "money_candidate"]

# An amount, on norm()-ed text (so Devanagari digits are already ASCII):
#   1. a currency sign or code right before a number   (Rs 5, रु. 50, US$ 2)
#   2. a number, then up to three words, then a currency word
#      (5 crore rupees, ३६ लाख अमेरिकी डलर, 92 लाख 68 हजार 505 रुपैयाँ)
# A unit alone is not enough: "2 करोड घनमिटर" is cubic metres of water.
_CURRENCY_WORD = (r"(?:rupees?|रुपैयाँ|रुपैंया|रुपियाँ|dollars?|डलर|euros?|युरो|यूरो"
                  r"|pounds?|पाउन्ड|yuan|युआन|yen|येन)")
MONEY_RE = re.compile(
    r"(?:\brs\.?|\bnrs\.?|\bnpr\b|\bus\$|\$|\busd\b|€|£|\binr\b"
    r"|(?<![\u0900-\u097F])(?:रु|रू)\.?)\s?\d"
    rf"|\d[\d,.]*(?:\s+\S+){{0,3}}?\s+{_CURRENCY_WORD}",
    re.IGNORECASE)


def flood_terms_in(text: str, lang: str) -> list:
    low = norm(text).lower()
    found = []
    for term in FLOOD_TERMS["en"] + FLOOD_TERMS["ne"]:
        t = term.lower()
        if t.isascii():
            if re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", low):
                found.append(term)
        elif t in low:
            found.append(term)
    return found


def classify(gaz: Gazetteer, row: dict) -> dict:
    text = articles.read_text(row["article_id"]) or ""
    title_matches = gaz.match(row["title"])
    body_matches = gaz.match(text)
    every = title_matches + body_matches
    floods = flood_terms_in(f"{row['title']}\n{text}", row["lang"])
    resolved = sorted({m.place_id for m in every if m.place_id})
    if not every:
        relevance = "none"
    elif title_matches and floods:
        relevance = "title"
    elif len(body_matches) >= 2 and floods:
        relevance = "body"
    else:
        relevance = "weak"
    money_hits = len(MONEY_RE.findall(norm(text))) if text else 0
    candidate = (relevance in ("title", "body") and money_hits > 0
                 and row["text_available"] == "yes"
                 and row["published_at_utc"][:10] >= EVENT_DATE.isoformat())
    return {
        "article_id": row["article_id"], "published_week": row["published_week"],
        "lang": row["lang"], "place_ids": "|".join(resolved),
        "pcodes": "|".join(sorted({m.pcode for m in every if m.pcode})),
        "unresolved": "|".join(sorted({m.alias for m in every if not m.place_id})),
        "title_places": str(len(title_matches)), "body_mentions": str(len(body_matches)),
        "flood_terms": "|".join(dict.fromkeys(floods)), "relevance": relevance,
        "money_hits": str(money_hits), "money_candidate": "yes" if candidate else "no",
    }


def load_matches() -> dict:
    if not MATCHES_CSV.exists():
        return {}
    with open(MATCHES_CSV, newline="", encoding="utf-8") as f:
        return {r["article_id"]: r for r in csv.DictReader(f)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", metavar="WEEK",
                        help="print 50 random title/body headlines from WEEK for a hand check")
    args = parser.parse_args()

    gaz = Gazetteer.load()
    store = articles.load()
    rows = [classify(gaz, r) for r in store.values()]
    PROCESSED.mkdir(parents=True, exist_ok=True)
    with open(MATCHES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["published_week"], r["article_id"])))

    # --- funnel counts per week ------------------------------------------
    print(f"{'week':<9} {'lang':<4} {'all':>5} {'place':>6} {'title':>6} {'body':>5} "
          f"{'weak':>5} {'money':>6}")
    weeks = sorted({(r["published_week"], r["lang"]) for r in rows})
    for wk, lang in weeks:
        sub = [r for r in rows if r["published_week"] == wk and r["lang"] == lang]
        n = lambda cond: sum(1 for r in sub if cond(r))
        print(f"{wk:<9} {lang:<4} {len(sub):>5} {n(lambda r: r['relevance'] != 'none'):>6} "
              f"{n(lambda r: r['relevance'] == 'title'):>6} {n(lambda r: r['relevance'] == 'body'):>5} "
              f"{n(lambda r: r['relevance'] == 'weak'):>5} "
              f"{n(lambda r: r['money_candidate'] == 'yes'):>6}")
    print(f"saved {len(rows)} rows to {MATCHES_CSV.relative_to(PROCESSED.parent.parent)}")

    if args.sample:
        pool = [r for r in rows if r["published_week"] == args.sample
                and r["relevance"] in ("title", "body")]
        random.seed(20261007)
        print(f"\nhand-check sample: 50 of {len(pool)} title/body articles in {args.sample} "
              "(is each one about the flood or its recovery?)")
        for r in random.sample(pool, min(50, len(pool))):
            a = store[r["article_id"]]
            print(f"  [{r['relevance']:<5}] {a['lang']} {a['title'][:95]}  <{r['place_ids']}>")


if __name__ == "__main__":
    main()
