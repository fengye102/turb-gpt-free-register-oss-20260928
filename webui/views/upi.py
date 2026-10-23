# -*- coding: utf-8 -*-
"""「零元提链」API（可选组件）。

core/upi_zero_web_service.py 与 core/upi_live_feed.py 不在当前源码分发里，
所以这两个模块在这里可选导入：缺件时 Blueprint 照样注册，每个接口返回 503 并说明缺哪个文件，
而不是让整个 WebUI 起不来。
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from core import db

logger = logging.getLogger(__name__)

try:
    from core import upi_live_feed, upi_zero_web_service

    UPI_ZERO_AVAILABLE = True
except ImportError:
    upi_live_feed = None
    upi_zero_web_service = None
    UPI_ZERO_AVAILABLE = False
    logger.warning(
        "零元提链组件缺失（core/upi_zero_web_service.py / core/upi_live_feed.py），相关接口将返回 503"
    )

bp = Blueprint("upi", __name__)



def _upi_zero_unavailable():
    """提链组件缺件时的统一应答，避免前端只拿到 404 无从判断。"""
    return jsonify({
        "ok": False,
        "error": "零元提链组件未包含在当前源码分发中"
                 "（缺少 core/upi_zero_web_service.py 或 core/upi_live_feed.py）",
    }), 503

@bp.get("/api/upi-zero/candidates")
def api_upi_zero_candidates():
    if not UPI_ZERO_AVAILABLE:
        return _upi_zero_unavailable()
    try:
        page = int(request.args.get("page", "1"))
        page_size = int(request.args.get("page_size", "50"))
        if page < 1 or page_size < 1:
            raise ValueError
    except ValueError:
        return jsonify({"ok": False, "error": "分页参数无效"}), 400
    response = jsonify(upi_zero_web_service.candidates(
        page=page, page_size=page_size, q=(request.args.get("q") or "")[:200]))
    response.headers["Cache-Control"] = "no-store"
    return response

@bp.post("/api/upi-zero/extract")
def api_upi_zero_extract():
    if not UPI_ZERO_AVAILABLE:
        return _upi_zero_unavailable()
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "请求体无效"}), 400
    try:
        raw_id = data.get("account_id")
        if isinstance(raw_id, bool) or str(raw_id).strip() != str(int(raw_id)):
            raise ValueError
        account_id = int(raw_id)
        if account_id < 1:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "account_id 无效"}), 400
    try:
        result = upi_zero_web_service.enqueue(account_id, proxy=data.get("proxy"), tries=data.get("tries"),
                                              retry_uncertain=data.get("retry_uncertain", False))
    except LookupError:
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not result.get("ok"):
        return jsonify(result), 409 if result.get("busy") else 503
    return jsonify(result), 202

@bp.get("/api/upi-zero/status/<int:account_id>")
def api_upi_zero_status(account_id: int):
    if not UPI_ZERO_AVAILABLE:
        return _upi_zero_unavailable()
    if account_id not in upi_zero_web_service.source_ids():
        return jsonify({"ok": False, "error": "账号不在零元池"}), 404
    acc = db.get_account(account_id)
    if not acc:
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    response = jsonify({"ok": True, **upi_zero_web_service.public_status(acc)})
    response.headers["Cache-Control"] = "no-store"
    return response

@bp.get("/api/upi-zero/log/<int:account_id>")
def api_upi_zero_log(account_id: int):
    if not UPI_ZERO_AVAILABLE:
        return _upi_zero_unavailable()
    try:
        after = int(request.args.get("after", "0"))
        if after < 0:
            raise ValueError
    except ValueError:
        return jsonify({"ok": False, "error": "after 参数无效"}), 400
    if account_id not in upi_zero_web_service.source_ids():
        return jsonify({"ok": False, "error": "账号不在零元池"}), 404
    acc = db.get_account(account_id)
    if not acc:
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    snapshot = upi_zero_web_service.log_events(account_id, after=after)
    response = jsonify({"ok": True, "account_id": account_id, **snapshot,
                        "status": upi_zero_web_service.public_status(acc)["extract_link_status"]})
    response.headers["Cache-Control"] = "no-store"
    return response

@bp.get("/api/upi-zero/live-log")
def api_upi_zero_live_log():
    """全池提链的实时日志尾巴：读 sweep 自己的日志文件，增量返回。"""
    if not UPI_ZERO_AVAILABLE:
        return _upi_zero_unavailable()
    try:
        after = int(request.args.get("after", "0"))
        tail = int(request.args.get("tail", "0"))
        if after < 0 or tail < 0:
            raise ValueError
    except ValueError:
        return jsonify({"ok": False, "error": "after/tail 参数无效"}), 400
    try:
        payload = upi_live_feed.read(source=(request.args.get("source") or "")[:64],
                                     after=after, tail=tail)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response
