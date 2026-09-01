"""发送人相关的消息扩展。"""

from __future__ import annotations

import time

from mabowx.core.locks import uilock
from mabowx.param import WxResponse

from .base import HumanMessage


class FriendMessage(HumanMessage):
    """对方消息。"""

    attr = "friend"

    def _load_sender_info(self) -> dict:
        from mabowx.ui.component import ProfileWnd

        self.click_head()
        time.sleep(0.6)
        profile = ProfileWnd(self.parent, timeout=3.0)
        if profile.control is None:
            return {}
        info = profile.info
        profile.close()
        return info

    @uilock
    def sender_info(self) -> dict:
        """点击头像打开资料卡，返回发送人信息。"""
        if not self.exists():
            raise RuntimeError("消息对象已失效")
        info = self._load_sender_info()
        nickname = info.get("nickname") or info.get("display_name") or ""
        if nickname:
            self.sender = nickname
        return info

    @uilock
    def at(self, content: str = "", quote: bool = False) -> WxResponse:
        """@当前消息发送者，并发送指定内容。"""
        if not self.sender:
            info = self.sender_info()
            self.sender = info.get("nickname") or ""
        if not self.sender:
            return WxResponse.failure("无法确定消息发送者")
        parent = self.parent
        if parent is None:
            return WxResponse.failure("消息父窗口不存在")

        response = parent.clear_edit()
        if not response.is_success:
            return response
        if quote:
            response = self.select_option("引用")
            if not response.is_success:
                return response
        response = parent.input_at(self.sender)
        if not response.is_success:
            return response
        if content:
            response = parent.append_edit_text(content, verify=True)
            if not response.is_success:
                return response
        return parent.send_current_input()


class SelfMessage(HumanMessage):
    """自己发送的消息。"""

    attr = "self"
