# -*- coding: utf-8 -*-
"""SQLite 连接按线程复用；不能被上一位调用方关掉，也不能跨线程串。"""
from __future__ import annotations

import sqlite3
import threading

import pytest


def test_same_thread_reuses_connection(isolated_db):
    db = isolated_db
    assert db._sqlite_conn() is db._sqlite_conn()


def test_conn_scope_borrows_without_closing(isolated_db):
    db = isolated_db
    with db._conn_scope() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert db._sqlite_conn() is conn
    assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_other_threads_get_their_own_connection(isolated_db):
    db = isolated_db
    main_conn = db._sqlite_conn()
    seen: dict = {}

    def worker():
        try:
            conn = db._sqlite_conn()
            seen["conn"] = conn
            seen["alive"] = conn.execute("SELECT 1").fetchone()[0] == 1
        finally:
            db.close_thread_conn()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen["alive"] is True
    assert seen["conn"] is not main_conn


def test_externally_closed_connection_is_replaced(isolated_db):
    db = isolated_db
    broken = db._sqlite_conn()
    broken.close()
    fresh = db._sqlite_conn()
    assert fresh is not broken
    assert fresh.execute("SELECT 1").fetchone()[0] == 1


def test_close_thread_conn_drops_cache(isolated_db):
    db = isolated_db
    first = db._sqlite_conn()
    db.close_thread_conn()
    second = db._sqlite_conn()
    assert second is not first
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")


def test_pragmas_still_applied_on_new_connection(isolated_db):
    db = isolated_db
    db.close_thread_conn()
    conn = db._sqlite_conn()
    assert conn.row_factory is sqlite3.Row
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_write_is_visible_to_next_call(isolated_db):
    db = isolated_db
    db.insert_account(email="commit@example.com", access_token="t")
    assert db.get_account_by_email("commit@example.com") is not None


def test_path_switch_builds_new_connection(isolated_db, tmp_path, monkeypatch):
    db = isolated_db
    first = db._sqlite_conn()
    other = tmp_path / "另一处"
    other.mkdir()
    monkeypatch.setattr(db, "_ACCOUNTS_JSON", other / "注册成功的邮箱.json")
    db._SQLITE_READY = False
    db._SQLITE_READY_PATH = None
    second = db._sqlite_conn()
    assert second is not first
    assert str(other) in str(db._active_sqlite_path())
    db.close_thread_conn()
    first.close()
