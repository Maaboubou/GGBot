"""Single-instance support for the desktop launcher."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import IO, Self

from .constants import DATA_DIR, SHOW_SIGNAL_FILE


class SingleInstance:
    MUTEX_NAME = r"Local\MabobotDesktopLauncherV3"
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, lock_file: Path | None = None):
        self.lock_file = lock_file or (DATA_DIR / "launcher.lock")
        self._mutex = None
        self._file: IO[str] | None = None

    def acquire(self) -> bool:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            self._mutex = kernel32.CreateMutexW(None, False, self.MUTEX_NAME)
            if not self._mutex:
                raise ctypes.WinError()
            return kernel32.GetLastError() != self.ERROR_ALREADY_EXISTS

        import fcntl

        self._file = self.lock_file.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self._file.close()
            self._file = None
            return False

    def request_show(self) -> None:
        SHOW_SIGNAL_FILE.write_text(str(os.getpid()), encoding="ascii")

    def release(self) -> None:
        if os.name == "nt" and self._mutex:
            ctypes.windll.kernel32.CloseHandle(self._mutex)
            self._mutex = None
        if self._file:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    def __enter__(self) -> Self:
        if not self.acquire():
            self.request_show()
            raise RuntimeError("Mabobot launcher is already running")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
