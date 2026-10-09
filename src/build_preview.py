"""Build a local preview of every week for the map, without publishing anything.

Writes docs/data/preview/<week>.json and docs/data/preview/index.json
(gitignored: a preview is never committed or deployed). The map loads the
published files in docs/data/weeks/ when they exist and falls back to the
preview, with a banner saying so.

A preview uses exactly the same assembly as publishing (weekfile.py), so
the ledger holds only entries Jack has approved in review_ui.py; drafts
that have not been reviewed never appear.

Run from the repo root:  .venv/bin/python src/build_preview.py
Then:                    preview "map" (localhost:8040)
"""

import hashlib
import json

from config import DOCS, FIRST_WEEK, NPT
from fetch_common import utc_now
from weekfile import build_week, summary, write_places
from weeks import week_of, weeks_between

PREVIEW_DIR = DOCS / "data" / "preview"


def main() -> None:
    built_at = utc_now()
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    n_places = write_places()
    index = []
    for week in weeks_between(FIRST_WEEK, week_of(built_at)):
        data = build_week(week, built_at, preview=True)
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        (PREVIEW_DIR / f"{week}.json").write_bytes(body)
        index.append({"week": week, "file": f"{week}.json", "kind": data["meta"]["kind"],
                      "open_week": data["meta"]["open_week"], "preview": True,
                      "built_at": data["meta"]["built_at"],
                      "sha256": hashlib.sha256(body).hexdigest(),
                      "bytes": len(body), "summary": summary(data)})
        s = index[-1]["summary"]
        print(f"  {week} {data['meta']['kind']:<13} {len(body) / 1024:>5.0f} KB  "
              f"FTS ${s['fts_funded_usd'] / 1e6:5.2f}M  ledger entries {s['ledger_entries']:>3}  "
              f"articles {s['articles_week']['en']:>4} en / {s['articles_week']['ne']:>4} ne  "
              f"homes destroyed {s['homes_destroyed']}")
    (PREVIEW_DIR / "index.json").write_text(json.dumps(
        {"preview": True, "built_at": index[-1]["built_at"], "weeks": index},
        ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"preview of {len(index)} weeks and {n_places} places written "
          f"(built {built_at.astimezone(NPT):%Y-%m-%d %H:%M} NPT); not for publishing")


if __name__ == "__main__":
    main()
