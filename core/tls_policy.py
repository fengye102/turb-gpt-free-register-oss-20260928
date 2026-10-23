# -*- coding: utf-8 -*-
"""邮箱 / flow 网关的 TLS 校验策略。

这些自建网关的调用点以前一律写死 verify=False（当年图省事）。代价是 OTP 取码通道
可以被中间人替换：验证码是整条注册链路里最容易被劫持、也最值钱的一环，不该默认关着。

现在统一走这里：默认开启校验，只有确实用自签证书的网关才在 .env 里
MAIL_TLS_VERIFY=False 显式关掉，且关掉时打一次 warning 留痕。
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_WARNED = False


def verify() -> bool:
    """当前是否校验 TLS 证书。读配置失败按「校验」处理（安全默认）。"""
    enabled = True
    try:
        from config import email as _email_cfg

        enabled = bool(getattr(_email_cfg, "MAIL_TLS_VERIFY", True))
    except Exception:
        enabled = True
    if not enabled:
        _warn_once()
    return enabled


def _warn_once() -> None:
    """关闭校验只提示一次，避免刷屏。"""
    global _WARNED
    with _LOCK:
        if _WARNED:
            return
        _WARNED = True
    logger.warning(
        "MAIL_TLS_VERIFY=False：邮箱/flow 网关的 TLS 证书校验已关闭，"
        "取码通道存在被中间人替换的风险"
    )
