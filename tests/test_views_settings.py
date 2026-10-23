# -*- coding: utf-8 -*-
"""配置读写 + Roxy 辅助接口 Blueprint。

注意：POST /api/config 会真的写项目根 .env 并热加载，所以这里把 env 路径指到 tmp_path，
并且只测「非法 key 被忽略」这条不会落盘的路径。
"""
from __future__ import annotations

import pytest

pytest.importorskip("pyotp", reason="WebUI 依赖 pyotp；CI 会装 requirements.txt")

AUTH = {"X-Auth-Code": "test-code", "Accept-Encoding": "identity"}


@pytest.fixture()
def client(isolated_db, tmp_path, monkeypatch):
    from config import env_loader

    monkeypatch.setattr(env_loader, "_ENV_PATH", tmp_path / ".env")
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    with app.test_client() as test_client:
        yield test_client


def test_settings_and_roxy_endpoints_registered():
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    settings = {n for n in app.view_functions if n.startswith("settings.")}
    roxy = {n for n in app.view_functions if n.startswith("roxy.")}
    assert settings == {
        "settings.api_config_get",
        "settings.api_config_set",
        "settings.api_cloudmail_gen_token",
        "settings.api_cloudmail_domains",
    }
    assert roxy == {"roxy.api_roxy_workspaces"}


def test_config_routes_require_auth(client):
    assert client.get("/api/config").status_code == 401
    assert client.post("/api/cloudmail/gen-token", json={}).status_code == 401
    assert client.get("/api/roxy/workspaces").status_code == 401


def test_config_get_returns_whitelist(client):
    body = client.get("/api/config", headers=AUTH).get_json()
    assert isinstance(body, list) and len(body) > 50
    keys = {field["key"] for field in body}
    assert "MAIL_TLS_VERIFY" in keys
    assert "WEBUI_AUTH_CODE" in keys
    assert "PROXY_POOL" in keys
    # 协议级常量不该暴露给前端
    assert "CLIENT_ID" not in keys


def test_config_get_returns_secret_values_only_behind_auth(client):
    """/api/config 会把 secret 字段的当前值一并下发（WebUI 要能回显/编辑）。

    这是有意的设计取舍：控制台本来就是「本机、单用户、拿着明文凭据干活」的工具。
    但因此它必须被授权码挡住 —— 上面的 test_config_routes_require_auth 就是这条底线。
    """
    body = client.get("/api/config", headers=AUTH).get_json()
    secret_fields = [f for f in body if f.get("secret")]
    assert secret_fields, "应该存在被标记为 secret 的字段"
    assert all("value" in f for f in secret_fields)


def test_config_set_ignores_unknown_key(client):
    # updates 是 {key: value} 字典（不是数组）；未知 key 只进 ignored，不落盘。
    resp = client.post("/api/config", headers=AUTH, json={"updates": {"NOPE_KEY": "x"}})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "NOPE_KEY" in body["ignored"]


def test_cloudmail_requires_admin_email(client):
    resp = client.post("/api/cloudmail/gen-token", headers=AUTH, json={})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
