# -*- coding: utf-8 -*-
"""WebUI 的无状态小工具。

放在这里而不是塞进 `app.py`，是为了能在**不导入 Flask 应用**（那会连带
pyotp / 注册服务等一堆重依赖）的前提下做单元测试。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid

from flask import request

logger = logging.getLogger(__name__)


class PreparedDownloads:
    """限时 + 限量的一次性下载缓存：`/api/downloads/<id>` 取走即删。

    两件事必须自带：
    - 锁：`app.run(threaded=True)` 下请求线程会同时读写同一份字典；
    - 上限：批量导出几千个账号的 zip 有几 MB 到几十 MB，只清过期项挡不住内存堆积，
      超限时按最久未使用淘汰。
    """

    def __init__(self, ttl: float = 600, limit: int = 24) -> None:
        self._ttl = float(ttl)
        self._limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    def put(self, content: bytes, filename: str, mimetype: str = "application/zip") -> str:
        """存入内容并返回一次性 download_id。"""
        now = time.time()
        download_id = uuid.uuid4().hex
        entry = {
            "content": bytes(content),
            "filename": filename,
            "mimetype": mimetype,
            "created_at": now,
        }
        with self._lock:
            self._sweep(now)
            while len(self._items) >= self._limit:
                oldest = min(self._items, key=lambda k: float(self._items[k].get("created_at") or 0))
                self._items.pop(oldest, None)
            self._items[download_id] = entry
        return download_id

    def take(self, download_id: str) -> dict | None:
        """取出并删除；不存在（或已过期被清）返回 None。"""
        with self._lock:
            return self._items.pop(str(download_id or ""), None)

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def _sweep(self, now: float) -> None:
        for key, value in list(self._items.items()):
            if now - float(value.get("created_at") or 0) > self._ttl:
                self._items.pop(key, None)


# ============================================================
# 列表 / 分页 / 日志尾巴：给路由用的无状态工具
# ============================================================

POOL_SOURCE_VALUES = frozenset(("all", "outlook", "generic_api", "imap", "cloudflare_domain"))

def pool_source_arg(default: str = "outlook") -> str:
    src = str(request.args.get("source") or "").strip().lower()
    if not src and request.method == "POST":
        data = request.get_json(silent=True) or {}
        src = str(data.get("source") or data.get("type") or "").strip().lower()
    return src if src in POOL_SOURCE_VALUES else default

def with_pool_source(rows: list[dict], source: str) -> list[dict]:
    out = []
    for r in rows:
        x = dict(r)
        x["source"] = source
        if not x.get("copy_line"):
            x["copy_line"] = x.get("email") or ""
        out.append(x)
    return out

def matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False

def paginate_items(items: list[dict], *, page: int, page_size: int) -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(500, int(page_size or 50)))
    total = len(items)
    offset = (page - 1) * page_size
    return {
        "ok": True,
        "items": items[offset:offset + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "offset": offset,
        "limit": page_size,
    }

def compact_account_for_list(row: dict) -> dict:
    """账号列表轻量对象：只返回当前表格渲染和按钮判断必需字段。

    原则：
    - 不返回完整 Token / Token 预览 / TOTP Secret / Agent Token。
    - 时间戳、错误原因、提链详情等只在前端确实要展示时返回；空值不返回。
    - 复制/下载敏感内容时再通过 /secret 接口按需读取。
    """
    out = {
        "id": row.get("id"),
        "email": row.get("email"),
        "has_access_token": bool(str(row.get("access_token") or "").strip()),
        "totp_enabled": bool(row.get("totp_secret")),
        "codex_agent_has_token": bool(str(row.get("codex_agent_token") or "").strip()),
    }

    extra_raw = row.get("extra_json")
    extra = {}
    if isinstance(extra_raw, str) and extra_raw.strip():
        try:
            extra = json.loads(extra_raw)
        except Exception:
            extra = {}
    elif isinstance(extra_raw, dict):
        extra = extra_raw
    password = str(
        extra.get("registration_password")
        or row.get("registration_password")
        or ""
    ).strip()
    if password:
        out["password"] = password
    traffic = extra.get("network_traffic") if isinstance(extra, dict) else None
    if isinstance(traffic, dict) and traffic:
        out["network_traffic"] = traffic
    # 检测（API）流量：和注册那套口径不同，前端分开展示（注册页面级 + 检测 API）
    promo_raw = row.get("promo_check_json")
    if promo_raw:
        try:
            promo_obj = json.loads(promo_raw) if isinstance(promo_raw, str) else promo_raw
            api_traffic = (promo_obj or {}).get("api_traffic") if isinstance(promo_obj, dict) else None
            if isinstance(api_traffic, dict) and api_traffic:
                out["api_traffic"] = api_traffic
        except Exception as _ignored_exc:
            logger.debug("忽略异常 %s: %s", type(_ignored_exc).__name__, _ignored_exc)

    # 这些是列表固定列直接展示字段。
    for key in (
        "user_name", "email_source", "original_email", "note", "archived", "created_at",
        "plan_type", "current_plan_type", "plus_trial_eligible",
        "promo_ok", "promo_summary", "promo_method_labels", "promo_session_type",
        "plan_check_status", "codex_status", "codex_agent_status",
        "totp_setup_status",
    ):
        if key in row:
            out[key] = row.get(key)

    if row.get("plan_check_status") in ("queued", "running") or row.get("plan_check_ok") is False:
        out["plan_check_ok"] = row.get("plan_check_ok")

    # 下面字段仅在有值时返回，避免每行堆满 null/空字符串/内部状态。
    optional_keys = (
        # 套餐展示补充：付费到期/折扣/失败原因。
        "plan_check_error", "plan_expires_at", "plan_renews_at", "renews_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id",
        "promo_countries", "promo_methods", "promo_method_labels", "promo_checked_at", "promo_error",
        "token_expired", "token_expires_at",
        # 查活状态。
        "live_check_status", "live_check_error", "live_checked_at",
        "live_check_proxy_used", "live_check_fingerprint_text",
        # 提链成功/失败时才需要。
        "extract_link_status", "extract_link_type", "extract_link_message", "extract_link_error",
        "extract_link_long_url", "extract_link_copy_paste", "extract_link_image_url_png",
        "extract_link_image_url_svg", "extract_link_expires_at",
        # Codex / Agent 状态提示。
        "codex_error", "codex_agent_message", "codex_agent_runtime_id",
        "codex_agent_sub2api_url", "codex_agent_sub2api_mode", "codex_agent_sub2api_total",
        "totp_setup_error", "totp_setup_message", "totp_setup_started_at", "totp_setup_completed_at",
        "email_change_status", "email_change_error", "email_change_new_email",
        "email_change_started_at", "email_change_completed_at",
    )
    for key in optional_keys:
        value = row.get(key)
        if value is not None and value != "":
            out[key] = value
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
    if any(x in plan for x in ("plus", "pro", "team", "go")):
        expire = row.get("expires_at")
        if expire:
            out["expires_at"] = expire
    return out

def account_secret_value(row: dict, field: str) -> str:
    field = (field or "").strip()
    if field == "access_token":
        return str(row.get("access_token") or "")
    if field == "copy_line":
        try:
            from core.db import _account_line

            return str(_account_line(row) or "")
        except Exception:
            return str(row.get("copy_line") or "")
    if field == "codex_agent_token":
        return str(row.get("codex_agent_token") or "")
    if field == "totp_secret":
        return str(row.get("totp_secret") or "")
    if field == "totp_code":
        secret = str(row.get("totp_secret") or "").strip()
        if not secret:
            return ""
        import pyotp  # 只有这一条分支需要它

        return pyotp.TOTP(secret).now()
    if field == "password":
        extra_raw = row.get("extra_json")
        extra = {}
        if isinstance(extra_raw, str) and extra_raw.strip():
            try:
                extra = json.loads(extra_raw)
            except Exception:
                extra = {}
        elif isinstance(extra_raw, dict):
            extra = extra_raw
        return str(extra.get("registration_password") or row.get("registration_password") or "未设置")
    raise ValueError("field 仅支持 access_token/copy_line/codex_agent_token/totp_secret/totp_code/password")

def compact_job_for_list(row: dict) -> dict:
    """注册任务列表轻量对象：只返回表格展示和按钮判断需要的字段。"""
    out = {
        "id": row.get("id"),
        "status": row.get("status"),
    }
    for key in (
        "parent_job_id", "retry_attempt", "email", "started_at", "completed_at",
        "display_status", "retryable", "retry_action", "retry_label",
        "manual_otp_required",
    ):
        value = row.get(key)
        if value is not None and value != "" and value is not False:
            out[key] = value
    err = str(row.get("error_message") or "").strip()
    if err:
        # 列表只需要摘要；完整错误和堆栈看“任务日志”。
        out["error_message"] = err[:240] + ("…" if len(err) > 240 else "")
    traffic = row.get("network_traffic")
    if isinstance(traffic, dict) and traffic.get("available"):
        # 流量统计只包含字节计数，不带 URL/Header/请求体，可直接随任务列表返回。
        out["network_traffic"] = traffic
    return out

def job_status_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    counts["active"] = sum(int(counts.get(s, 0) or 0) for s in ("pending", "running", "stopping"))
    return counts

def read_log_tail(path, *, max_bytes: int, default_running: bool = False, running_fn=None) -> dict:
    if not path.exists():
        return {"ok": True, "log": "", "running": bool(default_running)}
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        content = f.read().decode("utf-8", errors="replace")
    running = bool(default_running)
    if callable(running_fn):
        try:
            running = bool(running_fn())
        except Exception as _ignored_exc:
            logger.debug("忽略异常 %s: %s", type(_ignored_exc).__name__, _ignored_exc)
    return {"ok": True, "log": content, "running": running}
