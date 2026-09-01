"""UI 窗口基类。"""

from __future__ import annotations

from abc import ABC
from typing import Any

from mabowx.core import uia
from mabowx.core.win32 import force_foreground, get_monitor_info, move_window, set_window_pos
from mabowx.logger import wxlog
from mabowx.param import WxParam


def compute_auto_resize_size(
    default_width: int,
    default_height: int,
    work_width: int,
    work_height: int,
) -> tuple[int, int]:
    """根据显示器工作区计算不会破坏双栏布局的窗口尺寸。

    微信 4.x 在窗口过窄时会切换成单栏布局，会话列表和聊天页不会同时
    显示；因此宽度必须随工作区动态放大，不能固定使用过小的 1200。
    """
    if work_width >= 1800:
        width = max(default_width, min(int(work_width * 0.55), 2000))
    elif work_width >= 1400:
        width = max(default_width, min(int(work_width * 0.85), 1680))
    else:
        width = max(960, min(default_width, work_width - 40))
    # 无论哪个区间，窗口都不能比工作区更宽，否则同样会触发异常布局。
    width = min(width, max(960, work_width - 40))
    return width, default_height


class BaseUIWnd(ABC):
    """UIA 控件窗口包装基类。"""

    _ui_cls_name: str | None = None
    _ui_name: str | None = None
    control: Any = None
    HWND: int | None = None

    def __repr__(self) -> str:
        return f"<mabowx {self.__class__.__name__} at {hex(id(self))}>"

    def __bool__(self) -> bool:
        return self.exists()

    def _lang(self, text: str) -> str:
        """语言映射钩子。V1 只支持简体中文，直接返回原文。"""
        return text

    def exists(self, wait: float = 0.0) -> bool:
        if self.control is None:
            return False
        try:
            return bool(self.control.Exists(wait))
        except Exception:
            return False

    @property
    def pid(self) -> int | None:
        if self.control is None:
            return None
        try:
            return int(self.control.ProcessId or 0) or None
        except Exception:
            return None

    def _sync_hwnd(self) -> None:
        if self.HWND is None and self.control is not None:
            try:
                hwnd = self.control.NativeWindowHandle
                self.HWND = int(hwnd) if hwnd else None
            except Exception:
                pass

    def show(self) -> None:
        """显示窗口并尽力切换到前台。"""
        self._sync_hwnd()
        if self.HWND:
            try:
                from mabowx.core.win32 import show_window

                show_window(self.HWND)
                force_foreground(self.HWND)
            except Exception:
                pass
        if self.control is not None:
            try:
                self.control.SwitchToThisWindow()
            except Exception:
                pass

    def close(self) -> None:
        """发送 Esc 关闭当前浮层/窗口。"""
        if self.control is not None and self.exists():
            try:
                self.control.SendKeys("{Esc}")
            except Exception:
                pass

    def set_window_size(
        self,
        width: int,
        height: int,
        location: tuple[int, int] | None = None,
    ) -> None:
        self._sync_hwnd()
        if self.HWND is None:
            raise RuntimeError("窗口 HWND 未知")
        if location is not None:
            move_window(self.HWND, location[0], location[1], width, height)
        else:
            set_window_pos(self.HWND, width=width, height=height)

    def auto_resize(self) -> None:
        """把窗口放到高度最大的显示器，并按工作区计算安全尺寸。

        宽度过小会让微信 4.x 切换成单栏布局；这里使用动态安全宽度，
        并保留原版的大高度以加载更多 UIA 消息控件。
        """
        try:
            monitors = get_monitor_info()
            monitor = max(monitors, key=lambda item: int(item["Height"]))
            position = tuple(monitor.get("WorkPosition") or monitor["Position"])
            work_width = int(monitor.get("WorkWidth") or monitor.get("Width") or 1600)
            work_height = int(monitor.get("WorkHeight") or monitor.get("Height") or 1000)
        except Exception:
            position = (0, 0)
            work_width, work_height = 1600, 1000
        default_width, default_height = WxParam.CHAT_WINDOW_SIZE
        width, height = compute_auto_resize_size(
            default_width, default_height, work_width, work_height
        )
        self.set_window_size(width, height, position)  # type: ignore[arg-type]


class BaseUISubWnd(BaseUIWnd):
    """依附于父窗口/根窗口的子窗口。"""

    root: BaseUIWnd | None = None
    parent: BaseUIWnd | None = None

    def _lang(self, text: str) -> str:
        if getattr(self, "parent", None):
            return self.parent._lang(text)
        if getattr(self, "root", None):
            return self.root._lang(text)
        return text
