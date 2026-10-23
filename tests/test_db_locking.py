# -*- coding: utf-8 -*-
"""读写锁的边界：读不再抢 _LOCK（WAL + 事务已经够用），写继续串行。"""
from __future__ import annotations

import threading


class _LockHolder:
    """在后台线程里占着 _LOCK，直到 release()。"""

    def __init__(self, db):
        self.db = db
        self.started = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with self.db._LOCK:
            self.started.set()
            self.release.wait(20)

    def __enter__(self):
        self.thread.start()
        assert self.started.wait(5), "后台线程没能拿到 _LOCK"
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(5)
        return False


def test_reads_are_not_serialized_by_the_write_lock(isolated_db):
    db = isolated_db
    db.insert_account(email="a@example.com", access_token="t")
    result: dict = {}

    def reader():
        result["total"] = db.list_accounts_page(limit=5, offset=0)["total"]
        result["by_email"] = bool(db.get_account_by_email("a@example.com"))
        result["count"] = db.count_accounts()
        result["jobs"] = db.list_jobs_page(limit=5, offset=0)["total"]

    with _LockHolder(db):
        worker = threading.Thread(target=reader, daemon=True)
        worker.start()
        worker.join(5)
        assert not worker.is_alive(), "读取被 _LOCK 串行化了"

    assert result["total"] == 1
    assert result["by_email"] is True
    assert result["count"] >= 1
    assert result["jobs"] == 0


def test_writes_still_serialize_on_the_write_lock(isolated_db):
    db = isolated_db
    acc_id = db.insert_account(email="b@example.com", access_token="t")
    done: list = []

    def writer():
        db.claim_account_plan_check(acc_id)
        done.append(True)

    with _LockHolder(db):
        worker = threading.Thread(target=writer, daemon=True)
        worker.start()
        worker.join(0.6)
        assert not done, "写操作不该绕开 _LOCK"
    worker.join(5)
    assert done == [True]


def test_concurrent_inserts_do_not_lose_rows(isolated_db):
    db = isolated_db
    errors: list = []

    def insert_many(prefix):
        try:
            for i in range(8):
                db.insert_account(email=f"{prefix}{i}@example.com", access_token="t")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            db.close_thread_conn()

    threads = [threading.Thread(target=insert_many, args=(f"t{n}-",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert errors == []
    assert db.list_accounts_page(limit=100, offset=0)["total"] == 32
