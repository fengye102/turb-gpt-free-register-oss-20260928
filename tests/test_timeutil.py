# -*- coding: utf-8 -*-
"""统一时间基准：落库 UTC，旧的无时区字符串按本地解释。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def test_utc_now_iso_is_offset_aware():
    from core import timeutil

    parsed = datetime.fromisoformat(timeutil.utc_now_iso())
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert timeutil.utc_now().tzinfo is timezone.utc


def test_to_utc_accepts_datetime():
    from core import timeutil

    aware = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert timeutil.to_utc(aware) == aware
    naive = datetime(2026, 1, 2, 3, 4, 5)
    # 无时区按本地解释，转成 UTC 后必然是同一时刻
    assert timeutil.to_utc(naive).timestamp() == pytest.approx(naive.timestamp())


def test_to_utc_accepts_strings():
    from core import timeutil

    assert timeutil.to_utc("2026-01-02T03:04:05+00:00") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert timeutil.to_utc("2026-01-02T03:04:05Z") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert timeutil.to_utc("2026-01-02T03:04:05").timestamp() == pytest.approx(naive.timestamp())


def test_to_utc_accepts_epoch():
    from core import timeutil

    seconds = 1767000000
    assert timeutil.to_utc(seconds) == datetime.fromtimestamp(seconds, tz=timezone.utc)
    assert timeutil.to_utc(seconds * 1000) == datetime.fromtimestamp(seconds, tz=timezone.utc)


def test_to_utc_rejects_garbage():
    from core import timeutil

    for bad in ("", None, "不是时间"):
        with pytest.raises(ValueError):
            timeutil.to_utc(bad)


def test_seconds_since_handles_both_shapes():
    from core import timeutil

    fresh = timeutil.utc_now_iso()
    legacy = datetime.now().isoformat(timespec="seconds")
    for stamp in (fresh, legacy):
        assert 0 <= timeutil.seconds_since(stamp) < 10, stamp
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    assert 7100 <= timeutil.seconds_since(old) <= 7300


def test_db_delegates_instead_of_reimplementing(isolated_db, monkeypatch):
    """db 里的 _now / _elapsed_seconds 必须是委托，不能自己再写一份。"""
    db = isolated_db

    monkeypatch.setattr(db, "utc_now_iso", lambda *a, **k: "MARKER")
    assert db._now() == "MARKER"
    monkeypatch.setattr(db, "seconds_since", lambda value: 42.0)
    assert db._elapsed_seconds("随便什么") == 42.0
