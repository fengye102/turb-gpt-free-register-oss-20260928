# -*- coding: utf-8 -*-
"""「零元提链」Blueprint：可选组件，缺件时必须降级成 503 而不是让 WebUI 起不来。

这是当初整个 WebUI ImportError 起不来的元凶，所以单独钉一遍。
"""
from __future__ import annotations

import pytest

pytest.importorskip("pyotp", reason="WebUI 依赖 pyotp；CI 会装 requirements.txt")

AUTH = {"X-Auth-Code": "test-code"}
UPI_ROUTES = [
    ("get", "/api/upi-zero/candidates"),
    ("get", "/api/upi-zero/live-log"),
    ("get", "/api/upi-zero/status/1"),
    ("get", "/api/upi-zero/log/1"),
]


@pytest.fixture()
def client(isolated_db):
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    with app.test_client() as test_client:
        yield test_client


def test_upi_endpoints_registered_even_when_component_missing():
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    endpoints = {n for n in app.view_functions if n.startswith("upi.")}
    assert endpoints == {
        "upi.api_upi_zero_candidates",
        "upi.api_upi_zero_extract",
        "upi.api_upi_zero_status",
        "upi.api_upi_zero_log",
        "upi.api_upi_zero_live_log",
    }


def test_upi_flag_is_reexported_for_compat():
    """app 模块仍要暴露 _UPI_ZERO_AVAILABLE（老代码/冒烟用例读它）。"""
    from webui import app as app_module

    assert hasattr(app_module, "_UPI_ZERO_AVAILABLE")


def test_upi_routes_require_auth(client):
    for method, path in UPI_ROUTES:
        assert getattr(client, method)(path).status_code == 401


@pytest.mark.parametrize("method,path", UPI_ROUTES)
def test_upi_routes_degrade_to_503(client, method, path):
    from webui.views import upi as upi_module

    if upi_module.UPI_ZERO_AVAILABLE:
        pytest.skip("本机包含提链组件")
    resp = getattr(client, method)(path, headers=AUTH)
    assert resp.status_code == 503
    assert "零元提链组件" in resp.get_json()["error"]


def test_upi_extract_degrades_to_503(client):
    from webui.views import upi as upi_module

    if upi_module.UPI_ZERO_AVAILABLE:
        pytest.skip("本机包含提链组件")
    resp = client.post("/api/upi-zero/extract", headers=AUTH, json={"account_id": 1})
    assert resp.status_code == 503
