# -*- coding: utf-8 -*-
"""SQLite 持久化层的关键行为。"""
from __future__ import annotations

import pytest


def test_insert_and_read_account(isolated_db):
    db = isolated_db
    acc_id = db.insert_account(
        email="tester@example.com",
        access_token="tok-abc",
        totp_secret="JBSWY3DPEHPK3PXP",
        email_source="outlook",
        proxy_used="socks5h://127.0.0.1:11080",
        extra={"engine": "protocol", "registration_password": "pw-1"},
    )
    assert isinstance(acc_id, int) and acc_id > 0

    acc = db.get_account(acc_id)
    assert acc is not None
    assert acc["email"] == "tester@example.com"
    assert acc["access_token"] == "tok-abc"
    assert acc["totp_secret"] == "JBSWY3DPEHPK3PXP"

    assert db.get_account_by_email("tester@example.com")["id"] == acc_id
    assert db.get_account(acc_id + 999) is None


def test_insert_account_rejects_duplicate_email(isolated_db):
    db = isolated_db
    first = db.insert_account(email="dup@example.com", access_token="a")
    second = db.insert_account(email="dup@example.com", access_token="b")
    # 同邮箱重复登记不应该悄悄造出第二个账号
    assert second == first
    rows = db.list_accounts(limit=50)
    assert len([r for r in rows if r["email"] == "dup@example.com"]) == 1


def test_account_paging_and_search(isolated_db):
    db = isolated_db
    for i in range(7):
        db.insert_account(email=f"user{i}@example.com", access_token=f"tok{i}")

    page1 = db.list_accounts_page(limit=3, offset=0)
    page2 = db.list_accounts_page(limit=3, offset=3)
    assert len(page1["items"]) == 3
    assert page1["total"] == 7
    assert {r["id"] for r in page1["items"]}.isdisjoint({r["id"] for r in page2["items"]})

    hit = db.list_accounts_page(limit=10, offset=0, q="user3@")
    assert [r["email"] for r in hit["items"]] == ["user3@example.com"]


def test_account_archive_and_delete(isolated_db):
    db = isolated_db
    acc_id = db.insert_account(email="arch@example.com", access_token="t")
    assert db.archive_account(acc_id, True) is True
    assert db.list_accounts_page(limit=10, offset=0, archived=False)["total"] == 0
    assert db.list_accounts_page(limit=10, offset=0, archived="all")["total"] == 1


def test_email_pool_claim_and_release(isolated_db):
    db = isolated_db
    inserted, skipped = db.import_outlook_accounts([
        {"email": "pool1@outlook.com", "password": "p1", "refresh_token": "r1"},
        {"email": "pool2@outlook.com", "password": "p2", "refresh_token": "r2"},
        {"email": "", "password": "bad"},
    ])
    assert (inserted, skipped) == (2, 1)
    assert db.outlook_pool_summary()["available"] == 2

    claimed = db.claim_next_outlook()
    assert claimed and claimed["email"].startswith("pool")
    db.release_outlook(claimed["email"], status="used", note="用例标记")
    summary = db.outlook_pool_summary()
    assert summary["available"] == 1
    assert summary["used"] == 1


def test_account_filter_sql_is_parameterized():
    """过滤条件必须走占位符：一旦有人把用户输入拼进 SQL，这条会立刻红。"""
    from core import db

    payload = "free'; DROP TABLE accounts; --"
    where, params = db._account_filter_sql(
        plan_filter="free", codex_filter=payload, totp_filter="enabled"
    )
    sql = " AND ".join(where)
    assert "DROP TABLE" not in sql
    # 过滤值统一小写后作为绑定参数传入，不参与 SQL 文本拼接
    assert payload.lower() in params


@pytest.mark.parametrize(
    "filename",
    ["../evil.json", "..\\evil.json", "sub/codex-1.json", "cookie-1.json", "codex-1.txt", ""],
)
def test_read_codex_credential_blocks_traversal(isolated_db, filename):
    with pytest.raises(ValueError):
        isolated_db.read_codex_credential(filename)


def test_read_codex_credential_missing_file(isolated_db):
    with pytest.raises(ValueError):
        isolated_db.read_codex_credential("codex-does-not-exist.json")
