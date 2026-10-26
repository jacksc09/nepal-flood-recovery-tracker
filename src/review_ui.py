"""The local review app: every ledger entry, listing and FTS scope call is decided here.

Nothing drafted by the language model reaches the public data without a
person deciding it in this app, one item at a time (there is no
"approve all"). Three tabs:

  Ledger         drafted money entries: approve, edit then approve, reject
                 with a reason, add as another source of an existing entry,
                 record as a later stage of the same money, link an FTS flow.
  Organisations  "organisation X is working in place Y" sightings: accept
                 (linked to an organisation record) or reject. Each
                 organisation's donation link is entered and confirmed by
                 hand here, after opening it. The Records view lists every
                 organisation, so a link can be added or checked at any
                 time, and new organisations (e.g. for the national list)
                 can be added. Suggested links (org_link_suggestions.jsonl)
                 are shown as suggestions only: using one fills the form,
                 and nothing is saved or marked checked until Jack does it.
  Duplicate groups (in the Ledger tab): when one announcement has been
                 drafted from many articles, duplicate_groups.jsonl lists
                 the pending copies. Jack reads the list, unticks any that
                 should stay, and rejects the rest with one click; each is
                 logged as its own rejection, exactly as if done one by one.
  FTS scope      OCHA flows reported outside the appeal: in scope or not.

Writes (and nothing else):
  data/ledger/ledger.jsonl, data/ledger/review_log.csv,
  data/orgs/orgs.csv, data/orgs/org_areas.jsonl, data/manual/fts_scope.csv,
  data/review/decisions.journal.jsonl (every decision, append-only).
On startup a backup copy of each of those files goes to
data/review/backups/<time>/.

Run from the repo root:  .venv/bin/python src/review_ui.py
then open http://localhost:8766 (it opens itself unless --no-open).
"""

import argparse
import csv
import json
import re
import shutil
import threading
import webbrowser
from datetime import date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import articles
import extract_prompt as ep
import ledger
import orgs
from config import MANUAL, NPT, PROCESSED, REVIEW_DIR
from fx import Rates
from gazetteer import Gazetteer
from textnorm import norm

QUEUE = REVIEW_DIR / "queue.jsonl"
TRANSLATIONS = REVIEW_DIR / "translations.jsonl"   # reading aid from translate_items.py
PLACE_SUGGESTIONS = REVIEW_DIR / "place_suggestions.jsonl"   # from check_places.py
EXTRA_SUGGESTIONS = REVIEW_DIR / "extra_suggestions.jsonl"   # repeats/overlaps found by hand
LINK_SUGGESTIONS = REVIEW_DIR / "org_link_suggestions.jsonl"   # official pages to open and check
DUPLICATE_GROUPS = REVIEW_DIR / "duplicate_groups.jsonl"   # many drafts of one announcement
JOURNAL = REVIEW_DIR / "decisions.journal.jsonl"
REVIEW_LOG = ledger.LEDGER_DIR / "review_log.csv"
SCOPE_CSV = MANUAL / "fts_scope.csv"
FTS_CSV = PROCESSED / "fts_flows.csv"
LOG_FIELDS = ["item_id", "kind", "decision", "reason", "entry_id", "reviewed_on"]
SCOPE_FIELDS = ["flow_id", "in_scope", "reason", "decided_on"]
REJECT_REASONS = {
    "ledger": {"1": "not money for this flood", "2": "duplicate of an existing entry",
               "3": "amount or currency unclear", "4": "not a donation or allocation",
               "5": "other"},
    "org": {"1": "not an organisation people can donate to", "2": "not working in that place",
            "3": "duplicate", "4": "wrong organisation or place", "5": "other"},
}
PUBLIC_EXCERPT_WORDS = 25


def today() -> str:
    return datetime.now(NPT).date().isoformat()


def read_csv(path) -> list:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_csv(path, fields, row) -> None:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)


def first_words(text: str, n: int) -> str:
    words = text.split()
    return " ".join(words[:n]) + (" …" if len(words) > n else "")


def count_from(text):
    """'300 families' / '९७४ परिवार' -> 300 / 974 (None if there is no single number)."""
    if not text:
        return None
    nums = re.findall(r"\d[\d,]*", norm(text))
    return int(nums[0].replace(",", "")) if len(nums) == 1 else None


def same_donor(a: dict, b: dict) -> bool:
    """Shared name words, or the same name when it is only common words ("Government of Nepal")."""
    ta, tb = tokens(a["draft"]["donor_name_en"]), tokens(b["draft"]["donor_name_en"])
    if ta or tb:
        return bool(ta & tb)
    return a["draft"]["donor_name_en"].strip().lower() == b["draft"]["donor_name_en"].strip().lower()


def same_money_entries(a: dict, b: dict) -> bool:
    """Two ledger entries for the same gift: same donor, amount within 5%, a week apart at most."""
    if a["donor_name"].strip().lower() != b["donor_name"].strip().lower():
        return False
    x, y = Decimal(a["amount_npr"]), Decimal(b["amount_npr"])
    days = abs((date.fromisoformat(a["announced_on"]) - date.fromisoformat(b["announced_on"])).days)
    return abs(x - y) <= max(x, y) * Decimal("0.05") and days <= 7


def same_place(a: dict, b: dict) -> bool:
    """Where both drafts resolved a place, it must be the same one."""
    pa, pb = set(a["computed"]["place_candidates"]), set(b["computed"]["place_candidates"])
    return not (pa and pb) or bool(pa & pb)


def tokens(name: str) -> set:
    stop = {"of", "the", "and", "government", "nepal", "ltd", "limited", "bank", "for"}
    return {t for t in re.findall(r"[a-z]{3,}", (name or "").lower()) if t not in stop}


