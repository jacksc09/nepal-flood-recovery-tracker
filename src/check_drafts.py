"""The gate between drafted lines and human review.

Drafting sessions write data/raw/packets/<week>/packet_NN.out.jsonl, one
line per article. Every line goes through mechanical checks, and nothing
reaches the review queue until EVERY article in the week's packets has a
valid line:

  1. the line is JSON matching extract_prompt.OUTPUT_SCHEMA (all enums);
  2. its article belongs to this packet, appears once, and has stored text;
  3. each evidence_excerpt (at most 40 words) is found word for word in the
     stored article, after textnorm.norm() on both sides;
  4. amount_text sits inside that excerpt (and unit_count_text too);
  5. amounts.parse_amount(amount_text) succeeds - THE CODE decides the
     number and the currency - and if the model named a currency, it must
     agree with the parser;
  6. place, donor and recipient (as written) all occur in the article;
  7. money marked is_this_flood=false is dropped and logged, not queued.

Run from the repo root:
  .venv/bin/python src/check_drafts.py --week 2026-W35               # report
  .venv/bin/python src/check_drafts.py --week 2026-W35 --make-retry  # re-packet failures
  .venv/bin/python src/check_drafts.py --week 2026-W35 --queue       # -> review queue (100% only)
"""

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field

import jsonschema

import articles
from amounts import AmountError, needs_confirmation, parse_amount
from config import NPT, REVIEW_DIR
from extract_prompt import MAX_EXCERPT_WORDS, OUTPUT_SCHEMA
from textnorm import contains

QUEUE = REVIEW_DIR / "queue.jsonl"
_VALIDATOR = jsonschema.Draft202012Validator(OUTPUT_SCHEMA)


@dataclass
class LineResult:
    article_id: str = None
    entries: list = field(default_factory=list)   # checked, with amount/currency added
    orgs: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    dropped: list = field(default_factory=list)   # not this flood


@dataclass
class PacketReport:
    valid: set = field(default_factory=set)
    results: dict = field(default_factory=dict)   # article_id -> LineResult
    errors: list = field(default_factory=list)    # (line number, message)
    missing: set = field(default_factory=set)


def _excerpt_problems(label: str, excerpt: str, text: str) -> list:
    problems = []
    if len(excerpt.split()) > MAX_EXCERPT_WORDS:
        problems.append(f"{label}: evidence_excerpt has {len(excerpt.split())} words "
                        f"(max {MAX_EXCERPT_WORDS})")
    if not contains(text, excerpt):
        problems.append(f"{label}: evidence_excerpt not found in the article")
    return problems


