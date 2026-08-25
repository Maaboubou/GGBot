"""Pure helpers for classifying native window geometry."""

from __future__ import annotations

from typing import Iterable, Mapping

OFFSCREEN_SENTINEL = -30000


def is_offscreen_sentinel_rect(
    rect: dict,
    *,
    sentinel: int = OFFSCREEN_SENTINEL,
) -> bool:
    """Return whether a window rectangle uses Windows' minimized/off-screen sentinel."""
    return (
        int(rect.get("left", 0)) <= sentinel
        and int(rect.get("top", 0)) <= sentinel
    )


def is_unrecoverable_offscreen_window(
    *,
    window_rect: dict,
    normal_rect: dict,
    iconic: bool,
) -> bool:
    """Identify a non-minimized window whose current and restore positions are invalid."""
    return bool(
        not iconic
        and is_offscreen_sentinel_rect(window_rect)
        and is_offscreen_sentinel_rect(normal_rect)
    )


def is_usable_window_rect(rect: Mapping[str, int] | None) -> bool:
    """Return whether a rectangle is a usable on-screen recovery candidate."""
    try:
        if not rect or is_offscreen_sentinel_rect(dict(rect)):
            return False
        return (
            int(rect.get("right", 0)) > int(rect.get("left", 0))
            and int(rect.get("bottom", 0)) > int(rect.get("top", 0))
        )
    except (TypeError, ValueError):
        return False


def choose_window_recovery_rect(
    preferred: Mapping[str, int] | None,
    candidates: Iterable[Mapping[str, int] | None],
    fallback: Mapping[str, int] | None = None,
) -> dict:
    """Choose the first valid rectangle without ever reusing the sentinel."""
    for rect in (preferred, *candidates, fallback):
        if is_usable_window_rect(rect):
            return {
                "left": int(rect["left"]),
                "top": int(rect["top"]),
                "right": int(rect["right"]),
                "bottom": int(rect["bottom"]),
            }
    return {}


def advance_window_repair_confirmation(
    previous: Mapping[str, object] | None,
    *,
    hwnd: int,
    unhealthy: bool,
    now: float,
    threshold: int,
) -> tuple[dict, bool]:
    """Advance a per-HWND confirmation counter and report when repair is due."""
    if not unhealthy:
        return {}, False

    required = max(2, int(threshold))
    same_window = bool(previous and int(previous.get("hwnd", 0) or 0) == int(hwnd))
    count = int(previous.get("count", 0) or 0) + 1 if same_window else 1
    first_seen_at = (
        float(previous.get("first_seen_at", now) or now)
        if same_window
        else float(now)
    )
    state = {
        "hwnd": int(hwnd),
        "count": count,
        "first_seen_at": first_seen_at,
        "last_seen_at": float(now),
    }
    return state, count >= required
