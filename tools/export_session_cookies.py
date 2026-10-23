# -*- coding: utf-8 -*-
"""把注册窗口抓到的浏览器 cookie 落盘，供后续复用登录态。

为什么需要这个文件
------------------
roxy / 指纹浏览器注册流程跑完时，窗口本来就是已登录状态，此时
driver.get_cookies() 的产物是零成本的高价值凭证：__Secure-next-auth.session-token
是服务端签的 JWE，**只在 OAuth callback 那一刻下发**，access_token 反推不出来。
事后想再拿就得重走一遍登录（邮箱 OTP + MFA）。

产物
----
    exports/cookies/<email>.json   完整 cookie 列表 + 顺手拼好的 cookie_header
    exports/cookies/<email>.txt    一行 email----access_token----cookie_header，便于复制

用法
----
    # 由 core/roxy_registration.py 调用
    from tools.export_session_cookies import write_outputs
    write_outputs(email, cookies, access_token)

    # 独立调试：把 driver.get_cookies() 的 JSON 存成文件后
    python tools/export_session_cookies.py user@example.com cookies.json [access_token]

cookies 每项接受 Selenium（expiry）与 Playwright（expires/expirationDate）两种字段名。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = ROOT / "exports" / "cookies"

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._@-]+")
_DEFAULT_DOMAIN = ".chatgpt.com"


def safe_filename(email: str) -> str:
    """把邮箱转成安全的文件名片段，挡掉路径分隔符与 .. 之类的穿越尝试。"""
    name = _SAFE_NAME_RE.sub("_", str(email or "").strip())
    name = name.strip("._") or "unknown"
    return name[:120]


def normalize_cookie(item: dict) -> dict | None:
    """统一 Selenium / Playwright 两种 cookie 字段名，缺 name 的直接丢弃。"""
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    raw_expires = item.get("expires")
    if raw_expires is None:
        raw_expires = item.get("expiry")
    if raw_expires is None:
        raw_expires = item.get("expirationDate")
    try:
        expires = float(raw_expires) if raw_expires is not None else -1.0
    except (TypeError, ValueError):
        expires = -1.0
    same_site = str(item.get("sameSite") or item.get("same_site") or "Lax").strip() or "Lax"
    return {
        "name": name,
        "value": str(item.get("value") or ""),
        "domain": str(item.get("domain") or _DEFAULT_DOMAIN),
        "path": str(item.get("path") or "/"),
        "expires": expires,
        "httpOnly": bool(item.get("httpOnly", item.get("http_only", False))),
        "secure": bool(item.get("secure", True)),
        "sameSite": same_site,
    }


def cookie_header(cookies: list[dict]) -> str:
    """把 cookie 列表拼成 Cookie 请求头。"""
    parts = []
    for item in cookies:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        parts.append(f"{name}={item.get('value') or ''}")
    return "; ".join(parts)


def has_session_token(cookies: list[dict]) -> bool:
    """判断这批 cookie 里有没有 chatgpt.com 的登录态。"""
    return any("session-token" in str(c.get("name") or "") for c in cookies)


def write_outputs(
    email: str,
    cookies: list[dict],
    access_token: str = "",
    *,
    out_dir: Path | None = None,
) -> dict:
    """写 cookie 产物，返回 {json_path, txt_path, count, has_session}。

    参数顺序与 core/roxy_registration.py 的调用保持一致（位置传参）。
    """
    normalized = [c for c in (normalize_cookie(x) for x in (cookies or [])) if c]
    header = cookie_header(normalized)
    target_dir = Path(out_dir) if out_dir else DEFAULT_OUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    stem = safe_filename(email)
    json_path = target_dir / f"{stem}.json"
    txt_path = target_dir / f"{stem}.txt"

    payload = {
        "email": str(email or ""),
        "access_token": str(access_token or ""),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cookie_count": len(normalized),
        "has_session_token": has_session_token(normalized),
        "cookie_header": header,
        "cookies": normalized,
    }
    _atomic_write(json_path, json.dumps(payload, ensure_ascii=False, indent=2))
    _atomic_write(txt_path, "----".join([str(email or ""), str(access_token or ""), header]))

    return {
        "json_path": str(json_path),
        "txt_path": str(txt_path),
        "count": len(normalized),
        "has_session": payload["has_session_token"],
    }


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再替换，避免读到半截内容。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _load_cookies(path: Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cookies") or []
    if not isinstance(data, list):
        raise ValueError("cookies 文件必须是数组，或含 cookies 数组的对象")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把浏览器 cookie 落盘到 exports/cookies/")
    parser.add_argument("email", help="注册邮箱")
    parser.add_argument("cookies", help="driver.get_cookies() 导出的 JSON 文件")
    parser.add_argument("access_token", nargs="?", default="", help="可选 access_token")
    parser.add_argument("--out-dir", default="", help="输出目录，默认 exports/cookies")
    args = parser.parse_args(argv)

    try:
        cookies = _load_cookies(Path(args.cookies))
    except Exception as exc:
        print(f"读取 cookies 失败: {type(exc).__name__}: {exc}")
        return 1
    result = write_outputs(
        args.email,
        cookies,
        args.access_token,
        out_dir=Path(args.out_dir) if args.out_dir else None,
    )
    print(f"已写入 {result['count']} 个 cookie（session-token={'有' if result['has_session'] else '无'}）")
    print(f"  {result['json_path']}")
    print(f"  {result['txt_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
