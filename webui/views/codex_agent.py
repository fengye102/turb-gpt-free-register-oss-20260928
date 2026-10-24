# -*- coding: utf-8 -*-
"""Codex Agent Token / CPA 凭证导出 API。

从 app.py 整块搬出（10 条路由 + 4 个内部小工具）。依赖 core.db、core.codex_agent、
core.codex_oauth 与 config.sub2api，以及 api_utils 里的共享下载缓存
（导出 zip 走的是同一个 /api/downloads/<id> 通道）。
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from urllib.parse import urlparse

from flask import Blueprint, Response, jsonify, request

from core import codex_agent_service, db
from webui.api_utils import put_prepared_download

logger = logging.getLogger(__name__)

bp = Blueprint("codex_agent", __name__)


@bp.post("/api/accounts/codex-agent")
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

@bp.post("/api/accounts/codex-agent-bulk")
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

@bp.post("/api/accounts/<int:acc_id>/codex-agent/upload-sub2")
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

@bp.post("/api/accounts/codex-agent/upload-sub2-bulk")
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

@bp.get("/api/accounts/<int:acc_id>/codex-agent/download")
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

@bp.post("/api/accounts/codex-agent/download-bulk")
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

@bp.post("/api/accounts/download-cpa-bulk")
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
        download_id = put_prepared_download(zip_bytes, dl_name, "application/zip")
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
