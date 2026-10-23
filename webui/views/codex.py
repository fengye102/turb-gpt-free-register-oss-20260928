# -*- coding: utf-8 -*-
"""Codex 授权账号（CPA 兼容凭证）API。

从 app.py 的 create_app() 整块搬出来（20 条路由 + 3 个内部小工具）。这一块只依赖
core.db 与 core.codex_retry_service：补跑任务本身跑在 codex_retry_service 的线程池里，
路由只负责参数校验、状态查询和文件下载。
"""
from __future__ import annotations

import io
import json
import threading
import zipfile
from datetime import datetime

from flask import Blueprint, Response, jsonify, request

from core import codex_retry_service, db

logger = __import__("logging").getLogger(__name__)

bp = Blueprint("codex", __name__)


# ----------------------------------------------------------
# Codex 授权账号（CPA 兼容凭证）
# ----------------------------------------------------------
@bp.get("/api/codex")
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

@bp.post("/api/codex/archive")
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

@bp.post("/api/codex/archive-bulk")
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

@bp.get("/api/codex/download/<path:filename>")
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

@bp.get("/api/codex/download-from-cpa/<path:filename>")
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

@bp.post("/api/codex/download-bulk-from-cpa")
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

@bp.post("/api/codex/download-bulk")
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

@bp.post("/api/codex/reset-export")
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

@bp.post("/api/codex/delete")
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

@bp.post("/api/codex/delete-bulk")
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


@bp.post("/api/codex/stop")
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

@bp.post("/api/codex/stop-bulk")
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

@bp.post("/api/codex/reset-retrying")
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

@bp.post("/api/codex/retry")
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

@bp.post("/api/codex/retry-bulk")
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

@bp.get("/api/codex/retry-log")
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
