"""Fetch English and Nepali news about the flood corridor for a date window.

Three kinds of source (all keyless):
  * WordPress REST search on OnlineKhabar (Nepali and English) and
    english.nepalnews.com. These can be asked for any past date range, so
    they power the reconstructed weeks. Each corridor place name is
    searched separately (config.SEARCH_TERMS); the full article text comes
    with the result.
  * Outlet RSS feeds (Kathmandu Post, Himalayan Times, Rising Nepal,
    Setopati): recent items only. An item is kept only if its headline or
    summary names a corridor place or the flood; then its page is fetched
    for the text.
  * Google News RSS (English only): headlines and links only. Its links are
    opaque redirects, stored exactly as given; these items are never used
    as evidence for the ledger.

Raw responses go to data/raw/news/<UTC stamp>/ and article texts to
data/raw/articles/ (both gitignored: the texts are the publishers'
copyright). Metadata goes to data/processed/articles.csv (committed).

Run from the repo root:
  .venv/bin/python src/fetch_news.py --week 2026-W35      # one ISO week
  .venv/bin/python src/fetch_news.py --since 2026-10-01 --until 2026-10-07
  .venv/bin/python src/fetch_news.py                      # since the last run, minus 3 days
"""

import argparse
import json
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

import articles
from config import (FLOOD_TERMS, GNEWS_QUERIES, GNEWS_RSS, MANIFESTS, NPT, RAW,
                    RSS_FEEDS, SEARCH_TERMS, WP_SITES)
from fetch_common import append_manifest, get, raw_record, sha256_hex, utc_now, utc_stamp
from gazetteer import Gazetteer
from textnorm import norm
from weeks import week_bounds, week_of

OVERLAP_DAYS = 3        # re-read the last few days so late-indexed stories aren't missed
WP_PER_PAGE = 100
GNEWS_CAP = 100         # Google News returns at most 100 items per query


class Run:
    """Collects raw files and counts for one fetch run's manifest line."""

    def __init__(self):
        self.started = utc_now()
        self.dir = RAW / "news" / self.started.strftime("%Y%m%dT%H%M%SZ")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.raw, self.counts, self.warnings = [], {}, []

    def keep(self, name: str, content: bytes) -> None:
        path = self.dir / name
        path.write_bytes(content)
        self.raw.append(raw_record(path))

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_article(store: dict, run: Run, *, url, title, outlet, lang, kind, published,
                precision, text=None, wp_post_id="", found_by="") -> bool:
    """Add (or update) one article; return True if it was new."""
    aid = articles.article_id(url)
    row = store.get(aid)
    is_new = row is None
    if is_new:
        week = week_of(published)
        row = {"article_id": aid, "url": url.strip(), "title": " ".join(title.split()),
               "outlet": outlet, "lang": lang, "source_kind": kind, "wp_post_id": wp_post_id,
               "published_at_utc": (iso_utc(published) if isinstance(published, datetime)
                                    else published.isoformat()),
               "published_week": week, "date_precision": precision,
               "first_fetched_utc": utc_stamp(run.started), "text_available": "no",
               "text_chars": "0", "text_sha256": "", "found_by": found_by}
        store[aid] = row
    elif found_by and found_by not in row["found_by"].split("; "):
        row["found_by"] = "; ".join(filter(None, [row["found_by"], found_by]))
    if text and row["text_available"] == "no":
        if len(text) >= articles.MIN_TEXT_CHARS:
            path = articles.text_path(aid)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            row.update(text_available="yes", text_chars=str(len(text)),
                       text_sha256=sha256_hex(text.encode("utf-8")))
        else:
            run.count(f"short_text:{outlet}")
    return is_new


