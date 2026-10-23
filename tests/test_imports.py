# -*- coding: utf-8 -*-
"""发布守卫：内部引用完整性 + 几个「删不干净」的历史坑。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from tools import check_imports


def test_no_dangling_internal_imports(project_root):
    """core/config/webui/tools 的引用必须都能落到真实文件。

    这条就是当初该拦住 core/upi_zero_web_service.py 悬空的那道闸。
    """
    result = check_imports.scan(project_root)
    assert result["hard"] == [], "存在悬空引用：\n" + "\n".join(result["hard"])


def test_optional_components_are_guarded(project_root):
    """已知缺件（提链、mail.com 改密）必须是「可选引用」，不能是硬引用。"""
    result = check_imports.scan(project_root)
    optional = "\n".join(result["soft"])
    assert "upi_zero_web_service" in optional
    assert "mailcom_client" in optional


def test_webui_app_imports_without_optional_components():
    """缺提链组件时 WebUI 必须照常 import（回归：曾经直接 ImportError 起不来）。"""
    pytest.importorskip("pyotp", reason="WebUI 依赖 pyotp；CI 会装 requirements.txt")
    import importlib

    module = importlib.import_module("webui.app")
    assert hasattr(module, "_UPI_ZERO_AVAILABLE")


def test_sentinel_js_copy_has_not_drifted(project_root):
    """sentinel/ 与 vendor/ 两份 openai_sentinel_quickjs.js 目前逐字节相同。

    两份都在用（core/sentinel_protocol.py 用仓库根那份，
    vendor 的 sentinel_quickjs.py 用自己那份），只改一边就会静默分叉。
    """
    left = (project_root / "sentinel" / "openai_sentinel_quickjs.js").read_bytes()
    right = (
        project_root / "vendor" / "gpt-register-tool" / "协议" / "openai_sentinel_quickjs.js"
    ).read_bytes()
    assert left == right, "两份 sentinel JS 已分叉：确认是有意为之，再同步它们"


def test_no_verify_false_left_in_mail_path(project_root):
    """取码通道不能再出现裸 verify=False（必须走 core.tls_policy.verify()）。"""
    # core/tls_policy.py 是策略本体，文档里必然提到这个写法，排除它。
    skip = {"core/tls_policy.py"}
    offenders = []
    for path in (project_root / "core").rglob("*.py"):
        rel = path.relative_to(project_root).as_posix()
        if rel in skip:
            continue
        if "verify=False" in path.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert offenders == [], f"这些文件里还有裸 verify=False: {offenders}"
