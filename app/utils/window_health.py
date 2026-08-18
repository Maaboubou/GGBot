"""Pure helpers for classifying native window geometry."""

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
