"""A second look at every unpublished ledger entry: its place, and repeats of the same gift.

After the first review the reviewer asked for a careful re-check of where
each entry's money is going (municipality, district, province, several
districts or national), and the same gift often appears as several entries
because several articles reported it. This script writes "check packets"
for a model session to read, and --check validates the answers and writes
data/review/place_suggestions.jsonl.

The suggestions are only suggestions: review_ui.py shows each one beside
the entry's current place, and the reviewer accepts or rejects it. Nothing
here edits the ledger.

Each answer says, for one entry:
  geo_level  municipality | district | province | corridor | national
  place_ids  the place(s) from the list in the packet (empty for corridor
             and national); several only when the amount goes to EACH
  each       true when the same amount goes to each of several places
  same_as    an earlier entry id in the packet if this is the same gift
             (same donor, same amount, same recipient) reported again
  reason     a short reason

Run from the repo root:
  .venv/bin/python src/check_places.py --make
  (answer each packet in-session)
  .venv/bin/python src/check_places.py --check
"""

import argparse
import csv
import json
import re

import ledger
from config import RAW, REVIEW_DIR
from extract_prompt import GEO_LEVELS, draft_model
from gazetteer import Gazetteer

PACKET_DIR = RAW / "packets" / "placecheck"
OUT = REVIEW_DIR / "place_suggestions.jsonl"
TRANSLATIONS = REVIEW_DIR / "translations.jsonl"
REVIEW_LOG = ledger.LEDGER_DIR / "review_log.csv"
MAX_ENTRIES = 55
FIELDS = {"entry_id", "geo_level", "place_ids", "each", "same_as", "reason"}
KIND_FOR_LEVEL = {"municipality": {"municipality", "town"}, "district": {"district"},
                  "province": {"province"}}

HEADER = """\
# Place check packet: {name}

Below are {n} entries from a public record of money for the recovery from
the 26 August 2026 Rasuwa / Bhote Koshi / Trishuli flood in Nepal. Each was
copied from a news article. Use only this file: do not read other files or
search the web.

For EACH entry decide, from the quote (and its English translation):

1. WHERE THE MONEY IS GOING TO - not where the donor is from.
   - A gift to the Prime Minister's relief fund (प्रधानमन्त्री दैवी प्रकोप
     उद्धार कोष), a national fund, or "flood victims" with no place named:
     geo_level "national", place_ids [].
   - Money for one named district (e.g. a district disaster management
     fund, "victims in Rasuwa"): "district" with that district's id.
   - Money for one named municipality, or a town inside it: "municipality"
     with the municipality's id (or the town's id from the list).
   - Money for the province's own fund or programme: "province" with the
     province id.
   - Money for several corridor districts together, or "flood-affected
     areas", without saying how much each gets: "corridor", place_ids [].
   - A donor's own location (e.g. "Syangja municipalities donate", "a bank
     in Biratnagar") is NOT the place. Places outside the list below are
     not corridor places: use "national" unless the money is for a listed place.
2. "each": if the quote gives one amount for EACH of several places
   ("1 crore each to Rasuwa and Nuwakot", "१ करोडका दरले"), set each true,
   geo_level to their level, and list every place in place_ids.
   Otherwise each is false and place_ids has at most one id.
3. "same_as": if this entry is the SAME gift as an EARLIER entry in this
   packet (same donor, same amount, same recipient, reported again by
   another article or in the other language), give that earlier
   entry_id. Otherwise null. Different amounts from the same donor are
   different gifts.
4. "reason": under 15 words, in English.

Only use place ids from this list:

{places}

When you have decided all {n} entries, use the Write tool to create:

{out_path}

with exactly {n} lines, in the order listed, each a JSON object:
{{"entry_id": "...", "geo_level": "...", "place_ids": [...], "each": false, "same_as": null, "reason": "..."}}
No markdown, nothing else. The list ends with "END OF ENTRY LIST"; if you
do not see it, read the rest of the file first.

## Entries

"""


