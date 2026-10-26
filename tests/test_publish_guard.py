"""Publishing guards: a week is published once, in order, only after it ends."""

import json
from datetime import datetime

import pytest

import publish_week
import verify_published
from config import NPT


def test_refuses_a_week_that_has_not_ended():
    sunday = datetime(2026, 10, 11, 23, 59, 0, tzinfo=NPT)
    problems = publish_week.order_problems("2026-W41", sunday, published=[
        f"2026-W{n}" for n in range(35, 41)])
    assert any("has not ended" in p for p in problems)
    monday = datetime(2026, 10, 12, 0, 0, 1, tzinfo=NPT)
    assert publish_week.order_problems("2026-W41", monday, published=[
        f"2026-W{n}" for n in range(35, 41)]) == []


def test_refuses_to_overwrite_a_published_week():
    now = datetime(2026, 10, 20, tzinfo=NPT)
    problems = publish_week.order_problems("2026-W35", now, published=["2026-W35"])
    assert any("already published" in p for p in problems)


def test_refuses_to_skip_a_week():
    now = datetime(2026, 10, 20, tzinfo=NPT)
    problems = publish_week.order_problems("2026-W37", now, published=["2026-W35"])
    assert any("2026-W36" in p for p in problems)


def test_the_first_week_is_w35():
    now = datetime(2026, 10, 20, tzinfo=NPT)
    assert publish_week.order_problems("2026-W35", now, published=[]) == []
    assert publish_week.order_problems("2026-W36", now, published=[])


def make_published(tmp_path, monkeypatch):
    weeks = tmp_path / "weeks"
    weeks.mkdir()
    log = tmp_path / "published.jsonl"
    monkeypatch.setattr(verify_published, "WEEKS_DIR", weeks)
    monkeypatch.setattr(verify_published, "PUBLISHED_LOG", log)
    body = json.dumps({"meta": {"week": "2026-W35"}}).encode()
    (weeks / "2026-W35.json").write_bytes(body)
    sha = publish_week.sha256_hex(body)
    (weeks / "index.json").write_text(json.dumps({"weeks": [
        {"week": "2026-W35", "file": "2026-W35.json", "sha256": sha}]}))
    log.write_text(json.dumps({"week": "2026-W35", "sha256": sha}) + "\n")
    return weeks


def test_verify_passes_untouched_files(tmp_path, monkeypatch):
    make_published(tmp_path, monkeypatch)
    assert verify_published.problems() == []


def test_verify_catches_a_rewritten_week_even_if_the_index_is_rewritten_too(tmp_path, monkeypatch):
    weeks = make_published(tmp_path, monkeypatch)
    body = json.dumps({"meta": {"week": "2026-W35"}, "edited": True}).encode()
    (weeks / "2026-W35.json").write_bytes(body)
    (weeks / "index.json").write_text(json.dumps({"weeks": [
        {"week": "2026-W35", "file": "2026-W35.json",
         "sha256": publish_week.sha256_hex(body)}]}))
    assert any("changed since it was published" in p for p in verify_published.problems())
