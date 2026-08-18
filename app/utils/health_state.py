"""Debounce transient health failures before changing confirmed state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Set


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


@dataclass(frozen=True)
class HealthTransition:
    accepted: bool
    healthy: bool
    became_unhealthy: bool
    reconnected: bool
    consecutive_failures: int
    confirmed: Dict[str, Any]


class ConsecutiveHealthGate:
    """Ignore short unhealthy runs and expose confirmed state transitions."""

    def __init__(self, initial: Dict[str, Any], *, failure_threshold: int = 3) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.confirmed = dict(initial)
        self.consecutive_failures = 0

    def observe(self, sample: Dict[str, Any]) -> HealthTransition:
        candidate = dict(sample)
        candidate_healthy = _is_healthy(candidate)
        previous_healthy = _is_healthy(self.confirmed)

        if not candidate_healthy:
            self.consecutive_failures += 1
            if self.consecutive_failures < self.failure_threshold:
                return HealthTransition(
                    accepted=False,
                    healthy=previous_healthy,
                    became_unhealthy=False,
                    reconnected=False,
                    consecutive_failures=self.consecutive_failures,
                    confirmed=dict(self.confirmed),
                )

            self.confirmed = candidate
            return HealthTransition(
                accepted=True,
                healthy=False,
                became_unhealthy=previous_healthy,
                reconnected=False,
                consecutive_failures=self.consecutive_failures,
                confirmed=dict(self.confirmed),
            )

        self.consecutive_failures = 0
        self.confirmed = candidate
        return HealthTransition(
            accepted=True,
            healthy=True,
            became_unhealthy=False,
            reconnected=not previous_healthy,
            consecutive_failures=0,
            confirmed=dict(self.confirmed),
        )
