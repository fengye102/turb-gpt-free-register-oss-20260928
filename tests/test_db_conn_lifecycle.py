# -*- coding: utf-8 -*-
"""回归闸门：连接注册表按线程索引、请求收尾会关连接、池子 claim 原子。

对应两个已修 P0：
  1. WebUI 每请求一个线程，按线程缓存的 SQLite 连接没人回收 → 连接与 fd 随请求数线性增长；
  2. _claim_next_pool_row 是「SELECT 再 UPDATE」两步，多进程并发会重复领取同一个邮箱。
"""
from __future__ import annotations

import json
import threading

import pytest


def test_all_conns_is_thread_indexed(isolated_db):
    db = isolated_db
    conn = db._sqlite_conn()
    assert isinstance(db._ALL_CONNS, dict), "注册表必须按线程 id 索引，否则 close 是 O(n) 线性扫描"
    assert db._ALL_CONNS.get(threading.get_ident()) is conn


def test_close_thread_conn_removes_only_own_entry(isolated_db):
    db = isolated_db
    main_conn = db._sqlite_conn()
    other: dict = {}
    ready = threading.Event()

    def worker():
        other["conn"] = db._sqlite_conn()
        other["ident"] = threading.get_ident()
        ready.set()
        other["hold"] = threading.Event()
        other["hold"].wait(10)
        db.close_thread_conn()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert ready.wait(5)
    assert other["conn"] is not main_conn
    assert len(db._ALL_CONNS) == 2

    other["hold"].set()
    thread.join(5)
    assert not thread.is_alive()
    # 子线程收尾只摘掉自己那条，主线程的连接仍在注册表里
    assert db._ALL_CONNS.get(threading.get_ident()) is main_conn
    assert other["ident"] not in db._ALL_CONNS


def test_close_all_conns_closes_every_thread(isolated_db):
    db = isolated_db
    db._sqlite_conn()
    held: list = []
    barrier = threading.Barrier(4)

    def worker():
        held.append(db._sqlite_conn())
        barrier.wait(10)  # 故意不 close，模拟没人收尾的后台线程

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(db._ALL_CONNS) == 5

    db._close_all_conns()
    assert db._ALL_CONNS == {}
    for conn in held:
        with pytest.raises(Exception):
            conn.execute("SELECT 1")


def test_request_teardown_closes_thread_conn(isolated_db, monkeypatch):
    """teardown_appcontext 必须真的关掉请求线程的连接（否则 P0 会原地复活）。"""
    from webui.app import create_app

    app = create_app(auth_code="teardown-code")
    # 先确保「请求开始前」本线程没有缓存连接，这样 teardown 关的是请求自己的那条。
    isolated_db.close_thread_conn()
    seen: list = []
    before = len(isolated_db._ALL_CONNS)
    original = isolated_db.close_thread_conn

    def spy():
        seen.append(len(isolated_db._ALL_CONNS))
        original()

    monkeypatch.setattr(isolated_db, "close_thread_conn", spy)
    resp = app.test_client().get("/api/summary", headers={"X-Auth-Code": "teardown-code"})
    assert resp.status_code == 200
    assert seen, "create_app 没有注册 teardown_appcontext 回调"
    assert seen[-1] >= 1, "请求期间本应建过连接"
    assert len(isolated_db._ALL_CONNS) == before == 0, "请求结束后连接必须被摘掉并关闭"


def test_claim_is_atomic_under_threads(isolated_db):
    """同一进程多线程并发 claim：每个邮箱只能被领走一次。"""
    db = isolated_db
    total = 12
    db.import_outlook_accounts([
        {"email": f"t{i}@outlook.com", "password": "p", "client_id": "c", "refresh_token": "r"}
        for i in range(total)
    ])
    got: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(6)
    errors: list = []

    def worker():
        try:
            barrier.wait(10)
            local = []
            while True:
                row = db.claim_next_outlook()
                if row is None:
                    break
                local.append(row["email"])
            with lock:
                got.extend(local)
        except Exception as exc:  # noqa: BLE001 - 测试要把异常带回主线程
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            db.close_thread_conn()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert len(got) == total, f"领了 {len(got)} 次，池子只有 {total} 条"
    assert len(set(got)) == total, "出现重复领取"


def test_claim_payload_matches_save_pool_row_semantics(isolated_db):
    """写回语义必须跟 _save_pool_row 一致：used / used_at / note=None，其余字段保留。"""
    db = isolated_db
    db.import_outlook_accounts([
        {"email": "sem@outlook.com", "password": "pw", "client_id": "cid", "refresh_token": "rt"},
    ])
    with db._conn_scope() as conn, conn:
        conn.execute("UPDATE email_pool SET payload=json_set(payload,'$.note','旧备注')")

    claimed = db.claim_next_outlook()
    assert claimed["status"] == "used" and claimed["note"] is None and claimed["used_at"]

    with db._conn_scope() as conn:
        stored = conn.execute(
            "SELECT payload, status, email, archived, updated_at FROM email_pool WHERE id=?",
            (int(claimed["id"]),),
        ).fetchone()
    payload = json.loads(stored["payload"])
    assert stored["status"] == payload["status"] == "used"
    assert payload["used_at"] == claimed["used_at"]
    assert payload["note"] is None
    # 原 _save_pool_row 的列上 updated_at 取 row.get("updated_at") or _now()。
    # `import_outlook_accounts` 建的行 payload 里本来就没有 updated_at 这个键，
    # 所以这里断言「列有值、payload 没这个键」—— claim 不该凭空加键。
    assert stored["updated_at"]
    assert "updated_at" not in payload
    # 派生/业务字段不能被 claim 弄丢
    assert payload["password"] == "pw" and payload["client_id"] == "cid"
    assert payload["refresh_token"] == "rt" and payload["copy_line"]
