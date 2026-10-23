# -*- coding: utf-8 -*-
"""真机启动探针：起一个真实 Flask 服务（临时数据库）并并发压一轮接口。

为什么单靠 pytest 不够：用例走的是 test_client + 临时库、同一个线程里跑，
验不出「线程局部连接在真 Web 线程池下会不会串库」「鉴权中间件会不会把页面拦掉」
「gzip 中间件会不会把 JSON 压坏」这类只在真跑起来才暴露的问题。

用法：
    python tools/live_probe.py
    python tools/live_probe.py --port 55999 --threads 12 --rounds 20

全绿退出码 0；任何一项不达标退出码 1。全程只碰临时目录，不动 turb.sqlite3。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AUTH_CODE = "live-probe-code"
PAGE = "/api/accounts?page=1&limit=20"
READ_ENDPOINTS = ("/api/summary", PAGE, "/api/jobs?page=1&limit=20")
ALL_ENDPOINTS = ("/", "/api/config") + READ_ENDPOINTS
SEED_COUNT = 20


def seed_temp_db() -> pathlib.Path:
    """把 core.db 的落盘目录指到临时目录，并塞入种子账号。"""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="live-probe-"))
    from core import db

    db._DATA_DIR = tmp
    db._LOG_DIR = tmp / "注册日志"
    db._ACCOUNTS_JSON = tmp / "注册成功的邮箱.json"
    db._OUTLOOK_JSON = tmp / "用于注册的邮箱.json"
    db._JOBS_JSON = tmp / "注册任务.json"
    db._SQLITE_READY = False
    db._SQLITE_READY_PATH = None
    for i in range(SEED_COUNT):
        db.insert_account(email="probe%d@example.com" % i, access_token="t%d" % i, email_source="outlook")
    db.close_thread_conn()
    return tmp


def start_server(port: int) -> None:
    import logging

    from webui.app import create_app

    # 探针要的是结论，不是 144 行访问日志。
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app = create_app()
    threading.Thread(
        target=app.run,
        kwargs={"host": "127.0.0.1", "port": port, "threaded": True, "use_reloader": False},
        daemon=True,
    ).start()


def wait_ready(base: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/login", timeout=2).read()
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.4)
    return False


def request(base: str, path: str, auth_code: str | None = None) -> tuple[int, bytes]:
    # 探针必须显式声明 identity：WebUI 的 gzip 中间件在没有 Accept-Encoding 时也会压缩。
    headers = {"Accept-Encoding": "identity"}
    if auth_code:
        headers["X-Auth-Code"] = auth_code
    req = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def check_auth_gate(base: str) -> list[str]:
    """没有授权码时 /api/* 必须 401，不能被绕过。"""
    failures = []
    for path in ("/api/summary", PAGE):
        status, _body = request(base, path)
        if status != 401:
            failures.append("无授权码访问 %s 期望 401，实际 %s" % (path, status))
    return failures


def check_endpoints(base: str) -> list[str]:
    failures = []
    page_payload = None
    for path in ALL_ENDPOINTS:
        status, body = request(base, path, AUTH_CODE)
        if status != 200:
            failures.append("带授权码访问 %s 期望 200，实际 %s" % (path, status))
            continue
        if path != PAGE:
            continue
        try:
            page_payload = json.loads(body.decode("utf-8"))
        except Exception as exc:
            failures.append("%s 返回的不是合法 JSON: %s" % (path, type(exc).__name__))
    if page_payload is not None:
        total = page_payload.get("total")
        items = page_payload.get("items") or []
        if total != SEED_COUNT:
            failures.append("%s 的 total 期望 %s，实际 %s" % (PAGE, SEED_COUNT, total))
        if len(items) != SEED_COUNT:
            failures.append("%s 的 items 数量期望 %s，实际 %s" % (PAGE, SEED_COUNT, len(items)))
    return failures


def check_concurrency(base: str, threads: int, rounds: int) -> tuple[list[str], float]:
    """并发读：线程局部连接一旦串库或串线程，这里会成片 500。"""
    errors: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(rounds):
            for path in READ_ENDPOINTS:
                try:
                    status, body = request(base, path, AUTH_CODE)
                    if status != 200:
                        with lock:
                            errors.append("%s -> %s" % (path, status))
                    elif path == PAGE and json.loads(body.decode("utf-8")).get("total") != SEED_COUNT:
                        with lock:
                            errors.append("%s -> total 不一致" % path)
                except Exception as exc:
                    with lock:
                        errors.append("%s -> %s: %s" % (path, type(exc).__name__, exc))

    pool = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    started = time.perf_counter()
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join(120)
    return errors, time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser(description="真机启动探针")
    parser.add_argument("--port", type=int, default=55333)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--rounds", type=int, default=6)
    args = parser.parse_args()

    seed_temp_db()
    start_server(args.port)
    base = "http://127.0.0.1:%d" % args.port
    if not wait_ready(base):
        print("服务未能在 40 秒内就绪")
        return 1

    failures = check_auth_gate(base)
    endpoint_failures = check_endpoints(base)
    failures += endpoint_failures
    errors, elapsed = check_concurrency(base, args.threads, args.rounds)
    failures += errors

    total_requests = args.threads * args.rounds * len(READ_ENDPOINTS)
    print("服务已就绪:", base)
    print("鉴权门禁:", "通过（无码 401 / 带码 200）" if not endpoint_failures else "失败")
    print(
        "并发压测: %d 线程 x %d 轮 x %d 接口 = %d 次请求，耗时 %.2fs，失败 %d 次"
        % (args.threads, args.rounds, len(READ_ENDPOINTS), total_requests, elapsed, len(errors))
    )
    for item in failures[:10]:
        print("  -", item)
    print("结论:", "全绿" if not failures else "%d 项不达标" % len(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
