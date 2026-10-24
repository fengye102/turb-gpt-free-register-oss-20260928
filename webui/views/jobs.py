# -*- coding: utf-8 -*-
"""注册任务 API：任务列表 / 创建 / 停止 / 重试 / 删除 / 日志，以及手动 OTP 通道。

从 app.py 的 create_app() 里整块搬出来（原来是 11 个闭包）。这里只依赖 db 与
core.registration_service，没有闭包状态；任务本身跑在 registration_service 的线程池里，
路由只做参数校验和状态查询。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core import db
from core import registration_service as svc
from webui.api_utils import compact_job_for_list

bp = Blueprint("jobs", __name__)


@bp.get("/api/jobs")
def api_jobs():
    limit = request.args.get("limit", default=100, type=int)
    paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
    page_arg = request.args.get("page", default=None, type=int)
    page_size_arg = request.args.get("page_size", default=None, type=int)
    from config import email as _email_cfg
    auto_otp = bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True))
    if paged or page_arg is not None or page_size_arg is not None:
        page = max(1, int(page_arg or 1))
        page_size = max(1, min(500, int(page_size_arg or limit or 50)))
        result = db.list_jobs_page(
            limit=page_size, offset=(page - 1) * page_size
        )
        rows = result.get("items") or []
        for row in rows:
            row["manual_otp_required"] = (not auto_otp) or str(row.get("email_source") or "") == "manual"
            row.update(svc.get_retry_info(row))
        result.update({"ok": True, "page": page, "page_size": page_size})
        result["items"] = [compact_job_for_list(r) for r in rows]
        result["status_counts"] = db.job_status_counts()
        result["compact"] = True
        return jsonify(result)
    rows = db.list_jobs(limit=max(1, int(limit or 1)))
    for row in rows:
        row["manual_otp_required"] = (not auto_otp) or str(row.get("email_source") or "") == "manual"
        row.update(svc.get_retry_info(row))
    return jsonify(rows)


@bp.post("/api/jobs")
def api_jobs_create():
    """启动批量注册：body {count, workers, email?, emails?}。"""
    data = request.get_json(silent=True) or {}
    from core.registration_service import _parse_manual_emails
    manual_emails = _parse_manual_emails(data.get("emails") or data.get("email") or "")
    try:
        count = int(data.get("count", 1))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "count 非法"}), 400
    if manual_emails:
        count = len(manual_emails)
    if count < 1 or count > 200:
        return jsonify({"ok": False, "error": "count 需在 1~200 之间"}), 400

    # workers 控制本次新提交任务使用的线程池；若和上次不同，服务层会为新任务切换到新池。
    try:
        workers = max(1, min(16, int(data.get("workers", 3))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "workers 非法"}), 400

    # 提交前先确认池里有足够可用邮箱，给前端一个温和提示（不阻断）
    from config import email as _email_cfg
    from config import register as _register_cfg
    from core.email_provider import parse_email_sources
    if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True)):
        reg_email = str(getattr(_register_cfg, "REGISTER_EMAIL", "") or "").strip()
        if not manual_emails:
            if not reg_email:
                return jsonify({
                    "ok": False,
                    "error": "请在注册页填写邮箱，或到配置页填写「手动注册邮箱」，或开启自动取邮箱+收码。",
                }), 400
            manual_emails = [reg_email]
            count = 1
        jobs = svc.submit_registration(count=count, workers=workers, emails=manual_emails)
        shown = "、".join(manual_emails[:3])
        extra = f" 等 {len(manual_emails)} 个" if len(manual_emails) > 3 else ""
        return jsonify({
            "ok": True,
            "submitted": len(jobs),
            "jobs": jobs,
            "warning": f"手动 OTP 模式：将使用 {shown}{extra}；验证码请在任务旁提交",
            "workers": workers,
        })
    sources = parse_email_sources(_email_cfg.EMAIL_SOURCE)
    if "gptmail" in sources:
        api_key = str(getattr(_email_cfg, "GPTMAIL_API_KEY", "") or "").strip()
        if not api_key:
            return jsonify({
                "ok": False,
                "error": "已选择 gptmail 邮箱来源，请填写 GPTMail API Key（配置 → 邮箱 / OTP）。",
            }), 400
    if "cloudflare" in sources:
        api_base = str(getattr(_email_cfg, "CLOUDFLARE_API_BASE", "") or "").strip()
        if not api_base:
            return jsonify({
                "ok": False,
                "error": "已选择 cloudflare 邮箱来源，请填写 Cloudflare API 地址（配置 → 邮箱 / OTP）。",
            }), 400
        auth_mode = str(getattr(_email_cfg, "CLOUDFLARE_AUTH_MODE", "none") or "none").strip().lower()
        accounts_path = str(getattr(_email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address") or "").strip().lower()
        api_key = str(getattr(_email_cfg, "CLOUDFLARE_API_KEY", "") or "").strip()
        needs_key = auth_mode in ("x-admin-auth", "bearer", "x-api-key", "query-key") or accounts_path.rstrip("/").endswith("/admin/new_address")
        if needs_key and not api_key:
            return jsonify({
                "ok": False,
                "error": "Cloudflare admin/鉴权模式需要填写 Cloudflare API Key（配置 → 邮箱 / OTP）。",
            }), 400
    if "mailnest" in sources:
        api_key = str(getattr(_email_cfg, "MAIL_NEST_API_KEY", "") or "").strip()
        project_code = str(getattr(_email_cfg, "MAIL_NEST_PROJECT_CODE", "") or "").strip()
        if not api_key:
            return jsonify({
                "ok": False,
                "error": "已选择 mailnest 邮箱来源，请填写 MailNest API Key（配置 → 邮箱 / OTP）。",
            }), 400
        if not project_code:
            return jsonify({
                "ok": False,
                "error": "已选择 mailnest 邮箱来源，请填写 MailNest 项目代码（配置 → 邮箱 / OTP）。",
            }), 400
    if "cloudmail" in sources:
        api_base = str(getattr(_email_cfg, "CLOUDMAIL_API_BASE", "") or "").strip()
        token = str(getattr(_email_cfg, "CLOUDMAIL_AUTH_TOKEN", "") or "").strip()
        if not api_base:
            return jsonify({
                "ok": False,
                "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail API 地址（配置 → 邮箱 / OTP）。",
            }), 400
        if not token:
            return jsonify({
                "ok": False,
                "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail Token（配置 → 邮箱 / OTP）。",
            }), 400
    if "remail" in sources:
        api_base = str(getattr(_email_cfg, "REMAIL_API_BASE", "") or "").strip()
        api_key = str(getattr(_email_cfg, "REMAIL_API_KEY", "") or "").strip()
        try:
            project_id = int(getattr(_email_cfg, "REMAIL_PROJECT_ID", 2) or 0)
        except (TypeError, ValueError):
            project_id = 0
        suffix = str(getattr(_email_cfg, "REMAIL_EMAIL_SUFFIX", "") or "").strip()
        service_mode = str(getattr(_email_cfg, "REMAIL_SERVICE_MODE", "purchase") or "purchase").strip().lower()
        if not api_base:
            return jsonify({
                "ok": False,
                "error": "已选择 remail 邮箱来源，请填写 Remail API 地址（配置 → 邮箱 / OTP）。",
            }), 400
        if not api_key:
            return jsonify({
                "ok": False,
                "error": "已选择 remail 邮箱来源，请填写 Remail API Key（配置 → 邮箱 / OTP）。",
            }), 400
        if project_id <= 0:
            return jsonify({
                "ok": False,
                "error": "已选择 remail 邮箱来源，请填写 Remail 项目 ID（配置 → 邮箱 / OTP）。",
            }), 400
        if not suffix:
            return jsonify({
                "ok": False,
                "error": "已选择 remail 邮箱来源，请填写 Remail 邮箱后缀（例如 outlook.com）。",
            }), 400
        if service_mode not in ("code", "purchase"):
            return jsonify({
                "ok": False,
                "error": "Remail 服务模式只能填写 code 或 purchase（配置 → 邮箱 / OTP）。",
            }), 400
    if manual_emails:
        warning = f"将使用手动填写的 {len(manual_emails)} 个邮箱；验证码请在任务旁提交"
    elif "gptmail" in sources or "mailnest" in sources or "cloudmail" in sources or "remail" in sources or "cloudflare" in sources:
        # 临时邮箱在任务开始时动态生成，不需要本地邮箱池容量提示。
        warning = ""
    elif "cloudflare_domain" in sources:
        pool = db.domain_email_pool_summary()
        warning = ""
        if sources == ["cloudflare_domain"] and pool.get("available", 0) < count:
            warning = f"域名邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会自动生成"
    elif sources == ["generic_api"]:
        pool = db.generic_api_email_pool_summary()
        warning = ""
        if pool.get("available", 0) < count:
            warning = f"通用 API 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
    elif sources == ["imap"]:
        pool = db.imap_email_pool_summary()
        warning = ""
        if pool.get("available", 0) < count:
            warning = f"通用 IMAP 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
    elif len(sources) > 1:
        available = 0
        if "outlook" in sources:
            available += db.outlook_pool_summary().get("available", 0)
        if "generic_api" in sources:
            available += db.generic_api_email_pool_summary().get("available", 0)
        if "imap" in sources:
            available += db.imap_email_pool_summary().get("available", 0)
        warning = ""
        if available < count:
            warning = f"多个邮箱池合计仅 {available} 个可用，少于任务数 {count}，不足的会失败"
    else:
        pool = db.outlook_pool_summary()
        warning = ""
        if pool.get("available", 0) < count:
            warning = f"可用邮箱仅 {pool.get('available', 0)} 个，少于任务数 {count}，不足的会失败"
    submit_kwargs = {"count": count, "workers": workers}
    if manual_emails:
        submit_kwargs["emails"] = manual_emails
    jobs = svc.submit_registration(**submit_kwargs)
    return jsonify({"ok": True, "submitted": len(jobs), "jobs": jobs, "warning": warning, "workers": workers})


@bp.get("/api/manual-otp/waiting")
def api_manual_otp_waiting():
    """列出当前正在等待手动验证码的邮箱。"""
    from core.manual_otp import list_waiting
    return jsonify({"ok": True, "waiting": list_waiting()})


@bp.post("/api/manual-otp")
def api_manual_otp_submit():
    """提交手动邮箱验证码。Body: {email, code} 或 {job_id, code}。"""
    from core.manual_otp import submit_manual_otp
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or data.get("otp") or "").strip()
    email = (data.get("email") or "").strip()
    job_id = data.get("job_id")
    if not email and job_id is not None:
        job = db.get_job(int(job_id))
        email = (job or {}).get("email") or ""
    if not email:
        return jsonify({"ok": False, "error": "email/job_id 缺失"}), 400
    try:
        result = submit_manual_otp(email, code)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400


@bp.post("/api/jobs/cancel-pending")
def api_jobs_cancel_pending():
    """取消所有还在排队（status=pending）的任务。已在 running 的不动。"""
    cancelled = svc.cancel_pending_jobs()
    return jsonify({"ok": True, "cancelled": cancelled})


@bp.post("/api/jobs/<int:job_id>/stop")
def api_job_stop(job_id: int):
    """手动停止单个注册任务。pending 取消；running 发送停止信号。"""
    result = svc.request_stop_job(job_id)
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error") or "停止失败"}), int(result.get("status") or 400)
    return jsonify(result)


@bp.post("/api/jobs/<int:job_id>/retry")
def api_job_retry(job_id: int):
    """重试失败/停止/取消任务；服务端自动判断完整注册或 Codex 补跑。"""
    data = request.get_json(silent=True) or {}
    try:
        workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "workers 非法"}), 400
    result = svc.retry_job(job_id, workers=workers)
    if not result.get("ok"):
        return jsonify(result), int(result.get("status") or 400)
    return jsonify(result)


@bp.post("/api/jobs/retry-bulk")
def api_jobs_retry_bulk():
    """批量重试任务；不支持项逐条跳过并返回原因。"""
    data = request.get_json(silent=True) or {}
    job_ids = data.get("job_ids") or data.get("ids") or []
    if not isinstance(job_ids, list) or not job_ids:
        return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
    if len(job_ids) > 500:
        return jsonify({"ok": False, "error": "单次最多重试 500 个任务"}), 400
    try:
        workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "workers 非法"}), 400

    started: list[dict] = []
    reused: list[dict] = []
    skipped: list[dict] = []
    seen: set[int] = set()
    for raw_id in job_ids:
        try:
            one_id = int(raw_id)
        except (TypeError, ValueError):
            skipped.append({"id": raw_id, "reason": "ID 非法"})
            continue
        if one_id in seen:
            continue
        seen.add(one_id)
        result = svc.retry_job(one_id, workers=workers)
        if not result.get("ok"):
            skipped.append({"id": one_id, "reason": result.get("error") or "不能重试"})
        elif result.get("reused"):
            reused.append(result)
        else:
            started.append(result)
    return jsonify({
        "ok": True,
        "started": started,
        "started_count": len(started),
        "reused": reused,
        "reused_count": len(reused),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "workers": workers,
    })


@bp.post("/api/jobs/<int:job_id>/delete")
def api_job_delete(job_id: int):
    """删除一个任务记录。运行中的任务不允许删除；排队任务删除后执行前会自动跳过。"""
    job = db.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    if job.get("status") in ("running", "stopping"):
        return jsonify({"ok": False, "error": "运行中的任务不能删除，请等待完成后再删"}), 409
    deleted = db.delete_job(job_id, delete_log=True, allow_running=False)
    if not deleted:
        return jsonify({"ok": False, "error": "任务不存在或已开始运行"}), 409
    return jsonify({"ok": True, "deleted": deleted})


@bp.post("/api/jobs/delete-bulk")
def api_jobs_delete_bulk():
    """批量删除任务记录。running 任务跳过，其它任务删除记录和日志。"""
    data = request.get_json(silent=True) or {}
    job_ids = data.get("job_ids") or data.get("ids") or []
    if not isinstance(job_ids, list) or not job_ids:
        return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
    if len(job_ids) > 1000:
        return jsonify({"ok": False, "error": "单次最多删除 1000 个任务"}), 400

    deleted: list[int] = []
    skipped: list[dict] = []
    seen: set[int] = set()
    for raw_id in job_ids:
        try:
            job_id = int(raw_id)
        except (TypeError, ValueError):
            skipped.append({"id": raw_id, "reason": "ID 非法"})
            continue
        if job_id in seen:
            continue
        seen.add(job_id)

        job = db.get_job(job_id)
        if not job:
            skipped.append({"id": job_id, "reason": "任务不存在"})
            continue
        if job.get("status") in ("running", "stopping"):
            skipped.append({"id": job_id, "reason": "运行中，不能删除"})
            continue
        if db.delete_job(job_id, delete_log=True, allow_running=False):
            deleted.append(job_id)
        else:
            skipped.append({"id": job_id, "reason": "任务不存在或已开始运行"})

    return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})


@bp.get("/api/jobs/<int:job_id>/log")
def api_job_log(job_id: int):
    job = db.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({
        "ok": True,
        "job": job,
        "log": svc.read_job_log(job_id),
    })
