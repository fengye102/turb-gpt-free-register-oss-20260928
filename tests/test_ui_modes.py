# -*- coding: utf-8 -*-
"""legacy 界面的冻结契约：页面自带声明，且首次使用时只提醒一次。"""
from __future__ import annotations

import logging

import pytest

pytest.importorskip("pyotp", reason="WebUI 依赖 pyotp；CI 会装 requirements.txt")

AUTH = {"X-Auth-Code": "test-code"}


@pytest.fixture()
def client(isolated_db):
    from webui.app import create_app

    app = create_app(auth_code="test-code")
    with app.test_client() as test_client:
        yield test_client


def test_legacy_page_carries_frozen_notice(client):
    resp = client.get("/?ui=legacy", headers=AUTH)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "冻结界面" in body, "legacy 模板顶部应有冻结声明"
    assert "index_legacy.html" in body


def test_modern_page_has_no_frozen_notice(client):
    body = client.get("/?ui=modern", headers=AUTH).get_data(as_text=True)
    assert "冻结界面" not in body


def test_legacy_warning_logs_only_once(client, caplog):
    import webui.app as app_module

    app_module._LEGACY_UI_WARNED = False
    with caplog.at_level(logging.WARNING, logger="webui.app"):
        client.get("/?ui=legacy", headers=AUTH)
        client.get("/?ui=legacy", headers=AUTH)
    hits = [r for r in caplog.records if "legacy 界面" in r.getMessage()]
    assert len(hits) == 1, f"提醒应当只打一次，实际 {len(hits)} 次"
