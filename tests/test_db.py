"""Tests for the PrintQueue SQLite database layer."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from printqueue.db import Database, JobError, utc_to_local_str


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


# ---- jobs roundtrip / ordering -------------------------------------------
def test_add_get_list_roundtrip(db):
    jid = db.add_job("widget", filament_type="petg", estimated_minutes=42,
                     estimated_grams=12.5, priority="high", notes="hi")
    job = db.get_job(jid)
    assert job["name"] == "widget"
    assert job["filament_type"] == "PETG"  # upper-cased
    assert job["estimated_minutes"] == 42
    assert job["priority"] == "high"
    assert job["status"] == "queued"

    jobs = db.list_jobs()
    assert [j["id"] for j in jobs] == [jid]


def test_queued_priority_ordering(db):
    low = db.add_job("low", priority="low")
    high = db.add_job("high", priority="high")
    med = db.add_job("med", priority="medium")
    ordered = [j["id"] for j in db.list_jobs()]
    # queued jobs come out high, medium, low regardless of insertion order
    assert ordered == [high, med, low]


# ---- finish_job happy path ------------------------------------------------
def test_finish_job_happy_path(db):
    fid = db.add_filament("PLA", "black", grams=1000)
    jid = db.add_job("cube", filament_id=fid, estimated_grams=50,
                     estimated_minutes=90)

    returned = db.finish_job(jid)
    assert returned["id"] == jid  # pre-update row returned

    job = db.get_job(jid)
    assert job["status"] == "done"
    assert job["finished_at"] is not None

    hist = db.list_history()
    assert len(hist) == 1
    assert hist[0]["job_id"] == jid
    assert hist[0]["grams_used"] == 50
    assert hist[0]["minutes_taken"] == 90
    assert hist[0]["status"] == "done"

    fil = db.get_filament(fid)
    assert fil["grams_remaining"] == 950  # deducted exactly once


def test_finish_job_defaults_from_estimates(db):
    jid = db.add_job("j", estimated_grams=7, estimated_minutes=11)
    db.finish_job(jid)
    hist = db.list_history()[0]
    assert hist["grams_used"] == 7
    assert hist["minutes_taken"] == 11


def test_finish_job_explicit_overrides(db):
    fid = db.add_filament("PLA", "red", grams=1000)
    jid = db.add_job("j", filament_id=fid, estimated_grams=50)
    db.finish_job(jid, grams=20, minutes=5, notes="partial")
    hist = db.list_history()[0]
    assert hist["grams_used"] == 20
    assert hist["minutes_taken"] == 5
    assert hist["notes"] == "partial"
    assert db.get_filament(fid)["grams_remaining"] == 980


# ---- double completion guard ---------------------------------------------
def test_finish_job_twice_raises_and_no_double_deduct(db):
    fid = db.add_filament("PLA", "blue", grams=1000)
    jid = db.add_job("cube", filament_id=fid, estimated_grams=50)
    db.finish_job(jid)
    assert db.get_filament(fid)["grams_remaining"] == 950

    with pytest.raises(JobError) as exc:
        db.finish_job(jid)
    assert "already done" in str(exc.value)

    # still only one history row, filament unchanged
    assert len(db.list_history()) == 1
    assert db.get_filament(fid)["grams_remaining"] == 950


def test_finish_job_failed(db):
    fid = db.add_filament("PLA", "black", grams=1000)
    jid = db.add_job("cube", filament_id=fid, estimated_grams=30)
    db.finish_job(jid, failed=True)
    assert db.get_job(jid)["status"] == "failed"
    hist = db.list_history()[0]
    assert hist["status"] == "failed"
    # filament still deducted on failure
    assert db.get_filament(fid)["grams_remaining"] == 970

    with pytest.raises(JobError) as exc:
        db.finish_job(jid)
    assert "already failed" in str(exc.value)


def test_finish_job_missing_raises(db):
    with pytest.raises(JobError) as exc:
        db.finish_job(999)
    assert "not found" in str(exc.value)


def test_finish_job_no_filament_link(db):
    jid = db.add_job("j", estimated_grams=50)
    db.finish_job(jid)  # must not error despite no filament_id
    assert db.get_job(jid)["status"] == "done"


def test_finish_job_zero_grams_no_deduct(db):
    fid = db.add_filament("PLA", "black", grams=1000)
    jid = db.add_job("j", filament_id=fid, estimated_grams=0)
    db.finish_job(jid)  # grams == 0 -> skip deduction, no ValueError
    assert db.get_filament(fid)["grams_remaining"] == 1000


# ---- update_filament_grams validation ------------------------------------
def test_update_filament_rejects_zero(db):
    fid = db.add_filament("PLA", "black", grams=1000)
    with pytest.raises(ValueError):
        db.update_filament_grams(fid, 0)


def test_update_filament_rejects_negative(db):
    fid = db.add_filament("PLA", "black", grams=1000)
    with pytest.raises(ValueError):
        db.update_filament_grams(fid, -50)
    assert db.get_filament(fid)["grams_remaining"] == 1000


def test_update_filament_clamps_at_zero(db):
    fid = db.add_filament("PLA", "black", grams=100)
    db.update_filament_grams(fid, 250)
    assert db.get_filament(fid)["grams_remaining"] == 0


# ---- utc_to_local_str -----------------------------------------------------
def test_utc_to_local_str_converts():
    ts = "2026-07-14 11:10:00"
    expected = (
        datetime.fromisoformat(ts)
        .replace(tzinfo=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
    )
    assert utc_to_local_str(ts) == expected


def test_utc_to_local_str_accepts_t_separator():
    ts = "2026-07-14T11:10:00"
    expected = (
        datetime.fromisoformat(ts)
        .replace(tzinfo=timezone.utc)
        .astimezone()
        .strftime("%m-%d %H:%M")
    )
    assert utc_to_local_str(ts, "%m-%d %H:%M") == expected


def test_utc_to_local_str_passthrough_garbage():
    assert utc_to_local_str("not a date") == "not a date"


def test_utc_to_local_str_none():
    assert utc_to_local_str(None) == "—"


# ---- was_created ----------------------------------------------------------
def test_was_created_fresh_then_false_on_reopen(tmp_path):
    p = tmp_path / "fresh.db"
    first = Database(p)
    assert first.was_created is True
    second = Database(p)
    assert second.was_created is False


# ---- seed idempotence -----------------------------------------------------
def test_seed_defaults_idempotent(db):
    assert db.seed_defaults() is True
    assert len(db.list_filament()) == 3
    # second call is a no-op
    assert db.seed_defaults() is False
    assert len(db.list_filament()) == 3
    # force adds again
    assert db.seed_defaults(force=True) is True
    assert len(db.list_filament()) == 6
