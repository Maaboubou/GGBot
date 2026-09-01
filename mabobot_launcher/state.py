"""Launcher capability state and file-based Web control compatibility."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .constants import LAUNCHER_ID, SIGNAL_PROTOCOL, STATE_FILE


def write_launcher_state(path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "launcher_id": LAUNCHER_ID,
        "pid": os.getpid(),
        "signal_protocol": SIGNAL_PROTOCOL,
        "started_at": time.time(),
    }
    temporary = path.with_suffix(
        f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def clear_launcher_state(path: Path = STATE_FILE) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if int(payload.get("pid", -1)) == os.getpid():
            path.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return


def consume_control_signal(path: Path) -> str:
    requested_action = path.read_text(encoding="utf-8").strip().casefold()
    path.unlink(missing_ok=True)
    aliases = {
        "web": "web",
        "app": "web",
        "start.py": "web",
        "start-bot": "start-bot",
        "stop-bot": "stop-bot",
    }
    return aliases.get(requested_action, "all")
