# -*- coding: utf-8 -*-
"""
Flask 本地控制台。

复用现有后端：
    core.db                     —— 账号 / 邮箱池 / 任务的 SQLite 持久化与查询
    core.registration_service   —— 线程池批量注册 + 任务日志
    webui.config_editor         —— 安全读写 config/*.py

所有接口返回 JSON；前端是单文件 templates/index.html（原生 JS + fetch）。
默认绑定 127.0.0.1，仅本地访问。
"""
import logging
import gzip
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, make_response, render_template, request
import pyotp

from core import (codex_retry_service, db, plan_check_service, codex_agent_service, live_check_service)
from webui.api_utils import (
    POOL_SOURCE_VALUES,
    PreparedDownloads,
    account_secret_value,
    compact_account_for_list,
    compact_job_for_list,
    job_status_counts,
    matches_query,
    paginate_items,
    pool_source_arg,
    read_log_tail,
    with_pool_source,
)
from webui.auth import init_auth, register_auth_routes
from webui.views import pools as views_pools
from webui.views import jobs as views_jobs
from core import registration_service as svc
from webui import config_editor

logger = logging.getLogger(__name__)

# 「零元提链」是可选组件：core/upi_zero_web_service.py 与 core/upi_live_feed.py 不在当前
# 源码分发里。缺件时只关掉对应的 /api/upi-zero/* 路由，绝不能让整个 WebUI ImportError 起不来。
try:
    from core import upi_live_feed, upi_zero_web_service

    _UPI_ZERO_AVAILABLE = True
except ImportError:
    upi_live_feed = None
    upi_zero_web_service = None
    _UPI_ZERO_AVAILABLE = False
    logger.warning(
        "零元提链组件缺失（core/upi_zero_web_service.py / core/upi_live_feed.py），"
        "相关接口将返回 503"
    )


# 生成好的下载包先驻留内存，由 /api/downloads/<id> 取走（过期与上限在 PreparedDownloads 里管）。
_PREPARED_DOWNLOAD_TTL = 600
_PREPARED_DOWNLOAD_MAX = 24
_prepared_downloads = PreparedDownloads(ttl=_PREPARED_DOWNLOAD_TTL, limit=_PREPARED_DOWNLOAD_MAX)

# 页面全是单文件内联 JS，script-src 只能留 unsafe-inline，光靠它挡不住 XSS。
# 真正有用的是后面几条：connect-src 'self' 断掉把 token 外发的路，
# object-src/base-uri 'none' 掐掉变形的脚本入口，frame-ancestors 只能靠响应头生效。
_CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)





















