# -*- coding: utf-8 -*-
"""落库时间戳必须带时区；旧的无时区本地时间戳仍要能被正确判定新旧。"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone

import json

import pytest


def _set_plan_stamp(db, acc_id: int, stamp: str) -> None:
    """plan_check_* 存在 payload JSON 里，不是独立列，必须整行改写 payload。"""
    with closing(db._sqlite_conn()) as conn, conn:
        row = conn.execute("SELECT payload FROM accounts WHERE id=?", (acc_id,)).fetchone()
        data = json.loads(row["payload"])
        data["plan_check_status"] = "queued"
        data["plan_check_queued_at"] = stamp
        conn.execute(
            "UPDATE accounts SET payload=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), acc_id),
        )


def test_now_is_utc_aware(isolated_db):
    parsed = datetime.fromisoformat(isolated_db._now())
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_elapsed_seconds_accepts_utc_and_legacy_local(isolated_db):
    db = isolated_db
    fresh_utc = db._now()
    fresh_local = datetime.now().isoformat(timespec="seconds")
    for stamp in (fresh_utc, fresh_local):
        elapsed = db._elapsed_seconds(stamp)
        assert 0 <= elapsed < 10, (stamp, elapsed)


def test_elapsed_seconds_measures_both_shapes(isolated_db):
    db = isolated_db
    old_utc = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    old_local = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    for stamp in (old_utc, old_local):
        assert 7100 <= db._elapsed_seconds(stamp) <= 7300, stamp


def test_elapsed_seconds_rejects_garbage(isolated_db):
    db = isolated_db
    for bad in ("", None, "不是时间"):
        with pytest.raises(ValueError):
            db._elapsed_seconds(bad)


def test_stale_queued_plan_check_is_marked_failed(isolated_db):
    db = isolated_db
    acc_id = db.insert_account(email="stale@example.com", access_token="t")
    ancient = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    _set_plan_stamp(db, acc_id, ancient)
    decorated = db._decorate_account(dict(db.get_account(acc_id)))
    assert decorated["plan_check_status"] == "failed"
    assert decorated["plan_check_stale"] is True


def test_fresh_queued_plan_check_survives(isolated_db):
    db = isolated_db
    acc_id = db.insert_account(email="fresh@example.com", access_token="t")
    _set_plan_stamp(db, acc_id, db._now())
    decorated = db._decorate_account(dict(db.get_account(acc_id)))
    assert decorated["plan_check_status"] == "queued"
    assert not decorated.get("plan_check_stale")