class Store:
    """All review state, read from disk at startup and written through on each decision."""

    def __init__(self):
        self.lock = threading.Lock()
        self.gaz = Gazetteer.load()
        self.rates = Rates.load()
        self.queue = [json.loads(l) for l in QUEUE.read_text(encoding="utf-8").splitlines()
                      if l] if QUEUE.exists() else []
        self.flows = {r["flow_id"]: r for r in read_csv(FTS_CSV)}
        self.reload()

    def reload(self):
        self.ledger = ledger.load_ledger()
        self.log = {r["item_id"]: r for r in read_csv(REVIEW_LOG)}
        self.orgs = orgs.load_orgs()
        self.areas = orgs.load_areas()
        self.scope = {r["flow_id"]: r for r in read_csv(SCOPE_CSV)}

    # --- helpers -----------------------------------------------------------
    def journal(self, kind: str, payload: dict) -> None:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now(NPT).isoformat(timespec="seconds"),
                                "kind": kind, **payload}, ensure_ascii=False) + "\n")

    def write_ledger(self):
        ledger.LEDGER_JSONL.parent.mkdir(parents=True, exist_ok=True)
        tmp = ledger.LEDGER_JSONL.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in self.ledger),
                       encoding="utf-8")
        tmp.replace(ledger.LEDGER_JSONL)

    def next_id(self, prefix: str, existing) -> str:
        nums = [int(x.split("-")[1]) for x in existing if x and x.startswith(prefix + "-")]
        return f"{prefix}-{max(nums, default=0) + 1:06d}"

    def place_label(self, place_id: str) -> str:
        p = self.gaz.places.get(place_id)
        if not p:
            return place_id
        district = self.gaz.places.get(p.pcode[:6])
        where = f", {district.name_en}" if district and p.kind not in ("district", "province") else ""
        return f"{p.name_en} {p.name_ne} ({p.kind}{where})"

    def context(self, aid: str, excerpt: str) -> dict:
        """About 400 characters either side of the excerpt, for reading it in place."""
        text = articles.read_text(aid) or ""
        i = text.find(excerpt)
        if i < 0:   # differs only by normalisation: show the normalised text
            text, excerpt = norm(text), norm(excerpt)
            i = text.find(excerpt)
        if i < 0:
            return {"before": "", "match": excerpt, "after": ""}
        return {"before": text[max(0, i - 400):i], "match": excerpt,
                "after": text[i + len(excerpt):i + len(excerpt) + 400]}

    def convert(self, amount: Decimal, currency: str, on: date) -> dict:
        npr, rate_date = self.rates.convert(amount, currency, "NPR", on)
        usd, _ = self.rates.convert(npr, "NPR", "USD", on)
        per_unit, _ = self.rates.npr_per_unit(currency, on)
        usd_rate, usd_date = self.rates.npr_per_unit("USD", on)
        return {"npr": npr.quantize(Decimal(1)), "usd": usd.quantize(Decimal(1)),
                "fx": {"npr_per_unit": str(per_unit), "usd_npr": str(usd_rate),
                       "rate_date": rate_date or usd_date,
                       "source": "Nepal Rastra Bank, mid of buy and sell"}}

    def suggestions(self, item: dict) -> dict:
        """Possible duplicates (same money) and FTS flows, for a person to judge."""
        d, c = item["draft"], item["computed"]
        amount = Decimal(c["amount"])
        day = date.fromisoformat(item["article"]["published_at_utc"][:10])
        donor = tokens(d["donor_name_en"])
        dupes = []
        for e in self.ledger:
            close_amount = abs(Decimal(e["amount"]) - amount) <= amount * Decimal("0.05")
            close_day = abs((date.fromisoformat(e["announced_on"]) - day).days) <= 7
            if close_amount and close_day and (tokens(e["donor_name"]) & donor):
                dupes.append({"entry_id": e["entry_id"], "money_id": e["money_id"],
                              "label": f"{e['entry_id']} {e['donor_name']} {e['amount_text']} "
                                       f"{e['status']} {e['announced_on']}"})
        fts = []
        if not c["currency_needs_confirmation"]:
            try:
                usd = self.convert(amount, c["currency"], day)["usd"]
            except (LookupError, ValueError):
                usd = None
            for f in self.flows.values():
                if not usd or not f["amount_usd"]:
                    continue
                if abs(Decimal(f["amount_usd"]) - usd) <= usd * Decimal("0.05") and \
                        abs((date.fromisoformat(f["date"]) - day).days) <= 14 and \
                        tokens(f["source_org"]) & donor:
                    fts.append({"flow_id": f["flow_id"],
                                "label": f"FTS {f['flow_id']} {f['source_org']} -> "
                                         f"{f['destination_org']} ${float(f['amount_usd']):,.0f}"})
        return {"dupes": dupes, "fts": fts}

    # --- state for the page -----------------------------------------------------
    def state(self) -> dict:
        with self.lock:
            items = []
            translations = {}
            if TRANSLATIONS.exists():   # re-read each time, so new ones show on reload
                translations = {r["item_id"]: r for r in map(
                    json.loads, TRANSLATIONS.read_text(encoding="utf-8").splitlines())}
            for it in self.queue:
                if it["item_id"] in self.log:
                    continue
                view = dict(it)
                view["translation"] = translations.get(it["item_id"])
                view["context"] = self.context(it["article"]["article_id"],
                                               it["draft"]["evidence_excerpt"])
                view["place_options"] = [{"id": p, "label": self.place_label(p)}
                                         for p in it["computed"]["place_candidates"]]
                if it["kind"] == "ledger":
                    view["suggest"] = self.suggestions(it)
                items.append(view)
            items = self.group_lookalikes(items)
            decided = Counter_like(r["decision"] for r in self.log.values())
            out_of_plan = [f for f in self.flows.values() if f["in_plan"] == "no"]
            return {
                "items": items,
                "places": sorted(({"id": p.place_id, "label": self.place_label(p.place_id)}
                                  for p in self.gaz.places.values()), key=lambda x: x["label"]),
                "enums": {"status": ep.STATUS, "basis": ep.AMOUNT_BASIS, "form": ep.FORM,
                          "donor_type": ep.DONOR_TYPES, "sector": ep.SECTORS,
                          "phase": ep.PHASES, "org_type": ep.ORG_TYPES,
                          "currencies": [c for c in ep.CURRENCY_HINTS if c != "unknown"]},
                "reasons": REJECT_REASONS,
                "orgs": sorted(self.orgs.values(), key=lambda o: o["name_en"]),
                **self.org_records(),
                "dup_groups": self.duplicate_groups(translations),
                "flows": sorted(out_of_plan, key=lambda f: -float(f["amount_usd"] or 0)),
                "scope": self.scope,
                "stats": {"pending": len(items), "decided": decided,
                          "ledger_entries": len(self.ledger)},
                "checks": self.checks(translations),
            }

    # --- organisation records and suggested links -------------------------------------
    def link_suggestions(self) -> list:
        """Suggested official pages (re-read each time). Shown, never saved by code."""
        if not LINK_SUGGESTIONS.exists():
            return []
        out = []
        for g in map(json.loads, filter(None, LINK_SUGGESTIONS.read_text(encoding="utf-8").splitlines())):
            url = g.get("donate_url") or ""
            host = re.sub(r"^www\.", "", (url.split("/")[2] if "//" in url else "").lower())
            # The rules the link would face when saved, so a bad suggestion shows as one.
            g["problems"] = orgs.url_problems(url, {"donate_domain": host,
                                                    "website": g.get("website") or ""}) if url else []
            out.append(g)
        return out

    def org_records(self) -> dict:
        suggestions = self.link_suggestions()
        places = {}
        for l in self.areas:
            places.setdefault(l["org_id"], set()).add(l["place_id"])
        records = [{**o, "places": sorted(self.place_label(p) for p in places.get(o["org_id"], ())),
                    "suggestions": [g for g in suggestions if g.get("org_id") == o["org_id"]]}
                   for o in sorted(self.orgs.values(), key=lambda o: o["name_en"])]
        new = [g for g in suggestions if not g.get("org_id") or g["org_id"] not in self.orgs]
        return {"org_records": records, "new_org_suggestions": new}

    # --- duplicate groups ---------------------------------------------------------------
    def duplicate_groups(self, translations: dict) -> list:
        """Suggested groups of pending drafts that repeat one announcement (still pending only)."""
        if not DUPLICATE_GROUPS.exists():
            return []
        by_id = {i["item_id"]: i for i in self.queue}
        out = []
        for g in map(json.loads, filter(None, DUPLICATE_GROUPS.read_text(encoding="utf-8").splitlines())):
            items = []
            for iid in g["item_ids"]:
                it = by_id.get(iid)
                if not it or iid in self.log:
                    continue
                d, t = it["draft"], translations.get(iid) or {}
                items.append({"item_id": iid, "amount_text": d["amount_text"], "basis": d["amount_basis"],
                              "donor": d["donor_name_en"], "title": it["article"]["title"],
                              "outlet": it["article"]["outlet"], "url": it["article"]["url"],
                              "published": it["article"]["published_at_utc"][:10],
                              "excerpt": d["evidence_excerpt"], "excerpt_en": t.get("excerpt_en", "")})
            if items:
                out.append({"group_id": g["group_id"], "label": g["label"], "note": g.get("note", ""),
                            "reason": g.get("reason", "2"), "items": items})
        return out

    def reject_group(self, p: dict) -> dict:
        """Reject the copies Jack left ticked; each is logged as its own rejection."""
        if p.get("reason") not in REJECT_REASONS["ledger"]:
            raise ValueError("choose a reason")
        rows = [self.decide_ledger({"item_id": iid, "action": "reject", "reason": p["reason"],
                                    "group_id": p.get("group_id")})
                for iid in p.get("item_ids") or []]
        return {"rejected": len(rows), "group_id": p.get("group_id")}

    # --- the place / repeat / overlap check -------------------------------------------
    def all_suggestions(self) -> list:
        """Every check suggestion, each with a check_id and a kind.

        place_suggestions.jsonl (check_places.py): kind place, each or repeat,
        check_id = the entry id. extra_suggestions.jsonl (hand-found): kind
        repeat, remove (a total whose parts are also entries) or stage (the
        same money at another stage), with its own check_id.
        """
        out = []
        if PLACE_SUGGESTIONS.exists():
            for line in PLACE_SUGGESTIONS.read_text(encoding="utf-8").splitlines():
                sug = json.loads(line)
                kind = "repeat" if sug["same_as"] else "each" if sug["each"] else "place"
                out.append({**sug, "check_id": sug["entry_id"], "kind": kind})
        if EXTRA_SUGGESTIONS.exists():
            for line in EXTRA_SUGGESTIONS.read_text(encoding="utf-8").splitlines():
                sug = json.loads(line)
                out.append({"geo_level": None, "place_ids": [], "each": False, "parts": [],
                            "model": sug.get("found_by", ""), **sug})
        return out

    def entry_label(self, e: dict) -> str:
        return (f"{e['entry_id']}: {e['donor_name']}, {e['amount_text']}, {e['announced_on']}, "
                f"{self.where_label(e['geo_level'], [e['place_id']])}")

    def checks(self, translations: dict) -> list:
        """Suggestions not yet decided that would change something."""
        by_entry = {e["entry_id"]: e for e in self.ledger}
        item_of = {r["entry_id"]: r["item_id"] for r in self.log.values()
                   if r["kind"] == "ledger" and r["entry_id"]}
        out = []
        for sug in self.all_suggestions():
            e = by_entry.get(sug["entry_id"])
            if not e or e.get("first_published_week") or f"check:{sug['check_id']}" in self.log:
                continue
            if sug["kind"] == "place" and sug["geo_level"] == e["geo_level"] and (
                    sug["geo_level"] in ("corridor", "national") or sug["place_ids"][0] == e["place_id"]):
                continue                          # the check agrees with the entry
            target = None
            if sug["kind"] in ("repeat", "stage"):
                target = by_entry.get(self.merged_target(sug["same_as"]))
                if not target or target is e:
                    continue
            parts = [by_entry[x] for x in sug.get("parts") or [] if x in by_entry]
            src = e["sources"][0]
            out.append({
                "check_id": sug["check_id"], "kind": sug["kind"],
                "entry": {k: e[k] for k in ("entry_id", "amount_text", "amount_npr", "donor_name",
                                            "announced_on", "geo_level", "place_id",
                                            "recipient_as_written", "place_as_written")},
                "current": self.where_label(e["geo_level"], [e["place_id"]]),
                "suggestion": sug,
                "suggested": (self.where_label(sug["geo_level"], sug["place_ids"])
                              if sug["geo_level"] else ""),
                "target": target and {"entry_id": target["entry_id"], "label": self.entry_label(target)},
                "parts": [self.entry_label(x) for x in parts],
                "source": src, "context": self.context(src["article_id"], src["evidence_excerpt"]),
                "translation": translations.get(item_of.get(e["entry_id"], "")),
            })
        return out

    def where_label(self, level: str, place_ids: list) -> str:
        if level == "national":
            return "National (not location-specific)"
        if level == "corridor":
            return "Several corridor districts (not location-specific)"
        return f"{level}: " + " + ".join(self.place_label(p) for p in place_ids)

    def merged_target(self, entry_id: str) -> str:
        """Follow earlier merges: if the target was itself merged, use where it went."""
        merged_into = {}
        for sug in self.all_suggestions():
            row = self.log.get(f"check:{sug['check_id']}")
            if row and row["decision"] == "merged":
                merged_into[sug["entry_id"]] = row["entry_id"]
        live = {e["entry_id"] for e in self.ledger}
        seen = set()
        while entry_id and entry_id not in live and entry_id not in seen:
            seen.add(entry_id)
            entry_id = merged_into.get(entry_id)
        return entry_id

    def set_place(self, e: dict, level: str, place_id: str) -> None:
        if level in ("corridor", "national"):
            e["geo_level"], e["pcode"], e["place_id"] = level, level.upper(), level.upper()
            return
        place = self.gaz.places.get(place_id)
        if not place:
            raise ValueError("choose a place")
        e["geo_level"], e["pcode"], e["place_id"] = level, place.pcode, place_id

    def decide_check(self, p: dict) -> dict:
        with self.lock:
            check_id = p.get("check_id") or p["entry_id"]
            if f"check:{check_id}" in self.log:
                raise ValueError("already decided")
            sug = next((s for s in self.all_suggestions() if s["check_id"] == check_id), None)
            if not sug:
                raise ValueError("unknown check")
            e = next((x for x in self.ledger if x["entry_id"] == sug["entry_id"]), None)
            if not e or e.get("first_published_week"):
                raise ValueError("entry not found or already published")
            action, result_id, decision = p["action"], e["entry_id"], "kept"
            if action == "accept" and sug["kind"] == "remove":
                self.ledger.remove(e)              # a total whose parts are also entries
                decision = "removed"
            elif action == "accept" and sug["kind"] == "stage":
                target = next(x for x in self.ledger if x["entry_id"] == self.merged_target(sug["same_as"]))
                e["money_id"] = target["money_id"]  # one stream of money, counted once
                decision, result_id = "linked", target["entry_id"]
            elif action == "accept" and sug["kind"] == "repeat":
                target = next(x for x in self.ledger if x["entry_id"] == self.merged_target(sug["same_as"]))
                target["sources"].extend(e["sources"])
                self.ledger.remove(e)
                decision, result_id = "merged", target["entry_id"]
            elif action == "accept" and sug["kind"] == "each":
                # One amount for EACH place. A place that already has an entry
                # for this gift gets this article as another source instead of
                # a second entry, so nothing is counted twice.
                new_places = []
                for place_id in sug["place_ids"]:
                    twin_of = next((x for x in self.ledger if x is not e and x["place_id"] == place_id
                                    and same_money_entries(x, e)), None)
                    if twin_of:
                        twin_of["sources"].extend(e["sources"])
                    else:
                        new_places.append(place_id)
                if not new_places:
                    self.ledger.remove(e)
                    decision = "merged"
                else:
                    self.set_place(e, sug["geo_level"], new_places[0])
                    decision = "split" if len(new_places) > 1 else "place_changed"
                    for extra in new_places[1:]:
                        twin = json.loads(json.dumps(e))
                        twin["entry_id"] = self.next_id("L", [x["entry_id"] for x in self.ledger])
                        twin["money_id"] = self.next_id("M", [x["money_id"] for x in self.ledger])
                        self.set_place(twin, sug["geo_level"], extra)
                        twin["review"] = dict(twin["review"], decision="split", split_from=e["entry_id"])
                        self.ledger.append(twin)
            elif action == "accept":
                self.set_place(e, sug["geo_level"], (sug["place_ids"] or [""])[0])
                decision = "place_changed"
            elif action == "custom":
                self.set_place(e, p["geo_level"], p.get("place_id") or "")
                decision = "place_changed"
            if decision in ("split", "place_changed") and e in self.ledger:
                e["review"]["edited_fields"] = sorted(set(e["review"]["edited_fields"]) | {"place"})
                e["review"]["decision"] = "edited" if e["review"]["decision"] != "split" else "split"
            self.write_ledger()
            row = {"item_id": f"check:{check_id}", "kind": "check", "decision": decision,
                   "reason": sug["reason"][:120] if action == "accept" else "",
                   "entry_id": result_id, "reviewed_on": today()}
            append_csv(REVIEW_LOG, LOG_FIELDS, row)
            self.log[row["item_id"]] = row
            self.journal("check", {"payload": p, "suggestion": sug, "result": row})
            return row

    def group_lookalikes(self, items: list) -> list:
        """Put drafts that look like the same donation next to each other.

        The same gift is often reported by several articles (in Nepali and
        English). Sorting by donor and amount keeps them together, and each
        item lists its look-alikes, so the first can be approved and the
        rest recorded as further sources of that entry instead of being
        counted twice. A hint only: the reviewer decides.
        """
        money = [i for i in items if i["kind"] == "ledger"]
        others = [i for i in items if i["kind"] != "ledger"]
        key = lambda i: (" ".join(sorted(tokens(i["draft"]["donor_name_en"]))),
                         Decimal(i["computed"]["amount"]), i["article"]["published_at_utc"])
        money.sort(key=key)
        for a in money:
            a["lookalikes"] = []
            amt_a = Decimal(a["computed"]["amount"])
            day_a = date.fromisoformat(a["article"]["published_at_utc"][:10])
            for b in money:
                if b is a or b["computed"]["currency"].rstrip("?") != a["computed"]["currency"].rstrip("?"):
                    continue
                amt_b = Decimal(b["computed"]["amount"])
                close = abs(amt_b - amt_a) <= max(amt_a, amt_b) * Decimal("0.05")
                near = abs((date.fromisoformat(b["article"]["published_at_utc"][:10]) - day_a).days) <= 7
                if close and near and same_donor(a, b) and same_place(a, b):
                    a["lookalikes"].append({"item_id": b["item_id"], "outlet": b["article"]["outlet"]})
        return money + others

    # --- decisions --------------------------------------------------------------
    def decide_ledger(self, p: dict) -> dict:
        with self.lock:
            item = next(i for i in self.queue if i["item_id"] == p["item_id"])
            if item["item_id"] in self.log:
                raise ValueError("already decided")
            action = p["action"]
            entry_id = ""
            if action == "reject":
                reason = REJECT_REASONS["ledger"][p["reason"]]
                decision = "rejected"
            elif action == "add_source":
                target = next(e for e in self.ledger if e["entry_id"] == p["entry_id"])
                if target.get("first_published_week"):
                    raise ValueError(f"{target['entry_id']} is already published: add the "
                                     "source with src/correct.py instead")
                target["sources"].append(self.source(item))
                entry_id, decision, reason = target["entry_id"], "added_as_source", ""
                self.write_ledger()
            else:
                entry = self.build_entry(item, p)
                self.ledger.append(entry)
                self.write_ledger()
                entry_id, decision, reason = entry["entry_id"], entry["review"]["decision"], ""
            row = {"item_id": item["item_id"], "kind": "ledger", "decision": decision,
                   "reason": reason, "entry_id": entry_id, "reviewed_on": today()}
            append_csv(REVIEW_LOG, LOG_FIELDS, row)
            self.log[item["item_id"]] = row
            self.journal("ledger", {"payload": p, "result": row})
            return row

    def source(self, item: dict) -> dict:
        a = item["article"]
        return {"article_id": a["article_id"], "url": a["url"], "outlet": a["outlet"],
                "lang": a["lang"], "published_at": a["published_at_utc"],
                "title": a["title"], "evidence_excerpt": item["draft"]["evidence_excerpt"]}

    def build_entry(self, item: dict, p: dict) -> dict:
        d, c = item["draft"], item["computed"]
        f = p["fields"]       # what the reviewer saw and possibly changed
        currency = f["currency"]
        if not currency or currency.endswith("?"):
            raise ValueError("confirm the currency first")
        amount = Decimal(c["amount"])
        on = date.fromisoformat(f["announced_on"])
        count = int(f["unit_count"]) if f.get("unit_count") else None
        computed = f["amount_basis"] != "total" and count is not None
        total = amount * count if computed else amount
        money = self.convert(total, currency, on)
        geo_level, place_id = f["geo_level"], f.get("place_id") or ""
        if geo_level in ("municipality", "district", "province"):
            place = self.gaz.places.get(place_id)
            if not place:
                raise ValueError("choose the place")
            pcode = place.pcode
        else:
            pcode, place_id = ("CORRIDOR", "CORRIDOR") if geo_level == "corridor" \
                else ("NATIONAL", "NATIONAL")
        drafted = {"status": d["status"], "amount_basis": d["amount_basis"],
                   "donor_type": d["donor_type"], "sector": d["sector"], "phase": d["phase"],
                   "donor_name": d["donor_name_en"], "form": d["form"],
                   "currency": c["currency"], "geo_level": d["geo_hint"],
                   "announced_on": item["article"]["published_at_utc"][:10],
                   "place_id": (c["place_candidates"] or [""])[0]}
        edited = sorted(k for k, v in drafted.items() if str(f.get(k, v)) != str(v))
        if f.get("unit_count") and count != count_from(d["unit_count_text"]):
            edited.append("unit_count")
        if p.get("fts_flow_id"):
            edited.append("fts_flow_id")
        money_id = p.get("money_id") or self.next_id("M", [e["money_id"] for e in self.ledger])
        return {
            "entry_id": self.next_id("L", [e["entry_id"] for e in self.ledger]),
            "money_id": money_id, "status": f["status"],
            "amount_text": d["amount_text"], "amount": str(amount), "currency": currency,
            "amount_basis": f["amount_basis"], "unit_count_text": d["unit_count_text"],
            "unit_count": count, "amount_computed": computed,
            "amount_npr": str(money["npr"]), "amount_usd": str(money["usd"]), "fx": money["fx"],
            "form": f["form"], "donor_name": f["donor_name"],
            "donor_as_written": d["donor_as_written"], "donor_type": f["donor_type"],
            "recipient_as_written": d["recipient_as_written"],
            "geo_level": geo_level, "pcode": pcode, "place_id": place_id,
            "place_as_written": d["place_as_written"],
            "sector": f["sector"], "phase": f["phase"], "announced_on": on.isoformat(),
            "sources": [self.source(item)], "fts_flow_id": p.get("fts_flow_id") or None,
            "drafted_by": item["drafted_by"], "drafted_in": f"{item['week']}/{item['packet']}",
            "review": {"decision": "edited" if edited else "approved",
                       "edited_fields": edited, "reviewed_on": today()},
            "first_published_week": None,
        }

    def decide_org(self, p: dict) -> dict:
        with self.lock:
            item = next(i for i in self.queue if i["item_id"] == p["item_id"])
            if item["item_id"] in self.log:
                raise ValueError("already decided")
            if p["action"] == "reject":
                row = {"item_id": item["item_id"], "kind": "org", "decision": "rejected",
                       "reason": REJECT_REASONS["org"][p["reason"]], "entry_id": "",
                       "reviewed_on": today()}
            else:
                org_id = p["org_id"]
                if org_id not in self.orgs:
                    raise ValueError("choose or create the organisation record first")
                if p["place_id"] not in self.gaz.places:
                    raise ValueError("choose the place")
                a = item["article"]
                listing = {"org_id": org_id, "place_id": p["place_id"],
                           "evidence": {"url": a["url"], "kind": "article",
                                        "date": a["published_at_utc"][:10],
                                        "title": a["title"], "outlet": a["outlet"],
                                        "excerpt": first_words(item["draft"]["evidence_excerpt"],
                                                               PUBLIC_EXCERPT_WORDS)},
                           "activity": item["draft"]["activity"], "item_id": item["item_id"],
                           "reviewed_on": today(), "first_published_week": None}
                with open(orgs.ORG_AREAS, "a", encoding="utf-8") as f:
                    f.write(json.dumps(listing, ensure_ascii=False) + "\n")
                self.areas.append(listing)
                row = {"item_id": item["item_id"], "kind": "org", "decision": "approved",
                       "reason": "", "entry_id": org_id, "reviewed_on": today()}
            append_csv(REVIEW_LOG, LOG_FIELDS, row)
            self.log[item["item_id"]] = row
            self.journal("org", {"payload": p, "result": row})
            return row

    def save_org_record(self, p: dict) -> dict:
        """Create or update an organisation record (the donation link is typed by hand)."""
        with self.lock:
            rec = dict(self.orgs.get(p.get("org_id")) or {})
            if not rec:
                slug = re.sub(r"[^a-z0-9]+", "-", p["name_en"].lower()).strip("-")[:40]
                rec = {k: "" for k in orgs.ORG_FIELDS}
                rec["org_id"] = f"O-{slug}"
                if rec["org_id"] in self.orgs:
                    raise ValueError(f"{rec['org_id']} exists: choose it instead")
            for k in ("name_en", "name_ne", "type", "registration", "website", "notes",
                      "national_fallback"):
                if k in p:
                    rec[k] = p[k].strip()
            url = (p.get("donate_url") or "").strip()
            if url != rec.get("donate_url"):
                rec["donate_url"], rec["verified_on"] = url, ""   # a new link needs re-checking
                rec["donate_domain"] = (re.sub(r"^www\.", "", (url.split("/")[2] if "//" in url
                                                               else "").lower()))
                rec["link_status"] = ""
            if url:
                problems = orgs.url_problems(url, rec)
                if problems:
                    raise ValueError("link refused: " + "; ".join(problems))
            if p.get("confirm_verified"):
                if not url:
                    raise ValueError("no link to confirm")
                rec["verified_on"] = today()
                rec["link_status"] = "ok"
            self.orgs[rec["org_id"]] = rec
            orgs.save_orgs(self.orgs)
            self.journal("org_record", {"payload": p, "org_id": rec["org_id"]})
            return rec

    def decide_scope(self, p: dict) -> dict:
        with self.lock:
            if p["flow_id"] not in self.flows:
                raise ValueError("unknown flow")
            if p["in_scope"] not in ("yes", "no") or not p.get("reason", "").strip():
                raise ValueError("choose in or out of scope and give a reason")
            row = {"flow_id": p["flow_id"], "in_scope": p["in_scope"],
                   "reason": p["reason"].strip(), "decided_on": today()}
            self.scope[p["flow_id"]] = row
            rows = sorted(self.scope.values(), key=lambda r: r["flow_id"])
            SCOPE_CSV.parent.mkdir(parents=True, exist_ok=True)
            with open(SCOPE_CSV, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=SCOPE_FIELDS)
                w.writeheader()
                w.writerows(rows)
            self.journal("fts_scope", {"payload": p, "result": row})
            return row


