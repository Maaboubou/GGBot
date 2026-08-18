"""Promote an isolated historical-memory experiment into the live store."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.plugins.builtin_chatbot.memory_experiment import (
    MemoryExperimentError,
    verify_experiment,
)
from app.plugins.builtin_chatbot.memory_store import MemoryStore


class MemoryActivationError(RuntimeError):
    """Raised when activation cannot safely reach an atomic commit."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
    temp.replace(path)


def _backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise MemoryActivationError(f"backup already exists: {destination}")
    source_connection = sqlite3.connect(source, timeout=60)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.execute("PRAGMA busy_timeout=60000")
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _source_is_registered(
    database: Path,
    chat_name: str,
    source_namespace: str,
) -> bool:
    connection = sqlite3.connect(database, timeout=30)
    try:
        if not _table_exists(connection, "memory_sources"):
            return False
        return (
            connection.execute(
                """
                SELECT 1 FROM memory_sources
                WHERE chat_name = ? AND source_namespace = ?
                """,
                (chat_name, source_namespace),
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def _load_chat_bundle(database: Path, chat_name: str) -> Dict[str, Any]:
    connection = sqlite3.connect(database, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        event_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(memory_events)"
            ).fetchall()
        }
        events = []
        for row in connection.execute(
            "SELECT * FROM memory_events WHERE chat_name = ? ORDER BY id",
            (chat_name,),
        ).fetchall():
            value = dict(row)
            if "source_namespace" not in event_columns:
                value["source_namespace"] = "live_chat_log"
            events.append(value)
        state_row = connection.execute(
            "SELECT * FROM memory_state WHERE chat_name = ?",
            (chat_name,),
        ).fetchone()
        people = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM memory_people
                WHERE chat_name = ? ORDER BY person_name
                """,
                (chat_name,),
            ).fetchall()
        ]
        person_identities: List[Dict[str, Any]] = []
        person_aliases: List[Dict[str, Any]] = []
        person_facts: List[Dict[str, Any]] = []
        if _table_exists(connection, "memory_person_identities"):
            person_identities = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM memory_person_identities
                    WHERE chat_name = ? ORDER BY id
                    """,
                    (chat_name,),
                ).fetchall()
            ]
        if _table_exists(connection, "memory_person_aliases"):
            person_aliases = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM memory_person_aliases
                    WHERE chat_name = ? ORDER BY id
                    """,
                    (chat_name,),
                ).fetchall()
            ]
        if _table_exists(connection, "memory_person_facts"):
            person_facts = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM memory_person_facts
                    WHERE chat_name = ? ORDER BY id
                    """,
                    (chat_name,),
                ).fetchall()
            ]
        sources: List[Dict[str, Any]] = []
        if _table_exists(connection, "memory_sources"):
            sources = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM memory_sources
                    WHERE chat_name = ?
                    ORDER BY source_namespace
                    """,
                    (chat_name,),
                ).fetchall()
            ]
        event_messages: List[Dict[str, Any]] = []
        if _table_exists(connection, "memory_event_messages"):
            event_messages = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        source.id AS source_event_id,
                        message.ordinal,
                        message.log_cursor,
                        message.message_json,
                        message.created_at
                    FROM memory_event_messages AS message
                    JOIN memory_events AS source
                      ON source.id = message.event_id
                    WHERE source.chat_name = ?
                    ORDER BY source.id, message.ordinal
                    """,
                    (chat_name,),
                ).fetchall()
            ]
        return {
            "chat_name": chat_name,
            "events": events,
            "state": dict(state_row) if state_row is not None else None,
            "people": people,
            "person_identities": person_identities,
            "person_aliases": person_aliases,
            "person_facts": person_facts,
            "sources": sources,
            "event_messages": event_messages,
        }
    finally:
        connection.close()


