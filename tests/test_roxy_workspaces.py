# -*- coding: utf-8 -*-
"""工作区探测：连接层失败要立刻放弃、成功过的路径要记住、整体要有预算。"""
from __future__ import annotations

from core import roxybrowser_client as roxy


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.text = "{}"

    def json(self):
        return self._payload


class FakeHttp:
    """记录每次调用（method, path, timeout）；handler 决定返回对象还是抛异常。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, json=None, timeout=None):
        path = url.split("//", 1)[-1].split("/", 1)[-1] if "//" in url else url
        self.calls.append((method, path, timeout))
        result = self.handler(method, path, len(self.calls))
        if isinstance(result, Exception):
            raise result
        return result


def make_client(handler, monkeypatch):
    monkeypatch.setattr(roxy, "_WORKSPACE_PATH_CACHE", {})
    client = roxy.RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="token")
    client.http = FakeHttp(handler)
    return client


def test_service_down_fails_fast(monkeypatch):
    """Roxy 没起来时，不能把 24 条候选乘上每条 3 次重试全试一遍。"""
    client = make_client(
        lambda method, path, n: roxy.requests.exceptions.ConnectionError("Connection refused"),
        monkeypatch,
    )
    result = client.list_workspaces()
    assert result["ok"] is False
    assert result.get("service_down") is True
    assert len(client.http.calls) == 1, client.http.calls
    assert any("未响应" in str(item.get("error", "")) for item in result["errors"])


def test_probe_uses_short_timeout(monkeypatch):
    """探测要用短超时，而不是业务请求那个 ROXY_SELENIUM_TIMEOUT（默认 90s）。"""
    client = make_client(lambda method, path, n: FakeResponse({}), monkeypatch)
    monkeypatch.setattr(client, "_extract_workspace_items", lambda payload: [])
    client.list_workspaces(budget_seconds=3)
    assert client.http.calls
    for _method, _path, timeout in client.http.calls:
        assert timeout is not None and timeout <= roxy.WORKSPACE_PROBE_TIMEOUT_SECONDS


def test_resolved_path_is_remembered(monkeypatch):
    """第一轮探出来的路径要记住，第二轮直接命中，不再从头踩一遍候选表。"""

    def handler(method, path, n):
        if method == "GET" and path.endswith("/team/list"):
            return FakeResponse({"hit": True})
        return FakeResponse({})

    client = make_client(handler, monkeypatch)
    monkeypatch.setattr(
        client,
        "_extract_workspace_items",
        lambda payload: [{"id": 1, "name": "技能组"}] if payload.get("hit") else [],
    )
    first = client.list_workspaces()
    assert first["ok"] is True
    assert first["path"].endswith("/team/list")
    assert len(client.http.calls) > 1, "第一轮应该探测了不止一条候选"

    client.http.calls.clear()
    second = client.list_workspaces()
    assert second["ok"] is True
    assert second["path"] == first["path"]
    assert len(client.http.calls) == 1, client.http.calls


def test_probe_respects_total_budget(monkeypatch):
    """预算用完就停，不能把 24 条候选全跑一遍。"""

    def handler(method, path, n):
        roxy.time.sleep(0.4)
        return FakeResponse({})

    client = make_client(handler, monkeypatch)
    monkeypatch.setattr(client, "_extract_workspace_items", lambda payload: [])
    result = client.list_workspaces(budget_seconds=1.0)
    assert result["ok"] is False
    assert result.get("timed_out") is True
    assert len(client.http.calls) <= 4, client.http.calls


def test_unlimited_api_shortcut_skips_probe(monkeypatch):
    """本地无限窗口 API 不走服务端，不该触发任何探测。"""
    monkeypatch.setattr(roxy, "_is_unlimited_api", lambda: True)
    client = make_client(lambda method, path, n: FakeResponse({}), monkeypatch)
    result = client.list_workspaces()
    assert result["ok"] is True
    assert client.http.calls == []
