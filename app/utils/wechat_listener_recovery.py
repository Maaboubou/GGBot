"""Small, testable helpers for rebuilding wxautox listener windows."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any


def session_name(session: Any) -> str:
    """Return the exact display name exposed by a wxautox session item."""
    try:
        return str(getattr(session, "name", "") or "").strip()
    except Exception:
        return ""


def find_exact_session(sessions: Iterable[Any], who: str) -> Any | None:
    """Find an exact session match; partial matches are unsafe for listeners."""
    target = str(who or "").strip()
    if not target:
        return None
    for session in sessions or []:
        if session_name(session) == target:
            return session
    return None


def open_listener_from_existing_session(
    wechat: Any,
    who: str,
    *,
    get_listener_chat: Callable[[str], Any | None],
    verify_attempts: int = 8,
    verify_delay: float = 0.25,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[Any | None, str]:
    """Open a listener window from the exact current-session context menu.

    wxautox ``AddListenChat`` first searches through the main-window search
    edit and then uses the client-version-specific detach action.  Recent
    WeChat 4.1 builds label the action ``独立窗口显示``.  Opening it from a
    fresh exact SessionElement bypasses both stale search controls and old menu
    wording, after which callers can invoke native ``AddListenChat`` again to
    register the callback.
    """
    sessions = wechat.GetSession() or []
    session = find_exact_session(sessions, who)
    if session is None:
        return None, "session_not_found"

    select_option = getattr(session, "select_option", None)
    if not callable(select_option):
        return None, "menu_option_unavailable"

    selected_option = ""
    for option in ("独立窗口显示", "在独立窗口打开", "在独立窗口中打开"):
        result = select_option(option)
        if result:
            selected_option = option
            break
    if not selected_option:
        return None, "detach_menu_option_not_found"

    for _ in range(max(1, int(verify_attempts))):
        chat = get_listener_chat(who)
        if chat is not None:
            return chat, f"session_menu:{selected_option}"
        sleeper(max(0.0, float(verify_delay)))

    return None, "window_not_created"
