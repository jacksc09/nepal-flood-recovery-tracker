"""The article store: one metadata row per news item, plus its stored text.

  data/processed/articles.csv      metadata only (committed): headline, link,
                                   outlet, language, date, text checksum
  data/raw/articles/<id>.txt       the article's full text (gitignored,
                                   because it is the publisher's copyright)

An article's id is the first 12 hex characters of the SHA-1 of its
canonical URL, so the same story found twice (by a search and by an RSS
feed) is stored once.
"""

import csv
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from config import PROCESSED, RAW

ARTICLES_CSV = PROCESSED / "articles.csv"
TEXT_DIR = RAW / "articles"
FIELDS = ["article_id", "url", "title", "outlet", "lang", "source_kind", "wp_post_id",
          "published_at_utc", "published_week", "date_precision", "first_fetched_utc",
          "text_available", "text_chars", "text_sha256", "found_by"]

# Below this many characters an extraction is a teaser or a failure, not text.
MIN_TEXT_CHARS = 300


def canonical_url(url: str) -> str:
    """https, lower-case host, no query/fragment, no trailing slash."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", parts.netloc.lower(), path, "", ""))


def article_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:12]


def text_path(aid: str):
    return TEXT_DIR / f"{aid}.txt"


def read_text(aid: str):
    path = text_path(aid)
    return path.read_text(encoding="utf-8") if path.exists() else None


def paragraphs_from_html(html: str, whole_page: bool = False) -> str:
    """Paragraph text from HTML, one blank line between paragraphs.

    For a WordPress post body (whole_page=False) every <p>, <li> and
    sub-heading is kept. For a full web page, the element whose direct
    <p> children hold the most text is taken as the story body, so menus,
    footers and "related stories" lists are left out.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "aside", "form"]):
        tag.decompose()
    root = soup
    if whole_page:
        best, best_len = None, 0
        for el in soup.find_all(["article", "section", "div", "main"]):
            n = sum(len(p.get_text(strip=True)) for p in el.find_all("p", recursive=False))
            if n > best_len:
                best, best_len = el, n
        if best is None:
            return ""
        root = best
    blocks = [el.get_text(" ", strip=True)
              for el in root.find_all(["p", "li", "h2", "h3", "h4"])]
    return "\n\n".join(re.sub(r"\s+", " ", b) for b in blocks if b)


def load() -> dict:
    """{article_id: row} from articles.csv (empty if there is none yet)."""
    if not ARTICLES_CSV.exists():
        return {}
    with open(ARTICLES_CSV, newline="", encoding="utf-8") as f:
        return {r["article_id"]: r for r in csv.DictReader(f)}


def save(rows: dict) -> None:
    ARTICLES_CSV.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows.values(), key=lambda r: (r["published_at_utc"], r["article_id"]))
    with open(ARTICLES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(ordered)
