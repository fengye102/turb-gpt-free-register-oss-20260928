# -*- coding: utf-8 -*-
"""已注册账号 Blueprint：搬出 app.py 后行为必须和闭包版一致。

只覆盖读路径与参数校验；查活/查套餐/优惠会打外网，拉起浏览器会起进程，都不进单测。
"""
from __future__ import annotations

import pytest

pytest.importorskip("pyotp", reason="WebUI 依赖 pyotp；CI 会装 requirements.txt")

AUTH = {"X-Auth-Code": "test-code", "Accept-Encoding": "identity"}

EXPECTED_ENDPOINTS = {
    "accounts.api_accounts",
    "accounts.api_account_plan_check_status",
    "accounts.api_account_secret",
    "accounts.api_accounts_secret_bulk",
    "accounts.api_account_archive",
    "accounts.api_accounts_archive_bulk",
    "accounts.api_account_delete",
    "accounts.api_accounts_delete_bulk",
    "accounts.api_account_note",
    "accounts.api_account_totp_setup",
    "accounts.api_account_change_email",
    "accounts.api_accounts_change_email_bulk",
    "accounts.api_accounts_totp_setup_bulk",
    "accounts.api_accounts_note_bulk",
    "accounts.api_accounts_check_live_bulk",
    "accounts.api_account_check_plan",
    "accounts.api_accounts_check_plan_bulk",
    "accounts.api_account_check_promo",
    "accounts.api_accounts_check_promo_bulk",
    "accounts.api_account_open_browser",
    "accounts.api_account_open_browser_status",
    "accounts.api_account_live_check_log",
    "accounts.api_account_totp_setup_log",
    "accounts.api_account_change_email_log",
}


@pytest.fixture()
def client(isolated_db):
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture()
def account_id(isolated_db):
    return isolated_db.insert_account(
        email="probe@example.com",
        access_token="tok-123",
        totp_secret="JBSWY3DPEHPK3PXP",
        email_source="outlook",
        extra={"registration_password": "pw-1"},
    )


def test_accounts_endpoints_registered():
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    endpoints = {n for n in app.view_functions if n.startswith("accounts.")}
    assert endpoints == EXPECTED_ENDPOINTS


def test_accounts_routes_require_auth(client):
    assert client.get("/api/accounts").status_code == 401
    assert client.post("/api/accounts/1/delete").status_code == 401


def test_accounts_list_and_paged_compaction(client, account_id):
    legacy = client.get("/api/accounts", headers=AUTH).get_json()
    assert [row["email"] for row in legacy] == ["probe@example.com"]

    paged = client.get("/api/accounts?paged=1&page=1&page_size=10", headers=AUTH).get_json()
    assert paged["compact"] is True
    assert paged["total"] == 1
    item = paged["items"][0]
    # 分页分支必须走 compact：不下发 token / TOTP 密钥
    assert "access_token" not in item
    assert "totp_secret" not in item
    assert item["has_access_token"] is True
    assert item["totp_enabled"] is True


def test_account_secret_reads_value(client, account_id):
    resp = client.get(f"/api/accounts/{account_id}/secret?field=access_token", headers=AUTH)
    assert resp.status_code == 200
    assert resp.get_json()["value"] == "tok-123"

    bad = client.get(f"/api/accounts/{account_id}/secret?field=bogus", headers=AUTH)
    assert bad.status_code == 400

    missing = client.get("/api/accounts/9999/secret?field=access_token", headers=AUTH)
    assert missing.status_code == 404


def test_account_secret_bulk(client, account_id):
    resp = client.post(
        "/api/accounts/secret-bulk",
        headers=AUTH,
        json={"account_ids": [account_id, 9999, "bad"], "field": "access_token"},
    )
    body = resp.get_json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["values"][0]["value"] == "tok-123"
    assert len(body["skipped"]) == 2

    empty = client.post("/api/accounts/secret-bulk", headers=AUTH, json={"account_ids": []})
    assert empty.status_code == 400


def test_account_note_and_archive(client, account_id):
    note = client.post(f"/api/accounts/{account_id}/note", headers=AUTH, json={"note": "hello"})
    assert note.get_json() == {"ok": True, "id": account_id, "note": "hello", "updated": True}

    archived = client.post(f"/api/accounts/{account_id}/archive", headers=AUTH, json={"archived": True})
    assert archived.get_json()["archived"] is True
    assert client.get("/api/accounts", headers=AUTH).get_json() == []
    assert len(client.get("/api/accounts?archived=all", headers=AUTH).get_json()) == 1


def test_account_delete_missing_returns_404(client):
    resp = client.post("/api/accounts/9999/delete", headers=AUTH)
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "账号不存在"


@pytest.mark.parametrize(
    "path",
    [
        "/api/accounts/note-bulk",
        "/api/accounts/archive-bulk",
        "/api/accounts/delete-bulk",
        "/api/accounts/totp-setup-bulk",
        "/api/accounts/check-live-bulk",
        "/api/accounts/check-plan-bulk",
        "/api/accounts/check-promo-bulk",
    ],
)
def test_account_bulk_endpoints_require_ids(client, path):
    resp = client.post(path, headers=AUTH, json={})
    assert resp.status_code == 400
    assert "account_ids" in resp.get_json()["error"]


def test_change_email_bulk_requires_valid_source(client):
    """换绑批量接口先校验邮箱来源，再校验 account_ids —— 顺序是既有行为。"""
    resp = client.post("/api/accounts/change-email-bulk", headers=AUTH, json={})
    assert resp.status_code == 400
    assert "邮箱来源" in resp.get_json()["error"]


def test_check_plan_missing_account_returns_404(client):
    resp = client.post("/api/accounts/check-plan", headers=AUTH, json={})
    assert resp.status_code == 404


def test_log_endpoints(client, account_id):
    bad = client.get("/api/accounts/live-check-log", headers=AUTH)
    assert bad.status_code == 400
    assert bad.get_json()["error"] == "email 为空"

    ok = client.get("/api/accounts/live-check-log?email=probe@example.com", headers=AUTH).get_json()
    assert ok["ok"] is True

    totp = client.get("/api/accounts/totp-setup-log?email=probe@example.com", headers=AUTH)
    assert totp.status_code == 200

    change = client.get(f"/api/accounts/{account_id}/change-email-log", headers=AUTH)
    assert change.status_code == 200
    assert change.get_json()["account_id"] == account_id

    missing = client.get("/api/accounts/9999/change-email-log", headers=AUTH)
    assert missing.status_code == 404


def test_plan_check_status_shape(client):
    body = client.get("/api/accounts/plan-check-status", headers=AUTH).get_json()
    assert body["items"] == []
    assert "queue" in body
