"""Deterministic completion contract for user-facing Assistant replies.

This module intentionally contains no model calls. It validates only facts the
host can know reliably: the terminal status/shape, non-empty user-visible
messages, protocol encapsulation, and whether the final text promises work that
has not been completed in the current turn.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple


TERMINAL_REPLY_STATUSES = ("answered", "not_found", "blocked")


@dataclass(frozen=True)
class ReplyCompletionValidation:
    valid: bool
    code: str = ""
    status: str = ""
    messages: Tuple[str, ...] = ()


_FENCED_JSON_RE = re.compile(
    r"\A\s*```(?:json)?\s*(.*?)\s*```\s*\Z",
    flags=re.IGNORECASE | re.DOTALL,
)

# These patterns are deliberately about deferred first-person work or a later
# reply, rather than generic words such as "搜索". A completed answer may quite
# reasonably describe a search it already performed.
_DEFERRED_WORK_PATTERNS = (
    re.compile(
        r"(?:^|[，。！？!?；;])\s*"
        r"(?:继续|接着|再去|马上去|现在就去|这就去)\s*"
        r"(?:搜|搜索|查|查询|找|翻|检索|调查|研究|处理|核实)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?:我|俺|我们|我这边|这边)\s*"
        r"(?:会|要|得|准备|打算|马上|现在就|这就|再|继续|接着|还要|还得)+\s*"
        r"(?:去|来)?\s*(?:搜|搜索|查|查询|找|翻|检索|调查|研究|处理|核实)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?:我|俺|我们|我这边|这边)?\s*(?:正在|还在)\s*"
        r"(?:搜|搜索|查|查询|找|翻|检索|调查|研究|处理|核实)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?:等|待)\s*(?:我|我们)?\s*"
        r"(?:搜|搜索|查|查询|找|翻|检索|调查|研究|处理|核实)"
        r".{0,16}?(?:再|就)?\s*(?:回复|告诉|通知|更新|发给)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?:搜|搜索|查|查询|找|翻|检索|调查|研究|处理|核实)"
        r".{0,12}?(?:到|完|好|结束)(?:后|了)?"
        r".{0,10}?(?:再|就)?\s*(?:回复|告诉|通知|更新|发给)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?:稍后|晚点|一会儿|过会儿|回头)"
        r".{0,12}?(?:回复|告诉|通知|更新|发给)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i(?:'ll|\s+will|\s+am\s+going\s+to)|i\s+need\s+to|let\s+me)\s+"
        r"(?:(?:keep|continue|go\s+and|now)\s+)?"
        r"(?:search(?:ing)?|look(?:ing)?|check(?:ing)?|investigat(?:e|ing)|"
        r"research(?:ing)?|work(?:ing)?|process(?:ing)?|verif(?:y|ying))\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i(?:'m|\s+am)\s+)?(?:still|currently)\s+"
        r"(?:searching|looking|checking|investigating|researching|working|processing|verifying)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i(?:'ll|\s+will)\s+)?"
        r"(?:get\s+back\s+to\s+you|update\s+you|reply|respond)"
        r"(?:\s+\w+){0,3}\s+(?:later|soon|afterwards)\b",
        flags=re.IGNORECASE,
    ),
)

_NEGATED_DEFERRED_WORK_RE = re.compile(
    r"(?:不(?:会|用|必|需要|打算)?|无需|没必要|别)\s*"
    r"(?:再|继续|接着)?\s*"
    r"(?:搜|搜索|查|查询|找|翻|检索|调查|研究|处理|核实)",
    flags=re.IGNORECASE,
)


def terminal_reply_output_schema(max_messages: int = 3) -> Dict[str, Any]:
    """Return the stable JSON schema for a terminal WeChat reply."""
    bounded_max = max(1, int(max_messages or 1))
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": list(TERMINAL_REPLY_STATUSES),
            },
            "messages": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": bounded_max,
            },
        },
        "required": ["status", "messages"],
        "additionalProperties": False,
    }


def _strip_outer_json_fence(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    match = _FENCED_JSON_RE.fullmatch(text)
    return match.group(1).strip() if match else text


def is_serialized_reply_protocol(value: Any) -> bool:
    """Return whether a message string is another encoded reply envelope.

    This closes the escape hatch where a model satisfies ``messages: [string]``
    by placing a second JSON protocol object inside that string.
    """
    text = str(value or "").strip()
    for _ in range(3):
        if not text or text[0] not in '{["':
            return False
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if isinstance(parsed, str):
            text = parsed.strip()
            continue
        if isinstance(parsed, dict):
            keys = {str(key).strip().lower() for key in parsed}
            return "messages" in keys or (
                "status" in keys and bool(keys & {"message", "content", "text"})
            )
        return False
    return False


def contains_deferred_work_promise(text: str) -> bool:
    """Detect an assistant final that promises work or a reply in the future."""
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return False
    # Remove explicit negations such as "我不会继续搜索" before applying the
    # positive patterns, so a truthful terminal statement is not rejected.
    normalized = _NEGATED_DEFERRED_WORK_RE.sub("", normalized)
    return any(pattern.search(normalized) for pattern in _DEFERRED_WORK_PATTERNS)


def _clean_message_items(values: Sequence[Any]) -> Optional[Tuple[str, ...]]:
    cleaned = []
    for value in values:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        cleaned.append(text)
    return tuple(cleaned) if cleaned else None


def validate_structured_terminal_reply(
    raw_text: str,
    *,
    max_messages: int = 0,
) -> ReplyCompletionValidation:
    """Validate a strict structured terminal reply without semantic model review."""
    text = _strip_outer_json_fence(raw_text)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ReplyCompletionValidation(False, "invalid_json")

    if not isinstance(parsed, dict) or set(parsed) != {"status", "messages"}:
        return ReplyCompletionValidation(False, "invalid_shape")

    status = str(parsed.get("status") or "").strip().lower()
    if status not in TERMINAL_REPLY_STATUSES:
        return ReplyCompletionValidation(False, "non_terminal_status", status=status)

    raw_messages = parsed.get("messages")
    if not isinstance(raw_messages, list):
        return ReplyCompletionValidation(False, "invalid_messages", status=status)
    messages = _clean_message_items(raw_messages)
    if not messages:
        return ReplyCompletionValidation(False, "empty_messages", status=status)
    if max_messages > 0 and len(messages) > int(max_messages):
        return ReplyCompletionValidation(
            False,
            "too_many_messages",
            status=status,
            messages=messages,
        )
    if any(is_serialized_reply_protocol(message) for message in messages):
        return ReplyCompletionValidation(
            False,
            "nested_reply_protocol",
            status=status,
            messages=messages,
        )
    if contains_deferred_work_promise("\n".join(messages)):
        return ReplyCompletionValidation(
            False,
            "deferred_work_promise",
            status=status,
            messages=messages,
        )
    return ReplyCompletionValidation(True, status=status, messages=messages)


def validate_plain_terminal_reply(raw_text: str) -> ReplyCompletionValidation:
    """Apply the provider-neutral hard checks to a non-JSON role reply."""
    text = str(raw_text or "").strip()
    if not text:
        return ReplyCompletionValidation(False, "empty_messages")
    if is_serialized_reply_protocol(text):
        return ReplyCompletionValidation(False, "nested_reply_protocol")
    if contains_deferred_work_promise(text):
        return ReplyCompletionValidation(False, "deferred_work_promise")
    return ReplyCompletionValidation(True, status="answered", messages=(text,))
