# -*- coding: utf-8 -*-
"""一次性下载缓存：不丢、不超限、不过期滞留。

这个类曾经是 app.py 里的一个裸 dict —— 定义被挪走却忘了补回来时，
任何下载都会 NameError。所以这里把行为钉死。
"""
from __future__ import annotations

import threading
import time

import pytest

from webui.api_utils import (
    PreparedDownloads,
    account_secret_value,
    compact_account_for_list,
    compact_job_for_list,
    job_status_counts,
    matches_query,
    paginate_items,
    pool_source_arg,
    read_log_tail,
    with_pool_source,
)


def test_put_then_take_is_one_shot(tmp_path):
    cache = PreparedDownloads(ttl=60, limit=4)
    download_id = cache.put(b"zip-bytes", "accounts.zip")
    assert cache.count() == 1

    item = cache.take(download_id)
    assert item["content"] == b"zip-bytes"
    assert item["filename"] == "accounts.zip"
    assert item["mimetype"] == "application/zip"
    # 取走即删，第二次必须拿不到
    assert cache.take(download_id) is None
    assert cache.count() == 0


def test_take_unknown_id_returns_none():
    cache = PreparedDownloads()
    assert cache.take("not-a-real-id") is None
    assert cache.take("") is None


def test_limit_evicts_oldest():
    cache = PreparedDownloads(ttl=600, limit=3)
    ids = [cache.put(f"payload-{i}".encode(), f"{i}.zip") for i in range(5)]
    assert cache.count() == 3
    assert cache.take(ids[0]) is None
    assert cache.take(ids[1]) is None
    assert cache.take(ids[4]) is not None


def test_expired_entries_are_swept_on_next_put():
    cache = PreparedDownloads(ttl=0.05, limit=10)
    stale = cache.put(b"old", "old.zip")
    time.sleep(0.08)
    cache.put(b"new", "new.zip")
    assert cache.take(stale) is None
    assert cache.count() == 1