def check_line(line: str, expected_ids: set, get_text) -> LineResult:
    """Check one drafted output line against its stored article."""
    result = LineResult()
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        result.errors.append(f"not JSON ({exc.msg})")
        return result
    schema_errors = sorted(_VALIDATOR.iter_errors(record), key=lambda e: list(e.path))
    if schema_errors:
        e = schema_errors[0]
        where = "/".join(str(p) for p in e.path) or "line"
        result.errors.append(f"schema: {where}: {e.message[:120]}")
        return result
    aid = result.article_id = record["article_id"]
    if aid not in expected_ids:
        result.errors.append(f"article {aid} is not in this packet")
        return result
    text = get_text(aid)
    if not text:
        result.errors.append(f"article {aid} has no stored text")
        return result

    for i, e in enumerate(record["entries"], 1):
        label = f"entry {i}"
        problems = _excerpt_problems(label, e["evidence_excerpt"], text)
        if not contains(e["evidence_excerpt"], e["amount_text"]):
            problems.append(f"{label}: amount_text {e['amount_text']!r} is not inside the "
                            "evidence_excerpt")
        if e["unit_count_text"] and not contains(e["evidence_excerpt"], e["unit_count_text"]):
            problems.append(f"{label}: unit_count_text is not inside the evidence_excerpt")
        try:
            amount, currency = parse_amount(e["amount_text"])
        except AmountError as exc:
            problems.append(f"{label}: amount_text could not be parsed ({exc})")
            amount = currency = None
        if currency and e["currency_hint"] != "unknown" \
                and e["currency_hint"] != currency.rstrip("?"):
            problems.append(f"{label}: currency_hint {e['currency_hint']} but the text "
                            f"says {currency}")
        if currency == "USD?" and e["currency_hint"] != "unknown":
            problems.append(f"{label}: a bare '$' must have currency_hint 'unknown'")
        for key in ("place_as_written", "donor_as_written", "recipient_as_written"):
            if e[key] and not contains(text, e[key]):
                problems.append(f"{label}: {key} {e[key]!r} not found in the article")
        if problems:
            result.errors.extend(problems)
            continue
        clean = dict(e, amount=amount, currency=currency,
                     currency_needs_confirmation=needs_confirmation(currency))
        (result.entries if e["is_this_flood"] else result.dropped).append(clean)

    for i, o in enumerate(record["orgs"], 1):
        label = f"org {i}"
        problems = _excerpt_problems(label, o["evidence_excerpt"], text)
        for key in ("org_as_written", "place_as_written"):
            if not contains(text, o[key]):
                problems.append(f"{label}: {key} {o[key]!r} not found in the article")
        if problems:
            result.errors.extend(problems)
        else:
            result.orgs.append(dict(o))

    if result.errors:
        result.entries, result.orgs, result.dropped = [], [], []
    return result


def check_packet_lines(lines: list, expected_ids: set, get_text) -> PacketReport:
    """Check every line of one packet's output; every article needs one valid line."""
    report = PacketReport()
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        result = check_line(line, expected_ids, get_text)
        if result.errors:
            report.errors.extend((n, e) for e in result.errors)
            continue
        if result.article_id in report.valid:
            report.errors.append((n, f"article {result.article_id} appears more than once "
                                     "(kept the first line)"))
            continue
        report.valid.add(result.article_id)
        report.results[result.article_id] = result
    report.missing = set(expected_ids) - report.valid
    return report


# --- the command-line tool ---------------------------------------------------------
def week_report(week: str):
    """(index, {packet: PacketReport}) for one week's packets."""
    from draft_packet import load_index, packet_dir
    index = load_index(week)
    reports = {}
    for name, entry in sorted(index["packets"].items()):
        out = packet_dir(week) / entry["out"]
        lines = out.read_text(encoding="utf-8").splitlines() if out.exists() else []
        reports[name] = check_packet_lines(lines, set(entry["ids"]), articles.read_text)
    return index, reports


def article_status(index: dict, reports: dict) -> tuple:
    """Latest valid result per article (retries override earlier packets)."""
    best, all_ids = {}, set()
    for name in sorted(index["packets"]):       # packet_* before retry_*
        all_ids |= set(index["packets"][name]["ids"])
        for aid, res in reports[name].results.items():
            best[aid] = (name, res)
    return best, all_ids - set(best)