def _replace_chat_bundle(
    target_database: Path,
    bundle: Dict[str, Any],
    *,
    source_namespace_override: str = "",
    source_cursor_override: Optional[int] = None,
    source_message_count_override: Optional[int] = None,
) -> Dict[str, int]:
    """Atomically replace one chat while preserving every other chat."""
    chat_name = str(bundle["chat_name"])
    connection = sqlite3.connect(target_database, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    old_to_new: Dict[int, int] = {}
    old_to_new_person: Dict[int, int] = {}
    old_to_new_fact: Dict[int, int] = {}
    MemoryStore(target_database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM memory_person_facts WHERE chat_name = ?",
            (chat_name,),
        )
        connection.execute(
            "DELETE FROM memory_person_aliases WHERE chat_name = ?",
            (chat_name,),
        )
        connection.execute(
            "DELETE FROM memory_person_identities WHERE chat_name = ?",
            (chat_name,),
        )
        connection.execute(
            "DELETE FROM memory_people WHERE chat_name = ?",
            (chat_name,),
        )
        connection.execute(
            """
            DELETE FROM memory_event_messages
            WHERE event_id IN(
                SELECT id FROM memory_events WHERE chat_name = ?
            )
            """,
            (chat_name,),
        )
        connection.execute(
            "DELETE FROM memory_events WHERE chat_name = ?",
            (chat_name,),
        )
        connection.execute(
            "DELETE FROM memory_state WHERE chat_name = ?",
            (chat_name,),
        )
        connection.execute(
            "DELETE FROM memory_sources WHERE chat_name = ?",
            (chat_name,),
        )

        for event in bundle.get("events") or []:
            cursor = connection.execute(
                """
                INSERT INTO memory_events(
                    chat_name, source_namespace,
                    source_start_cursor, source_end_cursor,
                    start_time, end_time, title, summary,
                    participants_json, keywords_json, opinions_json,
                    decisions_json, open_items_json, importance, card_json,
                    search_text, embedding, embedding_dim,
                    supersedes_event_id, superseded_by_event_id,
                    relation_reason, verification_status,
                    verification_note, created_at
                ) VALUES(
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    0, 0, ?, ?, ?, ?
                )
                """,
                (
                    chat_name,
                    source_namespace_override
                    or str(
                        event.get("source_namespace")
                        or "live_chat_log"
                    ),
                    int(event.get("source_start_cursor") or 0),
                    int(event.get("source_end_cursor") or 0),
                    event.get("start_time"),
                    event.get("end_time"),
                    str(event.get("title") or "未命名事件"),
                    str(event.get("summary") or ""),
                    str(event.get("participants_json") or "[]"),
                    str(event.get("keywords_json") or "[]"),
                    str(event.get("opinions_json") or "[]"),
                    str(event.get("decisions_json") or "[]"),
                    str(event.get("open_items_json") or "[]"),
                    float(event.get("importance") or 0.5),
                    str(event.get("card_json") or "{}"),
                    str(event.get("search_text") or ""),
                    event.get("embedding"),
                    int(event.get("embedding_dim") or 0),
                    str(event.get("relation_reason") or ""),
                    str(
                        event.get("verification_status")
                        or "not_required"
                    ),
                    str(event.get("verification_note") or ""),
                    str(event.get("created_at") or _now()),
                ),
            )
            old_to_new[int(event["id"])] = int(cursor.lastrowid)

        for event in bundle.get("events") or []:
            new_id = old_to_new[int(event["id"])]
            supersedes = old_to_new.get(
                int(event.get("supersedes_event_id") or 0),
                0,
            )
            superseded_by = old_to_new.get(
                int(event.get("superseded_by_event_id") or 0),
                0,
            )
            if supersedes or superseded_by:
                connection.execute(
                    """
                    UPDATE memory_events
                    SET supersedes_event_id = ?, superseded_by_event_id = ?
                    WHERE id = ?
                    """,
                    (supersedes, superseded_by, new_id),
                )

        for message in bundle.get("event_messages") or []:
            target_event_id = old_to_new.get(
                int(message.get("source_event_id") or 0)
            )
            if not target_event_id:
                continue
            connection.execute(
                """
                INSERT INTO memory_event_messages(
                    event_id, ordinal, log_cursor, message_json, created_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    target_event_id,
                    int(message.get("ordinal") or 0),
                    int(message.get("log_cursor") or 0),
                    str(message.get("message_json") or "{}"),
                    str(message.get("created_at") or _now()),
                ),
            )

        state = bundle.get("state")
        if state is not None:
            stage_source = old_to_new.get(
                int(state.get("stage_source_event_id") or 0),
                0,
            )
            connection.execute(
                """
                INSERT INTO memory_state(
                    chat_name, source_cursor, source_message_count,
                    stage_source_event_id, stage_summary, stage_json,
                    stage_updated_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_name,
                    int(
                        source_cursor_override
                        if source_cursor_override is not None
                        else state.get("source_cursor") or 0
                    ),
                    int(
                        source_message_count_override
                        if source_message_count_override is not None
                        else state.get("source_message_count") or 0
                    ),
                    stage_source,
                    str(state.get("stage_summary") or ""),
                    str(state.get("stage_json") or "{}"),
                    state.get("stage_updated_at"),
                    _now(),
                ),
            )

        for person in bundle.get("people") or []:
            connection.execute(
                """
                INSERT INTO memory_people(
                    chat_name, person_name, profile_json, profile_text,
                    source_event_id, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_name,
                    str(person.get("person_name") or ""),
                    str(person.get("profile_json") or "{}"),
                    str(person.get("profile_text") or ""),
                    old_to_new.get(
                        int(person.get("source_event_id") or 0),
                        0,
                    ),
                    str(person.get("updated_at") or _now()),
                ),
            )

        for identity in bundle.get("person_identities") or []:
            cursor = connection.execute(
                """
                INSERT INTO memory_person_identities(
                    chat_name, canonical_name, status,
                    merged_into_person_id, manual_lock,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_name,
                    str(identity.get("canonical_name") or ""),
                    str(identity.get("status") or "active"),
                    0,
                    int(identity.get("manual_lock") or 0),
                    str(identity.get("created_at") or _now()),
                    str(identity.get("updated_at") or _now()),
                ),
            )
            old_to_new_person[int(identity.get("id") or 0)] = int(
                cursor.lastrowid
            )
        for identity in bundle.get("person_identities") or []:
            merged_old = int(identity.get("merged_into_person_id") or 0)
            if merged_old:
                connection.execute(
                    """
                    UPDATE memory_person_identities
                    SET merged_into_person_id = ?
                    WHERE id = ?
                    """,
                    (
                        old_to_new_person.get(merged_old, 0),
                        old_to_new_person.get(int(identity.get("id") or 0), 0),
                    ),
                )
        for alias in bundle.get("person_aliases") or []:
            person_id = old_to_new_person.get(
                int(alias.get("person_id") or 0),
                0,
            )
            if not person_id:
                continue
            connection.execute(
                """
                INSERT INTO memory_person_aliases(
                    chat_name, person_id, alias_name, external_id,
                    source, confidence, status, first_seen_at,
                    last_seen_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_name,
                    person_id,
                    str(alias.get("alias_name") or ""),
                    str(alias.get("external_id") or ""),
                    str(alias.get("source") or "activation"),
                    float(alias.get("confidence") or 0.0),
                    str(alias.get("status") or "confirmed"),
                    alias.get("first_seen_at"),
                    alias.get("last_seen_at"),
                    str(alias.get("created_at") or _now()),
                    str(alias.get("updated_at") or _now()),
                ),
            )
        for fact in bundle.get("person_facts") or []:
            person_id = old_to_new_person.get(
                int(fact.get("person_id") or 0),
                0,
            )
            if not person_id:
                continue
            source_ids = [
                old_to_new.get(int(value), 0)
                for value in json.loads(
                    str(fact.get("source_event_ids_json") or "[]")
                )
                if old_to_new.get(int(value), 0)
            ]
            cursor = connection.execute(
                """
                INSERT INTO memory_person_facts(
                    chat_name, person_id, field_name, value,
                    normalized_value, fact_key, status, confidence,
                    valid_from, valid_to, observed_at,
                    first_seen_at, last_seen_at, temporal_note,
                    source_event_id, source_event_ids_json, evidence_json,
                    mention_count, manual_override,
                    superseded_by_fact_id, deleted_at,
                    created_at, updated_at
                ) VALUES(
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, 0, ?, ?, ?
                )
                """,
                (
                    chat_name,
                    person_id,
                    str(fact.get("field_name") or "other"),
                    str(fact.get("value") or ""),
                    str(fact.get("normalized_value") or ""),
                    str(fact.get("fact_key") or ""),
                    str(fact.get("status") or "uncertain"),
                    float(fact.get("confidence") or 0.0),
                    fact.get("valid_from"),
                    fact.get("valid_to"),
                    fact.get("observed_at"),
                    fact.get("first_seen_at"),
                    fact.get("last_seen_at"),
                    str(fact.get("temporal_note") or ""),
                    old_to_new.get(
                        int(fact.get("source_event_id") or 0),
                        0,
                    ),
                    json.dumps(source_ids, ensure_ascii=False),
                    str(fact.get("evidence_json") or "[]"),
                    int(fact.get("mention_count") or 1),
                    int(fact.get("manual_override") or 0),
                    fact.get("deleted_at"),
                    str(fact.get("created_at") or _now()),
                    str(fact.get("updated_at") or _now()),
                ),
            )
            old_to_new_fact[int(fact.get("id") or 0)] = int(
                cursor.lastrowid
            )
        for fact in bundle.get("person_facts") or []:
            superseded_old = int(fact.get("superseded_by_fact_id") or 0)
            if superseded_old:
                connection.execute(
                    """
                    UPDATE memory_person_facts
                    SET superseded_by_fact_id = ?
                    WHERE id = ?
                    """,
                    (
                        old_to_new_fact.get(superseded_old, 0),
                        old_to_new_fact.get(int(fact.get("id") or 0), 0),
                    ),
                )

        for source in bundle.get("sources") or []:
            connection.execute(
                """
                INSERT INTO memory_sources(
                    chat_name, source_namespace, source_type,
                    source_path, metadata_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_name,
                    str(source.get("source_namespace") or ""),
                    str(source.get("source_type") or ""),
                    str(source.get("source_path") or ""),
                    str(source.get("metadata_json") or "{}"),
                    str(source.get("created_at") or _now()),
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "events": len(bundle.get("events") or []),
        "people": len(bundle.get("people") or []),
        "person_identities": len(bundle.get("person_identities") or []),
        "person_aliases": len(bundle.get("person_aliases") or []),
        "person_facts": len(bundle.get("person_facts") or []),
        "sources": len(bundle.get("sources") or []),
        "event_messages": len(bundle.get("event_messages") or []),
    }


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                messages.append(value)
    return messages


def _last_jsonl_message(path: Path) -> Dict[str, Any]:
    last: Dict[str, Any] = {}
    with path.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                last = value
    if not last:
        raise MemoryActivationError(f"historical source is empty: {path}")
    return last


def _find_live_boundary(
    historical_messages: Path,
    live_log: Path,
) -> Dict[str, Any]:
    historical_last = _last_jsonl_message(historical_messages)
    live_messages = _read_jsonl(live_log)
    historic_content = str(historical_last.get("content") or "").strip()
    historic_time = _parse_time(historical_last.get("time"))
    candidates = []
    for index, message in enumerate(live_messages, start=1):
        if str(message.get("content") or "").strip() != historic_content:
            continue
        live_time = _parse_time(message.get("time"))
        delta = (
            abs((live_time - historic_time).total_seconds())
            if live_time is not None and historic_time is not None
            else float("inf")
        )
        candidates.append((delta, -index, index, message))
    if not candidates:
        raise MemoryActivationError(
            "cannot align the last historical message with the live chat log"
        )
    delta, _, cursor, live_message = min(candidates)
    if delta > 300:
        raise MemoryActivationError(
            "historical/live boundary content matched but timestamps differ "
            f"by {delta:.0f}s"
        )
    return {
        "alignment_mode": "matched_anchor",
        "physical_cursor": cursor,
        "historical_time": historical_last.get("time"),
        "live_time": live_message.get("time"),
        "time_delta_seconds": delta,
        "live_physical_count": len(live_messages),
    }


def _disjoint_live_boundary(
    historical_messages: Path,
    live_log: Path,
) -> Dict[str, Any]:
    """Treat the live log as a separate successor source with no shared cursor.

    This is intentionally opt-in. It is needed when an exported old group and
    a newly created replacement group overlap in wall-clock time but do not
    share messages. Every physical live-log row must then be backfilled.
    """

    historical_last = _last_jsonl_message(historical_messages)
    live_messages = _read_jsonl(live_log)
    first_live = live_messages[0] if live_messages else {}
    return {
        "alignment_mode": "disjoint_sources",
        "physical_cursor": 0,
        "historical_time": historical_last.get("time"),
        "live_time": None,
        "first_live_time": first_live.get("time"),
        "time_delta_seconds": None,
        "live_physical_count": len(live_messages),
    }


def _load_memory_config(chat_name: str) -> Dict[str, Any]:
    config_path = Path("app/plugins/builtin_chatbot/config.json")
    with config_path.open("r", encoding="utf-8") as source:
        document = json.load(source)
    config = dict(document.get("config") or {})

    permissions_db = Path("data/database.db")
    if permissions_db.is_file():
        connection = sqlite3.connect(permissions_db, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT p.memory_profile
                FROM wechat_users AS u
                JOIN user_permissions AS p ON p.user_id = u.id
                WHERE u.chat_name = ? AND p.plugin_name = 'builtin_chatbot'
                LIMIT 1
                """,
                (chat_name,),
            ).fetchone()
            if row and row["memory_profile"]:
                profile = json.loads(row["memory_profile"])
                if isinstance(profile, dict) and profile.get("enabled"):
                    overrides = profile.get("overrides")
                    if not isinstance(overrides, dict):
                        overrides = profile
                    config.update(
                        {
                            key: value
                            for key, value in overrides.items()
                            if value is not None
                        }
                    )
        finally:
            connection.close()
    config["memory_enabled"] = True
    config["memory_background_enabled"] = True
    config["memory_event_min_messages"] = 5
    config["memory_max_chunks_per_run"] = 20
    # Event activation commits the event/legacy-person bundle only. Person V3
    # has its own candidate and atomic activation path, so generating V3
    # observations here would spend model calls that are never promoted.
    config["memory_person_v3_enabled"] = False
    return config


def _backfill_candidate(
    candidate_database: Path,
    chat_name: str,
    *,
    minimum_message_count: int,
) -> Dict[str, Any]:
    import litellm

    # LiteLLM's aiohttp transport can outlive the configured request timeout
    # on interrupted Windows runs. The event experiment and person rebuild
    # already use the stable HTTPX path; activation backfill must match them.
    litellm.disable_aiohttp_transport = True

    from app.plugins.builtin_chatbot.chat_log import ChatLogManager
    from app.plugins.builtin_chatbot.context_manager import ChatContextManager
    from app.plugins.builtin_chatbot.memory_service import ChatMemoryService
    from app.services.llm_manager import LLMManager

    chat_log = ChatLogManager()
    manager = LLMManager(
        telemetry_dir=candidate_database.parent / "telemetry",
    )
    service = ChatMemoryService(
        chat_log,
        ChatContextManager(),
        store=MemoryStore(candidate_database),
        llm_manager=manager,
        llm_history_chat_name=chat_name,
    )
    passes: List[Dict[str, Any]] = []
    previous_count = -1

    def active_missing_embeddings() -> int:
        connection = sqlite3.connect(candidate_database, timeout=30)
        try:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM memory_events
                    WHERE chat_name = ?
                      AND superseded_by_event_id = 0
                      AND is_invalidated = 0
                      AND verification_status != 'quarantined'
                      AND (embedding IS NULL OR embedding_dim <= 0)
                    """,
                    (chat_name,),
                ).fetchone()[0]
            )
        finally:
            connection.close()

    try:
        config = _load_memory_config(chat_name)
        for pass_number in range(1, 10001):
            result = service.process_pending(
                chat_name,
                config,
                force_tail=True,
            )
            state = service.store.get_state(chat_name)
            current_count = int(state.get("source_message_count") or 0)
            pending = max(0, int(minimum_message_count) - current_count)
            missing_embeddings = active_missing_embeddings()
            passes.append(
                {
                    "pass": pass_number,
                    "result": result,
                    "source_message_count": current_count,
                    "pending_messages": pending,
                    "active_missing_embeddings": missing_embeddings,
                }
            )
            print(
                "memory activation backfill: "
                f"pass={pass_number} "
                f"chunks={int(result.get('chunks') or 0)} "
                f"events={int(result.get('events') or 0)} "
                f"pending={pending} "
                f"missing_embeddings={missing_embeddings}",
                flush=True,
            )
            if pending < 5 and missing_embeddings == 0:
                break
            if pending >= 5 and (
                int(result.get("chunks") or 0) <= 0
                or current_count <= previous_count
            ):
                raise MemoryActivationError(
                    "candidate gap backfill made no progress with "
                    f"{pending} messages pending"
                )
            if (
                pending < 5
                and missing_embeddings > 0
                and int(result.get("embedded") or 0) <= 0
            ):
                raise MemoryActivationError(
                    "candidate embedding drain made no progress with "
                    f"{missing_embeddings} active events pending"
                )
            previous_count = current_count
        else:
            raise MemoryActivationError(
                "candidate gap backfill exceeded 10000 bounded passes"
            )
    finally:
        service.close()
    if int(state.get("source_message_count") or 0) < minimum_message_count:
        pending = minimum_message_count - int(
            state.get("source_message_count") or 0
        )
        if pending >= 5:
            raise MemoryActivationError(
                f"candidate gap backfill stopped with {pending} messages pending"
            )
    return {
        "passes": passes,
        "result": {
            "chunks": sum(
                int(item["result"].get("chunks") or 0)
                for item in passes
            ),
            "events": sum(
                int(item["result"].get("events") or 0)
                for item in passes
            ),
            "embedded": sum(
                int(item["result"].get("embedded") or 0)
                for item in passes
            ),
            "stage": sum(
                int(item["result"].get("stage") or 0)
                for item in passes
            ),
        },
        "state": state,
    }


def _validate_database(
    database: Path,
    chat_name: str,
    *,
    history_namespace: str,
    expected_historical_events: int,
) -> Dict[str, Any]:
    connection = sqlite3.connect(database, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        counts = dict(
            connection.execute(
                """
                SELECT
                    COUNT(*) AS events,
                    SUM(CASE
                        WHEN superseded_by_event_id = 0
                         AND is_invalidated = 0
                         AND verification_status != 'quarantined'
                        THEN 1 ELSE 0
                    END)
                        AS active_events,
                    SUM(CASE WHEN embedding IS NOT NULL AND embedding_dim > 0
                             THEN 1 ELSE 0 END) AS embedded_events,
                    SUM(CASE WHEN source_namespace = ? THEN 1 ELSE 0 END)
                        AS historical_events
                FROM memory_events WHERE chat_name = ?
                """,
                (history_namespace, chat_name),
            ).fetchone()
        )
        people = int(
            connection.execute(
                "SELECT COUNT(*) FROM memory_people WHERE chat_name = ?",
                (chat_name,),
            ).fetchone()[0]
        )
        state = connection.execute(
            "SELECT * FROM memory_state WHERE chat_name = ?",
            (chat_name,),
        ).fetchone()
        dangling = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM memory_events AS event
                WHERE event.chat_name = ?
                  AND (
                    (
                      event.supersedes_event_id > 0
                      AND NOT EXISTS(
                        SELECT 1 FROM memory_events AS target
                        WHERE target.id = event.supersedes_event_id
                          AND target.chat_name = event.chat_name
                      )
                    )
                    OR
                    (
                      event.superseded_by_event_id > 0
                      AND NOT EXISTS(
                        SELECT 1 FROM memory_events AS target
                        WHERE target.id = event.superseded_by_event_id
                          AND target.chat_name = event.chat_name
                      )
                    )
                  )
                """,
                (chat_name,),
            ).fetchone()[0]
        )
        source = connection.execute(
            """
            SELECT * FROM memory_sources
            WHERE chat_name = ? AND source_namespace = ?
            """,
            (chat_name, history_namespace),
        ).fetchone()
        archived_messages = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM memory_event_messages AS message
                JOIN memory_events AS event ON event.id = message.event_id
                WHERE event.chat_name = ?
                """,
                (chat_name,),
            ).fetchone()[0]
        )
        if int(counts.get("historical_events") or 0) != int(
            expected_historical_events
        ):
            raise MemoryActivationError(
                "historical event count changed during promotion"
            )
        if state is None or source is None or dangling:
            raise MemoryActivationError(
                "candidate validation failed: missing state/source or "
                f"{dangling} dangling event relations"
            )
        source_path = Path(str(source["source_path"]))
        if not source_path.is_file():
            raise MemoryActivationError(
                f"registered historical source is missing: {source_path}"
            )
        return {
            **{key: int(value or 0) for key, value in counts.items()},
            "people": people,
            "archived_event_messages": archived_messages,
            "dangling_relations": dangling,
            "source_cursor": int(state["source_cursor"] or 0),
            "source_message_count": int(
                state["source_message_count"] or 0
            ),
            "stage_source_event_id": int(
                state["stage_source_event_id"] or 0
            ),
            "historical_source_path": str(source_path),
        }
    finally:
        connection.close()


