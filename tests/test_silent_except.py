# -*- coding: utf-8 -*-
"""回归闸门：core/ 与 webui/ 里不允许再出现「静默吞掉异常」。

2026-10 的排查里发现：除了 except: pass，还有一批 except: continue 在
逐帧试探、时间戳解析、导入扫描里悄悄吃掉异常 —— 结果就是「找不到元素」
这类失败完全没有线索。这里把规则固化：except 体只写 continue/pass 一律判红，
需要真忽略就必须显式写一行 logger.debug。

tools/ 与 config/、web.py 不在范围内：那里的静默多半是 socket 关闭、
临时句柄释放这类 teardown 噪音，记日志反而脏。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCOPE = ("core", "webui")
SKIP_FILES = set()


def _silent_sites(text: str) -> list[int]:
    """返回「except 体只有 continue/pass」的行号（1 起）。"""
    lines = text.replace("\r\n", "\n").split("\n")
    hits = []
    for i, line in enumerate(lines):
        if line.strip() not in ("continue", "pass"):
            continue
        j = i - 1
        while j >= 0 and lines[j].strip() == "":
            j -= 1
        if j >= 0 and re.match(r"\s*except\b.*:\s*$", lines[j]):
            hits.append(i + 1)
    return hits


def test_no_silent_except_in_core_and_webui():
    offenders = []
    for folder in SCOPE:
        for path in sorted((ROOT / folder).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in SKIP_FILES:
                continue
            text = path.read_bytes().decode("utf-8", "replace")
            for lineno in _silent_sites(text):
                offenders.append(f"{rel}:{lineno}")
    assert offenders == [], f"这些地方又静默吞异常了：{offenders}"
