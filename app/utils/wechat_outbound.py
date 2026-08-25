"""Pure helpers for reliable, observable WeChat text sends."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence


MENTION_OBJECT_CHARACTER = "\ufffc"


def _response_payload(response: Any) -> Any:
    """Convert wxautox responses to a log/JSON friendly value."""
    if response is None:
        return None

    payload = response
    to_dict = getattr(response, "to_dict", None)
    if callable(to_dict):
        try:
            payload = to_dict()
        except Exception:
            pass
    elif isinstance(response, dict):
        payload = dict(response)

    try:
        json.dumps(payload, ensure_ascii=False)
        return payload
    except (TypeError, ValueError):
        return str(payload)


@dataclass(frozen=True)
class TextSendAttempt:
    route: str
    success: bool
    elapsed_seconds: float
    response: Any = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "success": self.success,
            "elapsed_ms": round(self.elapsed_seconds * 1000, 1),
            "response": self.response,
            "error": self.error or None,
        }


@dataclass(frozen=True)
class TextSendOutcome:
    success: bool
    attempts: tuple[TextSendAttempt, ...]
    elapsed_seconds: float

    @property
    def last_attempt(self) -> Optional[TextSendAttempt]:
        return self.attempts[-1] if self.attempts else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "elapsed_ms": round(self.elapsed_seconds * 1000, 1),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


def send_text_with_retry(
    wx: Any,
    who: str,
    message: str,
    *,
    get_listener_chat: Callable[[str], Any],
    activate_main_window: Callable[[str], Any],
    logger: Any = None,
    clock: Callable[[], float] = time.monotonic,
) -> TextSendOutcome:
    """Send text once and retry once only after an explicit false result.

    A detached listener chat is the preferred route because it already points
    at the intended conversation and avoids a main-window search. Exceptions
    are deliberately not retried: wxautox may have partially acted before an
    exception, so only an explicit false ``WxResponse`` authorizes retry.
    """
    started_at = clock()
    attempts: list[TextSendAttempt] = []

    def run_attempt(route: str, operation: Callable[[], Any]) -> tuple[bool, bool]:
        attempt_started_at = clock()
        try:
            response = operation()
        except Exception as exc:
            attempts.append(
                TextSendAttempt(
                    route=route,
                    success=False,
                    elapsed_seconds=clock() - attempt_started_at,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            if logger:
                logger.error("WeChat text send attempt raised: route=%s error=%s", route, exc)
            return False, True

        success = bool(response)
        attempts.append(
            TextSendAttempt(
                route=route,
                success=success,
                elapsed_seconds=clock() - attempt_started_at,
                response=_response_payload(response),
            )
        )
        return success, False

    listener_chat = None
    try:
        listener_chat = get_listener_chat(who)
    except Exception as exc:
        if logger:
            logger.warning("Failed to resolve listener chat for %s; using main window: %s", who, exc)

    if listener_chat is not None:
        first_route = "listener_chat"
        send_operation = lambda: listener_chat.SendMsg(message)
    else:
        first_route = "main_window"
        send_operation = lambda: wx.SendMsg(
            message,
            who,
            exact=True,
            max_retries=1,
        )

    success, raised = run_attempt(first_route, send_operation)
    if success or raised:
        return TextSendOutcome(success, tuple(attempts), clock() - started_at)

    # The first operation returned an explicit failure, so one controlled
    # retry is permitted. Keep using a verified listener child window when it
    # exists; otherwise re-activate the main window before searching again.
    if listener_chat is not None:
        try:
            show = getattr(listener_chat, "Show", None)
            if callable(show):
                show()
        except Exception as exc:
            if logger:
                logger.warning("Failed to activate listener chat before retry: who=%s error=%s", who, exc)
        retry_route = "listener_chat_retry"
    else:
        try:
            activate_main_window(f"text_send_retry:{who}")
        except Exception as exc:
            if logger:
                logger.warning("Failed to activate main window before retry: who=%s error=%s", who, exc)
        retry_route = "main_window_retry"

    success, _raised = run_attempt(retry_route, send_operation)
    return TextSendOutcome(success, tuple(attempts), clock() - started_at)


def _control_name(control: Any) -> str:
    try:
        return str(getattr(control, "Name", "") or "")
    except Exception:
        return ""


def _control_type(control: Any) -> str:
    try:
        return str(getattr(control, "ControlTypeName", "") or "")
    except Exception:
        return ""


def _walk_controls(root: Any, *, max_depth: int = 9):
    """Yield a bounded UIA subtree without depending on wxautox internals."""
    stack = [(root, 0)]
    while stack:
        control, depth = stack.pop()
        yield control
        if depth >= max_depth:
            continue
        try:
            children = list(control.GetChildren() or [])
        except Exception:
            children = []
        stack.extend((child, depth + 1) for child in reversed(children))


def _find_exact_mention_candidates(editbox: Any, user: str) -> list[Any]:
    try:
        top = editbox.GetTopLevelControl()
    except Exception:
        top = None
    if top is None:
        return []
    return [
        control
        for control in _walk_controls(top)
        if _control_type(control) == "ListItemControl" and _control_name(control) == user
    ]


def _read_edit_value(editbox: Any) -> str:
    pattern = editbox.GetValuePattern()
    return str(getattr(pattern, "Value", "") or "")


def _normalize_at_users(at_users: Sequence[str]) -> list[str]:
    users: list[str] = []
    seen: set[str] = set()
    for raw_user in at_users:
        user = str(raw_user).strip().lstrip("@").strip()
        if user and user not in seen:
            seen.add(user)
            users.append(user)
    return users


def send_text_with_exact_mentions(
    wx: Any,
    who: str,
    message: str,
    at_users: Sequence[str],
    *,
    uia_module: Any,
    activate_main_window: Callable[[str], Any],
    logger: Any = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    candidate_timeout_seconds: float = 2.5,
) -> TextSendOutcome:
    """Send a verified real WeChat mention through the main-window UI.

    wxautox's native ``at=`` path can report success while only inserting plain
    text on unsupported WeChat versions. This route selects an exact member in
    WeChat's own suggestion list and verifies the resulting U+FFFC rich-edit
    object before it presses Enter. It intentionally never retries, because a
    retry after UI uncertainty could duplicate a successfully sent message.
    """
    started_at = clock()
    attempt_started_at = clock()
    route = "main_window_exact_mention"
    users = _normalize_at_users(at_users)
    chatbox = None

    try:
        if not users:
            raise ValueError("at_users is empty")

        activate_main_window(f"exact_mention:{who}")
        wx.ChatWith(who, exact=True)

        chat_info = getattr(wx, "ChatInfo", None)
        if callable(chat_info):
            info = chat_info() or {}
            actual_chat = str(info.get("chat_name") or "").strip() if isinstance(info, dict) else ""
            if actual_chat and actual_chat != who:
                raise RuntimeError(
                    f"chat verification failed: expected={who!r} actual={actual_chat!r}"
                )

        chatbox = wx.ChatBox
        activate_editbox = getattr(chatbox, "_activate_editbox", None)
        if callable(activate_editbox):
            activate_editbox()
        chatbox.clear_edit()
        editbox = chatbox.editbox
        editbox.Click()

        for expected_count, user in enumerate(users, start=1):
            uia_module.SendKeys("@", waitTime=0.1)
            deadline = clock() + max(0.0, candidate_timeout_seconds)
            matches: list[Any] = []
            while True:
                matches = _find_exact_mention_candidates(editbox, user)
                if matches or clock() >= deadline:
                    break
                sleep(0.1)

            if not matches:
                raise RuntimeError(f"exact mention candidate not found: {user}")
            if len(matches) > 1:
                raise RuntimeError(f"exact mention candidate is ambiguous: {user}")

            matches[0].Click()
            sleep(0.15)
            object_count = _read_edit_value(editbox).count(MENTION_OBJECT_CHARACTER)
            if object_count != expected_count:
                raise RuntimeError(
                    f"mention object verification failed: user={user!r} "
                    f"expected={expected_count} actual={object_count}"
                )

        body = f" {message}"
        if not uia_module.SetClipboardText(body):
            raise RuntimeError("failed to place message body on clipboard")
        uia_module.SendKeys("{Ctrl}v", waitTime=0.1)
        sleep(0.1)

        draft = _read_edit_value(editbox)
        object_count = draft.count(MENTION_OBJECT_CHARACTER)
        if object_count != len(users):
            raise RuntimeError(
                f"mention object was lost before send: expected={len(users)} actual={object_count}"
            )
        if message not in draft:
            raise RuntimeError("message body verification failed before send")

        uia_module.SendKeys("{Enter}", waitTime=0.2)
        sleep(0.2)
        remaining_draft = _read_edit_value(editbox)
        if remaining_draft:
            raise RuntimeError("message draft was not cleared after Enter")

        attempt = TextSendAttempt(
            route=route,
            success=True,
            elapsed_seconds=clock() - attempt_started_at,
            response={
                "mentioned_users": users,
                "verified_mention_objects": object_count,
            },
        )
        return TextSendOutcome(True, (attempt,), clock() - started_at)
    except Exception as exc:
        if chatbox is not None:
            try:
                chatbox.clear_edit()
            except Exception:
                pass
        if logger:
            logger.error(
                "Verified WeChat mention send failed: chat=%s users=%s error=%s",
                who,
                users,
                exc,
            )
        attempt = TextSendAttempt(
            route=route,
            success=False,
            elapsed_seconds=clock() - attempt_started_at,
            error=f"{type(exc).__name__}: {exc}",
        )
        return TextSendOutcome(False, (attempt,), clock() - started_at)
