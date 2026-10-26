"""Publish one week: write its permanent file in docs/data/weeks/.

A published week file is never rewritten (corrections go in
data/ledger/corrections.jsonl and show as a banner). So this script
refuses to publish unless everything is in order:

  * the week has ended (Sunday 23:59:59 Nepal time has passed);
  * it is the next week in sequence (none skipped) and is not already
    published;
  * every money candidate dated in or before the week has been drafted,
    every drafted week's checks reached 100% and were queued, and every
    queued item up to this week has been decided in review_ui.py;
  * every OCHA flow outside the appeal first reported by the week's end has
    an in-scope / out-of-scope decision.

Then it writes the file (from weekfile.py, the same assembly the preview
uses), marks the ledger entries and donation listings it contains as
published (after which they change only through corrections), records the
file's SHA-256 in data/manifests/published.jsonl, rebuilds the index, and
runs verify_published.py. It never commits: it prints the git commands.

Run from the repo root:  .venv/bin/python src/publish_week.py [--week 2026-W35] [--check]
"""

import argparse
import csv
import json
import sys

import articles
import ledger
import orgs
from config import EVENT_DATE, FIRST_WEEK, MANUAL, NPT, PROCESSED, REVIEW_DIR, WEEKS_DIR
from draft_packet import PACKETS, already_packeted
from fetch_common import utc_now
from match_places import MATCHES_CSV
from verify_published import PUBLISHED_LOG, problems as verify_problems, sha256_hex
from weekfile import build_week, summary, write_places
from weeks import is_closed, next_week, week_end_date, week_start_date


def published_weeks() -> list:
    if not PUBLISHED_LOG.exists():
        return []
    return [json.loads(l)["week"] for l in PUBLISHED_LOG.read_text().splitlines() if l]


def order_problems(week: str, now, published: list) -> list:
    """Publish once, in order, and only after the week has ended."""
    out = []
    if week in published:
        out.append(f"{week} is already published and is never rewritten")
    expected = next_week(published[-1]) if published else FIRST_WEEK
    if week not in published and week != expected:
        out.append(f"the next week to publish is {expected}, not {week}")
    if not is_closed(week, now):
        out.append(f"{week} has not ended yet (it closes Sunday 23:59:59 Nepal time)")
    return out


def data_problems(week: str) -> list:
    """Is everything dated up to this week drafted, checked, queued and decided?"""
    out, end = [], week_end_date(week).isoformat()
    store = articles.load()
    with open(MATCHES_CSV, newline="", encoding="utf-8") as f:
        candidates = {m["article_id"] for m in csv.DictReader(f) if m["money_candidate"] == "yes"
                      and store[m["article_id"]]["published_at_utc"][:10] <= end}
    undrafted = candidates - already_packeted()
    if undrafted:
        out.append(f"{len(undrafted)} money candidates up to {week} are not in any drafting "
                   f"packet: run src/draft_packet.py --week {week}")
    for index in sorted(PACKETS.glob("20*-W*/index.json")):
        pw = index.parent.name
        if week_start_date(pw) <= week_start_date(week) and not (index.parent / "queued.json").exists():
            out.append(f"{pw}'s drafts have not passed the checks and been queued: "
                       f"run src/check_drafts.py --week {pw} --queue")
    queue = REVIEW_DIR / "queue.jsonl"
    with open(ledger.LEDGER_DIR / "review_log.csv", newline="", encoding="utf-8") as f:
        decided = {r["item_id"] for r in csv.DictReader(f)}
    if queue.exists():
        pending = [i for i in map(json.loads, queue.read_text(encoding="utf-8").splitlines())
                   if week_start_date(i["week"]) <= week_start_date(week) and i["item_id"] not in decided]
        if pending:
            kinds = {}
            for i in pending:
                kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
            out.append(f"{len(pending)} review items up to {week} are undecided {kinds}: "
                       "finish them in review_ui.py")
    scope_path = MANUAL / "fts_scope.csv"
    scope = set()
    if scope_path.exists():
        with open(scope_path, newline="", encoding="utf-8") as f:
            scope = {r["flow_id"] for r in csv.DictReader(f)}
    with open(PROCESSED / "fts_flows.csv", newline="", encoding="utf-8") as f:
        undecided = [r["flow_id"] for r in csv.DictReader(f) if r["in_plan"] == "no"
                     and r["first_reported_date"] <= end and r["flow_id"] not in scope]
    if undecided:
        out.append(f"{len(undecided)} FTS flows outside the appeal (reported by {end}) need a "
                   "scope decision in review_ui.py's FTS scope tab")
    return out


