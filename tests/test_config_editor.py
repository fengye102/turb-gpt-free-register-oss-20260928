# -*- coding: utf-8 -*-
"""配置读写层：路径守卫 + 白名单自洽性。"""
from __future__ import annotations

import pytest

from webui import config_editor


@pytest.mark.parametrize("bad", ["../.env", "../../etc/passwd", "..\\email.py", ""])
def test_config_path_blocks_escape(bad):
    with pytest.raises(ValueError):
        config_editor._config_path(bad)


def test_config_path_allows_nested_path_inside_config(project_root):
    """子目录也算「在 config/ 内」，只是目前没有这种文件，守卫不该误报。"""
    path = config_editor._config_path("sub/email.py")
    assert (project_root / "config") in path.parents


def test_config_path_accepts_known_file(project_root):
    path = config_editor._config_path("email.py")
    assert path.parent == project_root / "config"
    assert path.exists()


def test_editable_fields_are_self_consistent(project_root):
    seen = set()
    for field in config_editor.EDITABLE_FIELDS:
        key = field["key"]
        assert key not in seen, f"重复的配置键: {key}"
        seen.add(key)
        assert field["type"] in {
            "bool", "int", "float", "str", "list_str_multiline"
        }, f"{key} 的类型 {field['type']} 没有对应的写回格式"
        assert (project_root / "config" / field["file"]).exists(), f"{key} 指向不存在的配置文件"


def test_tls_verify_knob_is_exposed_and_mapped(project_root):
    """MAIL_TLS_VERIFY 必须同时出现在白名单和 config.email 的 env 覆盖表里。"""
    keys = {f["key"] for f in config_editor.EDITABLE_FIELDS}
    assert "MAIL_TLS_VERIFY" in keys
    assert "MAIL_TLS_VERIFY" in (project_root / "config" / "email.py").read_text(encoding="utf-8")


def test_env_round_trip(tmp_path, monkeypatch):
    from config import env_loader

    target = tmp_path / ".env"
    monkeypatch.setattr(env_loader, "_ENV_PATH", target)
    written = env_loader.write_env_values({"MAIL_TLS_VERIFY": "False", "OTP_MAX_WAIT": "42"})
    assert set(written) == {"MAIL_TLS_VERIFY", "OTP_MAX_WAIT"}
    values = env_loader.read_env_file()
    assert values["MAIL_TLS_VERIFY"] == "False"
    assert values["OTP_MAX_WAIT"] == "42"
