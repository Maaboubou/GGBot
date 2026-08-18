"""
Dashboard structured event storage utilities.

This module provides a lightweight JSONL sink for dashboard-critical events
that should not depend on fragile log text parsing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.config_service import get_setting

logger = logging.getLogger(__name__)


def _get_events_file() -> Path:
    """Return dashboard events file path."""
    return Path(get_setting("DASHBOARD_EVENTS_FILE", "logs/dashboard_events.jsonl"))


def append_dashboard_event(event_type: str, payload: Dict[str, Any]) -> None:
    """
    Append one structured dashboard event as a JSONL line.

    Args:
        event_type: Event category, e.g. "judge_decision", "web_search"
        payload: Event data
    """
    try:
        events_file = _get_events_file()
        events_file.parent.mkdir(parents=True, exist_ok=True)

        event = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event_type": event_type,
            "payload": payload,
        }
        with open(events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"Failed to append dashboard event '{event_type}': {e}")


def get_latest_dashboard_event(event_type: str) -> Optional[Dict[str, Any]]:
    """
    Get latest structured event by type.

    Reads recent JSONL lines from bottom to top and returns the first match.
    """
    try:
        events_file = _get_events_file()
        if not events_file.exists():
            return None

        with open(events_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        for line in reversed(lines[-5000:]):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            if item.get("event_type") == event_type:
                return item
        return None
    except Exception as e:
        logger.warning(f"Failed to read latest dashboard event '{event_type}': {e}")
        return None


def get_recent_dashboard_events(event_type: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Get recent structured events by type, newest first.

    Args:
        event_type: Event category to read.
        limit: Maximum number of events to return.
    """
    try:
        events_file = _get_events_file()
        if not events_file.exists():
            return []

        with open(events_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        events: List[Dict[str, Any]] = []
        for line in reversed(lines[-5000:]):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            if item.get("event_type") != event_type:
                continue

            events.append(item)
            if len(events) >= limit:
                break

        return events
    except Exception as e:
        logger.warning(f"Failed to read recent dashboard events '{event_type}': {e}")
        return []
