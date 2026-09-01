"""消息模型基类。"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any

from mabowx.core import uia
from mabowx.core.clipboard import get_text, set_text
from mabowx.core.locks import uilock
from mabowx.core.win32 import (
    force_foreground,
    get_foreground_window,
    is_windows,
    post_right_click,
)
from mabowx.logger import wxlog


HTTP_URL_PATTERN = re.compile(r"https?://[^\s<>\"',;，。；、！？）】》]+")
FOLDED_TEXT_PATTERN = re.compile(r"(?:\.{3}|…+|．{3})\s*更多\s*$")
CONTEXT_MENU_NEAR_SIDE_INSET = 96


def _first_http_url(value: str) -> str:
    match = HTTP_URL_PATTERN.search(str(value or ""))
    if not match:
        return ""
    return match.group(0).rstrip(".,，。；;、)）]】")


class Message:
    """公开消息对象。"""

    type: str = "unknown"
    mtype: str = ""
    attr: str = ""
    content: str = ""
    sender_remark: str = ""
    sender: str = ""
    time: str = ""
    id: str = ""
    hash: str = ""
    direction: str | None = None
    chat_info: dict[str, Any] = {}

    def __init__(self, control=None, parent=None) -> None:
        self.control = control
        self.parent = parent
        # attr 使用 friend / self / system 等方向标记，
        # 不是 UIA ClassName。ClassName 另存为 control_class_name，便于调试。
        self.control_class_name = (
            str(getattr(control, "ClassName", "") or "") if control is not None else ""
        )

    def __repr__(self) -> str:
        return f"<mabowx {self.__class__.__name__}({self.sender!r}: {self.content!r})>"

    def exists(self) -> bool:
        if self.control is None:
            return False
        try:
            return bool(self.control.Exists(0))
        except Exception:
            return False

    def _source_window(self):
        """返回消息所属聊天窗口包装器和顶层 UIA 控件。"""
        parent = getattr(self, "parent", None)
        window = getattr(parent, "root", None)
        window_control = getattr(window, "control", None)
        message_top = None
        try:
            message_top = self.control.GetTopLevelControl() if self.control is not None else None
        except Exception:
            pass
        return window, window_control or message_top, message_top

    def _activate_source_window(self) -> bool:
        """激活并校验消息所属窗口，避免在重叠窗口上按旧坐标误点。"""
        if self.control is None or not self.exists():
            return False

        window, window_control, message_top = self._source_window()
        if window_control is None:
            return False

        try:
            window_class = str(getattr(window_control, "ClassName", "") or "")
            if window_class and window_class not in {
                "mmui::ChatSingleWindow",
                "mmui::MainWindow",
            }:
                wxlog.warning(f"拒绝激活非聊天消息窗口: class={window_class!r}")
                return False

            target_hwnd = int(
                getattr(window, "HWND", 0)
                or getattr(window_control, "NativeWindowHandle", 0)
                or 0
            )
            message_hwnd = int(getattr(message_top, "NativeWindowHandle", 0) or 0)
            if target_hwnd and message_hwnd and target_hwnd != message_hwnd:
                wxlog.warning(
                    "拒绝点击跨窗口的消息控件: "
                    f"target_hwnd={target_hwnd} message_hwnd={message_hwnd}"
                )
                return False

            already_foreground = False
            if is_windows() and target_hwnd:
                try:
                    already_foreground = get_foreground_window() == target_hwnd
                except Exception:
                    already_foreground = False

            # 刚从同一聊天收到回调时窗口通常已经在前台。微信 UIA 的
            # SwitchToThisWindow 默认自带等待；无条件重复激活会让每次引用
            # 平白增加数百毫秒。只有目标确实不在前台时才执行激活。
            if not already_foreground:
                show = getattr(window, "show", None)
                if callable(show):
                    show()
                else:
                    try:
                        window_control.SwitchToThisWindow()
                    except Exception:
                        pass

            if is_windows() and target_hwnd:
                try:
                    if get_foreground_window() != target_hwnd:
                        force_foreground(target_hwnd)
                    if get_foreground_window() != target_hwnd:
                        wxlog.warning(
                            f"消息所属窗口无法切到前台，取消点击: hwnd={target_hwnd}"
                        )
                        return False
                except Exception as exc:
                    wxlog.warning(f"消息所属窗口前台校验失败: {exc}")
                    return False
        except Exception as exc:
            wxlog.warning(f"消息所属窗口校验失败: {exc}")
            return False
        return self.exists()

    def raw(self) -> str | None:
        if self.control is None:
            return None
        try:
            return str(self.control.Name)
        except Exception:
            return None

    def get_media_audit(self) -> dict[str, Any]:
        """Return public diagnostics for the most recent media download.

        Hosts can log verification results without depending on mabowx's
        private implementation attributes.
        """
        audit = dict(getattr(self, "_last_media_verification", {}) or {})
        audit.setdefault(
            "candidate_path",
            str(getattr(self, "_last_media_candidate_path", "") or ""),
        )
        audit.setdefault("route", str(getattr(self, "_last_media_route", "") or ""))
        audit.setdefault("delivery_id", str(getattr(self, "delivery_id", "") or ""))
        audit.setdefault("raw_message_id", str(getattr(self, "id", "") or ""))
        return audit

    @uilock
    def roll_into_view(self) -> bool:
        if self.control is None:
            return False
        if not self._activate_source_window():
            return False
        try:
            pattern = self.control.GetScrollItemPattern()
            return bool(pattern.ScrollIntoView())
        except Exception:
            return False

    def _make_hash(self, text: str) -> str:
        return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


class BaseMessage(Message):
    """所有聊天消息的基类。"""


class HumanMessage(BaseMessage):
    """真人消息。"""

    def _bias(self) -> tuple[int, int] | None:
        """计算消息气泡内适合点击的坐标。

        消息 ListItem 覆盖整行，若点击行中心可能落在空白处；
        自己消息偏右，对方消息偏左。
        """
        if self.control is None or not self.exists():
            return None
        try:
            rect = self.control.BoundingRectangle
        except Exception:
            return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None
        y = int(rect.top) + height // 2
        if self.direction == "friend":
            x = int(rect.left) + min(220, max(80, width // 5))
        elif self.direction == "self":
            x = int(rect.right) - min(220, max(80, width // 5))
        else:
            x = int(rect.left) + width // 2
        return x, y

    def _context_menu_bias(self) -> tuple[int, int] | None:
        """返回气泡靠头像一侧、适合打开右键菜单的安全坐标。

        微信 4.x 的消息 ListItem 横跨整行，短文本气泡却可能只有一百多
        像素宽。普通点击使用的较深偏移会落到短气泡右侧空白处，导致
        “复制”等菜单根本没有打开。右键操作只需落在气泡内部，因此固定
        使用靠头像一侧的小偏移；对于异常窄的控件则退回半宽位置。
        """
        if self.control is None or not self.exists():
            return None
        try:
            rect = self.control.BoundingRectangle
        except Exception:
            return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None
        inset = min(CONTEXT_MENU_NEAR_SIDE_INSET, max(1, width // 2))
        y = int(rect.top) + height // 2
        if self.direction == "friend":
            return int(rect.left) + inset, y
        if self.direction == "self":
            return int(rect.right) - inset, y
        return int(rect.left) + width // 2, y

    @uilock
    def click(self) -> None:
        if self.control is None or not self.exists():
            return
        if not self._activate_source_window():
            raise RuntimeError("消息所属聊天窗口未能安全激活，已取消点击")
        point = self._bias()
        if point is not None:
            uia.click_screen(point[0], point[1], wait=0.5)
        else:
            self.control.Click(simulateMove=False, waitTime=0.5)

    @uilock
    def right_click(self) -> None:
        if self.control is None or not self.exists():
            return
        if not self._activate_source_window():
            raise RuntimeError("消息所属聊天窗口未能安全激活，已取消右键点击")
        point = self._context_menu_bias()
        if point is not None:
            uia.right_click_screen(point[0], point[1], wait=0.4)
        else:
            self.control.RightClick(simulateMove=False, waitTime=0.4)

    @uilock
    def click_head(self, right: bool = False) -> None:
        """点击消息头像。对方头像在左，自己头像在右。"""
        if self.control is None or not self.exists():
            return
        if not self._activate_source_window():
            raise RuntimeError("消息所属聊天窗口未能安全激活，已取消头像点击")
        try:
            rect = self.control.BoundingRectangle
        except Exception:
            return
        height = int(rect.bottom - rect.top)
        y = int(rect.top) + min(50, max(24, height // 3))
        if self.direction == "friend":
            x = int(rect.left) + 42
        elif self.direction == "self":
            x = int(rect.right) - 42
        else:
            return
        if right:
            uia.right_click_screen(x, y, wait=0.4)
        else:
            uia.click_screen(x, y, wait=0.5)

    def _head_context_menu_bias(self) -> tuple[int, int] | None:
        """返回头像内适合打开“拍一拍”菜单的安全坐标。

        使用在微信 4.x 上验证过的几何：消息在左侧时取左
        头像，在右侧时取右头像，纵向最多偏移 30 像素。与普通消息气泡
        的右键点分开计算，避免短消息或高气泡把点击落到昵称/空白处。
        """
        if self.control is None or not self.exists():
            return None
        if self.direction not in {"friend", "self"}:
            return None
        try:
            rect = self.control.BoundingRectangle
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
        except Exception:
            return None
        if width < 110 or height < 35:
            return None
        horizontal = min(51, max(20, width // 8))
        vertical = min(30, max(12, height // 3), height - 2)
        x = int(rect.left) + horizontal
        if self.direction == "self":
            x = int(rect.right) - horizontal
        return x, int(rect.top) + vertical

    def _select_context_option(
        self,
        option: str,
        point: tuple[int, int],
        timeout: float,
    ):
        """在已校验坐标打开 owner-bound 菜单并选择一项。"""
        from mabowx.param import WxResponse
        from mabowx.ui.component import Menu

        window, window_control, _message_top = self._source_window()
        try:
            owner_hwnd = int(
                getattr(window, "HWND", 0)
                or getattr(window_control, "NativeWindowHandle", 0)
                or 0
            )
            parent_pid = int(
                getattr(self.parent, "pid", 0)
                or getattr(window_control, "ProcessId", 0)
                or 0
            )
        except Exception:
            owner_hwnd = 0
            parent_pid = 0

        baseline_hwnds: set[int] = set()
        if owner_hwnd and parent_pid:
            try:
                existing_menus = uia.find_top_level_controls(
                    "mmui::XMenu",
                    pid=parent_pid,
                    max_results=10,
                )
            except Exception:
                existing_menus = []
            for existing in existing_menus:
                try:
                    # Qt 会缓存并复用已经隐藏的 XMenu HWND。只有右键前仍
                    # 真实存在且可见的菜单才属于 baseline；把隐藏缓存也
                    # 记进去，会在连续引用时拒绝刚重新显示的同一 HWND。
                    if not existing.Exists(0) or bool(
                        getattr(existing, "IsOffscreen", False)
                    ):
                        continue
                    hwnd = int(getattr(existing, "NativeWindowHandle", 0) or 0)
                except Exception:
                    hwnd = 0
                if hwnd:
                    baseline_hwnds.add(hwnd)

        strict_menu = bool(owner_hwnd and parent_pid)
        # 已确认消息、坐标、PID 和所属聊天 HWND 时，直接向该窗口同步投递
        # 右键。相比移动系统鼠标并固定等待两次 0.4 秒，这既更快，也不会
        # 因用户恰好移动鼠标而把菜单开到别处。Win32 投递不可用时再回退。
        posted = bool(
            strict_menu
            and post_right_click(owner_hwnd, point[0], point[1])
        )
        if not posted:
            uia.right_click_screen(point[0], point[1], wait=0.05)
        wxlog.debug(
            "消息菜单右键已投递: "
            f"targeted={posted} owner={owner_hwnd} point={point} "
            f"visible_baseline={sorted(baseline_hwnds)!r}"
        )
        menu = Menu(
            self.parent,
            timeout=timeout,
            anchor=point,
            baseline_hwnds=baseline_hwnds if strict_menu else None,
            require_new=strict_menu,
            expected_owner_hwnd=owner_hwnd if strict_menu else None,
        )
        response = menu.select(option)
        if not response.is_success:
            menu.close()
        return response

    @uilock
    def tickle(self, timeout: float = 2.0):
        """拍一拍该条真人消息的发送者。

        右击消息头像后选择“拍一拍”。mabowx
        额外校验消息所属聊天 HWND、微信 PID、弹出菜单 owner 和菜单锚点，
        并返回 ``WxResponse``，让调用方能判断菜单项是否真正执行。
        """
        from mabowx.param import WxResponse

        if not self._activate_source_window():
            return WxResponse.failure("消息所属聊天窗口未能安全激活")
        point = self._head_context_menu_bias()
        if point is None:
            return WxResponse.failure("无法定位消息头像")
        return self._select_context_option("拍一拍", point, timeout)

    @uilock
    def select_option(self, option: str, timeout: float = 2.0):
        """右键消息并选择菜单项。"""
        from mabowx.param import WxResponse

        if not self._activate_source_window():
            return WxResponse.failure("消息所属聊天窗口未能安全激活")
        point = self._context_menu_bias()
        if point is None:
            return WxResponse.failure("无法定位消息气泡")
        return self._select_context_option(option, point, timeout)

    @uilock
    def copy_text(self, timeout: float = 3.0) -> str:
        """通过消息右键“复制”读取完整文本，并恢复原剪贴板。"""
        original = get_text()
        sentinel = f"__MABOWX_COPY_{uuid.uuid4().hex}__"
        try:
            if not self.roll_into_view():
                wxlog.debug("消息无法滚入可见区域，继续尝试当前控件")
            set_text(sentinel)
            if get_text() != sentinel:
                raise RuntimeError("无法建立剪贴板哨兵，拒绝读取旧内容")
            response = self.select_option("复制", timeout=min(2.0, timeout))
            if not response.is_success:
                raise RuntimeError(response["message"])
            deadline = time.monotonic() + max(0.5, float(timeout))
            while time.monotonic() < deadline:
                copied = get_text()
                if copied and copied != sentinel:
                    return copied
                time.sleep(0.1)
            raise RuntimeError("复制消息后剪贴板未更新")
        finally:
            try:
                set_text(original)
            except Exception as exc:
                wxlog.warning(f"恢复复制消息前的剪贴板失败: {exc}")

    def get_url(self, timeout: float = 3.0) -> str:
        """返回正文或折叠全文中的第一个 HTTP(S) URL。"""
        content = str(getattr(self, "content", "") or "")
        direct = _first_http_url(content)
        if direct:
            return direct
        copied = self.copy_text(timeout=timeout)
        copied_url = _first_http_url(copied)
        if not copied_url:
            raise RuntimeError("消息完整文本中没有 HTTP(S) URL")
        return copied_url

    def get_url_hint(self) -> dict[str, str]:
        """Classify URL handling without touching UI.

        ``kind`` is ``inline``, ``folded`` or ``card``.  An empty dictionary
        means the message has no known URL shape.
        """
        content = str(getattr(self, "content", "") or "").strip()
        direct = _first_http_url(content)
        if direct:
            return {"kind": "inline", "url": direct}
        if FOLDED_TEXT_PATTERN.search(content):
            return {"kind": "folded", "url": ""}
        if str(getattr(self, "type", "") or "") == "link" or content.startswith("[链接]"):
            return {"kind": "card", "url": ""}
        return {}

    @uilock
    def forward(self, targets, message: str | None = None, timeout: int = 3, interval: float = 0.1):
        """转发消息给一个或多个目标。"""
        from mabowx.param import WxResponse
        from mabowx.ui.component import SelectContactWnd

        if isinstance(targets, str):
            targets = [targets]
        if not targets:
            return WxResponse.failure("转发目标不能为空")
        response = self.select_option("转发...")
        if not response.is_success:
            return response
        picker = SelectContactWnd(self.parent, timeout=timeout)
        if picker.control is None:
            return WxResponse.failure("转发联系人窗口未出现")
        for target in targets:
            picker.search(target, interval=interval)
            response = picker.select(target)
            if not response.is_success:
                picker.close()
                return response
        if message:
            response = picker.add_message(message)
            if not response.is_success:
                picker.close()
                return response
        response = picker.confirm()
        if not response.is_success:
            picker.close()
        return response

    @uilock
    def quote(self, text: str, at=None, timeout: float = 3.0):
        """引用当前消息并发送指定内容。"""
        from mabowx.param import WxResponse

        if not self.exists():
            return WxResponse.failure("消息对象已失效")
        if not text:
            return WxResponse.failure("引用回复内容不能为空")
        parent = self.parent
        if parent is None:
            return WxResponse.failure("消息父窗口不存在")

        started = time.perf_counter()
        response = self.select_option("引用", timeout=timeout)
        if not response.is_success:
            return response
        selected_at = time.perf_counter()

        # 微信 4.x 把引用预览渲染为独立的 mmui::ReferView，输入框的
        # ValuePattern 在引用态仍然是空字符串。等待输入框变成非空会稳定
        # 吃满 timeout，且把引用原文再拼进正文。应等待真实引用控件。
        response = parent.wait_quote_context(timeout=timeout)
        if not response.is_success:
            return response
        context_at = time.perf_counter()

        if at:
            # 把字符串或列表整体交给 ChatBox.input_at。
            response = parent.input_at(at)
            if not response.is_success:
                return response
        # 只向普通输入框追加回复正文；ReferView 是独立富文本上下文，不能
        # 读取、复制或 Ctrl+A 覆盖。
        response = parent.append_quote_text(text, verify=True)
        if not response.is_success:
            return response
        input_at = time.perf_counter()
        response = parent.send_quote_input(text)
        finished_at = time.perf_counter()
        wxlog.debug(
            "引用回复阶段耗时: "
            f"menu={(selected_at - started) * 1000:.1f}ms "
            f"context={(context_at - selected_at) * 1000:.1f}ms "
            f"input={(input_at - context_at) * 1000:.1f}ms "
            f"send={(finished_at - input_at) * 1000:.1f}ms "
            f"total={(finished_at - started) * 1000:.1f}ms"
        )
        return response

    def reply(self, text: str, at=None):
        """回复当前消息。4.1.12 中回复等价于引用，对方消息默认 @发送者。"""
        if at is None and self.direction == "friend" and self.sender:
            at = [self.sender]
        return self.quote(text, at=at)


class NotExistsMessage(BaseMessage):
    content = "消息对象已失效"
