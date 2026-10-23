# -*- coding: utf-8 -*-
"""邮箱池 Blueprint：搬出 app.py 之后行为必须和闭包版一致。

这组用例的价值在「搬家不改行为」——pools 域从 create_app() 的闭包变成
webui/views/pools.py 之后，路径、参数、状态码、鉴权都不许变。
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("pyotp", reason="WebUI 依赖 pyotp；CI 会装 requirements.txt")

AUTH = {"X-Auth-Code": "test-code"}
OUTLOOK_LINES = "\n".join([
    "a@x.com----pw1----cid1----rt1",
    "b@x.com----pw2----cid2----rt2",
    "# 注释行会被跳过",
    "坏行",
])


@pytest.fixture()
def client(isolated_db):
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    with app.test_client() as test_client:
        yield test_client


def test_pool_endpoints_are_registered_under_blueprint():
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    endpoints = {name for name in app.view_functions if name.startswith("pools.")}
    assert endpoints == {
        "pools.api_outlook",
        "pools.api_outlook_import",
        "pools.api_outlook_status",
        "pools.api_outlook_status_bulk",
        "pools.api_outlook_delete",
        "pools.api_outlook_delete_bulk",
        "pools.api_domain_pool",
        "pools.api_domain_pool_status",
        "pools.api_domain_pool_delete",
    }


def test_pool_routes_still_require_auth(client):
    assert client.get("/api/outlook").status_code == 401
    assert client.post("/api/outlook/import", json={}).status_code == 401


def test_outlook_import_list_and_paging(client):
    resp = client.post("/api/outlook/import", headers=AUTH, json={"source": "outlook", "text": OUTLOOK_LINES})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "inserted": 2, "skipped": 0, "parsed": 2, "as_registered": False}

    items = client.get("/api/outlook?source=outlook", headers=AUTH).get_json()
    # 列表按 id 倒序（最新导入在前），这是既有行为，不要改。
    assert [row["email"] for row in items] == ["b@x.com", "a@x.com"]

    paged = client.get("/api/outlook?source=outlook&paged=1&page=1&page_size=1", headers=AUTH).get_json()
    assert paged["total"] == 2 and paged["page"] == 1 and paged["page_size"] == 1
    assert len(paged["items"]) == 1


def test_outlook_import_rejects_bad_input(client):
    resp = client.post("/api/outlook/import", headers=AUTH, json={"source": "outlook", "text": "没有分隔符"})
    assert resp.status_code == 400
    assert "未解析到有效邮箱行" in resp.get_json()["error"]

    resp2 = client.post("/api/outlook/import", headers=AUTH, json={"source": "domain", "text": "x"})
    assert resp2.status_code == 400


def test_outlook_status_and_bulk(client):
    client.post("/api/outlook/import", headers=AUTH, json={"source": "outlook", "text": OUTLOOK_LINES})

    single = client.post("/api/outlook/status", headers=AUTH,
                         json={"email": "a@x.com", "status": "used", "source": "outlook"})
    assert single.get_json() == {"ok": True}

    bad = client.post("/api/outlook/status", headers=AUTH, json={"email": "a@x.com", "status": "nope"})
    assert bad.status_code == 400

    bulk = client.post("/api/outlook/status-bulk", headers=AUTH,
                       json={"source": "outlook", "status": "failed", "items": ["a@x.com", "b@x.com", "a@x.com"]})
    body = bulk.get_json()
    assert body["ok"] is True
    # 重复项只算一次
    assert body["updated_count"] == 2
    assert {item["email"] for item in body["updated"]} == {"a@x.com", "b@x.com"}


def test_outlook_bulk_limits_and_validation(client):
    too_many = client.post("/api/outlook/status-bulk", headers=AUTH,
                           json={"status": "used", "items": ["x%d@y.com" % i for i in range(5001)]})
    assert too_many.status_code == 400

    empty = client.post("/api/outlook/status-bulk", headers=AUTH, json={"status": "used", "items": []})
    assert empty.status_code == 400


def test_outlook_delete_bulk(client):
    client.post("/api/outlook/import", headers=AUTH, json={"source": "outlook", "text": OUTLOOK_LINES})
    resp = client.post("/api/outlook/delete-bulk", headers=AUTH,
                       json={"source": "outlook", "items": ["a@x.com", {"email": "b@x.com"}]})
    body = resp.get_json()
    assert body["deleted_count"] == 2
    assert client.get("/api/outlook?source=outlook", headers=AUTH).get_json() == []

    again = client.post("/api/outlook/delete-bulk", headers=AUTH, json={"source": "outlook", "items": ["a@x.com"]})
    assert again.get_json()["skipped"][0]["reason"] == "邮箱不存在"


def test_domain_pool_routes(client):
    assert client.get("/api/domain-pool", headers=AUTH).get_json() == []
    bad = client.post("/api/domain-pool/status", headers=AUTH, json={"email": "z@y.com", "status": "nope"})
    assert bad.status_code == 400
    ok = client.post("/api/domain-pool/status", headers=AUTH, json={"email": "z@y.com", "status": "used"})
    assert ok.get_json() == {"ok": True}
    deleted = client.post("/api/domain-pool/delete", headers=AUTH, json={"email": "z@y.com"})
    assert deleted.get_json() == {"ok": True, "deleted": False}


def test_invalid_email_source_is_rejected(client):
    resp = client.post("/api/outlook/delete", headers=AUTH, json={"email": "a@x.com", "source": "不存在的来源"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "邮箱来源非法"


def test_pool_list_does_not_carry_account_secrets(client, isolated_db):
    """池子行只带账号 id 与「有没有」标记，不随列表下发所关联账号的凭证。

    这条用例必须真的挂一个同邮箱账号，否则断言全是空转。
    """
    db = isolated_db
    client.post("/api/outlook/import", headers=AUTH, json={"source": "outlook", "text": OUTLOOK_LINES})
    db.insert_account(
        email="a@x.com",
        access_token="tok-aaa",
        totp_secret="JBSWY3DPEHPK3PXP",
        email_source="outlook",
        extra={"registration_password": "pw1"},
    )

    rows = client.get("/api/outlook?source=outlook", headers=AUTH).get_json()
    linked = next(item for item in rows if item["email"] == "a@x.com")
    assert linked["registered_account_id"]
    assert linked["has_access_token"] is True
    assert linked["totp_enabled"] is True
    for secret in ("access_token", "access_token_preview", "totp_secret", "account_copy_line"):
        assert secret not in linked, f"池子列表不该下发 {secret}"

    # 池子自身的导入素材照旧下发：这个页面本来就是拿来导出这些的。
    assert linked["copy_line"].startswith("a@x.com----pw1")

    # 账号凭证按需取，仍然拿得到。
    secret = client.get(
        f"/api/accounts/{linked['registered_account_id']}/secret?field=access_token", headers=AUTH
    )
    assert secret.get_json()["value"] == "tok-aaa"


def test_pool_list_strips_legacy_credential_copies(client, isolated_db):
    """老库里池子 payload 存过账号凭证副本：列表必须剥掉，并转成「有没有」标记。"""
    db = isolated_db
    client.post("/api/outlook/import", headers=AUTH, json={"source": "outlook", "text": OUTLOOK_LINES})
    with db._conn_scope() as conn, conn:
        row = conn.execute("SELECT payload FROM email_pool WHERE email='a@x.com'").fetchone()
        payload = json.loads(row["payload"])
        payload["access_token"] = "legacy-tok"
        payload["access_token_preview"] = "legacy-tok..."
        payload["totp_secret"] = "LEGACYSECRET"
        payload["account_copy_line"] = "a@x.com----legacy"
        conn.execute(
            "UPDATE email_pool SET payload=? WHERE email='a@x.com'",
            (json.dumps(payload, ensure_ascii=False),),
        )

    rows = client.get("/api/outlook?source=outlook", headers=AUTH).get_json()
    item = next(r for r in rows if r["email"] == "a@x.com")
    for leaked in ("access_token", "access_token_preview", "totp_secret", "account_copy_line"):
        assert leaked not in item, f"老库副本 {leaked} 不该随列表下发"
    assert item["has_access_token"] is True
    assert item["totp_enabled"] is True
