"""Resolve raw chat messages referenced by Assistant memory events."""

from __future__ import annotations

import json
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.chat_log import ChatLogManager
from app.assistant.memory_store import MemoryStore


LIVE_CHAT_LOG_NAMESPACE = "live_chat_log"


def _read_jsonl_range(
    path: Path,
    *,
    start_cursor: int,
    end_cursor: int,
    limit: int,
) -> List[Dict[str, Any]]:
    if not path.is_file() or end_cursor < start_cursor or limit <= 0:
        return []

    start = max(1, int(start_cursor))
    end = max(start, int(end_cursor))
    messages: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as source:
        for line_number, line in enumerate(
            islice(source, start - 1, end),
            start=start,
        ):
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(value, dict):
                continue
            message = dict(value)
            message["_log_cursor"] = int(
                message.get("memory_cursor") or line_number
            )
            messages.append(message)
            if len(messages) >= limit:
                break
    return messages


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def _read_live_time_range(
    manager: ChatLogManager,
    chat_name: str,
    *,
    start_time: Any,
    end_time: Any,
    limit: int,
) -> Optional[List[Dict[str, Any]]]:
    start = _parse_time(start_time)
    end = _parse_time(end_time)
    if start is None or end is None:
        return None
    if end < start:
        end = start
    log_path = manager.log_dir / f"{chat_name}.jsonl"
    if not log_path.is_file():
        return []
    messages: List[Dict[str, Any]] = []
    with log_path.open("r", encoding="utf-8", errors="replace") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            message_time = _parse_time(message.get("time"))
            if message_time is None or message_time < start:
                continue
            if message_time > end:
                continue
            if manager._is_internal_action_message(message):
                continue
            message["_log_cursor"] = line_number
            messages.append(message)
            if len(messages) >= limit:
                break
    return messages


def read_event_source(
    store: MemoryStore,
    event: Dict[str, Any],
    *,
    chat_log_manager: Optional[ChatLogManager] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Read an event's source range from its declared cursor namespace."""
    chat_name = str(event.get("chat_name") or "")
    source_start = max(1, int(event.get("source_start_cursor") or 1))
    source_end = max(
        source_start,
        int(event.get("source_end_cursor") or source_start),
    )
    safe_limit = max(1, min(200, int(limit)))
    namespace = str(
        event.get("source_namespace") or LIVE_CHAT_LOG_NAMESPACE
    ).strip()

    stored_messages = store.list_event_messages(
        int(event.get("id") or 0)
    )
    if stored_messages:
        return stored_messages[:safe_limit]

    if namespace == LIVE_CHAT_LOG_NAMESPACE:
        manager = chat_log_manager or ChatLogManager()
        time_messages = _read_live_time_range(
            manager,
            chat_name,
            start_time=event.get("start_time"),
            end_time=event.get("end_time"),
            limit=safe_limit,
        )
        if time_messages is not None:
            return time_messages
        return manager.get_messages_after_sequence(
            chat_name,
            after_sequence=source_start - 1,
            through_sequence=source_end,
            limit=safe_limit,
        )

    source = store.get_source(chat_name, namespace)
    if not source or source.get("source_type") != "jsonl_memory":
        return []
    source_path = Path(str(source.get("source_path") or ""))
    if not source_path.is_absolute():
        source_path = Path.cwd() / source_path
    return _read_jsonl_range(
        source_path.resolve(),
        start_cursor=source_start,
        end_cursor=source_end,
        limit=safe_limit,
    )
