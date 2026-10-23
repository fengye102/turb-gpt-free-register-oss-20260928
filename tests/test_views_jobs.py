# -*- coding: utf-8 -*-
"""注册任务 Blueprint：从 app.py 搬出来后行为必须和闭包版一致。

注意这里**只测校验分支**：POST /api/jobs 传合法 body 会真的往 registration_service
的线程池里提交注册任务（进而起浏览器/打网络），不适合放进单测。
"""
from __future__ import annotations

import pytest

pytest.importorskip("pyotp", reason="WebUI 依赖 pyotp；CI 会装 requirements.txt")

AUTH = {"X-Auth-Code": "test-code"}


@pytest.fixture()
def client(isolated_db):
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    with app.test_client() as test_client:
        yield test_client


def test_job_endpoints_are_registered_under_blueprint():
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    endpoints = {name for name in app.view_functions if name.startswith("jobs.")}
    assert endpoints == {
        "jobs.api_jobs",
        "jobs.api_jobs_create",
        "jobs.api_manual_otp_waiting",
        "jobs.api_manual_otp_submit",
        "jobs.api_jobs_cancel_pending",
        "jobs.api_job_stop",
        "jobs.api_job_retry",
        "jobs.api_jobs_retry_bulk",
        "jobs.api_job_delete",
        "jobs.api_jobs_delete_bulk",
        "jobs.api_job_log",
    }


def test_job_routes_still_require_auth(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.post("/api/jobs", json={"count": 1}).status_code == 401


def test_job_list_is_empty_and_paged_shape_stable(client):
    assert client.get("/api/jobs", headers=AUTH).get_json() == []

    paged = client.get("/api/jobs?paged=1&page=1&page_size=5", headers=AUTH).get_json()
    assert paged["ok"] is True
    assert paged["items"] == []
    assert (paged["page"], paged["page_size"], paged["limit"], paged["offset"]) == (1, 5, 5, 0)
    assert "revision" in paged


@pytest.mark.parametrize("path", ["/api/jobs/999/log"])
def test_job_log_missing_job_returns_404(client, path):
    resp = client.get(path, headers=AUTH)
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "任务不存在"


@pytest.mark.parametrize("path", ["/api/jobs/999/stop", "/api/jobs/999/retry", "/api/jobs/999/delete"])
def test_job_actions_on_missing_job_return_404(client, path):
    resp = client.post(path, headers=AUTH)
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "任务不存在"


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"count": 0}, "count 需在 1~200 之间"),
        ({"count": 201}, "count 需在 1~200 之间"),
        ({"count": "abc"}, "count 非法"),
    ],
)
def test_job_create_validates_count_before_submitting(client, payload, error):
    resp = client.post("/api/jobs", headers=AUTH, json=payload)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == error


def test_job_delete_bulk_requires_ids(client):
    resp = client.post("/api/jobs/delete-bulk", headers=AUTH, json={})
    assert resp.status_code == 400
    assert "job_ids" in resp.get_json()["error"]


def test_cancel_pending_and_manual_otp_paths(client):
    assert client.post("/api/jobs/cancel-pending", headers=AUTH).get_json() == {"ok": True, "cancelled": 0}

    waiting = client.get("/api/manual-otp/waiting", headers=AUTH).get_json()
    assert waiting == {"ok": True, "waiting": []}

    bad = client.post("/api/manual-otp", headers=AUTH, json={})
    assert bad.status_code == 400
    assert bad.get_json()["error"] == "email/job_id 缺失"
