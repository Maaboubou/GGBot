"""Windows subprocess defaults for background Mabobot services."""

from __future__ import annotations

import os
import subprocess
from typing import Any


def hidden_creation_flags(
    existing: int = 0,
    *,
    platform_name: str | None = None,
) -> int:
    """Add ``CREATE_NO_WINDOW`` on Windows while preserving caller flags."""
    platform = os.name if platform_name is None else platform_name
    if platform != "nt":
        return int(existing or 0)
    no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    return int(existing or 0) | no_window


def hidden_process_kwargs(
    *,
    existing_creationflags: int = 0,
    new_process_group: bool = False,
    platform_name: str | None = None,
) -> dict[str, Any]:
    """Return portable ``Popen`` kwargs that suppress transient consoles."""
    platform = os.name if platform_name is None else platform_name
    if platform != "nt":
        return {}
    flags = int(existing_creationflags or 0)
    if new_process_group:
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    return {
        "creationflags": hidden_creation_flags(flags, platform_name=platform),
    }


def apply_hidden_process_defaults(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Copy subprocess kwargs and merge the Windows no-console flag."""
    result = dict(kwargs)
    hidden = hidden_process_kwargs(
        existing_creationflags=int(result.get("creationflags", 0) or 0)
    )
    result.update(hidden)
    return result
