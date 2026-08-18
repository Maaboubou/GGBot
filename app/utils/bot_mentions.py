"""群内机器人 @ 别名的统一匹配逻辑。"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional


_MENTION_SUFFIX = r"(?=$|[\s\u2005,.，。!！?？:：;；、])"


def _clean_name(value: Any) -> str:
    return str(value or "").strip().lstrip("@").strip()


def bot_names_for_user(user: Any, global_name: str) -> List[str]:
    """返回当前群可接受的 @ 别名，按展示优先级排序。

    微信群详情的精确读取值优先，但手动值与全局值仍保留为别名，
    让昵称变更期间的旧 @ 也能继续触发。
    """
    names: List[str] = []
    auto_enabled = bool(getattr(user, "bot_group_nickname_auto_enabled", True))
    candidates: Iterable[Any] = (
        getattr(user, "bot_group_nickname_detected", None) if auto_enabled else None,
        getattr(user, "bot_group_nickname", None),
        global_name,
    )
    for value in candidates:
        name = _clean_name(value)
        if name and name not in names:
            names.append(name)
    return names


def find_bot_mention(content: Any, names: Iterable[str]) -> Optional[str]:
    """返回消息中命中的机器人别名，未命中则返回 None。"""
    text = str(content or "")
    if not text:
        return None
    # 先比较长名称，防止“小助手”抢先命中“小助手Pro”。
    normalized = sorted({_clean_name(name) for name in names if _clean_name(name)}, key=len, reverse=True)
    for name in normalized:
        if re.search(rf"@{re.escape(name)}{_MENTION_SUFFIX}", text):
            return name
    return None


def strip_bot_mentions(content: Any, names: Iterable[str]) -> str:
    """移除所有已知机器人 @ 别名及微信插入的分隔空白。"""
    original = str(content or "")
    cleaned = original
    normalized = sorted({_clean_name(name) for name in names if _clean_name(name)}, key=len, reverse=True)
    for name in normalized:
        cleaned = re.sub(
            rf"@{re.escape(name)}{_MENTION_SUFFIX}[\s\u2005]*",
            "",
            cleaned,
        )
    return cleaned.strip() or original.strip()
