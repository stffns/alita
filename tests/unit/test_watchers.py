"""Watchers: interval parsing, CRUD, adaptive cadence (NOOP backoff)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_db):
    yield


@pytest.mark.parametrize(
    "raw,seconds",
    [
        ("every 30 minutes", 1800),
        ("every 2 hours", 7200),
        ("every 1 day", 86400),
        ("300", 300),
        (3600, 3600),
        (10, 60),  # below minimum -> clamped up
        ("every 1 second", 60),  # below minimum -> clamped up
    ],
)
def test_parse_interval(raw, seconds):
    from pelops import watchers

    assert watchers.parse_interval(raw) == seconds


def test_parse_interval_garbage_raises():
    from pelops import watchers

    with pytest.raises(ValueError, match="Cannot parse interval"):
        watchers.parse_interval("once in a blue moon")


def test_add_and_list_active():
    from pelops import watchers

    wid = watchers.add(
        query="What is the latest python version?",
        interval="every 1 hour",
        label="py-version",
    )
    actives = watchers.list_active()
    assert any(w["id"] == wid for w in actives)
    found = next(w for w in actives if w["id"] == wid)
    assert found["interval_seconds"] == 3600
    assert found["last_checked_at"] is None


def test_cancel_marks_inactive():
    from pelops import watchers

    wid = watchers.add(query="q", interval=300, label="cancel-me")
    assert watchers.cancel(wid) is True
    actives = watchers.list_active()
    assert all(w["id"] != wid for w in actives)


def test_hash_ignores_dates_and_times():
    from pelops import watchers

    a = "As of 2026-05-16 10:30: Python 3.14.5 is the latest stable."
    b = "As of 2026-05-17 14:22: Python 3.14.5 is the latest stable."
    assert watchers._hash(a) == watchers._hash(b)


def test_hash_detects_substance_change():
    from pelops import watchers

    a = "Python 3.14.5 is the latest stable."
    b = "Python 3.15.0 is the latest stable."
    assert watchers._hash(a) != watchers._hash(b)


def test_has_changed_first_run_is_true():
    from pelops import watchers

    fresh_row = {"last_seen_hash": None}
    assert watchers.has_changed(fresh_row, "anything") is True


def test_has_changed_compares_hash():
    from pelops import watchers

    row = {"last_seen_hash": watchers._hash("same")}
    assert watchers.has_changed(row, "same") is False
    assert watchers.has_changed(row, "different") is True


def test_note_noop_backs_off_after_threshold():
    from pelops import watchers

    wid = watchers.add(query="q", interval=120, label="quiet-topic")
    # First 4 NOOPs should not change interval
    for _ in range(watchers.NOOPS_BEFORE_BACKOFF - 1):
        res = watchers.note_noop(wid)
        assert res["changed"] is False
    # The fifth NOOP triggers backoff
    res = watchers.note_noop(wid)
    assert res["changed"] is True
    assert res["direction"] == "slower"
    assert res["new_interval"] == 240  # 120 * 2


def test_note_alert_speeds_up_after_backoff():
    from pelops import watchers

    wid = watchers.add(query="q", interval=120, label="topic")
    # Trigger backoff -> interval becomes 240
    for _ in range(watchers.NOOPS_BEFORE_BACKOFF):
        watchers.note_noop(wid)
    # A real alert halves us back toward initial
    res = watchers.note_alert(wid)
    assert res["changed"] is True
    assert res["direction"] == "faster"
    assert res["new_interval"] == 120  # back to initial


def test_note_alert_no_change_when_at_initial():
    from pelops import watchers

    wid = watchers.add(query="q", interval=120, label="topic")
    # No backoff happened yet -- alert should not adjust
    res = watchers.note_alert(wid)
    assert res["changed"] is False
