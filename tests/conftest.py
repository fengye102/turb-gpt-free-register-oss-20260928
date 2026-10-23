# -*- coding: utf-8 -*-
"""pytest 公共夹具。

目标只有一个：每个用例都在自己的临时目录里建库，
绝不碰仓库根目录的 turb.sqlite3 与真实邮箱池。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def project_root() -> Path:
    return ROOT


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """把 core.db 的落盘路径全部指向 tmp_path，并强制重新初始化 SQLite。"""
    from core import db

    monkeypatch.setattr(db, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_LOG_DIR", tmp_path / "注册日志")
    monkeypatch.setattr(db, "_ACCOUNTS_JSON", tmp_path / "注册成功的邮箱.json")
    monkeypatch.setattr(db, "_OUTLOOK_JSON", tmp_path / "用于注册的邮箱.json")
    monkeypatch.setattr(db, "_JOBS_JSON", tmp_path / "注册任务.json")
    monkeypatch.setattr(db, "_CODEX_DIR", tmp_path / "codex_accounts")
    monkeypatch.setattr(db, "_CODEX_AGENT_DIR", tmp_path / "codex_agent_accounts")
    monkeypatch.setattr(db, "_SQLITE_READY", False)
    monkeypatch.setattr(db, "_SQLITE_READY_PATH", None)
    yield db
    db.close_thread_conn()
    assert str(tmp_path) in str(db._active_sqlite_path()), "用例必须落在临时目录"
