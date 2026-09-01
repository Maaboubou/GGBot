"""Serialize WSL control-plane calls and stop probe storms after transport failures."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any, Callable, Sequence

from app.utils.subprocess_utils import apply_hidden_process_defaults


class WslCircuitOpenError(subprocess.SubprocessError):
    """Raised while the WSL control-plane circuit is cooling down."""


class WslTransportError(subprocess.SubprocessError):
    """Raised for an explicit WSL control-plane transport failure."""


_RUN_LOCK = threading.Lock()
_STATE_LOCK = threading.RLock()
_consecutive_failures = 0
_open_until = 0.0
_last_error = ""


def _positive_number(name: str, default: float) -> float:
    try:
        value = float(str(os.getenv(name) or default).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _failure_threshold() -> int:
    return max(1, int(_positive_number("MABOBOT_WSL_CIRCUIT_FAILURES", 1)))


def _cooldown_seconds() -> float:
    return _positive_number("MABOBOT_WSL_CIRCUIT_SECONDS", 90.0)


def _circuit_error(now: float) -> WslCircuitOpenError | None:
    with _STATE_LOCK:
        if now >= _open_until:
            return None
        remaining = max(1, int(_open_until - now + 0.999))
        detail = _last_error or "WSL transport unavailable"
    return WslCircuitOpenError(f"WSL 探测已熔断，请在约 {remaining} 秒后重试：{detail}")


def _record_success() -> None:
    global _consecutive_failures, _open_until, _last_error
    with _STATE_LOCK:
        _consecutive_failures = 0
        _open_until = 0.0
        _last_error = ""


def _record_failure(error: BaseException | str) -> None:
    global _consecutive_failures, _open_until, _last_error
    detail = str(error or "WSL transport unavailable").strip()[-1000:]
    with _STATE_LOCK:
        _consecutive_failures += 1
        _last_error = detail
        if _consecutive_failures >= _failure_threshold():
            _open_until = time.monotonic() + _cooldown_seconds()


def _is_transport_failure(result: Any) -> bool:
    if int(getattr(result, "returncode", 0) or 0) == 0:
        return False
    detail = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}".lower()
    markers = (
        "utilbindvsock",
        "initcreateprocessutilityvm",
        "wsl/service",
        "wsl_e_",
        "the virtual machine or container exited unexpectedly",
        "the operation timed out",
        "socket failed",
    )
    return any(marker in detail for marker in markers)


def run_guarded_wsl_command(
    command: Sequence[str],
    *,
    runner: Callable[..., Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Run one WSL command at a time and fail fast during circuit cooldown."""
    execute = runner or subprocess.run
    run_kwargs = apply_hidden_process_defaults(kwargs)
    error = _circuit_error(time.monotonic())
    if error is not None:
        raise error
    with _RUN_LOCK:
        error = _circuit_error(time.monotonic())
        if error is not None:
            raise error
        try:
            result = execute(list(command), **run_kwargs)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _record_failure(exc)
            raise
        if _is_transport_failure(result):
            detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "WSL transport failure")
            _record_failure(str(detail).strip())
            raise WslTransportError(str(detail).strip())
        else:
            _record_success()
        return result


def wsl_circuit_status() -> dict[str, Any]:
    """Expose non-sensitive diagnostics for health endpoints and tests."""
    now = time.monotonic()
    with _STATE_LOCK:
        return {
            "open": now < _open_until,
            "retry_after_seconds": max(0, int(_open_until - now + 0.999)),
            "consecutive_failures": _consecutive_failures,
            "last_error": _last_error,
        }


def reset_wsl_probe_guard() -> None:
    """Reset process-local state; intended for recovery hooks and isolated tests."""
    _record_success()
