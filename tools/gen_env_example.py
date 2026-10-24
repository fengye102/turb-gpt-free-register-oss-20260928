# -*- coding: utf-8 -*-
"""从 WebUI 配置白名单生成 .env.example。

为什么生成而不是手写
--------------------
WebUI 可编辑项有一百多个，手写模板必然和代码漂移：要么漏掉关键开关
（WEBUI_AUTH_CODE 就漏过），要么留下早就不存在的键。
这里以 webui/config_editor.py 的 EDITABLE_FIELDS 为唯一真源生成。

生成规则：所有键都写成 KEY=（空值）。空值在 config.env_loader 里表示
「用 config/*.py 里的默认值」，所以照抄成 .env 也不会把默认值清空。

用法
----
    python tools/gen_env_example.py            # 覆写 .env.example
    python tools/gen_env_example.py --check    # 只检查是否已同步（CI 用）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TARGET = ROOT / ".env.example"

# Windows 的 stdout/stderr 默认跟随控制台代码页（cp1252/cp936），输出中文会直接
# UnicodeEncodeError 崩掉（CI 的 windows-latest 就是这种情况）。这里把两个流强制
# 重配成 UTF-8；errors="replace" 保证即使目标终端不支持也不会再抛异常。
# 老版本 Python / 被替换成非 TextIOWrapper 的流没有 reconfigure，静默跳过。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HEADER = """# 复制成 .env 后按需填写；真实凭据不要提交。
#
# 本文件由 tools/gen_env_example.py 从 webui/config_editor.py 的配置白名单生成，
# 不要手改：改完下次生成就会被覆盖（要加配置请改 EDITABLE_FIELDS）。
#
# 空值 = 使用 config/*.py 里的默认值（config.env_loader 会把空字符串当作「未配置」），
# 所以直接复制这个文件不会改变任何默认行为。
"""


def _comment_lines(field: dict) -> list[str]:
    lines = [f"# {field.get('label') or field['key']}"]
    help_text = str(field.get("help") or "").strip()
    if help_text:
        lines.append(f"# {help_text}")
    if field.get("secret") or field.get("storage") == "env":
        lines.append("# 敏感值：只写 .env，不写回 config/*.py")
    return lines


def render() -> str:
    from webui import config_editor

    groups: dict[str, list[dict]] = {}
    for field in config_editor.EDITABLE_FIELDS:
        groups.setdefault(str(field.get("group") or "其他"), []).append(field)

    out = [HEADER.rstrip("\n")]
    for group, fields in groups.items():
        out.append("")
        out.append("# " + "=" * 60)
        out.append(f"# {group}")
        out.append("# " + "=" * 60)
        for field in fields:
            out.append("")
            out.extend(_comment_lines(field))
            out.append(f"{field['key']}=")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 / 校验 .env.example")
    parser.add_argument("--check", action="store_true", help="只检查是否与白名单同步")
    args = parser.parse_args(argv)

    # 统一用 LF 落盘：显式 newline="\n" 让 Windows 上也不做换行转换，
    # 这样 Windows 生成的文件和 Linux/macOS 逐字节一致（配合 .gitattributes 的
    # eol=lf，CI 双平台的 --check 才不会因为行尾不同而红）。
    text = render()
    if args.check:
        current = TARGET.read_bytes().decode("utf-8") if TARGET.exists() else ""
        if current != text:
            print(".env.example 与配置白名单不同步，请运行：python tools/gen_env_example.py")
            return 1
        print(".env.example 已同步")
        return 0

    with TARGET.open("w", encoding="utf-8", newline="\n") as fp:
        fp.write(text)
    from webui import config_editor

    print(f"已写入 {TARGET.name}（{len(config_editor.EDITABLE_FIELDS)} 个配置项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
