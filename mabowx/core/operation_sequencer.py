"""Per-chat ordering for delayed stateful UI operations."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Hashable


@dataclass(eq=False)
class _PendingOperation:
    sequence: int
    ticket: int
    enqueued_at: float


class OrderedOperationSequencer:
    """Serialize operations per scope, preferring the oldest delivery.

    HTTP handlers can reach :meth:`Message.download` in a different order from
    the listener callbacks that created the messages.  A short coalescing
    window lets an older delivery enter first, while an active operation keeps
    all later work queued.  This belongs to the UI library because callers
    should not need to understand WeChat's stateful message list.
    """

    def __init__(
        self,
        *,
        reorder_window_seconds: float = 0.35,
        wait_timeout_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.reorder_window_seconds = max(0.0, float(reorder_window_seconds))
        self.wait_timeout_seconds = max(0.1, float(wait_timeout_seconds))
        self._clock = clock
        self._condition = threading.Condition()
        self._pending: dict[Hashable, list[_PendingOperation]] = {}
        self._active: set[Hashable] = set()
        self._next_ticket = 0

    def run(
        self,
        scope: Hashable,
        sequence: int,
        operation: Callable[[], Any],
    ) -> Any:
        started_waiting = self._clock()
        with self._condition:
            self._next_ticket += 1
            pending = _PendingOperation(
                sequence=int(sequence or 0),
                ticket=self._next_ticket,
                enqueued_at=started_waiting,
            )
            self._pending.setdefault(scope, []).append(pending)
            self._condition.notify_all()

            while True:
                now = self._clock()
                elapsed = now - started_waiting
                if elapsed >= self.wait_timeout_seconds:
                    self._remove_pending_locked(scope, pending)
                    self._condition.notify_all()
                    raise TimeoutError(
                        "等待有序 UI 操作超时: "
                        f"scope={scope!r} sequence={sequence!r}"
                    )

                queue = self._pending.get(scope, [])
                first = min(
                    queue,
                    key=lambda item: (
                        item.sequence if item.sequence > 0 else 2**63,
                        item.ticket,
                    ),
                    default=None,
                )
                coalesced = (
                    now - pending.enqueued_at >= self.reorder_window_seconds
                    or len(queue) > 1
                )
                if scope not in self._active and first is pending and coalesced:
                    self._remove_pending_locked(scope, pending)
                    self._active.add(scope)
                    break

                coalesce_remaining = max(
                    0.01,
                    self.reorder_window_seconds - (now - pending.enqueued_at),
                )
                timeout_remaining = max(0.01, self.wait_timeout_seconds - elapsed)
                self._condition.wait(
                    timeout=min(coalesce_remaining, timeout_remaining, 0.25)
                )

        try:
            return operation()
        finally:
            with self._condition:
                self._active.discard(scope)
                self._condition.notify_all()

    def _remove_pending_locked(
        self,
        scope: Hashable,
        pending: _PendingOperation,
    ) -> None:
        queue = self._pending.get(scope)
        if not queue:
            return
        try:
            queue.remove(pending)
        except ValueError:
            pass
        if not queue:
            self._pending.pop(scope, None)
