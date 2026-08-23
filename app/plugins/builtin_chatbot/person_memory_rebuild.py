"""Historical rebuild and atomic activation for person memory.

The pipeline deliberately runs in an isolated SQLite candidate:

1. raw messages -> immutable person observations;
2. observations -> half-year evidence summaries;
3. summaries + recent observations -> facts/patterns/relationships/snapshot;
4. validation report;
5. explicit activation with a full production backup.

DeepSeek token spending is guarded in CNY.  Completed extraction/period files
are reusable, so an interrupted run resumes without paying for finished calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import litellm

from app.plugins.builtin_chatbot.context_manager import ChatContextManager
from app.plugins.builtin_chatbot.memory_service import ChatMemoryService
from app.plugins.builtin_chatbot.memory_store import MemoryStore
from app.plugins.builtin_chatbot.person_memory import (
    PERSON_MEMORY_SCHEMA_VERSION,
    PersonMemoryEngine,
    PersonMemoryStore,
    _clean_text,
    _json_dump,
    _json_load,
    _parse_time,
    _safe_int,
)
from app.services.llm_manager import LLMManager


PERSON_TABLES = (
    "memory_person_observations",
    "memory_person_fact_versions",
    "memory_person_patterns",
    "memory_person_relationships",
    "memory_person_period_summaries",
    "memory_person_snapshots",
    "memory_person_refresh_state",
    "memory_person_projection_audit",
    "memory_person_suppressions",
    "memory_person_source_messages",
    "memory_person_message_links",
    "memory_person_pipeline_state",
    "memory_person_claim_candidates",
    "memory_person_state",
)
PERSON_AUTOINCREMENT_TABLES = (
    "memory_person_observations",
    "memory_person_fact_versions",
    "memory_person_patterns",
    "memory_person_relationships",
    "memory_person_period_summaries",
    "memory_person_snapshots",
    "memory_person_projection_audit",
    "memory_person_suppressions",
    "memory_person_source_messages",
    "memory_person_message_links",
    "memory_person_claim_candidates",
)


class _NoChatLog:
    def count_messages(self, _chat_name: str) -> int:
        return 0

    def count_log_messages(self, _chat_name: str) -> int:
        return 0


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source, timeout=60)
    target_connection = sqlite3.connect(destination, timeout=60)
    try:
        source_connection.execute("PRAGMA busy_timeout=60000")
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


@dataclass
class CostReservation:
    reserved_cny: float
    released: bool = False


class DeepSeekBudget:
    """Thread-safe token/cost ledger using the user's v4-flash prices."""

    def __init__(
        self,
        path: Path,
        *,
        limit_cny: float,
        cache_hit_input_per_million: float = 0.02,
        cache_miss_input_per_million: float = 1.0,
        output_per_million: float = 2.0,
    ) -> None:
        self.path = path
        self.limit_cny = max(0.01, float(limit_cny))
        self.cache_hit_rate = float(cache_hit_input_per_million)
        self.cache_miss_rate = float(cache_miss_input_per_million)
        self.output_rate = float(output_per_million)
        self.lock = threading.Lock()
        self.actual_cny = 0.0
        self.reserved_cny = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 0
        self.calls = 0
        self.successful_calls = 0
        self.failed_calls = 0
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                self.actual_cny = float(existing.get("actual_cny") or 0.0)
                self.prompt_tokens = int(existing.get("prompt_tokens") or 0)
                self.completion_tokens = int(
                    existing.get("completion_tokens") or 0
                )
                self.cache_hit_tokens = int(
                    existing.get("cache_hit_tokens") or 0
                )
                self.cache_miss_tokens = int(
                    existing.get("cache_miss_tokens") or 0
                )
                self.calls = int(existing.get("calls") or 0)
                self.successful_calls = int(
                    existing.get("successful_calls") or 0
                )
                self.failed_calls = int(existing.get("failed_calls") or 0)
            except Exception as exc:
                raise RuntimeError(
                    "DeepSeek cost ledger is unreadable; refusing to reset "
                    f"spent cost to zero: {path}"
                ) from exc

    def estimate_cny(
        self,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        # Reservations assume a cache miss, which is the more expensive input
        # path.  Actual accounting later credits cache hits.
        return (
            max(0, int(input_tokens)) / 1_000_000 * self.cache_miss_rate
            + max(0, int(output_tokens)) / 1_000_000 * self.output_rate
        )

    def reserve(
        self,
        *,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
    ) -> CostReservation:
        amount = self.estimate_cny(
            estimated_input_tokens,
            estimated_output_tokens,
        )
        with self.lock:
            if self.actual_cny + self.reserved_cny + amount > self.limit_cny:
                raise RuntimeError(
                    "DeepSeek budget would be exceeded: "
                    f"actual={self.actual_cny:.4f} "
                    f"reserved={self.reserved_cny:.4f} "
                    f"request={amount:.4f} limit={self.limit_cny:.2f}"
                )
            self.reserved_cny += amount
        return CostReservation(amount)

    def release(self, reservation: CostReservation) -> None:
        with self.lock:
            if reservation.released:
                return
            self.reserved_cny = max(
                0.0,
                self.reserved_cny - reservation.reserved_cny,
            )
            reservation.released = True
            self._save_locked()

    def record_usage(self, usage: Dict[str, Any]) -> None:
        token_usage = (
            usage.get("token_usage")
            if isinstance(usage.get("token_usage"), dict)
            else {}
        )
        prompt = int(
            token_usage.get("prompt_tokens")
            or token_usage.get("input_tokens")
            or 0
        )
        completion = int(
            token_usage.get("completion_tokens")
            or token_usage.get("output_tokens")
            or 0
        )
        miss = int(
            token_usage.get("cache_miss_tokens")
            or token_usage.get("cache_creation_input_tokens")
            or prompt
        )
        hit = int(
            token_usage.get("cache_hit_tokens")
            or token_usage.get("cache_read_input_tokens")
            or max(0, prompt - miss)
        )
        miss = max(0, min(prompt, miss))
        hit = max(0, min(prompt - miss, hit))
        cost = (
            hit / 1_000_000 * self.cache_hit_rate
            + miss / 1_000_000 * self.cache_miss_rate
            + completion / 1_000_000 * self.output_rate
        )
        with self.lock:
            self.actual_cny += cost
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.cache_hit_tokens += hit
            self.cache_miss_tokens += miss
            self.calls += 1
            if usage.get("success", True):
                self.successful_calls += 1
            else:
                self.failed_calls += 1
            self._save_locked()

    def _payload_locked(self) -> Dict[str, Any]:
        return {
            "limit_cny": self.limit_cny,
            "actual_cny": round(self.actual_cny, 6),
            "reserved_cny": round(self.reserved_cny, 6),
            "remaining_cny": round(
                max(0.0, self.limit_cny - self.actual_cny - self.reserved_cny),
                6,
            ),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "calls": self.calls,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "prices_cny_per_million": {
                "cache_hit_input": self.cache_hit_rate,
                "cache_miss_input": self.cache_miss_rate,
                "output": self.output_rate,
            },
            "updated_at": _now(),
        }

    def _save_locked(self) -> None:
        _write_json(self.path, self._payload_locked())

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self._payload_locked())


def _clear_candidate_person_memory(store: MemoryStore, chat_name: str) -> None:
    with store._connection() as connection:
        for table in PERSON_TABLES:
            connection.execute(
                f"DELETE FROM {table} WHERE chat_name = ?",
                (chat_name,),
            )


def _clear_candidate_derived(store: MemoryStore, chat_name: str) -> None:
    with store._connection() as connection:
        for table in (
            "memory_person_fact_versions",
            "memory_person_patterns",
            "memory_person_relationships",
            "memory_person_snapshots",
            "memory_person_refresh_state",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE chat_name = ?",
                (chat_name,),
            )


def _reject_bot_prompt_observations(
    person_store: PersonMemoryStore,
    chat_name: str,
    excluded_sender_names: Optional[Iterable[str]],
) -> int:
    """Reject durable facts extracted from messages that prompt the bot.

    In long-running groups, users routinely invent relatives, jobs and assets
    while asking the bot to continue a joke. The raw message remains indexed,
    but a single bot-directed prompt is not accepted as identity evidence.
    """

    names = {
        str(value or "").strip()
        for value in (excluded_sender_names or [])
        if str(value or "").strip()
    }
    if not names:
        return 0
    high_impact_fields = {
        "identity",
        "group_role",
        "occupation",
        "employer",
        "education",
        "location",
        "family",
        "relationship",
        "health",
        "asset",
        "experience",
        "plan",
        "current_status",
    }
    rejected = 0
    for observation in person_store.list_observations(
        chat_name,
        quality_status="active",
        limit=100000,
    ):
        if str(observation.get("field_name") or "") not in high_impact_fields:
            continue
        excerpts = [
            item
            for item in observation.get("evidence_excerpt") or []
            if isinstance(item, dict)
        ]
        if not any(
            any(
                re.search(
                    rf"@\s*{re.escape(name)}"
                    rf"(?:\s|$|[，。！？；：、,.!?])",
                    str(item.get("content") or ""),
                    flags=re.IGNORECASE,
                )
                for name in names
            )
            for item in excerpts
        ):
            continue
        person_store.review_observation(
            chat_name,
            int(observation["id"]),
            quality_status="rejected",
            reason="自动复核：向机器人发出的设定或提问不作为高影响人物事实",
        )
        rejected += 1
    return rejected


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    messages = []
    with path.open("r", encoding="utf-8", errors="replace") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            content = str(raw.get("content") or "").replace("\x00", "")
            if not content.strip():
                continue
            value = dict(raw)
            value["_log_cursor"] = int(
                raw.get("memory_cursor") or line_number
            )
            # Very large forwarded articles harm both extraction accuracy and
            # batch economics; the beginning retains sender intent and entities.
            value["content"] = content[:2200]
            messages.append(value)
    return messages


