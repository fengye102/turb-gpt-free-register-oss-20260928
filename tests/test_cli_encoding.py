# -*- coding: utf-8 -*-
"""回归闸门：CLI 脚本在非 UTF-8 控制台（Windows cp1252）下不能崩。

windows-latest 的 stdout 编码是 cp1252，脚本一 print 中文就 UnicodeEncodeError，
整条 CI 在 Windows 上红。这里用子进程复现那种环境，两个脚本都必须退出码 0
且不出现 UnicodeEncodeError —— 在 Linux 上跑同样有效，不依赖 Windows runner。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"  # 模拟 Windows 控制台代码页
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / script), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.mark.parametrize("script,args", [("check_imports.py", ()), ("gen_env_example.py", ("--check",))])
def test_cli_survives_cp1252_stdout(script, args):
    proc = _run(script, *args)
    assert "UnicodeEncodeError" not in proc.stderr, f"{script} 在 cp1252 下仍会崩：\n{proc.stderr}"
    assert proc.returncode == 0, f"{script} 退出码 {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
