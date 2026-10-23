# -*- coding: utf-8 -*-
"""邮箱池 API：Outlook / 通用 API / 通用 IMAP 邮箱池 + Cloudflare 域名邮箱池。

这 9 个路由原来是 create_app() 里的闭包，和其余 120 多个路由挤在同一个 3000 行文件里。
搬成 Blueprint 之后它们只依赖 db 与 webui.api_utils（没有任何闭包状态），可以单独测；
app.py 只负责装配。继续拆其它域时照这个文件的样子来。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core import db
from webui.api_utils import POOL_SOURCE_VALUES, pool_source_arg

bp = Blueprint("pools", __name__)


@bp.get("/api/outlook")
def api_outlook():
    status = request.args.get("status") or None
    limit = request.args.get("limit", default=500, type=int)
    source = pool_source_arg()
    q = str(request.args.get("q", default="") or "").strip()
    paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
    page_arg = request.args.get("page", default=None, type=int)
    page_size_arg = request.args.get("page_size", default=None, type=int)
    if paged or page_arg is not None or page_size_arg is not None:
        page = max(1, int(page_arg or 1))
        page_size = max(1, min(500, int(page_size_arg or limit or 50)))
        offset = (page - 1) * page_size
        result = db.list_email_pool_page(
            source=source, status=status, q=q, limit=page_size, offset=offset
        )
        result.update({"ok": True, "page": page, "page_size": page_size})
        return jsonify(result)
    # 兼容旧接口仍返回数组，但查询本身也只从 SQLite 读取 limit 条。
    result = db.list_email_pool_page(
        source=source, status=status, q=q, limit=max(1, int(limit or 1)), offset=0
    )
    return jsonify(result["items"])


@bp.post("/api/outlook/import")
def api_outlook_import():
    """
    粘贴文本导入邮箱素材。
    Outlook：email----password----clientId----refreshToken
    通用 API：email----code_url
    通用 IMAP：email----password 或 email:password；服务器/端口/SSL 单独传入
    分隔符兼容 ---- 与 ====。
    """
    data = request.get_json(silent=True) or {}
    source = (data.get("source") or data.get("type") or "").strip()
    if source not in ("outlook", "generic_api", "imap"):
        return jsonify({"ok": False, "error": "导入时请选择具体类型：Outlook、通用 API 或通用 IMAP"}), 400
    text = data.get("text") or ""
    as_registered = bool(data.get("as_registered", False))
    imap_server = str(data.get("imap_server") or "").strip()
    try:
        imap_port = int(data.get("imap_port") or 993)
    except (TypeError, ValueError):
        imap_port = 0
    imap_ssl_raw = data.get("imap_ssl", True)
    imap_ssl = imap_ssl_raw if isinstance(imap_ssl_raw, bool) else str(imap_ssl_raw).strip().lower() not in {"0", "false", "no", "off"}
    if source == "imap" and (not imap_server or not (1 <= imap_port <= 65535)):
        return jsonify({"ok": False, "error": "通用 IMAP 导入必须填写有效的服务器和端口"}), 400
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if source == "imap":
            if "----" in line:
                parts = line.split("----", 1)
            elif "====" in line:
                parts = line.split("====", 1)
            elif ":" in line:
                parts = line.split(":", 1)
            else:
                continue
        else:
            parts = line.split("----") if "----" in line else line.split("====")
        parts = [p.strip() for p in parts]
        if source == "generic_api":
            if len(parts) < 2:
                continue
            records.append({
                "email": parts[0],
                "code_url": parts[1],
                "access_token": parts[2] if len(parts) > 2 else "",
                "totp_secret": parts[3] if len(parts) > 3 else "",
            })
            continue
        if source == "imap":
            if len(parts) < 2 or not parts[0] or not parts[1]:
                continue
            records.append({
                "email": parts[0], "imap_password": parts[1],
                "imap_server": imap_server, "imap_port": imap_port,
                "imap_ssl": imap_ssl, "imap_username": "",
            })
            continue
        if len(parts) < 4:
            continue
        records.append({
            "email": parts[0],
            "password": parts[1],
            "client_id": parts[2],
            "refresh_token": parts[3],
            "access_token": parts[4] if len(parts) > 4 else "",
            "totp_secret": parts[5] if len(parts) > 5 else "",
        })
    if not records:
        need = ("2 段：邮箱----取码地址" if source == "generic_api" else
                "邮箱----IMAP密码 或 邮箱:IMAP密码" if source == "imap" else
                "4 段：email----password----clientId----refreshToken")
        return jsonify({"ok": False, "error": f"未解析到有效邮箱行（需 {need}，---- 或 ==== 分隔）"}), 400
    if as_registered:
        inserted, skipped = db.import_registered_email_accounts(records, source=source)
    elif source == "generic_api":
        inserted, skipped = db.import_generic_api_emails(records)
    elif source == "imap":
        inserted, skipped = db.import_imap_emails(records)
    else:
        inserted, skipped = db.import_outlook_accounts(records)
    return jsonify({
        "ok": True,
        "inserted": inserted,
        "skipped": skipped,
        "parsed": len(records),
        "as_registered": as_registered,
    })


@bp.post("/api/outlook/status")
def api_outlook_status():
    """手动改邮箱状态：body {email, status, note?, source?}。status ∈ available/used/failed/disabled。"""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    status = (data.get("status") or "").strip()
    if not email or status not in ("available", "used", "failed", "disabled"):
        return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
    source = (data.get("source") or pool_source_arg()).strip()
    if source == "all":
        source = "outlook"
    if source == "generic_api":
        db.release_generic_api_email(email, status=status, note=data.get("note"))
    elif source == "imap":
        db.release_imap_email(email, status=status, note=data.get("note"))
    elif source == "cloudflare_domain":
        db.release_domain_email(email, status=status, note=data.get("note"))
    else:
        db.release_outlook(email, status=status, note=data.get("note"))
    return jsonify({"ok": True})


@bp.post("/api/outlook/status-bulk")
def api_outlook_status_bulk():
    """批量修改邮箱状态。Body {items:[{email,source}], status, note?}。"""
    data = request.get_json(silent=True) or {}
    items = data.get("items") or data.get("emails") or []
    status = (data.get("status") or "").strip()
    note = data.get("note")
    default_source = (data.get("source") or pool_source_arg()).strip()
    if status not in ("available", "used", "failed", "disabled"):
        return jsonify({"ok": False, "error": "status 非法"}), 400
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "items/emails 必须是非空数组"}), 400
    if len(items) > 5000:
        return jsonify({"ok": False, "error": "单次最多操作 5000 个邮箱"}), 400

    updated = []
    skipped = []
    seen = set()
    for raw_item in items:
        if isinstance(raw_item, dict):
            email = (str(raw_item.get("email") or "")).strip()
            item_source = (raw_item.get("source") or default_source or "outlook").strip()
        else:
            email = (str(raw_item or "")).strip()
            item_source = default_source
        if item_source == "all":
            item_source = "outlook"
        key = f"{item_source}:{email.lower()}"
        if not email:
            skipped.append({"email": raw_item, "reason": "邮箱为空"})
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            if item_source == "generic_api":
                db.release_generic_api_email(email, status=status, note=note)
            elif item_source == "imap":
                db.release_imap_email(email, status=status, note=note)
            elif item_source == "cloudflare_domain":
                db.release_domain_email(email, status=status, note=note)
            else:
                db.release_outlook(email, status=status, note=note)
            updated.append({"email": email, "source": item_source, "status": status})
        except Exception as exc:
            skipped.append({"email": email, "source": item_source, "reason": f"{type(exc).__name__}: {exc}"})
    return jsonify({
        "ok": True,
        "updated": updated,
        "updated_count": len(updated),
        "skipped": skipped,
    })


@bp.post("/api/outlook/delete")
def api_outlook_delete():
    """从邮箱池彻底删除一个邮箱：body {email, source?}。"""
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "email 为空"}), 400
    raw_source = data.get("source") or data.get("type")
    source = (
        pool_source_arg()
        if not str(raw_source or "").strip()
        else str(raw_source).strip().lower()
    )
    if source not in POOL_SOURCE_VALUES:
        return jsonify({"ok": False, "error": "邮箱来源非法"}), 400
    deleted = db.delete_email_pool(email, source=source)
    return jsonify({"ok": True, "deleted": deleted})


@bp.post("/api/outlook/delete-bulk")
def api_outlook_delete_bulk():
    """从邮箱池批量彻底删除邮箱：body {items/emails: [...], source?}。"""
    data = request.get_json(silent=True) or {}
    raw_source = data.get("source") or data.get("type")
    source = (
        pool_source_arg()
        if not str(raw_source or "").strip()
        else str(raw_source).strip().lower()
    )
    if source not in POOL_SOURCE_VALUES:
        return jsonify({"ok": False, "error": "邮箱来源非法"}), 400
    emails = data.get("items") or data.get("emails") or []
    if not isinstance(emails, list) or not emails:
        return jsonify({"ok": False, "error": "emails/items 必须是非空数组"}), 400
    if len(emails) > 5000:
        return jsonify({"ok": False, "error": "单次最多删除 5000 个邮箱"}), 400

    deleted: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for raw_item in emails:
        if isinstance(raw_item, dict):
            email = str(raw_item.get("email") or "").strip()
            raw_item_source = raw_item.get("source") or raw_item.get("type")
            item_source = (
                source
                if not str(raw_item_source or "").strip()
                else str(raw_item_source).strip().lower()
            )
        else:
            email = (str(raw_item or "")).strip()
            item_source = source
        if not email:
            skipped.append({"email": raw_item, "reason": "邮箱为空"})
            continue
        if item_source not in POOL_SOURCE_VALUES:
            skipped.append({"email": email, "source": item_source, "reason": "邮箱来源非法"})
            continue
        key = f"{item_source}:{email.casefold()}"
        if key in seen:
            continue
        seen.add(key)
        try:
            deleted_ok = db.delete_email_pool(email, source=item_source)
        except Exception as exc:
            skipped.append({
                "email": email,
                "source": item_source,
                "reason": f"{type(exc).__name__}: {exc}",
            })
            continue
        if deleted_ok:
            deleted.append({"email": email, "source": item_source})
        else:
            skipped.append({"email": email, "reason": "邮箱不存在"})

    return jsonify({
        "ok": True,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "skipped": skipped,
    })

# ----------------------------------------------------------
# 域名邮箱池（Cloudflare 域名邮箱模式）
# ----------------------------------------------------------


@bp.get("/api/domain-pool")
def api_domain_pool():
    status = request.args.get("status") or None
    limit = request.args.get("limit", default=500, type=int)
    return jsonify(db.list_domain_email_pool(status=status, limit=limit))


@bp.post("/api/domain-pool/status")
def api_domain_pool_status():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    status = (data.get("status") or "").strip()
    if not email or status not in ("available", "used", "failed"):
        return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
    db.release_domain_email(email, status=status, note=data.get("note"))
    return jsonify({"ok": True})


@bp.post("/api/domain-pool/delete")
def api_domain_pool_delete():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "email 为空"}), 400
    deleted = db.delete_domain_email(email)
    return jsonify({"ok": True, "deleted": deleted})