def _message_time(message: Dict[str, Any]) -> Optional[datetime]:
    return _parse_time(message.get("time"))


def _source_inputs(
    historical_path: Path,
    *,
    live_path: Optional[Path],
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    history = _read_jsonl(historical_path)
    result: List[Tuple[str, List[Dict[str, Any]]]] = [
        ("historical_jsonl", history)
    ]
    if live_path is None or not live_path.is_file():
        return result
    last_history_time = max(
        (
            parsed
            for parsed in (_message_time(message) for message in history)
            if parsed is not None
        ),
        default=None,
    )
    live = _read_jsonl(live_path)
    if last_history_time is not None:
        live = [
            message
            for message in live
            if (_message_time(message) or datetime.min) > last_history_time
        ]
    if live:
        result.append(("live_chat_log_catchup", live))
    return result


def _live_message_key(message: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        _clean_text(message.get("time"), 60),
        _clean_text(message.get("sender"), 120),
        str(message.get("content") or "").replace("\x00", "")[:12000],
    )


def _live_tail_after_anchor(
    live_messages: Sequence[Dict[str, Any]],
    anchor: Dict[str, Any],
    *,
    context_messages: int = 16,
) -> Dict[str, Any]:
    """Select an idempotent live tail after the last indexed raw message.

    Physical JSONL line numbers can move when the chat logger compacts its
    file. The activation boundary therefore uses the exact time/sender/text
    tuple and assigns a stable, activation-local cursor sequence.
    """

    ordered = [dict(message) for message in live_messages]
    anchor_key = _live_message_key(anchor)
    matches = [
        index
        for index, message in enumerate(ordered)
        if _live_message_key(message) == anchor_key
    ]
    if matches:
        anchor_index = matches[-1]
    else:
        anchor_time = _message_time(anchor)
        first_live_time = _message_time(ordered[0]) if ordered else None
        if (
            anchor_time is not None
            and first_live_time is not None
            and first_live_time > anchor_time
        ):
            # The logger has already compacted away the anchor. Every
            # remaining physical row is newer and is therefore safe to index.
            anchor_index = -1
        else:
            raise RuntimeError(
                "could not align the latest indexed person-memory message "
                "with the current live log"
            )

    tail_start = anchor_index + 1
    tail = ordered[tail_start:]
    context_start = max(0, tail_start - max(0, int(context_messages)))
    selected = ordered[context_start:]
    context_count = tail_start - context_start
    rebased = []
    for cursor, message in enumerate(selected, start=1):
        value = dict(message)
        value["_log_cursor"] = cursor
        rebased.append(value)
    core_cursors = list(
        range(context_count + 1, context_count + len(tail) + 1)
    )
    namespace_material = json.dumps(
        {
            "time": anchor_key[0],
            "sender": anchor_key[1],
            "content": anchor_key[2],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    namespace = (
        "live_activation_catchup:"
        + hashlib.sha256(namespace_material.encode("utf-8")).hexdigest()[:16]
    )
    return {
        "namespace": namespace,
        "anchor_index": anchor_index,
        "anchor": {
            "time": anchor_key[0],
            "sender": anchor_key[1],
            "content": anchor_key[2],
        },
        "selected": rebased,
        "core_cursors": core_cursors,
        "tail_count": len(tail),
        "context_count": context_count,
    }


def _observe_identities(
    store: MemoryStore,
    chat_name: str,
    sources: Sequence[Tuple[str, Sequence[Dict[str, Any]]]],
    *,
    excluded_sender_names: Optional[Iterable[str]] = None,
    excluded_sender_ids: Optional[Iterable[str]] = None,
) -> int:
    excluded_names = {
        str(value or "").strip().casefold()
        for value in (excluded_sender_names or [])
        if str(value or "").strip()
    }
    excluded_ids = {
        str(value or "").strip()
        for value in (excluded_sender_ids or [])
        if str(value or "").strip()
    }
    count = 0
    for namespace, messages in sources:
        human_messages = [
            message
            for message in messages
            if str(message.get("sender") or "").strip().casefold()
            not in excluded_names
            and str(message.get("sender_id") or "").strip()
            not in excluded_ids
        ]
        for start in range(0, len(human_messages), 2000):
            count += store.observe_message_identities(
                chat_name,
                human_messages[start : start + 2000],
                source=(
                    "historical_sender_id"
                    if namespace == "historical_jsonl"
                    else "live_message"
                ),
            )
    return count


def _normalize_twin_content(value: Any) -> str:
    return re.sub(
        r"\s+",
        " ",
        str(value or "").replace("\u2005", " ").strip(),
    )


def _strong_twin_content(value: Any) -> bool:
    text = _normalize_twin_content(value)
    if len(text) < 4:
        return False
    return text not in {
        "[图片]",
        "[视频]",
        "[动画表情]",
        "[语音]",
        "[文件]",
        "[位置]",
    }


def _infer_sender_ids_from_cross_source_twins(
    sources: Sequence[Tuple[str, Sequence[Dict[str, Any]]]],
    *,
    maximum_time_delta_seconds: float = 10.0,
    minimum_matches: int = 3,
    minimum_strong_contents: int = 2,
) -> Dict[str, Any]:
    """Attach stable sender IDs to duplicate rows from another source.

    CipherTalk history may expose an account name and stable wxid while the
    live wxauto log records the same message under a group nickname.  Exact
    cross-source message twins provide a deterministic bridge.  A single
    generic row is never enough: inference requires several timestamp-aligned
    duplicates and at least two distinct non-placeholder contents.
    """

    stable_by_content: Dict[
        str,
        List[Tuple[str, datetime, str]],
    ] = defaultdict(list)
    for namespace, messages in sources:
        for message in messages:
            sender_id = str(message.get("sender_id") or "").strip()
            parsed = _parse_time(message.get("time"))
            content = _normalize_twin_content(message.get("content"))
            if (
                not sender_id
                or sender_id.casefold().endswith("@chatroom")
                or parsed is None
                or not content
            ):
                continue
            stable_by_content[content].append(
                (str(namespace), parsed, sender_id)
            )

    votes: Dict[
        Tuple[str, str],
        Dict[str, List[Tuple[str, str]]],
    ] = defaultdict(lambda: defaultdict(list))
    display_names: Dict[Tuple[str, str], str] = {}
    for namespace, messages in sources:
        normalized_namespace = str(namespace)
        for message in messages:
            if str(message.get("sender_id") or "").strip():
                continue
            sender_name = str(message.get("sender") or "").strip()
            parsed = _parse_time(message.get("time"))
            content = _normalize_twin_content(message.get("content"))
            if not sender_name or parsed is None or not content:
                continue
            candidates = {
                sender_id
                for source_namespace, source_time, sender_id
                in stable_by_content.get(content, [])
                if source_namespace != normalized_namespace
                and abs((source_time - parsed).total_seconds())
                <= max(0.0, float(maximum_time_delta_seconds))
            }
            if len(candidates) != 1:
                continue
            key = (normalized_namespace, sender_name.casefold())
            display_names[key] = sender_name
            votes[key][next(iter(candidates))].append(
                (str(message.get("time") or ""), content)
            )

    inferred: Dict[Tuple[str, str], str] = {}
    evidence = []
    for key, candidates in votes.items():
        ranked = sorted(
            candidates.items(),
            key=lambda item: len(item[1]),
            reverse=True,
        )
        sender_id, matches = ranked[0]
        runner_count = len(ranked[1][1]) if len(ranked) > 1 else 0
        total = sum(len(items) for items in candidates.values())
        distinct_strong = {
            content
            for _message_time, content in matches
            if _strong_twin_content(content)
        }
        if (
            len(matches) < max(1, int(minimum_matches))
            or len(distinct_strong)
            < max(1, int(minimum_strong_contents))
            or len(matches) < runner_count + 2
            or len(matches) / max(1, total) < 0.9
        ):
            continue
        inferred[key] = sender_id
        evidence.append(
            {
                "source_namespace": key[0],
                "sender_name": display_names.get(key, key[1]),
                "sender_id": sender_id,
                "matched_messages": len(matches),
                "distinct_strong_contents": len(distinct_strong),
                "maximum_time_delta_seconds": float(
                    maximum_time_delta_seconds
                ),
            }
        )

    assigned_messages = 0
    for namespace, messages in sources:
        normalized_namespace = str(namespace)
        for message in messages:
            if str(message.get("sender_id") or "").strip():
                continue
            sender_name = str(message.get("sender") or "").strip()
            sender_id = inferred.get(
                (normalized_namespace, sender_name.casefold())
            )
            if not sender_id:
                continue
            message["sender_id"] = sender_id
            message["_sender_id_source"] = "cross_source_message_twin"
            assigned_messages += 1
    return {
        "inferred_alias_count": len(inferred),
        "assigned_message_count": assigned_messages,
        "aliases": sorted(
            evidence,
            key=lambda item: (
                str(item["source_namespace"]),
                str(item["sender_name"]).casefold(),
            ),
        ),
    }



def _period_key(observation: Dict[str, Any]) -> str:
    parsed = _parse_time(
        observation.get("observed_at")
        or observation.get("valid_from")
    )
    if parsed is None:
        return "unknown"
    half = 1 if parsed.month <= 6 else 2
    return f"{parsed.year:04d}-H{half}"


def _period_prompt(
    person_name: str,
    period_key: str,
    observations: Sequence[Dict[str, Any]],
) -> List[Dict[str, str]]:
    material = []
    for observation in observations:
        material.append(
            {
                "id": int(observation.get("id") or 0),
                "type": observation.get("observation_type"),
                "field": observation.get("field_name"),
                "statement": observation.get("statement"),
                "source_relation": observation.get("source_relation"),
                "epistemic_status": observation.get("epistemic_status"),
                "confidence": observation.get("confidence"),
                "valid_from": observation.get("valid_from"),
                "valid_to": observation.get("valid_to"),
                "observed_at": observation.get("observed_at"),
                "sensitivity": observation.get("sensitivity"),
            }
        )
    return [
        {
            "role": "system",
            "content": (
                "你是人物长期记忆的分期证据整理员。只压缩给定 observation，"
                "不得新增信息；每项必须引用 observation ID。保留这段时间真正有长期"
                "意义的身份/职业/家庭/地点/技能/兴趣变化、重要经历、明确计划和群内"
                "角色证据。删除普通互动、一次性观点、下注流水、链接转发和重复说法。"
                "这是中间证据层，不要下跨多年性格结论；可输出 pattern_signals，但稳定"
                "与否由最终聚合器跨时期判断。严格控制输出长度：candidate_facts最多"
                "12条、timeline最多10条、pattern_signals最多6条、"
                "relationship_signals最多6条。只输出JSON对象。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"人物：{person_name}\n时期：{period_key}\n"
                f"观察：{_json_dump(material)}\n\n"
                "严格输出："
                '{"summary":"这一时期不超过300字的高价值变化概括",'
                '"candidate_facts":[{"field":"固定字段","value":"原子事实",'
                '"status":"current|historical|planned|uncertain|disputed",'
                '"valid_from":"","valid_to":"","evidence_observation_ids":[1],'
                '"sensitivity":"low|medium|high"}],'
                '"timeline":[{"text":"重要经历或变化",'
                '"evidence_observation_ids":[1]}],'
                '"pattern_signals":[{"type":"interest|preference|habit|skill|'
                'group_role|communication_style|trait","description":"仅作信号",'
                '"evidence_observation_ids":[1]}],'
                '"relationship_signals":[{"target_name":"对象",'
                '"description":"关系证据","evidence_observation_ids":[1]}]}'
            ),
        },
    ]


def _validate_period_payload(
    payload: Dict[str, Any],
    allowed_ids: set[int],
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "summary": _clean_text(payload.get("summary"), 1600),
        "candidate_facts": [],
        "timeline": [],
        "pattern_signals": [],
        "relationship_signals": [],
    }
    limits = {
        "candidate_facts": 12,
        "timeline": 10,
        "pattern_signals": 6,
        "relationship_signals": 6,
    }
    for key, limit in limits.items():
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for raw in values[:limit]:
            if not isinstance(raw, dict):
                continue
            ids = sorted(
                {
                    _safe_int(value)
                    for value in raw.get("evidence_observation_ids") or []
                    if _safe_int(value) in allowed_ids
                }
            )
            if not ids:
                continue
            item = dict(raw)
            item["evidence_observation_ids"] = ids
            result[key].append(item)
    return result


def _configure_deepseek_manager(
    workspace: Path,
) -> LLMManager:
    # Historical rebuilds issue many threaded synchronous requests. LiteLLM's
    # shared aiohttp transport is bound to asyncio event loops and can leave a
    # request pending past its configured timeout when those loops are
    # repeatedly created and destroyed. The HTTPX transport is safe for this
    # workload and matches the event-memory backfill runner.
    litellm.disable_aiohttp_transport = True
    manager = LLMManager(
        config_dir="data",
        telemetry_dir=workspace / "telemetry",
    )
    models = manager.config.get("models", {})
    if "deepseek" not in models:
        raise RuntimeError("LLM Manager has no 'deepseek' model")
    chatbot = manager.config.setdefault("plugin_mappings", {}).setdefault(
        "builtin_chatbot",
        {},
    )
    for call_type in ("memory_generate", "memory_review", "memory_synthesize"):
        mapping = chatbot.setdefault(call_type, {})
        mapping["primary"] = "deepseek"
        mapping["fallback"] = []
        mapping["override_params"] = {}
    return manager


def _candidate_report(
    store: MemoryStore,
    chat_name: str,
    *,
    cost: Dict[str, Any],
    source_counts: Dict[str, int],
    extraction_batches: int,
    period_summaries: int,
    consolidation_results: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    person_store = PersonMemoryStore(store)
    stats = person_store.observation_stats(chat_name)
    profiles = person_store.list_profiles(chat_name)
    with store._connection() as connection:
        confirmed_pattern_violations = int(
            connection.execute(
                """
                SELECT COUNT(*) AS value FROM memory_person_patterns
                WHERE chat_name = ? AND state = 'confirmed'
                  AND (
                    independent_day_count < 3
                    OR evidence_span_days < 30
                  )
                """,
                (chat_name,),
            ).fetchone()["value"]
        )
        unsupported_snapshot_items = 0
        rows = connection.execute(
            """
            SELECT sections_json FROM memory_person_snapshots
            WHERE chat_name = ? AND is_active = 1
            """,
            (chat_name,),
        ).fetchall()
        valid_observation_ids = {
            int(row["id"])
            for row in connection.execute(
                """
                SELECT id FROM memory_person_observations
                WHERE chat_name = ? AND quality_status = 'active'
                """,
                (chat_name,),
            ).fetchall()
        }
        for row in rows:
            sections = _json_load(row["sections_json"], {})
            for items in sections.values():
                for item in items or []:
                    ids = {
                        int(value)
                        for value in item.get("evidence_observation_ids") or []
                    }
                    if not ids or not ids.issubset(valid_observation_ids):
                        unsupported_snapshot_items += 1
        counts = {
            table: int(
                connection.execute(
                    f"SELECT COUNT(*) AS value FROM {table} WHERE chat_name = ?",
                    (chat_name,),
                ).fetchone()["value"]
            )
            for table in PERSON_TABLES
        }
    return {
        "status": "candidate_ready",
        "chat_name": chat_name,
        "schema_version": PERSON_MEMORY_SCHEMA_VERSION,
        "source_message_counts": source_counts,
        "extraction_batches": extraction_batches,
        "period_summary_count": period_summaries,
        "observation_stats": stats,
        "profile_count": len(profiles),
        "snapshot_count": sum(bool(item.get("snapshot_id")) for item in profiles),
        "profile_overview": [
            {
                "person_id": int(profile.get("person_id") or 0),
                "person_name": profile.get("person_name"),
                "observation_count": int(
                    profile.get("observation_count") or 0
                ),
                "fact_count": len(profile.get("facts") or []),
                "confirmed_pattern_count": sum(
                    1
                    for item in profile.get("patterns") or []
                    if item.get("state") == "confirmed"
                ),
                "candidate_pattern_count": sum(
                    1
                    for item in profile.get("patterns") or []
                    if item.get("state") == "candidate"
                ),
                "relationship_count": len(
                    profile.get("relationships") or []
                ),
                "snapshot_generation": int(
                    profile.get("generation") or 0
                ),
            }
            for profile in profiles
        ],
        "consolidation_results": list(consolidation_results),
        "integrity": {
            "confirmed_pattern_threshold_violations":
                confirmed_pattern_violations,
            "unsupported_snapshot_items": unsupported_snapshot_items,
        },
        "table_counts": counts,
        "cost": cost,
        "completed_at": _now(),
    }


def rebuild_candidate(
    *,
    production_database: Path,
    chat_name: str,
    workspace: Path,
    historical_source: Path,
    live_source: Optional[Path] = None,
    concurrency: int = 48,
    target_messages: int = 120,
    overlap: int = 16,
    input_token_budget: int = 24000,
    budget_cny: float = 200.0,
    fresh: bool = False,
    rebuild_derived: bool = False,
    max_observations_per_batch: int = 16,
    candidate_memory_value: float = 0.58,
    only_people: Optional[Iterable[str]] = None,
    excluded_sender_names: Optional[Iterable[str]] = None,
    excluded_sender_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    candidate_path = workspace / "candidate.db"
    if fresh:
        for path in (
            candidate_path,
            workspace / "cost.json",
            workspace / "report.json",
        ):
            if path.is_file():
                path.unlink()
        for directory in (
            workspace / "observation_batches",
            workspace / "period_summaries",
            workspace / "consolidation",
            workspace / "telemetry",
        ):
            if directory.is_dir():
                shutil.rmtree(directory)
    elif rebuild_derived:
        consolidation_directory = workspace / "consolidation"
        if consolidation_directory.is_dir():
            shutil.rmtree(consolidation_directory)
    if not candidate_path.exists():
        _backup_database(production_database, candidate_path)
    store = MemoryStore(candidate_path)
    person_store = PersonMemoryStore(store)
    if fresh:
        _clear_candidate_person_memory(store, chat_name)
    elif rebuild_derived:
        _clear_candidate_derived(store, chat_name)
    person_store.ensure_chat_state(
        chat_name,
        mode="building",
        source_namespace="historical_jsonl",
    )
    sources = _source_inputs(
        historical_source,
        live_path=live_source,
    )
    sender_id_twin_inference = (
        _infer_sender_ids_from_cross_source_twins(sources)
    )
    observed_identity_messages = _observe_identities(
        store,
        chat_name,
        sources,
        excluded_sender_names=excluded_sender_names,
        excluded_sender_ids=excluded_sender_ids,
    )
    only_person_values = sorted(
        {
            str(value or "").strip()
            for value in (only_people or [])
            if str(value or "").strip()
        }
    )
    selected_person_ids: Optional[set[int]] = None
    selected_person_names: List[str] = []
    if only_person_values:
        selectors = {value.casefold() for value in only_person_values}
        selected_person_ids = set()
        for person in store.list_person_directory(chat_name):
            identity_values = {
                str(person.get("canonical_name") or "").strip().casefold(),
                str(person.get("person_name") or "").strip().casefold(),
            }
            for alias in person.get("aliases") or []:
                identity_values.add(
                    str(alias.get("alias_name") or "").strip().casefold()
                )
                identity_values.add(
                    str(alias.get("external_id") or "").strip().casefold()
                )
            if selectors & {value for value in identity_values if value}:
                selected_person_ids.add(int(person["person_id"]))
                selected_person_names.append(
                    str(person.get("canonical_name") or "")
                )
        if not selected_person_ids:
            raise ValueError(
                "none of --only-person matched an active identity: "
                + ", ".join(only_person_values)
            )
    context_manager = ChatContextManager()
    for namespace, messages in sources:
        for start in range(0, len(messages), 2000):
            chunk = messages[start : start + 2000]
            person_store.index_person_messages(
                chat_name,
                chunk,
                source_namespace=namespace,
                core_cursors=[
                    int(message.get("_log_cursor") or 0)
                    for message in chunk
                ],
                excluded_sender_names=excluded_sender_names,
                excluded_sender_ids=excluded_sender_ids,
                included_person_ids=selected_person_ids,
            )
    source_counts = {
        namespace: len(messages) for namespace, messages in sources
    }
    budget = DeepSeekBudget(
        workspace / "cost.json",
        limit_cny=budget_cny,
    )
    manager = _configure_deepseek_manager(workspace)
    service = ChatMemoryService(
        _NoChatLog(),
        context_manager,
        store=store,
        llm_manager=manager,
        llm_history_chat_name=chat_name,
        llm_history_mode="summary",
        llm_usage_callback=budget.record_usage,
    )

    def budgeted_call_json(**kwargs: Any) -> Dict[str, Any]:
        messages = kwargs.get("messages") or []
        estimated_input = sum(
            context_manager.estimate_tokens(message.get("content") or "")
            for message in messages
        ) + 600
        call_type = str(kwargs.get("call_type") or "")
        estimated_output = (
            7000
            if call_type == "memory_person_consolidate"
            else 8000
            if call_type in {
                "memory_person_extract",
                "memory_person_review",
                "memory_person_projection_review",
            }
            else 5000
        )
        reservation = budget.reserve(
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=estimated_output,
        )
        try:
            return service._call_memory_json(**kwargs)
        finally:
            budget.release(reservation)

    engine = PersonMemoryEngine(
        store,
        context_manager,
        budgeted_call_json,
        excluded_person_names=excluded_sender_names,
    )
    batch_directory = workspace / "observation_batches"
    batch_directory.mkdir(exist_ok=True)
    output_lock = threading.Lock()


    completed = 0
    extraction_batch_count = 0
    try:
        states = person_store.due_indexed_people(
            chat_name,
            force=True,
            limit=1000,
        )
        if selected_person_ids is not None:
            states = [
                state
                for state in states
                if int(state.get("person_id") or 0)
                in selected_person_ids
            ]
        total_pending_links = sum(
            int(state.get("pending_link_count") or 0)
            for state in states
        )
        processed_links = 0
        state_by_key = {
            (
                int(state["person_id"]),
                str(state["source_namespace"]),
            ): state
            for state in states
        }
        queue_cursors = {
            key: int(state.get("processed_link_id") or 0)
            for key, state in state_by_key.items()
        }
        exhausted: set[Tuple[int, str]] = set()

        def prepare_person_task(
            state: Dict[str, Any],
            after_link_id: int,
        ) -> Optional[Dict[str, Any]]:
            person_id = int(state["person_id"])
            namespace = str(state["source_namespace"])
            batch = person_store.next_indexed_person_batch(
                chat_name,
                person_id,
                namespace,
                limit=max(20, min(240, int(target_messages))),
                context_radius=max(1, min(8, int(overlap) // 4)),
                after_link_id=after_link_id,
            )
            batch = engine._fit_indexed_person_batch(
                batch,
                input_token_budget=max(4000, int(input_token_budget)),
            )
            core_cursors = list(batch.get("core_cursors") or [])
            link_ids = list(batch.get("link_ids") or [])
            messages = list(batch.get("messages") or [])
            if not core_cursors or not link_ids or not messages:
                return None
            return {
                "state": state,
                "person_id": person_id,
                "namespace": namespace,
                "core_cursors": core_cursors,
                "link_ids": link_ids,
                "messages": messages,
            }

        def extract_person_task(task: Dict[str, Any]) -> Dict[str, Any]:
            state = task["state"]
            person_id = int(task["person_id"])
            namespace = str(task["namespace"])
            core_cursors = list(task["core_cursors"])
            link_ids = list(task["link_ids"])
            messages = list(task["messages"])
            destination = (
                batch_directory
                / (
                    f"person-{person_id:05d}-{namespace}-"
                    f"{min(core_cursors):09d}-"
                    f"{max(core_cursors):09d}.json"
                )
            )
            if destination.is_file():
                return json.loads(
                    destination.read_text(encoding="utf-8")
                )

            def extract_segment(
                segment_cursors: Sequence[int],
                segment_messages: Sequence[Dict[str, Any]],
                *,
                depth: int = 0,
            ) -> Dict[str, Any]:
                ordered_cursors = sorted(
                    {
                        int(cursor)
                        for cursor in segment_cursors
                        if int(cursor) > 0
                    }
                )
                if not ordered_cursors:
                    raise RuntimeError(
                        "per-person split produced no core cursors"
                    )
                base_limit = max(
                    4,
                    min(30, int(max_observations_per_batch)),
                )
                proportional_limit = (
                    base_limit * len(ordered_cursors)
                    + max(1, len(core_cursors))
                    - 1
                ) // max(1, len(core_cursors))
                segment_limit = max(
                    4,
                    min(base_limit, proportional_limit),
                )
                batch_key = (
                    f"per-person:{namespace}:{person_id}:"
                    f"{min(ordered_cursors)}:{max(ordered_cursors)}:"
                    f"d{depth}"
                )
                result = engine.extract_observations(
                    chat_name,
                    segment_messages,
                    core_start_cursor=min(ordered_cursors),
                    core_end_cursor=max(ordered_cursors),
                    core_cursors=ordered_cursors,
                    source_namespace=namespace,
                    batch_key=batch_key,
                    excluded_sender_names=excluded_sender_names,
                    excluded_sender_ids=excluded_sender_ids,
                    target_person_id=person_id,
                    max_observations=segment_limit,
                    minimum_memory_value=max(
                        0.35,
                        min(0.95, float(candidate_memory_value)),
                    ),
                )
                if result is not None:
                    return {
                        "inserted": int(result.get("inserted") or 0),
                        "quarantined": int(
                            result.get("quarantined") or 0
                        ),
                        "filtered_low_value": int(
                            result.get("filtered_low_value") or 0
                        ),
                        "filtered_weak_evidence": int(
                            result.get("filtered_weak_evidence") or 0
                        ),
                        "filtered_verification": int(
                            result.get("filtered_verification") or 0
                        ),
                        "verification_quarantined": int(
                            result.get("verification_quarantined") or 0
                        ),
                        "observation_ids": list(
                            result.get("observation_ids") or []
                        ),
                        "split_depth": depth,
                        "model_segments": 1,
                    }
                if len(ordered_cursors) <= 16 or depth >= 4:
                    raise RuntimeError(
                        "per-person observation extraction failed "
                        "after split: "
                        f"{person_id}/{namespace}/"
                        f"{min(ordered_cursors)}-"
                        f"{max(ordered_cursors)}"
                    )
                middle = len(ordered_cursors) // 2
                child_results = []
                context_radius = max(
                    1,
                    min(8, int(overlap) // 4),
                )
                for child_cursors in (
                    ordered_cursors[:middle],
                    ordered_cursors[middle:],
                ):
                    allowed_cursors = {
                        cursor
                        for core_cursor in child_cursors
                        for cursor in range(
                            max(1, core_cursor - context_radius),
                            core_cursor + context_radius + 1,
                        )
                    }
                    child_messages = [
                        dict(message)
                        for message in segment_messages
                        if int(message.get("_log_cursor") or 0)
                        in allowed_cursors
                    ]
                    child_results.append(
                        extract_segment(
                            child_cursors,
                            child_messages,
                            depth=depth + 1,
                        )
                    )
                return {
                    "inserted": sum(
                        item["inserted"] for item in child_results
                    ),
                    "quarantined": sum(
                        item["quarantined"] for item in child_results
                    ),
                    "filtered_low_value": sum(
                        item["filtered_low_value"]
                        for item in child_results
                    ),
                    "filtered_weak_evidence": sum(
                        item["filtered_weak_evidence"]
                        for item in child_results
                    ),
                    "filtered_verification": sum(
                        item["filtered_verification"]
                        for item in child_results
                    ),
                    "verification_quarantined": sum(
                        item["verification_quarantined"]
                        for item in child_results
                    ),
                    "observation_ids": sorted(
                        {
                            int(value)
                            for item in child_results
                            for value in item["observation_ids"]
                        }
                    ),
                    "split_depth": max(
                        item["split_depth"] for item in child_results
                    ),
                    "model_segments": sum(
                        item["model_segments"]
                        for item in child_results
                    ),
                }

            result = extract_segment(
                core_cursors,
                messages,
            )
            record = {
                "person_id": person_id,
                "person_name": state.get("canonical_name"),
                "namespace": namespace,
                "core_cursors": core_cursors,
                "link_ids": link_ids,
                "inserted": int(result.get("inserted") or 0),
                "quarantined": int(result.get("quarantined") or 0),
                "filtered_low_value": int(
                    result.get("filtered_low_value") or 0
                ),
                "filtered_weak_evidence": int(
                    result.get("filtered_weak_evidence") or 0
                ),
                "filtered_verification": int(
                    result.get("filtered_verification") or 0
                ),
                "verification_quarantined": int(
                    result.get("verification_quarantined") or 0
                ),
                "observation_ids": list(
                    result.get("observation_ids") or []
                ),
                "split_depth": int(result.get("split_depth") or 0),
                "model_segments": int(
                    result.get("model_segments") or 1
                ),
                "completed_at": _now(),
            }
            with output_lock:
                _write_json(destination, record)
            return record

        worker_count = max(1, min(96, int(concurrency)))
        wave_size = worker_count * 2
        while len(exhausted) < len(state_by_key):
            wave: List[Dict[str, Any]] = []
            made_progress = True
            while len(wave) < wave_size and made_progress:
                made_progress = False
                for key, state in state_by_key.items():
                    if key in exhausted or len(wave) >= wave_size:
                        continue
                    task = prepare_person_task(
                        state,
                        queue_cursors[key],
                    )
                    if task is None:
                        exhausted.add(key)
                        continue
                    wave.append(task)
                    queue_cursors[key] = max(task["link_ids"])
                    made_progress = True
            if not wave:
                break
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="person-memory-person",
            ) as executor:
                futures = {
                    executor.submit(extract_person_task, task): task
                    for task in wave
                }
                for future in as_completed(futures):
                    future.result()
            completed += len(wave)
            wave_links: Dict[Tuple[int, str], List[int]] = {}
            for task in wave:
                key = (
                    int(task["person_id"]),
                    str(task["namespace"]),
                )
                wave_links.setdefault(key, []).extend(task["link_ids"])
                processed_links += len(task["link_ids"])
            for key, link_ids in wave_links.items():
                person_store.mark_indexed_person_batch_processed(
                    chat_name,
                    key[0],
                    key[1],
                    link_ids,
                )
            snapshot = budget.snapshot()
            print(
                "person-memory per-person observations: "
                f"batches={completed} "
                f"links={processed_links}/{total_pending_links} "
                f"cost=¥{snapshot['actual_cny']:.4f}",
                flush=True,
            )
        extraction_batch_count = completed
        if completed:
            snapshot = budget.snapshot()
            print(
                "person-memory per-person observations complete: "
                f"batches={completed} cost=¥{snapshot['actual_cny']:.4f}",
                flush=True,
            )

        bot_prompt_rejections = _reject_bot_prompt_observations(
            person_store,
            chat_name,
            excluded_sender_names,
        )
        if bot_prompt_rejections:
            with store._connection() as connection:
                connection.execute(
                    """
                    DELETE FROM memory_person_period_summaries
                    WHERE chat_name = ?
                    """,
                    (chat_name,),
                )
            stale_period_directory = workspace / "period_summaries"
            if stale_period_directory.is_dir():
                shutil.rmtree(stale_period_directory)
            print(
                "person-memory bot-prompt observations rejected: "
                f"{bot_prompt_rejections}",
                flush=True,
            )

        profiles_with_observations = person_store.list_profiles(chat_name)
        period_directory = workspace / "period_summaries"
        period_directory.mkdir(exist_ok=True)
        period_jobs = []
        for profile in profiles_with_observations:
            person_id = int(profile["person_id"])
            observations = person_store.list_observations(
                chat_name,
                person_id=person_id,
                quality_status="active",
                limit=10000,
            )
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for observation in observations:
                grouped.setdefault(_period_key(observation), []).append(
                    observation
                )
            for base_period, values in sorted(grouped.items()):
                for part_index, start in enumerate(
                    range(0, len(values), 100),
                    start=1,
                ):
                    chunk = values[start : start + 100]
                    key = (
                        base_period
                        if len(values) <= 100
                        else f"{base_period}-P{part_index:02d}"
                    )
                    period_jobs.append(
                        (
                            person_id,
                            str(profile.get("person_name") or ""),
                            key,
                            chunk,
                        )
                    )

        def summarize_period(
            person_id: int,
            person_name: str,
            period_key: str,
            observations: List[Dict[str, Any]],
        ) -> Dict[str, Any]:
            destination = (
                period_directory
                / f"person-{person_id:05d}-{period_key}.json"
            )
            if destination.is_file():
                record = json.loads(destination.read_text(encoding="utf-8"))
                person_store.upsert_period_summary(
                    chat_name,
                    person_id,
                    period_key,
                    record["payload"],
                    evidence_observation_ids=record["observation_ids"],
                    source_observation_max_id=max(record["observation_ids"]),
                    generator_version="person-memory.1-history",
                )
                return record
            allowed_ids = {int(item["id"]) for item in observations}

            def summarize_segment(
                values: Sequence[Dict[str, Any]],
                *,
                depth: int = 0,
            ) -> Dict[str, Any]:
                segment_ids = {
                    int(item["id"])
                    for item in values
                }
                try:
                    payload = budgeted_call_json(
                        call_type="memory_person_period_summarize",
                        messages=_period_prompt(
                            person_name,
                            period_key,
                            values,
                        ),
                        schema_hint=(
                            "根对象必须包含 summary、candidate_facts、"
                            "timeline、pattern_signals、"
                            "relationship_signals"
                        ),
                        chat_name=chat_name,
                    )
                    return _validate_period_payload(
                        payload,
                        segment_ids,
                    )
                except Exception as exc:
                    if "budget would be exceeded" in str(exc):
                        raise
                    if len(values) <= 20 or depth >= 3:
                        raise
                    middle = len(values) // 2
                    children = [
                        summarize_segment(
                            values[:middle],
                            depth=depth + 1,
                        ),
                        summarize_segment(
                            values[middle:],
                            depth=depth + 1,
                        ),
                    ]
                    merged: Dict[str, Any] = {
                        "summary": _clean_text(
                            "；".join(
                                str(child.get("summary") or "")
                                for child in children
                                if child.get("summary")
                            ),
                            1600,
                        ),
                        "candidate_facts": [],
                        "timeline": [],
                        "pattern_signals": [],
                        "relationship_signals": [],
                    }
                    limits = {
                        "candidate_facts": 12,
                        "timeline": 10,
                        "pattern_signals": 6,
                        "relationship_signals": 6,
                    }
                    for key, limit in limits.items():
                        merged[key] = [
                            item
                            for child in children
                            for item in child.get(key) or []
                        ][:limit]
                    return merged

            normalized = summarize_segment(observations)
            person_store.upsert_period_summary(
                chat_name,
                person_id,
                period_key,
                normalized,
                evidence_observation_ids=allowed_ids,
                source_observation_max_id=max(allowed_ids),
                generator_version="person-memory.1-history",
            )
            record = {
                "person_id": person_id,
                "person_name": person_name,
                "period_key": period_key,
                "observation_ids": sorted(allowed_ids),
                "payload": normalized,
                "completed_at": _now(),
            }
            with output_lock:
                _write_json(destination, record)
            return record

        completed_periods = 0
        with ThreadPoolExecutor(
            max_workers=max(1, min(48, int(concurrency))),
            thread_name_prefix="person-memory-period",
        ) as executor:
            futures = {
                executor.submit(summarize_period, *job): job[:3]
                for job in period_jobs
            }
            for future in as_completed(futures):
                future.result()
                completed_periods += 1
                if (
                    completed_periods % 20 == 0
                    or completed_periods == len(period_jobs)
                ):
                    snapshot = budget.snapshot()
                    print(
                        "person-memory periods: "
                        f"{completed_periods}/{len(period_jobs)} "
                        f"cost=¥{snapshot['actual_cny']:.4f}",
                        flush=True,
                    )

        consolidation_directory = workspace / "consolidation"
        consolidation_directory.mkdir(exist_ok=True)
        consolidation_results: List[Dict[str, Any]] = []

        def consolidate(profile: Dict[str, Any]) -> Dict[str, Any]:
            person_id = int(profile["person_id"])
            destination = (
                consolidation_directory / f"person-{person_id:05d}.json"
            )
            if destination.is_file():
                return json.loads(destination.read_text(encoding="utf-8"))
            result = None
            for attempt in range(1, 4):
                result = engine.consolidate_person(
                    chat_name,
                    person_id,
                    force=True,
                    observation_limit=500,
                    minimum_pattern_span_days=30,
                )
                if result is not None:
                    break
                print(
                    "person-memory final retry: "
                    f"person={person_id} attempt={attempt}/3",
                    flush=True,
                )
            if result is None:
                result = {
                    "person_id": person_id,
                    "person_name": profile.get("person_name"),
                    "skipped": True,
                }
            with output_lock:
                _write_json(destination, result)
            return result

        with ThreadPoolExecutor(
            max_workers=max(1, min(24, int(concurrency))),
            thread_name_prefix="person-memory-final",
        ) as executor:
            futures = {
                executor.submit(consolidate, profile): int(
                    profile["person_id"]
                )
                for profile in person_store.list_profiles(chat_name)
                if int(profile.get("observation_count") or 0) > 0
            }
            for future in as_completed(futures):
                consolidation_results.append(future.result())
                print(
                    "person-memory final profiles: "
                    f"{len(consolidation_results)}/{len(futures)}",
                    flush=True,
                )
    finally:
        service.close()

    report = _candidate_report(
        store,
        chat_name,
        cost=budget.snapshot(),
        source_counts=source_counts,
        extraction_batches=extraction_batch_count,
        period_summaries=len(period_jobs),
        consolidation_results=consolidation_results,
    )
    report.update(
        {
            "candidate_database": str(candidate_path),
            "historical_source": str(historical_source),
            "live_source": str(live_source or ""),
            "identity_messages_observed": observed_identity_messages,
            "sender_id_twin_inference": sender_id_twin_inference,
            "max_observations_per_batch": int(
                max_observations_per_batch
            ),
            "candidate_memory_value": float(candidate_memory_value),
            "bot_prompt_observations_rejected": int(
                bot_prompt_rejections
            ),
            "only_people": only_person_values,
            "selected_person_ids": sorted(selected_person_ids or []),
            "selected_person_names": sorted(selected_person_names),
            "excluded_sender_names": sorted(
                {
                    str(value or "").strip()
                    for value in (excluded_sender_names or [])
                    if str(value or "").strip()
                }
            ),
            "excluded_sender_ids": sorted(
                {
                    str(value or "").strip()
                    for value in (excluded_sender_ids or [])
                    if str(value or "").strip()
                }
            ),
        }
    )
    _write_json(workspace / "report.json", report)
    return report


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> List[str]:
    return [
        str(row["name"])
        for row in connection.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    ]


def _candidate_person_mapping(
    candidate_store: MemoryStore,
    production_store: MemoryStore,
    chat_name: str,
) -> Dict[int, int]:
    with candidate_store._connection() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT referenced.person_id FROM (
                SELECT person_id FROM memory_person_observations
                WHERE chat_name = ?
                UNION
                SELECT person_id FROM memory_person_snapshots
                WHERE chat_name = ?
                UNION
                SELECT person_id FROM memory_person_message_links
                WHERE chat_name = ?
                UNION
                SELECT person_id FROM memory_person_pipeline_state
                WHERE chat_name = ?
                UNION
                SELECT person_id FROM memory_person_claim_candidates
                WHERE chat_name = ?
            ) AS referenced
            JOIN memory_person_identities AS identity
              ON identity.id = referenced.person_id
             AND identity.chat_name = ?
             AND identity.status = 'active'
            ORDER BY referenced.person_id
            """,
            (
                chat_name,
                chat_name,
                chat_name,
                chat_name,
                chat_name,
                chat_name,
            ),
        ).fetchall()
    result = {}
    for row in rows:
        candidate_id = int(row["person_id"])
        with candidate_store._connection() as connection:
            identity = connection.execute(
                """
                SELECT canonical_name FROM memory_person_identities
                WHERE chat_name = ? AND id = ?
                """,
                (chat_name, candidate_id),
            ).fetchone()
            aliases = connection.execute(
                """
                SELECT alias_name, external_id
                FROM memory_person_aliases
                WHERE chat_name = ? AND person_id = ?
                  AND status = 'confirmed'
                ORDER BY CASE WHEN external_id != '' THEN 0 ELSE 1 END, id
                """,
                (chat_name, candidate_id),
            ).fetchall()
        if identity is None:
            continue
        target = None
        for alias in aliases:
            external_id = str(alias["external_id"] or "").strip()
            if not external_id or external_id.endswith("@chatroom"):
                continue
            target = production_store.resolve_person(
                chat_name,
                str(alias["alias_name"] or identity["canonical_name"]),
                external_id=external_id,
            )
            if target is not None:
                break
        if target is None:
            target = production_store.resolve_person(
                chat_name,
                str(identity["canonical_name"]),
            )
        if target is None:
            preferred_alias = aliases[0] if aliases else None
            target = production_store.resolve_person(
                chat_name,
                str(identity["canonical_name"]),
                external_id=(
                    str(preferred_alias["external_id"] or "")
                    if preferred_alias is not None
                    else ""
                ),
                create=True,
            )
        if target is None:
            raise RuntimeError(
                f"could not map candidate person {candidate_id}"
            )
        result[candidate_id] = int(target["id"])
    return result


def _allocate_id_map(
    production: sqlite3.Connection,
    candidate_rows: Sequence[Dict[str, Any]],
    table: str,
) -> Dict[int, int]:
    row = production.execute(
        f"SELECT COALESCE(MAX(id), 0) AS value FROM {table}"
    ).fetchone()
    next_id = int(row["value"] or 0) + 1
    result = {}
    for candidate in candidate_rows:
        old_id = int(candidate["id"])
        result[old_id] = next_id
        next_id += 1
    return result


def _remap_ids_json(raw: Any, mapping: Dict[int, int]) -> str:
    values = _json_load(raw, [])
    return _json_dump(
        [
            mapping.get(_safe_int(value), _safe_int(value))
            for value in values
            if _safe_int(value) > 0
        ]
    )


def activate_candidate(
    *,
    production_database: Path,
    candidate_database: Path,
    chat_name: str,
    workspace: Path,
) -> Dict[str, Any]:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = workspace / f"production-before-person-memory-activation-{timestamp}.db"
    _backup_database(production_database, backup)
    production_store = MemoryStore(production_database)
    candidate_store = MemoryStore(candidate_database)
    production_person = PersonMemoryStore(production_store)
    PersonMemoryStore(candidate_store)
    person_map = _candidate_person_mapping(
        candidate_store,
        production_store,
        chat_name,
    )
    candidate_connection = sqlite3.connect(candidate_database, timeout=60)
    candidate_connection.row_factory = sqlite3.Row
    production_connection = sqlite3.connect(production_database, timeout=60)
    production_connection.row_factory = sqlite3.Row
    try:
        production_connection.execute("PRAGMA journal_mode=WAL")
        production_connection.execute("PRAGMA busy_timeout=60000")
        candidate_rows: Dict[str, List[Dict[str, Any]]] = {}
        active_candidate_person_ids = {
            int(row["id"])
            for row in candidate_connection.execute(
                """
                SELECT id FROM memory_person_identities
                WHERE chat_name = ? AND status = 'active'
                """,
                (chat_name,),
            ).fetchall()
        }
        for table in PERSON_TABLES:
            candidate_rows[table] = [
                dict(row)
                for row in candidate_connection.execute(
                    f"SELECT * FROM {table} WHERE chat_name = ?",
                    (chat_name,),
                ).fetchall()
                if (
                    "person_id" not in row.keys()
                    or int(row["person_id"] or 0) == 0
                    or int(row["person_id"]) in active_candidate_person_ids
                )
            ]
        id_maps = {
            table: _allocate_id_map(
                production_connection,
                candidate_rows[table],
                table,
            )
            for table in PERSON_AUTOINCREMENT_TABLES
        }
        observation_map = id_maps["memory_person_observations"]
        fact_map = id_maps["memory_person_fact_versions"]
        source_message_map = id_maps["memory_person_source_messages"]
        message_link_map = id_maps["memory_person_message_links"]

        production_connection.execute("BEGIN IMMEDIATE")
        try:
            for table in PERSON_TABLES:
                production_connection.execute(
                    f"DELETE FROM {table} WHERE chat_name = ?",
                    (chat_name,),
                )
            for table in PERSON_TABLES:
                columns = _table_columns(production_connection, table)
                for source in candidate_rows[table]:
                    row = dict(source)
                    if "person_id" in row:
                        row["person_id"] = person_map.get(
                            int(row["person_id"]),
                            int(row["person_id"]),
                        )
                    if table in id_maps:
                        row["id"] = id_maps[table][int(source["id"])]
                    if table == "memory_person_observations":
                        evidence_cursors = _json_load(
                            row.get("evidence_cursors_json"),
                            [],
                        )
                        fingerprint_material = "|".join(
                            (
                                str(row["person_id"]),
                                str(
                                    row.get("observation_type")
                                    or "objective_fact"
                                ),
                                str(row.get("field_name") or "other"),
                                str(row.get("normalized_statement") or ""),
                                str(
                                    row.get("source_namespace")
                                    or "live_chat_log"
                                ),
                                ",".join(
                                    str(_safe_int(value))
                                    for value in evidence_cursors
                                    if _safe_int(value) > 0
                                ),
                            )
                        )
                        row["fingerprint"] = hashlib.sha256(
                            fingerprint_material.encode("utf-8")
                        ).hexdigest()
                    elif table == "memory_person_fact_versions":
                        row["evidence_observation_ids_json"] = _remap_ids_json(
                            row["evidence_observation_ids_json"],
                            observation_map,
                        )
                        row["supersedes_fact_id"] = fact_map.get(
                            int(row["supersedes_fact_id"] or 0),
                            0,
                        )
                        row["superseded_by_fact_id"] = fact_map.get(
                            int(row["superseded_by_fact_id"] or 0),
                            0,
                        )
                    elif table in {
                        "memory_person_patterns",
                        "memory_person_relationships",
                        "memory_person_period_summaries",
                        "memory_person_snapshots",
                    }:
                        row["evidence_observation_ids_json"] = _remap_ids_json(
                            row["evidence_observation_ids_json"],
                            observation_map,
                        )
                        if table == "memory_person_relationships":
                            target = int(row.get("target_person_id") or 0)
                            row["target_person_id"] = person_map.get(
                                target,
                                target,
                            )
                        if table == "memory_person_period_summaries":
                            row["source_observation_max_id"] = observation_map.get(
                                int(row["source_observation_max_id"] or 0),
                                0,
                            )
                        if table == "memory_person_snapshots":
                            sections = _json_load(row["sections_json"], {})
                            for items in sections.values():
                                for item in items or []:
                                    item["evidence_observation_ids"] = [
                                        observation_map.get(
                                            _safe_int(value),
                                            _safe_int(value),
                                        )
                                        for value in item.get(
                                            "evidence_observation_ids"
                                        )
                                        or []
                                    ]
                            row["sections_json"] = _json_dump(sections)
                            row["source_observation_max_id"] = observation_map.get(
                                int(row["source_observation_max_id"] or 0),
                                0,
                            )
                    elif table == "memory_person_refresh_state":
                        row["consolidated_observation_id"] = observation_map.get(
                            int(row["consolidated_observation_id"] or 0),
                            0,
                        )
                    elif table == "memory_person_pipeline_state":
                        row["processed_link_id"] = message_link_map.get(
                            int(row["processed_link_id"] or 0),
                            0,
                        )
                    elif table == "memory_person_projection_audit":
                        row["target_id"] = (
                            observation_map.get(
                                int(row["target_id"] or 0),
                                int(row["target_id"] or 0),
                            )
                            if row.get("target_type") == "observation"
                            else row.get("target_id")
                        )
                    elif table == "memory_person_message_links":
                        row["source_message_id"] = source_message_map.get(
                            int(row["source_message_id"] or 0),
                            int(row["source_message_id"] or 0),
                        )
                    elif table == "memory_person_state":
                        row["mode"] = "active"
                        row["activated_at"] = _now()
                        row["updated_at"] = _now()
                    values = [row.get(column) for column in columns]
                    placeholders = ",".join("?" for _ in columns)
                    production_connection.execute(
                        f"""
                        INSERT INTO {table}({','.join(columns)})
                        VALUES({placeholders})
                        """,
                        values,
                    )
            production_connection.commit()
        except Exception:
            production_connection.rollback()
            raise
    finally:
        production_connection.close()
        candidate_connection.close()

    state = production_person.get_chat_state(chat_name)
    profiles = production_person.list_profiles(chat_name)
    result = {
        "status": "activated",
        "chat_name": chat_name,
        "schema_version": PERSON_MEMORY_SCHEMA_VERSION,
        "candidate_database": str(candidate_database),
        "production_database": str(production_database),
        "backup_database": str(backup),
        "person_id_map": person_map,
        "profile_count": len(profiles),
        "snapshot_count": sum(bool(item.get("snapshot_id")) for item in profiles),
        "observation_stats": production_person.observation_stats(chat_name),
        "mode": state.get("mode"),
        "activated_at": state.get("activated_at"),
    }
    _write_json(workspace / "activation.json", result)
    return result


def catch_up_live_after_activation(
    *,
    production_database: Path,
    chat_name: str,
    workspace: Path,
    live_source: Path,
    budget_cny: Optional[float] = None,
) -> Dict[str, Any]:
    """Index and consolidate messages appended after a person-memory candidate was built.

    This is intentionally separate from event ingestion. During an atomic
    historical activation the event cursor is advanced to the live tail, so
    relying on a future event batch would permanently skip person indexing for
    messages that arrived while the candidate was being built.
    """

    if not live_source.is_file():
        raise FileNotFoundError(live_source)
    workspace.mkdir(parents=True, exist_ok=True)
    report_path = workspace / "report.json"
    report = (
        json.loads(report_path.read_text(encoding="utf-8"))
        if report_path.is_file()
        else {}
    )
    excluded_sender_names = list(report.get("excluded_sender_names") or [])
    excluded_sender_ids = list(report.get("excluded_sender_ids") or [])
    maximum_observations = max(
        4,
        min(30, int(report.get("max_observations_per_batch") or 16)),
    )
    minimum_memory_value = max(
        0.35,
        min(0.95, float(report.get("candidate_memory_value") or 0.58)),
    )
    report_cost = (
        report.get("cost")
        if isinstance(report.get("cost"), dict)
        else {}
    )
    limit_cny = float(
        budget_cny
        if budget_cny is not None
        else report_cost.get("limit_cny")
        or 200.0
    )

    store = MemoryStore(production_database)
    person_store = PersonMemoryStore(store)
    state = person_store.get_chat_state(chat_name)
    if state.get("mode") != "active":
        raise RuntimeError(
            "person memory must be active before live catch-up"
        )
    with store._connection() as connection:
        anchor_row = connection.execute(
            """
            SELECT message_time AS time, sender_name AS sender, content
            FROM memory_person_source_messages
            WHERE chat_name = ?
              AND (
                source_namespace IN(
                  'live_chat_log',
                  'live_chat_log_catchup'
                )
                OR source_namespace LIKE 'live_activation_catchup:%'
              )
            ORDER BY message_time DESC, id DESC
            LIMIT 1
            """,
            (chat_name,),
        ).fetchone()
        pending_links_before = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(pending_link_count), 0)
                FROM memory_person_pipeline_state
                WHERE chat_name = ?
                """,
                (chat_name,),
            ).fetchone()[0]
            or 0
        )
        pending_observations_before = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(pending_observation_count), 0)
                FROM memory_person_refresh_state
                WHERE chat_name = ?
                """,
                (chat_name,),
            ).fetchone()[0]
            or 0
        )
    if anchor_row is None:
        raise RuntimeError(
            "active person-memory candidate has no indexed live-message anchor"
        )

    boundary = _live_tail_after_anchor(
        _read_jsonl(live_source),
        dict(anchor_row),
    )
    if (
        int(boundary["tail_count"]) <= 0
        and pending_links_before <= 0
        and pending_observations_before <= 0
    ):
        result = {
            "status": "up_to_date",
            "chat_name": chat_name,
            "tail_count": 0,
            "pending_links_before": 0,
            "pending_observations_before": 0,
            "completed_at": _now(),
        }
        _write_json(workspace / "live_catchup.json", result)
        return result

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = (
        workspace
        / f"production-before-person-memory-live-catchup-{timestamp}.db"
    )
    _backup_database(production_database, backup)
    result: Dict[str, Any] = {
        "status": "catching_up",
        "chat_name": chat_name,
        "live_source": str(live_source),
        "backup_database": str(backup),
        "namespace": boundary["namespace"],
        "anchor": boundary["anchor"],
        "tail_count": int(boundary["tail_count"]),
        "context_count": int(boundary["context_count"]),
        "pending_links_before": pending_links_before,
        "pending_observations_before": pending_observations_before,
        "started_at": _now(),
    }
    _write_json(workspace / "live_catchup.json", result)

    service: Optional[ChatMemoryService] = None
    try:
        selected = list(boundary["selected"])
        core_cursors = list(boundary["core_cursors"])
        if core_cursors:
            core_cursor_set = set(core_cursors)
            core_messages = [
                message
                for message in selected
                if int(message.get("_log_cursor") or 0) in core_cursor_set
            ]
            _observe_identities(
                store,
                chat_name,
                [(boundary["namespace"], core_messages)],
                excluded_sender_names=excluded_sender_names,
                excluded_sender_ids=excluded_sender_ids,
            )
            indexed = person_store.index_person_messages(
                chat_name,
                selected,
                source_namespace=boundary["namespace"],
                core_cursors=core_cursors,
                excluded_sender_names=excluded_sender_names,
                excluded_sender_ids=excluded_sender_ids,
            )
        else:
            indexed = {
                "source_messages": 0,
                "links": 0,
                "authored_links": 0,
                "mention_links": 0,
                "people_touched": [],
            }
        result["indexed"] = indexed
        _write_json(workspace / "live_catchup.json", result)

        context_manager = ChatContextManager()
        budget = DeepSeekBudget(
            workspace / "cost.json",
            limit_cny=limit_cny,
        )
        manager = _configure_deepseek_manager(workspace)
        service = ChatMemoryService(
            _NoChatLog(),
            context_manager,
            store=store,
            llm_manager=manager,
            llm_history_chat_name=chat_name,
            llm_history_mode="summary",
            llm_usage_callback=budget.record_usage,
        )

        def budgeted_call_json(**kwargs: Any) -> Dict[str, Any]:
            messages = kwargs.get("messages") or []
            estimated_input = sum(
                context_manager.estimate_tokens(
                    message.get("content") or ""
                )
                for message in messages
            ) + 600
            call_type = str(kwargs.get("call_type") or "")
            estimated_output = (
                7000
                if call_type == "memory_person_consolidate"
                else 8000
                if call_type in {
                    "memory_person_extract",
                    "memory_person_review",
                    "memory_person_projection_review",
                }
                else 5000
            )
            reservation = budget.reserve(
                estimated_input_tokens=estimated_input,
                estimated_output_tokens=estimated_output,
            )
            try:
                return service._call_memory_json(**kwargs)
            finally:
                budget.release(reservation)

        engine = PersonMemoryEngine(
            store,
            context_manager,
            budgeted_call_json,
            excluded_person_names=excluded_sender_names,
        )
        batch_passes = []
        for pass_number in range(1, 101):
            batch_result = engine.process_due_person_batches(
                chat_name,
                threshold=1,
                batch_size=80,
                max_people=20,
                input_token_budget=24000,
                max_observations=maximum_observations,
                minimum_memory_value=minimum_memory_value,
                force=True,
                excluded_sender_names=excluded_sender_names,
                excluded_sender_ids=excluded_sender_ids,
            )
            batch_passes.append(batch_result)
            print(
                "[person-live-catchup] batch pass "
                f"{pass_number}: people="
                f"{batch_result.get('people_processed', 0)} "
                f"links={batch_result.get('links_processed', 0)} "
                f"observations={batch_result.get('inserted', 0)}",
                flush=True,
            )
            if int(batch_result.get("people_due") or 0) <= 0:
                break
        else:
            raise RuntimeError(
                "person live catch-up batch drain did not converge"
            )

        refresh_passes = []
        for pass_number in range(1, 101):
            refresh_result = engine.refresh_due_people(
                chat_name,
                threshold=1,
                force=False,
                limit=20,
            )
            refresh_passes.append(refresh_result)
            print(
                "[person-live-catchup] refresh pass "
                f"{pass_number}: people="
                f"{refresh_result.get('people_refreshed', 0)}",
                flush=True,
            )
            if int(refresh_result.get("people_due") or 0) <= 0:
                break
        else:
            raise RuntimeError(
                "person live catch-up profile refresh did not converge"
            )

        with store._connection() as connection:
            pending_links_after = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(pending_link_count), 0)
                    FROM memory_person_pipeline_state
                    WHERE chat_name = ?
                    """,
                    (chat_name,),
                ).fetchone()[0]
                or 0
            )
            pending_observations_after = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(pending_observation_count), 0)
                    FROM memory_person_refresh_state
                    WHERE chat_name = ?
                    """,
                    (chat_name,),
                ).fetchone()[0]
                or 0
            )
            quick_check = str(
                connection.execute("PRAGMA quick_check").fetchone()[0]
            )
        if pending_links_after or pending_observations_after:
            raise RuntimeError(
                "person live catch-up left pending work: "
                f"links={pending_links_after}, "
                f"observations={pending_observations_after}"
            )
        if quick_check != "ok":
            raise RuntimeError(
                f"production database quick_check failed: {quick_check}"
            )
        person_store.set_chat_mode(chat_name, "active")
        result.update(
            {
                "status": "complete",
                "batch_passes": batch_passes,
                "refresh_passes": refresh_passes,
                "pending_links_after": pending_links_after,
                "pending_observations_after":
                    pending_observations_after,
                "observation_stats": person_store.observation_stats(chat_name),
                "profile_count": sum(
                    bool(profile.get("snapshot_id"))
                    for profile in person_store.list_profiles(chat_name)
                ),
                "cost": budget.snapshot(),
                "quick_check": quick_check,
                "completed_at": _now(),
            }
        )
        _write_json(workspace / "live_catchup.json", result)
        return result
    except Exception as exc:
        result.update(
            {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "failed_at": _now(),
            }
        )
        _write_json(workspace / "live_catchup.json", result)
        raise
    finally:
        if service is not None:
            service.close()


def rollback_activation(
    *,
    production_database: Path,
    backup_database: Path,
    workspace: Path,
) -> Dict[str, Any]:
    if not backup_database.is_file():
        raise FileNotFoundError(backup_database)
    safety_backup = (
        workspace
        / (
            "production-before-person-memory-rollback-"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
            + ".db"
        )
    )
    _backup_database(production_database, safety_backup)
    replacement = production_database.with_suffix(".db.rollback.tmp")
    _backup_database(backup_database, replacement)
    os.replace(replacement, production_database)
    result = {
        "status": "rolled_back",
        "production_database": str(production_database),
        "restored_backup": str(backup_database),
        "pre_rollback_safety_backup": str(safety_backup),
        "completed_at": _now(),
    }
    _write_json(workspace / "rollback.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild person memory from raw historical messages",
    )
    parser.add_argument("--chat", required=True)
    parser.add_argument(
        "--database",
        default="data/chat_memory.db",
    )
    parser.add_argument("--source")
    parser.add_argument("--live-source", default="")
    parser.add_argument(
        "--workspace",
        default="data/person_rebuilds/person-memory",
    )
    parser.add_argument("--concurrency", type=int, default=48)
    parser.add_argument("--batch-messages", type=int, default=120)
    parser.add_argument("--overlap", type=int, default=16)
    parser.add_argument("--input-token-budget", type=int, default=24000)
    parser.add_argument(
        "--max-observations-per-batch",
        type=int,
        default=16,
    )
    parser.add_argument(
        "--candidate-memory-value",
        type=float,
        default=0.58,
    )
    parser.add_argument(
        "--only-person",
        action="append",
        default=[],
        help=(
            "quality-pilot identity name, alias, or sender_id; repeatable. "
            "Partial candidates cannot be activated"
        ),
    )
    parser.add_argument("--budget-cny", type=float, default=200.0)
    parser.add_argument(
        "--exclude-sender-name",
        action="append",
        default=[],
        help="sender name to exclude as a bot/system identity; repeatable",
    )
    parser.add_argument(
        "--exclude-sender-id",
        action="append",
        default=[],
        help="stable sender_id to exclude; repeatable",
    )
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument(
        "--rebuild-derived",
        action="store_true",
        help="reuse observations but rebuild facts, patterns and snapshots",
    )
    parser.add_argument("--activate-only", action="store_true")
    parser.add_argument("--catchup-live-only", action="store_true")
    parser.add_argument("--rollback-backup", default="")
    args = parser.parse_args()

    database = Path(args.database).resolve()
    workspace = Path(args.workspace).resolve()
    candidate = workspace / "candidate.db"
    if args.rollback_backup:
        result = rollback_activation(
            production_database=database,
            backup_database=Path(args.rollback_backup).resolve(),
            workspace=workspace,
        )
    elif args.catchup_live_only:
        if not args.live_source:
            raise ValueError(
                "--live-source is required with --catchup-live-only"
            )
        result = catch_up_live_after_activation(
            production_database=database,
            chat_name=args.chat,
            workspace=workspace,
            live_source=Path(args.live_source).resolve(),
            budget_cny=args.budget_cny,
        )
    elif args.activate_only:
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        report_path = workspace / "report.json"
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("only_people"):
                raise RuntimeError(
                    "partial --only-person candidate is for quality review "
                    "only and cannot replace production person memory"
                )
        result = activate_candidate(
            production_database=database,
            candidate_database=candidate,
            chat_name=args.chat,
            workspace=workspace,
        )
    else:
        if not args.source:
            raise ValueError("--source is required for rebuild")
        result = rebuild_candidate(
            production_database=database,
            chat_name=args.chat,
            workspace=workspace,
            historical_source=Path(args.source).resolve(),
            live_source=(
                Path(args.live_source).resolve()
                if args.live_source
                else None
            ),
            concurrency=args.concurrency,
            target_messages=args.batch_messages,
            overlap=args.overlap,
            input_token_budget=args.input_token_budget,
            budget_cny=args.budget_cny,
            fresh=args.fresh,
            rebuild_derived=args.rebuild_derived,
            max_observations_per_batch=args.max_observations_per_batch,
            candidate_memory_value=args.candidate_memory_value,
            only_people=args.only_person,
            excluded_sender_names=args.exclude_sender_name,
            excluded_sender_ids=args.exclude_sender_id,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
