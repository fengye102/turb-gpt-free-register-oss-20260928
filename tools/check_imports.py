# -*- coding: utf-8 -*-
"""内部 import 完整性检查 —— 发布前跑一遍。

背景
----
清理源码分发时最容易犯的错不是忘了删文件，而是**删了模块但调用点还在**：
core/upi_live_feed.py、core/mailcom_client.py、tools/export_session_cookies.py
都曾经这样悬空，后果是整个 WebUI 启动即 ImportError。
这个脚本把「core/config/webui/tools 下的引用能不能落到真实文件」变成一条可执行的检查。

用法
----
    python tools/check_imports.py            # 人读输出
    python tools/check_imports.py --json     # CI 消费
退出码 0 = 全部可解析，1 = 存在硬性悬空引用。

被 try/except ImportError 包住的引用（可选组件）单独归类为 optional，不算失败。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("core", "config", "webui", "tools")

# Windows 的 stdout/stderr 默认跟随控制台代码页（cp1252/cp936），输出中文会直接
# UnicodeEncodeError 崩掉（CI 的 windows-latest 就是这种情况）。这里把两个流强制
# 重配成 UTF-8；errors="replace" 保证即使目标终端不支持也不会再抛异常。
# 老版本 Python / 被替换成非 TextIOWrapper 的流没有 reconfigure，静默跳过。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
SKIP_DIRS = {"vendor", ".venv", "node_modules", "__pycache__", ".git", "build", "dist"}


def _targets(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        out.append(path)
    return sorted(out)


def _resolves(root: Path, dotted: str) -> bool:
    """dotted 形如 core.otp_utils —— 判断它在文件系统上存在。"""
    parts = dotted.split(".")
    if parts[0] not in PACKAGES:
        return True
    base = root.joinpath(*parts)
    # 目录存在也算通过：tools/ 是 PEP 420 命名空间包，没有 __init__.py。
    return base.with_suffix(".py").exists() or base.is_dir()


def _package_exports(root: Path, package: str, name: str) -> bool:
    """from config import USER_AGENT 这种「从包顶层取属性」的写法。

    判定方式：包 __init__.py 里出现过该名字（重导出或 __all__）就算通过。
    """
    init = root / package / "__init__.py"
    if not init.exists():
        return False
    try:
        text = init.read_text(encoding="utf-8")
    except OSError:
        return False
    return name in text


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    exc = handler.type
    if exc is None:
        return True
    names = []
    if isinstance(exc, ast.Name):
        names = [exc.id]
    elif isinstance(exc, ast.Tuple):
        names = [e.id for e in exc.elts if isinstance(e, ast.Name)]
    return any(n in ("ImportError", "ModuleNotFoundError", "Exception", "BaseException") for n in names)


class _Visitor(ast.NodeVisitor):
    def __init__(self, root: Path, path: Path) -> None:
        self.root = root
        self.path = path
        self.optional = 0
        self.hard: list[str] = []
        self.soft: list[str] = []

    def _record(self, dotted: str, lineno: int) -> None:
        if _resolves(self.root, dotted):
            return
        item = f"{self.path.relative_to(self.root).as_posix()}:{lineno}: {dotted}"
        (self.soft if self.optional else self.hard).append(item)

    def visit_Try(self, node: ast.Try) -> None:
        guarded = any(_catches_import_error(h) for h in node.handlers)
        if guarded:
            self.optional += 1
        for child in node.body:
            self.visit(child)
        if guarded:
            self.optional -= 1
        for handler in node.handlers:
            self.visit(handler)
        for child in list(node.orelse) + list(node.finalbody):
            self.visit(child)

    def visit_If(self, node: ast.If) -> None:
        """`if TYPE_CHECKING:` 里的引用运行期根本不执行，不可能造成 ImportError。

        这类引用（例如可选组件的类型注解）一律算 optional，不算硬悬空。
        """
        test = node.test
        guarded = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if guarded:
            self.optional += 1
        for child in list(node.body) + list(node.orelse):
            self.visit(child)
        if guarded:
            self.optional -= 1

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module.split(".")[0] not in PACKAGES:
            return
        self._record(module, node.lineno)
        # from PKG import NAME：NAME 可能是子模块，也可能是包顶层属性
        if module in PACKAGES:
            for alias in node.names:
                if alias.name == "*":
                    continue
                if not _resolves(self.root, f"{module}.{alias.name}") and not _package_exports(
                    self.root, module, alias.name
                ):
                    item = (
                        f"{self.path.relative_to(self.root).as_posix()}:{node.lineno}: "
                        f"{module}.{alias.name}"
                    )
                    (self.soft if self.optional else self.hard).append(item)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[0] in PACKAGES:
                self._record(alias.name, node.lineno)


def scan(root: Path | None = None) -> dict:
    """扫描整仓，返回 {hard: [...], soft: [...], files: n}。"""
    base = Path(root) if root else ROOT
    hard: list[str] = []
    soft: list[str] = []
    paths = _targets(base)
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            hard.append(f"{path.relative_to(base).as_posix()}: 语法错误 {exc}")
            continue
        visitor = _Visitor(base, path)
        visitor.visit(tree)
        hard.extend(visitor.hard)
        soft.extend(visitor.soft)
    return {"hard": sorted(set(hard)), "soft": sorted(set(soft)), "files": len(paths)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 core/config/webui/tools 的引用是否都能落地")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--root", default="", help="项目根目录，默认取脚本上一级")
    args = parser.parse_args(argv)

    result = scan(Path(args.root) if args.root else None)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"扫描 {result['files']} 个文件")
        if result["soft"]:
            print(f"\n可选引用（被 try/except ImportError 包住，缺失不影响启动）：{len(result['soft'])}")
            for item in result["soft"]:
                print(f"  - {item}")
        if result["hard"]:
            print(f"\n悬空引用（必须修）：{len(result['hard'])}")
            for item in result["hard"]:
                print(f"  ! {item}")
        else:
            print("\n没有悬空引用: 通过")
    return 1 if result["hard"] else 0


if __name__ == "__main__":
    sys.exit(main())
