# -*- coding: utf-8 -*-
"""cookie 落盘工具（补齐的 tools/export_session_cookies.py）。"""
from __future__ import annotations

import json

from tools.export_session_cookies import cookie_header, normalize_cookie, safe_filename, write_outputs

SELENIUM_COOKIE = {
    "name": "__Secure-next-auth.session-token",
    "value": "eyJhbGciOi.payload.sig",
    "domain": ".chatgpt.com",
    "path": "/",
    "expiry": 1799999999.0,
    "httpOnly": True,
    "secure": True,
    "sameSite": "Lax",
}
PLAYWRIGHT_COOKIE = {
    "name": "oai-did",
    "value": "abc-123",
    "domain": ".openai.com",
    "path": "/",
    "expires": 1799999999,
}


def test_safe_filename_blocks_traversal():
    assert "/" not in safe_filename("../evil/x@y.com")
    assert "\\" not in safe_filename("..\\evil")
    assert safe_filename("") == "unknown"


def test_normalize_unifies_selenium_and_playwright():
    a = normalize_cookie(SELENIUM_COOKIE)
    b = normalize_cookie(PLAYWRIGHT_COOKIE)
    assert a["expires"] == 1799999999.0
    assert b["expires"] == 1799999999.0
    assert a["httpOnly"] is True and b["httpOnly"] is False
    assert normalize_cookie({"name": ""}) is None
    assert normalize_cookie("not-a-dict") is None


def test_write_outputs_round_trip(tmp_path):
    result = write_outputs(
        "user+tag@example.com",
        [SELENIUM_COOKIE, PLAYWRIGHT_COOKIE, {"name": ""}],
        "access-token-1",
        out_dir=tmp_path,
    )
    assert result["count"] == 2
    assert result["has_session"] is True

    payload = json.loads((tmp_path / f"{safe_filename('user+tag@example.com')}.json").read_text(encoding="utf-8"))
    assert payload["access_token"] == "access-token-1"
    assert payload["cookie_count"] == 2
    assert "session-token=" in payload["cookie_header"]

    line = (tmp_path / f"{safe_filename('user+tag@example.com')}.txt").read_text(encoding="utf-8")
    assert line.startswith("user+tag@example.com----access-token-1----")


def test_cookie_header_format():
    assert cookie_header([{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]) == "a=1; b=2"
    assert cookie_header([]) == ""
