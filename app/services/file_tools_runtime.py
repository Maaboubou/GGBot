"""Portable discovery and launch helpers for Codex-side document tools."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Optional

from app.services.wsl_probe_guard import (
    WslCircuitOpenError,
    WslTransportError,
    run_guarded_wsl_command,
)
from app.utils.subprocess_utils import apply_hidden_process_defaults


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROBE_SCRIPT = PROJECT_ROOT / "scripts" / "file_tools" / "probe_runtime.py"
RUN_SCRIPT = PROJECT_ROOT / "scripts" / "file_tools" / "run_codex.py"
_CACHE_TTL_SECONDS = 60.0
_CACHE_LOCK = threading.RLock()
_CACHE: dict[tuple[bool, str], tuple[float, dict[str, Any]]] = {}
_LAST_GOOD: dict[tuple[bool, str], dict[str, Any]] = {}
CODEX_WSL_SETTING_KEY = "CODEX_PROXY_WSL_BIN"


def _default_use_wsl() -> bool:
    value = str(os.getenv("CODEX_PROXY_USE_WSL") or "true").strip().lower()
    return os.name == "nt" and value not in {"0", "false", "no", "off"}


def _as_wsl_path(path: Path | str) -> str:
    value = str(path)
    if os.name != "nt":
        return value.replace("\\", "/")
    windows = PureWindowsPath(value)
    drive = windows.drive.rstrip(":").lower()
    if drive:
        remainder = "/".join(windows.parts[1:])
        return f"/mnt/{drive}/{remainder}"
    return value.replace("\\", "/")


def _configured_tool_roots() -> list[str]:
    names = (
        "MABOBOT_FILE_TOOLS_PREFIX",
        "MABOBOT_TESSERACT_PREFIX",
        "MABOBOT_CLAMAV_ROOT",
        "MABOBOT_CLAMAV_DATABASE",
    )
    return [str(os.getenv(name) or "").strip() for name in names if str(os.getenv(name) or "").strip()]


def get_codex_bin_selection(*, use_wsl: Optional[bool] = None) -> dict[str, str]:
    """Resolve the persisted runtime selection before environment defaults."""
    resolved_wsl = _default_use_wsl() if use_wsl is None else bool(use_wsl)
    setting_key = CODEX_WSL_SETTING_KEY if resolved_wsl else "CODEX_PROXY_BIN"
    if resolved_wsl:
        try:
            from app.services.config_service import get_setting

            persisted = str(get_setting(setting_key, "") or "").strip()
        except Exception:
            persisted = ""
        if persisted:
            return {"configured": persisted, "source": "setting"}
    environment = str(os.getenv(setting_key) or "").strip()
    if environment:
        return {"configured": environment, "source": "environment"}
    return {"configured": "codex", "source": "automatic"}


def _validate_wsl_codex_path(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized.startswith("/") or "\\" in normalized:
        raise ValueError("请输入 WSL 内的绝对路径，例如 /home/user/.local/bin/codex")
    if Path(normalized).as_posix().lower().startswith(tuple(f"/mnt/{letter}/" for letter in "abcdefghijklmnopqrstuvwxyz")):
        raise ValueError("不能选择 Windows 挂载目录中的 Codex，请选择 WSL 原生路径")
    return normalized


def persist_wsl_codex_bin(path: str) -> str:
    """Persist an already-probed WSL Codex executable selection."""
    normalized = _validate_wsl_codex_path(path)
    from app.services.config_service import update_setting

    if not update_setting(CODEX_WSL_SETTING_KEY, normalized):
        raise RuntimeError("保存 Codex 路径失败")
    with _CACHE_LOCK:
        _CACHE.clear()
    return normalized


def _probe_command(*, use_wsl: bool, codex_bin: str) -> list[str]:
    script = _as_wsl_path(PROBE_SCRIPT) if use_wsl else str(PROBE_SCRIPT)
    command = ["wsl.exe", "python3", script] if use_wsl else [sys.executable, script]
    command.extend(["--json", "--codex-bin", codex_bin or "codex"])
    for root in _configured_tool_roots():
        command.extend(["--trusted-root", root])
    return command


def _failure_snapshot(*, use_wsl: bool, error: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "runtime": "wsl" if use_wsl else ("windows" if os.name == "nt" else "linux"),
        "codex": {"available": False},
        "commands": [],
        "command_names": [],
        "tool_roots": [],
        "permission_roots": [],
        "path_dirs": [],
        "error": str(error or "Runtime capability probe failed")[-2000:],
    }


def get_file_tools_runtime(
    *,
    use_wsl: Optional[bool] = None,
    codex_bin: Optional[str] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Return a cached snapshot from the same host where Codex executes."""
    resolved_wsl = _default_use_wsl() if use_wsl is None else bool(use_wsl)
    selection = (
        {"configured": str(codex_bin).strip(), "source": "request"}
        if codex_bin is not None and str(codex_bin).strip()
        else get_codex_bin_selection(use_wsl=resolved_wsl)
    )
    resolved_bin = selection["configured"]
    cache_key = (resolved_wsl, resolved_bin)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and not force and now - cached[0] < _CACHE_TTL_SECONDS:
            return deepcopy(cached[1])

    try:
        command = _probe_command(use_wsl=resolved_wsl, codex_bin=resolved_bin)
        run = run_guarded_wsl_command if resolved_wsl else subprocess.run
        run_kwargs = {
            "capture_output": True,
            "text": True,
            "timeout": 20,
            "check": False,
        }
        run_kwargs = apply_hidden_process_defaults(run_kwargs)
        if resolved_wsl:
            result = run(command, runner=subprocess.run, **run_kwargs)
        else:
            result = run(command, **run_kwargs)
        output_lines = (result.stdout or "").strip().splitlines()
        if not output_lines:
            detail = (result.stderr or "Runtime capability probe returned no data").strip()
            snapshot = _failure_snapshot(use_wsl=resolved_wsl, error=detail)
        else:
            snapshot = json.loads(output_lines[-1])
            if not isinstance(snapshot, dict):
                raise ValueError("Runtime capability probe returned a non-object")
            if result.returncode != 0 and not snapshot.get("error"):
                snapshot["error"] = (result.stderr or "Codex CLI was not detected").strip()
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        with _CACHE_LOCK:
            last_good = deepcopy(_LAST_GOOD.get(cache_key))
        if resolved_wsl and last_good is not None and isinstance(
            exc,
            (OSError, subprocess.TimeoutExpired, WslCircuitOpenError, WslTransportError),
        ):
            last_good["stale"] = True
            last_good["stale_reason"] = str(exc)
            return last_good
        snapshot = _failure_snapshot(use_wsl=resolved_wsl, error=str(exc))

    snapshot["use_wsl"] = resolved_wsl
    snapshot["probe_command"] = "wsl.exe python3" if resolved_wsl else Path(sys.executable).name
    snapshot["selection"] = selection
    with _CACHE_LOCK:
        _CACHE[cache_key] = (now, deepcopy(snapshot))
        if snapshot.get("status") == "ready" and snapshot.get("codex", {}).get("available"):
            clean_snapshot = deepcopy(snapshot)
            clean_snapshot.pop("stale", None)
            clean_snapshot.pop("stale_reason", None)
            _LAST_GOOD[cache_key] = clean_snapshot
    return snapshot


