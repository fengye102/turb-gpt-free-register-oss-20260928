# -*- coding: utf-8 -*-
"""
Flask 本地控制台。

职责只剩「装配」：
    1. 建 Flask app、挂安全头与 gzip；
    2. 注册各域 Blueprint（webui/views/*.py）；
    3. 保留尚未拆走的账号相关路由（/api/accounts*）。

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
from pathlib import Path

from flask import Flask, Response, jsonify, make_response, render_template, request

from core import db, live_check_service, plan_check_service
from webui.api_utils import (
    POOL_SOURCE_VALUES,
    account_secret_value,
    compact_account_for_list,
    compact_job_for_list,
    job_status_counts,
    matches_query,
    paginate_items,
    pool_source_arg,
    prepared_downloads,
    read_log_tail,
    with_pool_source,
)
from webui.auth import init_auth, register_auth_routes
from webui.views import codex as views_codex
from webui.views import config as views_settings
from webui.views import jobs as views_jobs
from webui.views import pools as views_pools
from webui.views import roxy as views_roxy
from webui.views import upi as views_upi
from webui.views import codex_agent as views_codex_agent

logger = logging.getLogger(__name__)

# 「零元提链」的接口在 webui/views/upi.py；缺件时那边返回 503，不影响启动。
from webui.views.upi import UPI_ZERO_AVAILABLE as _UPI_ZERO_AVAILABLE


# 生成好的下载包先驻留内存，由 /api/downloads/<id> 取走（过期与上限见 api_utils.PreparedDownloads）。

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
    app.register_blueprint(views_codex.bp)

    app.register_blueprint(views_roxy.bp)
    app.register_blueprint(views_settings.bp)
    app.register_blueprint(views_upi.bp)
    app.register_blueprint(views_codex_agent.bp)

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

    @app.get("/api/downloads/<download_id>")
    def api_prepared_download(download_id: str):
        item = prepared_downloads.take(download_id)
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

    return app
