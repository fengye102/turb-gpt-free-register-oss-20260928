# -*- coding: utf-8 -*-
"""邮箱池的 claim/release 必须只动一行：整表重写会冲掉别的来源/别的行。"""
from __future__ import annotations

import json

LINES = "p1@outlook.com----pw1----cid1----rt1\np2@outlook.com----pw2----cid2----rt2"


def _pool_row(db, email: str):
    with db._conn_scope() as conn:
        row = conn.execute(
            "SELECT payload FROM email_pool WHERE email=? COLLATE NOCASE", (email,)
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def test_claim_next_outlook_marks_only_one_row(isolated_db):
    db = isolated_db
    db.import_outlook_accounts([
        {"email": "p1@outlook.com", "password": "pw1", "client_id": "cid1", "refresh_token": "rt1"},
        {"email": "p2@outlook.com", "password": "pw2", "client_id": "cid2", "refresh_token": "rt2"},
    ])
    claimed = db.claim_next_outlook()
    assert claimed["email"] == "p1@outlook.com", "应领取 id 最小的可用邮箱"
    assert _pool_row(db, "p1@outlook.com")["status"] == "used"
    assert _pool_row(db, "p2@outlook.com")["status"] == "available"
    summary = db.outlook_pool_summary()
    assert (summary["available"], summary["used"]) == (1, 1)


def test_release_outlook_touches_only_that_row(isolated_db):
    db = isolated_db
    db.import_outlook_accounts([
        {"email": "p1@outlook.com", "password": "pw1", "client_id": "cid1", "refresh_token": "rt1"},
        {"email": "p2@outlook.com", "password": "pw2", "client_id": "cid2", "refresh_token": "rt2"},
    ])
    db.claim_next_outlook()
    db.release_outlook("p1@outlook.com", status="available", note="回收")
    row1 = _pool_row(db, "p1@outlook.com")
    assert row1["status"] == "available" and row1["used_at"] is None and row1["note"] == "回收"
    row2 = _pool_row(db, "p2@outlook.com")
    assert row2["status"] == "available" and row2["note"] is None


def test_release_unconsumed_requires_no_registered_account(isolated_db):
    db = isolated_db
    db.import_outlook_accounts([
        {"email": "p1@outlook.com", "password": "pw1", "client_id": "cid1", "refresh_token": "rt1"},
    ])
    db.claim_next_outlook()
    assert _pool_row(db, "p1@outlook.com")["status"] == "used"

    # 已有同邮箱账号：不能回收
    db.insert_account(email="p1@outlook.com", access_token="t", email_source="outlook")
    assert db.release_unconsumed_outlook("p1@outlook.com") is False
    assert _pool_row(db, "p1@outlook.com")["status"] == "used"


def test_release_unconsumed_returns_row_to_available(isolated_db):
    db = isolated_db
    db.import_outlook_accounts([
        {"email": "p1@outlook.com", "password": "pw1", "client_id": "cid1", "refresh_token": "rt1"},
    ])
    db.claim_next_outlook()
    assert db.release_unconsumed_outlook("p1@outlook.com", note="未生成账号") is True
    row = _pool_row(db, "p1@outlook.com")
    assert row["status"] == "available" and row["used_at"] is None and row["note"] == "未生成账号"
