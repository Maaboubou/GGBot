"""Run a prepared historical-memory experiment with bounded concurrency."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import litellm

from app.plugins.builtin_chatbot.context_manager import ChatContextManager
from app.plugins.builtin_chatbot.embedding_service import LocalEmbeddingService
from app.plugins.builtin_chatbot.memory_experiment import (
    MemoryExperimentError,
    _write_json,
    verify_experiment,
)
from app.plugins.builtin_chatbot.memory_service import ChatMemoryService
from app.plugins.builtin_chatbot.memory_store import MemoryStore
from app.services.llm_manager import LLMManager


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventBatch:
    index: int
    start_cursor: int
    end_cursor: int
    messages: List[Dict[str, Any]]


class ExperimentLedger:
    """Thread-safe durable extraction and billing ledger."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            path,
            timeout=30,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS event_batches (
                    batch_index INTEGER PRIMARY KEY,
                    start_cursor INTEGER NOT NULL,
                    end_cursor INTEGER NOT NULL,
                    message_count INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    cards_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS llm_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    trace_id TEXT NOT NULL DEFAULT '',
                    call_type TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    actual_model TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
                    response_time REAL NOT NULL DEFAULT 0,
                    cost_yuan REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _call_cost(entry: Dict[str, Any]) -> tuple[float, Dict[str, int]]:
        usage = entry.get("token_usage") or {}
        prompt = max(0, int(usage.get("prompt_tokens") or 0))
        completion = max(0, int(usage.get("completion_tokens") or 0))
        cache_miss = max(
            0,
            int(
                usage.get("cache_miss_tokens")
                if usage.get("cache_miss_tokens") is not None
                else prompt
            ),
        )
        cache_hit = max(
            0,
            int(usage.get("cached_tokens") or (prompt - cache_miss)),
        )
        actual_model = str(entry.get("actual_model") or "").lower()
        model_id = str(entry.get("model_id") or "").lower()
        if "deepseek-v4-flash" in actual_model or model_id in {
            "deepseek",
            "deepseek-followup",
        }:
            cost = (
                cache_miss * 1.0 / 1_000_000
                + cache_hit * 0.02 / 1_000_000
                + completion * 2.0 / 1_000_000
            )
        elif "free" in model_id:
            cost = 0.0
        else:
            cost = 0.0
        return cost, {
            "prompt": prompt,
            "completion": completion,
            "cache_miss": cache_miss,
            "cache_hit": cache_hit,
        }

    def record_llm_call(self, entry: Dict[str, Any]) -> None:
        cost, tokens = self._call_cost(entry)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO llm_calls(
                    timestamp, trace_id, call_type, model_id, actual_model,
                    success, prompt_tokens, completion_tokens,
                    cache_miss_tokens, cache_hit_tokens, response_time,
                    cost_yuan, error
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(entry.get("timestamp") or self._now()),
                    str(entry.get("trace_id") or ""),
                    str(entry.get("call_type") or ""),
                    str(entry.get("model_id") or ""),
                    str(entry.get("actual_model") or ""),
                    int(bool(entry.get("success"))),
                    tokens["prompt"],
                    tokens["completion"],
                    tokens["cache_miss"],
                    tokens["cache_hit"],
                    float(entry.get("response_time") or 0),
                    float(cost),
                    str(entry.get("error") or "")[:2000],
                ),
            )

    def ensure_batches(self, batches: Sequence[EventBatch]) -> None:
        now = self._now()
        with self._lock, self._connection:
            existing = {
                int(row["batch_index"]): (
                    int(row["start_cursor"]),
                    int(row["end_cursor"]),
                    int(row["message_count"]),
                )
                for row in self._connection.execute(
                    """
                    SELECT batch_index, start_cursor, end_cursor, message_count
                    FROM event_batches
                    """
                )
            }
            expected = {
                batch.index: (
                    batch.start_cursor,
                    batch.end_cursor,
                    len(batch.messages),
                )
                for batch in batches
            }
            if existing and existing != expected:
                raise MemoryExperimentError(
                    "prepared batch layout differs from the existing run ledger"
                )
            if existing:
                return
            self._connection.executemany(
                """
                INSERT INTO event_batches(
                    batch_index, start_cursor, end_cursor, message_count,
                    status, updated_at
                ) VALUES(?, ?, ?, ?, 'pending', ?)
                """,
                [
                    (
                        batch.index,
                        batch.start_cursor,
                        batch.end_cursor,
                        len(batch.messages),
                        now,
                    )
                    for batch in batches
                ],
            )

    def pending_batch_indexes(self, limit: int = 0) -> List[int]:
        sql = (
            "SELECT batch_index FROM event_batches "
            "WHERE status NOT IN ('extracted', 'committed') "
            "ORDER BY batch_index"
        )
        params: list[Any] = []
        if limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            return [
                int(row[0])
                for row in self._connection.execute(sql, params).fetchall()
            ]

    def save_extraction(
        self,
        batch_index: int,
        *,
        cards: Sequence[Dict[str, Any]] | None,
        attempts: int,
        error: str = "",
    ) -> None:
        status = "extracted" if cards is not None else "failed"
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE event_batches SET
                    status = ?, attempts = ?, cards_json = ?, error = ?,
                    updated_at = ?
                WHERE batch_index = ?
                """,
                (
                    status,
                    max(0, int(attempts)),
                    json.dumps(cards or [], ensure_ascii=False),
                    str(error or "")[:4000],
                    self._now(),
                    int(batch_index),
                ),
            )

    def extracted_cards(self, batch_index: int) -> List[Dict[str, Any]] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT status, cards_json FROM event_batches
                WHERE batch_index = ?
                """,
                (int(batch_index),),
            ).fetchone()
        if row is None or row["status"] not in {"extracted", "committed"}:
            return None
        value = json.loads(row["cards_json"] or "[]")
        return value if isinstance(value, list) else []

    def mark_committed(self, batch_index: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE event_batches
                SET status = 'committed', updated_at = ?
                WHERE batch_index = ?
                """,
                (self._now(), int(batch_index)),
            )

    def reconcile_committed_cursor(self, source_message_count: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE event_batches
                SET status = 'committed', updated_at = ?
                WHERE end_cursor <= ? AND status = 'extracted'
                """,
                (self._now(), max(0, int(source_message_count))),
            )

    def counts(self) -> Dict[str, int]:
        with self._lock:
            result = {
                str(row["status"]): int(row["value"])
                for row in self._connection.execute(
                    """
                    SELECT status, COUNT(*) AS value
                    FROM event_batches GROUP BY status
                    """
                )
            }
            result["llm_calls"] = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM llm_calls"
                ).fetchone()[0]
            )
        return result

    def billing(self) -> Dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    COUNT(*) AS calls,
                    COALESCE(SUM(cost_yuan), 0) AS cost,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(cache_miss_tokens), 0) AS cache_miss_tokens,
                    COALESCE(SUM(cache_hit_tokens), 0) AS cache_hit_tokens
                FROM llm_calls
                """
            ).fetchone()
            by_type = {
                str(item["call_type"]): {
                    "calls": int(item["calls"]),
                    "cost_yuan": round(float(item["cost"]), 6),
                }
                for item in self._connection.execute(
                    """
                    SELECT call_type, COUNT(*) AS calls,
                           COALESCE(SUM(cost_yuan), 0) AS cost
                    FROM llm_calls GROUP BY call_type
                    """
                )
            }
        return {
            "calls": int(row["calls"]),
            "cost_yuan": round(float(row["cost"]), 6),
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
            "cache_miss_tokens": int(row["cache_miss_tokens"]),
            "cache_hit_tokens": int(row["cache_hit_tokens"]),
            "by_type": by_type,
        }

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _load_messages(path: Path) -> List[Dict[str, Any]]:
    messages = []
    with path.open("r", encoding="utf-8") as source:
        for cursor, line in enumerate(source, start=1):
            if not line.strip():
                continue
            message = json.loads(line)
            expected = int(message.get("memory_cursor") or 0)
            if expected != cursor:
                raise MemoryExperimentError(
                    f"memory cursor mismatch at line {cursor}: {expected}"
                )
            message["_log_cursor"] = cursor
            messages.append(message)
    return messages


def _build_batches(
    messages: Sequence[Dict[str, Any]],
    context_manager: ChatContextManager,
    *,
    maximum: int = 60,
    token_budget: int = 12000,
) -> List[EventBatch]:
    batches = []
    offset = 0
    while offset < len(messages):
        requested = list(messages[offset : offset + maximum])
        selected, _ = context_manager.select_prefix_messages(
            requested,
            token_budget,
        )
        if not selected:
            raise MemoryExperimentError(
                f"no message fits the token budget at cursor {offset + 1}"
            )
        start_cursor = int(selected[0]["_log_cursor"])
        end_cursor = int(selected[-1]["_log_cursor"])
        batches.append(
            EventBatch(
                index=len(batches) + 1,
                start_cursor=start_cursor,
                end_cursor=end_cursor,
                messages=list(selected),
            )
        )
        offset += len(selected)
    return batches


def _update_state(workspace: Path, **updates: Any) -> Dict[str, Any]:
    path = workspace / "run_state.json"
    with path.open("r", encoding="utf-8") as source:
        state = json.load(source)
    state.update(updates)
    state["updated_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    _write_json(path, state)
    return state


def _memory_config(manifest: Dict[str, Any]) -> Dict[str, Any]:
    planned = manifest.get("planned_memory_config") or {}
    stage_input_event_limit = max(
        5,
        int(planned.get("stage_input_event_limit") or 80),
    )
    backfill_stage_threshold = max(
        5,
        int(
            planned.get("backfill_stage_event_threshold")
            or stage_input_event_limit
        ),
    )
    return {
        "memory_enabled": True,
        "memory_embedding_enabled": True,
        "memory_embedding_model": str(
            planned.get("embedding_model") or "BAAI/bge-small-zh-v1.5"
        ),
        "memory_embedding_threads": 4,
        "memory_dedup_enabled": True,
        "memory_verification_enabled": True,
        "memory_dedup_lookback_days": int(
            planned.get("dedup_lookback_days") or 30
        ),
        "memory_dedup_candidate_threshold": 0.78,
        "memory_duplicate_similarity_threshold": 0.90,
        "memory_dedup_compact_output": True,
        "memory_stage_event_threshold": backfill_stage_threshold,
        # A live version update should refresh the stage immediately. During a
        # chronological backfill, however, versioned events are common and
        # refreshing after each batch would produce thousands of disposable
        # intermediate snapshots. The final forced refresh still guarantees
        # that the completed experiment has an up-to-date stage.
        "memory_stage_immediate_version_update": False,
        "memory_stage_input_event_limit": stage_input_event_limit,
        "memory_stage_input_token_budget": int(
            planned.get("stage_input_token_budget") or 24000
        ),
        "memory_stage_char_limit": 6000,
        "memory_experiment_commit_group_batches": max(
            1,
            min(
                16,
                int(planned.get("commit_group_batches") or 8),
            ),
        ),
    }


def _configure_experiment_llm(
    manager: LLMManager,
    _manifest: Dict[str, Any],
) -> None:
    """Tune structured calls only inside the isolated backfill manager."""
    plugin_mappings = manager.config.get("plugin_mappings")
    if not isinstance(plugin_mappings, dict):
        return
    plugin = plugin_mappings.get("builtin_chatbot")
    if not isinstance(plugin, dict):
        return
    generate_mapping = plugin.get("memory_generate")
    if isinstance(generate_mapping, dict):
        overrides = generate_mapping.setdefault("override_params", {})
        if isinstance(overrides, dict):
            # DeepSeek reasoning tokens count against max_tokens. With
            # thinking enabled, long batches frequently spend the entire
            # ceiling before emitting the JSON payload.
            overrides["extra_body"] = {
                "thinking": {"type": "disabled"},
            }
    models = manager.config.get("models")
    synthesize_mapping = plugin.get("memory_synthesize")
    if (
        isinstance(models, dict)
        and "deepseek-followup" in models
        and isinstance(synthesize_mapping, dict)
    ):
        previous_primary = str(synthesize_mapping.get("primary") or "").strip()
        synthesize_mapping["primary"] = "deepseek-followup"
        fallbacks = [
            str(item).strip()
            for item in synthesize_mapping.get("fallback") or []
            if str(item).strip() and str(item).strip() != "deepseek-followup"
        ]
        if previous_primary and previous_primary != "deepseek-followup":
            fallbacks.insert(0, previous_primary)
        synthesize_mapping["fallback"] = list(dict.fromkeys(fallbacks))


def _extract_batches(
    *,
    service: ChatMemoryService,
    ledger: ExperimentLedger,
    batches: Sequence[EventBatch],
    chat_name: str,
    experiment_id: str,
    concurrency: int,
    max_attempts: int,
    max_cost_yuan: float,
    max_batches: int = 0,
    workspace: Path,
) -> None:
    pending = ledger.pending_batch_indexes(limit=max_batches)
    if not pending:
        return
    by_index = {batch.index: batch for batch in batches}
    stop_event = threading.Event()

    def worker(batch_index: int) -> tuple[int, bool, int, str]:
        last_error = ""
        for attempt in range(1, max_attempts + 1):
            if stop_event.is_set():
                return batch_index, False, attempt - 1, "stopped"
            if ledger.billing()["cost_yuan"] >= max_cost_yuan:
                stop_event.set()
                return batch_index, False, attempt - 1, "cost limit reached"
            trace_id = (
                f"{experiment_id}:event:{batch_index:04d}:attempt:{attempt}"
            )
            try:
                cards = service._extract_event_cards(
                    chat_name,
                    by_index[batch_index].messages,
                    trace_id=trace_id,
                )
                if cards is not None:
                    ledger.save_extraction(
                        batch_index,
                        cards=cards,
                        attempts=attempt,
                    )
                    return batch_index, True, attempt, ""
                last_error = "event extraction returned no valid payload"
            except Exception as exc:
                last_error = str(exc)
            if attempt < max_attempts:
                time.sleep(min(2.0 * attempt, 5.0))
        ledger.save_extraction(
            batch_index,
            cards=None,
            attempts=max_attempts,
            error=last_error,
        )
        return batch_index, False, max_attempts, last_error

    completed = 0
    failed = 0
    started_at = time.perf_counter()
    _update_state(
        workspace,
        status="extracting",
        extraction_concurrency=concurrency,
        max_cost_yuan=max_cost_yuan,
    )
    with ThreadPoolExecutor(
        max_workers=max(1, int(concurrency)),
        thread_name_prefix="memory-extract",
    ) as executor:
        futures = {
            executor.submit(worker, batch_index): batch_index
            for batch_index in pending
        }
        for future in as_completed(futures):
            _, success, _, _ = future.result()
            completed += 1
            failed += int(not success)
            if completed % 10 == 0 or completed == len(pending):
                billing = ledger.billing()
                elapsed = max(0.001, time.perf_counter() - started_at)
                rate = completed / elapsed
                remaining = max(0, len(pending) - completed)
                _update_state(
                    workspace,
                    extracted_batches=ledger.counts().get("extracted", 0),
                    extraction_failed_batches=failed,
                    llm_calls=billing["calls"],
                    actual_cost_yuan=billing["cost_yuan"],
                    extraction_batches_per_second=round(rate, 4),
                    extraction_eta_seconds=round(remaining / rate)
                    if rate > 0
                    else 0,
                )
                print(
                    json.dumps(
                        {
                            "phase": "extract",
                            "completed_now": completed,
                            "scheduled_now": len(pending),
                            "failed_now": failed,
                            "cost_yuan": billing["cost_yuan"],
                            "eta_seconds": round(remaining / rate)
                            if rate > 0
                            else 0,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            if ledger.billing()["cost_yuan"] >= max_cost_yuan:
                stop_event.set()
    if failed:
        raise MemoryExperimentError(
            f"{failed} event batches failed after {max_attempts} attempts"
        )


def _commit_batches(
    *,
    service: ChatMemoryService,
    store: MemoryStore,
    ledger: ExperimentLedger,
    batches: Sequence[EventBatch],
    chat_name: str,
    config: Dict[str, Any],
    max_cost_yuan: float,
    max_batches: int = 0,
    workspace: Path,
) -> None:
    state = store.get_state(chat_name)
    ledger.reconcile_committed_cursor(
        int(state.get("source_message_count") or 0)
    )
    committed_now = 0
    with (workspace / "run_state.json").open("r", encoding="utf-8") as source:
        run_state = json.load(source)
    stage_total = int(run_state.get("stage_updates") or 0)
    group_size = max(
        1,
        min(
            16,
            int(config.get("memory_experiment_commit_group_batches") or 1),
        ),
    )
    pending_group: List[tuple[EventBatch, List[Dict[str, Any]]]] = []
    last_reported = 0

    def commit_group(
        group: Sequence[tuple[EventBatch, List[Dict[str, Any]]]],
    ) -> None:
        nonlocal committed_now, last_reported, stage_total
        if not group:
            return
        if ledger.billing()["cost_yuan"] >= max_cost_yuan:
            raise MemoryExperimentError("cost limit reached before commit")

        cards = [
            card
            for _batch, extracted_cards in group
            for card in extracted_cards
        ]
        group_messages = [
            message
            for batch, _extracted_cards in group
            for message in batch.messages
        ]
        for card in cards:
            source_start = int(card.get("source_start_cursor") or 0)
            source_end = int(card.get("source_end_cursor") or source_start)
            card["source_messages"] = [
                dict(message)
                for message in group_messages
                if source_start
                <= int(message.get("_log_cursor") or 0)
                <= source_end
            ]
        if (
            config.get("memory_verification_enabled", True)
            and cards
            and hasattr(service, "_verify_high_risk_event_cards")
        ):
            cards = service._verify_high_risk_event_cards(
                chat_name,
                cards,
                group_messages,
            )
        eligible_cards = [
            card
            for card in cards
            if card.get("verification_status") != "quarantined"
        ]
        quarantined_cards = [
            card
            for card in cards
            if card.get("verification_status") == "quarantined"
        ]
        vectors = service.embedding_service.embed_passages(
            card.get("search_text") or ""
            for card in eligible_cards
        )
        for index, card in enumerate(eligible_cards):
            card["embedding"] = (
                vectors[index] if index < len(vectors) else None
            )
        eligible_cards, _ = service._apply_event_deduplication(
            chat_name,
            eligible_cards,
            config,
        )
        store.add_events(
            chat_name,
            [*eligible_cards, *quarantined_cards],
        )
        last_batch = group[-1][0]
        store.advance_cursor(
            chat_name,
            source_cursor=last_batch.end_cursor,
            source_message_count=last_batch.end_cursor,
        )
        service.invalidate(chat_name)
        stage_updated = int(service._refresh_stage_if_due(chat_name, config))
        for batch, _cards in group:
            ledger.mark_committed(batch.index)
        committed_now += len(group)
        stage_total += stage_updated

        if committed_now - last_reported >= 10:
            billing = ledger.billing()
            current_state = store.get_state(chat_name)
            _update_state(
                workspace,
                status="committing",
                processed_messages=int(
                    current_state.get("source_message_count") or 0
                ),
                committed_batches=ledger.counts().get("committed", 0),
                events_created=store.count_events(chat_name),
                stage_updates=stage_total,
                llm_calls=billing["calls"],
                actual_cost_yuan=billing["cost_yuan"],
            )
            print(
                json.dumps(
                    {
                        "phase": "commit",
                        "committed_now": committed_now,
                        "processed_messages": current_state.get(
                            "source_message_count"
                        ),
                        "events": store.count_events(chat_name),
                        "cost_yuan": billing["cost_yuan"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            last_reported = committed_now

    cursor = int(state.get("source_message_count") or 0)
    for batch in batches:
        if (
            max_batches > 0
            and committed_now + len(pending_group) >= max_batches
        ):
            break
        if batch.end_cursor <= cursor:
            ledger.mark_committed(batch.index)
            continue
        cards = ledger.extracted_cards(batch.index)
        if cards is None:
            break
        pending_group.append((batch, cards))
        if len(pending_group) >= group_size:
            commit_group(pending_group)
            cursor = pending_group[-1][0].end_cursor
            pending_group = []

    if pending_group:
        commit_group(pending_group)

    if committed_now:
        stage_total += int(
            service._refresh_stage_if_due(chat_name, config, force=True)
        )
    billing = ledger.billing()
    final_state = store.get_state(chat_name)
    counts = ledger.counts()
    _update_state(
        workspace,
        processed_messages=int(
            final_state.get("source_message_count") or 0
        ),
        committed_batches=counts.get("committed", 0),
        events_created=store.count_events(chat_name),
        stage_updates=stage_total,
        llm_calls=billing["calls"],
        actual_cost_yuan=billing["cost_yuan"],
    )


def run_experiment(
    workspace: str | Path,
    *,
    concurrency: int = 64,
    max_cost_yuan: float = 35.0,
    max_attempts: int = 2,
    max_batches: int = 0,
) -> Dict[str, Any]:
    root = Path(workspace)
    with (root / "run_state.json").open("r", encoding="utf-8") as source:
        existing_state = json.load(source)
    allow_started = (
        (root / "run_ledger.db").exists()
        or str(existing_state.get("status") or "") != "prepared"
        or int(existing_state.get("llm_calls") or 0) > 0
    )
    verify_experiment(root, allow_started=allow_started)
    with (root / "manifest.json").open("r", encoding="utf-8") as source:
        manifest = json.load(source)
    experiment_id = str(manifest["experiment_id"])
    chat_name = str(manifest["chat_name"])
    context_manager = ChatContextManager()
    messages = _load_messages(root / "memory_messages.jsonl")
    batches = _build_batches(messages, context_manager)
    expected = int(
        manifest["selection"]["estimated_event_batches_at_60_messages"]
    )
    if len(batches) != expected:
        raise MemoryExperimentError(
            f"batch count changed: expected {expected}, got {len(batches)}"
        )

    ledger = ExperimentLedger(root / "run_ledger.db")
    ledger.ensure_batches(batches)
    # The runner uses synchronous calls from a long-lived thread pool. HTTPX
    # keeps one reusable sync connection pool for that workload, whereas
    # LiteLLM's default aiohttp transport is tied to asyncio event loops and
    # can churn sessions when invoked through many sync worker threads.
    litellm.disable_aiohttp_transport = True
    manager = LLMManager(config_dir="data", telemetry_dir=root)
    _configure_experiment_llm(manager, manifest)
    embedding = LocalEmbeddingService(
        model_name=str(
            manifest["planned_memory_config"].get("embedding_model")
            or "BAAI/bge-small-zh-v1.5"
        ),
        threads=4,
    )
    store = MemoryStore(root / "chat_memory.db")
    service = ChatMemoryService(
        chat_log_manager=None,
        context_manager=context_manager,
        store=store,
        embedding_service=embedding,
        llm_manager=manager,
        llm_history_chat_name=f"memory-experiment:{experiment_id}",
        llm_history_mode="summary",
        llm_usage_callback=ledger.record_llm_call,
    )
    config = _memory_config(manifest)
    try:
        _extract_batches(
            service=service,
            ledger=ledger,
            batches=batches,
            chat_name=chat_name,
            experiment_id=experiment_id,
            concurrency=max(1, min(256, int(concurrency))),
            max_attempts=max(1, min(5, int(max_attempts))),
            max_cost_yuan=max(1.0, float(max_cost_yuan)),
            max_batches=max(0, int(max_batches)),
            workspace=root,
        )
        if not embedding.warmup():
            raise MemoryExperimentError(
                "local embedding model unavailable: " + embedding.last_error
            )
        _commit_batches(
            service=service,
            store=store,
            ledger=ledger,
            batches=batches,
            chat_name=chat_name,
            config=config,
            max_cost_yuan=max(1.0, float(max_cost_yuan)),
            max_batches=max(0, int(max_batches)),
            workspace=root,
        )
        counts = ledger.counts()
        all_done = counts.get("committed", 0) == len(batches)
        billing = ledger.billing()
        result = {
            "experiment_id": experiment_id,
            "status": "complete" if all_done else "preflight_complete",
            "batch_count": len(batches),
            "counts": counts,
            "events": store.count_events(chat_name),
            "active_events": store.count_events(
                chat_name,
                active_only=True,
            ),
            "people": store.count_people(chat_name),
            "billing": billing,
        }
        _update_state(root, **result)
        return result
    except Exception as exc:
        billing = ledger.billing()
        _update_state(
            root,
            status="failed",
            last_error=str(exc)[:4000],
            llm_calls=billing["calls"],
            actual_cost_yuan=billing["cost_yuan"],
        )
        raise
    finally:
        service.close()
        ledger.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a prepared historical-memory experiment."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--max-cost-yuan", type=float, default=35.0)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="Limit pending extraction and commit batches; 0 runs all.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        result = run_experiment(
            args.workspace,
            concurrency=args.concurrency,
            max_cost_yuan=args.max_cost_yuan,
            max_attempts=args.max_attempts,
            max_batches=args.max_batches,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0
    except Exception as exc:
        logger.exception("Memory experiment failed")
        print(
            json.dumps(
                {"status": "failed", "error": str(exc)},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
