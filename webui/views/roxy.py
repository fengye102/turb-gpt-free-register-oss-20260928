# -*- coding: utf-8 -*-
"""RoxyBrowser 辅助接口（当前只有「列出团队/工作区」）。

搬出 app.py 是为了让「探测 Roxy 本地 API」这条容易卡住的路径独立出来，
以后要加超时或换探测策略时改动面小。
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify

bp = Blueprint("roxy", __name__)
logger = logging.getLogger(__name__)


# ----------------------------------------------------------
# RoxyBrowser 辅助接口
# ----------------------------------------------------------
@bp.get("/api/roxy/workspaces")
def api_roxy_workspaces():
    try:
        from core.roxybrowser_client import RoxyBrowserClient
        result = RoxyBrowserClient().list_workspaces()
        return jsonify(result)
    except Exception as exc:
        logger.exception("获取 Roxy 团队/工作区失败")
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
