# -*- coding: utf-8 -*-
"""统一时间基准：落库一律 UTC 带偏移，展示由前端按本地时区渲染。

为什么单独开一个模块：改动前 datetime.now() 散落在 8 个服务里，落库的是裸本地
时间字符串——没有偏移量，跨时区迁移、夏令时切换、把库拷到别的机器再看，都会算错
「排了多久 / 超时没超时」。而且每个文件各写一遍，修一个漏一个。这里收成一份，
core/db.py 也改用同一份实现，全仓只有一个时间真相。

注意区分：落库与比较用本模块；给人看的日志行（形如 23:57:54）继续用本地时间，
用户读日志时按本地钟表对上才有意义。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = ["utc_now", "utc_now_iso", "to_utc", "seconds_since"]


def utc_now() -> datetime:
    """当前 UTC 时刻（带时区信息）。"""
    return datetime.now(timezone.utc)


def utc_now_iso(timespec: str = "seconds") -> str:
    """当前 UTC 时刻的 ISO 字符串，形如 2026-10-23T15:52:31+00:00。"""
    return utc_now().isoformat(timespec=timespec)


def to_utc(value: Any) -> datetime:
    """把各历史形状的时间值统一成带时区的 UTC datetime。

    接受 datetime、ISO 字符串、epoch 秒/毫秒。
    不带时区的老数据按本地时区解释：它们本来就是 datetime.now() 写下的本地时间，
    按本地补全才不会把历史行的「排了多久」算歪。解析不出来就抛 ValueError/TypeError，
    由调用方原有的 except 分支兜底。
    """
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e11:
            seconds = seconds / 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("空时间戳")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp.astimezone(timezone.utc)


def seconds_since(value: Any) -> float:
    """返回 value 距今的秒数，正数表示已经过去。"""
    return (utc_now() - to_utc(value)).total_seconds()
