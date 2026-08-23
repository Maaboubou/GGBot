"""Bounded background scheduling for conversational-memory pipelines."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable


class MemoryBackgroundScheduler:
    def __init__(self, *, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="chat-memory",
        )
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._closed = False

    def submit(
        self,
        chat_name: str,
        work: Callable[[], None],
        *,
        logger: logging.Logger,
    ) -> bool:
        if self._closed or not chat_name:
            return False
        with self._lock:
            if chat_name in self._pending:
                return False
            self._pending.add(chat_name)

        def worker() -> None:
            try:
                work()
            except Exception:
                logger.exception("⚠️ Memory background refresh failed for %s", chat_name)
            finally:
                with self._lock:
                    self._pending.discard(chat_name)

        self._executor.submit(worker)
        return True

    def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
