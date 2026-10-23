# -*- coding: utf-8 -*-
"""Codex 授权账号 Blueprint：搬出 app.py 后行为必须和闭包版一致。

只覆盖「读」和「校验」分支：/api/codex/retry、/stop 之类的写接口会真的动线程池。
"""
from __future__ import annotations

import pytest

pytest.importorskip("pyotp", reason="WebUI 依赖 pyotp；CI 会装 requirements.txt")

AUTH = {"X-Auth-Code": "test-code"}

EXPECTED_ENDPOINTS = {
    "codex.api_codex_list",
    "codex.api_codex_archive",
    "codex.api_codex_archive_bulk",
    "codex.api_codex_download",
    "codex.api_codex_download_from_cpa",
    "codex.api_codex_download_bulk_from_cpa",
    "codex.api_codex_download_bulk",
    "codex.api_codex_reset_export",
    "codex.api_codex_delete",
    "codex.api_codex_delete_bulk",
    "codex.api_codex_stop",
    "codex.api_codex_stop_bulk",
    "codex.api_codex_reset_retrying",
    "codex.api_codex_retry",
    "codex.api_codex_retry_bulk",
    "codex.api_codex_retry_log",
}


@pytest.fixture()
def client(isolated_db):
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    with app.test_client() as test_client:
        yield test_client


def test_codex_endpoints_are_registered_under_blueprint():
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    endpoints = {name for name in app.view_functions if name.startswith("codex.")}
    assert endpoints == EXPECTED_ENDPOINTS


def test_codex_routes_still_require_auth(client):
    assert client.get("/api/codex").status_code == 401
    assert client.post("/api/codex/archive", json={}).status_code == 401


def test_codex_list_shape(client):
    body = client.get("/api/codex", headers=AUTH).get_json()
    assert body["accounts"] == []
    assert body["summary"] == {"exported": 0, "pending": 0, "total": 0}


@pytest.mark.parametrize(
    "filename",
    ["codex-nope.json", "../evil.json", "..\\evil.json", "not-codex.json"],
)
def test_codex_download_rejects_bad_or_missing_filename(client, filename):
    resp = client.get(f"/api/codex/download/{filename}", headers=AUTH)
    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False


def test_codex_retry_log_validation(client):
    bad = client.get("/api/codex/retry-log", headers=AUTH)
    assert bad.status_code == 400
    assert bad.get_json()["error"] == "email 为空"

    ok = client.get("/api/codex/retry-log?email=a@b.com", headers=AUTH).get_json()
    assert ok == {"ok": True, "log": "", "running": False}


@pytest.mark.parametrize(
    "path,error",
    [
        ("/api/codex/archive", "filename 缺失"),
        ("/api/codex/delete", "filename 为空"),
        ("/api/codex/reset-export", "filename 为空"),
    ],
)
def test_codex_filename_endpoints_validate(client, path, error):
    resp = client.post(path, headers=AUTH, json={})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == error


def test_codex_retry_requires_email(client):
    resp = client.post("/api/codex/retry", headers=AUTH, json={})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "email 为空"
