"""Shared settings for the whole tracker: dates, places, sources, paths.

Everything that more than one script needs lives here, so a value (the
event date, the corridor districts, the FTS plan id) is written down once.
Nothing in this file talks to the network.
"""

from datetime import date, timedelta, timezone
from pathlib import Path

# --- the event ---------------------------------------------------------------
# 26 Aug 2026: a rock-ice avalanche off Langtang Lirung became a debris flow
# down the Bhote Koshi / Trishuli. Anything dated before this is out of scope.
EVENT_DATE = date(2026, 8, 26)

# The first ISO week of the timeline (Mon 24 Aug - Sun 30 Aug 2026).
FIRST_WEEK = "2026-W35"

# Weeks up to and including this one were rebuilt after the fact from dated
# records ("reconstructed"); later weeks are snapshots taken at the time.
LAST_RECONSTRUCTED_WEEK = "2026-W40"

# Nepal Time is a fixed UTC+5:45 all year (no daylight saving), so a
# constant offset is simpler and safer than a timezone database.
NPT = timezone(timedelta(hours=5, minutes=45), name="NPT")

# A snapshot published more than this many days after its week ended is
# labelled a "late snapshot" on the map.
LATE_SNAPSHOT_DAYS = 7

# --- the corridor --------------------------------------------------------------
# District pcodes from the OCHA COD-AB (HDX cod-ab-npl, v02). COD spells
# Chitwan "Chitawan" and Tanahun "Tanahu".
CORE_DISTRICTS = {
    "NP0329": "Rasuwa",
    "NP0328": "Nuwakot",
    "NP0330": "Dhading",
}
DOWNSTREAM_DISTRICTS = {
    "NP0436": "Gorkha",
    "NP0335": "Chitawan",
    "NP0440": "Tanahu",
    "NP0447": "Nawalparasi East",
    "NP0547": "Nawalparasi West",
}
CORRIDOR_DISTRICTS = {**CORE_DISTRICTS, **DOWNSTREAM_DISTRICTS}

# Pseudo-pcode for things that belong to the whole corridor rather than one
# admin unit (the rivers themselves, "flood-affected districts" in general).
CORRIDOR_PCODE = "CORRIDOR"
NATIONAL_PCODE = "NATIONAL"

# --- sources -------------------------------------------------------------------
USER_AGENT = ("nepal-flood-recovery-tracker/0.1 "
              "(open research dataset; contact via GitHub)")

# OCHA Financial Tracking Service: the 2026 flood flash appeal.
FTS_PLAN_ID = 1637
FTS_API = "https://api.hpc.tools/v1/public"

# Nepal Rastra Bank exchange rates (keyless).
NRB_FOREX_API = "https://www.nrb.org.np/api/forex/v1/rates"

# HDX datasets (CKAN package ids).
HDX_API = "https://data.humdata.org/api/3/action/package_show"
HDX_COD_AB = "cod-ab-npl"
HDX_EMS = "npl-flood-emsr927"

# --- paths ---------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"
RAW = DATA / "raw"                 # gitignored: verbatim downloads, article texts
MANIFESTS = DATA / "manifests"     # committed: what was fetched, with SHA-256
MANUAL = DATA / "manual"           # committed: hand-edited lookups
GAZETTEER = DATA / "gazetteer" / "places.csv"
PROCESSED = DATA / "processed"
LEDGER_DIR = DATA / "ledger"
ORGS_DIR = DATA / "orgs"
REVIEW_DIR = DATA / "review"       # gitignored: queue, journal, backups
DOCS = REPO_ROOT / "docs"          # GitHub Pages root (public)
WEEKS_DIR = DOCS / "data" / "weeks"
FEEDS_DIR = DOCS / "feeds"

# --- news ------------------------------------------------------------------------
# WordPress sites whose REST search can be filtered by date (so past weeks
# can be rebuilt). Each search term is run separately; results are merged.
WP_SITES = [
    {"outlet": "onlinekhabar", "lang": "ne", "base": "https://www.onlinekhabar.com"},
    {"outlet": "onlinekhabar_en", "lang": "en", "base": "https://english.onlinekhabar.com"},
    {"outlet": "nepalnews", "lang": "en", "base": "https://english.nepalnews.com"},
]
SEARCH_TERMS = {
    "en": ["Rasuwa", "Nuwakot", "Trishuli", "Bhote Koshi", "Syabrubesi", "Rasuwagadhi",
           "Dhading"],
    "ne": ["रसुवा", "नुवाकोट", "त्रिशूली", "भोटेकोशी", "स्याफ्रुबेसी", "रसुवागढी", "धादिङ"],
}
# Outlet RSS feeds: recent items only (no way to ask for past weeks).
RSS_FEEDS = [
    {"outlet": "kathmandupost", "lang": "en", "url": "https://kathmandupost.com/rss"},
    {"outlet": "himalayantimes", "lang": "en", "url": "https://thehimalayantimes.com/rssFeed/15"},
    {"outlet": "risingnepal", "lang": "en", "url": "https://risingnepaldaily.com/rss"},
    {"outlet": "setopati", "lang": "ne", "url": "https://www.setopati.com/feed"},
]
# Google News RSS search (English only: there is no Nepali edition). Links
# are opaque redirects, so these items are headlines + links only and can
# never be ledger evidence.
GNEWS_RSS = "https://news.google.com/rss/search"
GNEWS_QUERIES = ["Rasuwa flood", "Trishuli flood", "Rasuwa reconstruction",
                 "Rasuwa relief", "Nuwakot flood", "Rasuwagadhi"]

# Words that mark an article as being about the flood (used with a place
# match; a place alone is not enough).
FLOOD_TERMS = {
    "en": ["flood", "floods", "flooding", "flooded", "debris flow", "debris-flow",
           "glacial", "glacier", "GLOF", "avalanche", "landslide", "disaster"],
    "ne": ["बाढी", "हिमताल", "हिमनदी", "हिमपहिरो", "लेदो", "पहिरो", "विपद्", "प्रकोप"],
}
