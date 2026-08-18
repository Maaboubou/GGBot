"""Single authoritative global order for plugin event listeners."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict, Iterable, List


class RoutingOrderError(ValueError):
    pass


class RoutingOrderStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._events: Dict[str, List[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._events = {}
            return
        payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if payload.get("schema_version") != 1 or not isinstance(payload.get("events"), dict):
            raise RoutingOrderError("routing_order.json 格式无效")
        events: Dict[str, List[str]] = {}
        for event, keys in payload["events"].items():
            if not isinstance(keys, list) or any(not isinstance(key, str) or not key for key in keys):
                raise RoutingOrderError(f"routing_order.json 中 {event} 必须是非空字符串数组")
            if len(keys) != len(set(keys)):
                raise RoutingOrderError(f"routing_order.json 中 {event} 存在重复监听器")
            events[str(event)] = list(keys)
        self._events = events

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {"schema_version": 1, "events": self._events},
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def index_for(self, event: str, listener_key: str) -> int:
        """Return the global order index, appending newly installed listeners once."""
        with self._lock:
            keys = self._events.setdefault(event, [])
            if listener_key not in keys:
                keys.append(listener_key)
                self._save()
            return keys.index(listener_key)

    def replace_event(self, event: str, listener_keys: Iterable[str]) -> None:
        keys = list(listener_keys)
        if not keys or any(not isinstance(key, str) or not key for key in keys):
            raise RoutingOrderError("执行顺序不能为空")
        if len(keys) != len(set(keys)):
            raise RoutingOrderError("执行顺序包含重复监听器")
        with self._lock:
            previous = self._events.get(event, [])
            inactive = [key for key in previous if key not in keys]
            self._events[event] = keys + inactive
            self._save()

    def event_order(self, event: str) -> List[str]:
        with self._lock:
            return list(self._events.get(event, []))

    def snapshot(self) -> Dict[str, List[str]]:
        with self._lock:
            return {event: list(keys) for event, keys in self._events.items()}
