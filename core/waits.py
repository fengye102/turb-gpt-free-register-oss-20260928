# -*- coding: utf-8 -*-
"""等待、轮询间隔与重试退避的统一实现，附带「停止信号可打断」。

改动前全仓 183 处 time.sleep 散在 30 多个文件里：字面量各写各的（0.5 / 1 / 1.5 / 2 / 5 …）、
重试退避是 1.5*attempt 这种裸线性式且没有抖动、轮询间隔不夹剩余时间 —— 最后一次循环会睡过头，
把 max_wait 拖长一整个 interval。SQLite 那边的排队超时判定是按秒算的，这属于真会咬人的偏差。

这里收口三件事：
    1. sleep()：可抖动（多账号打同一个邮箱/短信平台时别同步发作）、可被停止信号打断。
    2. poll_delay()：把轮询间隔夹进剩余时间里，保证总等待不超预算。
    3. backoff_delay()：线性退避 + 上限 + 抖动。

关于打断：停止事件按线程绑定（bind_stop_event），job 线程启动时注册、结束时清除。
绑了之后睡眠按 0.25s 切片检查，用户一按停止就抛 StopRequested —— 不用等 OTP 轮询
（默认 180s）或退避整段睡完才响应。
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any

__all__ = [
    "StopRequested",
    "OTP_RESEND_SETTLE",
    "POLL_FLOOR",
    "STOP_SLICE",
    "sleep",
    "poll_delay",
    "backoff_delay",
    "bind_stop_event",
    "clear_stop_event",
    "bound_stop_event",
    "stop_requested",
]

# 重新发送验证码后到下次取信之间的让步时间（原先散落着 time.sleep(1)）。
OTP_RESEND_SETTLE = 1.0
# 轮询间隔下限：剩余时间归零时也别 0 秒空转打爆对方接口。
POLL_FLOOR = 0.05
# 可打断睡眠的切片粒度：越小越灵敏，越大越省 CPU。
STOP_SLICE = 0.25


class StopRequested(RuntimeError):
    """用户手动停止：等待被停止信号打断时抛出。

    与 core/registration_service.py 里的同名异常是**同一个类型**（那里做了 re-export），
    所以各处已有的 except StopRequested 分支不用为「可打断睡眠」再改一遍。
    """


_TLS = threading.local()


def bind_stop_event(event: Any) -> None:
    """把停止事件绑定到当前线程；此后本线程的 sleep() 会响应它。"""
    _TLS.stop_event = event


def clear_stop_event() -> None:
    """解除当前线程的绑定（job 结束时调用）。"""
    _TLS.__dict__.pop("stop_event", None)


def bound_stop_event() -> Any:
    """当前线程绑定的停止事件，没有就是 None。"""
    return getattr(_TLS, "stop_event", None)


def stop_requested() -> bool:
    """当前线程绑定的停止事件是否已置位；没绑定就是 False。"""
    event = bound_stop_event()
    return bool(event is not None and event.is_set())


def sleep(seconds: float, *, jitter: float = 0.0) -> float:
    """睡 seconds 秒，返回实际睡了的秒数。

    jitter 是上下抖动比例（0.25 表示 ±25%）；被停止信号打断时抛 StopRequested。
    """
    if stop_requested():
        raise StopRequested("等待被用户停止打断")
    total = max(0.0, float(seconds or 0.0))
    if jitter and total > 0:
        total = max(0.0, total * (1.0 + random.uniform(-abs(jitter), abs(jitter))))
    if total <= 0:
        return 0.0
    if bound_stop_event() is None:
        time.sleep(total)
        return total
    slept = 0.0
    while slept < total:
        if stop_requested():
            raise StopRequested("等待被用户停止打断")
        chunk = min(STOP_SLICE, total - slept)
        time.sleep(chunk)
        slept += chunk
    return slept


def poll_delay(interval: float, remaining: float, *, floor: float = POLL_FLOOR, ceil: float | None = None) -> float:
    """轮询间隔夹进剩余时间：不会睡过 deadline，也不会 0 秒空转。

    remaining <= 0 时返回 floor，给最后一轮留一点点让步，避免 while 空转。
    """
    step = max(0.0, float(interval or 0.0))
    if ceil is not None:
        step = min(step, max(0.0, float(ceil)))
    left = max(0.0, float(remaining or 0.0))
    delay = min(step, left) if step > 0 else left
    if delay <= 0:
        delay = min(float(floor), left) if left > 0 else float(floor)
    return max(0.0, delay)


def backoff_delay(attempt: int, *, base: float = 1.5, cap: float = 15.0, jitter: float = 0.3) -> float:
    """第 attempt 次重试前该等多久：随次数线性增长、封顶、带抖动。

    原来是 1.5*attempt 这种裸线性式：既没有上限（重试越多等越久），也没有抖动。
    """
    step = max(1, int(attempt or 1))
    delay = min(float(cap), float(base) * float(step))
    if jitter and delay > 0:
        delay = delay * (1.0 + random.uniform(-abs(float(jitter)), abs(float(jitter))))
    return max(0.0, delay)
