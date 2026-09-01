"""UIA 树 dump 工具（只读，不操作微信界面）。

用法：
    python -m mabowx.tools.uia_dump --max-nodes 500 --out uia_tree.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dump 微信主窗口 UIA 树（只读）")
    parser.add_argument("--name", default="微信")
    parser.add_argument("--class-name", default="mmui::MainWindow")
    parser.add_argument("--max-nodes", type=int, default=500)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--activate", action="store_true", help="是否先将微信窗口切到前台")
    args = parser.parse_args(argv)

    try:
        from mabowx.core import uia
    except Exception as exc:  # pragma: no cover - 仅在 Linux 上给友好提示
        print(f"mabowx UIA 工具只能在 Windows 上运行: {exc}", file=sys.stderr)
        return 2

    win = uia.find_main_window(name=args.name, class_name=args.class_name)
    if args.activate:
        uia.activate(win)
    lines = uia.dump_tree(win, max_nodes=args.max_nodes)

    text = "\n".join(lines) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"已写入: {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
