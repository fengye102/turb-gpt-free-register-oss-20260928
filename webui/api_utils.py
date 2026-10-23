# -*- coding: utf-8 -*-
"""WebUI 的无状态小工具。

放在这里而不是塞进 `app.py`，是为了能在**不导入 Flask 应用**（那会连带
pyotp / 注册服务等一堆重依赖）的前提下做单元测试。
"""
from __future__ import annotations

import threading
import time
import uuid


class PreparedDownloads:
    """限时 + 限量的一次性下载缓存：`/api/downloads/<id>` 取走即删。

    两件事必须自带：
    - 锁：`app.run(threaded=True)` 下请求线程会同时读写同一份字典；
    - 上限：批量导出几千个账号的 zip 有几 MB 到几十 MB，只清过期项挡不住内存堆积，
      超限时按最久未使用淘汰。
    """

    def __init__(self, ttl: float = 600, limit: int = 24) -> None:
        self._ttl = float(ttl)
        self._limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    def put(self, content: bytes, filename: str, mimetype: str = "application/zip") -> str:
        """存入内容并返回一次性 download_id。"""
        now = time.time()
        download_id = uuid.uuid4().hex
        entry = {
            "content": bytes(content),
            "filename": filename,
            "mimetype": mimetype,
            "created_at": now,
        }
        with self._lock:
            self._sweep(now)
            while len(self._items) >= self._limit:
                oldest = min(self._items, key=lambda k: float(self._items[k].get("created_at") or 0))
                self._items.pop(oldest, None)
            self._items[download_id] = entry
        return download_id

    def take(self, download_id: str) -> dict | None:
        """取出并删除；不存在（或已过期被清）返回 None。"""
        with self._lock:
            return self._items.pop(str(download_id or ""), None)

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def _sweep(self, now: float) -> None:
        for key, value in list(self._items.items()):
            if now - float(value.get("created_at") or 0) > self._ttl:
                self._items.pop(key, None)