def refresh_file_tools_runtime(*, use_wsl: Optional[bool] = None) -> dict[str, Any]:
    with _CACHE_LOCK:
        _CACHE.clear()
    return get_file_tools_runtime(use_wsl=use_wsl, force=True)


def build_codex_runtime_command(
    args: Iterable[Any],
    *,
    use_wsl: bool,
    snapshot: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Build an argv-only command; no WSL login shell or interpolation."""
    values = [str(arg) for arg in args]
    if not use_wsl or not values:
        return values
    runtime = snapshot or get_file_tools_runtime(use_wsl=True, codex_bin=values[0])
    codex_path = str(runtime.get("codex", {}).get("path") or values[0])
    payload = {
        "codex_bin": codex_path,
        "path_dirs": [str(value) for value in runtime.get("path_dirs") or []],
        "tool_roots": [str(value) for value in runtime.get("tool_roots") or []],
        "args": values[1:],
    }
    encoded_payload = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return [
        "wsl.exe",
        "python3",
        _as_wsl_path(RUN_SCRIPT),
        "--payload",
        encoded_payload,
    ]


def runtime_permission_roots(snapshot: Optional[dict[str, Any]]) -> tuple[str, ...]:
    values = (snapshot or {}).get("permission_roots") or []
    return tuple(dict.fromkeys(str(value) for value in values if str(value).startswith("/")))


def runtime_command_names(snapshot: Optional[dict[str, Any]]) -> tuple[str, ...]:
    values = (snapshot or {}).get("command_names") or []
    return tuple(dict.fromkeys(str(value) for value in values if str(value).strip()))
