# -*- coding: utf-8 -*-
"""Codex Agent Token / CPA 导出 Blueprint：搬家不改行为。

只测校验与 404 分支：真正生成 Token 需要活账号 + 外网。
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


def test_codex_agent_endpoints_registered():
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    endpoints = {n for n in app.view_functions if n.startswith("codex_agent.")}
    assert endpoints == {
        "codex_agent.api_account_codex_agent",
        "codex_agent.api_accounts_codex_agent_bulk",
        "codex_agent.api_account_codex_agent_upload_sub2",
        "codex_agent.api_accounts_codex_agent_upload_sub2_bulk",
        "codex_agent.api_account_codex_agent_download",
        "codex_agent.api_accounts_codex_agent_download_bulk",
        "codex_agent.api_accounts_download_cpa_bulk",
    }


def test_codex_agent_routes_require_auth(client):
    assert client.post("/api/accounts/codex-agent", json={}).status_code == 401
    assert client.get("/api/accounts/1/codex-agent/download").status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/api/accounts/9999/codex-agent/upload-sub2",
    ],
)
def test_codex_agent_missing_account_returns_404(client, path):
    resp = client.post(path, headers=AUTH, json={})
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "账号不存在"


def test_codex_agent_download_missing_account_returns_404(client):
    resp = client.get("/api/accounts/9999/codex-agent/download", headers=AUTH)
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "账号不存在"


@pytest.mark.parametrize(
    "path",
    [
        "/api/accounts/codex-agent-bulk",
        "/api/accounts/download-cpa-bulk",
        "/api/accounts/codex-agent/download-bulk",
        "/api/accounts/codex-agent/upload-sub2-bulk",
    ],
)
def test_codex_agent_bulk_requires_account_ids(client, path):
    resp = client.post(path, headers=AUTH, json={})
    assert resp.status_code == 400
    assert "account_ids" in resp.get_json()["error"]


def test_codex_agent_single_requires_valid_account_id(client):
    resp = client.post("/api/accounts/codex-agent", headers=AUTH, json={})
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "账号不存在"
