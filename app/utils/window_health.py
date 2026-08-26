"""Pure helpers for classifying native window geometry."""

from __future__ import annotations

from typing import Iterable, Mapping

OFFSCREEN_SENTINEL = -30000


def _normalized_rect(rect: Mapping[str, int] | None) -> dict:
    value = rect or {}
    return {
        "left": int(value.get("left", 0) or 0),
        "top": int(value.get("top", 0) or 0),
        "right": int(value.get("right", 0) or 0),
        "bottom": int(value.get("bottom", 0) or 0),
    }


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


def build_window_observation(
    matches: Iterable[Mapping[str, object]] | None,
    *,
    observable: bool = True,
) -> dict:
    """Build one compact, JSON-safe observation for an expected native window.

    The result reuses data already collected by the Win32 geometry scan.  It
    deliberately contains no UIA probe or foreground operation.
    """
    candidates = [dict(item) for item in (matches or [])]
    if not observable:
        return {
            "state": "unobservable",
            "match_count": 0,
            "candidate_hwnds": [],
        }
    if not candidates:
        return {
            "state": "missing",
            "match_count": 0,
            "candidate_hwnds": [],
        }

    candidate_hwnds = sorted(int(item.get("hwnd") or 0) for item in candidates)
    if len(candidates) != 1:
        return {
            "state": "ambiguous",
            "match_count": len(candidates),
            "candidate_hwnds": candidate_hwnds,
        }

    target = candidates[0]
    window_rect = _normalized_rect(target.get("window_rect"))
    normal_rect = _normalized_rect(target.get("normal_rect"))
    visible = bool(target.get("visible"))
    iconic = bool(target.get("iconic"))
    offscreen_sentinel = bool(
        target.get("offscreen_sentinel", is_offscreen_sentinel_rect(window_rect))
    )
    normal_offscreen_sentinel = bool(
        target.get(
            "normal_offscreen_sentinel",
            is_offscreen_sentinel_rect(normal_rect),
        )
    )
    unrecoverable_offscreen = bool(
        target.get(
            "unrecoverable_offscreen",
            is_unrecoverable_offscreen_window(
                window_rect=window_rect,
                normal_rect=normal_rect,
                iconic=iconic,
            ),
        )
    )

    if unrecoverable_offscreen:
        state = "unrecoverable_offscreen"
    elif not visible:
        state = "hidden"
    elif iconic:
        state = "minimized"
    elif offscreen_sentinel or normal_offscreen_sentinel:
        state = "partial_offscreen_sentinel"
    else:
        state = "healthy"

    return {
        "state": state,
        "match_count": 1,
        "candidate_hwnds": candidate_hwnds,
        "hwnd": int(target.get("hwnd") or 0),
        "pid": int(target.get("pid") or 0),
        "class_name": str(target.get("class_name") or ""),
        "visible": visible,
        "iconic": iconic,
        "show_cmd": int(target.get("show_cmd") or 0),
        "window_rect": window_rect,
        "normal_rect": normal_rect,
        "offscreen_sentinel": offscreen_sentinel,
        "normal_offscreen_sentinel": normal_offscreen_sentinel,
        "unrecoverable_offscreen": unrecoverable_offscreen,
    }


def window_observation_fingerprint(observation: Mapping[str, object] | None) -> tuple:
    """Return the semantic fields that should create a transition audit line."""
    value = observation or {}
    return (
        str(value.get("state") or ""),
        int(value.get("match_count") or 0),
        tuple(int(item or 0) for item in (value.get("candidate_hwnds") or [])),
        int(value.get("hwnd") or 0),
        int(value.get("pid") or 0),
        bool(value.get("visible")),
        bool(value.get("iconic")),
        int(value.get("show_cmd") or 0),
        bool(value.get("offscreen_sentinel")),
        bool(value.get("normal_offscreen_sentinel")),
        bool(value.get("unrecoverable_offscreen")),
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
