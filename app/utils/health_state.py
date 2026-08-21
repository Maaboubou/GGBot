"""Health-state primitives shared by the Web process and the WeChat bridge."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from time import time
from typing import Any, Callable, Dict, Optional, Set


def _is_healthy(sample: Dict[str, Any]) -> bool:
    return bool(sample.get("wechat_connected") and sample.get("wechat_online"))


def stable_active_listeners(listener_status: Dict[str, Any]) -> Set[str]:
    """Return listeners that are present and registered as successfully bound.

    After a wx_bot process restart, old chat windows can still be discoverable
    while the new process has an empty desired-listener registry. Those windows
    need one callback rebind and therefore are not stable yet.
    """
    actual = {str(name) for name in (listener_status.get("actual") or []) if name}
    desired = {str(name) for name in (listener_status.get("desired") or []) if name}
    desired_meta = listener_status.get("desired_meta") or {}
    stable = set()
    for name in actual & desired:
        meta = desired_meta.get(name) or {}
        if meta.get("active") is True or meta.get("status") == "active":
            stable.add(name)
    return stable


def should_resync_connection(
    previous_connection_id: Optional[str],
    current_connection_id: Optional[str],
    *,
    recovered: bool,
) -> bool:
    """Return whether listener state belongs to a genuinely new connection.

    A health HTTP timeout can recover while wx_bot still owns the same WeChat
    object. Rebinding listeners in that case creates needless UI work. Older
    wx_bot versions do not expose connection IDs, so their confirmed recovery
    transition retains the legacy resync behavior.
    """
    if current_connection_id:
        return current_connection_id != previous_connection_id
    return bool(recovered)


@dataclass(frozen=True)
class HealthTransition:
    accepted: bool
    healthy: bool
    became_unhealthy: bool
    reconnected: bool
    consecutive_failures: int
    consecutive_successes: int
    confirmed: Dict[str, Any]


class ConsecutiveHealthGate:
    """Apply failure and recovery hysteresis to aggregate health samples."""

    def __init__(
        self,
        initial: Dict[str, Any],
        *,
        failure_threshold: int = 3,
        recovery_threshold: int = 2,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_threshold = max(1, int(recovery_threshold))
        self.confirmed = dict(initial)
        self.consecutive_failures = 0
        self.consecutive_successes = 0

    def observe(self, sample: Dict[str, Any]) -> HealthTransition:
        candidate = dict(sample)
        candidate_healthy = _is_healthy(candidate)
        previous_healthy = _is_healthy(self.confirmed)

        if not candidate_healthy:
            self.consecutive_successes = 0
            self.consecutive_failures += 1
            if previous_healthy and self.consecutive_failures < self.failure_threshold:
                return HealthTransition(
                    accepted=False,
                    healthy=previous_healthy,
                    became_unhealthy=False,
                    reconnected=False,
                    consecutive_failures=self.consecutive_failures,
                    consecutive_successes=0,
                    confirmed=dict(self.confirmed),
                )

            self.confirmed = candidate
            return HealthTransition(
                accepted=True,
                healthy=False,
                became_unhealthy=previous_healthy,
                reconnected=False,
                consecutive_failures=self.consecutive_failures,
                consecutive_successes=0,
                confirmed=dict(self.confirmed),
            )

        self.consecutive_failures = 0
        if not previous_healthy:
            self.consecutive_successes += 1
            if self.consecutive_successes < self.recovery_threshold:
                return HealthTransition(
                    accepted=False,
                    healthy=False,
                    became_unhealthy=False,
                    reconnected=False,
                    consecutive_failures=0,
                    consecutive_successes=self.consecutive_successes,
                    confirmed=dict(self.confirmed),
                )

        self.confirmed = candidate
        recovered = not previous_healthy
        accepted_successes = self.consecutive_successes
        self.consecutive_successes = 0
        return HealthTransition(
            accepted=True,
            healthy=True,
            became_unhealthy=False,
            reconnected=recovered,
            consecutive_failures=0,
            consecutive_successes=accepted_successes,
            confirmed=dict(self.confirmed),
        )


class OnlineProbeTracker:
    """Own one debounced online signal without turning busy UI into offline state.

    The WeChat UI is a serialized resource. A skipped probe means the UI is busy,
    while a failed probe is only a suspicion until the configured threshold is
    reached. Recovery is also confirmed over multiple samples so a single
    optimistic result cannot create a false reconnect event.
    """

    def __init__(
        self,
        *,
        initial_online: bool = False,
        failure_threshold: int = 3,
        recovery_threshold: int = 2,
        clock: Callable[[], float] = time,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_threshold = max(1, int(recovery_threshold))
        self._clock = clock
        self._lock = RLock()
        self._online = bool(initial_online)
        self._state = "healthy" if self._online else "offline"
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_checked_at = 0.0
        self._last_success_at = 0.0
        self._last_failure_at = 0.0
        self._last_duration_ms: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_failure_kind: Optional[str] = None
        self._source = "initial"
        self._skipped_reason: Optional[str] = None
        self._total_checks = 0
        self._total_skips = 0

    def mark_connected(self, *, source: str = "client_initialized", at: Optional[float] = None) -> Dict[str, Any]:
        now = self._clock() if at is None else float(at)
        with self._lock:
            self._online = True
            self._state = "healthy"
            self._consecutive_failures = 0
            self._consecutive_successes = 0
            self._last_checked_at = now
            self._last_success_at = now
            self._last_duration_ms = None
            self._last_error = None
            self._last_failure_kind = None
            self._source = source
            self._skipped_reason = None
            return self._snapshot_locked(now)

    def mark_disconnected(self, reason: str, *, at: Optional[float] = None) -> Dict[str, Any]:
        now = self._clock() if at is None else float(at)
        with self._lock:
            self._online = False
            self._state = "offline"
            self._consecutive_failures = self.failure_threshold
            self._consecutive_successes = 0
            self._last_checked_at = now
            self._last_failure_at = now
            self._last_duration_ms = None
            self._last_error = str(reason)
            self._last_failure_kind = "disconnected"
            self._source = "connection_supervisor"
            self._skipped_reason = None
            return self._snapshot_locked(now)

    def record_result(
        self,
        online: bool,
        *,
        duration_ms: Optional[float] = None,
        source: str = "connection_supervisor",
        at: Optional[float] = None,
    ) -> Dict[str, Any]:
        now = self._clock() if at is None else float(at)
        with self._lock:
            self._total_checks += 1
            self._last_checked_at = now
            self._last_duration_ms = None if duration_ms is None else round(float(duration_ms), 1)
            self._source = source
            self._skipped_reason = None

            if online:
                self._last_success_at = now
                self._last_error = None
                self._last_failure_kind = None
                self._consecutive_failures = 0
                if self._online:
                    self._state = "healthy"
                    self._consecutive_successes = 0
                else:
                    self._consecutive_successes += 1
                    if self._consecutive_successes >= self.recovery_threshold:
                        self._online = True
                        self._state = "healthy"
                        self._consecutive_successes = 0
                    else:
                        self._state = "recovering"
                return self._snapshot_locked(now)

            return self._record_failure_locked(
                now,
                kind="offline_result",
                error="IsOnline returned false",
            )

    def record_error(
        self,
        error: object,
        *,
        duration_ms: Optional[float] = None,
        source: str = "connection_supervisor",
        at: Optional[float] = None,
    ) -> Dict[str, Any]:
        now = self._clock() if at is None else float(at)
        with self._lock:
            self._total_checks += 1
            self._last_checked_at = now
            self._last_duration_ms = None if duration_ms is None else round(float(duration_ms), 1)
            self._source = source
            self._skipped_reason = None
            return self._record_failure_locked(now, kind="probe_error", error=str(error))

    def record_busy(self, reason: str = "ui_lock_busy", *, at: Optional[float] = None) -> Dict[str, Any]:
        now = self._clock() if at is None else float(at)
        with self._lock:
            self._total_skips += 1
            self._source = "connection_supervisor"
            self._skipped_reason = str(reason)
            if self._online and self._state == "healthy":
                self._state = "busy"
            return self._snapshot_locked(now)

    def snapshot(self, *, at: Optional[float] = None) -> Dict[str, Any]:
        now = self._clock() if at is None else float(at)
        with self._lock:
            return self._snapshot_locked(now)

    def _record_failure_locked(self, now: float, *, kind: str, error: str) -> Dict[str, Any]:
        self._last_failure_at = now
        self._last_error = error
        self._last_failure_kind = kind
        self._consecutive_successes = 0
        self._consecutive_failures += 1
        if self._online and self._consecutive_failures < self.failure_threshold:
            self._state = "suspect"
        else:
            self._online = False
            self._state = "offline"
        return self._snapshot_locked(now)

    def _snapshot_locked(self, now: float) -> Dict[str, Any]:
        age_sec = None
        if self._last_checked_at > 0:
            age_sec = round(max(0.0, now - self._last_checked_at), 1)
        return {
            "online": self._online,
            "state": self._state,
            "consecutive_failures": self._consecutive_failures,
            "consecutive_successes": self._consecutive_successes,
            "failure_threshold": self.failure_threshold,
            "recovery_threshold": self.recovery_threshold,
            "checked_at": self._last_checked_at or None,
            "age_sec": age_sec,
            "last_success_at": self._last_success_at or None,
            "last_failure_at": self._last_failure_at or None,
            "last_duration_ms": self._last_duration_ms,
            "last_error": self._last_error,
            "last_failure_kind": self._last_failure_kind,
            "source": self._source,
            "skipped_reason": self._skipped_reason,
            "total_checks": self._total_checks,
            "total_skips": self._total_skips,
        }
