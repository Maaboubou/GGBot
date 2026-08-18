"""Pure helpers for reliable, observable WeChat text sends."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional


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
