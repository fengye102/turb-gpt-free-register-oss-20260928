# -*- coding: utf-8 -*-
"""WebUI 装配冒烟：路由注册、鉴权拦截、缺件组件的降级应答。"""
from __future__ import annotations

import pytest

pytest.importorskip("pyotp", reason="WebUI 依赖 pyotp；CI 会装 requirements.txt")


@pytest.fixture()
def app():
    from webui.app import create_app

    return create_app(auth_code="test-code")


def test_html_carries_security_headers(app):
    """页面要有 CSP：connect-src 'self' 是「token 偷不走」这条底线的兜底。"""
    resp = app.test_client().get("/", headers={"X-Auth-Code": "test-code"})
    assert resp.status_code == 200
    csp = resp.headers["Content-Security-Policy"]
    assert "connect-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_json_api_does_not_get_html_headers(app):
    resp = app.test_client().get("/api/summary", headers={"X-Auth-Code": "test-code"})
    assert resp.status_code == 200
    assert "Content-Security-Policy" not in resp.headers


def test_routes_registered(app):
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    for expected in ("/", "/api/summary", "/api/accounts", "/api/jobs", "/api/config"):
        assert expected in rules, f"缺少路由 {expected}"
    assert len(rules) > 50


def test_api_blocked_without_auth(app):
    client = app.test_client()
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", headers={"X-Auth-Code": "test-code"}).status_code == 200


def test_upi_zero_degrades_to_503_when_component_missing(app):
    """提链组件不在分发里时，接口要给出可读的 503，而不是 404/500。"""
    from webui import app as app_module

    if app_module._UPI_ZERO_AVAILABLE:
        pytest.skip("本机包含提链组件")
    client = app.test_client()
    resp = client.get("/api/upi-zero/candidates", headers={"X-Auth-Code": "test-code"})
    assert resp.status_code == 503
    assert "零元提链组件" in resp.get_json()["error"]