def rebuild_index() -> None:
    rows = []
    for row in map(json.loads, PUBLISHED_LOG.read_text().splitlines()):
        data = json.loads((WEEKS_DIR / f"{row['week']}.json").read_bytes())
        rows.append({"week": row["week"], "file": f"{row['week']}.json", "sha256": row["sha256"],
                     "kind": data["meta"]["kind"], "open_week": False, "preview": False,
                     "built_at": data["meta"]["built_at"], "summary": summary(data)})
    (WEEKS_DIR / "index.json").write_text(json.dumps(
        {"preview": False, "built_at": rows[-1]["built_at"], "weeks": rows},
        ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def stamp_published(week: str, data: dict) -> tuple:
    """Mark what this file contains as published: it can now only be corrected."""
    shown = {e["entry_id"] for e in data["ledger"]["entries"]}
    entries = ledger.load_ledger()
    n_entries = 0
    for e in entries:
        if e["entry_id"] in shown and not e.get("first_published_week"):
            e["first_published_week"] = week
            n_entries += 1
    ledger.LEDGER_JSONL.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
                                   encoding="utf-8")
    listings, n_listings = orgs.load_areas(), 0
    shown_orgs = {(i["org_id"], i["place_id"]) for b in data["donate"].values()
                  for i in b["working"]}
    for l in listings:
        if (l["org_id"], l["place_id"]) in shown_orgs and not l.get("first_published_week"):
            l["first_published_week"] = week
            n_listings += 1
    if listings:
        orgs.ORG_AREAS.write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in listings),
                                  encoding="utf-8")
    return n_entries, n_listings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", help="default: the next unpublished week")
    parser.add_argument("--check", action="store_true", help="only report what blocks publishing")
    args = parser.parse_args()

    published = published_weeks()
    week = args.week or (next_week(published[-1]) if published else FIRST_WEEK)
    now = utc_now()
    found = order_problems(week, now, published) + data_problems(week) + verify_problems()
    if found:
        print(f"cannot publish {week} yet:")
        for p in found:
            print(f"  - {p}")
        sys.exit(1)
    if args.check:
        print(f"{week} is ready to publish")
        return

    data = build_week(week, now, preview=False)
    for e in data["ledger"]["entries"]:
        for s in e["sources"]:
            assert len(s["excerpt"].replace(" …", "").split()) <= 25, "excerpt over 25 words"
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    WEEKS_DIR.mkdir(parents=True, exist_ok=True)
    with open(WEEKS_DIR / f"{week}.json", "xb") as f:      # "x": never overwrite
        f.write(body)
    sha = sha256_hex(body)
    with open(PUBLISHED_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"week": week, "sha256": sha, "bytes": len(body),
                            "kind": data["meta"]["kind"],
                            "published_at": data["meta"]["built_at"]}) + "\n")
    n_entries, n_listings = stamp_published(week, data)
    rebuild_index()
    write_places()
    still = verify_problems()
    assert not still, still

    s = summary(data)
    print(f"published {week} ({data['meta']['kind']}, {len(body) / 1024:.0f} KB, sha256 {sha[:12]}…)")
    print(f"  FTS funded ${s['fts_funded_usd']:,.0f} of ${s['requirement_usd']:,.0f}; "
          f"ledger entries {s['ledger_entries']} ({n_entries} published for the first time); "
          f"donation listings newly published: {n_listings}")
    print("  verify_published: OK")
    print("\nNow review the change and commit it yourself:")
    print(f"  git add docs/data data/ledger data/orgs data/manifests && "
          f"git commit -m \"Publish {week}\" && git push")


if __name__ == "__main__":
    main()