def test_concurrent_put_and_take_is_safe():
    cache = PreparedDownloads(ttl=600, limit=5000)
    produced: list[str] = []
    lock = threading.Lock()

    def producer(n: int) -> None:
        for i in range(40):
            download_id = cache.put(f"p{n}-{i}".encode(), f"{n}-{i}.zip")
            with lock:
                produced.append(download_id)

    def consumer() -> None:
        deadline = time.time() + 2
        while time.time() < deadline:
            with lock:
                batch = produced[:20]
                del produced[:20]
            for download_id in batch:
                cache.take(download_id)

    threads = [threading.Thread(target=producer, args=(n,)) for n in range(4)]
    threads.append(threading.Thread(target=consumer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for download_id in produced:
        cache.take(download_id)
    assert cache.count() == 0


# ============================================================
# 从 app.py 抽出来的列表 / 分页 / 日志工具
# ============================================================


def test_paginate_items_clamps_and_slices():
    items = [{"i": i} for i in range(12)]
    # 非法分页参数回落到默认值：页码至少 1，页宽 0/None → 50，上限 500。
    page = paginate_items(items, page=0, page_size=0)
    assert page["page"] == 1 and page["page_size"] == 50
    assert page["items"] == items and page["total"] == 12

    page2 = paginate_items(items, page=2, page_size=5)
    assert page2["offset"] == 5 and page2["limit"] == 5
    assert page2["items"] == items[5:10]

    huge = paginate_items(items, page=1, page_size=99999)
    assert huge["page_size"] == 500


def test_matches_query_is_case_insensitive_and_reads_all_values():
    row = {"email": "User@Example.com", "note": "已注册", "count": 3}
    assert matches_query(row, None) is True
    assert matches_query(row, "   ") is True
    assert matches_query(row, "user@example") is True
    assert matches_query(row, "已注册") is True
    assert matches_query(row, "3") is True
    assert matches_query(row, "nope") is False


def test_with_pool_source_does_not_mutate_input():
    rows = [{"email": "a@b.com"}, {"email": "c@d.com", "copy_line": "keep-me"}]
    out = with_pool_source(rows, "outlook")
    assert [r["source"] for r in out] == ["outlook", "outlook"]
    assert out[0]["copy_line"] == "a@b.com"
    assert out[1]["copy_line"] == "keep-me"
    assert "source" not in rows[0]


def test_compact_account_for_list_never_leaks_secrets():
    """列表对象是下发给前端的，绝不能带上完整 token / TOTP 密钥。"""
    row = {
        "id": 7,
        "email": "a@b.com",
        "access_token": "super-secret-token",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "codex_agent_token": "agent-secret",
        "extra_json": '{"registration_password": "pw-1", "network_traffic": {"available": true}}',
        "user_name": "Tester",
        "note": "",
        "promo_summary": "",
    }
    out = compact_account_for_list(row)
    assert out["has_access_token"] is True
    assert out["totp_enabled"] is True
    assert out["codex_agent_has_token"] is True
    assert out["password"] == "pw-1"
    assert "access_token" not in out
    assert "totp_secret" not in out
    assert "codex_agent_token" not in out
    assert "extra_json" not in out
    # 固定列允许是空值（前端要占位），可选列（查活/提链之类）为空时必须不出现。
    assert out["note"] == ""
    assert "live_check_status" not in out
    assert "extract_link_status" not in out


def test_compact_account_for_list_tolerates_broken_extra_json():
    out = compact_account_for_list({"id": 1, "email": "a@b.com", "extra_json": "{not json"})
    assert "password" not in out
    assert out["has_access_token"] is False


def test_account_secret_value_supported_fields_and_errors():
    row = {
        "access_token": "tok",
        "codex_agent_token": "agent",
        "totp_secret": "SECRET",
        "copy_line": "line",
        "extra_json": '{"registration_password": "pw"}',
    }
    assert account_secret_value(row, "access_token") == "tok"
    assert account_secret_value(row, "codex_agent_token") == "agent"
    assert account_secret_value(row, "totp_secret") == "SECRET"
    assert account_secret_value(row, "password") == "pw"
    assert account_secret_value(row, "copy_line")
    # 没有 secret 时取 totp_code 直接返回空串，连 pyotp 都不需要导入
    assert account_secret_value({"totp_secret": ""}, "totp_code") == ""
    assert account_secret_value({"id": 1}, "password") == "未设置"
    with pytest.raises(ValueError):
        account_secret_value(row, "access_token_please")


def test_compact_job_for_list_truncates_error_and_gates_traffic():
    long_error = "x" * 300
    job = {
        "id": 3,
        "status": "failed",
        "error_message": long_error,
        "network_traffic": {"available": False, "up": 1},
        "retryable": False,
        "email": "a@b.com",
    }
    out = compact_job_for_list(job)
    assert out["error_message"].endswith("…")
    assert len(out["error_message"]) == 241
    assert "network_traffic" not in out
    assert "retryable" not in out

    ok = compact_job_for_list({"id": 4, "status": "success", "network_traffic": {"available": True, "up": 5}})
    assert ok["network_traffic"]["up"] == 5


def test_job_status_counts_includes_active_bucket():
    counts = job_status_counts([
        {"status": "pending"}, {"status": "running"}, {"status": "stopping"},
        {"status": "success"}, {"status": "success"}, {},
    ])
    assert counts["active"] == 3
    assert counts["success"] == 2
    assert counts["unknown"] == 1


def test_read_log_tail_handles_missing_and_huge_files(tmp_path):
    missing = tmp_path / "nope.log"
    assert read_log_tail(missing, max_bytes=100) == {"ok": True, "log": "", "running": False}

    log = tmp_path / "run.log"
    log.write_text("A" * 50 + "TAIL", encoding="utf-8")
    result = read_log_tail(log, max_bytes=4, default_running=True)
    assert result["log"] == "TAIL"
    assert result["running"] is True


def test_read_log_tail_survives_broken_running_probe(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("hello", encoding="utf-8")

    def boom():
        raise RuntimeError("探针自己的毛病")

    result = read_log_tail(log, max_bytes=100, default_running=False, running_fn=boom)
    assert result["log"] == "hello"
    assert result["running"] is False


def test_pool_source_arg_reads_query_then_body():
    from flask import Flask

    app = Flask(__name__)
    with app.test_request_context("/api/outlook?source=generic_api"):
        assert pool_source_arg() == "generic_api"
    with app.test_request_context("/api/outlook?source=bogus"):
        assert pool_source_arg() == "outlook"
        assert pool_source_arg(default="all") == "all"
    with app.test_request_context("/api/outlook", method="POST", json={"type": "imap"}):
        assert pool_source_arg() == "imap"
    with app.test_request_context("/api/outlook"):
        assert pool_source_arg() == "outlook"


def test_shared_download_cache_is_module_level():
    """生成下载包的接口和 /api/downloads/<id> 分属不同 Blueprint，必须共用一份缓存。"""
    from webui import api_utils

    download_id = api_utils.put_prepared_download(b"zip", "x.zip")
    assert isinstance(download_id, str) and len(download_id) == 32
    item = api_utils.prepared_downloads.take(download_id)
    assert item["filename"] == "x.zip"
    assert item["content"] == b"zip"
    assert api_utils.prepared_downloads.take(download_id) is None

