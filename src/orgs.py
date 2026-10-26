"""'Where to donate': which organisations to list for a place, and which links are safe.

Donation scams rise after every disaster, so the rules are strict:
  * A listing needs EVIDENCE that the organisation works in that place (a
    reviewed news excerpt or the organisation's own report), with a date.
    Once Jack approves the evidence, the organisation is listed for that
    place ("working here"), with or without a donation link.
  * A link is shown only after Jack has opened it and confirmed it
    (verified_on in data/orgs/orgs.csv), and only if it is https, on the
    organisation's own domain or an official one (gov.np, UN agencies,
    the Red Cross), and is not a crowdfunding page, a social-media post, a
    messaging link, a link shortener, or an image (QR codes).
  * A link the weekly check (check_links.py) finds dead is hidden.
  * Evidence older than 12 weeks shows "last seen active", not "active".
  * A place with no evidence of its own shows its municipality's or
    district's list, labelled as such; while no listed organisation has a
    checked link, a clearly labelled national block is shown as well.
  * No ranking: most recent evidence first, with the organisation type.

Data (append/correct-only, like the ledger):
  data/orgs/orgs.csv          one row per organisation
  data/orgs/org_areas.jsonl   one row per (organisation, place) with its evidence
"""

import csv
import json
from datetime import date, timedelta
from urllib.parse import urlsplit

from config import ORGS_DIR

ORGS_CSV = ORGS_DIR / "orgs.csv"
ORG_AREAS = ORGS_DIR / "org_areas.jsonl"
ORG_FIELDS = ["org_id", "name_en", "name_ne", "type", "registration", "website",
              "donate_url", "donate_domain", "verified_on", "link_status",
              "link_checked_on", "national_fallback", "notes"]

DISCLAIMER = ("We don't collect or handle donations. A listing shows evidence of activity "
              "here, not an endorsement or audit — check registration before giving.")
FALLBACK_LABEL = "National response — not specific to this place"
FRESH_WEEKS = 12
MAX_EXCERPT_WORDS = 25

# Official domains an organisation's donation page may sit on even when it
# is not the organisation's own website domain.
OFFICIAL_SUFFIXES = ("gov.np", "un.org", "unicef.org", "wfp.org", "who.int", "undp.org",
                     "unfpa.org", "unhcr.org", "iom.int", "ifrc.org", "icrc.org", "nrcs.org")
# Never acceptable, whatever the organisation says.
BLOCKED_DOMAINS = ("gofundme.com", "gogetfunding.com", "ketto.org", "milaap.org",
                   "justgiving.com", "crowdfunder.co.uk", "givebutter.com", "fundly.com",
                   "facebook.com", "fb.com", "fb.me", "instagram.com", "x.com", "twitter.com",
                   "tiktok.com", "youtube.com", "linkedin.com", "wa.me", "whatsapp.com",
                   "t.me", "telegram.me", "viber.com", "bit.ly", "tinyurl.com", "goo.gl",
                   "ow.ly", "rb.gy", "cutt.ly", "paypal.me", "esewa.com.np", "khalti.com")
IMAGE_ENDINGS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _on_domain(host: str, domain: str) -> bool:
    """host is domain itself or a subdomain of it (www.nrcs.org on nrcs.org)."""
    return host == domain or host.endswith("." + domain)


def domain_problems(org: dict) -> list:
    """Is the organisation's approved donate_domain its own or an official one?"""
    domain = (org.get("donate_domain") or "").lower()
    if not domain:
        return ["no approved donation domain"]
    if any(_on_domain(domain, b) for b in BLOCKED_DOMAINS):
        return [f"{domain} is a crowdfunding, social, messaging, payment-app or short-link domain"]
    own = _host(org.get("website") or "")
    if own and (_on_domain(own, domain) or _on_domain(domain, own.removeprefix("www."))):
        return []
    if any(_on_domain(domain, s) for s in OFFICIAL_SUFFIXES):
        return []
    return [f"{domain} is neither the organisation's own website domain nor an official one"]


def url_problems(url: str, org: dict) -> list:
    """Every reason a donate link may not be shown (empty list = acceptable)."""
    problems = []
    parts = urlsplit(url or "")
    host = _host(url or "")
    if parts.scheme != "https":
        problems.append("not https")
    if not host:
        problems.append("no host")
    if any(_on_domain(host, b) for b in BLOCKED_DOMAINS):
        problems.append(f"{host} is a crowdfunding, social, messaging or short-link site")
    if parts.path.lower().endswith(IMAGE_ENDINGS):
        problems.append("links to an image (QR codes are never shown)")
    domain = (org.get("donate_domain") or "").lower()
    if not domain or not _on_domain(host, domain):
        problems.append(f"{host} is not on the organisation's approved domain {domain!r}")
    return problems + domain_problems(org)


