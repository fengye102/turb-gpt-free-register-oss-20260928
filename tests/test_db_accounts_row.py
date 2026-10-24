# -*- coding: utf-8 -*-
"""账号写路径必须按行：整表重写会冲掉别的进程刚写进去的字段。"""
from __future__ import annotations

import json


def _mark_payload(db, row_id: int, marker: str, table: str = "accounts") -> None:
    """模拟「另一个进程刚往这一行写了字段」：直接改库里的 payload。"""
    with db._conn_scope() as conn, conn:
        row = conn.execute(f"SELECT payload FROM {table} WHERE id=?", (row_id,)).fetchone()
        payload = json.loads(row["payload"])
        payload["marker"] = marker
        conn.execute(
            f"UPDATE {table} SET payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), row_id),
        )


def test_mark_plan_check_running_keeps_other_accounts(isolated_db):
    db = isolated_db
    first = db.insert_account(email="a@x.com", access_token="ta")
    other = db.insert_account(email="b@x.com", access_token="tb")
    assert db.claim_account_plan_check(first, trigger="manual") is True
    _mark_payload(db, other, "keep-me")

    assert db.mark_account_plan_check_running(first) is True

    assert db.get_account(other)["marker"] == "keep-me", "整表重写把别的账号行冲掉了"
    assert db.get_account(first)["plan_check_status"] == "running"


def test_update_note_keeps_other_accounts(isolated_db):
    db = isolated_db
    target = db.insert_account(email="c@x.com", access_token="tc")
    other = db.insert_account(email="d@x.com", access_token="td")
    _mark_payload(db, other, "keep-me")
    assert db.update_account_note(target, "备注") is True
    assert db.get_account(other)["marker"] == "keep-me"
    assert db.get_account(target)["note"] == "备注"


def test_archive_account_keeps_other_accounts(isolated_db):
    db = isolated_db
    target = db.insert_account(email="e@x.com", access_token="te")
    other = db.insert_account(email="f@x.com", access_token="tf")
    _mark_payload(db, other, "keep-me")
    assert db.archive_account(target, True) is True
    assert db.get_account(other)["marker"] == "keep-me"
    assert db.get_account(target)["archived"] is True


def test_claim_and_mark_totp_setup_keeps_other_accounts(isolated_db):
    db = isolated_db
    target = db.insert_account(email="g@x.com", access_token="tg")
    other = db.insert_account(email="h@x.com", access_token="th")
    _mark_payload(db, other, "keep-me")
    assert db.claim_account_totp_setup(target) is True
    assert db.mark_account_totp_setup_running(target) is True
    assert db.get_account(other)["marker"] == "keep-me"
    assert db.get_account(target)["totp_setup_status"] == "running"


def test_codex_status_by_email_is_single_row(isolated_db):
    db = isolated_db
    target = db.insert_account(email="i@x.com", access_token="ti")
    other = db.insert_account(email="j@x.com", access_token="tj")
    _mark_payload(db, other, "keep-me")
    assert db.update_account_codex_status("i@x.com", "success") is True
    assert db.get_account(other)["marker"] == "keep-me"
    assert db.get_account(target)["codex_status"] == "success"


def test_single_row_save_refreshes_copy_line(isolated_db):
    """整表写回时 _save_accounts 会给每行重算 copy_line，按行写必须保持一致。"""
    db = isolated_db
    acc_id = db.insert_account(
        email="k@x.com", access_token="tk", extra={"registration_password": "old-pw"}
    )
    assert db.update_account_extra(acc_id, {"registration_password": "new-pw"}) is True
    fresh = db.get_account(acc_id)
    assert fresh["copy_line"] == db._account_line(fresh)
