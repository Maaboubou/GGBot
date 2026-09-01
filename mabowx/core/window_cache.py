"""主窗口 HWND/PID 缓存。

缓存必须经过验证才可以使用；默认过期时间 5 分钟。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_CACHE_DIR = Path.home() / ".mabowx" / "runtime"
DEFAULT_CACHE_FILE = DEFAULT_CACHE_DIR / "window_cache.json"
DEFAULT_TTL = 300.0


class WindowCache:
    """一个简单、线程安全的 JSON 文件缓存。"""

    def __init__(
        self,
        cache_file: Path | str | None = None,
        ttl: float = DEFAULT_TTL,
        validator: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        self.cache_file = Path(cache_file) if cache_file else DEFAULT_CACHE_FILE
        self.ttl = ttl
        self.validator = validator or (lambda entry: True)
        self._lock = threading.RLock()
        self._entries: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            if not self.cache_file.exists():
                return
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._entries = data
        except Exception:
            self._entries = {}

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(key)
            if not isinstance(entry, dict):
                return None
            timestamp = float(entry.get("timestamp", 0))
            if self.ttl > 0 and time.time() - timestamp > self.ttl:
                self._entries.pop(key, None)
                return None
            if not self.validator(entry):
                self._entries.pop(key, None)
                return None
            return dict(entry)

    def put(self, key: str, **fields: Any) -> None:
        with self._lock:
            entry = {"timestamp": time.time(), **fields}
            self._entries[key] = entry
            self._save()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            try:
                if self.cache_file.exists():
                    self.cache_file.unlink()
            except Exception:
                pass

    def _save(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps(self._entries, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
