# -*- coding: utf-8 -*-
"""Flask 本地控制台 —— 只做装配。

职责：
    1. 建 Flask app，挂安全响应头与 gzip；
    2. 注册各域 Blueprint（见 webui/views/）；
    3. 保留三条「全局」路由：页面 / 统计概览 / 一次性下载。

业务路由按域分散在 webui/views/*.py，每个文件只依赖 core 与 api_utils，可以单独测。
所有接口返回 JSON；前端是单文件 templates/index.html（原生 JS + fetch）。
默认绑定 127.0.0.1，仅本地访问。
"""
import gzip
import logging

from flask import Flask, Response, jsonify, make_response, render_template, request

from core import db
from webui.api_utils import prepared_downloads
from webui.auth import init_auth, register_auth_routes
from webui.views import accounts as views_accounts
from webui.views import codex as views_codex
from webui.views import codex_agent as views_codex_agent
from webui.views import config as views_settings
from webui.views import jobs as views_jobs
from webui.views import pools as views_pools
from webui.views import roxy as views_roxy
from webui.views import upi as views_upi
from webui.views.upi import UPI_ZERO_AVAILABLE as _UPI_ZERO_AVAILABLE

logger = logging.getLogger(__name__)

# legacy 界面（index_legacy.html）已冻结：只修安全问题、不再加功能。
# 提醒只在首次访问时打一次，避免刷日志。
_LEGACY_UI_WARNED = False

# 「零元提链」是可选组件：缺件时 webui/views/upi.py 返回 503，不影响启动。
# 这里 re-export 一个标记，保持对老代码/冒烟用例的兼容。
# 生成好的下载包由 api_utils.prepared_downloads 统一保管（过期与上限也在那边）。

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

    # 各域路由走 Blueprint，这里只做装配。
    for _bp in (
        views_pools.bp,
        views_jobs.bp,
        views_codex.bp,
        views_codex_agent.bp,
        views_accounts.bp,
        views_roxy.bp,
        views_settings.bp,
        views_upi.bp,
    ):
        app.register_blueprint(_bp)

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
        global _LEGACY_UI_WARNED
        requested_ui = (request.args.get("ui") or "").strip().lower()
        if requested_ui in {"legacy", "modern"}:
            ui_mode = requested_ui
        else:
            ui_mode = (request.cookies.get("ui_mode") or "modern").strip().lower()
            if ui_mode not in {"legacy", "modern"}:
                ui_mode = "modern"

        template_name = "index_legacy.html" if ui_mode == "legacy" else "index.html"
        if ui_mode == "legacy" and not _LEGACY_UI_WARNED:
            _LEGACY_UI_WARNED = True
            logger.warning(
                "你正在用 legacy 界面（index_legacy.html）：它已冻结，只修安全问题、不再加功能；"
                "新功能只在 index.html。加 ?ui=modern 可切回现代界面。"
            )
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

    return app
