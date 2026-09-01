"""UI 事务锁。

- 线程维度：RLock，允许同一线程重入。
- 进程维度：Windows 命名 Mutex，防止多进程同时操作微信 UI。
- 非 Windows 平台：自动退化为纯线程锁，便于 Linux 上跑纯逻辑测试。
"""

from __future__ import annotations

import functools
import hashlib
import math
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from .win32 import is_windows

_thread_lock = threading.RLock()
_mutex_handle = None
_mutex_pid: int | None = None


def _open_named_mutex():
    """创建当前用户会话级命名 Mutex，不暴露用户 SID。"""
    global _mutex_handle, _mutex_pid
    if not is_windows():
        return None

    import win32api
    import win32con
    import win32event
    import win32security
    import win32ts

    pid = os.getpid()
    if _mutex_handle is not None and _mutex_pid == pid:
        return _mutex_handle

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    sid_text = win32security.ConvertSidToStringSid(sid)
    identity = hashlib.sha256(sid_text.encode("ascii")).hexdigest()[:24]
    session = win32ts.ProcessIdToSessionId(pid)
    name = f"Local\\mabowx-ui-{identity}-{session}"
    _mutex_handle = win32event.CreateMutex(None, False, name)
    _mutex_pid = pid
    return _mutex_handle


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


@contextmanager
def ui_transaction(timeout: float | None = 30.0) -> Iterator[None]:
    """把一段 UI 操作串行化。"""
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise ValueError("UI transaction timeout must be a finite non-negative number")

    deadline = None if timeout is None else time.monotonic() + timeout
    remaining = _remaining(deadline)
    acquired_thread = (
        _thread_lock.acquire()
        if remaining is None
        else _thread_lock.acquire(timeout=remaining)
    )
    if not acquired_thread:
        raise TimeoutError("等待 mabowx UI 事务锁超时")

    acquired_mutex = False
    try:
        handle = _open_named_mutex()
        if handle is not None:
            import win32event

            remaining = _remaining(deadline)
            wait_ms = win32event.INFINITE if remaining is None else math.ceil(remaining * 1000)
            outcome = win32event.WaitForSingleObject(handle, wait_ms)
            if outcome == win32event.WAIT_TIMEOUT:
                raise TimeoutError("等待 mabowx UI 进程锁超时")
            if outcome not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
                raise OSError(f"命名 Mutex 等待结果异常: {outcome}")
            acquired_mutex = True
        yield
    finally:
        if acquired_mutex:
            import win32event

            win32event.ReleaseMutex(handle)
        _thread_lock.release()


def uilock(func):
    """把单个 UI 操作包装为可重入的线程/进程事务。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with ui_transaction():
            return func(*args, **kwargs)

    return wrapper