def create_app(auth_code: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")

    # 各域路由走 Blueprint，逐个搬到 webui/views/ 下；这里只做装配。
    app.register_blueprint(views_pools.bp)
    app.register_blueprint(views_jobs.bp)

    @app.after_request
    def _security_headers(response: Response):
        """HTML 响应补安全头（只作用于页面，下载与 JSON 接口不受影响）。"""
        if (response.mimetype or "").startswith("text/html"):
            response.headers.setdefault("Content-Security-Policy", _CSP_POLICY)
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "same-origin")
            response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @app.after_request
    def _compress_json_response(response: Response):
        """默认对 JSON API 响应启用 gzip，减少本地前端拉取大列表的传输体积。"""
        accept_encoding = (request.headers.get("Accept-Encoding") or "").lower()
        # 默认开启 gzip：浏览器会自动带 gzip；本地脚本未带 Accept-Encoding 时也压缩。
        # 只有客户端明确声明 identity 且没有 gzip 时，才按明文返回。
        gzip_allowed = ("gzip" in accept_encoding) or (not accept_encoding)
        if (
            response.direct_passthrough
            or response.headers.get("Content-Encoding")
            or not gzip_allowed
        ):
            return response
        mimetype = (response.mimetype or "").lower()
        if mimetype != "application/json":
            return response
        data = response.get_data()
        if not data or len(data) < 1024:
            return response
        compressed = gzip.compress(data, compresslevel=6)
        if len(compressed) >= len(data):
            return response
        response.set_data(compressed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(compressed))
        vary = response.headers.get("Vary")
        response.headers["Vary"] = "Accept-Encoding" if not vary else f"{vary}, Accept-Encoding"
        return response

    def _put_prepared_download(content: bytes, filename: str, mimetype: str = "application/zip") -> str:
        return _prepared_downloads.put(content, filename, mimetype)

    @app.get("/api/downloads/<download_id>")
    def api_prepared_download(download_id: str):
        item = _prepared_downloads.take(download_id)
        if not item:
            return jsonify({"ok": False, "error": "下载已过期或不存在，请重新生成"}), 404
        content = item.get("content") or b""
        filename = item.get("filename") or "download.zip"
        mimetype = item.get("mimetype") or "application/octet-stream"
        return Response(
            content,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(content)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    init_auth(app, auth_code=auth_code)
    register_auth_routes(app)
    recovered_plan_checks = db.recover_interrupted_plan_checks()
    if recovered_plan_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的套餐查询状态", recovered_plan_checks)
    recovered_extract_links = db.recover_interrupted_extract_links()
    if recovered_extract_links:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的提链状态", recovered_extract_links)
    recovered_live_checks = db.recover_interrupted_live_checks()
    if recovered_live_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的查活状态", recovered_live_checks)
    recovered_codex_agents = db.recover_interrupted_codex_agents()
    if recovered_codex_agents:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 Codex Agent Token 状态", recovered_codex_agents)
    recovered_totp_setups = db.recover_interrupted_totp_setups()
    if recovered_totp_setups:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 2FA 状态", recovered_totp_setups)
    recovered_email_changes = db.recover_interrupted_email_changes()
    if recovered_email_changes:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的邮箱换绑状态", recovered_email_changes)

    # ----------------------------------------------------------
    # 页面
    # ----------------------------------------------------------
    @app.get("/")
    def index():
        requested_ui = (request.args.get("ui") or "").strip().lower()
        if requested_ui in {"legacy", "modern"}:
            ui_mode = requested_ui
        else:
            ui_mode = (request.cookies.get("ui_mode") or "modern").strip().lower()
            if ui_mode not in {"legacy", "modern"}:
                ui_mode = "modern"

        template_name = "index_legacy.html" if ui_mode == "legacy" else "index.html"
        resp = make_response(render_template(template_name))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        if requested_ui in {"legacy", "modern"}:
            resp.set_cookie("ui_mode", ui_mode, max_age=60 * 60 * 24 * 365, samesite="Lax")
        return resp

    # ----------------------------------------------------------
    # 统计概览
    # ----------------------------------------------------------
    @app.get("/api/summary")
    def api_summary():
        from config import email as _email_cfg
        from core.email_provider import parse_email_sources
        pool = {"total": 0, "available": 0, "used": 0, "failed": 0}
        for src in parse_email_sources(_email_cfg.EMAIL_SOURCE):
            # GPTMail/MailNest/CloudMail 地址按需生成，不属于本地邮箱池。
            if src in ("gptmail", "mailnest", "cloudmail", "cloudflare"):
                continue
            one = (
                db.generic_api_email_pool_summary() if src == "generic_api"
                else db.imap_email_pool_summary() if src == "imap"
                else db.domain_email_pool_summary() if src == "cloudflare_domain"
                else db.outlook_pool_summary()
            )
            for k in pool:
                pool[k] += int(one.get(k, 0) or 0)
        domain_pool = db.domain_email_pool_summary()
        return jsonify({
            "accounts": db.count_accounts(),
            "outlook_total": pool.get("total", 0),
            "outlook_available": pool.get("available", 0),
            "outlook_used": pool.get("used", 0),
            "outlook_failed": pool.get("failed", 0),
            "domain_total": domain_pool.get("total", 0),
            "domain_available": domain_pool.get("available", 0),
            "domain_used": domain_pool.get("used", 0),
            "domain_failed": domain_pool.get("failed", 0),
        })

    # ----------------------------------------------------------
    # 已注册账号
    # ----------------------------------------------------------
    @app.get("/api/accounts")
    def api_accounts():
        limit = request.args.get("limit", default=500, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        codex_filter = str(request.args.get("codex_status", default="") or "").strip().lower()
        totp_filter = str(
            request.args.get("totp_status")
            or request.args.get("totp_filter")
            or request.args.get("twofa_status")
            or ""
        ).strip().lower()
        q = str(request.args.get("q", default="") or "").strip()
        date_from = str(request.args.get("date_from", default="") or "").strip() or None
        date_to = str(request.args.get("date_to", default="") or "").strip() or None
        # 新分页接口：传 page/page_size 或 paged=1 时返回 {items,total,page,page_size,...}
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            result = db.list_accounts_page(limit=page_size, offset=offset, archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, date_from=date_from, date_to=date_to, totp_filter=totp_filter)
            result["items"] = [compact_account_for_list(r) for r in (result.get("items") or [])]
            result.update({"ok": True, "page": page, "page_size": page_size, "compact": True})
            return jsonify(result)
        return jsonify(db.list_accounts(limit=limit, archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, date_from=date_from, date_to=date_to, totp_filter=totp_filter))

    @app.get("/api/accounts/plan-check-status")
    def api_account_plan_check_status():
        """套餐查询轻量状态，不返回 Token、邮箱密码等敏感字段。"""
        limit = request.args.get("limit", default=5000, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        codex_filter = str(request.args.get("codex_status", default="") or "").strip().lower()
        totp_filter = str(
            request.args.get("totp_status")
            or request.args.get("totp_filter")
            or request.args.get("twofa_status")
            or ""
        ).strip().lower()
        q = str(request.args.get("q", default="") or "").strip()
        date_from = str(request.args.get("date_from", default="") or "").strip() or None
        date_to = str(request.args.get("date_to", default="") or "").strip() or None
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            snapshot = db.list_account_plan_check_statuses(limit=page_size, offset=offset, archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, date_from=date_from, date_to=date_to, totp_filter=totp_filter)
            snapshot.update({"page": page, "page_size": page_size})
        else:
            snapshot = db.list_account_plan_check_statuses(limit=max(1, min(5000, limit)), archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, date_from=date_from, date_to=date_to, totp_filter=totp_filter)
        snapshot["queue"] = plan_check_service.queue_settings()
        return jsonify(snapshot)


    @app.get("/api/accounts/<int:acc_id>/secret")
    def api_account_secret(acc_id: int):
        """按需读取单账号敏感值，避免账号列表一次性下发完整 Token/整行。"""
        field = str(request.args.get("field") or "").strip()
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            value = account_secret_value(acc, field)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "id": acc_id, "field": field, "value": value})

    @app.post("/api/accounts/secret-bulk")
    def api_accounts_secret_bulk():
        """按需批量读取账号敏感值。Body {account_ids:[...], field}."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        field = str(data.get("field") or "").strip()
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多读取 5000 个账号"}), 400
        values = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            try:
                value = account_secret_value(acc, field)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            if value:
                values.append({"id": acc_id, "email": acc.get("email"), "value": value})
            else:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "值为空"})
        return jsonify({"ok": True, "field": field, "values": values, "count": len(values), "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/archive")
    def api_account_archive(acc_id: int):
        """归档/取消归档一个账号。Body {archived: true|false}。"""
        data = request.get_json(silent=True) or {}
        archived = bool(data.get("archived", True))
        updated = db.archive_account(acc_id=acc_id, archived=archived)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "archived": archived})

    @app.post("/api/accounts/archive-bulk")
    def api_accounts_archive_bulk():
        """批量归档/取消归档账号。Body {account_ids:[...], archived:true|false}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        archived = bool(data.get("archived", True))
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多归档 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.archive_accounts(account_ids=account_ids, archived=archived)
        skipped.extend(db_skipped)
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "archived": archived, "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/delete")
    def api_account_delete(acc_id: int):
        """删除一个已注册账号记录。只删除本地保存的账号/token记录，不改邮箱池状态。"""
        deleted = db.delete_account(acc_id=acc_id)
        if not deleted:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "deleted": True})

    @app.post("/api/accounts/delete-bulk")
    def api_accounts_delete_bulk():
        """批量删除已注册账号记录。Body {account_ids: [...]} 或 {ids: [...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        deleted, db_skipped = db.delete_accounts(account_ids=account_ids)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    @app.post("/api/accounts/<int:acc_id>/note")
    def api_account_note(acc_id: int):
        """更新单个已注册账号备注。Body {note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        note = str(data.get("note") or "")
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400
        updated = db.update_account_note(acc_id=acc_id, note=note)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "note": note})

    @app.post("/api/accounts/<int:acc_id>/totp-setup")
    def api_account_totp_setup(acc_id: int):
        """为单个账号开启 2FA/TOTP，成功后自动把 secret 写回账号记录。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = str(acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        if bool(acc.get("totp_secret")):
            return jsonify({"ok": False, "error": "该账号已经开启 2FA"}), 400

        try:
            from core import twofa_service
        except Exception as exc:
            return jsonify({"ok": False, "error": f"2FA 服务加载失败：{type(exc).__name__}: {exc}"}), 503

        queued = twofa_service.enqueue_account_totp_setup(
            account_id=acc_id,
            email=str(acc.get("email") or ""),
            access_token=token,
            trigger="manual",
            proxy=str(acc.get("proxy_used") or "") or None,
        )
        queued_payload = {k: v for k, v in queued.items() if k != "future"}
        if queued.get("busy"):
            return jsonify({"ok": False, **queued_payload}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued_payload}), 503
        return jsonify({"ok": True, "started": True, **queued_payload}), 202

    @app.post("/api/accounts/<int:acc_id>/change-email")
    def api_account_change_email(acc_id: int):
        """给单个账号排队换绑邮箱。Body {source}."""
        data = request.get_json(silent=True) or {}
        source = str(data.get("source") or "").strip().lower()
        allowed = {"outlook", "generic_api", "imap", "cloudflare_domain", "cloudflare", "gptmail", "mailnest", "cloudmail", "remail"}
        if source not in allowed:
            return jsonify({"ok": False, "error": "请选择有效的邮箱来源"}), 400
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        if not str(acc.get("access_token") or "").strip():
            return jsonify({"ok": False, "error": "账号缺少 access_token，请先查活刷新 AT"}), 400
        from core import email_change_service
        result = email_change_service.enqueue(acc_id, source, trigger="manual")
        public = {k: v for k, v in result.items() if k != "future"}
        return jsonify({"ok": bool(result.get("accepted")), **public}), (202 if result.get("accepted") else 409)

    @app.post("/api/accounts/change-email-bulk")
    def api_accounts_change_email_bulk():
        """批量换绑邮箱。Body {account_ids:[...], source}."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        source = str(data.get("source") or "").strip().lower()
        allowed = {"outlook", "generic_api", "imap", "cloudflare_domain", "cloudflare", "gptmail", "mailnest", "cloudmail", "remail"}
        if source not in allowed:
            return jsonify({"ok": False, "error": "请选择有效的邮箱来源"}), 400
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400
        from core import email_change_service
        started, skipped = [], []
        seen_ids: set[int] = set()
        for raw_id in ids:
            try:
                acc_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if acc_id in seen_ids:
                continue
            seen_ids.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not str(acc.get("access_token") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            result = email_change_service.enqueue(acc_id, source, trigger="manual_bulk")
            if result.get("accepted"):
                started.append({"id": acc_id, "email": acc.get("email"), "status": "queued"})
            else:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": result.get("error")})
        return jsonify({"ok": True, "started": started, "started_count": len(started), "skipped": skipped}), 202

    @app.post("/api/accounts/totp-setup-bulk")
    def api_accounts_totp_setup_bulk():
        """批量把账号 2FA/TOTP 设置任务加入后台队列。Body {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)

        accounts = []
        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = str(acc.get("email") or "").strip()
            token = str(acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            if str(acc.get("totp_secret") or "").strip():
                skipped.append({"id": acc_id, "email": email, "reason": "该账号已经开启 2FA"})
                continue
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            accounts.append(acc)

        try:
            from core import twofa_service
        except Exception as exc:
            return jsonify({"ok": False, "error": f"2FA 服务加载失败：{type(exc).__name__}: {exc}"}), 503

        started = []
        busy = []
        failed = []
        for acc in accounts:
            acc_id = int(acc.get("id") or 0)
            email = str(acc.get("email") or "").strip()
            try:
                queued = twofa_service.enqueue_account_totp_setup(
                    account_id=acc_id,
                    email=email,
                    access_token=str(acc.get("access_token") or "").strip(),
                    trigger="manual_bulk",
                    proxy=str(acc.get("proxy_used") or "") or None,
                )
            except Exception as exc:
                failed.append({
                    "id": acc_id,
                    "email": email,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue

            # Future 对象不可 JSON 序列化；批量接口只返回队列结果摘要。
            public_result = {k: v for k, v in queued.items() if k != "future"}
            item = {"id": acc_id, "email": email, **public_result}
            if queued.get("accepted"):
                item["status"] = "queued"
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)

        return jsonify({
            "ok": True,
            "message": f"已入队 {len(started)} 个 2FA 设置任务",
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.post("/api/accounts/note-bulk")
    def api_accounts_note_bulk():
        """批量更新已注册账号备注。Body {account_ids: [...], note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        note = str(data.get("note") or "")
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多备注 5000 个账号"}), 400
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400

        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.update_accounts_note(account_ids=account_ids, note=note)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.post("/api/accounts/check-live-bulk")
    def api_accounts_check_live_bulk():
        """批量查活：加入后台队列；协议 BrowserSession 指纹环境重新登录并刷新最新 AT。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查活 500 个账号"}), 400

        account_ids: list[int] = []
        skipped: list[dict] = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)

        accounts = []
        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = str(acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            accounts.append(acc)

        started = []
        busy_count = 0
        failed = []
        for acc in accounts:
            acc_id = int(acc.get("id") or 0)
            email = str(acc.get("email") or "")
            queued = live_check_service.enqueue_account_live_check(
                account_id=acc_id,
                email=email,
                trigger="manual",
                # 查活按“查套餐”同一套网络选路：
                # PLAN_CHECK_PROXY_MODE / PLAN_CHECK_PROXY / PROXY_POOL。
                # 不复用账号注册时的 proxy_used，避免旧注册出口被 CF 403 后一直失败。
                proxy=None,
            )
            if queued.get("accepted"):
                started.append({"id": acc_id, "email": email, "status": "queued"})
            elif queued.get("busy"):
                busy_count += 1
                skipped.append({"id": acc_id, "email": email, "reason": queued.get("error") or "正在查活"})
            else:
                failed.append({"id": acc_id, "email": email, "error": queued.get("error") or "入队失败"})

        return jsonify({
            "ok": True,
            "message": f"已入队 {len(started)} 个查活任务",
            "started": started,
            "started_count": len(started),
            "busy_count": busy_count,
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "queue": live_check_service.queue_settings(),
        }), 202


    @app.post("/api/accounts/check-plan")
    def api_account_check_plan():
        """把单账号套餐查询加入后台队列。Body {account_id|email, proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        email = (data.get("email") or "").strip()
        acc = None
        if acc_id is not None:
            try:
                acc = db.get_account(int(acc_id))
            except Exception:
                acc = None
        if acc is None and email:
            acc = db.get_account_by_email(email)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        account_id = int(acc.get("id"))
        queued = plan_check_service.enqueue_account_plan_check(
            account_id=account_id,
            email=acc.get("email") or "",
            access_token=token,
            trigger="manual",
            proxy=data.get("proxy") if "proxy" in data else None,
            timezone_offset_min=str(data.get("timezone_offset_min") or "-"),
        )
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **queued}), 202

    @app.post("/api/accounts/check-plan-bulk")
    def api_accounts_check_plan_bulk():
        """批量把套餐查询加入统一后台队列。Body {account_ids:[...], proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号"}), 400
        # 与单账号查询保持一致：未传时使用独立网络策略。
        proxy = data.get("proxy") if "proxy" in data else None
        timezone_offset_min = str(data.get("timezone_offset_min") or "-")

        items = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not (acc.get("access_token") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            items.append(acc)

        started = []
        busy = []
        failed = []
        for acc in items:
            queued = plan_check_service.enqueue_account_plan_check(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=acc.get("access_token") or "",
                trigger="manual_bulk",
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
            )
            item = {"id": acc.get("id"), "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.post("/api/accounts/check-promo")
    def api_account_check_promo():
        """对单个账号跑优惠结账检测。Body {account_id|id, countries?, proxy?}"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        acc = None
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        countries = data.get("countries") if isinstance(data.get("countries"), list) else None
        from core.promo_check import detect_account_promo
        promo = detect_account_promo(
            token,
            proxy=data.get("proxy") if "proxy" in data else None,
            countries=countries,
            campaign_id=str(acc.get("plus_trial_campaign_id") or ""),
        )
        db.update_account_plan_check(acc_id=int(acc.get("id")), result={
            "ok": True,
            "checked_at": acc.get("plan_checked_at") or promo.get("checked_at"),
            "current_plan_type": acc.get("current_plan_type") or acc.get("plan_type"),
            "plus_trial_eligible": acc.get("plus_trial_eligible"),
            "promo_ok": promo.get("promo_ok"),
            "promo_countries": promo.get("promo_countries") or [],
            "promo_methods": promo.get("promo_methods") or [],
            "promo_method_labels": promo.get("promo_method_labels") or [],
            "promo_summary": promo.get("promo_summary") or "",
            "promo_checked_at": promo.get("checked_at"),
            "promo_error": promo.get("error") if not promo.get("ok") else None,
            "promo_check": promo,
        })
        return jsonify({"ok": True, **promo})

    @app.post("/api/accounts/check-promo-bulk")
    def api_accounts_check_promo_bulk():
        """批量优惠检测。Body {account_ids:[...]}"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        started = []
        failed = []
        from core.promo_check import detect_account_promo
        for raw in ids[:50]:
            try:
                acc = db.get_account(int(raw))
            except Exception:
                acc = None
            if not acc or not (acc.get("access_token") or "").strip():
                failed.append({"id": raw, "reason": "账号不存在或缺少 token"})
                continue
            promo = detect_account_promo(
                acc.get("access_token") or "",
                proxy=data.get("proxy") if "proxy" in data else None,
                campaign_id=str(acc.get("plus_trial_campaign_id") or ""),
            )
            db.update_account_plan_check(acc_id=int(acc.get("id")), result={
                "ok": True,
                "checked_at": acc.get("plan_checked_at") or promo.get("checked_at"),
                "current_plan_type": acc.get("current_plan_type") or acc.get("plan_type"),
                "plus_trial_eligible": acc.get("plus_trial_eligible"),
                "promo_ok": promo.get("promo_ok"),
                "promo_countries": promo.get("promo_countries") or [],
                "promo_summary": promo.get("promo_summary") or "",
                "promo_checked_at": promo.get("checked_at"),
                "promo_error": promo.get("error") if not promo.get("ok") else None,
                "promo_check": promo,
            })
            started.append({"id": acc.get("id"), "email": acc.get("email"), "promo_ok": promo.get("promo_ok"), "promo_summary": promo.get("promo_summary")})
        return jsonify({"ok": True, "started": started, "started_count": len(started), "failed": failed, "failed_count": len(failed)})

    def _open_browser_paths(acc_id: int) -> tuple:
        root = Path(__file__).resolve().parent.parent
        logs = root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return root, logs / ("open-browser-%d.log" % int(acc_id))

    def _pick_free_port(start: int = 9222, end: int = 9280) -> int:
        for port in range(start, end + 1):
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        return start

    @app.post("/api/accounts/<int:acc_id>/open-browser")
    def api_account_open_browser(acc_id: int):
        """给账号开一个「已登录」的浏览器窗口（给支付链接回调用）。

        支付链接最后要回调到 chatgpt.com 完成结账，回调依赖**浏览器的登录态**；
        而 access_token 推不出浏览器 cookie（__Secure-next-auth.session-token 只在
        OAuth callback 那一刻下发），所以只能重走一次登录去抓。逻辑都封在
        tools/open_logged_in_browser.py，这里只负责异步拉起来，不阻塞请求。
        """
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        email = str(acc.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "账号没有邮箱"}), 400
        data = request.get_json(silent=True) or {}
        try:
            port = int(data.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not (1024 <= port <= 65535):
            port = _pick_free_port()
        root, log_path = _open_browser_paths(acc_id)
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PROXY_POOL", "socks5h://127.0.0.1:10808")
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            fh = open(log_path, "a", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, "-u", str(root / "tools" / "open_logged_in_browser.py"),
                 "--account-id", str(acc_id), "--port", str(port)],
                cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                stdout=fh, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True,
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        return jsonify({
            "ok": True, "pid": proc.pid, "port": port, "email": email,
            "log": str(log_path),
            "message": "已开始重登并准备浏览器（约 1-3 分钟），端口 %d" % port,
        }), 202

    @app.get("/api/accounts/<int:acc_id>/open-browser-status")
    def api_account_open_browser_status(acc_id: int):
        """看开浏览器这个任务的进度（前端轮询用）。"""
        acc = db.get_account(acc_id) or {}
        email = str(acc.get("email") or "").strip()
        safe = email.replace("@", "_at_").replace("/", "_")
        root, log_path = _open_browser_paths(acc_id)
        need_code = (root / "tools" / "_otp_requests" / (safe + ".need")).exists()
        tail = ""
        ready = False
        if log_path.exists():
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
                # 日志是追加写的，只看最后一次运行（脚本每次启动会打 === RUN <时间>）
                marker = text.rfind("=== RUN ")
                if marker >= 0:
                    text = text[marker:]
                ready = "浏览器已就绪" in text
                keep = [ln for ln in text.splitlines() if ln.startswith("===") and not ln.startswith("=== RUN")][-3:]
                tail = " | ".join(keep)
            except Exception:
                tail = ""
        return jsonify({"ok": True, "email": email, "need_code": need_code,
                        "ready": ready, "tail": tail,
                        "code_file": str(root / "tools" / "_otp_codes" / (safe + ".txt"))})

    def _upi_zero_unavailable():
        """提链组件缺件时的统一应答，避免前端只拿到 404 无从判断。"""
        return jsonify({
            "ok": False,
            "error": "零元提链组件未包含在当前源码分发中"
                     "（缺少 core/upi_zero_web_service.py 或 core/upi_live_feed.py）",
        }), 503

    @app.get("/api/upi-zero/candidates")
    def api_upi_zero_candidates():
        if not _UPI_ZERO_AVAILABLE:
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

    @app.post("/api/upi-zero/extract")
    def api_upi_zero_extract():
        if not _UPI_ZERO_AVAILABLE:
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

    @app.get("/api/upi-zero/status/<int:account_id>")
    def api_upi_zero_status(account_id: int):
        if not _UPI_ZERO_AVAILABLE:
            return _upi_zero_unavailable()
        if account_id not in upi_zero_web_service.source_ids():
            return jsonify({"ok": False, "error": "账号不在零元池"}), 404
        acc = db.get_account(account_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        response = jsonify({"ok": True, **upi_zero_web_service.public_status(acc)})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/upi-zero/log/<int:account_id>")
    def api_upi_zero_log(account_id: int):
        if not _UPI_ZERO_AVAILABLE:
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

    @app.get("/api/upi-zero/live-log")
    def api_upi_zero_live_log():
        """全池提链的实时日志尾巴：读 sweep 自己的日志文件，增量返回。"""
        if not _UPI_ZERO_AVAILABLE:
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

    @app.post("/api/accounts/codex-agent")
    def api_account_codex_agent():
        """单账号生成 Codex Agent Token。Body {account_id|id, verify_task?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = codex_agent_service.enqueue_account_codex_agent(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                verify_task=bool(data.get("verify_task", True)),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/codex-agent-bulk")
    def api_accounts_codex_agent_bulk():
        """批量生成 Codex Agent Token。Body {account_ids:[...], verify_task?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = codex_agent_service.enqueue_account_codex_agent(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    verify_task=bool(data.get("verify_task", True)),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    def _codex_agent_auth_for_account(acc: dict) -> tuple[str, str]:
        """从 SQLite 返回账号已生成的 Codex Agent auth.json 文本与下载文件名。"""
        import json as _json

        email = str(acc.get("email") or "").strip()
        safe_email = "".join(ch if ch.isalnum() or ch in ("@", ".", "-", "_") else "_" for ch in (email or f"account-{acc.get('id')}"))
        filename = f"codex-agent-{safe_email}.json"
        token_text = str(acc.get("codex_agent_token") or "").strip()
        if token_text:
            try:
                payload = _json.loads(token_text)
                token_text = _json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            except Exception:
                token_text = token_text + ("\n" if not token_text.endswith("\n") else "")
            return token_text, filename

        stored = db.get_codex_agent_credential(int(acc.get("id") or 0))
        if stored:
            return stored

        raise RuntimeError("该账号还没有生成 Codex Agent Token")

    def _join_sub2_url(base: str, path: str) -> str:
        base = str(base or "").strip().rstrip("/")
        path = str(path or "").strip()
        if not base or not path:
            return ""
        parsed = urlparse(path)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return path
        return f"{base}/{path.lstrip('/')}"

    def _sub2_codex_session_import_url() -> str:
        from config import sub2api as sub2api_cfg
        api_base = str(getattr(sub2api_cfg, "SUB2API_API_BASE", "") or "").strip()
        if api_base:
            return _join_sub2_url(api_base, "/api/v1/admin/accounts/import/codex-session")
        # 兼容旧配置：之前 SUB2API_API_URL 是完整上传接口 URL。
        return str(getattr(sub2api_cfg, "SUB2API_API_URL", "") or "").strip()

    def _upload_account_codex_agent_to_sub2(acc: dict) -> dict:
        """把账号已生成的 Codex Agent auth.json 上传到 sub2api。"""
        import json as _json
        from config import sub2api as sub2api_cfg
        from core.codex_agent import upload_sub2api_account

        text, _filename = _codex_agent_auth_for_account(acc)
        try:
            auth_json = _json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"Agent Token JSON 无效: {exc}") from exc

        api_url = _sub2_codex_session_import_url()
        api_token = str(getattr(sub2api_cfg, "SUB2API_API_KEY", "") or getattr(sub2api_cfg, "SUB2API_API_TOKEN", "") or "").strip()
        auth_header = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_HEADER", "x-api-key") or "x-api-key").strip()
        auth_prefix = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_PREFIX", "") or "").strip()
        payload_mode = "codex_session_import"
        proxy_key = str(getattr(sub2api_cfg, "SUB2API_PROXY_KEY", "") or "").strip() or None
        timeout = float(getattr(sub2api_cfg, "SUB2API_API_TIMEOUT", 20) or 20)

        result = upload_sub2api_account(
            auth_json,
            api_url,
            api_token=api_token,
            auth_header=auth_header,
            auth_prefix=auth_prefix,
            payload_mode=payload_mode,
            proxy_key=proxy_key,
            timeout=timeout,
        )
        try:
            db.update_account_codex_agent(int(acc.get("id")), {
                "ok": True,
                "status": "success",
                "message": "Agent Token 已上传 sub2api",
                "sub2api_url": result.get("url"),
                "sub2api_mode": result.get("payload_mode"),
                "sub2api_total": result.get("total"),
            })
        except Exception:
            logger.exception("更新账号 sub2api 上传状态失败: account_id=%s", acc.get("id"))
        return result

    @app.post("/api/accounts/<int:acc_id>/codex-agent/upload-sub2")
    def api_account_codex_agent_upload_sub2(acc_id: int):
        """单账号把已生成的 Codex Agent Token 上传到 sub2api。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            result = _upload_account_codex_agent_to_sub2(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        return jsonify({"ok": True, "account_id": acc_id, "email": acc.get("email"), "result": result})

    @app.post("/api/accounts/codex-agent/upload-sub2-bulk")
    def api_accounts_codex_agent_upload_sub2_bulk():
        """批量把已生成的 Codex Agent Token 上传到 sub2api。Body {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        uploaded, failed, skipped = [], [], []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if (acc.get("codex_agent_status") or "") != "success" and not (acc.get("codex_agent_token") or acc.get("codex_agent_auth_path")):
                skipped.append({"id": acc_id, "email": email, "reason": "未生成 Agent Token"})
                continue
            try:
                result = _upload_account_codex_agent_to_sub2(acc)
                uploaded.append({"id": acc_id, "email": email, "url": result.get("url"), "status_code": result.get("status_code")})
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "uploaded": uploaded,
            "uploaded_count": len(uploaded),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.get("/api/accounts/<int:acc_id>/codex-agent/download")
    def api_account_codex_agent_download(acc_id: int):
        """下载单个账号的 Codex Agent auth.json。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            content, filename = _codex_agent_auth_for_account(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 404
        data = content.encode("utf-8")
        return Response(
            data,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/codex-agent/download-bulk")
    def api_accounts_codex_agent_download_bulk():
        """下载选中账号已生成的 Codex Agent Token，打包 ZIP。"""
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        added = []
        errors = []
        used_names = set()
        seen = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw in ids:
                try:
                    acc_id = int(raw)
                except Exception:
                    errors.append({"id": raw, "error": "ID 非法"})
                    continue
                if acc_id in seen:
                    continue
                seen.add(acc_id)
                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                try:
                    content, filename = _codex_agent_auth_for_account(acc)
                    arcname = filename
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, content)
                    added.append({"id": acc_id, "email": acc.get("email"), "filename": arcname})
                except Exception as exc:
                    errors.append({"id": acc_id, "email": acc.get("email"), "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-codex-agent",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有可下载的 Codex Agent Token", "errors": errors}), 404
        now = _dt.now()
        dl_name = f"accounts-codex-agent-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/download-cpa-bulk")
    def api_accounts_download_cpa_bulk():
        """
        从账号列表选中的账号直接到 CPA auth-files 下载 Codex CPA JSON，并打包为 ZIP。
        Body: {"account_ids": [1,2,...]} 或 {"ids": [...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text, list_cpa_codex_auth_files

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        try:
            cpa_files = list_cpa_codex_auth_files()
        except Exception as exc:
            return jsonify({"ok": False, "error": f"读取 CPA auth-files 失败: {type(exc).__name__}: {exc}"}), 502

        def _match_cpa_file(email: str, local_filename: str = "") -> dict | None:
            """在已缓存的 CPA 文件列表中匹配，避免每个账号都重新请求 auth-files。"""
            email_l = str(email or "").strip().lower()
            local_name_l = str(local_filename or "").strip().lower()
            local_stem_l = local_name_l[:-5] if local_name_l.endswith(".json") else local_name_l

            def score(item: dict) -> int:
                name_l = str(item.get("name") or "").lower()
                item_email_l = str(item.get("email") or "").lower()
                s = 0
                if local_name_l and name_l == local_name_l:
                    s = max(s, 100)
                if local_stem_l and name_l.startswith(local_stem_l):
                    s = max(s, 80)
                if email_l and item_email_l == email_l:
                    s = max(s, 70)
                if email_l and email_l in name_l:
                    s = max(s, 60)
                if local_stem_l.endswith("-cpa-callback"):
                    base = local_stem_l[:-len("-cpa-callback")]
                    if base and name_l.startswith(base + "-"):
                        s = max(s, 75)
                return s

            ranked = sorted(((score(item), item) for item in cpa_files), key=lambda x: x[0], reverse=True)
            return ranked[0][1] if ranked and ranked[0][0] > 0 else None

        # 建立 email -> 本地 codex 文件名索引；有本地文件名时传给 CPA 匹配逻辑可提升命中率。
        local_by_email: dict[str, str] = {}
        try:
            for item in db.list_codex_accounts():
                email_key = str(item.get("email") or "").strip().lower()
                fname = str(item.get("filename") or "").strip()
                if email_key and fname and email_key not in local_by_email:
                    local_by_email[email_key] = fname
        except Exception:
            local_by_email = {}

        errors = []
        added = []
        used_names = set()
        seen_ids = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw_id in ids:
                try:
                    acc_id = int(raw_id)
                except (TypeError, ValueError):
                    errors.append({"id": raw_id, "error": "ID 非法"})
                    continue
                if acc_id in seen_ids:
                    continue
                seen_ids.add(acc_id)

                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                email = str(acc.get("email") or "").strip()
                if not email:
                    errors.append({"id": acc_id, "error": "账号缺少 email"})
                    continue

                local_filename = local_by_email.get(email.lower(), "")
                try:
                    meta = _match_cpa_file(email=email, local_filename=local_filename)
                    cpa_name_hint = str((meta or {}).get("name") or "").strip()
                    if not cpa_name_hint:
                        raise RuntimeError(f"[Codex][CPA] 未在 CPA auth-files 中找到匹配的 Codex 凭证: {email}")
                    cpa_text, cpa_name, meta = download_cpa_codex_auth_text(
                        cpa_name=cpa_name_hint,
                    )
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({
                        "id": acc_id,
                        "email": email,
                        "local_filename": local_filename,
                        "cpa_filename": cpa_name,
                        "cpa_meta": meta,
                    })
                    if local_filename:
                        try:
                            db.mark_codex_exported(local_filename)
                        except Exception as _ignored_exc:
                            logger.debug("忽略异常 %s: %s", type(_ignored_exc).__name__, _ignored_exc)
                except Exception as exc:
                    errors.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})

            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"accounts-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        if isinstance(data, dict) and data.get("prepare"):
            download_id = _put_prepared_download(zip_bytes, dl_name, "application/zip")
            return jsonify({
                "ok": True,
                "prepared": True,
                "download_id": download_id,
                "download_url": f"/api/downloads/{download_id}",
                "filename": dl_name,
                "added_count": len(added),
                "error_count": len(errors),
            })
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    # ----------------------------------------------------------
    # Codex 授权账号（CPA 兼容凭证）
    # ----------------------------------------------------------
    @app.get("/api/codex")
    def api_codex_list():
        q = str(request.args.get("q", default="") or "").strip()
        archived = str(request.args.get("archived", default="0") or "0").lower()
        date_from = str(request.args.get("date_from", default="") or "").strip() or None
        date_to = str(request.args.get("date_to", default="") or "").strip() or None
        limit = request.args.get("limit", default=500, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = db.list_codex_accounts_page(
                archived=archived,
                date_from=date_from,
                date_to=date_to,
                q=q,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
            result.update({"ok": True, "page": page, "page_size": page_size})
            result["accounts"] = result.pop("items")
            result["summary"] = db.codex_accounts_summary()
            return jsonify(result)
        result = db.list_codex_accounts_page(
            archived=archived,
            date_from=date_from,
            date_to=date_to,
            q=q,
            limit=max(1, int(limit or 1)),
            offset=0,
        )
        return jsonify({
            "summary": db.codex_accounts_summary(),
            "accounts": result["items"],
        })

    @app.post("/api/codex/archive")
    def api_codex_archive():
        """归档/取消归档一条 Codex 授权凭证。Body {filename, archived}。"""
        data = request.get_json(silent=True) or {}
        filename = str(data.get("filename") or "").strip()
        archived = bool(data.get("archived", True))
        if not filename:
            return jsonify({"ok": False, "error": "filename 必填"}), 400
        try:
            rec = db.archive_codex(filename=filename, archived=archived)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if rec is None:
            return jsonify({"ok": False, "error": f"凭证不存在: {filename}"}), 404
        return jsonify({"ok": True, "filename": filename, "archived": archived, "record": rec})

    @app.post("/api/codex/archive-bulk")
    def api_codex_archive_bulk():
        """批量归档/取消归档 Codex 授权凭证。Body {filenames:[...], archived}。"""
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        archived = bool(data.get("archived", True))
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400
        updated = []
        skipped = []
        seen = set()
        for fname in filenames:
            if not isinstance(fname, str) or not fname:
                skipped.append({"filename": str(fname), "reason": "非法文件名"})
                continue
            if fname in seen:
                continue
            seen.add(fname)
            try:
                rec = db.archive_codex(filename=fname, archived=archived)
            except ValueError as exc:
                skipped.append({"filename": fname, "reason": str(exc)})
                continue
            if rec is None:
                skipped.append({"filename": fname, "reason": "凭证不存在"})
            else:
                updated.append({"filename": fname, "archived": archived})
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "archived": archived, "skipped": skipped})

    @app.get("/api/codex/download/<path:filename>")
    def api_codex_download(filename: str):
        """
        下载一个 CPA 兼容的 codex-*.json 文件，下载即标记为已导出（计数+1）。
        前端通过浏览器原生下载触发（a 标签 / window.location）。
        """
        try:
            content, fname = db.read_codex_credential(filename)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        db.mark_codex_exported(fname)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/codex/download-from-cpa/<path:filename>")
    def api_codex_download_from_cpa(filename: str):
        """按本地 codex 文件/回执匹配 CPA auth-files，并从 CPA 下载实际 Codex JSON。"""
        try:
            content, fname = db.read_codex_credential(filename)
            import json as _json
            try:
                local = _json.loads(content)
            except Exception:
                local = {}
            email = str(local.get("email") or "").strip()
            from core.codex_oauth import download_cpa_codex_auth_text
            cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=fname)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
        db.mark_codex_exported(fname)
        return Response(
            cpa_text,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{cpa_name}"'},
        )

    @app.post("/api/codex/download-bulk-from-cpa")
    def api_codex_download_bulk_from_cpa():
        """
        批量从 CPA 下载选中的 Codex 凭证，打包成 zip；zip 内每个文件都是 CPA 原始 JSON。
        Body: {"filenames": ["codex-xxx-cpa-callback.json", ...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        errors = []
        added = []
        used_names = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fname in filenames:
                if not isinstance(fname, str):
                    errors.append({"filename": str(fname), "error": "非字符串"})
                    continue
                try:
                    content, real_fname = db.read_codex_credential(fname)
                    try:
                        local = _json.loads(content)
                    except Exception:
                        local = {}
                    email = str(local.get("email") or "").strip()
                    cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=real_fname)
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({"local_filename": real_fname, "cpa_filename": cpa_name})
                    db.mark_codex_exported(real_fname)
                except Exception as exc:
                    errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"codex-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        return Response(
            buf.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/download-bulk")
    def api_codex_download_bulk():
        """
        批量下载选中的 codex 凭证，打包到一个 JSON 文件里。

        Body: {"filenames": ["codex-xxx.json", ...]}
        响应：聚合 JSON（attachment 触发浏览器下载），结构：
            {
              "exported_at": "...",
              "count": N,
              "credentials": [{"filename": "...", "data": {...原始凭证内容...}}, ...],
              "errors": [...]   // 仅当部分失败时出现
            }
        注意：聚合格式**不能直接被 CPA 读**，CPA 是按单文件加载 auths/ 目录的。
              本接口主要用途是备份 / 跨机迁移 / 二次处理。
        每个成功的凭证会自动标记 mark_exported（计数+1）。
        """
        import json as _json
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        bundle = []
        errors = []
        for fname in filenames:
            if not isinstance(fname, str):
                errors.append({"filename": str(fname), "error": "非字符串"})
                continue
            try:
                content, real_fname = db.read_codex_credential(fname)
                parsed = _json.loads(content)
                bundle.append({"filename": real_fname, "data": parsed})
                db.mark_codex_exported(real_fname)
            except Exception as exc:
                errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})

        now = _dt.now()
        result = {
            "exported_at": now.isoformat(timespec="seconds"),
            "count": len(bundle),
            "credentials": bundle,
        }
        if errors:
            result["errors"] = errors

        dl_name = f"codex-bulk-{now.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            _json.dumps(result, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/reset-export")
    def api_codex_reset_export():
        """清掉某个 codex 凭证的导出状态（重新标为未导出）。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            db.reset_codex_exported(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/codex/delete")
    def api_codex_delete():
        """删除一个 codex 凭证文件。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            deleted = db.delete_codex_credential(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not deleted:
            return jsonify({"ok": False, "error": "凭证文件不存在"}), 404
        return jsonify({"ok": True, "deleted": fname})

    @app.post("/api/codex/delete-bulk")
    def api_codex_delete_bulk():
        """批量删除 codex 凭证文件。body {filenames:[...]}。"""
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个"}), 400
        deleted = []
        skipped = []
        seen = set()
        for fname in filenames:
            fname = str(fname or "").strip()
            if not fname or fname in seen:
                continue
            seen.add(fname)
            try:
                ok = db.delete_codex_credential(fname)
                if ok:
                    deleted.append(fname)
                else:
                    skipped.append({"filename": fname, "reason": "文件不存在"})
            except Exception as exc:
                skipped.append({"filename": fname, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    def _reserve_codex_retry(email: str) -> bool:
        """进程内防重复占位；成功返回 True。"""
        return codex_retry_service.reserve(email)

    def _release_codex_retry(email: str) -> None:
        codex_retry_service.release(email)

    def _run_codex_retry_worker(email: str, *, batch_label: str | None = None, clear_log: bool = True) -> None:
        """执行一个账号的 Codex 补跑。调用前必须已经 reserve。"""
        codex_retry_service.run_worker(email, batch_label=batch_label, clear_log=clear_log)


    @app.post("/api/codex/stop")
    def api_codex_stop():
        """停止单个 Codex 补跑。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        result = codex_retry_service.request_stop(email)
        status = int(result.pop("status", 200) or 200)
        return jsonify(result), status

    @app.post("/api/codex/stop-bulk")
    def api_codex_stop_bulk():
        """批量停止 Codex 补跑。Body {emails:[...]} 或 {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        emails = data.get("emails") or []
        ids = data.get("account_ids") or data.get("ids") or []
        targets = []
        if isinstance(emails, list) and emails:
            targets = [str(x or "").strip() for x in emails]
        elif isinstance(ids, list) and ids:
            for raw in ids:
                try:
                    acc = db.get_account(int(raw))
                except Exception:
                    acc = None
                if acc and acc.get("email"):
                    targets.append(str(acc.get("email") or "").strip())
        else:
            return jsonify({"ok": False, "error": "emails 或 account_ids 必须是非空数组"}), 400
        if len(targets) > 500:
            return jsonify({"ok": False, "error": "单次最多停止 500 个"}), 400
        stopped = []
        skipped = []
        seen = set()
        for email in targets:
            key = email.lower()
            if not email or key in seen:
                continue
            seen.add(key)
            acc = db.get_account_by_email(email)
            if acc is None:
                skipped.append({"email": email, "reason": "账号不存在"})
                continue
            if (acc.get("codex_status") or "") != "retrying" and not codex_retry_service.is_retrying(email):
                skipped.append({"email": email, "reason": "未处于补跑中"})
                continue
            r = codex_retry_service.request_stop(email)
            if r.get("ok"):
                stopped.append({"email": email, "injected": r.get("injected"), "running": r.get("running")})
            else:
                skipped.append({"email": email, "reason": r.get("error") or "停止失败"})
        return jsonify({"ok": True, "stopped": stopped, "stopped_count": len(stopped), "skipped": skipped})

    @app.post("/api/codex/reset-retrying")
    def api_codex_reset_retrying():
        """手动重置某账号的 Codex 补跑中状态。Body {email, status?}。"""
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        raw_status = (data.get("status") or "failed").strip().lower()
        if raw_status in ("", "none", "null", "clear"):
            raw_status = "empty"
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        if raw_status not in ("failed", "skipped", "empty"):
            return jsonify({"ok": False, "error": "status 仅支持 failed/skipped/empty"}), 400

        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        new_status = "" if raw_status == "empty" else raw_status
        err = None if raw_status == "empty" else "用户手动重置补跑中状态"
        ok = db.update_account_codex_status(email, new_status, err)
        if not ok:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        _release_codex_retry(email)

        try:
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                ts = _dt.now().strftime("%H:%M:%S")
                shown = new_status or "空"
                f.write(f"{ts} [WARNING] [Codex 补跑] 用户手动重置补跑中状态，当前状态={shown}\n")
        except Exception:
            logger.exception("写入 Codex 补跑重置日志失败")

        return jsonify({"ok": True, "message": "已重置补跑中状态", "status": new_status})

    @app.post("/api/codex/retry")
    def api_codex_retry():
        """手动补跑某账号的 Codex 授权。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        if (acc.get("live_check_status") or "") == "deactivated":
            return jsonify({"ok": False, "error": "账号已废号，不能补跑 Codex"}), 409
        if not _reserve_codex_retry(email):
            return jsonify({"ok": False, "error": "该账号正在补跑中，请稍候"}), 409

        db.update_account_codex_status(email, "retrying", None)
        threading.Thread(
            target=_run_codex_retry_worker,
            kwargs={"email": email, "clear_log": True},
            name=f"codex-retry-{email}",
            daemon=True,
        ).start()
        return jsonify({"ok": True, "message": "已在后台开始补跑，~1-2 分钟后刷新查看"})

    @app.post("/api/codex/retry-bulk")
    def api_codex_retry_bulk():
        """批量补跑 Codex。Body {account_ids:[...], workers: 1-16}。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        workers = data.get("workers", 1)
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        try:
            workers = max(1, min(16, int(workers)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是数字"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多选择 500 个账号"}), 400

        selected = []
        skipped = []
        seen_ids = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen_ids:
                continue
            seen_ids.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = (acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            if (acc.get("live_check_status") or "") == "deactivated":
                skipped.append({"id": acc_id, "email": email, "reason": "账号已废号"})
                continue
            if not _reserve_codex_retry(email):
                skipped.append({"id": acc_id, "email": email, "reason": "正在补跑中"})
                continue
            selected.append({"id": acc_id, "email": email})

        if not selected:
            return jsonify({"ok": False, "error": "没有可补跑的账号", "skipped": skipped}), 409

        batch_id = _dt.now().strftime("%Y%m%d-%H%M%S")
        for item in selected:
            email = item["email"]
            db.update_account_codex_status(email, "retrying", None)
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"{_dt.now().strftime('%H:%M:%S')} [INFO] [Codex 批量补跑] 已加入批量任务 batch={batch_id} workers={workers}，等待线程执行\n",
                encoding="utf-8",
            )

        def _bulk_runner(items: list[dict], max_workers: int, batch: str):
            logger.info(f"[Codex 批量补跑] 启动 batch={batch} count={len(items)} workers={max_workers}")
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"codex-bulk-{batch}") as ex:
                futures = [ex.submit(_run_codex_retry_worker, it["email"], batch_label=f"{batch} #{idx}/{len(items)}", clear_log=False) for idx, it in enumerate(items, 1)]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception:
                        logger.exception(f"[Codex 批量补跑] 子任务异常 batch={batch}")
            logger.info(f"[Codex 批量补跑] 完成 batch={batch}")

        threading.Thread(
            target=_bulk_runner,
            args=(selected, workers, batch_id),
            name=f"codex-bulk-dispatch-{batch_id}",
            daemon=True,
        ).start()
        return jsonify({
            "ok": True,
            "message": f"已开始批量补跑 {len(selected)} 个账号，并发 {workers}",
            "started": selected,
            "started_count": len(selected),
            "skipped": skipped,
            "batch_id": batch_id,
        })

    @app.get("/api/codex/retry-log")
    def api_codex_retry_log():
        """读取某邮箱最近一次补跑的日志。?email=xxx"""
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = codex_retry_service.log_path(email)
        if not p.exists():
            return jsonify({"ok": True, "log": "", "running": False})
        max_bytes = 50_000
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            content = f.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": codex_retry_service.is_retrying(email),
        })

    @app.get("/api/accounts/live-check-log")
    def api_account_live_check_log():
        """读取某邮箱最近一次查活日志。?email=xxx"""
        from core import account_liveness
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = account_liveness.log_path(email)
        data = read_log_tail(p, max_bytes=80_000, running_fn=lambda: live_check_service.is_checking(email))
        return jsonify(data)

    @app.get("/api/accounts/totp-setup-log")
    def api_account_totp_setup_log():
        """读取某邮箱最近一次 2FA 设置日志。?email=xxx"""
        from core import twofa_service
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = twofa_service.log_path(email)
        data = read_log_tail(p, max_bytes=80_000, running_fn=lambda: False)
        try:
            acc = db.get_account_by_email(email) or {}
            data["running"] = bool(str(acc.get("totp_setup_status") or "") in {"queued", "running"}) or twofa_service.is_running(int(acc.get("id") or 0))
        except Exception as _ignored_exc:
            logger.debug("忽略异常 %s: %s", type(_ignored_exc).__name__, _ignored_exc)
        return jsonify(data)

    @app.get("/api/accounts/<int:acc_id>/change-email-log")
    def api_account_change_email_log(acc_id: int):
        """读取账号最近一次邮箱换绑日志。"""
        from core import email_change_service
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        data = read_log_tail(
            email_change_service.log_path(acc_id), max_bytes=80_000,
            running_fn=lambda: email_change_service.is_running(acc_id),
        )
        data["account_id"] = acc_id
        data["email"] = acc.get("email")
        data["running"] = bool(data.get("running") or str(acc.get("email_change_status") or "") in {"queued", "running"})
        return jsonify(data)

    # ----------------------------------------------------------
    # RoxyBrowser 辅助接口
    # ----------------------------------------------------------
    @app.get("/api/roxy/workspaces")
    def api_roxy_workspaces():
        try:
            from core.roxybrowser_client import RoxyBrowserClient
            result = RoxyBrowserClient().list_workspaces()
            return jsonify(result)
        except Exception as exc:
            logger.exception("获取 Roxy 团队/工作区失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    # ----------------------------------------------------------
    # 配置读写
    # ----------------------------------------------------------
    @app.get("/api/config")
    def api_config_get():
        return jsonify(config_editor.get_config())

    @app.post("/api/cloudmail/gen-token")
    def api_cloudmail_gen_token():
        """手动生成 CloudMail Authorization Token，并把本次填写的 CloudMail 配置一并写入 .env。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import gen_token
            from config.env_loader import write_env_values

            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            path = (data.get("path") or "/api/public/genToken").strip() or "/api/public/genToken"
            token = gen_token(
                email=admin_email,
                password=password,
                path=path,
                base_url=api_base,
            )
            updates = {"CLOUDMAIL_AUTH_TOKEN": token}
            # 生成 Token 时用户通常尚未点“保存配置”；这里同步保存本次填写的字段，
            # 避免 loadConfig() 后 API 地址/账号/密码被旧 .env 值覆盖。
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if path:
                updates["CLOUDMAIL_TOKEN_PATH"] = path
            written = write_env_values(updates)
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail Token 写入后热加载失败")
            return jsonify({
                "ok": True,
                "token": token,
                "written": written,
                "message": "CloudMail Token 已生成，且当前 CloudMail 配置已保存",
            })
        except Exception as exc:
            logger.exception("生成 CloudMail Token 失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/cloudmail/domains")
    def api_cloudmail_domains():
        """从 CloudMail 平台获取域名列表，并可写入 .env 作为本地缓存。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import fetch_domains
            from config.env_loader import write_env_values

            updates = {}
            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            token = (data.get("token") or "").strip()
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if token:
                updates["CLOUDMAIL_AUTH_TOKEN"] = token
            if updates:
                write_env_values(updates)
                import config as _config_pkg
                _config_pkg.reload_all()

            domains = fetch_domains(force=True)
            written = write_env_values({"CLOUDMAIL_DOMAINS": "\n".join(domains)})
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail 域名写入后热加载失败")
            return jsonify({
                "ok": True,
                "domains": domains,
                "count": len(domains),
                "written": written,
                "message": f"已获取 {len(domains)} 个 CloudMail 可用域名并保存",
            })
        except Exception as exc:
            logger.exception("获取 CloudMail 域名失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/config")
    def api_config_set():
        data = request.get_json(silent=True) or {}
        updates = data.get("updates") if isinstance(data.get("updates"), dict) else data
        if not isinstance(updates, dict) or not updates:
            return jsonify({"ok": False, "error": "无更新内容"}), 400
        try:
            result = config_editor.update_config(updates)
        except Exception as exc:
            logger.exception("配置写入失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

        # 写盘成功后立即热加载所有 config 子模块，让运行时代码看到新值。
        reload_ok = True
        reload_err = ""
        try:
            import config as _config_pkg
            _config_pkg.reload_all()
        except Exception as exc:
            reload_ok = False
            reload_err = f"{type(exc).__name__}: {exc}"
            logger.exception("配置热加载失败")

        return jsonify({
            "ok": True,
            "updated": result["updated"],
            "ignored": result["ignored"],
            "reloaded": reload_ok,
            "note": (
                "✅ 已保存并热加载，新值立即生效"
                if reload_ok
                else f"⚠️ 已写入文件但热加载失败（{reload_err}），需重启 Web 服务才能生效"
            ),
        })

    return app
