"""Prepare isolated, reproducible historical-memory experiments.

Preparation is deliberately local-only: it normalizes a structured chat
export, creates an empty MemoryStore-compatible database, snapshots the
target chat's current production memory, and records immutable hashes.  It
does not initialize embeddings or call an LLM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

from app.plugins.builtin_chatbot.memory_store import MemoryStore


MANIFEST_SCHEMA_VERSION = 1
RUN_STATE_SCHEMA_VERSION = 1
SUPPORTED_SOURCE_FORMAT = "ciphertalk-extracted-v2"
OPAQUE_MEMORY_TYPES = frozenset(
    {
        "表情包",
        "图片",
        "视频",
        "语音",
        "其他/未知",
    }
)
MEMORY_TABLES = ("memory_state", "memory_events", "memory_people")


class MemoryExperimentError(ValueError):
    """Raised when an experiment cannot be prepared or verified safely."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_experiment_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    normalized = normalized.strip(".-")
    if not normalized or len(normalized) > 100:
        raise MemoryExperimentError(
            "experiment_id must contain 1-100 safe filename characters"
        )
    return normalized


def _normalize_content(value: Any) -> str:
    return (
        str(value or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\x00", "")
        .strip()
    )


def _normalized_message(
    raw: Dict[str, Any],
    *,
    source_index: int,
    memory_cursor: int | None,
) -> Dict[str, Any]:
    message_type = str(
        raw.get("mappedTypeName")
        or raw.get("chatlabTypeName")
        or "其他/未知"
    ).strip()
    sender_id = str(raw.get("sender") or "").strip()
    sender_name = str(raw.get("senderName") or sender_id or "系统").strip()
    eligible = message_type not in OPAQUE_MEMORY_TYPES
    return {
        "time": str(raw.get("time") or "").strip(),
        "sender": sender_name,
        "content": _normalize_content(raw.get("content")),
        "source_id": str(raw.get("platformMessageId") or "").strip(),
        "sender_id": sender_id,
        "message_type": message_type,
        "source_type": str(raw.get("type") or "").strip(),
        "source_sub_type": str(raw.get("subType") or "").strip(),
        "source_index": int(source_index),
        "memory_cursor": memory_cursor,
        "memory_eligible": eligible,
        "exclusion_reason": "" if eligible else "opaque_media_without_text",
    }


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(payload, target, ensure_ascii=False, indent=2, sort_keys=True)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp_path, path)


def _copy_chat_rows(
    source_db: Path,
    destination_db: Path,
    chat_name: str,
) -> Dict[str, int]:
    """Create a group-scoped rollback snapshot without mutating production."""
    MemoryStore(destination_db)
    counts = {table: 0 for table in MEMORY_TABLES}
    if not source_db.exists():
        return counts

    source = sqlite3.connect(
        f"file:{source_db.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    destination = sqlite3.connect(destination_db, timeout=30)
    try:
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only=ON")
        source.execute("PRAGMA busy_timeout=30000")
        source.execute("BEGIN")
        with destination:
            for table in MEMORY_TABLES:
                rows = source.execute(
                    f"SELECT * FROM {table} WHERE chat_name = ?",
                    (chat_name,),
                ).fetchall()
                if not rows:
                    continue
                columns = list(rows[0].keys())
                placeholders = ",".join("?" for _ in columns)
                column_sql = ",".join(columns)
                destination.executemany(
                    f"INSERT INTO {table}({column_sql}) VALUES({placeholders})",
                    [tuple(row[column] for column in columns) for row in rows],
                )
                counts[table] = len(rows)
    finally:
        if source.in_transaction:
            source.rollback()
        destination.close()
        source.close()
    return counts


def _pipeline_hashes(project_root: Path) -> Dict[str, str]:
    relative_paths = (
        "app/plugins/builtin_chatbot/memory_experiment.py",
        "app/plugins/builtin_chatbot/memory_experiment_runner.py",
        "app/plugins/builtin_chatbot/memory_service.py",
        "app/plugins/builtin_chatbot/memory_store.py",
        "app/plugins/builtin_chatbot/embedding_service.py",
        "app/services/llm_manager.py",
        "app/plugins/builtin_chatbot/config.json",
        "data/llm_mappings.json",
    )
    result = {}
    for relative_path in relative_paths:
        path = project_root / relative_path
        if path.exists():
            result[relative_path] = _sha256_file(path)
    return result


def _line_count(path: Path) -> int:
    with path.open("rb") as source:
        return sum(1 for line in source if line.strip())


def _db_counts(path: Path) -> Dict[str, int]:
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
    )
    try:
        return {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in MEMORY_TABLES
        }
    finally:
        connection.close()


