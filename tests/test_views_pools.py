# -*- coding: utf-8 -*-
"""邮箱池 Blueprint：搬出 app.py 之后行为必须和闭包版一致。

这组用例的价值在「搬家不改行为」——pools 域从 create_app() 的闭包变成
webui/views/pools.py 之后，路径、参数、状态码、鉴权都不许变。
"""
from __future__ import annotations

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
    assert [row["email"] for row in items] == ["a@x.com", "b@x.com"]

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
