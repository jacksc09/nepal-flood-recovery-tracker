"""Check that no published week file has changed since it was published.

Every publish appends the file's SHA-256 to data/manifests/published.jsonl
(committed, append-only). This script recomputes every published file's
hash and compares it with that log and with docs/data/weeks/index.json, so
a rewritten week is caught even if someone also rewrote the index. Git
history is the second witness.

Run from the repo root:  .venv/bin/python src/verify_published.py
"""

import hashlib
import json
import sys

from config import FIRST_WEEK, MANIFESTS, WEEKS_DIR
from weeks import next_week

PUBLISHED_LOG = MANIFESTS / "published.jsonl"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def problems() -> list:
    out = []
    log = []
    if PUBLISHED_LOG.exists():
        log = [json.loads(l) for l in PUBLISHED_LOG.read_text().splitlines() if l]
    index_path = WEEKS_DIR / "index.json"
    index = json.loads(index_path.read_text())["weeks"] if index_path.exists() else []
    expected = FIRST_WEEK
    for row in log:
        if row["week"] != expected:
            out.append(f"published log jumps to {row['week']} where {expected} was expected")
        expected = next_week(row["week"])
        path = WEEKS_DIR / f"{row['week']}.json"
        if not path.exists():
            out.append(f"{path.name} is in the published log but missing")
            continue
        body = path.read_bytes()
        if sha256_hex(body) != row["sha256"]:
            out.append(f"{path.name} has changed since it was published")
        if json.loads(body)["meta"]["week"] != row["week"]:
            out.append(f"{path.name} says it is week {json.loads(body)['meta']['week']}")
    logged = {r["week"] for r in log}
    for entry in index:
        path = WEEKS_DIR / entry["file"]
        if entry["week"] not in logged:
            out.append(f"index lists {entry['week']}, which was never published")
        elif path.exists() and sha256_hex(path.read_bytes()) != entry["sha256"]:
            out.append(f"index hash for {entry['week']} does not match the file")
    for path in WEEKS_DIR.glob("*.json"):
        if path.name != "index.json" and path.stem not in logged:
            out.append(f"{path.name} is not in the published log")
    return out


def main() -> None:
    found = problems()
    n = len([l for l in PUBLISHED_LOG.read_text().splitlines() if l]) if PUBLISHED_LOG.exists() else 0
    if found:
        print("PUBLISHED FILES DO NOT VERIFY:")
        for p in found:
            print(f"  {p}")
        sys.exit(1)
    print(f"verified {n} published week file(s): unchanged since publication")


if __name__ == "__main__":
    main()
