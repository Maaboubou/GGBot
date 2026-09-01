"""聊天输入框与发送。"""

from __future__ import annotations

import difflib
import time
from pathlib import Path
from typing import Any

from mabowx.core import uia
from mabowx.core.clipboard import set_files, set_text
from mabowx.core.locks import ui_transaction, uilock
from mabowx.core.operation_sequencer import OrderedOperationSequencer
from mabowx.core.win32 import post_right_click
from mabowx.logger import wxlog
from mabowx.msgs import classify, make_message, parse_content
from mabowx.msgs.identity import attach_delivery_context
from mabowx.utils.tools import detect_message_direction
from mabowx.param import WxParam, WxResponse

from .base import BaseUISubWnd, BaseUIWnd


GROUP_SENDER_HEAD_CLASS = "mmui::ContactHeadView"
GROUP_SENDER_FOCUS_TIMEOUT_SEC = 0.22
MENTION_OBJECT_CHARACTER = "\ufffc"


def group_sender_head_point(control, direction: str | None) -> tuple[int, int] | None:
    """返回头像探测点（屏幕绝对坐标）。"""
    if direction not in {"friend", "self"}:
        return None
    try:
        rect = control.BoundingRectangle
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
    except Exception:
        return None
    if width < 110 or height < 35:
        return None
    horizontal = min(51, max(20, width // 8))
    vertical = min(30, max(12, height // 3), height - 2)
    x = int(rect.left) + horizontal
    if direction == "self":
        x = int(rect.right) - horizontal
    return x, int(rect.top) + vertical


def group_sender_from_focused_control(
    message_control,
    focused_control,
    direction: str | None,
) -> str:
    """校验隐藏头像焦点控件并返回群成员显示名。

    微信 4.1.x 的消息 ``ListItem`` 没有 UIA 子节点；在头像处右键后，
    ``GetFocusedControl`` 会临时返回 ``ContactHeadView``，其 ``Name``
    就是群里显示的发送者。这里同时校验类名、顶层 HWND、矩形包含关系
    和左右方向，避免把其他窗口或消息菜单的焦点误认成发送者。
    """
    if message_control is None or focused_control is None:
        return ""
    if direction not in {"friend", "self"}:
        return ""
    try:
        if str(getattr(focused_control, "ClassName", "") or "") != GROUP_SENDER_HEAD_CLASS:
            return ""
        if str(getattr(focused_control, "ControlTypeName", "") or "") != "ButtonControl":
            return ""
        name = str(getattr(focused_control, "Name", "") or "").strip()
        if not name:
            return ""

        message_rect = message_control.BoundingRectangle
        head_rect = focused_control.BoundingRectangle
        message_left = int(message_rect.left)
        message_top = int(message_rect.top)
        message_right = int(message_rect.right)
        message_bottom = int(message_rect.bottom)
        head_left = int(head_rect.left)
        head_top = int(head_rect.top)
        head_right = int(head_rect.right)
        head_bottom = int(head_rect.bottom)
        if head_right <= head_left or head_bottom <= head_top:
            return ""
        margin = 4
        if (
            head_left < message_left - margin
            or head_right > message_right + margin
            or head_top < message_top - margin
            or head_bottom > message_bottom + margin
        ):
            return ""
        width = message_right - message_left
        head_center = (head_left + head_right) / 2
        side_limit = min(240, max(110, int(width * 0.28)))
        if direction == "friend" and head_center > message_left + side_limit:
            return ""
        if direction == "self" and head_center < message_right - side_limit:
            return ""

        try:
            message_top_control = message_control.GetTopLevelControl()
            head_top_control = focused_control.GetTopLevelControl()
            message_hwnd = int(getattr(message_top_control, "NativeWindowHandle", 0) or 0)
            head_hwnd = int(getattr(head_top_control, "NativeWindowHandle", 0) or 0)
            if message_hwnd and head_hwnd and message_hwnd != head_hwnd:
                return ""
        except Exception:
            # 某些测试替身和旧 UIA Provider 没有顶层句柄；矩形/类名校验
            # 仍足以使用，真机对象则会走上面的严格分支。
            pass
        return name
    except Exception:
        return ""


def _message_signature_content(message) -> str:
    content = str(getattr(message, "content", "") or "")
    if getattr(message, "type", "") == "quote":
        nickname = str(getattr(message, "quote_nickname", "") or "")
        quoted = str(getattr(message, "quote_content", "") or "")
        return f"{content}\x1f{nickname}\x1f{quoted}"
    return content


def message_signature(message) -> str:
    return (
        f"{message.type}|{_message_signature_content(message)}|"
        f"{message.sender}|{message.time}|{message.attr}"
    )


def message_anchor_signature(message) -> str:
    """用于 UIA 尾锚点的稳定签名，不包含可能延迟变化的时间/发送者。"""
    return f"{message.type}|{_message_signature_content(message)}|{message.attr}"


MESSAGE_SIGNATURE_TTL_SEC = 15.0
MUTABLE_MEDIA_ANCHOR_TYPES = frozenset({"image", "video", "voice", "file"})


def _anchor_type_attr(signature: str) -> tuple[str, str]:
    """从 ``type|content|attr`` 中取不受 content 内竖线影响的两端字段。"""
    msg_type, separator, _rest = str(signature or "").partition("|")
    _head, tail_separator, attr = str(signature or "").rpartition("|")
    if not separator or not tail_separator:
        return "", ""
    return msg_type, attr


def _is_mutating_self_media_anchor(previous_signature: str, current_signature: str) -> bool:
    """判断同一 RuntimeId 是否只是自发媒体从上传态变成最终态。"""
    previous_type, previous_attr = _anchor_type_attr(previous_signature)
    current_type, current_attr = _anchor_type_attr(current_signature)
    return bool(
        previous_type in MUTABLE_MEDIA_ANCHOR_TYPES
        and current_type in MUTABLE_MEDIA_ANCHOR_TYPES
        and (previous_attr == "self" or current_attr == "self")
    )


def filter_recent_signatures(
    messages: list,
    recent: dict[str, float],
    now: float,
    ttl: float = MESSAGE_SIGNATURE_TTL_SEC,
) -> list:
    """过滤短时间内已经出现过的消息签名。

    微信消息列表滚动时旧消息可能获得新 RuntimeId；只靠 id 去重会把
    旧消息误判为新消息。用最近签名做第二道短 TTL 过滤。
    """
    result: list = []
    for message in messages:
        signature = message_signature(message)
        last_seen = recent.get(signature)
        if last_seen is None or now - last_seen > ttl:
            result.append(message)
    return result


def diff_new_messages(
    messages: list,
    used_ids: set[str],
    last_signatures: dict[str, str],
) -> tuple[list, set[str], dict[str, str]]:
    """纯函数：根据 id + 内容签名计算新消息。

    返回 ``(new_messages, next_used_ids, next_signatures)``。
    """
    new_messages: list = []
    next_ids: set[str] = set()
    next_signatures: dict[str, str] = {}
    for message in messages:
        if not message.id:
            continue
        signature = message_signature(message)
        next_ids.add(message.id)
        next_signatures[message.id] = signature
        old_signature = last_signatures.get(message.id)
        if message.id not in used_ids:
            new_messages.append(message)
        elif old_signature is not None and old_signature != signature:
            new_messages.append(message)
    return new_messages, next_ids, next_signatures


def messages_after_anchor(
    messages: list,
    anchor_id: str,
    anchor_signature: str,
) -> tuple[list, bool]:
    """返回上一轮尾消息之后的消息。

    微信 4.x 会虚拟化消息列表：滚动、切换前台或打开内置浏览器后，
    可见旧消息可能获得全新的 RuntimeId。仅靠 RuntimeId/短时签名去重
    仍可能重放旧消息，因此以“上一轮可见列表的最后一条消息”为锚点。

    先匹配 RuntimeId + 内容签名；UIA 重建导致 RuntimeId 改变时，再按
    内容签名匹配最后一个同签名项。找不到锚点时返回空列表，调用方应
    暂停派发，而不是把整页虚拟化旧消息当成新消息。
    """
    if not anchor_id and not anchor_signature:
        return list(messages), True

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if (
            str(getattr(message, "id", "") or "") == anchor_id
            and message_anchor_signature(message) == anchor_signature
        ):
            return list(messages[index + 1 :]), True

    # 自发图片/视频在发送过程中会原地经历“上传中 -> 最终时长/缩略图”变形，
    # RuntimeId 通常不变，但 content（偶尔连 type）会变。若仍坚持内容签名
    # 完全相等，紧随其后的朋友消息会在锚点缺失时被静默吞入新基线。
    if anchor_id:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if str(getattr(message, "id", "") or "") != anchor_id:
                continue
            if _is_mutating_self_media_anchor(
                anchor_signature,
                message_anchor_signature(message),
            ):
                return list(messages[index + 1 :]), True

    if anchor_signature:
        for index in range(len(messages) - 1, -1, -1):
            if message_anchor_signature(messages[index]) == anchor_signature:
                return list(messages[index + 1 :]), True
    return [], False


def messages_after_previous_overlap(
    messages: list,
    previous_snapshot: tuple[tuple[str, str], ...] | list[tuple[str, str]],
) -> tuple[list, bool]:
    """尾锚点变形/重建后，用更早的可见重叠项恢复确定的新消息后缀。

    优先匹配 RuntimeId + 稳定签名，其次允许同 RuntimeId 的自发媒体变形，
    最后才用稳定签名匹配 UIA 整体重建。只返回匹配项之后的消息；完全没有
    重叠时仍保持保守抑制，避免把滚动到的旧页面整页重放。
    """
    if not messages or not previous_snapshot:
        return [], False

    current_snapshot = [
        (
            str(getattr(message, "id", "") or ""),
            message_anchor_signature(message),
        )
        for message in messages
    ]

    def _result_after(previous_index: int, current_index: int) -> tuple[list, bool]:
        candidates = list(messages[current_index + 1 :])
        # 更早的重叠项之后可能还夹着未变形的旧控件。按完整 token 逐个扣除，
        # 但不按内容签名全局去重；这样 RuntimeId 不同的连续相同文本仍会保留。
        remaining_old = list(previous_snapshot[previous_index + 1 :])
        filtered: list = []
        for message in candidates:
            token = (
                str(getattr(message, "id", "") or ""),
                message_anchor_signature(message),
            )
            try:
                old_index = remaining_old.index(token)
            except ValueError:
                filtered.append(message)
            else:
                remaining_old.pop(old_index)
        return filtered, True

    # 1) 最可靠：完整 RuntimeId + 稳定签名重叠。
    for previous_index in range(len(previous_snapshot) - 1, -1, -1):
        token = tuple(previous_snapshot[previous_index])
        for current_index in range(len(current_snapshot) - 1, -1, -1):
            if current_snapshot[current_index] == token:
                return _result_after(previous_index, current_index)

    # 2) 同一 RuntimeId 的自发媒体控件原地变形。
    for previous_index in range(len(previous_snapshot) - 1, -1, -1):
        previous_id, previous_signature = previous_snapshot[previous_index]
        if not previous_id:
            continue
        for current_index in range(len(current_snapshot) - 1, -1, -1):
            current_id, current_signature = current_snapshot[current_index]
            if current_id == previous_id and _is_mutating_self_media_anchor(
                previous_signature,
                current_signature,
            ):
                return _result_after(previous_index, current_index)

    # 3) UIA 整体重建：RuntimeId 全变时以稳定签名找最后一个重叠项。
    for previous_index in range(len(previous_snapshot) - 1, -1, -1):
        previous_signature = previous_snapshot[previous_index][1]
        if not previous_signature:
            continue
        for current_index in range(len(current_snapshot) - 1, -1, -1):
            if current_snapshot[current_index][1] == previous_signature:
                return _result_after(previous_index, current_index)
    return [], False


def text_similarity(left: str, right: str) -> float:
    if not left and not right:
        return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


class ChatMoreInfoWnd:
    """群资料面板。

    群昵称读取是微信 UI 适配能力，必须由 mabowx 自己完成。宿主程序只
    读取字符串，不需要替换类、遍历 UIA 控件或拦截写入方法。
    """

    _more_button_aid = (
        "content_view.top_content_view.title_h_view.right_v_view."
        "right_content_h_view.right_content_v_view.right_ui_.more_button"
    )
    _panel_class_name = "mmui::ChatRoomMemberInfoView"

    def __init__(self, parent=None) -> None:
        self.parent = parent
        self.root = getattr(parent, "control", None)
        self.panel = None
        self.more_button = None
        self._opened_by_us = False
        self._open()

    def _find_open_panel(self, timeout: float = 0.3):
        if self.root is None:
            return None
        return uia.find_descendant(
            self.root,
            class_name=self._panel_class_name,
            timeout=timeout,
        )

    def _open(self) -> None:
        if self.root is None:
            return
        show = getattr(self.parent, "show", None)
        if callable(show):
            try:
                show()
                time.sleep(0.2)
            except Exception:
                pass
        existing = self._find_open_panel(timeout=0.2)
        if existing is not None:
            self.panel = existing
            return
        more = uia.find_descendant(
            self.root,
            control_type="ButtonControl",
            name="聊天信息",
            automation_id=self._more_button_aid,
            timeout=1.5,
        )
        if more is None:
            raise RuntimeError("未找到聊天信息按钮")
        self.more_button = more
        try:
            rect = more.BoundingRectangle
            uia.click_screen(
                int(rect.left + (rect.right - rect.left) // 2),
                int(rect.top + (rect.bottom - rect.top) // 2),
                wait=0.6,
            )
        except Exception:
            more.Click(simulateMove=False, waitTime=0.6)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            panel = self._find_open_panel(timeout=0.2)
            if panel is not None:
                self.panel = panel
                self._opened_by_us = True
                return
            time.sleep(0.08)
        raise RuntimeError("聊天信息侧栏未打开")

    @staticmethod
    def _control_identity(control) -> tuple:
        try:
            runtime_id = tuple(int(value) for value in control.GetRuntimeId())
        except Exception:
            runtime_id = ()
        if runtime_id:
            return ("runtime", *runtime_id)
        try:
            rect = control.BoundingRectangle
            rectangle = (
                int(rect.left),
                int(rect.top),
                int(rect.right),
                int(rect.bottom),
            )
        except Exception:
            rectangle = ()
        try:
            return (
                "properties",
                str(getattr(control, "Name", "") or ""),
                str(getattr(control, "ClassName", "") or ""),
                str(getattr(control, "AutomationId", "") or ""),
                rectangle,
            )
        except Exception:
            return ("object", id(control))

    def get_item_control(self, item: str, *, max_tabs: int = 64):
        """Find a virtualized chat-info row using keyboard focus traversal.

        WeChat 4.1 renders the settings below the member grid, but those rows
        are not children of the exposed UIA tree.  They become real controls
        only while Tab moves focus through the side panel.  This is the same
        navigation used by the former application-side compatibility patch, now owned
        by mabowx and bounded to this exact chat window.
        """
        if self.root is None or self.panel is None:
            return None
        try:
            expected_hwnd = int(getattr(self.root, "NativeWindowHandle", 0) or 0)
        except Exception:
            expected_hwnd = 0
        try:
            self.root.SetFocus()
        except Exception:
            try:
                self.panel.SetFocus()
            except Exception:
                return None

        seen: set[tuple] = set()
        for _ in range(max(1, int(max_tabs))):
            try:
                self.root.SendKeys("{Tab}", waitTime=0.03)
                control = uia.get_focused_control()
            except Exception:
                continue
            if control is None:
                continue
            identity = self._control_identity(control)
            if identity in seen:
                break
            seen.add(identity)
            try:
                top = control.GetTopLevelControl()
                top_hwnd = int(getattr(top, "NativeWindowHandle", 0) or 0)
                if expected_hwnd and top_hwnd != expected_hwnd:
                    break
                name = str(getattr(control, "Name", "") or "").strip()
                control_type = str(
                    getattr(control, "ControlTypeName", "") or ""
                )
            except Exception:
                continue
            if name == item and control_type == "ButtonControl":
                return control
        return None

    def close(self) -> bool:
        """Close only the side panel opened by this instance.

        Never send Esc to the chat root: when panel opening failed, Esc closes
        the detached listener window itself.
        """
        if not self._opened_by_us or self.root is None:
            return True
        panel = self._find_open_panel(timeout=0.2)
        if panel is None:
            self._opened_by_us = False
            return True
        more = self.more_button or uia.find_descendant(
            self.root,
            control_type="ButtonControl",
            name="聊天信息",
            automation_id=self._more_button_aid,
            timeout=0.5,
        )
        if more is None:
            return False
        try:
            rect = more.BoundingRectangle
            uia.click_screen(
                int(rect.left + (rect.right - rect.left) // 2),
                int(rect.top + (rect.bottom - rect.top) // 2),
                wait=0.3,
            )
        except Exception:
            return False
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if self._find_open_panel(timeout=0.1) is None:
                self._opened_by_us = False
                self.panel = None
                return True
            time.sleep(0.05)
        return False

    @staticmethod
    def _text_values(control, *, max_nodes: int = 80) -> list[str]:
        ignored = {"我在本群的昵称", "编辑", "进入", "返回"}
        result: list[str] = []
        for candidate in (control, *list(uia.iter_descendants(control, max_nodes=max_nodes))):
            try:
                name = str(getattr(candidate, "Name", "") or "").strip()
                control_type = str(
                    getattr(candidate, "ControlTypeName", "") or ""
                )
                class_name = str(getattr(candidate, "ClassName", "") or "")
            except Exception:
                continue
            if (
                name
                and name not in ignored
                and len(name) <= 64
                and (
                    control_type in {"TextControl", "EditControl"}
                    or class_name == "mmui::XTextView"
                )
                and name not in result
            ):
                result.append(name)
        return result

    def _nickname_setting_candidates(self) -> list[str]:
        """Read only the setting row labelled ``我在本群的昵称``."""
        if self.root is None:
            return []
        label = self.get_item_control("我在本群的昵称")
        if label is None:
            # Compatibility fallback for builds that expose the row normally.
            label = uia.find_descendant(
                self.root,
                control_type="ButtonControl",
                name="我在本群的昵称",
                timeout=0.5,
            )
        if label is None:
            return []

        # The value is a descendant in 4.1.12 and may be a sibling in other
        # 4.x builds. Walk only a bounded row ancestry; never inspect members.
        row = label
        for _ in range(4):
            values = self._text_values(row)
            if values:
                return values
            try:
                parent = row.GetParentControl()
            except Exception:
                parent = None
            if parent is None or parent is self.root:
                break
            row = parent
        return []

    def get_my_nickname(self) -> str:
        candidates = self._nickname_setting_candidates()
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise RuntimeError(f"群昵称设置值不唯一: {candidates!r}")
        raise RuntimeError("未读取到“我在本群的昵称”")

    def set_my_nickname(self, value):
        raise NotImplementedError("当前版本仅支持读取群昵称")

    def set_group_name(self, value):
        return None

    def set_group_remark(self, value):
        return None

    def set_announcement(self, value):
        return None


class ChatBox(BaseUISubWnd):
    """聊天页面：输入框、发送按钮、消息列表。"""

    _ui_cls_name = "mmui::ChatMessagePage"

    def __init__(self, root: BaseUIWnd) -> None:
        self.root = root
        self.parent = None
        self.control = None
        self._input = None
        self._send_btn = None
        self._message_list = None
        self._last_time = ""
        self._used_msg_ids: set[str] = set()
        self._last_signatures: dict[str, str] = {}
        self._recent_signatures: dict[str, float] = {}
        self._tail_message_id = ""
        self._tail_message_signature = ""
        self._visible_message_snapshot: tuple[tuple[str, str], ...] = ()
        self._anchor_miss_snapshot: tuple[tuple[str, str], ...] | None = None
        self._anchor_miss_rounds = 0
        self._sent_texts: dict[str, float] = {}
        self._sent_filenames: dict[str, float] = {}
        self._direction_cache: dict[str, str] = {}
        self._cache_chat_name: str | None = None
        self._delivery_sequence = 0
        self._media_operation_sequencer = OrderedOperationSequencer()
        self.init()

    def init(self) -> None:
        if self.root is None or not self.root.exists():
            return
        self.control = uia.find_descendant(
            self.root.control,
            class_name=self._ui_cls_name,
            timeout=2.0,
        )
        if self.control is None:
            self.control = self.root.control
        self._input = uia.find_descendant(
            self.root.control,
            control_type="EditControl",
            class_name="mmui::ChatInputField",
            automation_id="chat_input_field",
            timeout=1.5,
        )
        self._send_btn = uia.find_descendant(
            self.root.control,
            control_type="ButtonControl",
            name="发送",
            class_name="mmui::XOutlineButton",
            timeout=1.0,
        )
        self._message_list = uia.find_descendant(
            self.root.control,
            control_type="ListControl",
            automation_id="chat_message_list",
            timeout=1.0,
        )

    def _cached_control_is_in_root(self, control) -> bool:
        """快速校验缓存控件仍属于当前聊天顶层窗口。"""
        if control is None:
            return False
        try:
            if not control.Exists(0):
                return False
            root_control = getattr(self.root, "control", None)
            root_hwnd = int(
                getattr(self.root, "HWND", 0)
                or getattr(root_control, "NativeWindowHandle", 0)
                or 0
            )
            top = control.GetTopLevelControl()
            control_hwnd = int(getattr(top, "NativeWindowHandle", 0) or 0)
            return not (root_hwnd and control_hwnd) or root_hwnd == control_hwnd
        except Exception:
            return False

    def refresh(self, force: bool = False) -> None:
        """重新定位输入框/发送按钮/消息列表。

        搜索切换聊天后，旧 UIA Element 可能虽存在但已失效，
        因此发送前必须校验。缓存控件仍属于同一顶层窗口时直接复用，
        避免一次发送重复三遍 UIA 全树查询。
        """
        if not force and all(
            self._cached_control_is_in_root(control)
            for control in (self._input, self._send_btn, self._message_list)
        ):
            return
        self.init()

    @property
    def input_control(self):
        if self._input is None or not self._input.Exists(0):
            self.init()
        return self._input

    @property
    def editbox(self):
        """Mabobot 兼容别名：部分辅助函数直接访问 ``ChatBox.editbox``。"""
        return self.input_control

    @property
    def send_button(self):
        if self._send_btn is None or not self._send_btn.Exists(0):
            self._send_btn = uia.find_descendant(
                self.root.control,
                control_type="ButtonControl",
                name="发送",
                class_name="mmui::XOutlineButton",
                timeout=1.0,
            )
        return self._send_btn

    @property
    def message_list(self):
        if getattr(self.root, "control", None) is None:
            return None
        if self._message_list is None or not self._message_list.Exists(0):
            self._message_list = uia.find_descendant(
                self.root.control,
                control_type="ListControl",
                automation_id="chat_message_list",
                timeout=1.0,
            )
        return self._message_list

    @property
    def who(self) -> str:
        getter = getattr(self.root, "get_current_chat_name", None)
        if getter is not None:
            try:
                current = getter() or ""
                if current:
                    return str(current).strip()
            except Exception:
                pass
        # 独立聊天窗口没有 get_current_chat_name()，但其顶层窗口包装器
        # 暴露了 who。旧逻辑直接返回空串，导致所有私聊来信 sender=''。
        try:
            current = getattr(self.root, "who", "") or ""
            if current:
                return str(current).splitlines()[0].strip()
        except Exception:
            pass
        return ""

    def get_info(self) -> dict[str, Any]:
        return {"chat_name": self.who, "exists": self.exists()}

    # ------------------------------------------------------------------
    # 输入框
    # ------------------------------------------------------------------

    def _activate_editbox(self) -> bool:
        inp = self.input_control
        if inp is None or not inp.Exists(0):
            return False
        inp.Click(simulateMove=False, waitTime=0.08)
        return True

    @uilock
    def clear_edit(self) -> WxResponse:
        """清空输入框。"""
        self.refresh()
        if not self._activate_editbox():
            return WxResponse.failure("输入框不存在")
        inp = self.input_control
        inp.SendKeys("{Ctrl}a", waitTime=0.05)
        inp.SendKeys("{BACK}", waitTime=0.08)
        deadline = time.monotonic() + 0.6
        value = self.get_edit_text()
        while value.strip() and time.monotonic() < deadline:
            time.sleep(0.03)
            value = self.get_edit_text()
        if not value.strip():
            return WxResponse.success()
        return WxResponse.failure(f"输入框未能清空: {value!r}")

    def get_edit_text(self) -> str:
        inp = self.input_control
        if inp is None or not inp.Exists(0):
            return ""
        try:
            return str(inp.GetValuePattern().Value or "")
        except Exception:
            # 注意：不能回退到 inp.Name，因为输入框 Name 是当前聊天标题。
            return ""

    def wait_quote_context(self, timeout: float = 3.0) -> WxResponse:
        """等待微信把当前引用草稿渲染成独立 ``ReferView``。

        微信 4.x 不会把引用预览写进输入框的 ValuePattern。以输入框文本
        是否非空判断引用态，会在空正文时必然等满超时；ReferView 才是
        可直接观察的完成信号。
        """
        root_control = getattr(self.root, "control", None)
        if root_control is None:
            return WxResponse.failure("聊天窗口不存在")
        control = uia.find_descendant(
            root_control,
            control_type="CustomControl",
            class_name="mmui::ReferView",
            timeout=max(0.0, float(timeout)),
        )
        if control is None:
            return WxResponse.failure("引用状态未出现")
        try:
            name = str(getattr(control, "Name", "") or "")
        except Exception:
            name = ""
        return WxResponse.success(data={"quote_context": name})

    def _paste_edit_text(
        self,
        text: str,
        *,
        verify: bool = True,
        replace: bool = False,
        verify_contains: bool = False,
    ) -> WxResponse:
        """在已刷新控件上粘贴文本；可用 Ctrl+A 原子替换现有内容。"""
        if not self._activate_editbox():
            return WxResponse.failure("输入框不存在")
        inp = self.input_control
        if replace:
            inp.SendKeys("{Ctrl}a", waitTime=0.05)
        set_text(text)
        time.sleep(0.03)
        inp.SendKeys("{Ctrl}v", waitTime=0.12)
        deadline = time.monotonic() + 0.8
        actual = self.get_edit_text()
        ratio = text_similarity(actual, text)
        matched = text in actual if verify_contains else ratio >= WxParam.SEND_CONTENT_RATIO
        while verify and not matched and time.monotonic() < deadline:
            time.sleep(0.03)
            actual = self.get_edit_text()
            ratio = text_similarity(actual, text)
            matched = text in actual if verify_contains else ratio >= WxParam.SEND_CONTENT_RATIO
        if verify:
            wxlog.debug(
                "输入框校验: "
                f"actual={actual!r} ratio={ratio:.3f} contains={text in actual}"
            )
            if not matched:
                return WxResponse.failure(
                    f"输入框内容校验失败: ratio={ratio:.3f}, actual={actual!r}"
                )
        return WxResponse.success()

    @uilock
    def set_edit_text(self, text: str, verify: bool = True) -> WxResponse:
        """把文本可靠地写入输入框，并校验相似度。"""
        self.refresh()
        return self._paste_edit_text(text, verify=verify)

    @uilock
    def append_edit_text(self, text: str, verify: bool = True) -> WxResponse:
        """在当前输入框/富文本上下文末尾追加文本。"""
        if not text:
            return WxResponse.success()
        self.refresh()
        # 不能先读取 current 再把 current + text 整段粘贴回去：粘贴动作
        # 本身就是追加，那样会重复已有草稿。引用预览和 @ 对象还可能是
        # ValuePattern 不完整表达的富文本，只验证新增正文确实出现即可。
        return self._paste_edit_text(
            text,
            verify=verify,
            verify_contains=True,
        )

    @uilock
    def append_quote_text(self, text: str, verify: bool = True) -> WxResponse:
        """在已经建立并聚焦的引用上下文中快速追加正文。

        ``select_option('引用')`` 会把焦点直接交给输入框。沿用这一
        交互，复用缓存控件并直接粘贴，避免再次刷新三类控件、点击输入框
        和等待固定的键盘间隔；SendKeys 会自行恢复输入框焦点。
        """
        if not text:
            return WxResponse.success()
        inp = self._input
        if inp is None:
            return self.append_edit_text(text, verify=verify)
        try:
            # SendKeys 自身会 SetFocus；quote() 外层持有同一 UI 锁，而且刚
            # 用 ReferView 校验过当前聊天，因此无需再做 Exists/焦点查询。
            set_text(text)
            inp.SendKeys("{Ctrl}v", waitTime=0.02)
        except Exception as exc:
            return WxResponse.error(f"引用正文写入失败: {exc}")
        if not verify:
            return WxResponse.success()

        deadline = time.monotonic() + 0.5
        actual = self.get_edit_text()
        while text not in actual and time.monotonic() < deadline:
            time.sleep(0.02)
            actual = self.get_edit_text()
        if text not in actual:
            return WxResponse.failure(f"引用正文写入校验失败: actual={actual!r}")
        wxlog.debug(f"引用正文校验: actual={actual!r}")
        return WxResponse.success()

    @uilock
    def send_quote_input(
        self,
        expected_text: str,
        completion_timeout: float = 0.5,
    ) -> WxResponse:
        """快速提交已聚焦的引用正文，并用输入框变化做短确认。"""
        expected_text = str(expected_text or "").strip()
        if not expected_text:
            return WxResponse.failure("引用回复内容不能为空")

        inp = self._input
        if inp is None:
            return self.send_current_input()

        button = self._send_btn
        if button is None:
            button = self.send_button
        try:
            if button is None or not button.IsEnabled:
                return WxResponse.failure("发送按钮未启用")
        except Exception:
            return WxResponse.failure("发送按钮状态不可用")

        self._mark_sent_text(expected_text)
        try:
            # 引用菜单和粘贴均已把输入框置于焦点；直接按回车，避免普通
            # 路径再次 refresh + Click。SendKeys 自身仍会 SetFocus 兜底。
            inp.SendKeys("{Enter}", waitTime=0.02)
        except Exception as exc:
            return WxResponse.error(f"引用回复回车发送失败: {exc}")

        deadline = time.monotonic() + max(0.0, float(completion_timeout))
        while True:
            current = self.get_edit_text()
            if expected_text not in current:
                return WxResponse.success()
            try:
                if button is not None and button.Exists(0) and not button.IsEnabled:
                    return WxResponse.success()
            except Exception:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # 与现有 send_current_input 一致：键盘投递成功后，UI 状态
                # 超时只是不再阻塞调用方，不把可能已发送的消息判成失败。
                return WxResponse.success()
            time.sleep(min(0.02, remaining))

    @uilock
    def send_current_input(self) -> WxResponse:
        """发送输入框当前内容（不清理、不覆盖）。"""
        self.refresh()
        sent_text = self.get_edit_text().strip()
        if not sent_text:
            return WxResponse.failure("输入框为空")
        if not self._wait_send_button_enabled():
            return WxResponse.failure("发送按钮未启用")
        self._mark_sent_text(sent_text)
        response = self._click_send()
        if not response.is_success:
            return response
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            btn = self.send_button
            if btn is not None and btn.Exists(0) and not btn.IsEnabled:
                break
            if not self.get_edit_text().strip():
                break
            time.sleep(0.2)
        return WxResponse.success()

    def _mark_sent_text(self, text: str) -> None:
        if text:
            self._sent_texts[text] = time.time()

    def _mark_sent_files(self, paths) -> None:
        for path in paths:
            self._sent_filenames[Path(path).name] = time.time()

    def _prune_sent_marks(self) -> None:
        now = time.time()
        self._sent_texts = {k: v for k, v in self._sent_texts.items() if now - v < 300}
        self._sent_filenames = {k: v for k, v in self._sent_filenames.items() if now - v < 300}

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    def _wait_send_button_enabled(self, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            btn = self.send_button
            if btn is not None and btn.Exists(0):
                try:
                    if btn.IsEnabled:
                        return True
                except Exception:
                    pass
            time.sleep(0.2)
        return False

    def _click_send(self) -> WxResponse:
        """按回车直接发送；输入框不可用时才回退点击发送按钮。"""
        inp = self.input_control
        if inp is not None and inp.Exists(0):
            try:
                inp.Click(simulateMove=False, waitTime=0.05)
                inp.SendKeys("{Enter}", waitTime=0.15)
                wxlog.debug("已按回车发送")
                return WxResponse.success()
            except Exception as exc:
                wxlog.warning(f"回车发送失败，准备点击发送按钮: {exc}")
        btn = self.send_button
        if btn is None or not btn.Exists(0):
            wxlog.warning("未找到发送按钮")
            return WxResponse.failure("未找到发送按钮")
        if not btn.IsEnabled:
            wxlog.warning("发送按钮不可用")
            return WxResponse.failure("发送按钮不可用")
        wxlog.log_control("点击发送按钮", btn, level="debug")
        btn.Click(simulateMove=False, waitTime=0.8)
        wxlog.debug("发送按钮已点击")
        return WxResponse.success()

    @uilock
    def send_text(
        self,
        msg: str,
        clear: bool = True,
        at: str | list[str] | None = None,
    ) -> WxResponse:
        """发送文本消息。"""
        self.refresh()
        if not msg:
            return WxResponse.failure("消息内容不能为空")
        if not self.exists():
            return WxResponse.failure("聊天窗口不存在")

        if at:
            response = self.clear_edit()
            if not response.is_success:
                return WxResponse.failure(f"无法清除输入框内容，发送失败: {response['message']}")
            response = self.input_at(at, exact=True, verify=True)
            if not response.is_success:
                self.clear_edit()
                return response
            members = list(response["data"].get("mentioned_users") or [])
            response = self._paste_edit_text(f" {msg}", verify=False)
            if response.is_success:
                draft = self.get_edit_text()
                object_count = draft.count(MENTION_OBJECT_CHARACTER)
                if object_count != len(members):
                    response = WxResponse.failure(
                        "@对象在发送前丢失: "
                        f"expected={len(members)} actual={object_count}"
                    )
                elif msg not in draft:
                    response = WxResponse.failure("发送前未能验证消息正文")
        else:
            # 普通文本可在一次焦点事务中 Ctrl+A + 粘贴完成替换。旧流程
            # 先 clear_edit 再 set_edit_text，会重复刷新控件和激活输入框，
            # 每段回复额外消耗约一秒。粘贴后的相似度校验仍阻止错发。
            response = self._paste_edit_text(msg, verify=True, replace=True)
        if not response.is_success:
            return response

        if not self._wait_send_button_enabled():
            return WxResponse.failure("发送按钮未启用")
        self._mark_sent_text(msg)
        response = self._click_send()
        if not response.is_success:
            return response

        # 微信发送成功后输入框通常自动清空。
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            btn = self.send_button
            if btn is not None and btn.Exists(0) and not btn.IsEnabled:
                break
            if not self.get_edit_text().strip():
                break
            time.sleep(0.2)

        if not clear:
            self.set_edit_text(msg, verify=False)
        return WxResponse.success()

    # ------------------------------------------------------------------
    # @ 成员
    # ------------------------------------------------------------------

    @uilock
    def input_at(
        self,
        members: str | list[str],
        *,
        exact: bool = True,
        verify: bool = True,
    ) -> WxResponse:
        """在输入框中 @ 一个或多个群成员，并验证富文本对象。"""
        if isinstance(members, str):
            members = [members]
        normalized = []
        for member in members:
            value = str(member or "").strip().lstrip("@").strip()
            if value and value not in normalized:
                normalized.append(value)
        if not normalized:
            return WxResponse.failure("@成员不能为空")
        for expected_count, member in enumerate(normalized, start=1):
            if not self._activate_editbox():
                return WxResponse.failure("输入框不存在")
            self.input_control.SendKeys("@", waitTime=0.1)
            matches = self._find_mention_items(member, timeout=3.0, exact=exact)
            if not matches:
                self.input_control.SendKeys("{Esc}", waitTime=0.2)
                return WxResponse.failure(f"未找到@成员：{member}")
            if exact and len(matches) != 1:
                self.input_control.SendKeys("{Esc}", waitTime=0.2)
                return WxResponse.failure(f"@成员精确匹配不唯一：{member}")
            matches[0].Click(simulateMove=False, waitTime=0.3)
            time.sleep(0.15)
            if verify:
                object_count = self.get_edit_text().count(MENTION_OBJECT_CHARACTER)
                if object_count != expected_count:
                    return WxResponse.failure(
                        f"@成员富文本校验失败：{member} "
                        f"expected={expected_count} actual={object_count}"
                    )
        return WxResponse.success(
            data={
                "mentioned_users": normalized,
                "verified_mention_objects": len(normalized) if verify else None,
            }
        )

    def _find_mention_items(
        self,
        member: str,
        timeout: float = 3.0,
        *,
        exact: bool = True,
    ) -> list:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches = []
            for control in uia.find_controls(
                self.root.control,
                class_name="mmui::ChatMentionList",
                max_results=3,
            ):
                for candidate in uia.iter_descendants(control, max_nodes=80):
                    try:
                        name = str(candidate.Name or "")
                        first = name.splitlines()[0].strip() if name else ""
                        matched = first == member if exact else member in first
                        if matched:
                            matches.append(candidate)
                    except Exception:
                        continue
            if matches:
                return matches
            time.sleep(0.25)
        return []

    def _find_mention_item(self, member: str, timeout: float = 3.0):
        """Backward-compatible single-item accessor."""
        matches = self._find_mention_items(member, timeout=timeout, exact=True)
        return matches[0] if len(matches) == 1 else None

    # ------------------------------------------------------------------
    # 文件
    # ------------------------------------------------------------------

    @uilock
    def send_files(self, filepaths) -> WxResponse:
        """一次性粘贴多个文件到输入框，只点击一次发送。"""
        self.refresh()
        paths: list[Path] = []
        for filepath in filepaths:
            path = Path(filepath)
            if not path.is_file():
                return WxResponse.failure(f"文件路径不存在: {filepath}")
            paths.append(path)
        if not paths:
            return WxResponse.failure("文件列表不能为空")
        if not self.exists():
            return WxResponse.failure("聊天窗口不存在")

        response = self.clear_edit()
        if not response.is_success:
            return WxResponse.failure(f"无法清除输入框内容: {response['message']}")

        try:
            set_files([str(path) for path in paths])
        except Exception as exc:
            return WxResponse.error(f"设置文件剪贴板失败: {exc}")

        if not self._activate_editbox():
            return WxResponse.failure("输入框不存在")
        self.input_control.SendKeys("{Ctrl}v", waitTime=0.8)
        time.sleep(1.0)

        if not self._wait_send_button_enabled():
            return WxResponse.failure("文件粘贴后发送按钮未启用")
        self._mark_sent_files(paths)
        response = self._click_send()
        if not response.is_success:
            return response
        # 等微信把附件从输入框拿走，避免连续操作时互相干扰。
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            btn = self.send_button
            if btn is not None and btn.Exists(0) and not btn.IsEnabled:
                break
            if not self.get_edit_text().strip():
                break
            time.sleep(0.2)
        wxlog.info(f"批量发送文件完成: {[p.name for p in paths]}")
        return WxResponse.success()

    @uilock
    def send_file(self, filepath: str | Path) -> WxResponse:
        """通过剪贴板 CF_HDROP 粘贴并发送单个文件。"""
        self.refresh()
        path = Path(filepath)
        if not path.is_file():
            return WxResponse.failure(f"文件路径不存在: {filepath}")
        if not self.exists():
            return WxResponse.failure("聊天窗口不存在")

        response = self.clear_edit()
        if not response.is_success:
            return WxResponse.failure(f"无法清除输入框内容: {response['message']}")

        try:
            set_files([str(path)])
        except Exception as exc:
            return WxResponse.error(f"设置文件剪贴板失败: {exc}")

        if not self._activate_editbox():
            return WxResponse.failure("输入框不存在")
        self.input_control.SendKeys("{Ctrl}v", waitTime=0.8)
        time.sleep(1.0)

        if not self._wait_send_button_enabled():
            return self._send_file_via_dialog(path)
        response = self._click_send()
        if response.is_success:
            return response
        return self._send_file_via_dialog(path)

    def set_group_my_nickname(self, value) -> None:
        """打开群资料并设置“我在本群的昵称”（尚未开放写入）。"""
        wnd = ChatMoreInfoWnd(self.root)
        return wnd.set_my_nickname(value)

    @uilock
    def get_group_my_nickname(self) -> str:
        """从群资料面板只读返回当前账号的群昵称。"""
        wnd = ChatMoreInfoWnd(self.root)
        try:
            return wnd.get_my_nickname()
        finally:
            wnd.close()

    def run_ordered_media_operation(self, message, operation):
        """Run one delayed media action in listener delivery order.

        Ordering is resolved before acquiring the global UI transaction.  This
        avoids a later request holding the UI lock while an earlier request is
        still waiting to enter the per-chat queue.
        """
        sequence = int(getattr(message, "delivery_sequence", 0) or 0)

        def _run():
            with ui_transaction(timeout=120.0):
                return operation()

        return self._media_operation_sequencer.run(self.who, sequence, _run)

    def _send_file_via_dialog(self, path: Path) -> WxResponse:
        """后备方案：点击“发送文件”按钮，通过 Windows 文件对话框选择。"""
        file_button = uia.find_descendant(
            self.root.control,
            control_type="ButtonControl",
            name="发送文件",
            timeout=1.0,
        )
        if file_button is None:
            return WxResponse.failure("未找到发送文件按钮")
        file_button.Click(simulateMove=False, waitTime=0.8)
        deadline = time.monotonic() + 5.0
        dialog = None
        while time.monotonic() < deadline:
            dialog = uia.find_top_level_control("#32770", timeout=0.5)
            if dialog is not None:
                break
            time.sleep(0.2)
        if dialog is None:
            return WxResponse.failure("文件对话框未出现")
        dialog.SendKeys(str(path), interval=0.02, waitTime=0.5)
        dialog.SendKeys("{Enter}", waitTime=1.0)
        return WxResponse.success()

    # ------------------------------------------------------------------
    # 消息读取
    # ------------------------------------------------------------------

    def is_group_chat(self) -> bool:
        """通过标题栏 (N) 判断当前是否为群聊。"""
        for control in uia.find_controls(
            self.root.control,
            control_type="TextControl",
            class_name="mmui::XTextView",
            max_results=20,
        ):
            try:
                automation_id = control.AutomationId or ""
                if automation_id.endswith("current_chat_count_label") and control.Name:
                    return True
            except Exception:
                continue
        return False

    def _direction_for(self, control) -> str | None:
        """优先用窗口 DC 截图判断方向，结果按消息键缓存。"""
        class_name = str(getattr(control, "ClassName", "") or "")
        if class_name in ("mmui::ChatItemView", "mmui::ChatSystemInfoItemView", "mmui::ChatAppReaderItemView"):
            return None
        try:
            runtime_id = "-".join(str(part) for part in control.GetRuntimeId())
            name = str(getattr(control, "Name", "") or "")[:80]
            key = f"{runtime_id}|{class_name}|{name}"
            cached = self._direction_cache.get(key)
            if cached is not None:
                return cached
            hwnd = getattr(self.root, "HWND", None)
            direction = detect_message_direction(control, hwnd=int(hwnd) if hwnd else None)
            if direction is not None:
                self._direction_cache[key] = direction
            return direction
        except Exception:
            return None

    def _direction_for_message(self, msg_type: str, raw_name: str, content: str) -> str | None:
        """确定性方向判断：不依赖截图。

        规则：
        - 系统/时间/公众号消息返回 None，由类型层标记为 system；
        - mabowx 自己发送过的文本/文件名 -> self；
        - 文件进度消息 -> self；
        - 其余真人消息 -> friend。

        局限：用户在微信界面手动发送、且未经过 mabowx 的自发消息会按
        friend 处理；bot 正常收发流程不受影响。
        """
        self._prune_sent_marks()
        if msg_type in ("system", "official", "time"):
            return None
        if msg_type == "file" and content.startswith("进度"):
            return "self"
        if msg_type in ("text", "quote", "other") and raw_name in self._sent_texts:
            return "self"
        if content in self._sent_texts:
            return "self"
        if msg_type == "file" and content in self._sent_filenames:
            return "self"
        return "friend"

    def _sender_for(self, direction: str | None, msg_type: str) -> str:
        if direction == "self":
            return getattr(self.root, "nickname", None) or "我"
        if direction == "friend":
            if self.is_group_chat():
                # 群成员名需要在消息差分完成后，通过隐藏头像焦点控件补齐。
                # 此处保持空值，避免 sender 的延迟变化干扰尾锚点/重复检测。
                return ""
            return self.who
        if msg_type == "system":
            return "系统"
        if msg_type == "official":
            return "公众号"
        return ""

    def _extract_group_sender(self, message) -> str:
        """对单条群消息执行一次头像焦点探测。"""
        control = getattr(message, "control", None)
        direction = str(getattr(message, "direction", "") or "")
        try:
            if not control.Exists(0):
                return ""
        except Exception:
            return ""

        root_hwnd = int(getattr(self.root, "HWND", 0) or 0)
        if not root_hwnd:
            return ""
        try:
            message_top = control.GetTopLevelControl()
            message_hwnd = int(
                getattr(message_top, "NativeWindowHandle", 0) or 0
            )
            if message_hwnd and message_hwnd != root_hwnd:
                return ""
        except Exception:
            return ""

        last_point = None
        try:
            # 原版会先探测左头像、失败后再探测右头像。mabowx 已经有独立
            # 的方向判断，因此每条来信只探测左侧。后台 WM_RBUTTON 精确
            # 投递给所属 HWND，多个聊天窗口完全重叠时也不会误点前台窗口。
            for _attempt in range(2):
                point = group_sender_head_point(control, direction)
                if point is None:
                    break
                last_point = point
                control.SetFocus()
                if not post_right_click(root_hwnd, point[0], point[1]):
                    break
                deadline = time.monotonic() + GROUP_SENDER_FOCUS_TIMEOUT_SEC
                while True:
                    focused = uia.get_focused_control()
                    sender = group_sender_from_focused_control(
                        control,
                        focused,
                        direction,
                    )
                    if sender:
                        return sender
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
                # 新消息布局仍在调整时，用实时 BoundingRectangle 再投递一次。
                time.sleep(0.04)
        except Exception as exc:
            wxlog.debug(f"群聊发送者头像焦点探测失败: {exc}")
        finally:
            # 右击头像成功取到隐藏焦点后，微信仍会异步弹出“@ / 拍一拍”
            # 联系人菜单。此前成功分支直接 return，菜单残留后会污染紧随
            # 其后的文件右键下载。成功或失败都必须收尾；Menu.close 只会
            # 定向关闭已验证的独立 XMenu HWND，绝不向聊天窗口发送 Esc。
            if last_point is not None:
                try:
                    from mabowx.ui.component import Menu

                    Menu(self, timeout=0.15, anchor=last_point).close()
                except Exception:
                    pass
        return ""

    @uilock
    def _resolve_group_senders(self, messages: list) -> list:
        """只为即将交付的群聊来信补齐 sender，不污染消息差分基线。"""
        if not messages or not self.is_group_chat():
            return messages
        candidates = [
            message
            for message in messages
            if str(getattr(message, "direction", "") or "") == "friend"
            and not str(getattr(message, "sender", "") or "").strip()
            and str(getattr(message, "type", "") or "")
            not in {"system", "time", "official"}
        ]
        if not candidates:
            return messages

        resolved = 0
        for message in candidates:
            sender = self._extract_group_sender(message)
            if sender:
                message.sender = sender
                resolved += 1
            else:
                wxlog.debug(
                    "群聊发送者未识别: "
                    f"chat={self.who!r} type={getattr(message, 'type', '')!r} "
                    f"content={str(getattr(message, 'content', '') or '')[:80]!r}"
                )
        if resolved:
            wxlog.debug(
                f"群聊发送者已识别: chat={self.who!r} "
                f"resolved={resolved}/{len(candidates)}"
            )
        return messages

    def _sync_chat_cache(self) -> None:
        current = self.who
        # 独立窗口重绘/切前台的极短时刻，标题控件可能读成空字符串。
        # 这不是切换聊天，不能清空消息基线，否则下一轮会重放整页消息。
        if not current and self._cache_chat_name:
            return
        if current != self._cache_chat_name:
            self._cache_chat_name = current
            self._last_time = ""
            self._used_msg_ids.clear()
            self._last_signatures.clear()
            self._recent_signatures.clear()
            self._tail_message_id = ""
            self._tail_message_signature = ""
            self._visible_message_snapshot = ()
            self._anchor_miss_snapshot = None
            self._anchor_miss_rounds = 0
            self._direction_cache.clear()

    @uilock
    def get_messages(self, resolve_group_senders: bool = True) -> list:
        """读取当前可见消息并解析为消息对象。"""
        self._sync_chat_cache()
        result: list = []
        for control in self.get_visible_messages():
            try:
                raw_name = str(getattr(control, "Name", "") or "")
                msg_cls = classify(control)
                msg_type = getattr(msg_cls, "type", "other")
                content = parse_content(msg_type, raw_name)
                direction = self._direction_for(control)
                if direction is None:
                    direction = self._direction_for_message(msg_type, raw_name, content)
                msg = make_message(control, self, direction, self._last_time)
                if getattr(msg, "is_time", False):
                    self._last_time = msg.content
                    msg.sender = "系统"
                else:
                    msg.sender = self._sender_for(direction, msg.type)
                result.append(msg)
            except Exception as exc:
                try:
                    raw_cls = getattr(control, "ClassName", "")
                    raw_name = getattr(control, "Name", "")
                except Exception:
                    raw_cls, raw_name = "", ""
                wxlog.warning(
                    f"解析消息失败: class={raw_cls!r} name={raw_name[:120]!r} error={exc}"
                )
                continue
        if resolve_group_senders:
            self._resolve_group_senders(result)
        return result

    def _consume_messages(self, messages: list) -> list:
        """更新缓存并返回本轮真正位于旧尾锚点之后的消息。"""
        had_anchor = bool(self._tail_message_id or self._tail_message_signature)
        current_snapshot = tuple(
            (
                str(getattr(message, "id", "") or ""),
                message_anchor_signature(message),
            )
            for message in messages
        )
        after_anchor, anchor_found = messages_after_anchor(
            messages,
            self._tail_message_id,
            self._tail_message_signature,
        )
        recovered_from_overlap = False
        if had_anchor and not anchor_found:
            after_overlap, overlap_found = messages_after_previous_overlap(
                messages,
                getattr(self, "_visible_message_snapshot", ()),
            )
            if overlap_found:
                after_anchor = after_overlap
                anchor_found = True
                recovered_from_overlap = True
        new_messages, next_ids, next_signatures = diff_new_messages(
            messages,
            self._used_msg_ids,
            self._last_signatures,
        )
        self._used_msg_ids = next_ids
        self._last_signatures = next_signatures

        if had_anchor:
            # 锚点存在时，消息顺序比 RuntimeId 差分更可靠；找不到锚点
            # 则说明当前多半是虚拟化旧页，整轮不派发。
            new_messages = after_anchor if anchor_found else []

        if messages and (not had_anchor or anchor_found):
            tail = messages[-1]
            self._tail_message_id = str(getattr(tail, "id", "") or "")
            self._tail_message_signature = message_anchor_signature(tail)
            self._visible_message_snapshot = current_snapshot
            self._anchor_miss_snapshot = None
            self._anchor_miss_rounds = 0
        elif messages and had_anchor and not anchor_found:
            snapshot = tuple(
                (
                    str(getattr(message, "id", "") or ""),
                    message_anchor_signature(message),
                )
                for message in messages
            )
            if snapshot == self._anchor_miss_snapshot:
                self._anchor_miss_rounds += 1
            else:
                self._anchor_miss_snapshot = snapshot
                self._anchor_miss_rounds = 1
            # 如果锚点长期不再出现（窗口真正重建或一次涌入大量消息），
            # 在稳定三轮后静默重建基线，避免监听永久卡死。
            if self._anchor_miss_rounds >= 3:
                tail = messages[-1]
                self._tail_message_id = str(getattr(tail, "id", "") or "")
                self._tail_message_signature = message_anchor_signature(tail)
                self._visible_message_snapshot = current_snapshot
                self._anchor_miss_snapshot = None
                self._anchor_miss_rounds = 0
                wxlog.debug("消息尾锚点连续三轮缺失，已按稳定可见列表静默重建")

        if recovered_from_overlap and new_messages:
            wxlog.debug(
                f"消息尾锚点变形，已通过前序可见重叠恢复候选消息: count={len(new_messages)}"
            )

        now = time.monotonic()
        old_recent = dict(self._recent_signatures)
        for message in messages:
            self._recent_signatures[message_signature(message)] = now
        # 已找到尾锚点时，“锚点之后”本身就是确定的新消息，不能再按
        # 内容签名过滤，否则同一分钟连续发送相同文本会被误吞。只有尚未
        # 建立可靠锚点的兼容路径才使用短 TTL 签名过滤。
        if not (had_anchor and anchor_found):
            new_messages = filter_recent_signatures(new_messages, old_recent, now)
        if len(self._recent_signatures) > 600:
            cutoff = now - MESSAGE_SIGNATURE_TTL_SEC
            self._recent_signatures = {
                sig: ts for sig, ts in self._recent_signatures.items() if ts >= cutoff
            }
        return new_messages

    @uilock
    def get_new_messages(self) -> list:
        """返回自上次读取后出现的新消息。

        微信 4.x 消息列表是虚拟化列表，RuntimeId 会复用。因此综合使用
        RuntimeId、内容签名和上一轮尾消息锚点，避免旧页被重复回调。
        """
        visible_messages = self.get_messages(resolve_group_senders=False)
        messages = self._consume_messages(visible_messages)

        # RuntimeId belongs to a recyclable UI row, so it must never become the
        # durable identity used by an asynchronous plugin.  Attach a UUID to
        # every delivered occurrence.  Direct images additionally capture their
        # visual/neighbor identity before sender probing opens any context menu.
        attach_delivery_context(visible_messages, messages)
        for message in messages:
            self._delivery_sequence += 1
            message.delivery_sequence = self._delivery_sequence
        messages = self._resolve_group_senders(messages)
        for message in messages:
            target_error = str(getattr(message, "media_target_error", "") or "")
            if target_error:
                wxlog.warning(
                    "图片消息身份快照未建立，后续下载将失败关闭: "
                    f"chat={self.who!r} delivery_id="
                    f"{getattr(message, 'delivery_id', '')} error={target_error}"
                )
        return messages

    @uilock
    def prime_message_cache(
        self,
        settle_time: float = 0.0,
        interval: float = 0.15,
        stable_rounds: int = 1,
    ) -> None:
        """初始化消息缓存，等可见列表稳定后再开始监听。

        ``settle_time`` 是最长等待时间；默认参数仍只采样一次，以保持
        普通 ``Chat`` 构造的性能。监听窗口创建/重绑时会要求多轮稳定。
        """
        deadline = time.monotonic() + max(0.0, float(settle_time))
        required = max(1, int(stable_rounds))
        previous: tuple[tuple[str, str], ...] | None = None
        stable = 0
        while True:
            messages = self.get_messages(resolve_group_senders=False)
            self._consume_messages(messages)
            snapshot = tuple(
                (
                    str(getattr(message, "id", "") or ""),
                    message_anchor_signature(message),
                )
                for message in messages
            )
            if snapshot == previous:
                stable += 1
            else:
                previous = snapshot
                stable = 1
            if stable >= required or time.monotonic() >= deadline:
                return
            time.sleep(max(0.01, min(float(interval), deadline - time.monotonic())))

    @uilock
    def get_history_messages(
        self,
        n: int,
        callback=None,
        interval: float = 0.3,
        speed: int = 1,
        goback: bool = True,
        timeout: float | None = None,
    ) -> list:
        """向上滚动读取历史消息，直到数量达到 n 或无新消息。"""
        import time as _time

        self.refresh()
        collected: dict[str, object] = {}
        deadline = None if timeout is None else _time.monotonic() + timeout

        def _collect():
            for msg in self.get_messages():
                if msg.id and msg.id not in collected:
                    collected[msg.id] = msg
                    if callback is not None:
                        try:
                            callback(msg)
                        except Exception:
                            pass

        _collect()
        no_new_rounds = 0
        while len(collected) < n and no_new_rounds < 4:
            if deadline is not None and _time.monotonic() > deadline:
                break
            before = len(collected)
            try:
                if self.message_list is not None and self.message_list.Exists(0):
                    self.message_list.WheelUp(wheelTimes=speed, waitTime=interval)
            except Exception:
                pass
            _time.sleep(max(0.2, interval))
            _collect()
            if len(collected) == before:
                no_new_rounds += 1
            else:
                no_new_rounds = 0

        if goback:
            self._return_to_latest()
        return list(collected.values())

    def _return_to_latest(self) -> None:
        """把消息列表滚回最新位置。

        不点击消息列表内容，避免误触卡片/图片等消息；优先 SetFocus + End。
        """
        try:
            if self.message_list is not None and self.message_list.Exists(0):
                self.message_list.SetFocus()
                self.message_list.SendKeys("{End}", waitTime=0.5)
                return
        except Exception:
            pass
        try:
            if self.message_list is not None and self.message_list.Exists(0):
                self.message_list.WheelDown(wheelTimes=40, waitTime=0.1)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 消息列表（最小校验/旧接口）
    # ------------------------------------------------------------------

    @uilock
    def get_visible_messages(self) -> list[Any]:
        if self.message_list is None or not self.message_list.Exists(0):
            return []
        try:
            return list(self.message_list.GetChildren())
        except Exception:
            return []

    def last_message_names(self, limit: int = 5) -> list[str]:
        names = []
        for item in reversed(self.get_visible_messages()):
            try:
                name = str(item.Name or "")
                if name:
                    names.append(name)
            except Exception:
                continue
            if len(names) >= limit:
                break
        return names

    def has_text_in_messages(self, text: str) -> bool:
        for item in self.get_visible_messages():
            try:
                if text in str(item.Name or ""):
                    return True
            except Exception:
                continue
        return False
