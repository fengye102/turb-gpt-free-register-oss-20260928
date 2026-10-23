# -*- coding: utf-8 -*-
""" .env.example 与配置白名单必须同步（防止模板烂掉）。"""
from __future__ import annotations

from tools import gen_env_example


def test_env_example_matches_generator(project_root):
    expected = gen_env_example.render().replace("\n", "\r\n")
    actual = (project_root / ".env.example").read_bytes().decode("utf-8")
    assert actual == expected, "运行 python tools/gen_env_example.py 重新生成"


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
