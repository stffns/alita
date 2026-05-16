"""Followups: relative-offset parsing, table CRUD, retry backoff."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_db):
    yield


def test_compute_run_at_relative_simple():
    from pelops.jobs import _compute_run_at

    iso, cron = _compute_run_at("in 2 minutes")
    assert cron is None
    delta = datetime.fromisoformat(iso) - datetime.now(UTC)
    assert timedelta(seconds=110) < delta < timedelta(seconds=130)


def test_compute_run_at_relative_compound():
    from pelops.jobs import _compute_run_at

    iso, cron = _compute_run_at("in 1 hour and 30 minutes")
    assert cron is None
    delta = datetime.fromisoformat(iso) - datetime.now(UTC)
    assert timedelta(seconds=5395) < delta < timedelta(seconds=5410)


def test_compute_run_at_cron_returns_cron_field():
    from pelops.jobs import _compute_run_at

    iso, cron = _compute_run_at("0 9 * * *")
    assert cron == "0 9 * * *"
    # Next run should be in the future
    assert datetime.fromisoformat(iso) > datetime.now(UTC)


def test_compute_run_at_iso():
    from pelops.jobs import _compute_run_at

    iso, cron = _compute_run_at("2030-01-01T09:00:00")
    assert cron is None
    assert "2030-01-01" in iso


def test_compute_run_at_garbage_raises():
    from pelops.jobs import _compute_run_at

    with pytest.raises(ValueError, match="Cannot parse"):
        _compute_run_at("eventually maybe")


def test_add_and_list():
    from pelops import jobs

    jid = jobs.add(prompt="hello future", when="in 5 minutes", label="smoke")
    pending = jobs.list_pending()
    assert any(p["id"] == jid for p in pending)
    found = next(p for p in pending if p["id"] == jid)
    assert found["label"] == "smoke"
    assert found["cron"] is None


def test_cancel_marks_cancelled():
    from pelops import jobs

    jid = jobs.add(prompt="x", when="in 5 minutes", label="cancel-me")
    assert jobs.cancel(jid) is True
    assert all(p["id"] != jid for p in jobs.list_pending())
    # Second cancel is a no-op
    assert jobs.cancel(jid) is False


def test_claim_due_only_returns_past_jobs():
    from pelops import jobs

    future_id = jobs.add(prompt="a", when="in 1 hour", label="future")
    # Add a job in the past by sidestepping the parser
    past_iso = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    import sqlite3

    con = sqlite3.connect(str(jobs._db_path()))
    con.execute(
        "INSERT INTO pelops_followups "
        "(id, label, prompt, run_at_utc, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, 'pending')",
        ("fu_past_test", "past", "b", past_iso, past_iso),
    )
    con.commit()
    con.close()

    claimed = jobs.claim_due()
    claimed_ids = {r["id"] for r in claimed}
    assert "fu_past_test" in claimed_ids
    assert future_id not in claimed_ids


def test_mark_failed_schedules_retry_with_backoff():
    from pelops import jobs

    jid = jobs.add(prompt="x", when="in 30 seconds", label="will-fail")
    res1 = jobs.mark_failed(jid, last_error="boom")
    assert res1["retried"] is True
    assert res1["attempt"] == 1
    res2 = jobs.mark_failed(jid, last_error="boom")
    assert res2["attempt"] == 2
    # Sixth attempt exhausts the backoff schedule
    for _ in range(4):
        jobs.mark_failed(jid, last_error="boom")
    final = jobs.mark_failed(jid, last_error="boom")
    assert final["retried"] is False
    assert final["next_run_at"] is None


def test_mark_done_records_response():
    from pelops import jobs

    jid = jobs.add(prompt="x", when="in 30 seconds", label="done-test")
    jobs.mark_done(jid, response="the answer is 42")
    # The recent_completions API should surface it
    completions = jobs.recent_completions(hours=1, limit=10)
    found = next((r for r in completions if r["id"] == jid), None)
    assert found is not None
    assert found["status"] == "done"
    assert found["response"] == "the answer is 42"
