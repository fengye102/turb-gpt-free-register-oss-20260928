# -*- coding: utf-8 -*-
"""WebUI 授权码与接口鉴权。"""
from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from webui import auth


@pytest.fixture()
def client():
    templates = Path(__file__).resolve().parent.parent / "webui" / "templates"
    app = Flask(__name__, template_folder=str(templates))
    app.config.update(TESTING=True)
    auth.init_auth(app, auth_code="correct-horse")

    @app.get("/api/ping")
    def _ping():
        return {"ok": True}

    @app.get("/")
    def _index():
        return "index"

    auth.register_auth_routes(app)
    with app.test_client() as c:
        yield c


def test_code_is_valid_uses_constant_time_compare():
    assert auth.code_is_valid("correct-horse") is False  # 未初始化前不接受任何码
    from flask import Flask as _F

    app = _F(__name__)
    auth.init_auth(app, auth_code="s3cret")
    assert auth.code_is_valid("s3cret") is True
    assert auth.code_is_valid("s3cre") is False
    assert auth.code_is_valid("") is False


def test_api_requires_auth(client):
    resp = client.get("/api/ping")
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_api_accepts_header_code(client):
    resp = client.get("/api/ping", headers={"X-Auth-Code": "correct-horse"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_api_accepts_bearer_code(client):
    resp = client.get("/api/ping", headers={"Authorization": "Bearer correct-horse"})
    assert resp.status_code == 200


def test_login_then_session_authorizes(client):
    resp = client.post("/login", data={"auth_code": "correct-horse"}, follow_redirects=False)
    assert resp.status_code == 302
    assert client.get("/api/ping").status_code == 200
    client.post("/logout")
    assert client.get("/api/ping").status_code == 401


def test_login_rejects_wrong_code(client):
    resp = client.post("/login", data={"auth_code": "nope"})
    assert resp.status_code == 200
    assert "授权码错误" in resp.get_data(as_text=True)


def test_login_blocks_open_redirect(client):
    """next 只能是站内路径，否则登录后会被带去外站。"""
    resp = client.post("/login", data={"auth_code": "correct-horse", "next": "//evil.example.com/"})
    assert resp.status_code == 302
    assert "evil.example.com" not in resp.headers["Location"]