def prepare_experiment(
    *,
    source_path: str | Path,
    chat_name: str,
    experiment_id: str,
    experiments_root: str | Path = "data/memory_experiments",
    production_memory_db: str | Path = "data/chat_memory.db",
    project_root: str | Path = ".",
) -> Dict[str, Any]:
    """Normalize one archive and create an isolated zero-call experiment."""
    source = Path(source_path).resolve()
    if not source.is_file():
        raise MemoryExperimentError(f"source file does not exist: {source}")
    normalized_chat_name = str(chat_name or "").strip()
    if not normalized_chat_name:
        raise MemoryExperimentError("chat_name is required")

    safe_id = _safe_experiment_id(experiment_id)
    # Keep writable paths relative when supplied that way. On case-insensitive
    # mounted workspaces, resolve() can change path casing and make a sandbox
    # treat the same directory as outside its writable root.
    root = Path(experiments_root)
    destination = root / safe_id
    if destination.exists():
        raise MemoryExperimentError(
            f"experiment already exists; refusing to overwrite: {destination}"
        )

    with source.open("r", encoding="utf-8") as source_file:
        payload = json.load(source_file)
    if not isinstance(payload, dict):
        raise MemoryExperimentError("source JSON root must be an object")
    source_format = str(payload.get("format") or "")
    if source_format != SUPPORTED_SOURCE_FORMAT:
        raise MemoryExperimentError(
            f"unsupported source format: {source_format or '(missing)'}"
        )
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise MemoryExperimentError("source messages must be an array")

    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".{safe_id}.preparing.{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        source_messages_path = staging / "source_messages.jsonl"
        memory_messages_path = staging / "memory_messages.jsonl"
        experiment_db_path = staging / "chat_memory.db"
        baseline_db_path = staging / "production_baseline.db"

        source_ids: set[str] = set()
        type_counts: Counter[str] = Counter()
        included_type_counts: Counter[str] = Counter()
        excluded_type_counts: Counter[str] = Counter()
        missing_sender_count = 0
        missing_content_count = 0
        earliest_time = ""
        latest_time = ""
        previous_time: datetime | None = None
        eligible_count = 0

        with (
            source_messages_path.open(
                "w",
                encoding="utf-8",
                newline="\n",
            ) as source_output,
            memory_messages_path.open(
                "w",
                encoding="utf-8",
                newline="\n",
            ) as memory_output,
        ):
            for source_index, raw in enumerate(raw_messages, start=1):
                if not isinstance(raw, dict):
                    raise MemoryExperimentError(
                        f"message {source_index} must be an object"
                    )
                source_id = str(raw.get("platformMessageId") or "").strip()
                if not source_id:
                    raise MemoryExperimentError(
                        f"message {source_index} has no platformMessageId"
                    )
                if source_id in source_ids:
                    raise MemoryExperimentError(
                        f"duplicate platformMessageId at message {source_index}"
                    )
                source_ids.add(source_id)

                time_text = str(raw.get("time") or "").strip()
                try:
                    parsed_time = datetime.fromisoformat(time_text)
                except ValueError as exc:
                    raise MemoryExperimentError(
                        f"message {source_index} has invalid ISO time"
                    ) from exc
                comparable_time = (
                    parsed_time.replace(tzinfo=None)
                    if parsed_time.tzinfo
                    else parsed_time
                )
                if previous_time is not None and comparable_time < previous_time:
                    raise MemoryExperimentError(
                        f"messages are not chronological at index {source_index}"
                    )
                previous_time = comparable_time
                earliest_time = earliest_time or time_text
                latest_time = time_text

                message_type = str(
                    raw.get("mappedTypeName")
                    or raw.get("chatlabTypeName")
                    or "其他/未知"
                ).strip()
                type_counts[message_type] += 1
                eligible = message_type not in OPAQUE_MEMORY_TYPES
                if eligible:
                    eligible_count += 1
                    included_type_counts[message_type] += 1
                else:
                    excluded_type_counts[message_type] += 1
                missing_sender_count += int(
                    not str(
                        raw.get("senderName") or raw.get("sender") or ""
                    ).strip()
                )
                missing_content_count += int(
                    not _normalize_content(raw.get("content"))
                )

                normalized = _normalized_message(
                    raw,
                    source_index=source_index,
                    memory_cursor=eligible_count if eligible else None,
                )
                source_output.write(
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                if eligible:
                    memory_output.write(
                        json.dumps(
                            normalized,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )

            for output in (source_output, memory_output):
                output.flush()
                os.fsync(output.fileno())

        MemoryStore(experiment_db_path)
        baseline_counts = _copy_chat_rows(
            Path(production_memory_db).resolve(),
            baseline_db_path,
            normalized_chat_name,
        )

        estimated_batches = (
            (eligible_count + 59) // 60 if eligible_count else 0
        )
        source_sha256 = _sha256_file(source)
        source_messages_sha256 = _sha256_file(source_messages_path)
        memory_messages_sha256 = _sha256_file(memory_messages_path)
        experiment_db_sha256 = _sha256_file(experiment_db_path)
        baseline_db_sha256 = _sha256_file(baseline_db_path)
        project = Path(project_root).resolve()
        prepared_at = _now()
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "experiment_id": safe_id,
            "chat_name": normalized_chat_name,
            "status": "prepared",
            "prepared_at": prepared_at,
            "isolation": {
                "production_memory_mutated": False,
                "production_chat_log_mutated": False,
                "rollback_strategy": "disable experiment; production remains intact",
            },
            "source": {
                "path": str(source),
                "format": source_format,
                "sha256": source_sha256,
                "size_bytes": source.stat().st_size,
                "message_count": len(raw_messages),
                "earliest_time": earliest_time,
                "latest_time": latest_time,
                "unique_message_id_count": len(source_ids),
            },
            "selection": {
                "policy_version": 1,
                "opaque_types_excluded_from_llm": sorted(OPAQUE_MEMORY_TYPES),
                "eligible_message_count": eligible_count,
                "excluded_message_count": len(raw_messages) - eligible_count,
                "type_counts": dict(sorted(type_counts.items())),
                "included_type_counts": dict(sorted(included_type_counts.items())),
                "excluded_type_counts": dict(sorted(excluded_type_counts.items())),
                "missing_sender_count": missing_sender_count,
                "missing_content_count": missing_content_count,
                "estimated_event_batches_at_60_messages": estimated_batches,
            },
            "artifacts": {
                "source_messages": {
                    "path": "source_messages.jsonl",
                    "sha256": source_messages_sha256,
                    "line_count": len(raw_messages),
                },
                "memory_messages": {
                    "path": "memory_messages.jsonl",
                    "sha256": memory_messages_sha256,
                    "line_count": eligible_count,
                },
                "experiment_memory_db": {
                    "path": "chat_memory.db",
                    "sha256": experiment_db_sha256,
                    "initial_counts": _db_counts(experiment_db_path),
                },
                "production_baseline_db": {
                    "path": "production_baseline.db",
                    "sha256": baseline_db_sha256,
                    "chat_row_counts": baseline_counts,
                },
            },
            "pipeline_hashes": _pipeline_hashes(project),
            "planned_memory_config": {
                "event_target_messages": 40,
                "event_max_messages": 60,
                "event_input_token_budget": 12000,
                "stage_event_threshold": 40,
                "stage_input_event_limit": 80,
                "stage_input_token_budget": 24000,
                "dedup_lookback_days": 30,
                "dedup_time_anchor": "candidate_event_time",
                "embedding_model": "BAAI/bge-small-zh-v1.5",
            },
            "billing": {
                "llm_calls_made": 0,
                "actual_cost_yuan": 0.0,
            },
        }
        _write_json(staging / "manifest.json", manifest)
        _write_json(
            staging / "run_state.json",
            {
                "schema_version": RUN_STATE_SCHEMA_VERSION,
                "experiment_id": safe_id,
                "status": "prepared",
                "next_memory_cursor": 1 if eligible_count else 0,
                "processed_messages": 0,
                "event_batches_completed": 0,
                "events_created": 0,
                "stage_updates": 0,
                "dedup_calls": 0,
                "llm_calls": 0,
                "actual_cost_yuan": 0.0,
                "last_error": "",
                "updated_at": prepared_at,
            },
        )
        os.replace(staging, destination)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_experiment(
    workspace: str | Path,
    *,
    allow_started: bool = False,
) -> Dict[str, Any]:
    """Verify immutable artifacts and the zero-call preparation checkpoint."""
    root = Path(workspace).resolve()
    manifest_path = root / "manifest.json"
    state_path = root / "run_state.json"
    if not manifest_path.is_file() or not state_path.is_file():
        raise MemoryExperimentError("manifest.json or run_state.json is missing")
    with manifest_path.open("r", encoding="utf-8") as source:
        manifest = json.load(source)
    with state_path.open("r", encoding="utf-8") as source:
        state = json.load(source)

    checks: Dict[str, bool] = {}
    for key in (
        "source_messages",
        "memory_messages",
        "experiment_memory_db",
        "production_baseline_db",
    ):
        artifact = manifest["artifacts"][key]
        path = root / artifact["path"]
        checks[f"{key}_exists"] = path.is_file()
        if key != "experiment_memory_db" or not allow_started:
            checks[f"{key}_sha256"] = (
                path.is_file() and _sha256_file(path) == artifact["sha256"]
            )
        if "line_count" in artifact:
            checks[f"{key}_line_count"] = (
                path.is_file() and _line_count(path) == artifact["line_count"]
            )

    experiment_counts = _db_counts(root / "chat_memory.db")
    if not allow_started:
        checks["experiment_db_empty"] = all(
            count == 0 for count in experiment_counts.values()
        )
        checks["zero_llm_calls"] = (
            int(manifest.get("billing", {}).get("llm_calls_made") or 0) == 0
            and int(state.get("llm_calls") or 0) == 0
        )
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise MemoryExperimentError(
            "experiment verification failed: " + ", ".join(failed)
        )
    return {
        "experiment_id": manifest["experiment_id"],
        "chat_name": manifest["chat_name"],
        "status": state["status"],
        "eligible_message_count": manifest["selection"][
            "eligible_message_count"
        ],
        "estimated_event_batches": manifest["selection"][
            "estimated_event_batches_at_60_messages"
        ],
        "experiment_db_counts": experiment_counts,
        "checks": checks,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or verify an isolated historical-memory experiment."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--source", required=True)
    prepare.add_argument("--chat-name", required=True)
    prepare.add_argument("--experiment-id", required=True)
    prepare.add_argument(
        "--experiments-root",
        default="data/memory_experiments",
    )
    prepare.add_argument(
        "--production-memory-db",
        default="data/chat_memory.db",
    )
    prepare.add_argument("--project-root", default=".")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--workspace", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_experiment(
                source_path=args.source,
                chat_name=args.chat_name,
                experiment_id=args.experiment_id,
                experiments_root=args.experiments_root,
                production_memory_db=args.production_memory_db,
                project_root=args.project_root,
            )
            summary = {
                "experiment_id": result["experiment_id"],
                "status": result["status"],
                "workspace": str(
                    Path(args.experiments_root).resolve()
                    / result["experiment_id"]
                ),
                "source_message_count": result["source"]["message_count"],
                "eligible_message_count": result["selection"][
                    "eligible_message_count"
                ],
                "estimated_event_batches": result["selection"][
                    "estimated_event_batches_at_60_messages"
                ],
                "llm_calls_made": result["billing"]["llm_calls_made"],
            }
        else:
            summary = verify_experiment(args.workspace)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (MemoryExperimentError, OSError, json.JSONDecodeError) as exc:
        print(f"memory experiment error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
