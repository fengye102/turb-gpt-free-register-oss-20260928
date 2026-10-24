# -*- coding: utf-8 -*-
""" .env.example 与配置白名单必须同步（防止模板烂掉）。"""
from __future__ import annotations

from tools import gen_env_example


def test_env_example_matches_generator(project_root):
    """生成器统一以 LF 落盘（配合 .gitattributes 的 eol=lf），
    所以这里直接逐字节比较 render() 的结果 —— 不能再按 CRLF 期待，
    否则 Windows/Linux 两个平台会给出不同结论。
    """
    expected = gen_env_example.render()
    actual = (project_root / ".env.example").read_bytes().decode("utf-8")
    assert actual == expected, "运行 python tools/gen_env_example.py 重新生成"


def test_env_example_has_no_crlf(project_root):
    """文件里不能出现 CR，否则 Windows 生成/检出的文件会与 Linux 不一致。"""
    raw = (project_root / ".env.example").read_bytes()
    assert b"\r" not in raw, ".env.example 必须用 LF 行尾（检查 .gitattributes / 生成器 newline）"


def test_env_example_covers_every_editable_field(project_root):
    from webui import config_editor

    text = (project_root / ".env.example").read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0] for line in text.splitlines() if "=" in line}
    missing = [f["key"] for f in config_editor.EDITABLE_FIELDS if f["key"] not in keys]
    assert missing == [], f"这些可编辑项没进 .env.example: {missing}"


def test_env_example_keys_are_empty_so_defaults_win(project_root):
    """空值 = 用 config/*.py 默认值；写死值会让复制模板的人被静默改配置。"""
    text = (project_root / ".env.example").read_text(encoding="utf-8")
    offenders = [
        line for line in text.splitlines()
        if "=" in line and not line.startswith("#") and line.split("=", 1)[1].strip()
    ]
    assert offenders == [], f"这些键带了默认值，应为空: {offenders[:5]}"
