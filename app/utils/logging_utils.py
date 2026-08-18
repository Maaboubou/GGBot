"""日志轮转与低内存读取工具。"""

import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional, Union


DEFAULT_LOG_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5
DEFAULT_LOG_TAIL_CHUNK_BYTES = 64 * 1024
DEFAULT_ROLLOVER_RETRY_SECONDS = 60.0


@dataclass(frozen=True)
class LogReadResult:
    lines: List[str]
    total_lines: Optional[int]
    filtered_count: Optional[int]
    counts_exact: bool
    strategy: str


class ResilientRotatingFileHandler(RotatingFileHandler):
    """Windows 文件暂时被占用时保留日志，并延后轮转重试。"""

    def __init__(self, *args, rollover_retry_seconds: float = DEFAULT_ROLLOVER_RETRY_SECONDS, **kwargs):
        self.rollover_retry_seconds = max(1.0, float(rollover_retry_seconds))
        self._rollover_retry_after = 0.0
        super().__init__(*args, **kwargs)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            now = time.monotonic()
            if now >= self._rollover_retry_after and self.shouldRollover(record):
                try:
                    self.doRollover()
                    self._rollover_retry_after = 0.0
                except OSError:
                    # Windows 上只要另一个进程仍持有文件句柄，rename 就会失败。
                    # 先继续写当前文件，避免丢日志；稍后再尝试轮转。
                    self._rollover_retry_after = now + self.rollover_retry_seconds
                    if self.stream is None:
                        self.stream = self._open()
            logging.FileHandler.emit(self, record)
        except Exception:
            self.handleError(record)


def create_rotating_file_handler(
    log_path: Union[str, Path],
    *,
    encoding: str = "utf-8",
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> ResilientRotatingFileHandler:
    """创建统一的按大小轮转处理器。"""
    path = Path(log_path)
    os.makedirs(path.parent, exist_ok=True)
    return ResilientRotatingFileHandler(
        path,
        mode="a",
        maxBytes=max(1, int(max_bytes)),
        backupCount=max(1, int(backup_count)),
        encoding=encoding,
        delay=True,
    )


def _read_tail_lines(
    log_path: Path,
    max_lines: int,
    *,
    chunk_bytes: int = DEFAULT_LOG_TAIL_CHUNK_BYTES,
) -> List[str]:
    """从文件末尾反向读取物理行，耗时和文件总大小基本无关。"""
    chunks = []
    newline_count = 0

    with log_path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        while position > 0 and newline_count <= max_lines:
            read_size = min(max(1024, int(chunk_bytes)), position)
            position -= read_size
            stream.seek(position)
            chunk = stream.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")

    if not chunks:
        return []

    raw_lines = b"".join(reversed(chunks)).splitlines(keepends=True)
    return [
        line.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        for line in raw_lines[-max_lines:]
    ]


def _log_files_in_chronological_order(log_path: Path, include_rotated: bool) -> List[Path]:
    """返回旧备份到当前文件的顺序，便于保持日志时间顺序。"""
    if not include_rotated:
        return [log_path]

    backups = []
    for candidate in log_path.parent.glob(f"{log_path.name}.*"):
        suffix = candidate.name[len(log_path.name) + 1:]
        if suffix.isdigit():
            backups.append((int(suffix), candidate))
    backups.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in backups] + [log_path]


def read_log_lines(
    log_path: Union[str, Path],
    *,
    max_lines: int,
    search: Optional[str] = None,
    required_text: Optional[str] = None,
    include_rotated: bool = False,
) -> LogReadResult:
    """
    读取日志最后若干行。

    无筛选时反向读取尾部；有筛选时逐行扫描并仅保留最后 max_lines 条，
    避免把整份日志及过滤结果同时加载到内存。
    """
    path = Path(log_path)
    limit = max(1, int(max_lines))
    search_folded = str(search or "").casefold()
    required = str(required_text or "")
    log_files = _log_files_in_chronological_order(path, include_rotated)

    if not search_folded and not required:
        tail_lines = []
        for candidate in reversed(log_files):
            remaining = limit - len(tail_lines)
            if remaining <= 0:
                break
            try:
                candidate_lines = _read_tail_lines(candidate, remaining)
            except FileNotFoundError:
                continue
            tail_lines = candidate_lines + tail_lines
        return LogReadResult(
            lines=tail_lines,
            total_lines=None,
            filtered_count=None,
            counts_exact=False,
            strategy="tail",
        )

    recent = deque(maxlen=limit)
    total_lines = 0
    filtered_count = 0
    for candidate in log_files:
        try:
            stream = candidate.open("r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        with stream:
            for line in stream:
                total_lines += 1
                if required and required not in line:
                    continue
                if search_folded and search_folded not in line.casefold():
                    continue
                filtered_count += 1
                recent.append(line)

    return LogReadResult(
        lines=list(recent),
        total_lines=total_lines,
        filtered_count=filtered_count,
        counts_exact=True,
        strategy="stream_filter",
    )
