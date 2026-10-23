# -*- coding: utf-8 -*-
"""等待与退避：夹住 deadline、带抖动、可被停止信号打断。"""
from __future__ import annotations

import pathlib
import threading
import time as real_time

import pytest

from core import waits

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 本轮从裸 time.sleep 迁到 waits 的模块；谁再写回裸 sleep，这条用例会红。
CONVERTED = (
    "core/mailtm_client.py",
    "core/tempmailio_client.py",
    "core/remail_client.py",
    "core/sms_provider.py",
    "core/account_liveness.py",
    "core/cf_temp_mail_client.py",
    "core/chatgpt_plan.py",
    "core/plan_check_service.py",
    "core/codex_agent_service.py",
    "core/promo_check.py",
    "core/email_change_service.py",
    "main.py",
)


class FakeTime:
    """替掉 waits 里的 time 模块，用来数睡了几次、各睡了多久。"""

    def __init__(self):
        self.calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)

    def monotonic(self) -> float:
        return 0.0


def test_poll_delay_never_overshoots_deadline():
    assert waits.poll_delay(3, 10) == 3
    # 剩余不足一个间隔时必须缩到剩余时间，否则总等待会超出 max_wait
    assert waits.poll_delay(3, 0.5) == 0.5
    assert waits.poll_delay(1.5, 0.3, floor=0.5) == 0.3
    assert waits.poll_delay(1.5, 99, floor=0.5) == 1.5


def test_poll_delay_floors_instead_of_busy_loop():
    assert waits.poll_delay(3, 0) == waits.POLL_FLOOR
    assert waits.poll_delay(3, -5) == waits.POLL_FLOOR
    assert waits.poll_delay(0, 10) == 10


def test_backoff_delay_is_linear_capped_and_jittered():
    assert waits.backoff_delay(1, jitter=0) == pytest.approx(1.5)
    assert waits.backoff_delay(3, jitter=0) == pytest.approx(4.5)
    assert waits.backoff_delay(50, jitter=0) == pytest.approx(15.0)
    for _ in range(50):
        low = waits.backoff_delay(4, base=2, cap=100, jitter=0.25)
        assert 8 * 0.75 <= low <= 8 * 1.25


def test_sleep_returns_without_sleeping_for_zero(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr(waits, "time", fake)
    assert waits.sleep(0) == 0.0
    assert waits.sleep(-3) == 0.0
    assert fake.calls == []


def test_sleep_passes_through_when_no_stop_event(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr(waits, "time", fake)
    waits.clear_stop_event()
    assert waits.sleep(1.2) == pytest.approx(1.2)
    assert fake.calls == [pytest.approx(1.2)]


def test_sleep_honours_jitter_bounds(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr(waits, "time", fake)
    for _ in range(50):
        waits.sleep(10, jitter=0.2)
    assert len(fake.calls) == 50
    assert all(8 <= c <= 12 for c in fake.calls)
    assert len(set(fake.calls)) > 1, "抖动应该真的抖动"


def test_stop_event_binding_and_predicate():
    waits.clear_stop_event()
    assert waits.stop_requested() is False
    event = threading.Event()
    waits.bind_stop_event(event)
    try:
        assert waits.stop_requested() is False
        event.set()
        assert waits.stop_requested() is True
    finally:
        waits.clear_stop_event()
    assert waits.stop_requested() is False


def test_sleep_raises_immediately_when_already_stopping(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr(waits, "time", fake)
    event = threading.Event()
    event.set()
    waits.bind_stop_event(event)
    try:
        with pytest.raises(waits.StopRequested):
            waits.sleep(30)
        assert fake.calls == [], "已经在停止就不该再睡"
    finally:
        waits.clear_stop_event()


def test_sleep_is_interrupted_midway():
    """真实计时：睡 5 秒的过程中置位停止事件，必须很快抛出而不是睡满。"""
    event = threading.Event()
    waits.bind_stop_event(event)
    threading.Timer(0.2, event.set).start()
    started = real_time.monotonic()
    try:
        with pytest.raises(waits.StopRequested):
            waits.sleep(5)
        elapsed = real_time.monotonic() - started
        assert elapsed < 2.0, f"打断了却等了 {elapsed:.2f}s"
    finally:
        waits.clear_stop_event()


def test_stop_requested_type_is_shared_with_registration_service():
    """waits.sleep 抛的必须就是任务停止用的那个类型，否则停止会被当成普通异常。

    先查源码里的 re-export（这条在没装 selenium 的环境也跑得起来），
    能 import 到服务时再验一次类型同一性。
    """
    source = (ROOT / "core/registration_service.py").read_bytes().decode("utf-8")
    assert "StopRequested = waits.StopRequested" in source, "re-export 被改掉了"
    try:
        from core import registration_service
    except ImportError:
        return
    assert registration_service.StopRequested is waits.StopRequested
    with pytest.raises(registration_service.StopRequested):
        raise waits.StopRequested("模拟：等待被打断")


def test_no_bare_time_sleep_left_in_converted_modules():
    offenders = []
    for name in CONVERTED:
        text = (ROOT / name).read_bytes().decode("utf-8")
        if "time.sleep(" in text:
            offenders.append(name)
    assert offenders == [], f"这些模块又写回了裸 time.sleep：{offenders}"