def listing_problems(listing: dict) -> list:
    """Every reason an approved listing may not be shown at all (its evidence)."""
    problems = []
    ev = listing.get("evidence") or {}
    for key in ("url", "date", "excerpt"):
        if not ev.get(key):
            problems.append(f"evidence has no {key}")
    # The review app trims long quotes to 25 words and marks the cut with "…",
    # which is not a word of the quote.
    if len([w for w in (ev.get("excerpt") or "").split() if w != "…"]) > MAX_EXCERPT_WORDS:
        problems.append(f"evidence excerpt is over {MAX_EXCERPT_WORDS} words")
    return problems


def link_problems(org: dict) -> list:
    """Every reason an organisation's donation link may not be shown."""
    problems = []
    if not org.get("verified_on"):
        problems.append("donation link not yet verified by hand")
    if org.get("link_status") == "dead":
        problems.append("the weekly link check found the page dead")
    return problems + url_problems(org.get("donate_url") or "", org)


def _shown(org: dict) -> bool:
    return not link_problems(org)


def _item(org: dict, listing: dict = None, as_of: date = None) -> dict:
    item = {"org_id": org["org_id"], "name_en": org["name_en"],
            "name_ne": org.get("name_ne", ""), "type": org["type"],
            "registration": org.get("registration", "")}
    if _shown(org):   # the link only after Jack has opened and confirmed it
        item.update(donate_url=org["donate_url"], verified_on=org["verified_on"])
    if listing:
        ev = listing["evidence"]
        old = date.fromisoformat(ev["date"]) < as_of - timedelta(weeks=FRESH_WEEKS)
        activity = " ".join((listing.get("activity") or "").split()[:MAX_EXCERPT_WORDS])
        item.update(place_id=listing["place_id"], activity=activity,
                    evidence_url=ev["url"], evidence_date=ev["date"],
                    freshness="last_seen" if old else "active")
    return item


def donate_block(place_id: str, listings: list, orgs_by_id: dict, as_of: date,
                 chains: dict = None) -> dict:
    """What a place's list of organisations shows on a given date.

    Two tiers:
      working   organisations with reviewed evidence of working in the place,
                listed as soon as Jack approves the evidence; each carries a
                donate link only once Jack has checked it.
      national  the labelled national block (organisations with checked
                links marked national_fallback), shown when nothing in
                'working' has a link.

    chains maps each place to itself and the larger areas it sits in
    (town -> municipality -> district), like the money roll-up: evidence in
    a town also counts for its municipality and district, never the other
    way. A place with no evidence of its own shows its municipality's or
    district's list, labelled as such (working_scope).
    """
    chains = chains or {}

    def chain(pid):
        return chains.get(pid) or [pid]

    usable = [l for l in listings if l["org_id"] in orgs_by_id and not listing_problems(l)
              and l["evidence"]["date"] <= as_of.isoformat()]

    def working_in(area):
        best = {}
        for l in usable:
            if area not in chain(l["place_id"]):
                continue
            if l["org_id"] not in best or l["evidence"]["date"] > best[l["org_id"]]["evidence"]["date"]:
                best[l["org_id"]] = l
        items = [_item(orgs_by_id[oid], l, as_of) for oid, l in best.items()]
        items.sort(key=lambda i: (i["evidence_date"], i["org_id"]), reverse=True)   # recency, not ranking
        return items

    scope, area, working = "none", None, []
    for n, a in enumerate(chain(place_id)):
        working = working_in(a)
        if working:
            scope, area = ("here" if n == 0 else "municipality" if len(a) == 9 else "district"), a
            break
    national = [_item(o) for o in orgs_by_id.values()
                if o.get("national_fallback") == "yes" and _shown(o)]
    has_link = any(i.get("donate_url") for i in working)
    return {"working_scope": scope, "working_area": area, "working": working,
            "national_label": FALLBACK_LABEL,
            "national": [] if has_link else sorted(national, key=lambda i: i["name_en"]),
            "disclaimer": DISCLAIMER}


def load_orgs() -> dict:
    if not ORGS_CSV.exists():
        return {}
    with open(ORGS_CSV, newline="", encoding="utf-8") as f:
        return {r["org_id"]: r for r in csv.DictReader(f)}


def save_orgs(rows: dict) -> None:
    ORGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(ORGS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ORG_FIELDS)
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: r["org_id"]))


def load_areas() -> list:
    if not ORG_AREAS.exists():
        return []
    return [json.loads(l) for l in ORG_AREAS.read_text(encoding="utf-8").splitlines() if l]