def places_block(gaz: Gazetteer) -> str:
    rows = []
    order = {"province": 0, "district": 1, "municipality": 2, "town": 3}
    for p in sorted(gaz.places.values(), key=lambda p: (p.pcode[:6], order.get(p.kind, 9), p.name_en)):
        if p.kind not in order or (p.kind == "town" and not p.pcode):
            continue
        district = gaz.places.get(p.pcode[:6])
        where = f", in {district.name_en}" if district and p.kind not in ("district", "province") else ""
        rows.append(f"{p.place_id} | {p.kind}{where} | {p.name_en} | {p.name_ne}")
    return "\n".join(rows)


def entry_translations() -> dict:
    """entry_id -> English translation (via the review log's item ids)."""
    if not TRANSLATIONS.exists():
        return {}
    by_item = {r["item_id"]: r for r in map(json.loads, TRANSLATIONS.read_text(encoding="utf-8").splitlines())}
    with open(REVIEW_LOG, newline="", encoding="utf-8") as f:
        return {r["entry_id"]: by_item[r["item_id"]] for r in csv.DictReader(f)
                if r["kind"] == "ledger" and r["item_id"] in by_item and r["entry_id"]}


def donor_key(e: dict) -> str:
    words = re.findall(r"[a-z]{3,}", e["donor_name"].lower())
    return " ".join(sorted(w for w in words if w not in {"the", "and", "for", "of"})) or e["donor_name"].lower()


def make() -> None:
    gaz = Gazetteer.load()
    entries = [e for e in ledger.load_ledger() if not e.get("first_published_week")]
    # Keep one donor's entries together (and in date order) so repeats of the
    # same gift land in the same packet, earliest first.
    entries.sort(key=lambda e: (donor_key(e), e["announced_on"], e["entry_id"]))
    groups = {}
    for e in entries:
        groups.setdefault(donor_key(e), []).append(e)
    trans = entry_translations()
    PACKET_DIR.mkdir(parents=True, exist_ok=True)
    places = places_block(gaz)
    packets, current = {}, []
    def flush():
        name = f"pc_{len(packets) + 1:02d}"
        out_path = PACKET_DIR / f"{name}.out.jsonl"
        body = [HEADER.format(name=name, n=len(current), out_path=out_path, places=places)]
        for e in current:
            s, t = e["sources"][0], trans.get(e["entry_id"])
            current_place = gaz.places[e["place_id"]].name_en if e["place_id"] in gaz.places else e["place_id"]
            body.append(
                f"### entry_id: {e['entry_id']}\n"
                f"Article: {s['title']} ({s['outlet']}, {s['published_at'][:10]})\n"
                + (f"Headline in English: {t['headline_en']}\n" if t else "")
                + f"Amount: {e['amount_text']} | Donor: {e['donor_name']} ({e['donor_as_written']})\n"
                f"Recipient as written: {e['recipient_as_written'] or '—'} | "
                f"Place as written: {e['place_as_written'] or '—'}\n"
                f"Quote: {s['evidence_excerpt']}\n"
                + (f"Quote in English: {t['excerpt_en']}\n" if t else "")
                + f"Currently recorded as: {e['geo_level']} / {current_place}\n")
        body.append(f"\nEND OF ENTRY LIST - {len(current)} entries. Now write {out_path} as instructed.\n")
        (PACKET_DIR / f"{name}.md").write_text("\n".join(body), encoding="utf-8")
        packets[name] = {"file": f"{name}.md", "out": out_path.name, "model": draft_model(),
                         "ids": [e["entry_id"] for e in current]}
    for group in groups.values():
        if current and len(current) + len(group) > MAX_ENTRIES:
            flush()
            current = []
        current.extend(group)
    if current:
        flush()
    (PACKET_DIR / "index.json").write_text(json.dumps({"packets": packets}, indent=2))
    print(f"wrote {len(packets)} packets for {len(entries)} unpublished entries")


def same_money(a: dict, b: dict) -> bool:
    """Same donor, amount within 5%, announced within a week."""
    from datetime import date
    if a["donor_name"].strip().lower() != b["donor_name"].strip().lower():
        return False
    x, y = float(a["amount_npr"]), float(b["amount_npr"])
    days = abs((date.fromisoformat(a["announced_on"]) - date.fromisoformat(b["announced_on"])).days)
    return abs(x - y) <= 0.05 * max(x, y) and days <= 7


