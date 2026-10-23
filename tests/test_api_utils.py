# -*- coding: utf-8 -*-
"""一次性下载缓存：不丢、不超限、不过期滞留。

这个类曾经是 app.py 里的一个裸 dict —— 定义被挪走却忘了补回来时，
任何下载都会 NameError。所以这里把行为钉死。
"""
from __future__ import annotations

import threading
import time

from webui.api_utils import PreparedDownloads


def test_put_then_take_is_one_shot(tmp_path):
    cache = PreparedDownloads(ttl=60, limit=4)
    download_id = cache.put(b"zip-bytes", "accounts.zip")
    assert cache.count() == 1

    item = cache.take(download_id)
    assert item["content"] == b"zip-bytes"
    assert item["filename"] == "accounts.zip"
    assert item["mimetype"] == "application/zip"
    # 取走即删，第二次必须拿不到
    assert cache.take(download_id) is None
    assert cache.count() == 0


def test_take_unknown_id_returns_none():
    cache = PreparedDownloads()
    assert cache.take("not-a-real-id") is None
    assert cache.take("") is None


def test_limit_evicts_oldest():
    cache = PreparedDownloads(ttl=600, limit=3)
    ids = [cache.put(f"payload-{i}".encode(), f"{i}.zip") for i in range(5)]
    assert cache.count() == 3
    assert cache.take(ids[0]) is None
    assert cache.take(ids[1]) is None
    assert cache.take(ids[4]) is not None


def test_expired_entries_are_swept_on_next_put():
    cache = PreparedDownloads(ttl=0.05, limit=10)
    stale = cache.put(b"old", "old.zip")
    time.sleep(0.08)
    cache.put(b"new", "new.zip")
    assert cache.take(stale) is None
    assert cache.count() == 1


def test_concurrent_put_and_take_is_safe():
    cache = PreparedDownloads(ttl=600, limit=5000)
    produced: list[str] = []
    lock = threading.Lock()

    def producer(n: int) -> None:
        for i in range(40):
            download_id = cache.put(f"p{n}-{i}".encode(), f"{n}-{i}.zip")
            with lock:
                produced.append(download_id)

    def consumer() -> None:
        deadline = time.time() + 2
        while time.time() < deadline:
            with lock:
                batch = produced[:20]
                del produced[:20]
            for download_id in batch:
                cache.take(download_id)

    threads = [threading.Thread(target=producer, args=(n,)) for n in range(4)]
    threads.append(threading.Thread(target=consumer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for download_id in produced:
        cache.take(download_id)
    assert cache.count() == 0