# --- WordPress search ------------------------------------------------------------
def fetch_wp(store, run, since: date, until: date) -> None:
    # WordPress compares against the site's local (Nepal) time; a day of
    # margin each side is harmless because every article is bucketed by
    # its exact UTC timestamp afterwards.
    after = f"{since - timedelta(days=1)}T00:00:00"
    before = f"{until + timedelta(days=1)}T23:59:59"
    for site in WP_SITES:
        for term in SEARCH_TERMS[site["lang"]]:
            page, pages = 1, 1
            while page <= pages:
                resp = get(f"{site['base']}/wp-json/wp/v2/posts", params={
                    "search": term, "after": after, "before": before,
                    "per_page": WP_PER_PAGE, "page": page,
                    "_fields": "id,date_gmt,link,title,content"})
                pages = int(resp.headers.get("X-WP-TotalPages", 1))
                total = int(resp.headers.get("X-WP-Total", 0))
                run.keep(f"wp_{site['outlet']}_{sha256_hex(term.encode())[:8]}_p{page}.json",
                         resp.content)
                for post in resp.json():
                    published = datetime.fromisoformat(post["date_gmt"]).replace(tzinfo=timezone.utc)
                    title = BeautifulSoup(post["title"]["rendered"], "lxml").get_text()
                    text = articles.paragraphs_from_html(post["content"]["rendered"])
                    if add_article(store, run, url=post["link"], title=title,
                                   outlet=site["outlet"], lang=site["lang"], kind="wp",
                                   published=published, precision="datetime", text=text,
                                   wp_post_id=str(post["id"]), found_by=f"wp:{term}"):
                        run.count(f"new:{site['outlet']}")
                if page == 1:
                    run.count(f"wp_total:{site['outlet']}:{term}", total)
                page += 1


# --- outlet RSS ------------------------------------------------------------------
def looks_relevant(gaz: Gazetteer, text: str, lang: str) -> bool:
    if gaz.match(text):
        return True
    low = norm(text).lower()
    return any(t.lower() in low for t in FLOOD_TERMS[lang])


def rss_date(item, link: str):
    """(datetime or date, precision). The Kathmandu Post has no pubDate:
    its date is in the URL path, /YYYY/MM/DD/, as a Nepal calendar date."""
    pub = item.find("pubDate")
    if pub and pub.get_text(strip=True):
        return parsedate_to_datetime(pub.get_text(strip=True)), "datetime"
    m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", link)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), "date"
    return None, None


def fetch_rss(store, run, gaz, since: date, until: date) -> None:
    for feed in RSS_FEEDS:
        resp = get(feed["url"])
        run.keep(f"rss_{feed['outlet']}.xml", resp.content)
        for item in BeautifulSoup(resp.content, "xml").find_all("item"):
            link = item.find("link").get_text(strip=True)
            title = item.find("title").get_text(strip=True)
            summary = item.find("description").get_text(" ", strip=True) if item.find("description") else ""
            published, precision = rss_date(item, link)
            if published is None:
                run.warnings.append(f"no date for {link}")
                continue
            day = published.astimezone(NPT).date() if isinstance(published, datetime) else published
            if not since <= day <= until or not looks_relevant(gaz, f"{title} {summary}", feed["lang"]):
                continue
            aid = articles.article_id(link)
            text = None
            if aid not in store or store[aid]["text_available"] == "no":
                try:
                    page = get(link)
                    run.keep(f"page_{aid}.html", page.content)
                    text = articles.paragraphs_from_html(page.text, whole_page=True)
                except Exception as exc:   # a dead link is logged, not fatal
                    run.warnings.append(f"could not fetch {link}: {exc}")
            if add_article(store, run, url=link, title=title, outlet=feed["outlet"],
                           lang=feed["lang"], kind="rss", published=published,
                           precision=precision, text=text, found_by="rss"):
                run.count(f"new:{feed['outlet']}")


# --- Google News (English, links only) -------------------------------------------
def simple_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", norm(title).lower())


