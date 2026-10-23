# -*- coding: utf-8 -*-
"""任务表必须按行读写：整表重写会掉别人刚写进去的字段。"""
from __future__ import annotations

import json


def test_update_job_does_not_clobber_other_rows(isolated_db):
    """update_job 原来是「整表读 -> 整表删 -> 整表插」，会把别的任务行一起写回旧值。"""
    db = isolated_db
    job_a = db.create_job("outlook", email="a@x.com")
    job_b = db.create_job("outlook", email="b@x.com")

    # 模拟另一个进程刚往 job_b 里写了字段：直接改库里的 payload
    with db._conn_scope() as conn, conn:
        conn.execute(
            "UPDATE registration_jobs SET payload=? WHERE id=?",
            (json.dumps({**job_b, "别的进程写的标记": "keep-me"}, ensure_ascii=False), job_b["id"]),
        )

    db.update_job(job_a["id"], status="running", started_at="2026-10-24T00:00:00+00:00")

    assert db.get_job(job_b["id"])["别的进程写的标记"] == "keep-me", "整表重写把别的任务行冲掉了"
    assert db.get_job(job_a["id"])["status"] == "running"


def test_created_jobs_get_increasing_ids(isolated_db):
    db = isolated_db
    first = db.create_job("outlook")
    second = db.create_job("outlook")
    assert second["id"] > first["id"]
    assert {j["id"] for j in db.list_jobs(limit=10)} == {first["id"], second["id"]}


def test_update_job_refreshes_updated_at(isolated_db):
    db = isolated_db
    job = db.create_job("outlook")
    db.update_job(job["id"], status="running")
    fresh = db.get_job(job["id"])
    assert fresh["status"] == "running"
    assert fresh["updated_at"]


def test_delete_job_keeps_others_and_respects_running(isolated_db):
    db = isolated_db
    keep = db.create_job("outlook", email="keep@x.com")
    drop = db.create_job("outlook", email="drop@x.com")
    assert db.delete_job(drop["id"]) is True
    assert db.get_job(drop["id"]) is None
    assert db.get_job(keep["id"]) is not None

    running = db.create_job("outlook")
    db.update_job(running["id"], status="running")
    assert db.delete_job(running["id"]) is False
    assert db.get_job(running["id"]) is not None
    assert db.delete_job(running["id"], allow_running=True) is True
    assert db.get_job(running["id"]) is None


def test_get_job_missing_returns_none(isolated_db):
    assert isolated_db.get_job(999999) is None