def activate_experiment(
    workspace: str | Path,
    *,
    production_database: str | Path = "data/chat_memory.db",
    activations_root: str | Path = "data/memory_activations",
    allow_disjoint_live_log: bool = False,
) -> Dict[str, Any]:
    experiment = Path(workspace).resolve()
    production = Path(production_database).resolve()
    if not production.is_file():
        raise MemoryActivationError(f"production database missing: {production}")

    try:
        verification = verify_experiment(experiment, allow_started=True)
    except MemoryExperimentError as error:
        raise MemoryActivationError(str(error)) from error
    run_state = json.loads(
        (experiment / "run_state.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (experiment / "manifest.json").read_text(encoding="utf-8")
    )
    if run_state.get("status") != "complete":
        raise MemoryActivationError(
            f"experiment is not complete: {run_state.get('status')}"
        )

    experiment_id = str(manifest["experiment_id"])
    chat_name = str(manifest["chat_name"])
    history_namespace = f"history:{experiment_id}"
    if _source_is_registered(
        production,
        chat_name,
        history_namespace,
    ):
        raise MemoryActivationError(
            "this experiment is already registered in the production store"
        )
    activation_id = f"{experiment_id}-activated-{_safe_timestamp()}"
    activation = Path(activations_root).resolve() / activation_id
    activation.mkdir(parents=True, exist_ok=False)
    activation_manifest_path = activation / "activation.json"
    activation_manifest: Dict[str, Any] = {
        "schema_version": 1,
        "activation_id": activation_id,
        "experiment_id": experiment_id,
        "chat_name": chat_name,
        "history_namespace": history_namespace,
        "status": "preparing",
        "started_at": _now(),
        "experiment_workspace": str(experiment),
        "production_database": str(production),
        "allow_disjoint_live_log": bool(allow_disjoint_live_log),
        "verification": verification,
    }
    _atomic_json(activation_manifest_path, activation_manifest)

    snapshot = activation / "production_before.db"
    candidate = activation / "candidate.db"
    history_source = activation / "history_messages.jsonl"
    try:
        _backup_database(production, snapshot)
        shutil.copy2(snapshot, candidate)
        shutil.copy2(experiment / "memory_messages.jsonl", history_source)
        activation_manifest["artifacts"] = {
            "production_before": {
                "path": str(snapshot),
                "sha256": _sha256_file(snapshot),
            },
            "historical_source": {
                "path": str(history_source),
                "sha256": _sha256_file(history_source),
                "line_count": int(
                    manifest["selection"]["eligible_message_count"]
                ),
            },
            "candidate": {"path": str(candidate)},
        }

        live_log = Path("data/chat_logs") / f"{chat_name}.jsonl"
        boundary = (
            _disjoint_live_boundary(history_source, live_log)
            if allow_disjoint_live_log
            else _find_live_boundary(history_source, live_log)
        )
        from app.plugins.builtin_chatbot.chat_log import ChatLogManager

        chat_log = ChatLogManager()
        cumulative_count = int(chat_log.count_messages(chat_name))
        physical_count = int(chat_log.count_log_messages(chat_name))
        post_boundary = max(
            0,
            physical_count - int(boundary["physical_cursor"]),
        )
        boundary_cumulative = max(0, cumulative_count - post_boundary)
        boundary.update(
            {
                "live_physical_count_at_rebase": physical_count,
                "live_cumulative_count_at_rebase": cumulative_count,
                "boundary_cumulative_count": boundary_cumulative,
                "gap_messages": post_boundary,
            }
        )
        activation_manifest["boundary"] = boundary
        _atomic_json(activation_manifest_path, activation_manifest)

        MemoryStore(candidate)
        experiment_bundle = _load_chat_bundle(
            experiment / "chat_memory.db",
            chat_name,
        )
        experiment_bundle["sources"] = [
            {
                "chat_name": chat_name,
                "source_namespace": history_namespace,
                "source_type": "jsonl_memory",
                "source_path": str(history_source),
                "metadata_json": json.dumps(
                    {
                        "experiment_id": experiment_id,
                        "latest_time": manifest["source"]["latest_time"],
                        "message_count": manifest["selection"][
                            "eligible_message_count"
                        ],
                        "sha256": _sha256_file(history_source),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "created_at": _now(),
            }
        ]
        _replace_chat_bundle(
            candidate,
            experiment_bundle,
            source_namespace_override=history_namespace,
            source_cursor_override=int(boundary["physical_cursor"]),
            source_message_count_override=boundary_cumulative,
        )
        backfill = _backfill_candidate(
            candidate,
            chat_name,
            minimum_message_count=cumulative_count,
        )
        candidate_validation = _validate_database(
            candidate,
            chat_name,
            history_namespace=history_namespace,
            expected_historical_events=int(run_state["events"]),
        )
        activation_manifest.update(
            {
                "status": "candidate_ready",
                "backfill": backfill,
                "candidate_validation": candidate_validation,
            }
        )
        _atomic_json(activation_manifest_path, activation_manifest)

        # Schema migration happens only after the candidate is fully ready.
        MemoryStore(production)
        candidate_bundle = _load_chat_bundle(candidate, chat_name)
        committed = _replace_chat_bundle(production, candidate_bundle)
        production_validation = _validate_database(
            production,
            chat_name,
            history_namespace=history_namespace,
            expected_historical_events=int(run_state["events"]),
        )
        activation_manifest.update(
            {
                "status": "active",
                "committed": committed,
                "production_validation": production_validation,
                "activated_at": _now(),
                "rollback": {
                    "available": True,
                    "snapshot": str(snapshot),
                    "command": (
                        f"{sys.executable} -m "
                        "app.plugins.builtin_chatbot.memory_activation "
                        f"rollback --activation \"{activation}\""
                    ),
                },
            }
        )
        _atomic_json(activation_manifest_path, activation_manifest)
        return activation_manifest
    except Exception as error:
        activation_manifest.update(
            {
                "status": "failed",
                "failed_at": _now(),
                "error": f"{type(error).__name__}: {error}",
            }
        )
        _atomic_json(activation_manifest_path, activation_manifest)
        raise


def resume_activation(
    activation_workspace: str | Path,
) -> Dict[str, Any]:
    """Resume an interrupted candidate backfill and commit it atomically."""

    activation = Path(activation_workspace).resolve()
    manifest_path = activation / "activation.json"
    if not manifest_path.is_file():
        raise MemoryActivationError("activation.json is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") == "active":
        return manifest
    if manifest.get("status") not in {
        "preparing",
        "failed",
        "candidate_ready",
    }:
        raise MemoryActivationError(
            f"activation cannot be resumed: {manifest.get('status')}"
        )

    production = Path(str(manifest["production_database"]))
    candidate = Path(
        str(manifest.get("artifacts", {}).get("candidate", {}).get("path") or "")
    )
    history_source = Path(
        str(
            manifest.get("artifacts", {})
            .get("historical_source", {})
            .get("path")
            or ""
        )
    )
    snapshot = Path(
        str(
            manifest.get("artifacts", {})
            .get("production_before", {})
            .get("path")
            or ""
        )
    )
    chat_name = str(manifest["chat_name"])
    history_namespace = str(manifest["history_namespace"])
    expected_historical_events = int(
        manifest.get("verification", {})
        .get("experiment_db_counts", {})
        .get("memory_events")
        or 0
    )
    for label, path in (
        ("production database", production),
        ("candidate database", candidate),
        ("historical source", history_source),
        ("rollback snapshot", snapshot),
    ):
        if not path.is_file():
            raise MemoryActivationError(f"{label} is missing: {path}")
    if not expected_historical_events:
        raise MemoryActivationError(
            "activation manifest has no expected historical event count"
        )

    candidate_connection = sqlite3.connect(candidate, timeout=60)
    try:
        integrity = str(
            candidate_connection.execute("PRAGMA quick_check").fetchone()[0]
        )
    finally:
        candidate_connection.close()
    if integrity != "ok":
        raise MemoryActivationError(
            f"candidate database quick_check failed: {integrity}"
        )

    manifest.update(
        {
            "status": "preparing",
            "resumed_at": _now(),
            "resume_count": int(manifest.get("resume_count") or 0) + 1,
        }
    )
    manifest.pop("failed_at", None)
    manifest.pop("error", None)
    _atomic_json(manifest_path, manifest)

    try:
        if _source_is_registered(
            production,
            chat_name,
            history_namespace,
        ):
            production_validation = _validate_database(
                production,
                chat_name,
                history_namespace=history_namespace,
                expected_historical_events=expected_historical_events,
            )
            manifest.update(
                {
                    "status": "active",
                    "production_validation": production_validation,
                    "activated_at": manifest.get("activated_at") or _now(),
                    "recovered_committed_activation": True,
                }
            )
            _atomic_json(manifest_path, manifest)
            return manifest

        from app.plugins.builtin_chatbot.chat_log import ChatLogManager

        chat_log = ChatLogManager()
        cumulative_count = int(chat_log.count_messages(chat_name))
        physical_count = int(chat_log.count_log_messages(chat_name))
        manifest["resume_boundary"] = {
            "live_physical_count": physical_count,
            "live_cumulative_count": cumulative_count,
        }

        if manifest.get("status") != "candidate_ready":
            backfill = _backfill_candidate(
                candidate,
                chat_name,
                minimum_message_count=cumulative_count,
            )
            candidate_validation = _validate_database(
                candidate,
                chat_name,
                history_namespace=history_namespace,
                expected_historical_events=expected_historical_events,
            )
            manifest.update(
                {
                    "status": "candidate_ready",
                    "backfill": backfill,
                    "candidate_validation": candidate_validation,
                }
            )
            _atomic_json(manifest_path, manifest)

        MemoryStore(production)
        candidate_bundle = _load_chat_bundle(candidate, chat_name)
        committed = _replace_chat_bundle(production, candidate_bundle)
        production_validation = _validate_database(
            production,
            chat_name,
            history_namespace=history_namespace,
            expected_historical_events=expected_historical_events,
        )
        manifest.update(
            {
                "status": "active",
                "committed": committed,
                "production_validation": production_validation,
                "activated_at": _now(),
                "rollback": {
                    "available": True,
                    "snapshot": str(snapshot),
                    "command": (
                        f"{sys.executable} -m "
                        "app.plugins.builtin_chatbot.memory_activation "
                        f"rollback --activation \"{activation}\""
                    ),
                },
            }
        )
        _atomic_json(manifest_path, manifest)
        return manifest
    except Exception as error:
        manifest.update(
            {
                "status": "failed",
                "failed_at": _now(),
                "error": f"{type(error).__name__}: {error}",
            }
        )
        _atomic_json(manifest_path, manifest)
        raise


def rollback_activation(
    activation_workspace: str | Path,
) -> Dict[str, Any]:
    activation = Path(activation_workspace).resolve()
    manifest_path = activation / "activation.json"
    if not manifest_path.is_file():
        raise MemoryActivationError("activation.json is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "active":
        raise MemoryActivationError(
            f"activation is not active: {manifest.get('status')}"
        )
    production = Path(str(manifest["production_database"]))
    snapshot = Path(str(manifest["rollback"]["snapshot"]))
    chat_name = str(manifest["chat_name"])
    if not snapshot.is_file():
        raise MemoryActivationError(f"rollback snapshot missing: {snapshot}")
    expected_sha256 = str(
        manifest.get("artifacts", {})
        .get("production_before", {})
        .get("sha256")
        or ""
    )
    if expected_sha256 and _sha256_file(snapshot) != expected_sha256:
        raise MemoryActivationError("rollback snapshot checksum mismatch")

    rollback_backup = activation / f"production_before_rollback_{_safe_timestamp()}.db"
    _backup_database(production, rollback_backup)
    MemoryStore(production)
    snapshot_bundle = _load_chat_bundle(snapshot, chat_name)
    restored = _replace_chat_bundle(production, snapshot_bundle)
    manifest.update(
        {
            "status": "rolled_back",
            "rolled_back_at": _now(),
            "rollback": {
                **dict(manifest.get("rollback") or {}),
                "available": False,
                "restored": restored,
                "pre_rollback_backup": str(rollback_backup),
            },
        }
    )
    _atomic_json(manifest_path, manifest)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Activate or roll back an isolated memory experiment."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    activate = subparsers.add_parser("activate")
    activate.add_argument("--workspace", required=True)
    activate.add_argument(
        "--production-db",
        default="data/chat_memory.db",
    )
    activate.add_argument(
        "--activations-root",
        default="data/memory_activations",
    )
    activate.add_argument(
        "--allow-disjoint-live-log",
        action="store_true",
        help=(
            "treat every live-log row as post-history data when the historical "
            "export and current chat are separate old/new groups"
        ),
    )
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--activation", required=True)
    resume = subparsers.add_parser("resume")
    resume.add_argument("--activation", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--activation", required=True)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    if arguments.command == "activate":
        result = activate_experiment(
            arguments.workspace,
            production_database=arguments.production_db,
            activations_root=arguments.activations_root,
            allow_disjoint_live_log=arguments.allow_disjoint_live_log,
        )
    elif arguments.command == "rollback":
        result = rollback_activation(arguments.activation)
    elif arguments.command == "resume":
        result = resume_activation(arguments.activation)
    else:
        path = Path(arguments.activation).resolve() / "activation.json"
        result = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