def write_queue(week: str, index: dict, best: dict) -> int:
    """Append this week's checked items to the review queue (once each)."""
    from gazetteer import Gazetteer
    gaz = Gazetteer.load()
    store = articles.load()
    existing = set()
    if QUEUE.exists():
        existing = {json.loads(l)["item_id"] for l in QUEUE.read_text().splitlines() if l}
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    added, dropped_log = 0, []
    with open(QUEUE, "a", encoding="utf-8") as q:
        for aid, (packet, res) in sorted(best.items()):
            meta = store[aid]
            text = articles.read_text(aid)
            article = {k: meta[k] for k in ("article_id", "title", "url", "outlet", "lang",
                                            "published_at_utc", "date_precision")}
            base = {"week": week, "packet": packet, "article": article,
                    "drafted_by": index["packets"][packet]["model"]}
            for n, e in enumerate(res.entries, 1):
                item_id = f"{week}:{aid}:e{n}"
                if item_id in existing:
                    continue
                cands = gaz.resolve(e["place_as_written"], text) if e["place_as_written"] else []
                q.write(json.dumps({"item_id": item_id, "kind": "ledger", **base,
                                    "draft": {k: v for k, v in e.items()
                                              if k not in ("amount", "currency",
                                                           "currency_needs_confirmation")},
                                    "computed": {"amount": str(e["amount"]),
                                                 "currency": e["currency"],
                                                 "currency_needs_confirmation":
                                                     e["currency_needs_confirmation"],
                                                 "place_candidates": cands}},
                                   ensure_ascii=False) + "\n")
                added += 1
            for n, o in enumerate(res.orgs, 1):
                item_id = f"{week}:{aid}:o{n}"
                if item_id in existing:
                    continue
                q.write(json.dumps({"item_id": item_id, "kind": "org", **base, "draft": o,
                                    "computed": {"place_candidates":
                                                 gaz.resolve(o["place_as_written"], text)}},
                                   ensure_ascii=False) + "\n")
                added += 1
            dropped_log.extend({"article_id": aid, **{k: str(v) for k, v in d.items()}}
                               for d in res.dropped)
    from draft_packet import packet_dir
    # Marker read by publish_week.py: this week's drafts passed and were queued.
    (packet_dir(week) / "queued.json").write_text(json.dumps(
        {"week": week, "items_added_this_run": added}, indent=1) + "\n")
    (packet_dir(week) / "dropped.jsonl").write_text(
        "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in dropped_log), encoding="utf-8")
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--week", required=True)
    parser.add_argument("--make-retry", action="store_true",
                        help="write retry packets for articles without a valid line")
    parser.add_argument("--queue", action="store_true",
                        help="add checked items to the review queue (needs 100%%)")
    args = parser.parse_args()

    index, reports = week_report(args.week)
    best, missing = article_status(index, reports)
    total = len(best) + len(missing)
    print(f"{args.week}: {len(index['packets'])} packets, {total} articles; "
          f"valid {len(best)} ({len(best) / max(total, 1):.0%}), still needed {len(missing)}")
    errors = [(name, n, e) for name, r in reports.items() for n, e in r.errors]
    if errors:
        kinds = Counter(e.split(": ", 1)[-1].split(" (")[0][:45] for _, _, e in errors)
        excerpt_fail = sum(1 for _, _, e in errors if "not found in the article" in e)
        print(f"invalid lines: {len(errors)}; literal-excerpt failures: {excerpt_fail} "
              f"({excerpt_fail / len(errors):.0%} of errors) - a high share points to a "
              "prompt problem, not a reason to relax the check")
        for name, n, e in errors[:12]:
            print(f"  {name} line {n}: {e}")
        print("  most common:", kinds.most_common(4))
    entries = sum(len(r.entries) for _, r in best.values())
    orgs = sum(len(r.orgs) for _, r in best.values())
    dropped = sum(len(r.dropped) for _, r in best.values())
    print(f"checked so far: {entries} money entries, {orgs} organisation sightings, "
          f"{dropped} dropped as not this flood")

    if args.make_retry:
        from draft_packet import make_retry
        if missing:
            names = make_retry(args.week, sorted(missing))
            print(f"wrote retry packet(s) {', '.join(names)} for {len(missing)} articles")
        else:
            print("nothing to retry")
    if args.queue:
        if missing:
            raise SystemExit(f"refusing to queue: {len(missing)} articles still lack a valid "
                             "line (run --make-retry and draft those first)")
        added = write_queue(args.week, index, best)
        print(f"queued {added} new items in {QUEUE.relative_to(REVIEW_DIR.parent.parent)}")
        print("GATE 2: review them at http://localhost:8766 (.venv/bin/python src/review_ui.py)")


if __name__ == "__main__":
    main()
