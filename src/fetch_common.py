"""Shared download helpers used by every fetch_*.py and build_*.py script.

Three habits keep the data honest and reproducible:
  1. Every request is polite (a User-Agent that says who we are, a pause
     between requests) and retried with backoff on network hiccups.
  2. Every download is saved verbatim under data/raw/ (gitignored) before
     anything is parsed, so a parsing fix never needs a re-fetch and the
     snapshot that was used stays fixed.
  3. Every fetch run appends one line to data/manifests/<source>.jsonl
     (committed): what was fetched, when, from where, and the SHA-256 of
     each raw file. The published week files cite these hashes.
"""

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from config import MANIFESTS, RAW, REPO_ROOT, USER_AGENT

SESSION = requests.Session()
SESSION.headers["User-Agent"] = USER_AGENT

# Seconds to wait between requests to the same site.
POLITE_DELAY = 1.0
_last_request = 0.0


def _worth_retrying(exc: BaseException) -> bool:
    """Network errors and 5xx/429 are retried; a 404 or 403 is final."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return isinstance(exc, requests.RequestException)


@retry(stop=stop_after_attempt(4),
       wait=wait_exponential(multiplier=2, min=2, max=30),
       retry=retry_if_exception(_worth_retrying),
       reraise=True)
def get(url: str, params=None, timeout: int = 60, **kwargs) -> requests.Response:
    """GET with a polite pause, retries, and an error on any non-2xx status."""
    global _last_request
    wait = POLITE_DELAY - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()
    resp = SESSION.get(url, params=params, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp(when: datetime = None) -> str:
    return (when or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_raw(relative_path: str, data: bytes) -> Path:
    """Write bytes verbatim to data/raw/<relative_path>; return the path."""
    path = RAW / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def raw_record(path: Path) -> dict:
    """{file, sha256, bytes} for a raw file, with a repo-relative path."""
    data = path.read_bytes()
    return {"file": str(path.relative_to(REPO_ROOT)),
            "sha256": sha256_hex(data), "bytes": len(data)}


def append_manifest(source: str, record: dict) -> dict:
    """Append one fetch run to data/manifests/<source>.jsonl.

    The run gets a short id (the first 16 hex characters of the SHA-256 of
    its own JSON), which week files cite to say which fetches they used.
    """
    MANIFESTS.mkdir(parents=True, exist_ok=True)
    record = {"source": source, **record}
    body = json.dumps(record, ensure_ascii=False, sort_keys=True)
    record["run_id"] = sha256_hex(body.encode("utf-8"))[:16]
    with open(MANIFESTS / f"{source}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def hdx_resources(package_id: str) -> list:
    """The resource list of an HDX dataset (name, url, last_modified, size)."""
    from config import HDX_API
    body = get(HDX_API, params={"id": package_id}).json()
    if not body.get("success"):
        raise RuntimeError(f"HDX package_show failed for {package_id}")
    return body["result"]["resources"]
