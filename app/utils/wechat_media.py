"""Pure helpers for auditing and classifying WeChat media downloads."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Collection, Dict


_CHAT_WINDOW_CLASSES = {
    "mmui::MainWindow",
    "mmui::ChatSingleWindow",
}
_MEDIA_WINDOW_HINTS = (
    "image",
    "picture",
    "photo",
    "preview",
    "gallery",
    "media",
)
_MEDIA_WINDOW_NAME_HINTS = (
    "图片",
    "预览",
    "image",
    "picture",
    "photo",
    "preview",
    "gallery",
)


def is_probable_wechat_media_window(
    *,
    class_name: str,
    name: str,
    handle: int,
    process_id: int,
    expected_process_id: int = 0,
    baseline_handles: Collection[int] = (),
    listener_names: Collection[str] = (),
) -> bool:
    """Return whether a top-level control is safe to treat as a media popup.

    Main/listener chat windows and the embedded browser are always excluded.
    A newly-created, same-process ``mmui`` top-level window is treated as a
    media popup while a download is in progress even if its exact class name
    changes between WeChat releases.
    """

    class_name = str(class_name or "").strip()
    name = str(name or "").strip()
    handle = int(handle or 0)
    process_id = int(process_id or 0)
    expected_process_id = int(expected_process_id or 0)

    if not handle:
        return False
    if class_name in _CHAT_WINDOW_CLASSES or class_name == "Chrome_WidgetWin_0":
        return False
    if class_name == "mmui::FramelessMainWindow" and name in set(listener_names):
        return False

    class_lower = class_name.lower()
    name_lower = name.lower()
    if any(hint in class_lower for hint in _MEDIA_WINDOW_HINTS):
        return True
    if class_name.startswith("mmui::") and any(
        hint in name_lower for hint in _MEDIA_WINDOW_NAME_HINTS
    ):
        return True

    # Generic/version-specific windows require same-process evidence. Explicit
    # mmui media class/name matches above are strong enough on their own and
    # also cover WeChat builds that host previews in a helper process.
    if expected_process_id and process_id and process_id != expected_process_id:
        return False

    # wxautox4 can leave a version-specific, generically named WeChat popup
    # behind. Only classify it generically when it appeared during this exact
    # operation; this avoids closing unrelated pre-existing WeChat windows.
    baseline = {int(item or 0) for item in baseline_handles}
    if class_name.startswith("mmui::") and handle not in baseline:
        return True
    return False


def image_file_fingerprint(file_path: str | Path) -> Dict[str, Any]:
    """Describe image bytes for message-to-file audit logs."""

    path = Path(file_path)
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as file_obj:
        while True:
            chunk = file_obj.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            byte_count += len(chunk)

    width = height = None
    image_format = None
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format
    except Exception:
        # The existence/hash check remains useful if Pillow cannot parse a
        # format or a non-image path is accidentally returned by wxautox4.
        pass

    return {
        "path": str(path),
        "bytes": byte_count,
        "sha256": digest.hexdigest(),
        "width": width,
        "height": height,
        "format": image_format,
    }
