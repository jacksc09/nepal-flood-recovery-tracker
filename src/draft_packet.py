"""Write drafting packets: self-contained files a model session reads and answers.

Drafting happens in-session (no paid API): a model reads one packet file
and writes packet_NN.out.jsonl next to it. Each packet holds the rules,
enums and worked examples from extract_prompt.py (the single source of
truth), then a batch of articles, then an END marker so a truncated read
is obvious.

Which articles: every money candidate (match_places.py) published in or
before the target week that has not been put in a packet before. So the
first run for 2026-W35 takes W35's candidates, and a story found late
(published in W36 but first fetched while preparing W38) is drafted with
W38 and later shown as a late find, never inserted back into W36.

Long articles are cut to the paragraphs that matter (the first one, every
paragraph with an amount or a corridor place, and one paragraph either
side of an amount), with "[...]" marking the gaps. An excerpt must still
be copied from one paragraph; it is checked against the FULL stored text.

Run from the repo root:  .venv/bin/python src/draft_packet.py --week 2026-W35
"""

import argparse
import csv
import json
import sys

import articles
from config import RAW
from extract_prompt import RULES, draft_model, render_examples
from gazetteer import Gazetteer
from match_places import MATCHES_CSV, MONEY_RE
from textnorm import norm
from weeks import week_end_date

PACKETS = RAW / "packets"
MAX_ARTICLES = 15
MAX_CHARS = 45_000
FULL_TEXT_UP_TO = 4_000      # shorter articles go in whole
MAX_ARTICLE_CHARS = 7_000

HEADER = """\
# Drafting packet: {week} / {name}

This file contains everything you need: the task, the rules, worked
examples and {n} news articles. Do not read any other files, and do not
search the web.

## Your task

Read EVERY article at the bottom of this file, in order, and decide for
yourself (do not write a script) what it records. Then use the Write tool
to create this exact file:

{out_path}

It must contain exactly {n} lines, one per article, in the order listed.
Each line is a single JSON object: {{"article_id": "...", "entries": [...],
"orgs": [...]}}, with the fields exactly as in the worked examples. No
markdown, no code fences, no blank lines, nothing before or after.

The article list ends with an "END OF ARTICLE LIST" marker. If you do not
see it, your read was cut short: read the rest with the offset parameter
before you start.

## Rules

{rules}

## Worked examples (invented text, for illustration only)

{examples}

## The articles ({n})

"""

FOOTER = """
END OF ARTICLE LIST - {n} articles. Now write {out_path} with exactly {n}
lines, one per article, as instructed at the top of this file.
"""


def packet_dir(week: str):
    return PACKETS / week


def load_index(week: str) -> dict:
    path = packet_dir(week) / "index.json"
    if not path.exists():
        raise SystemExit(f"no packets for {week}: run src/draft_packet.py --week {week}")
    return json.loads(path.read_text())


def already_packeted() -> set:
    ids = set()
    for index in PACKETS.glob("*/index.json"):
        for p in json.loads(index.read_text())["packets"].values():
            ids |= set(p["ids"])
    return ids


def article_view(gaz: Gazetteer, aid: str) -> str:
    """The text shown for one article: whole, or the paragraphs that matter."""
    text = articles.read_text(aid)
    if len(text) <= FULL_TEXT_UP_TO:
        return text
    paras = text.split("\n\n")
    keep = {0}
    for i, p in enumerate(paras):
        if MONEY_RE.search(norm(p)):
            keep |= {i - 1, i, i + 1}
        elif gaz.match(p):
            keep.add(i)
    out, size, last = [], 0, -1
    for i in sorted(k for k in keep if 0 <= k < len(paras)):
        if size + len(paras[i]) > MAX_ARTICLE_CHARS:
            out.append("[... article continues; rest omitted ...]")
            break
        if i != last + 1:
            out.append("[...]")
        out.append(paras[i])
        size += len(paras[i])
        last = i
    return "\n\n".join(out)