def reconcile(suggestions: dict, entries: dict, order: list) -> dict:
    """Make the model's suggestions safe to accept.

    1. An entry already at one of its "each" places was already split by
       place when it was drafted: it keeps its place and is not split again.
    2. A repeat must be the same gift in the same place. If the suggested
       earlier entry is somewhere else, the earliest earlier entry with the
       same donor, amount and place is used instead (or none).
    """
    fixed = {k: dict(v) for k, v in suggestions.items()}
    for eid in order:
        s, e = fixed.get(eid), entries.get(eid)
        if s and e and s["each"] and e["place_id"] in s["place_ids"]:
            s.update(each=False, place_ids=[e["place_id"]], geo_level=e["geo_level"],
                     reason=s["reason"] + " [already one entry per place: not split again]")

    def place(eid):
        s = fixed[eid]
        return (s["geo_level"], s["place_ids"][0] if s["place_ids"] and not s["each"] else
                tuple(s["place_ids"]))

    for i, eid in enumerate(order):
        s = fixed.get(eid)
        if not s or not s["same_as"]:
            continue
        target = s["same_as"]
        if target in fixed and place(target) == place(eid) and same_money(entries[target], entries[eid]):
            continue
        earlier = [t for t in order[:i] if t in fixed and place(t) == place(eid)
                   and same_money(entries[t], entries[eid])]
        s["same_as"] = earlier[0] if earlier else None
        s["reason"] += f" [repeat re-matched within the same place: {s['same_as'] or 'none'}]"
    return fixed


def check() -> None:
    gaz = Gazetteer.load()
    index = json.loads((PACKET_DIR / "index.json").read_text())
    good, problems = {}, []
    for name, p in sorted(index["packets"].items()):
        out = PACKET_DIR / p["out"]
        if not out.exists():
            problems.append(f"{name}: not answered yet")
            continue
        order = p["ids"]
        for n, line in enumerate(out.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                assert set(r) == FIELDS, f"fields {sorted(r)}"
                assert r["entry_id"] in order, "entry not in this packet"
                assert r["geo_level"] in GEO_LEVELS, f"bad geo_level {r['geo_level']}"
                ids = r["place_ids"]
                if r["geo_level"] in ("corridor", "national"):
                    assert ids == [], "corridor/national take no place ids"
                else:
                    assert ids, "no place given"
                    for pid in ids:
                        assert pid in gaz.places, f"unknown place {pid}"
                        assert gaz.places[pid].kind in KIND_FOR_LEVEL[r["geo_level"]], \
                            f"{pid} is a {gaz.places[pid].kind}, not a {r['geo_level']}"
                assert r["each"] == (len(ids) > 1), "each must be true exactly when several places are given"
                if r["same_as"]:
                    assert r["same_as"] in order and order.index(r["same_as"]) < order.index(r["entry_id"]), \
                        "same_as must be an earlier entry in this packet"
            except Exception as exc:
                problems.append(f"{name} line {n}: {exc}")
                continue
            good[r["entry_id"]] = {**r, "model": p["model"]}
        for missing in set(order) - set(good):
            problems.append(f"{name}: no valid line for {missing}")
    entries = {e["entry_id"]: e for e in ledger.load_ledger()}
    order = [eid for p in index["packets"].values() for eid in p["ids"] if eid in good]
    good = reconcile(good, entries, order)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in good.values()),
                   encoding="utf-8")
    changed = sum(1 for r in good.values()
                  if r["same_as"] or r["each"] or r["geo_level"] != entries[r["entry_id"]]["geo_level"]
                  or (r["place_ids"] or [entries[r["entry_id"]]["place_id"]])[0] != entries[r["entry_id"]]["place_id"]
                  and r["geo_level"] not in ("corridor", "national"))
    print(f"valid suggestions: {len(good)} of {sum(len(p['ids']) for p in index['packets'].values())}; "
          f"{changed} differ from the current entry "
          f"({sum(1 for r in good.values() if r['same_as'])} repeats, "
          f"{sum(1 for r in good.values() if r['each'])} 'each' splits)")
    for p in problems[:15]:
        print(f"  {p}")


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
