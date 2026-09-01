"""mabowx 兼容的 uia 子模块。

Mabobot 使用 ``from mabowx import uia`` 后调用：

- ``uia.WindowControl``
- ``uia.GetRootControl`` / ``uia.GetForegroundControl``
- ``uia.ControlFromPoint`` / ``uia.ControlFromHandle``
- ``uia.Click`` / ``uia.SendKeys``
- ``uia.GetClipboardText`` / ``uia.SetClipboardText``

这些能力直接来自上游 Apache-2.0 的 ``uiautomation``。
同时补充 mabowx 自己的查找/遍历辅助函数。
"""

import sys as _sys

if _sys.platform == "win32":
    try:
        from uiautomation import *  # noqa: F401,F403
    except ImportError:
        # 兼容“只复制 mabowx 包文件夹”的部署：目标机没有独立安装
        # uiautomation 时，使用 mabowx 内嵌的上游副本。
        from ._vendor import uiautomation as _vendor_uia

        globals().update(
            {
                name: getattr(_vendor_uia, name)
                for name in dir(_vendor_uia)
                if not name.startswith("_")
            }
        )

from .core.uia import (
    activate,
    click_screen,
    control_from_handle,
    describe,
    dump_tree,
    find_controls,
    find_descendant,
    find_main_window,
    find_top_level_control,
    find_top_level_controls,
    get_focused_control,
    iter_descendants,
    right_click_screen,
)

__all__ = [
    "activate",
    "click_screen",
    "control_from_handle",
    "describe",
    "dump_tree",
    "find_controls",
    "find_descendant",
    "find_main_window",
    "find_top_level_control",
    "find_top_level_controls",
    "get_focused_control",
    "iter_descendants",
    "right_click_screen",
]
