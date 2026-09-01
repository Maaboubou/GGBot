"""Application-side audit metadata for files already returned by mabowx.

This module intentionally contains no WeChat window detection, UI cleanup, or
download routing.  Those responsibilities belong to mabowx.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict


def image_file_fingerprint(file_path: str | Path) -> Dict[str, Any]:
    """Describe downloaded image bytes for application audit logs."""
    path = Path(file_path)
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as file_obj:
        while chunk := file_obj.read(1024 * 1024):
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
        pass

    return {
        "path": str(path),
        "bytes": byte_count,
        "sha256": digest.hexdigest(),
        "width": width,
        "height": height,
        "format": image_format,
    }