def write_packet(week: str, name: str, items: list, model: str) -> dict:
    """items: [(article_id, metadata row, view text)]. Returns the index entry."""
    out_path = packet_dir(week) / f"{name}.out.jsonl"
    body = [HEADER.format(week=week, name=name, n=len(items), out_path=out_path,
                          rules=RULES, examples=render_examples())]
    for aid, meta, view in items:
        day = meta["published_at_utc"][:10]
        body.append(f"### article_id: {aid}\nOutlet: {meta['outlet']} ({meta['lang']}) | "
                    f"Published: {day}\nHeadline: {meta['title']}\n\n{view}\n")
    body.append(FOOTER.format(n=len(items), out_path=out_path))
    (packet_dir(week) / f"{name}.md").write_text("\n".join(body), encoding="utf-8")
    return {"file": f"{name}.md", "out": out_path.name, "model": model,
            "ids": [aid for aid, _, _ in items]}


def pack(week: str, prefix: str, aids: list, start: int, model: str) -> dict:
    """Greedy packing into packets of at most MAX_ARTICLES / MAX_CHARS."""
    gaz = Gazetteer.load()
    store = articles.load()
    made, current, size = {}, [], 0
    for aid in aids:
        view = article_view(gaz, aid)
        if current and (len(current) >= MAX_ARTICLES or size + len(view) > MAX_CHARS):
            name = f"{prefix}_{start + len(made):02d}"
            made[name] = write_packet(week, name, current, model)
            current, size = [], 0
        current.append((aid, store[aid], view))
        size += len(view)
    if current:
        name = f"{prefix}_{start + len(made):02d}"
        made[name] = write_packet(week, name, current, model)
    return made


def make_retry(week: str, aids: list, model: str = None) -> list:
    """Re-packet articles whose lines failed the checks (called by check_drafts)."""
    index = load_index(week)
    n_retry = sum(1 for n in index["packets"] if n.startswith("retry_"))
    made = pack(week, "retry", aids, n_retry + 1, model or draft_model())
    index["packets"].update(made)
    (packet_dir(week) / "index.json").write_text(json.dumps(index, indent=2))
    return list(made)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", required=True)
    args = parser.parse_args()

    if (packet_dir(args.week) / "index.json").exists():
        sys.exit(f"packets for {args.week} already exist; drafting outputs would be "
                 "orphaned. Use check_drafts.py --make-retry for failed articles.")
    end = week_end_date(args.week).isoformat()
    done = already_packeted()
    with open(MATCHES_CSV, newline="", encoding="utf-8") as f:
        matches = list(csv.DictReader(f))
    store = articles.load()
    todo = sorted((m["article_id"] for m in matches
                   if m["money_candidate"] == "yes" and m["article_id"] not in done
                   and store[m["article_id"]]["published_at_utc"][:10] <= end),
                  key=lambda a: (store[a]["published_at_utc"], a))
    if not todo:
        print(f"{args.week}: no new money candidates to draft")
        return
    model = draft_model()
    packet_dir(args.week).mkdir(parents=True, exist_ok=True)
    made = pack(args.week, "packet", todo, 1, model)
    (packet_dir(args.week) / "index.json").write_text(json.dumps(
        {"week": args.week, "model": model, "packets": made}, indent=2))

    # --- sanity checks ---------------------------------------------------
    late = [a for a in todo if store[a]["published_week"] != args.week]
    sizes = [(packet_dir(args.week) / p["file"]).stat().st_size for p in made.values()]
    print(f"{args.week}: {len(todo)} articles in {len(made)} packets "
          f"({len(late)} published in earlier weeks, i.e. late finds)")
    print(f"largest packet file: {max(sizes) / 1024:.0f} KB")
    for p in made.values():
        assert (packet_dir(args.week) / p["file"]).read_text().rstrip().endswith(
            "as instructed at the top of this file."), "packet is missing its END marker"
    print("every packet ends with its END marker")
    print(f"GATE 1: draft {len(made)} packets in-session (model {model}), "
          f"then run: .venv/bin/python src/check_drafts.py --week {args.week}")


if __name__ == "__main__":
    main()