def fetch_gnews(store, run, since: date, until: date) -> None:
    known_titles = {simple_title(r["title"]) for r in store.values() if r["source_kind"] != "gnews"}
    # One query per week of the window keeps each under the 100-item cap.
    start = since
    while start <= until:
        end = min(start + timedelta(days=6), until)
        for q in GNEWS_QUERIES:
            query = f"{q} after:{start - timedelta(days=1)} before:{end + timedelta(days=1)}"
            resp = get(GNEWS_RSS, params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
            run.keep(f"gnews_{start}_{sha256_hex(q.encode())[:8]}.xml", resp.content)
            items = BeautifulSoup(resp.content, "xml").find_all("item")
            if len(items) >= GNEWS_CAP:
                run.warnings.append(f"Google News cap hit for {query!r}: some items missed")
            for item in items:
                source = item.find("source")
                outlet = source.get_text(strip=True) if source else "unknown"
                title = item.find("title").get_text(strip=True)
                if title.endswith(f" - {outlet}"):
                    title = title[: -len(f" - {outlet}")]
                if simple_title(title) in known_titles:
                    run.count("gnews_duplicate_of_fetched")
                    continue
                published = parsedate_to_datetime(item.find("pubDate").get_text(strip=True))
                if not since <= published.astimezone(NPT).date() <= until:
                    continue
                if add_article(store, run, url=item.find("link").get_text(strip=True),
                               title=title, outlet=outlet, lang="en", kind="gnews",
                               published=published, precision="datetime",
                               found_by=f"gnews:{q}"):
                    run.count("new:gnews")
        start = end + timedelta(days=1)


def last_window_end():
    path = MANIFESTS / "news.jsonl"
    if not path.exists():
        return None
    ends = [json.loads(line)["window"]["until"] for line in path.read_text().splitlines()]
    return date.fromisoformat(max(ends)) if ends else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", help="fetch one ISO week, e.g. 2026-W35")
    parser.add_argument("--since", type=date.fromisoformat)
    parser.add_argument("--until", type=date.fromisoformat)
    parser.add_argument("--skip", nargs="*", default=[], choices=["wp", "rss", "gnews"],
                        help="leave out a source kind (e.g. RSS when rebuilding old weeks)")
    args = parser.parse_args()

    today = utc_now().astimezone(NPT).date()
    if args.week:
        start, end = week_bounds(args.week)
        since, until = start.date(), end.date()
    else:
        until = args.until or today
        since = args.since or ((last_window_end() or today) - timedelta(days=OVERLAP_DAYS))
    print(f"window (Nepal dates): {since} to {until}")

    store = articles.load()
    before = len(store)
    run = Run()
    gaz = Gazetteer.load()
    if "wp" not in args.skip:
        fetch_wp(store, run, since, until)
    # RSS only carries the last few days, so it is skipped for old windows.
    if "rss" not in args.skip and until >= today - timedelta(days=2):
        fetch_rss(store, run, gaz, since, until)
    if "gnews" not in args.skip:
        fetch_gnews(store, run, since, until)
    articles.save(store)
    append_manifest("news", {"fetched_at_utc": utc_stamp(run.started),
                             "window": {"since": since.isoformat(), "until": until.isoformat()},
                             "counts": run.counts, "warnings": run.warnings, "raw": run.raw})

    # --- sanity checks ---------------------------------------------------
    print(f"articles in store: {len(store)} ({len(store) - before} new this run)")
    for key, n in sorted(run.counts.items()):
        if key.startswith(("new:", "short_text:", "gnews_dup")):
            print(f"  {key:<34} {n}")
    totals = {k: v for k, v in run.counts.items() if k.startswith("wp_total:")}
    print(f"  WordPress hits reported (X-WP-Total, before merging terms): {sum(totals.values())}")
    in_window = [r for r in store.values()
                 if since.isoformat() <= r["published_at_utc"][:10] <= until.isoformat()]
    by = {}
    for r in in_window:
        k = (r["published_week"], r["lang"], r["text_available"])
        by[k] = by.get(k, 0) + 1
    for (wk, lang, txt), n in sorted(by.items()):
        print(f"  {wk} {lang} text={txt}: {n}")
    for w in run.warnings[:10]:
        print(f"  warning: {w}")


if __name__ == "__main__":
    main()