def Counter_like(values) -> dict:
    out = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Review</title>
<style>
:root{--ink:#1a1a1a;--mut:#666;--line:#ddd;--accent:#2456a6;--good:#1d7a3d;--warn:#a63324;
--bg:#f7f7f5;--card:#fff;--hl:#fff1a8}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 -apple-system,"Segoe UI","Noto Sans Devanagari",sans-serif;color:var(--ink);background:var(--bg)}
header{display:flex;gap:14px;align-items:baseline;padding:12px 16px;border-bottom:1px solid var(--line);background:#fff;position:sticky;top:0;z-index:2}
header h1{font-size:17px;margin:0}.tabs button{border:1px solid var(--line);background:#fff;border-radius:6px;padding:5px 12px;cursor:pointer}
.tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
#msg{margin-left:auto;font-size:13px;color:var(--good)}#msg.err{color:var(--warn);font-weight:600}
main{max-width:1180px;margin:0 auto;padding:16px;display:grid;grid-template-columns:1fr 380px;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:12px}
h2{font-size:18px;margin:0 0 4px}.mut{color:var(--mut);font-size:13px}.ctx{white-space:pre-wrap;font-size:14px;max-height:340px;overflow:auto;border-left:3px solid var(--line);padding-left:10px}
mark{background:var(--hl)}label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:10px 0 3px}
select,input,textarea{width:100%;font:inherit;padding:6px;border:1px solid var(--line);border-radius:6px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.big{font-size:20px;font-weight:700}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.actions button{padding:8px 12px;border:1px solid var(--line);border-radius:7px;background:#fff;cursor:pointer}
.actions .ok{background:var(--good);color:#fff;border-color:var(--good);font-weight:600}.actions .no{color:var(--warn)}
kbd{background:#eee;border:1px solid #ccc;border-radius:4px;padding:0 5px;font-size:12px;color:var(--ink);font-weight:400}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{border-bottom:1px solid var(--line);padding:5px;text-align:left;vertical-align:top}
.warn{color:var(--warn);font-weight:600}
.need{margin:8px 0;padding:8px 10px;border-radius:8px;background:#fdecea;color:#7a1d12;font-size:14px}
select.bad{border:2px solid var(--warn)}
.tr{margin-top:12px;padding:10px 12px;background:#eef4fc;border-radius:8px;font-size:14px}.tr div{margin-top:3px}
.look{margin-top:10px;padding:8px 12px;background:#fdf3dc;border-radius:8px;font-size:14px}.hidden{display:none}
</style></head><body>
<header><h1>Review</h1><span class="tabs"><button data-tab="ledger">Ledger</button>
<button data-tab="org">Organisations</button><button data-tab="check">Check places</button><button data-tab="scope">FTS scope</button></span>
<span id="count" class="mut"></span><span id="msg"></span></header>
<main id="main"></main>
<script>
"use strict";
let S=null, tab="ledger", cur=0, rejectMode=false, orgView="sightings", recSel=null, groupView=null;
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const opts=(list,sel)=>list.map(v=>{const [val,lab]=Array.isArray(v)?v:[v,v];return `<option value="${esc(val)}"${val===sel?" selected":""}>${esc(lab)}</option>`}).join("");
function flash(m,err){const e=$("msg");e.textContent=m;e.className=err?"err":"";if(!err)setTimeout(()=>{if(e.textContent===m)e.textContent=""},1800)}
async function load(){const r=await fetch("/api/state");S=await r.json();render()}
async function post(path,body){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
 const o=await r.json();if(!r.ok){flash(o.error,true);throw new Error(o.error)}return o}
function items(){return S.items.filter(i=>i.kind===tab)}
function render(){document.querySelectorAll(".tabs button").forEach(b=>b.classList.toggle("on",b.dataset.tab===tab));
 const n={ledger:S.items.filter(i=>i.kind==="ledger").length,org:S.items.filter(i=>i.kind==="org").length};
 $("count").textContent=`${n.ledger} money items · ${n.org} organisation sightings · ${S.checks.length} place checks to review · ${S.stats.ledger_entries} ledger entries`;
 if(tab==="scope")return renderScope();if(tab==="check")return renderCheck();const list=items();cur=Math.min(cur,Math.max(0,list.length-1));
 if(tab==="org"&&(orgView==="records"||!list.length))return renderRecords();
 if(tab==="ledger"&&groupView&&S.dup_groups.some(g=>g.group_id===groupView))return renderGroup(S.dup_groups.find(g=>g.group_id===groupView));
 groupView=null;
 if(!list.length){$("main").innerHTML=`<div class="card">Nothing left to review here.</div>`;return}
 tab==="ledger"?renderLedger(list[cur]):renderOrg(list[cur])}
function translationHTML(it){const t=it.translation;if(!t)return it.article.lang==="ne"?`<p class="mut">English translation not ready yet — reload the page later.</p>`:"";
 return `<div class="tr"><label>English (machine translation — a reading aid; the Nepali above is what the checks used)</label>
 <div><b>Headline:</b> ${esc(t.headline_en)}</div><div><b>Quote:</b> ${esc(t.excerpt_en)}</div>
 <div class="mut">Donor: ${esc(t.donor_en)}${t.place_en?" · Place: "+esc(t.place_en):""}</div></div>`}
function lookalikeHTML(it){const L=it.lookalikes||[];if(!L.length)return "";const list=items();
 const refs=L.map(x=>{const n=list.findIndex(i=>i.item_id===x.item_id);return n<0?"":`#${n+1} (${esc(x.outlet)})`}).filter(Boolean).join(", ");
 return refs?`<p class="look">Looks like the same donation as ${refs}. Approve one, then record the others as “Another source for …”.</p>`:""}
function article(it){const a=it.article;return `<div class="card"><h2>${esc(a.title)}</h2>
 <div class="mut">${esc(a.outlet)} (${esc(a.lang)}) · ${esc(a.published_at_utc)} · <a href="${esc(a.url)}" target="_blank" rel="noopener">open article</a> · item ${cur+1} of ${items().length} · drafted by ${esc(it.drafted_by)}</div>
 <label>Evidence in context (highlighted = the quoted excerpt)</label>
 <div class="ctx">${esc(it.context.before)}<mark>${esc(it.context.match)}</mark>${esc(it.context.after)}</div>
 ${translationHTML(it)}${lookalikeHTML(it)}</div>`}
function placeSelect(it,sel){const cand=it.place_options.map(p=>[p.id,"★ "+p.label]);
 const rest=S.places.filter(p=>!it.place_options.some(o=>o.id===p.id)).map(p=>[p.id,p.label]);
 return `<select id="f-place_id"><option value="">— choose —</option>${opts(cand,sel)}<option disabled>──────</option>${opts(rest,sel)}</select>`}
function groupBanner(){return S.dup_groups.map(g=>`<div class="look" style="margin:0 0 12px">${g.items.length} pending drafts repeat one announcement: <b>${esc(g.label)}</b>.
 <button data-group="${esc(g.group_id)}">Review the list</button></div>`).join("")}
function bindGroupBanner(){document.querySelectorAll("button[data-group]").forEach(b=>b.onclick=()=>{groupView=b.dataset.group;render()})}
function renderGroup(g){
 const rows=g.items.map(i=>`<tr><td><input type="checkbox" class="g-pick" value="${esc(i.item_id)}" checked style="width:auto"></td>
 <td><b>${esc(i.amount_text)}</b> <span class="mut">${esc(i.basis)}</span><div class="mut">${esc(i.donor)}</div></td>
 <td><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a><div class="mut">${esc(i.outlet)} · ${esc(i.published)} · ${esc(i.item_id)}</div>
 <div>${esc(i.excerpt_en||i.excerpt)}</div></td></tr>`).join("");
 $("main").innerHTML=`<div style="grid-column:1/-1"><div class="card"><button id="g-back">← Back to items</button>
 <h2 style="margin-top:10px">${esc(g.label)}</h2><p class="mut">${esc(g.note)}</p>
 <p>Untick anything that is different money and should stay in the queue. Each ticked draft is rejected and logged on its own, as if you had rejected it here one at a time.</p>
 <table><thead><tr><th></th><th>Amount</th><th>Article and quote</th></tr></thead><tbody>${rows}</tbody></table>
 <label>Reason</label><select id="g-reason" style="max-width:360px">${opts(Object.entries(S.reasons.ledger).map(([k,v])=>[k,v]),g.reason)}</select>
 <div class="actions"><button class="no" id="g-reject">Reject the ${g.items.length} ticked drafts</button></div></div></div>`;
 const count=()=>{const n=document.querySelectorAll(".g-pick:checked").length;$("g-reject").textContent=`Reject the ${n} ticked draft${n===1?"":"s"}`;$("g-reject").disabled=!n};
 document.querySelectorAll(".g-pick").forEach(c=>c.onchange=count);
 $("g-back").onclick=()=>{groupView=null;render()};
 $("g-reject").onclick=async()=>{const ids=[...document.querySelectorAll(".g-pick:checked")].map(c=>c.value);if(!ids.length)return;
  const r=await post("/api/rejectgroup",{group_id:g.group_id,item_ids:ids,reason:$("g-reason").value});groupView=null;flash(`rejected ${r.rejected} drafts`);await load()}}
function renderLedger(it){const d=it.draft,c=it.computed,e=S.enums;
 const cur0=c.currency_needs_confirmation?"":c.currency;const geo=d.geo_hint;const place0=(it.place_options[0]||{}).id||"";
 $("main").innerHTML=article(it)+`<div><div class="card">
 <div class="big">${esc(d.amount_text)}</div><div class="mut">parsed by the code: ${esc(c.amount)} ${esc(c.currency)}</div>
 ${c.currency_needs_confirmation?`<div class="need">${c.currency==="USD?"
   ?"The quote only says “$” or “dollars”. Check the article or translation: US, Australian or Canadian dollars? Then pick it below."
   :"The quote names no currency, only lakh/crore. In Nepali news that almost always means rupees: if the translation fits, pick NPR below."}</div>`:""}
 <div class="row"><div><label>Currency</label><select id="f-currency"><option value="">— confirm —</option>${opts(e.currencies,cur0)}</select></div>
 <div><label>Announced on</label><input id="f-announced_on" value="${esc(it.article.published_at_utc.slice(0,10))}"></div></div>
 <div class="row"><div><label>Stage</label><select id="f-status">${opts(e.status,d.status)}</select></div>
 <div><label>Basis</label><select id="f-amount_basis">${opts(e.basis,d.amount_basis)}</select></div></div>
 <label>Count (per-household/person only; must be in the excerpt: ${esc(d.unit_count_text||"none given")})</label><input id="f-unit_count" value="">
 <label>Donor (as written: ${esc(d.donor_as_written)})</label><input id="f-donor_name" value="${esc(d.donor_name_en)}">
 <div class="row"><div><label>Donor type</label><select id="f-donor_type">${opts(e.donor_type,d.donor_type)}</select></div>
 <div><label>Form</label><select id="f-form">${opts(e.form,d.form)}</select></div></div>
 <label>Where (as written: ${esc(d.place_as_written||"—")})</label>
 <select id="f-geo_level">${opts(["municipality","district","province","corridor","national"],geo)}</select>
 ${placeSelect(it,place0)}
 <div class="row"><div><label>Sector</label><select id="f-sector">${opts(e.sector,d.sector)}</select></div>
 <div><label>Phase</label><select id="f-phase">${opts(e.phase,d.phase)}</select></div></div>
 <label>Same money as an existing entry?</label><select id="f-link">${opts([["","— new money —"]].concat(it.suggest.dupes.flatMap(x=>[["src:"+x.entry_id,"Another source for "+x.label],["stage:"+x.money_id,"Later stage of "+x.label]])),"")}</select>
 <label>Also reported to FTS?</label><select id="f-fts">${opts([["","— no link —"]].concat(it.suggest.fts.map(x=>[x.flow_id,x.label])),"")}</select>
 <div class="actions"><button class="ok" id="approve">Approve <kbd>a</kbd></button>
 <button class="no" id="reject">Reject <kbd>r</kbd>+1–5</button><button id="skip">Skip <kbd>→</kbd></button></div>
 <div id="reasons" class="mut ${rejectMode?"":"hidden"}">${Object.entries(S.reasons.ledger).map(([k,v])=>`<kbd>${k}</kbd> ${esc(v)}`).join(" · ")}</div>
 </div></div>`;
 if(S.dup_groups.length){$("main").firstElementChild.insertAdjacentHTML("afterbegin",groupBanner());bindGroupBanner()}
 $("approve").onclick=approveLedger;$("reject").onclick=()=>{rejectMode=true;render()};$("skip").onclick=()=>{cur++;render()}}
function fields(){const f={};["currency","announced_on","status","amount_basis","unit_count","donor_name","donor_type","form","geo_level","place_id","sector","phase"].forEach(k=>f[k]=$("f-"+k).value);return f}
async function approveLedger(){const it=items()[cur];const link=$("f-link").value;
 if(!link.startsWith("src:")&&!$("f-currency").value){$("f-currency").classList.add("bad");$("f-currency").focus();flash("pick the currency first",true);return}
 if(!link.startsWith("src:")&&["municipality","district","province"].includes($("f-geo_level").value)&&!$("f-place_id").value){
  $("f-place_id").classList.add("bad");$("f-place_id").focus();flash("choose the place from the list (or set Where to corridor/national if no single place is named)",true);return}
 const body={item_id:it.item_id,fields:fields(),fts_flow_id:$("f-fts").value};
 if(link.startsWith("src:")){body.action="add_source";body.entry_id=link.slice(4)}else{body.action="approve";if(link.startsWith("stage:"))body.money_id=link.slice(6)}
 const r=await post("/api/ledger",body);flash(`${r.decision} ${r.entry_id}`);await load()}
async function reject(k){const it=items()[cur];const reasons=S.reasons[tab];if(!reasons[k])return;
 const r=await post(tab==="ledger"?"/api/ledger":"/api/org",{item_id:it.item_id,action:"reject",reason:k});rejectMode=false;flash("rejected: "+r.reason);await load()}
function orgNav(){const n=items().length,r=S.org_records,k=r.filter(o=>o.verified_on).length;
 return `<div class="actions" style="margin:0 0 10px"><button data-ov="sightings" ${orgView==="sightings"&&n?'class="ok"':""}>Sightings (${n})</button>
 <button data-ov="records" ${orgView==="records"||!n?'class="ok"':""}>Organisation records (${r.length} · ${k} links checked)</button></div>`}
function bindOrgNav(){document.querySelectorAll("button[data-ov]").forEach(b=>b.onclick=()=>{orgView=b.dataset.ov;render()})}
function linkStatus(o){if(o.link_status==="dead")return `<span class="warn">dead link</span>`;
 if(o.verified_on)return `✓ checked ${esc(o.verified_on)}`;return o.donate_url?`<span class="warn">entered, not checked</span>`:`<span class="mut">none yet</span>`}
function suggestionHTML(list){if(!list.length)return "";
 return `<div class="look"><b>Suggested — not checked.</b> Open each link yourself first. “Use this” only fills the form; it saves nothing and ticks nothing.
 ${list.map((g,i)=>`<div style="margin-top:8px">${g.website?`Website: <a href="${esc(g.website)}" target="_blank" rel="noopener">${esc(g.website)}</a><br>`:""}
 ${g.donate_url?`Donation page: <a href="${esc(g.donate_url)}" target="_blank" rel="noopener">${esc(g.donate_url)}</a>`:"No donation page found"}
 <div class="mut">${g.found_on?`Found on ${esc(g.found_on)} · `:""}${esc(g.note||"")}${g.suggested_on?` · suggested ${esc(g.suggested_on)}`:""}</div>
 ${g.problems&&g.problems.length?`<div class="warn">Would be refused: ${esc(g.problems.join("; "))}</div>`:""}
 <button data-use="${i}">Use this</button></div>`).join("")}</div>`}
function bindSuggestions(list){document.querySelectorAll("button[data-use]").forEach(b=>b.onclick=()=>{const g=list[+b.dataset.use];
 if(g.website)$("o-website").value=g.website;if(g.donate_url)$("o-donate_url").value=g.donate_url;$("o-confirm").checked=false;
 flash("filled in — open the link, then tick the box yourself before saving")})}
function renderRecords(){const recs=S.org_records,fresh=S.new_org_suggestions;
 const sel=recs.find(o=>o.org_id===recSel),newSug=typeof recSel==="string"&&recSel.startsWith("new:")?fresh[+recSel.slice(4)]:null;
 const rows=recs.map(o=>`<tr data-o="${esc(o.org_id)}" style="cursor:pointer${o.org_id===recSel?";background:var(--hl)":""}"><td><b>${esc(o.name_en)}</b><div class="mut">${esc(o.type)}${o.national_fallback==="yes"?" · national list":""}</div></td>
 <td>${linkStatus(o)}</td><td class="mut">${esc(o.places.join("; ")||"—")}</td><td class="mut">${o.suggestions.length?"suggestion ready":""}</td></tr>`).join("");
 const freshRows=fresh.map((g,i)=>`<tr data-n="${i}" style="cursor:pointer${recSel==="new:"+i?";background:var(--hl)":""}"><td><b>${esc(g.name_en)}</b><div class="mut">${esc(g.type||"")}</div></td><td class="mut">${esc(g.note||"")}</td></tr>`).join("");
 const form=sel?`<div class="big">${esc(sel.name_en)}</div>${suggestionHTML(sel.suggestions)}${orgForm(sel)}`
  :newSug?`<div class="big">New: ${esc(newSug.name_en)}</div>${suggestionHTML([newSug])}${orgForm({name_en:newSug.name_en,name_ne:newSug.name_ne,type:newSug.type,registration:newSug.registration})}`
  :recSel==="new"?`<div class="big">New organisation</div>${orgForm({})}`:`<p class="mut">Choose an organisation on the left, or add one.</p>`;
 $("main").innerHTML=`<div><div class="card">${orgNav()}<h2>Organisation records</h2>
 <p class="mut">The map lists an organisation under a place once you accept a sighting there. Its donation link appears only after you open the page yourself and tick the box.</p>
 <table><thead><tr><th>Organisation</th><th>Donation link</th><th>Listed in</th><th></th></tr></thead><tbody>${rows||`<tr><td colspan="4" class="mut">No records yet.</td></tr>`}</tbody></table>
 <div class="actions"><button id="new-org">Add an organisation</button></div></div>
 ${freshRows?`<div class="card"><h2>Suggested organisations (not added)</h2><p class="mut">Choosing one opens a form with its name filled in. Nothing is added until you save.</p><table><tbody>${freshRows}</tbody></table></div>`:""}</div>
 <div><div class="card">${form}</div></div>`;
 bindOrgNav();document.querySelectorAll("tr[data-o]").forEach(t=>t.onclick=()=>{recSel=t.dataset.o;render()});
 document.querySelectorAll("tr[data-n]").forEach(t=>t.onclick=()=>{recSel="new:"+t.dataset.n;render()});
 $("new-org").onclick=()=>{recSel="new";render()};
 if($("save-org"))$("save-org").onclick=saveOrg;bindSuggestions(sel?sel.suggestions:newSug?[newSug]:[])}
function renderOrg(it){const d=it.draft;const place0=(it.place_options[0]||{}).id||"";
 const guess=S.orgs.find(o=>o.name_en.toLowerCase()===d.org_name_en.toLowerCase());
 $("main").innerHTML=article(it)+`<div><div class="card">${orgNav()}<div class="big">${esc(d.org_name_en)}</div>
 <div class="mut">as written: ${esc(d.org_as_written)} · ${esc(d.org_type)} · ${esc(d.activity)}</div>
 <label>Place (as written: ${esc(d.place_as_written)})</label>${placeSelect(it,place0)}
 <label>Organisation record</label><select id="f-org">${opts([["","— choose or create below —"]].concat(S.orgs.map(o=>[o.org_id,o.name_en+(o.verified_on?" ✓ link verified":" (link not verified)")])),guess?guess.org_id:"")}</select>
 <details ${guess?"":"open"}><summary class="mut">Create / edit the organisation record</summary>${orgForm(guess||{name_en:d.org_name_en,type:d.org_type})}</details>
 <div class="actions"><button class="ok" id="approve">Accept listing <kbd>a</kbd></button><button class="no" id="reject">Reject <kbd>r</kbd>+1–5</button><button id="skip">Skip <kbd>→</kbd></button></div>
 <div id="reasons" class="mut ${rejectMode?"":"hidden"}">${Object.entries(S.reasons.org).map(([k,v])=>`<kbd>${k}</kbd> ${esc(v)}`).join(" · ")}</div></div></div>`;
 $("approve").onclick=async()=>{const r=await post("/api/org",{item_id:it.item_id,action:"approve",org_id:$("f-org").value,place_id:$("f-place_id").value});flash("listed under "+r.entry_id);await load()};
 $("reject").onclick=()=>{rejectMode=true;render()};$("skip").onclick=()=>{cur++;render()};$("save-org").onclick=saveOrg;bindOrgNav()}
function orgForm(o){return `<input type="hidden" id="o-id" value="${esc(o.org_id||"")}">
 <label>Name (English)</label><input id="o-name_en" value="${esc(o.name_en||"")}"><label>Name (Nepali)</label><input id="o-name_ne" value="${esc(o.name_ne||"")}">
 <div class="row"><div><label>Type</label><select id="o-type">${opts(S.enums.org_type,o.type)}</select></div><div><label>Registration</label><input id="o-registration" value="${esc(o.registration||"")}" placeholder="SWC no. / UK charity no. / EIN / UN agency"></div></div>
 <label>Official website</label><input id="o-website" value="${esc(o.website||"")}" placeholder="https://...">
 <label>Donation page (type it yourself after opening it)</label><input id="o-donate_url" value="${esc(o.donate_url||"")}" placeholder="https://... on the organisation's own or an official domain">
 <label><input type="checkbox" id="o-confirm" style="width:auto"> I opened this link myself and it is the organisation's official donation page</label>
 <label><input type="checkbox" id="o-national" style="width:auto" ${o.national_fallback==="yes"?"checked":""}> Show in the national fallback list</label>
 <div class="actions"><button id="save-org">Save organisation record</button></div>`}
async function saveOrg(){const b={org_id:$("o-id").value,name_en:$("o-name_en").value,name_ne:$("o-name_ne").value,type:$("o-type").value,
 registration:$("o-registration").value,website:$("o-website").value,donate_url:$("o-donate_url").value,confirm_verified:$("o-confirm").checked,
 national_fallback:$("o-national").checked?"yes":"no"};const r=await post("/api/orgrecord",b);flash("saved "+r.org_id+(r.verified_on?" (link verified "+r.verified_on+")":""));
 if(orgView==="records"||!items().length)recSel=r.org_id;await load()}
function renderCheck(){const list=S.checks;cur=Math.min(cur,Math.max(0,list.length-1));
 if(!list.length){$("main").innerHTML=`<div class="card">No place or repeat suggestions left to check.</div>`;return}
 const c=list[cur],e=c.entry,g=c.suggestion,t=c.translation,a=c.source;
 const what=c.kind==="remove"?`<b>A total that double-counts.</b> This amount is the total of ${c.parts.map(esc).join("; ")} — already entries. Accepting removes this total, so the money is counted once.`
  :c.kind==="stage"?`<b>Same money, another stage.</b> Same as ${esc(c.target.label)}. Accepting links them as one stream of money (counted once, at its firmest stage); both entries stay.`
  :c.kind==="repeat"?`<b>Repeat of an earlier entry.</b> Same gift as ${esc(c.target.label)}. Accepting merges this article into that entry as another source, so the money is counted once.`
  :c.kind==="each"?`<b>Split:</b> the amount goes to EACH of: ${esc(c.suggested)}. Accepting makes one entry per place.`
  :`<b>Suggested:</b> ${esc(c.suggested)}`;
 $("main").innerHTML=`<div class="card"><h2>${esc(a.title)}</h2>
 <div class="mut">${esc(a.outlet)} · ${esc(a.published_at)} · <a href="${esc(a.url)}" target="_blank" rel="noopener">open article</a> · check ${cur+1} of ${list.length}</div>
 <label>Quote in context</label><div class="ctx">${esc(c.context.before)}<mark>${esc(c.context.match)}</mark>${esc(c.context.after)}</div>
 ${t?`<div class="tr"><label>English (machine translation)</label><div><b>Quote:</b> ${esc(t.excerpt_en)}</div></div>`:""}</div>
 <div><div class="card"><div class="big">${esc(e.amount_text)}</div>
 <div class="mut">${esc(e.entry_id)} · ${esc(e.donor_name)} · ${esc(e.announced_on)}${e.recipient_as_written?" · to "+esc(e.recipient_as_written):""}</div>
 <label>Currently</label><div>${esc(c.current)}</div>
 <label>Check (${esc(g.model)})</label><div class="look">${what}<div class="mut">Why: ${esc(g.reason)}</div></div>
 <div class="actions"><button class="ok" id="approve">Accept <kbd>a</kbd></button><button id="keep">Keep current <kbd>k</kbd></button><button id="skip">Skip <kbd>→</kbd></button></div>
 <div ${["remove","stage","repeat"].includes(c.kind)?'class="hidden"':""}><label>Or set the place yourself</label><select id="c-level">${opts(["municipality","district","province","corridor","national"],e.geo_level)}</select>
 <select id="f-place_id"><option value="">— choose —</option>${opts(S.places.map(p=>[p.id,p.label]),e.place_id)}</select>
 <div class="actions"><button id="custom">Save my choice</button></div></div></div></div>`;
 const send=async b=>{const r=await post("/api/check",Object.assign({check_id:c.check_id},b));flash(r.decision+" "+r.entry_id);await load()};
 $("approve").onclick=()=>send({action:"accept"});$("keep").onclick=()=>send({action:"keep"});
 $("custom").onclick=()=>send({action:"custom",geo_level:$("c-level").value,place_id:$("f-place_id").value});
 $("skip").onclick=()=>{cur++;render()}}
function renderScope(){$("main").innerHTML=`<div class="card" style="grid-column:1/-1"><h2>FTS flows outside the appeal</h2>
 <p class="mut">Is each flow for THIS flood's response? Out-of-scope flows never appear on the site. Every decision needs a reason.</p>
 <table><tr><th>Flow</th><th>Date</th><th>USD</th><th>From → to</th><th>Description</th><th>Decision</th></tr>
 ${S.flows.map(f=>{const s=S.scope[f.flow_id];return `<tr><td>${esc(f.flow_id)}</td><td>${esc(f.date)}</td><td>${Number(f.amount_usd||0).toLocaleString()}</td>
 <td>${esc(f.source_org)} → ${esc(f.destination_org)}<div class="mut">${esc(f.status)} · ${esc(f.keywords)}</div></td><td>${esc(f.description)}</td>
 <td>${s?`<b>${esc(s.in_scope)}</b> — ${esc(s.reason)} <div class="mut">${esc(s.decided_on)}</div>`:""}
 <input id="why-${esc(f.flow_id)}" placeholder="reason"><button data-f="${esc(f.flow_id)}" data-v="yes">In scope</button> <button data-f="${esc(f.flow_id)}" data-v="no">Out</button></td></tr>`}).join("")}</table></div>`;
 document.querySelectorAll("button[data-f]").forEach(b=>b.onclick=async()=>{await post("/api/scope",{flow_id:b.dataset.f,in_scope:b.dataset.v,reason:$("why-"+b.dataset.f).value});flash("saved");await load()})}
document.querySelectorAll(".tabs button").forEach(b=>b.onclick=()=>{tab=b.dataset.tab;cur=0;rejectMode=false;render()});
document.addEventListener("keydown",e=>{if(["INPUT","SELECT","TEXTAREA"].includes(e.target.tagName)||tab==="scope")return;
 if(groupView||(tab==="org"&&(orgView==="records"||!items().length)))return;   // no shortcuts for items not on screen
 if(tab==="check"&&e.key==="k"){$("keep")&&$("keep").click();return}
 if(rejectMode&&e.key>="1"&&e.key<="5"){reject(e.key);return}
 if(e.key==="a")$("approve")&&$("approve").click();else if(e.key==="r"){rejectMode=true;render()}
 else if(e.key==="Escape"){rejectMode=false;render()}else if(e.key==="ArrowRight"){cur++;rejectMode=false;render()}
 else if(e.key==="ArrowLeft"){cur=Math.max(0,cur-1);rejectMode=false;render()}});
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    store: Store = None

    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._json(200, self.store.state())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        routes = {"/api/ledger": self.store.decide_ledger, "/api/org": self.store.decide_org,
                  "/api/orgrecord": self.store.save_org_record,
                  "/api/rejectgroup": self.store.reject_group,
                  "/api/scope": self.store.decide_scope, "/api/check": self.store.decide_check}
        if self.path not in routes:
            return self._json(404, {"error": "not found"})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self._json(200, routes[self.path](data))
        except Exception as exc:   # show the reason in the page, not a stack trace
            self._json(400, {"error": str(exc)})

    def log_message(self, *args):
        pass


def backup() -> None:
    stamp = datetime.now(NPT).strftime("%Y%m%dT%H%M%S")
    dest = REVIEW_DIR / "backups" / stamp
    for path in (ledger.LEDGER_JSONL, REVIEW_LOG, orgs.ORGS_CSV, orgs.ORG_AREAS, SCOPE_CSV,
                 JOURNAL):
        if path.exists():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest / path.name)
    print(f"startup backup: {dest.relative_to(REVIEW_DIR.parent.parent)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    backup()
    Handler.store = Store()
    st = Handler.store.state()["stats"]
    print(f"{st['pending']} items to review; {st['ledger_entries']} ledger entries so far")
    url = f"http://localhost:{args.port}"
    server = ThreadingHTTPServer(("localhost", args.port), Handler)
    print(f"reviewing at {url}  (Ctrl+C to stop; every decision is saved immediately)")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped - every decision is already saved")


if __name__ == "__main__":
    main()
