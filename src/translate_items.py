"""English translations of the Nepali review items, as a reading aid for review.

The reviewer may not read Nepali. This script writes translation packets
(the headline, the quoted excerpt, and the donor/place names of every
Nepali item in the review queue), a model session translates them
in-session, and --check validates the result and writes
data/review/translations.jsonl, which review_ui.py shows next to the
original text, labelled as a machine translation.

A translation is never a decision and never reaches the public data. The
check is mechanical: one line per item, and every number in the original
quote (after Devanagari digits become ASCII) must appear in the English,
so a translation cannot silently drop or change an amount.

Run from the repo root:
  .venv/bin/python src/translate_items.py --make     # write packets for untranslated items
  (translate each packet in-session)
  .venv/bin/python src/translate_items.py --check    # validate -> data/review/translations.jsonl
"""

import argparse
import json
import re

from config import RAW, REVIEW_DIR
from extract_prompt import draft_model
from textnorm import norm

QUEUE = REVIEW_DIR / "queue.jsonl"
OUT = REVIEW_DIR / "translations.jsonl"
PACKET_DIR = RAW / "packets" / "translate"
PER_PACKET = 60
FIELDS = ["item_id", "headline_en", "excerpt_en", "donor_en", "place_en"]

HEADER = """\
# Translation packet: {name}

Translate each of the {n} items below from Nepali into plain English, for a
reviewer who does not read Nepali. Use only this file: do not read other
files or search the web.

Rules:
- Translate faithfully and literally enough that the meaning is exact. Do
  not summarise, explain, judge or add anything.
- Keep every number: write Devanagari digits as ordinary digits (५० -> 50),
  and keep Nepali units as words: लाख -> "lakh", करोड -> "crore",
  अर्ब -> "arba (billion)", हजार -> "thousand". Never convert or add up amounts.
- "headline_en": the headline. "excerpt_en": the quote. "donor_en" and
  "place_en": the names given (place_en is null when no place is given).
  Keep proper names in their usual English spelling.

When you have translated all {n} items, use the Write tool to create this
exact file:

{out_path}

with exactly {n} lines, one JSON object per line, in the order listed:
{{"item_id": "...", "headline_en": "...", "excerpt_en": "...", "donor_en": "...", "place_en": "..." or null}}
No markdown, no code fences, nothing else. The list ends with "END OF ITEM
LIST"; if you do not see it, read the rest of the file first.

## Items

"""


def load_queue() -> list:
    return [json.loads(l) for l in QUEUE.read_text(encoding="utf-8").splitlines() if l]


def load_translations() -> dict:
    if not OUT.exists():
        return {}
    return {r["item_id"]: r for r in map(json.loads, OUT.read_text(encoding="utf-8").splitlines())}


def item_text(it: dict) -> dict:
    d = it["draft"]
    if it["kind"] == "org":
        return {"donor": d["org_as_written"], "place": d["place_as_written"], "recipient": None}
    return {"donor": d["donor_as_written"], "place": d["place_as_written"],
            "recipient": d["recipient_as_written"]}


def numbers_in(text: str) -> list:
    """Every number in the text, digits only ('1,00,000' -> '100000')."""
    return [n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", norm(text or ""))]


def make() -> None:
    done = load_translations()
    todo = [it for it in load_queue()
            if it["article"]["lang"] == "ne" and it["item_id"] not in done]
    if not todo:
        print("every Nepali item already has a checked translation")
        return
    PACKET_DIR.mkdir(parents=True, exist_ok=True)
    existing = len(list(PACKET_DIR.glob("tr_*.md")))
    index_path = PACKET_DIR / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {"packets": {}}
    for start in range(0, len(todo), PER_PACKET):
        batch = todo[start:start + PER_PACKET]
        name = f"tr_{existing + start // PER_PACKET + 1:02d}"
        out_path = PACKET_DIR / f"{name}.out.jsonl"
        body = [HEADER.format(name=name, n=len(batch), out_path=out_path)]
        for it in batch:
            t = item_text(it)
            body.append(f"### item_id: {it['item_id']}\nHeadline: {it['article']['title']}\n"
                        f"Donor/organisation (as written): {t['donor']}\n"
                        f"Place (as written): {t['place'] or '—'}\n"
                        + (f"Recipient (as written): {t['recipient']}\n" if t["recipient"] else "")
                        + f"Quote: {it['draft']['evidence_excerpt']}\n")
        body.append(f"\nEND OF ITEM LIST - {len(batch)} items. Now write {out_path} as instructed.\n")
        (PACKET_DIR / f"{name}.md").write_text("\n".join(body), encoding="utf-8")
        index["packets"][name] = {"file": f"{name}.md", "out": out_path.name,
                                  "model": draft_model(), "ids": [i["item_id"] for i in batch]}
        print(f"wrote {name}.md ({len(batch)} items)")
    index_path.write_text(json.dumps(index, indent=2))
    print("Next: translate each packet in-session, then run --check")


def check() -> None:
    index = json.loads((PACKET_DIR / "index.json").read_text())
    queue = {it["item_id"]: it for it in load_queue()}
    good, problems = load_translations(), []
    for name, p in sorted(index["packets"].items()):
        out = PACKET_DIR / p["out"]
        if not out.exists():
            problems.append(f"{name}: not translated yet")
            continue
        seen = set()
        for n, line in enumerate(out.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                assert set(r) == set(FIELDS), f"fields {sorted(r)}"
                assert r["item_id"] in p["ids"], "item not in this packet"
                assert r["item_id"] not in seen, "item repeated"
                missing = [x for x in numbers_in(queue[r["item_id"]]["draft"]["evidence_excerpt"])
                           if x not in numbers_in(r["excerpt_en"])]
                assert not missing, f"numbers missing from the English: {missing}"
            except Exception as exc:   # any defect: report it, keep checking
                problems.append(f"{name} line {n}: {exc}")
                continue
            seen.add(r["item_id"])
            good[r["item_id"]] = {**r, "model": p["model"]}
        for missing_id in set(p["ids"]) - seen:
            if missing_id not in good:
                problems.append(f"{name}: no valid line for {missing_id}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in good.values()),
                   encoding="utf-8")
    nepali = sum(1 for it in queue.values() if it["article"]["lang"] == "ne")
    print(f"checked translations: {len(good)} of {nepali} Nepali items")
    for p in problems[:15]:
        print(f"  {p}")
    if problems:
        print("fix by re-translating those items (--make writes packets for the missing ones)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--make", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.make:
        make()
    if args.check:
        check()


if __name__ == "__main__":
    main()
