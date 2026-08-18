"""SQLite persistence for event-based conversational memory."""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

import numpy as np


PERSON_FACT_FIELDS = {
    "identity",
    "group_role",
    "occupation",
    "employer",
    "education",
    "location",
    "family",
    "relationship",
    "health",
    "preference",
    "interest",
    "skill",
    "asset",
    "experience",
    "habit",
    "plan",
    "current_status",
    "legacy_summary",
    "other",
}
PERSON_FACT_STATUSES = {
    "current",
    "historical",
    "planned",
    "uncertain",
    "disputed",
}
PERSON_FIELD_LABELS = {
    "identity": "身份信息",
    "group_role": "群内角色",
    "occupation": "职业",
    "employer": "单位",
    "education": "教育经历",
    "location": "地点",
    "family": "家庭",
    "relationship": "人物关系",
    "health": "健康",
    "preference": "偏好",
    "interest": "兴趣",
    "skill": "技能",
    "asset": "设备与资产",
    "experience": "经历",
    "habit": "习惯",
    "plan": "计划",
    "current_status": "当前状态",
    "legacy_summary": "旧版资料",
    "other": "其他",
}

PERSON_V3_CHAT_TABLES = (
    "memory_person_claim_candidates",
    "memory_person_pipeline_state",
    "memory_person_message_links",
    "memory_person_source_messages",
    "memory_person_v3_suppressions",
    "memory_person_snapshots",
    "memory_person_period_summaries",
    "memory_person_relationships",
    "memory_person_patterns",
    "memory_person_fact_versions",
    "memory_person_refresh_state",
    "memory_person_observations",
    "memory_person_v3_state",
)

PERSON_LEGACY_CHAT_TABLES = (
    "memory_people",
    "memory_person_facts",
    "memory_person_aliases",
    "memory_person_identities",
    "memory_person_audit",
    "memory_person_merge_artifacts",
)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class MemoryStore:
    CORE_SCHEMA_VERSION = 3
    _schema_locks_guard = threading.Lock()
    _schema_locks: Dict[str, threading.RLock] = {}

    _PERSON_SENDER_SOURCES = frozenset(
        {
            "message",
            "live_message",
            "live_sender_remark",
            "historical_message",
            "historical_sender_id",
        }
    )

    """Small, thread-safe SQLite store with vectors kept as float32 blobs."""

    def __init__(self, path: str | Path = "data/chat_memory.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path = str(self.path.resolve())
        with self._schema_locks_guard:
            self._schema_lock = self._schema_locks.setdefault(
                resolved_path,
                threading.RLock(),
            )
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def backup_database(self, destination: str | Path) -> Dict[str, Any]:
        """Create a transactionally consistent SQLite backup.

        SQLite's online backup API is safe while WAL readers/writers are active,
        unlike copying the database file directly.
        """
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(f"backup already exists: {target}")
        source_connection = self._connect()
        target_connection = sqlite3.connect(target, timeout=30)
        try:
            source_connection.backup(target_connection)
            target_connection.execute("PRAGMA optimize")
            target_connection.commit()
            check = target_connection.execute("PRAGMA quick_check").fetchone()
            quick_check = str(check[0] if check else "unknown")
            if quick_check.lower() != "ok":
                raise sqlite3.DatabaseError(
                    f"backup integrity check failed: {quick_check}"
                )
        except Exception:
            target_connection.close()
            source_connection.close()
            target.unlink(missing_ok=True)
            raise
        finally:
            try:
                target_connection.close()
            except Exception:
                pass
            try:
                source_connection.close()
            except Exception:
                pass
        return {
            "filename": target.name,
            "path": str(target),
            "bytes": int(target.stat().st_size),
            "quick_check": quick_check,
            "created_at": self._now(),
        }

    def _ensure_schema(self) -> None:
        with self._schema_lock, self._connection() as connection:
            # Journal mode is persistent. Configure it during schema setup
            # instead of negotiating WAL again on every read-only request.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_schema_meta (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            schema_row = connection.execute(
                """
                SELECT version FROM memory_schema_meta
                WHERE component = 'core'
                """
            ).fetchone()
            if (
                schema_row is not None
                and int(schema_row["version"] or 0) >= self.CORE_SCHEMA_VERSION
            ):
                return
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_state (
                    chat_name TEXT PRIMARY KEY,
                    source_cursor INTEGER NOT NULL DEFAULT 0,
                    source_message_count INTEGER NOT NULL DEFAULT 0,
                    stage_source_event_id INTEGER NOT NULL DEFAULT 0,
                    stage_summary TEXT NOT NULL DEFAULT '',
                    stage_json TEXT NOT NULL DEFAULT '{}',
                    stage_mode TEXT NOT NULL DEFAULT 'auto',
                    stage_manual_note TEXT NOT NULL DEFAULT '',
                    stage_manual_updated_at TEXT,
                    stage_updated_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    source_namespace TEXT NOT NULL DEFAULT 'live_chat_log',
                    source_start_cursor INTEGER NOT NULL,
                    source_end_cursor INTEGER NOT NULL,
                    start_time TEXT,
                    end_time TEXT,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    participants_json TEXT NOT NULL DEFAULT '[]',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    opinions_json TEXT NOT NULL DEFAULT '[]',
                    decisions_json TEXT NOT NULL DEFAULT '[]',
                    open_items_json TEXT NOT NULL DEFAULT '[]',
                    importance REAL NOT NULL DEFAULT 0.5,
                    card_json TEXT NOT NULL DEFAULT '{}',
                    search_text TEXT NOT NULL,
                    embedding BLOB,
                    embedding_dim INTEGER NOT NULL DEFAULT 0,
                    supersedes_event_id INTEGER NOT NULL DEFAULT 0,
                    superseded_by_event_id INTEGER NOT NULL DEFAULT 0,
                    relation_reason TEXT NOT NULL DEFAULT '',
                    verification_status TEXT NOT NULL DEFAULT 'not_required',
                    verification_note TEXT NOT NULL DEFAULT '',
                    is_invalidated INTEGER NOT NULL DEFAULT 0,
                    manual_revision INTEGER NOT NULL DEFAULT 0,
                    correction_id INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(
                        chat_name, source_namespace,
                        source_start_cursor, source_end_cursor, title
                    )
                );

                CREATE INDEX IF NOT EXISTS idx_memory_events_chat_id
                    ON memory_events(chat_name, id);
                CREATE INDEX IF NOT EXISTS idx_memory_events_chat_time
                    ON memory_events(chat_name, end_time);

                CREATE TABLE IF NOT EXISTS memory_people (
                    chat_name TEXT NOT NULL,
                    person_name TEXT NOT NULL,
                    profile_json TEXT NOT NULL DEFAULT '{}',
                    profile_text TEXT NOT NULL DEFAULT '',
                    source_event_id INTEGER NOT NULL DEFAULT 0,
                    manual_override INTEGER NOT NULL DEFAULT 0,
                    manual_note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_name, person_name)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_people_chat
                    ON memory_people(chat_name);

                CREATE TABLE IF NOT EXISTS memory_person_identities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    canonical_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    merged_into_person_id INTEGER NOT NULL DEFAULT 0,
                    manual_lock INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_name, canonical_name)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_person_identities_chat
                    ON memory_person_identities(chat_name, status, id);

                CREATE TABLE IF NOT EXISTS memory_person_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL,
                    alias_name TEXT NOT NULL,
                    external_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'automatic',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    first_seen_at TEXT,
                    last_seen_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_name, person_id, alias_name)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_person_aliases_name
                    ON memory_person_aliases(chat_name, alias_name, status);
                CREATE INDEX IF NOT EXISTS idx_memory_person_aliases_person
                    ON memory_person_aliases(person_id, status);
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_memory_person_aliases_external_confirmed
                    ON memory_person_aliases(chat_name, external_id)
                    WHERE external_id != '' AND status = 'confirmed';

                CREATE TABLE IF NOT EXISTS memory_person_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL,
                    field_name TEXT NOT NULL,
                    value TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'current',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    valid_from TEXT,
                    valid_to TEXT,
                    observed_at TEXT,
                    first_seen_at TEXT,
                    last_seen_at TEXT,
                    temporal_note TEXT NOT NULL DEFAULT '',
                    source_event_id INTEGER NOT NULL DEFAULT 0,
                    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    mention_count INTEGER NOT NULL DEFAULT 1,
                    manual_override INTEGER NOT NULL DEFAULT 0,
                    superseded_by_fact_id INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_name, person_id, fact_key)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_person_facts_person
                    ON memory_person_facts(person_id, status, deleted_at, id);
                CREATE INDEX IF NOT EXISTS idx_memory_person_facts_chat
                    ON memory_person_facts(chat_name, field_name, status);
                CREATE INDEX IF NOT EXISTS idx_memory_person_facts_event
                    ON memory_person_facts(source_event_id);

                CREATE TABLE IF NOT EXISTS memory_person_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    affected_person_ids_json TEXT NOT NULL DEFAULT '[]',
                    before_json TEXT NOT NULL DEFAULT '{}',
                    after_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    reverted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_memory_person_audit_chat
                    ON memory_person_audit(chat_name, status, id);

                CREATE TABLE IF NOT EXISTS memory_person_merge_artifacts (
                    chat_name TEXT NOT NULL,
                    audit_id INTEGER NOT NULL,
                    artifact_type TEXT NOT NULL,
                    artifact_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(audit_id, artifact_type, artifact_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_person_merge_artifacts_chat
                    ON memory_person_merge_artifacts(chat_name, audit_id);

                CREATE TABLE IF NOT EXISTS memory_sources (
                    chat_name TEXT NOT NULL,
                    source_namespace TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_path TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(chat_name, source_namespace)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_sources_chat
                    ON memory_sources(chat_name);

                CREATE TABLE IF NOT EXISTS memory_event_messages (
                    event_id INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    log_cursor INTEGER NOT NULL DEFAULT 0,
                    message_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(event_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_event_messages_event
                    ON memory_event_messages(event_id, ordinal);

                CREATE TABLE IF NOT EXISTS memory_corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    target_event_id INTEGER NOT NULL DEFAULT 0,
                    replacement_event_id INTEGER NOT NULL DEFAULT 0,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    false_claims_json TEXT NOT NULL DEFAULT '[]',
                    corrected_claim TEXT NOT NULL DEFAULT '',
                    affected_people_json TEXT NOT NULL DEFAULT '[]',
                    before_json TEXT NOT NULL DEFAULT '{}',
                    after_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    reverted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_memory_corrections_chat
                    ON memory_corrections(chat_name, status, id);

                CREATE TABLE IF NOT EXISTS memory_stage_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    before_json TEXT NOT NULL DEFAULT '{}',
                    after_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    reverted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_memory_stage_audit_chat
                    ON memory_stage_audit(chat_name, status, id);

                CREATE TABLE IF NOT EXISTS memory_maintenance_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_maintenance_audit_chat
                    ON memory_maintenance_audit(chat_name, id);
                """
            )
            # Existing installations need an additive migration because
            # CREATE TABLE IF NOT EXISTS does not add newly declared columns.
            event_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(memory_events)"
                ).fetchall()
            }
            for column_name, definition in (
                (
                    "source_namespace",
                    "TEXT NOT NULL DEFAULT 'live_chat_log'",
                ),
                ("supersedes_event_id", "INTEGER NOT NULL DEFAULT 0"),
                ("superseded_by_event_id", "INTEGER NOT NULL DEFAULT 0"),
                ("relation_reason", "TEXT NOT NULL DEFAULT ''"),
                (
                    "verification_status",
                    "TEXT NOT NULL DEFAULT 'not_required'",
                ),
                ("verification_note", "TEXT NOT NULL DEFAULT ''"),
                ("is_invalidated", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("correction_id", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column_name not in event_columns:
                    connection.execute(
                        f"ALTER TABLE memory_events ADD COLUMN {column_name} {definition}"
                    )
            people_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(memory_people)"
                ).fetchall()
            }
            for column_name, definition in (
                ("manual_override", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_note", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column_name not in people_columns:
                    connection.execute(
                        f"ALTER TABLE memory_people ADD COLUMN {column_name} {definition}"
                    )
            state_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(memory_state)"
                ).fetchall()
            }
            for column_name, definition in (
                ("stage_mode", "TEXT NOT NULL DEFAULT 'auto'"),
                ("stage_manual_note", "TEXT NOT NULL DEFAULT ''"),
                ("stage_manual_updated_at", "TEXT"),
            ):
                if column_name not in state_columns:
                    connection.execute(
                        f"ALTER TABLE memory_state ADD COLUMN {column_name} {definition}"
                    )
            self._ensure_source_aware_event_unique(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_events_chat_id
                ON memory_events(chat_name, id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_events_chat_time
                ON memory_events(chat_name, end_time)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_events_chat_source
                ON memory_events(chat_name, source_namespace, id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_events_chat_active
                ON memory_events(
                    chat_name, is_invalidated, verification_status,
                    superseded_by_event_id, id
                )
                """
            )
            self._migrate_legacy_people(connection)
            connection.execute(
                """
                INSERT INTO memory_schema_meta(component, version, updated_at)
                VALUES('core', ?, ?)
                ON CONFLICT(component) DO UPDATE SET
                    version = excluded.version,
                    updated_at = excluded.updated_at
                """,
                (self.CORE_SCHEMA_VERSION, self._now()),
            )

    def schema_versions(self) -> Dict[str, int]:
        """Return installed memory schema component versions."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT component, version
                FROM memory_schema_meta
                ORDER BY component
                """
            ).fetchall()
        return {
            str(row["component"]): int(row["version"] or 0)
            for row in rows
        }

    def schema_component_version(self, component: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT version FROM memory_schema_meta
                WHERE component = ?
                """,
                (str(component),),
            ).fetchone()
        return int(row["version"] or 0) if row is not None else 0

    def set_schema_component_version(
        self,
        component: str,
        version: int,
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        def write(target: sqlite3.Connection) -> None:
            target.execute(
                """
                INSERT INTO memory_schema_meta(component, version, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(component) DO UPDATE SET
                    version = excluded.version,
                    updated_at = excluded.updated_at
                """,
                (str(component), max(0, int(version)), self._now()),
            )

        if connection is not None:
            write(connection)
            return
        with self._connection() as owned_connection:
            write(owned_connection)

    @staticmethod
    def _ensure_source_aware_event_unique(
        connection: sqlite3.Connection,
    ) -> None:
        expected = [
            "chat_name",
            "source_namespace",
            "source_start_cursor",
            "source_end_cursor",
            "title",
        ]
        unique_indexes = []
        for index in connection.execute(
            "PRAGMA index_list(memory_events)"
        ).fetchall():
            if not int(index["unique"] or 0):
                continue
            columns = [
                str(row["name"])
                for row in connection.execute(
                    f'PRAGMA index_info("{index["name"]}")'
                ).fetchall()
            ]
            unique_indexes.append(columns)
        if expected in unique_indexes:
            return

        connection.executescript(
            """
            CREATE TABLE memory_events_source_aware (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_name TEXT NOT NULL,
                source_namespace TEXT NOT NULL DEFAULT 'live_chat_log',
                source_start_cursor INTEGER NOT NULL,
                source_end_cursor INTEGER NOT NULL,
                start_time TEXT,
                end_time TEXT,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                participants_json TEXT NOT NULL DEFAULT '[]',
                keywords_json TEXT NOT NULL DEFAULT '[]',
                opinions_json TEXT NOT NULL DEFAULT '[]',
                decisions_json TEXT NOT NULL DEFAULT '[]',
                open_items_json TEXT NOT NULL DEFAULT '[]',
                importance REAL NOT NULL DEFAULT 0.5,
                card_json TEXT NOT NULL DEFAULT '{}',
                search_text TEXT NOT NULL,
                embedding BLOB,
                embedding_dim INTEGER NOT NULL DEFAULT 0,
                supersedes_event_id INTEGER NOT NULL DEFAULT 0,
                superseded_by_event_id INTEGER NOT NULL DEFAULT 0,
                relation_reason TEXT NOT NULL DEFAULT '',
                verification_status TEXT NOT NULL DEFAULT 'not_required',
                verification_note TEXT NOT NULL DEFAULT '',
                is_invalidated INTEGER NOT NULL DEFAULT 0,
                manual_revision INTEGER NOT NULL DEFAULT 0,
                correction_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(
                    chat_name, source_namespace,
                    source_start_cursor, source_end_cursor, title
                )
            );

            INSERT OR IGNORE INTO memory_events_source_aware(
                id, chat_name, source_namespace,
                source_start_cursor, source_end_cursor,
                start_time, end_time, title, summary,
                participants_json, keywords_json, opinions_json,
                decisions_json, open_items_json, importance, card_json,
                search_text, embedding, embedding_dim,
                supersedes_event_id, superseded_by_event_id,
                relation_reason, verification_status, verification_note,
                is_invalidated, manual_revision,
                correction_id, created_at
            )
            SELECT
                id, chat_name, source_namespace,
                source_start_cursor, source_end_cursor,
                start_time, end_time, title, summary,
                participants_json, keywords_json, opinions_json,
                decisions_json, open_items_json, importance, card_json,
                search_text, embedding, embedding_dim,
                supersedes_event_id, superseded_by_event_id,
                relation_reason, verification_status, verification_note,
                is_invalidated, manual_revision,
                correction_id, created_at
            FROM memory_events
            ORDER BY id;

            DROP TABLE memory_events;
            ALTER TABLE memory_events_source_aware RENAME TO memory_events;
            """
        )

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _normalize_person_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

    @classmethod
    def is_non_person_sender(
        cls,
        chat_name: str,
        sender_name: Any,
        sender_external_id: Any = "",
    ) -> bool:
        name = str(sender_name or "").strip()
        external_id = str(sender_external_id or "").strip()
        if not name or name.casefold() in {"系统", "system"}:
            return True
        if external_id.casefold().endswith("@chatroom"):
            return True
        return (
            bool(str(chat_name or "").strip())
            and name.casefold() == str(chat_name).strip().casefold()
        )

    @classmethod
    def _person_fact_key(
        cls,
        field_name: str,
        value: Any,
        *,
        valid_from: Any = "",
        valid_to: Any = "",
    ) -> str:
        field = str(field_name or "other").strip().lower()
        if field == "legacy_summary":
            return "legacy_summary"
        return "|".join(
            (
                field,
                cls._normalize_person_text(value),
                str(valid_from or "").strip(),
                str(valid_to or "").strip(),
            )
        )

    def _ensure_person_identity(
        self,
        connection: sqlite3.Connection,
        chat_name: str,
        name: str,
        *,
        external_id: str = "",
        source: str = "automatic",
        confidence: float = 1.0,
        observed_at: str = "",
        alias_status: str = "confirmed",
    ) -> int:
        canonical_name = str(name or "").strip()
        if not canonical_name:
            return 0
        stable_id = str(external_id or "").strip()
        person_id = 0
        if stable_id:
            row = connection.execute(
                """
                SELECT person_id FROM memory_person_aliases
                WHERE chat_name = ? AND external_id = ?
                  AND status = 'confirmed'
                ORDER BY id LIMIT 1
                """,
                (chat_name, stable_id),
            ).fetchone()
            if row is not None:
                person_id = int(row["person_id"])
        if not person_id:
            rows = connection.execute(
                """
                SELECT DISTINCT person_id FROM memory_person_aliases
                WHERE chat_name = ? AND alias_name = ?
                  AND status = 'confirmed'
                ORDER BY person_id
                """,
                (chat_name, canonical_name),
            ).fetchall()
            if len(rows) == 1:
                person_id = int(rows[0]["person_id"])
        if not person_id:
            row = connection.execute(
                """
                SELECT id FROM memory_person_identities
                WHERE chat_name = ? AND canonical_name = ?
                """,
                (chat_name, canonical_name),
            ).fetchone()
            if row is not None:
                person_id = int(row["id"])
        now = self._now()
        if not person_id:
            cursor = connection.execute(
                """
                INSERT INTO memory_person_identities(
                    chat_name, canonical_name, status,
                    merged_into_person_id, manual_lock,
                    created_at, updated_at
                ) VALUES(?, ?, 'active', 0, 0, ?, ?)
                """,
                (chat_name, canonical_name, now, now),
            )
            person_id = int(cursor.lastrowid)
        else:
            connection.execute(
                """
                UPDATE memory_person_identities
                SET updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (now, person_id),
            )
        first_seen = str(observed_at or "").strip() or None
        existing = connection.execute(
            """
            SELECT * FROM memory_person_aliases
            WHERE chat_name = ? AND person_id = ? AND alias_name = ?
            """,
            (chat_name, person_id, canonical_name),
        ).fetchone()
        if existing is None:
            try:
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
                        canonical_name,
                        stable_id,
                        str(source or "automatic"),
                        max(0.0, min(1.0, float(confidence))),
                        (
                            alias_status
                            if alias_status in {"confirmed", "suggested", "rejected"}
                            else "confirmed"
                        ),
                        first_seen,
                        first_seen,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # A stable external ID is authoritative and may already have
                # been registered under another display name.
                if stable_id:
                    row = connection.execute(
                        """
                        SELECT person_id FROM memory_person_aliases
                        WHERE chat_name = ? AND external_id = ?
                          AND status = 'confirmed'
                        """,
                        (chat_name, stable_id),
                    ).fetchone()
                    if row is not None:
                        person_id = int(row["person_id"])
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO memory_person_aliases(
                                chat_name, person_id, alias_name, external_id,
                                source, confidence, status, first_seen_at,
                                last_seen_at, created_at, updated_at
                            ) VALUES(?, ?, ?, '', ?, ?, 'confirmed', ?, ?, ?, ?)
                            """,
                            (
                                chat_name,
                                person_id,
                                canonical_name,
                                str(source or "automatic"),
                                max(0.0, min(1.0, float(confidence))),
                                first_seen,
                                first_seen,
                                now,
                                now,
                            ),
                        )
        else:
            next_external = str(existing["external_id"] or "") or stable_id
            normalized_source = str(source or "automatic")
            sender_observation = (
                normalized_source in self._PERSON_SENDER_SOURCES
            )
            try:
                connection.execute(
                    """
                    UPDATE memory_person_aliases SET
                        external_id = ?,
                        source = CASE
                            WHEN ? = 1 AND external_id = '' THEN ?
                            ELSE source
                        END,
                        confidence = MAX(confidence, ?),
                        status = CASE
                            WHEN status = 'confirmed' THEN status
                            ELSE ?
                        END,
                        first_seen_at = COALESCE(first_seen_at, ?),
                        last_seen_at = CASE
                            WHEN ? = '' THEN last_seen_at
                            WHEN last_seen_at IS NULL OR last_seen_at < ? THEN ?
                            ELSE last_seen_at
                        END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_external,
                        int(sender_observation),
                        normalized_source,
                        max(0.0, min(1.0, float(confidence))),
                        alias_status,
                        first_seen,
                        str(observed_at or ""),
                        str(observed_at or ""),
                        str(observed_at or ""),
                        now,
                        int(existing["id"]),
                    ),
                )
            except sqlite3.IntegrityError:
                pass
        return person_id

    def _upsert_person_alias(
        self,
        connection: sqlite3.Connection,
        chat_name: str,
        person_id: int,
        alias_name: str,
        *,
        external_id: str = "",
        source: str = "manual",
        confidence: float = 1.0,
        status: str = "confirmed",
        observed_at: str = "",
    ) -> int:
        alias = str(alias_name or "").strip()
        if not alias:
            return 0
        normalized_status = (
            status
            if status in {"confirmed", "suggested", "rejected"}
            else "confirmed"
        )
        now = self._now()
        row = connection.execute(
            """
            SELECT * FROM memory_person_aliases
            WHERE chat_name = ? AND person_id = ? AND alias_name = ?
            """,
            (chat_name, int(person_id), alias),
        ).fetchone()
        if row is None:
            cursor = connection.execute(
                """
                INSERT INTO memory_person_aliases(
                    chat_name, person_id, alias_name, external_id,
                    source, confidence, status, first_seen_at,
                    last_seen_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_name,
                    int(person_id),
                    alias,
                    str(external_id or "").strip(),
                    str(source or "manual"),
                    max(0.0, min(1.0, float(confidence))),
                    normalized_status,
                    str(observed_at or "").strip() or None,
                    str(observed_at or "").strip() or None,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)
        connection.execute(
            """
            UPDATE memory_person_aliases SET
                external_id = CASE
                    WHEN external_id = '' THEN ? ELSE external_id
                END,
                source = CASE
                    WHEN status = 'confirmed' THEN source
                    ELSE ?
                END,
                confidence = MAX(confidence, ?),
                status = CASE
                    WHEN status = 'confirmed' THEN status
                    ELSE ?
                END,
                last_seen_at = COALESCE(NULLIF(?, ''), last_seen_at),
                updated_at = ?
            WHERE id = ?
            """,
            (
                str(external_id or "").strip(),
                str(source or "manual"),
                max(0.0, min(1.0, float(confidence))),
                normalized_status,
                str(observed_at or "").strip(),
                now,
                int(row["id"]),
            ),
        )
        return int(row["id"])

    def _upsert_person_fact(
        self,
        connection: sqlite3.Connection,
        chat_name: str,
        person_id: int,
        fact: Dict[str, Any],
        *,
        default_source_event_id: int = 0,
    ) -> int:
        field_name = str(fact.get("field") or fact.get("field_name") or "other").strip().lower()
        if field_name not in PERSON_FACT_FIELDS:
            field_name = "other"
        value = re.sub(r"\s+", " ", str(fact.get("value") or "").strip())
        if not value:
            return 0
        status = str(fact.get("status") or "current").strip().lower()
        if status not in PERSON_FACT_STATUSES:
            status = "uncertain"
        try:
            confidence = max(0.0, min(1.0, float(fact.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        valid_from = str(fact.get("valid_from") or "").strip()
        valid_to = str(fact.get("valid_to") or "").strip()
        observed_at = str(
            fact.get("observed_at")
            or fact.get("last_seen_at")
            or ""
        ).strip()
        source_ids = sorted(
            {
                int(value)
                for value in (
                    fact.get("source_event_ids")
                    or fact.get("evidence_event_ids")
                    or [default_source_event_id]
                )
                if str(value or "").isdigit() and int(value) > 0
            }
        )
        source_event_id = int(
            fact.get("source_event_id")
            or (source_ids[-1] if source_ids else default_source_event_id)
            or 0
        )
        if source_event_id > 0 and source_event_id not in source_ids:
            source_ids.append(source_event_id)
            source_ids.sort()
        if not observed_at and source_event_id > 0:
            event_row = connection.execute(
                """
                SELECT COALESCE(NULLIF(end_time, ''), start_time, created_at)
                AS observed_at
                FROM memory_events WHERE id = ? AND chat_name = ?
                """,
                (source_event_id, chat_name),
            ).fetchone()
            if event_row is not None:
                observed_at = str(event_row["observed_at"] or "").strip()
        evidence = fact.get("evidence") or []
        if not isinstance(evidence, list):
            evidence = []
        fact_key = str(fact.get("fact_key") or "").strip() or self._person_fact_key(
            field_name,
            value,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        now = self._now()
        existing = connection.execute(
            """
            SELECT * FROM memory_person_facts
            WHERE chat_name = ? AND person_id = ? AND fact_key = ?
            """,
            (chat_name, int(person_id), fact_key),
        ).fetchone()
        if existing is None:
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
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    1, ?, 0, NULL, ?, ?
                )
                """,
                (
                    chat_name,
                    int(person_id),
                    field_name,
                    value,
                    self._normalize_person_text(value),
                    fact_key,
                    status,
                    confidence,
                    valid_from or None,
                    valid_to or None,
                    observed_at or None,
                    observed_at or None,
                    observed_at or None,
                    str(fact.get("temporal_note") or "").strip(),
                    source_event_id,
                    _json_dump(source_ids),
                    _json_dump(evidence[:20]),
                    int(bool(fact.get("manual_override", False))),
                    now,
                    now,
                ),
            )
            fact_id = int(cursor.lastrowid)
        else:
            fact_id = int(existing["id"])
            if int(existing["manual_override"] or 0) and not fact.get("manual_override"):
                return fact_id
            merged_sources = sorted(
                set(_json_load(existing["source_event_ids_json"], []))
                | set(source_ids)
            )
            merged_evidence = list(_json_load(existing["evidence_json"], []))
            for item in evidence:
                if item not in merged_evidence:
                    merged_evidence.append(item)
            first_seen = str(existing["first_seen_at"] or "")
            last_seen = str(existing["last_seen_at"] or "")
            if observed_at:
                first_seen = min(
                    value for value in (first_seen, observed_at) if value
                )
                last_seen = max(last_seen, observed_at)
            connection.execute(
                """
                UPDATE memory_person_facts SET
                    value = ?, normalized_value = ?, status = ?,
                    confidence = MAX(confidence, ?),
                    valid_from = COALESCE(NULLIF(?, ''), valid_from),
                    valid_to = COALESCE(NULLIF(?, ''), valid_to),
                    observed_at = COALESCE(NULLIF(?, ''), observed_at),
                    first_seen_at = NULLIF(?, ''),
                    last_seen_at = NULLIF(?, ''),
                    temporal_note = CASE
                        WHEN ? != '' THEN ? ELSE temporal_note
                    END,
                    source_event_id = MAX(source_event_id, ?),
                    source_event_ids_json = ?,
                    evidence_json = ?,
                    mention_count = mention_count + 1,
                    manual_override = MAX(manual_override, ?),
                    deleted_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    value,
                    self._normalize_person_text(value),
                    status,
                    confidence,
                    valid_from,
                    valid_to,
                    observed_at,
                    first_seen,
                    last_seen,
                    str(fact.get("temporal_note") or "").strip(),
                    str(fact.get("temporal_note") or "").strip(),
                    source_event_id,
                    _json_dump(merged_sources),
                    _json_dump(merged_evidence[:40]),
                    int(bool(fact.get("manual_override", False))),
                    now,
                    fact_id,
                ),
            )
        replacement_ids = {
            int(value)
            for value in (fact.get("replaces_fact_ids") or [])
            if str(value or "").isdigit() and int(value) > 0
        }
        if replacement_ids:
            placeholders = ",".join("?" for _ in replacement_ids)
            connection.execute(
                f"""
                UPDATE memory_person_facts SET
                    status = 'historical',
                    valid_to = COALESCE(valid_to, NULLIF(?, '')),
                    superseded_by_fact_id = ?,
                    updated_at = ?
                WHERE chat_name = ? AND person_id = ?
                  AND id IN({placeholders})
                  AND id != ? AND manual_override = 0
                """,
                (
                    valid_from or observed_at,
                    fact_id,
                    now,
                    chat_name,
                    int(person_id),
                    *sorted(replacement_ids),
                    fact_id,
                ),
            )
        return fact_id

    def _migrate_legacy_people(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT * FROM memory_people ORDER BY chat_name, person_name"
        ).fetchall()
        for row in rows:
            name = str(row["person_name"] or "").strip()
            if not name:
                continue
            person_id = self._ensure_person_identity(
                connection,
                str(row["chat_name"]),
                name,
                source="legacy_profile",
                confidence=1.0,
                observed_at=str(row["updated_at"] or ""),
            )
            profile_text = str(row["profile_text"] or "").strip()
            if profile_text:
                has_structured = connection.execute(
                    """
                    SELECT 1 FROM memory_person_facts
                    WHERE chat_name = ? AND person_id = ?
                      AND field_name != 'legacy_summary'
                      AND deleted_at IS NULL
                    LIMIT 1
                    """,
                    (str(row["chat_name"]), person_id),
                ).fetchone()
                profile = _json_load(row["profile_json"], {})
                if has_structured is not None or (
                    isinstance(profile, dict)
                    and isinstance(profile.get("facts"), list)
                ):
                    continue
                existing_legacy = connection.execute(
                    """
                    SELECT id FROM memory_person_facts
                    WHERE chat_name = ? AND person_id = ?
                      AND fact_key = 'legacy_summary'
                    """,
                    (str(row["chat_name"]), person_id),
                ).fetchone()
                if existing_legacy is not None:
                    continue
                self._upsert_person_fact(
                    connection,
                    str(row["chat_name"]),
                    person_id,
                    {
                        "field": "legacy_summary",
                        "value": profile_text,
                        "status": "uncertain",
                        "confidence": float(profile.get("confidence", 0.5))
                        if isinstance(profile, dict)
                        else 0.5,
                        "observed_at": str(row["updated_at"] or ""),
                        "source_event_id": int(row["source_event_id"] or 0),
                        "manual_override": int(row["manual_override"] or 0),
                    },
                    default_source_event_id=int(row["source_event_id"] or 0),
                )

    def get_state(self, chat_name: str) -> Dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM memory_state WHERE chat_name = ?",
                (chat_name,),
            ).fetchone()
        if row is None:
            return {
                "chat_name": chat_name,
                "source_cursor": 0,
                "source_message_count": 0,
                "stage_source_event_id": 0,
                "stage_summary": "",
                "stage_json": {},
                "stage_mode": "auto",
                "stage_manual_note": "",
                "stage_manual_updated_at": None,
                "stage_updated_at": None,
            }
        value = dict(row)
        value["stage_json"] = _json_load(value.get("stage_json"), {})
        return value

    def initialize_cursor(
        self,
        chat_name: str,
        *,
        source_cursor: int,
        source_message_count: int,
    ) -> Dict[str, Any]:
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO memory_state(
                    chat_name, source_cursor, source_message_count, updated_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(chat_name) DO NOTHING
                """,
                (
                    chat_name,
                    max(0, int(source_cursor)),
                    max(0, int(source_message_count)),
                    now,
                ),
            )
        return self.get_state(chat_name)

    def chat_storage_stats(self, chat_name: str) -> Dict[str, Any]:
        """Return cheap per-chat counts and logical payload sizes."""
        specs = {
            "events": (
                "memory_events",
                "LENGTH(summary) + LENGTH(card_json) + LENGTH(search_text) "
                "+ COALESCE(LENGTH(embedding), 0)",
            ),
            "event_messages": (
                "memory_event_messages",
                "LENGTH(message_json)",
            ),
            "people": ("memory_person_identities", "LENGTH(canonical_name)"),
            "source_messages": (
                "memory_person_source_messages",
                "LENGTH(content)",
            ),
            "message_links": ("memory_person_message_links", "LENGTH(matched_alias)"),
            "candidates": (
                "memory_person_claim_candidates",
                "LENGTH(statement) + LENGTH(candidate_json) + LENGTH(verifier_json)",
            ),
            "observations": (
                "memory_person_observations",
                "LENGTH(statement) + LENGTH(evidence_excerpt_json) + LENGTH(context_json)",
            ),
            "person_snapshots": (
                "memory_person_snapshots",
                "LENGTH(sections_json) + LENGTH(rendered_text)",
            ),
            "corrections": (
                "memory_corrections",
                "LENGTH(before_json) + LENGTH(after_json)",
            ),
            "person_audits": (
                "memory_person_audit",
                "LENGTH(before_json) + LENGTH(after_json)",
            ),
            "person_v3_audits": (
                "memory_person_v3_audit",
                "LENGTH(before_json) + LENGTH(after_json)",
            ),
        }
        result: Dict[str, Dict[str, int]] = {}
        with self._connection() as connection:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            for key, (table, size_expression) in specs.items():
                if table not in tables:
                    result[key] = {"count": 0, "bytes": 0}
                    continue
                if table == "memory_event_messages":
                    row = connection.execute(
                        f"""
                        SELECT COUNT(*) AS count,
                               COALESCE(SUM({size_expression}), 0) AS bytes
                        FROM memory_event_messages
                        WHERE event_id IN(
                            SELECT id FROM memory_events WHERE chat_name = ?
                        )
                        """,
                        (chat_name,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        f"""
                        SELECT COUNT(*) AS count,
                               COALESCE(SUM({size_expression}), 0) AS bytes
                        FROM {table} WHERE chat_name = ?
                        """,
                        (chat_name,),
                    ).fetchone()
                result[key] = {
                    "count": int(row["count"] or 0),
                    "bytes": int(row["bytes"] or 0),
                }
            candidate_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS value
                FROM memory_person_claim_candidates
                WHERE chat_name = ? GROUP BY status
                """,
                (chat_name,),
            ).fetchall()
            event_rows = connection.execute(
                """
                SELECT
                    CASE
                        WHEN is_invalidated = 1 THEN 'invalidated'
                        WHEN superseded_by_event_id > 0 THEN 'superseded'
                        WHEN verification_status = 'quarantined' THEN 'quarantined'
                        ELSE 'active'
                    END AS status,
                    COUNT(*) AS value
                FROM memory_events WHERE chat_name = ? GROUP BY status
                """,
                (chat_name,),
            ).fetchall()
        return {
            "categories": result,
            "logical_bytes": sum(item["bytes"] for item in result.values()),
            "candidate_status": {
                str(row["status"]): int(row["value"] or 0)
                for row in candidate_rows
            },
            "event_status": {
                str(row["status"]): int(row["value"] or 0)
                for row in event_rows
            },
        }

    def prune_transient_candidates(
        self,
        chat_name: str,
        *,
        rejected_older_than_days: int = 90,
        dry_run: bool = True,
        reason: str = "清理过期的已拒绝人物候选",
    ) -> Dict[str, Any]:
        days = max(7, min(3650, int(rejected_older_than_days)))
        cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count,
                       COALESCE(SUM(
                           LENGTH(statement) + LENGTH(candidate_json)
                           + LENGTH(verifier_json)
                       ), 0) AS bytes
                FROM memory_person_claim_candidates
                WHERE chat_name = ? AND status = 'rejected'
                  AND updated_at < ?
                """,
                (chat_name, cutoff),
            ).fetchone()
            result = {
                "dry_run": bool(dry_run),
                "cutoff": cutoff,
                "rejected_candidate_count": int(row["count"] or 0),
                "estimated_bytes": int(row["bytes"] or 0),
            }
            if dry_run:
                return result
            if result["rejected_candidate_count"] > 0:
                connection.execute(
                    """
                    DELETE FROM memory_person_claim_candidates
                    WHERE chat_name = ? AND status = 'rejected'
                      AND updated_at < ?
                    """,
                    (chat_name, cutoff),
                )
            result["deleted"] = result["rejected_candidate_count"]
            connection.execute(
                """
                INSERT INTO memory_maintenance_audit(
                    chat_name, action, reason, result_json, created_at
                ) VALUES(?, 'prune_rejected_candidates', ?, ?, ?)
                """,
                (
                    chat_name,
                    str(reason or "").strip(),
                    _json_dump(result),
                    self._now(),
                ),
            )
        return result

    def record_maintenance_action(
        self,
        chat_name: str,
        *,
        action: str,
        reason: str,
        result: Dict[str, Any],
    ) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_maintenance_audit(
                    chat_name, action, reason, result_json, created_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    chat_name,
                    str(action or "maintenance"),
                    str(reason or "").strip(),
                    _json_dump(result or {}),
                    self._now(),
                ),
            )
        return int(cursor.lastrowid)

    def maybe_prune_transient_candidates(
        self,
        chat_name: str,
        *,
        rejected_older_than_days: int = 90,
        interval_hours: int = 24,
    ) -> Dict[str, Any]:
        interval = max(1, min(24 * 30, int(interval_hours)))
        cutoff = (
            datetime.now().astimezone() - timedelta(hours=interval)
        ).isoformat(timespec="seconds")
        with self._connection() as connection:
            recent = connection.execute(
                """
                SELECT id, created_at FROM memory_maintenance_audit
                WHERE chat_name = ?
                  AND action = 'prune_rejected_candidates'
                  AND created_at >= ?
                ORDER BY id DESC LIMIT 1
                """,
                (chat_name, cutoff),
            ).fetchone()
        if recent is not None:
            return {
                "skipped": True,
                "reason": "maintenance_interval",
                "last_run_at": recent["created_at"],
            }
        return self.prune_transient_candidates(
            chat_name,
            rejected_older_than_days=rejected_older_than_days,
            dry_run=False,
            reason="后台生命周期维护：清理过期的已拒绝人物候选",
        )

    def integrity_report(self, chat_name: str) -> Dict[str, Any]:
        """Check explicit references because legacy tables predate foreign keys."""
        checks = {
            "event_messages_without_event": """
                SELECT COUNT(*) FROM memory_event_messages AS child
                LEFT JOIN memory_events AS parent ON parent.id = child.event_id
                WHERE parent.id IS NULL
            """,
            "aliases_without_person": """
                SELECT COUNT(*) FROM memory_person_aliases AS child
                LEFT JOIN memory_person_identities AS parent
                  ON parent.id = child.person_id
                WHERE child.chat_name = ? AND parent.id IS NULL
            """,
            "observations_without_person": """
                SELECT COUNT(*) FROM memory_person_observations AS child
                LEFT JOIN memory_person_identities AS parent
                  ON parent.id = child.person_id
                WHERE child.chat_name = ? AND parent.id IS NULL
            """,
            "links_without_source": """
                SELECT COUNT(*) FROM memory_person_message_links AS child
                LEFT JOIN memory_person_source_messages AS parent
                  ON parent.id = child.source_message_id
                WHERE child.chat_name = ? AND parent.id IS NULL
            """,
            "multiple_active_snapshots": """
                SELECT COUNT(*) FROM (
                    SELECT person_id FROM memory_person_snapshots
                    WHERE chat_name = ? AND is_active = 1
                    GROUP BY person_id HAVING COUNT(*) > 1
                )
            """,
        }
        values: Dict[str, int] = {}
        with self._connection() as connection:
            for key, sql in checks.items():
                params = () if key == "event_messages_without_event" else (chat_name,)
                values[key] = int(connection.execute(sql, params).fetchone()[0])
        return {
            "ok": all(value == 0 for value in values.values()),
            "checks": values,
            "schema_versions": self.schema_versions(),
        }

    def advance_cursor(
        self,
        chat_name: str,
        *,
        source_cursor: int,
        source_message_count: int,
    ) -> None:
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO memory_state(
                    chat_name, source_cursor, source_message_count, updated_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(chat_name) DO UPDATE SET
                    source_cursor = CASE
                        WHEN excluded.source_message_count >= memory_state.source_message_count
                        THEN excluded.source_cursor
                        ELSE memory_state.source_cursor
                    END,
                    source_message_count = MAX(
                        memory_state.source_message_count,
                        excluded.source_message_count
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    chat_name,
                    max(0, int(source_cursor)),
                    max(0, int(source_message_count)),
                    now,
                ),
            )

    def set_ingestion_cursor(
        self,
        chat_name: str,
        *,
        source_cursor: int,
        source_message_count: int,
    ) -> Dict[str, Any]:
        """Explicitly rebase a chat onto another ingestion cursor space."""
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO memory_state(
                    chat_name, source_cursor, source_message_count, updated_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(chat_name) DO UPDATE SET
                    source_cursor = excluded.source_cursor,
                    source_message_count = excluded.source_message_count,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_name,
                    max(0, int(source_cursor)),
                    max(0, int(source_message_count)),
                    now,
                ),
            )
        return self.get_state(chat_name)

    def register_source(
        self,
        chat_name: str,
        *,
        source_namespace: str,
        source_type: str,
        source_path: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        namespace = str(source_namespace or "").strip()
        normalized_type = str(source_type or "").strip()
        if not namespace or not normalized_type:
            raise ValueError("source_namespace and source_type are required")
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO memory_sources(
                    chat_name, source_namespace, source_type,
                    source_path, metadata_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_name, source_namespace) DO UPDATE SET
                    source_type = excluded.source_type,
                    source_path = excluded.source_path,
                    metadata_json = excluded.metadata_json
                """,
                (
                    chat_name,
                    namespace,
                    normalized_type,
                    str(source_path or ""),
                    _json_dump(metadata or {}),
                    now,
                ),
            )
        return self.get_source(chat_name, namespace) or {}

    def get_source(
        self,
        chat_name: str,
        source_namespace: str,
    ) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_sources
                WHERE chat_name = ? AND source_namespace = ?
                """,
                (chat_name, str(source_namespace or "").strip()),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["metadata"] = _json_load(
            value.pop("metadata_json", None),
            {},
        )
        return value

    def list_sources(self, chat_name: str) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_sources
                WHERE chat_name = ? ORDER BY created_at, source_namespace
                """,
                (chat_name,),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["metadata"] = _json_load(
                value.pop("metadata_json", None),
                {},
            )
            result.append(value)
        return result

    @staticmethod
    def _render_person_v3_snapshot(
        sections: Dict[str, List[Dict[str, Any]]],
    ) -> str:
        labels = {
            "current_snapshot": "当前概况",
            "timeline": "关键时间线",
            "stable_traits": "稳定特点",
            "group_relationships": "群内角色与关系",
            "uncertain": "待确认",
        }
        blocks: List[str] = []
        for key in labels:
            items = sections.get(key) or []
            if not items:
                continue
            lines = [f"【{labels[key]}】"]
            for item in items:
                time_text = str(item.get("valid_from") or "")
                prefix = f"{time_text} " if time_text and key == "timeline" else ""
                lines.append(f"- {prefix}{item.get('text') or ''}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _event_correction_v3_snapshot(
        self,
        connection: sqlite3.Connection,
        chat_name: str,
        target: sqlite3.Row,
        people_names: Iterable[str],
    ) -> Dict[str, Any]:
        table_names = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required = {
            "memory_person_observations",
            "memory_person_identities",
            "memory_person_aliases",
        }
        if not required.issubset(table_names):
            return {}

        normalized_names = {
            str(value or "").strip().casefold()
            for value in people_names
            if str(value or "").strip()
        }
        person_ids: set[int] = set()
        if normalized_names:
            identities = connection.execute(
                """
                SELECT identity.id, identity.canonical_name, alias.alias_name
                FROM memory_person_identities AS identity
                LEFT JOIN memory_person_aliases AS alias
                  ON alias.chat_name = identity.chat_name
                 AND alias.person_id = identity.id
                 AND alias.status = 'confirmed'
                WHERE identity.chat_name = ? AND identity.status = 'active'
                """,
                (chat_name,),
            ).fetchall()
            for identity in identities:
                identity_names = {
                    str(identity["canonical_name"] or "").strip().casefold(),
                    str(identity["alias_name"] or "").strip().casefold(),
                }
                if normalized_names & identity_names:
                    person_ids.add(int(identity["id"]))

        clauses = [
            "chat_name = ?",
            "source_namespace = ?",
            "source_start_cursor <= ?",
            "source_end_cursor >= ?",
            "quality_status = 'active'",
        ]
        params: List[Any] = [
            chat_name,
            str(target["source_namespace"] or "live_chat_log"),
            int(target["source_end_cursor"] or 0),
            int(target["source_start_cursor"] or 0),
        ]
        if person_ids:
            placeholders = ",".join("?" for _ in person_ids)
            clauses.append(f"person_id IN({placeholders})")
            params.extend(sorted(person_ids))
        observations = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM memory_person_observations WHERE "
                + " AND ".join(clauses)
                + " ORDER BY id",
                params,
            ).fetchall()
        ]
        if not observations:
            return {}
        person_ids.update(
            int(row["person_id"])
            for row in observations
            if int(row.get("person_id") or 0) > 0
        )
        snapshot = self._person_snapshot(
            connection,
            chat_name,
            person_ids,
            include_v3_derived=True,
        )
        return {
            "person_ids": sorted(person_ids),
            "observations": observations,
            "derived": snapshot.get("v3_derived") or {},
            "audit_cutoff_id": int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(id), 0) AS value
                    FROM memory_person_v3_audit WHERE chat_name = ?
                    """,
                    (chat_name,),
                ).fetchone()["value"]
            ),
        }

    def _apply_event_correction_to_v3(
        self,
        connection: sqlite3.Connection,
        chat_name: str,
        target_event_id: int,
        snapshot: Dict[str, Any],
        *,
        correction_id: int,
        reason: str,
        now: str,
    ) -> Dict[str, Any]:
        observations = list(snapshot.get("observations") or [])
        observation_ids = {
            int(row.get("id") or 0)
            for row in observations
            if int(row.get("id") or 0) > 0
        }
        person_ids = {
            int(value)
            for value in snapshot.get("person_ids") or []
            if int(value) > 0
        }
        if not observation_ids:
            return {"observation_count": 0, "person_count": 0, "audit_ids": []}

        placeholders = ",".join("?" for _ in observation_ids)
        connection.execute(
            f"""
            UPDATE memory_person_observations SET
                quality_status = 'quarantined',
                rejection_reason = ?,
                updated_at = ?
            WHERE chat_name = ? AND id IN({placeholders})
            """,
            (
                f"事件纠错 #{correction_id}：{reason}",
                now,
                chat_name,
                *sorted(observation_ids),
            ),
        )

        for table in (
            "memory_person_fact_versions",
            "memory_person_patterns",
            "memory_person_relationships",
        ):
            if table not in (snapshot.get("derived") or {}):
                continue
            rows = connection.execute(
                f"""
                SELECT id, evidence_observation_ids_json
                FROM {table}
                WHERE chat_name = ? AND deleted_at IS NULL
                """,
                (chat_name,),
            ).fetchall()
            affected_ids = [
                int(row["id"])
                for row in rows
                if observation_ids
                & {
                    int(value)
                    for value in _json_load(
                        row["evidence_observation_ids_json"],
                        [],
                    )
                    if int(value) > 0
                }
            ]
            if affected_ids:
                id_placeholders = ",".join("?" for _ in affected_ids)
                connection.execute(
                    f"""
                    UPDATE {table} SET deleted_at = ?, updated_at = ?
                    WHERE id IN({id_placeholders})
                    """,
                    (now, now, *affected_ids),
                )

        created_snapshot_ids: List[int] = []
        for person_id in sorted(person_ids):
            row = connection.execute(
                """
                SELECT * FROM memory_person_snapshots
                WHERE chat_name = ? AND person_id = ? AND is_active = 1
                ORDER BY generation DESC LIMIT 1
                """,
                (chat_name, person_id),
            ).fetchone()
            if row is None:
                continue
            sections = _json_load(row["sections_json"], {})
            filtered: Dict[str, List[Dict[str, Any]]] = {}
            changed = False
            for key, items in sections.items():
                filtered[key] = []
                for item in items or []:
                    evidence_ids = {
                        int(value)
                        for value in item.get("evidence_observation_ids") or []
                        if int(value) > 0
                    }
                    if evidence_ids & observation_ids:
                        changed = True
                        continue
                    filtered[key].append(dict(item))
            if not changed:
                continue
            connection.execute(
                "UPDATE memory_person_snapshots SET is_active = 0 WHERE id = ?",
                (int(row["id"]),),
            )
            rendered = self._render_person_v3_snapshot(filtered)
            if rendered:
                evidence_ids = sorted(
                    {
                        int(value)
                        for items in filtered.values()
                        for item in items
                        for value in item.get("evidence_observation_ids") or []
                        if int(value) > 0
                    }
                )
                cursor = connection.execute(
                    """
                    INSERT INTO memory_person_snapshots(
                        chat_name, person_id, generation, sections_json,
                        rendered_text, evidence_observation_ids_json,
                        source_observation_max_id, is_active,
                        generator_version, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        chat_name,
                        person_id,
                        int(row["generation"] or 0) + 1,
                        _json_dump(filtered),
                        rendered,
                        _json_dump(evidence_ids),
                        int(row["source_observation_max_id"] or 0),
                        "person-v3-event-correction",
                        now,
                    ),
                )
                created_snapshot_ids.append(int(cursor.lastrowid))

        audit_ids: List[int] = []
        for person_id in sorted(person_ids):
            person_observation_ids = sorted(
                int(row["id"])
                for row in observations
                if int(row.get("person_id") or 0) == person_id
            )
            if not person_observation_ids:
                continue
            cursor = connection.execute(
                """
                INSERT INTO memory_person_v3_audit(
                    chat_name, person_id, action, target_type, target_id,
                    reason, before_json, after_json, status, created_at
                ) VALUES(?, ?, 'event_correction', 'event', ?, ?, ?, ?, 'active', ?)
                """,
                (
                    chat_name,
                    person_id,
                    int(target_event_id),
                    reason,
                    _json_dump({"observation_ids": person_observation_ids}),
                    _json_dump(
                        {
                            "quality_status": "quarantined",
                            "correction_id": correction_id,
                        }
                    ),
                    now,
                ),
            )
            audit_ids.append(int(cursor.lastrowid))
        return {
            "observation_count": len(observation_ids),
            "person_count": len(person_ids),
            "audit_ids": audit_ids,
            "created_snapshot_ids": created_snapshot_ids,
        }

    def _restore_event_correction_v3(
        self,
        connection: sqlite3.Connection,
        chat_name: str,
        snapshot: Dict[str, Any],
        after: Dict[str, Any],
        *,
        reverted_at: str,
    ) -> None:
        person_ids = sorted(
            {
                int(value)
                for value in snapshot.get("person_ids") or []
                if int(value) > 0
            }
        )
        if not person_ids:
            return
        placeholders = ",".join("?" for _ in person_ids)
        cutoff_id = int(snapshot.get("audit_cutoff_id") or 0)
        ignored_audit_ids = {
            int(value)
            for value in (after.get("person_v3") or {}).get("audit_ids") or []
            if int(value) > 0
        }
        later_rows = connection.execute(
            f"""
            SELECT id FROM memory_person_v3_audit
            WHERE chat_name = ? AND person_id IN({placeholders})
              AND status = 'active' AND id > ?
            ORDER BY id DESC
            """,
            (chat_name, *person_ids, cutoff_id),
        ).fetchall()
        blocking = [
            int(row["id"])
            for row in later_rows
            if int(row["id"]) not in ignored_audit_ids
        ]
        if blocking:
            raise ValueError(
                "存在影响同一人物的更晚 v3 修改，"
                f"请先处理人物修改 #{blocking[0]}"
            )

        for observation in snapshot.get("observations") or []:
            connection.execute(
                """
                UPDATE memory_person_observations SET
                    quality_status = ?, rejection_reason = ?, updated_at = ?
                WHERE chat_name = ? AND id = ?
                """,
                (
                    str(observation.get("quality_status") or "active"),
                    str(observation.get("rejection_reason") or ""),
                    str(observation.get("updated_at") or reverted_at),
                    chat_name,
                    int(observation.get("id") or 0),
                ),
            )

        derived = snapshot.get("derived") or {}
        for table in (
            "memory_person_fact_versions",
            "memory_person_patterns",
            "memory_person_period_summaries",
            "memory_person_snapshots",
            "memory_person_refresh_state",
            "memory_person_v3_suppressions",
            "memory_person_pipeline_state",
        ):
            if table not in derived:
                continue
            connection.execute(
                f"DELETE FROM {table} WHERE chat_name = ? AND person_id IN({placeholders})",
                (chat_name, *person_ids),
            )
            self._restore_rows(connection, table, derived.get(table) or [])
        if "memory_person_relationships" in derived:
            connection.execute(
                f"""
                DELETE FROM memory_person_relationships
                WHERE chat_name = ?
                  AND (person_id IN({placeholders})
                       OR target_person_id IN({placeholders}))
                """,
                (chat_name, *person_ids, *person_ids),
            )
            self._restore_rows(
                connection,
                "memory_person_relationships",
                derived.get("memory_person_relationships") or [],
            )
        if ignored_audit_ids:
            audit_placeholders = ",".join("?" for _ in ignored_audit_ids)
            connection.execute(
                f"""
                UPDATE memory_person_v3_audit
                SET status = 'reverted', reverted_at = ?
                WHERE chat_name = ? AND id IN({audit_placeholders})
                """,
                (reverted_at, chat_name, *sorted(ignored_audit_ids)),
            )

    def add_events(self, chat_name: str, events: Iterable[Dict[str, Any]]) -> List[int]:
        created_ids: List[int] = []
        now = self._now()
        with self._connection() as connection:
            for event in events:
                vector = event.get("embedding")
                if vector is not None:
                    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
                    embedding_blob: Optional[bytes] = vector.tobytes()
                    embedding_dim = int(vector.size)
                else:
                    embedding_blob = None
                    embedding_dim = 0

                supersedes_event_id = max(
                    0,
                    int(event.get("supersedes_event_id") or 0),
                )
                if supersedes_event_id:
                    target = connection.execute(
                        """
                        SELECT id FROM memory_events
                        WHERE chat_name = ? AND id = ?
                          AND superseded_by_event_id = 0
                          AND is_invalidated = 0
                          AND verification_status != 'quarantined'
                        """,
                        (chat_name, supersedes_event_id),
                    ).fetchone()
                    if target is None:
                        supersedes_event_id = 0

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO memory_events(
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
                        ?, 0, ?, ?, ?, ?
                    )
                    """,
                    (
                        chat_name,
                        str(
                            event.get("source_namespace")
                            or "live_chat_log"
                        ).strip(),
                        int(event["source_start_cursor"]),
                        int(event["source_end_cursor"]),
                        str(event.get("start_time") or ""),
                        str(event.get("end_time") or ""),
                        str(event.get("title") or "未命名事件"),
                        str(event.get("summary") or ""),
                        _json_dump(event.get("participants") or []),
                        _json_dump(event.get("keywords") or []),
                        _json_dump(event.get("opinions") or []),
                        _json_dump(event.get("decisions") or []),
                        _json_dump(event.get("open_items") or []),
                        float(event.get("importance") or 0.5),
                        _json_dump(event.get("card") or {}),
                        str(event.get("search_text") or ""),
                        embedding_blob,
                        embedding_dim,
                        supersedes_event_id,
                        str(event.get("relation_reason") or ""),
                        str(
                            event.get("verification_status")
                            or "not_required"
                        ),
                        str(event.get("verification_note") or ""),
                        now,
                    ),
                )
                if cursor.rowcount:
                    created_id = int(cursor.lastrowid)
                    created_ids.append(created_id)
                    self._insert_event_messages(
                        connection,
                        created_id,
                        event.get("source_messages") or [],
                        created_at=now,
                    )
                    if supersedes_event_id:
                        connection.execute(
                            """
                            UPDATE memory_events
                            SET superseded_by_event_id = ?
                            WHERE chat_name = ? AND id = ?
                              AND superseded_by_event_id = 0
                              AND is_invalidated = 0
                            """,
                            (created_id, chat_name, supersedes_event_id),
                        )
        return created_ids

    @staticmethod
    def _insert_event_messages(
        connection: sqlite3.Connection,
        event_id: int,
        messages: Iterable[Dict[str, Any]],
        *,
        created_at: str,
    ) -> int:
        count = 0
        for ordinal, source_message in enumerate(messages):
            if not isinstance(source_message, dict):
                continue
            message = dict(source_message)
            log_cursor = int(
                message.get("_log_cursor")
                or message.get("memory_cursor")
                or 0
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO memory_event_messages(
                    event_id, ordinal, log_cursor, message_json, created_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    int(event_id),
                    count,
                    log_cursor,
                    _json_dump(message),
                    created_at,
                ),
            )
            count += 1
        return count

    def attach_event_messages(
        self,
        event_id: int,
        messages: Iterable[Dict[str, Any]],
        *,
        replace: bool = False,
    ) -> int:
        with self._connection() as connection:
            existing = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM memory_event_messages
                    WHERE event_id = ?
                    """,
                    (int(event_id),),
                ).fetchone()[0]
            )
            if existing and not replace:
                return existing
            if replace:
                connection.execute(
                    "DELETE FROM memory_event_messages WHERE event_id = ?",
                    (int(event_id),),
                )
            return self._insert_event_messages(
                connection,
                int(event_id),
                messages,
                created_at=self._now(),
            )

    def list_event_messages(self, event_id: int) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT log_cursor, message_json
                FROM memory_event_messages
                WHERE event_id = ? ORDER BY ordinal
                """,
                (int(event_id),),
            ).fetchall()
        messages = []
        for row in rows:
            message = _json_load(row["message_json"], {})
            if not isinstance(message, dict):
                continue
            message["_log_cursor"] = int(
                row["log_cursor"]
                or message.get("_log_cursor")
                or message.get("memory_cursor")
                or 0
            )
            messages.append(message)
        return messages

    def update_event_embedding(self, event_id: int, vector: np.ndarray) -> None:
        value = np.asarray(vector, dtype=np.float32).reshape(-1)
        with self._connection() as connection:
            connection.execute(
                "UPDATE memory_events SET embedding = ?, embedding_dim = ? WHERE id = ?",
                (value.tobytes(), int(value.size), int(event_id)),
            )

    def list_events(
        self,
        chat_name: str,
        *,
        after_id: int = 0,
        limit: int = 0,
        newest_first: bool = False,
        require_embedding: bool = False,
        active_only: bool = False,
    ) -> List[Dict[str, Any]]:
        clauses = ["chat_name = ?", "id > ?"]
        params: List[Any] = [chat_name, max(0, int(after_id))]
        if require_embedding:
            clauses.append("embedding IS NOT NULL")
            clauses.append("embedding_dim > 0")
        if active_only:
            clauses.append("superseded_by_event_id = 0")
            clauses.append("is_invalidated = 0")
            clauses.append("verification_status != 'quarantined'")
        order = "DESC" if newest_first else "ASC"
        sql = (
            "SELECT * FROM memory_events WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY id {order}"
        )
        if limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._event_from_row(row) for row in rows]

    def browse_events(
        self,
        chat_name: str,
        *,
        query: str = "",
        date_from: str = "",
        date_to: str = "",
        status: str = "all",
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[List[Dict[str, Any]], int]:
        """Return lightweight event cards for the Web memory browser."""
        clauses = ["chat_name = ?"]
        params: List[Any] = [chat_name]
        terms = [
            term
            for term in re.split(r"\s+", str(query or "").strip())
            if term
        ][:8]
        for term in terms:
            pattern = f"%{term}%"
            clauses.append(
                "("
                "title LIKE ? OR summary LIKE ? OR search_text LIKE ? OR "
                "participants_json LIKE ? OR keywords_json LIKE ?"
                ")"
            )
            params.extend([pattern] * 5)
        if date_from:
            clauses.append(
                "COALESCE(NULLIF(end_time, ''), created_at) >= ?"
            )
            params.append(str(date_from))
        if date_to:
            clauses.append(
                "COALESCE(NULLIF(start_time, ''), created_at) <= ?"
            )
            params.append(f"{date_to} 23:59:59")
        normalized_status = str(status or "all").strip().lower()
        if normalized_status == "active":
            clauses.append("superseded_by_event_id = 0")
            clauses.append("is_invalidated = 0")
            clauses.append("verification_status != 'quarantined'")
        elif normalized_status == "superseded":
            clauses.append("superseded_by_event_id > 0")
        elif normalized_status == "invalidated":
            clauses.append("is_invalidated = 1")
        elif normalized_status == "quarantined":
            clauses.append("verification_status = 'quarantined'")
            clauses.append("is_invalidated = 0")

        where_sql = " AND ".join(clauses)
        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(100, int(limit)))
        with self._connection() as connection:
            count_row = connection.execute(
                f"SELECT COUNT(*) AS value FROM memory_events WHERE {where_sql}",
                params,
            ).fetchone()
            rows = connection.execute(
                f"""
                SELECT
                    id, chat_name, source_namespace,
                    source_start_cursor, source_end_cursor,
                    start_time, end_time, title, summary,
                    participants_json, keywords_json, opinions_json,
                    decisions_json, open_items_json, importance, card_json,
                    search_text, NULL AS embedding, embedding_dim,
                    supersedes_event_id, superseded_by_event_id,
                    relation_reason, verification_status, verification_note,
                    is_invalidated, manual_revision,
                    correction_id,
                    (
                        SELECT action FROM memory_corrections
                        WHERE id = memory_events.correction_id
                    ) AS correction_action,
                    created_at
                FROM memory_events
                WHERE {where_sql}
                ORDER BY COALESCE(NULLIF(end_time, ''), created_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, safe_limit, safe_offset],
            ).fetchall()
        return (
            [self._event_from_row(row) for row in rows],
            int(count_row["value"] if count_row else 0),
        )

    def list_missing_embeddings(self, chat_name: str, limit: int = 32) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_events
                WHERE chat_name = ? AND embedding IS NULL
                  AND is_invalidated = 0
                  AND verification_status != 'quarantined'
                ORDER BY id ASC LIMIT ?
                """,
                (chat_name, max(1, int(limit))),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def get_event(self, chat_name: str, event_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_events
                WHERE chat_name = ? AND id = ?
                """,
                (chat_name, int(event_id)),
            ).fetchone()
        return self._event_from_row(row) if row is not None else None

    def latest_event_id(self, chat_name: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS value FROM memory_events WHERE chat_name = ?",
                (chat_name,),
            ).fetchone()
        return int(row["value"] if row else 0)

    def count_events(self, chat_name: str, *, active_only: bool = False) -> int:
        active_clause = (
            " AND superseded_by_event_id = 0 AND is_invalidated = 0"
            " AND verification_status != 'quarantined'"
            if active_only
            else ""
        )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS value FROM memory_events "
                f"WHERE chat_name = ?{active_clause}",
                (chat_name,),
            ).fetchone()
        return int(row["value"] if row else 0)

    def _event_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        value = dict(row)
        for source, target, default in (
            ("participants_json", "participants", []),
            ("keywords_json", "keywords", []),
            ("opinions_json", "opinions", []),
            ("decisions_json", "decisions", []),
            ("open_items_json", "open_items", []),
            ("card_json", "card", {}),
        ):
            value[target] = _json_load(value.pop(source, None), default)
        blob = value.get("embedding")
        dim = int(value.get("embedding_dim") or 0)
        value["embedding"] = (
            np.frombuffer(blob, dtype=np.float32).copy()
            if blob is not None and dim > 0
            else None
        )
        return value

    def update_stage(
        self,
        chat_name: str,
        *,
        summary: str,
        stage_json: Dict[str, Any],
        source_event_id: int,
        mode: str = "auto",
        manual_note: str = "",
    ) -> None:
        now = self._now()
        normalized_mode = "manual" if str(mode).strip().lower() == "manual" else "auto"
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO memory_state(
                    chat_name, stage_source_event_id, stage_summary,
                    stage_json, stage_mode, stage_manual_note,
                    stage_manual_updated_at, stage_updated_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_name) DO UPDATE SET
                    stage_source_event_id = excluded.stage_source_event_id,
                    stage_summary = excluded.stage_summary,
                    stage_json = excluded.stage_json,
                    stage_mode = excluded.stage_mode,
                    stage_manual_note = excluded.stage_manual_note,
                    stage_manual_updated_at = excluded.stage_manual_updated_at,
                    stage_updated_at = excluded.stage_updated_at,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_name,
                    max(0, int(source_event_id)),
                    str(summary or ""),
                    _json_dump(stage_json or {}),
                    normalized_mode,
                    str(manual_note or "") if normalized_mode == "manual" else "",
                    now if normalized_mode == "manual" else None,
                    now,
                    now,
                ),
            )

    def update_stage_manual(
        self,
        chat_name: str,
        *,
        summary: str,
        reason: str,
    ) -> Dict[str, Any]:
        text = str(summary or "").strip()
        note = str(reason or "").strip()
        if len(note) < 2:
            raise ValueError("stage edit reason is required")
        before = self.get_state(chat_name)
        source_event_id = self.latest_event_id(chat_name)
        self.update_stage(
            chat_name,
            summary=text,
            stage_json={"summary": text, "manually_edited": True},
            source_event_id=source_event_id,
            mode="manual",
            manual_note=note,
        )
        after = self.get_state(chat_name)
        now = self._now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_stage_audit(
                    chat_name, action, reason, before_json, after_json,
                    status, created_at
                ) VALUES(?, 'manual_edit', ?, ?, ?, 'active', ?)
                """,
                (
                    chat_name,
                    note,
                    _json_dump(before),
                    _json_dump(after),
                    now,
                ),
            )
            audit_id = int(cursor.lastrowid)
        return {"audit_id": audit_id, "state": after}

    def enable_automatic_stage(
        self,
        chat_name: str,
        *,
        reason: str,
    ) -> Dict[str, Any]:
        note = str(reason or "").strip()
        if len(note) < 2:
            raise ValueError("stage mode change reason is required")
        before = self.get_state(chat_name)
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO memory_state(
                    chat_name, source_cursor, source_message_count,
                    stage_source_event_id, stage_summary, stage_json,
                    stage_mode, stage_manual_note, stage_manual_updated_at,
                    stage_updated_at, updated_at
                ) VALUES(?, 0, 0, 0, '', '{}', 'auto', '', NULL, NULL, ?)
                ON CONFLICT(chat_name) DO UPDATE SET
                    stage_source_event_id = 0,
                    stage_mode = 'auto',
                    stage_manual_note = '',
                    stage_manual_updated_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (chat_name, now),
            )
        after = self.get_state(chat_name)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_stage_audit(
                    chat_name, action, reason, before_json, after_json,
                    status, created_at
                ) VALUES(?, 'enable_auto', ?, ?, ?, 'active', ?)
                """,
                (
                    chat_name,
                    note,
                    _json_dump(before),
                    _json_dump(after),
                    now,
                ),
            )
            audit_id = int(cursor.lastrowid)
        return {"audit_id": audit_id, "state": after}

    def revert_stage_audit(
        self,
        chat_name: str,
        audit_id: int,
    ) -> Dict[str, Any]:
        now = self._now()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_stage_audit
                WHERE chat_name = ? AND id = ? AND status = 'active'
                """,
                (chat_name, int(audit_id)),
            ).fetchone()
            if row is None:
                raise ValueError("active stage change does not exist")
            later = connection.execute(
                """
                SELECT id FROM memory_stage_audit
                WHERE chat_name = ? AND status = 'active' AND id > ?
                ORDER BY id DESC LIMIT 1
                """,
                (chat_name, int(audit_id)),
            ).fetchone()
            if later is not None:
                raise ValueError(
                    f"存在更晚的阶段修改，请先撤销 #{int(later['id'])}"
                )
            before = _json_load(row["before_json"], {})
            connection.execute(
                """
                INSERT INTO memory_state(
                    chat_name, source_cursor, source_message_count,
                    stage_source_event_id, stage_summary, stage_json,
                    stage_mode, stage_manual_note, stage_manual_updated_at,
                    stage_updated_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_name) DO UPDATE SET
                    source_cursor = excluded.source_cursor,
                    source_message_count = excluded.source_message_count,
                    stage_source_event_id = excluded.stage_source_event_id,
                    stage_summary = excluded.stage_summary,
                    stage_json = excluded.stage_json,
                    stage_mode = excluded.stage_mode,
                    stage_manual_note = excluded.stage_manual_note,
                    stage_manual_updated_at = excluded.stage_manual_updated_at,
                    stage_updated_at = excluded.stage_updated_at,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_name,
                    int(before.get("source_cursor") or 0),
                    int(before.get("source_message_count") or 0),
                    int(before.get("stage_source_event_id") or 0),
                    str(before.get("stage_summary") or ""),
                    _json_dump(before.get("stage_json") or {}),
                    str(before.get("stage_mode") or "auto"),
                    str(before.get("stage_manual_note") or ""),
                    before.get("stage_manual_updated_at"),
                    before.get("stage_updated_at"),
                    str(before.get("updated_at") or now),
                ),
            )
            connection.execute(
                """
                UPDATE memory_stage_audit
                SET status = 'reverted', reverted_at = ?
                WHERE id = ?
                """,
                (now, int(audit_id)),
            )
        return self.get_state(chat_name)

    def upsert_people(
        self,
        chat_name: str,
        people: Iterable[Dict[str, Any]],
        *,
        source_event_id: int,
    ) -> int:
        count = 0
        now = self._now()
        with self._connection() as connection:
            for person in people:
                name = str(person.get("name") or "").strip()
                if not name:
                    continue
                profile_text = str(person.get("profile") or "").strip()
                aliases = [
                    str(value or "").strip()
                    for value in (person.get("aliases") or [])
                    if str(value or "").strip()
                ]
                person_id = self._ensure_person_identity(
                    connection,
                    chat_name,
                    name,
                    external_id=str(person.get("external_id") or ""),
                    source="memory_stage",
                    confidence=float(person.get("confidence", 0.5) or 0.5),
                    observed_at=str(person.get("observed_at") or ""),
                )
                for alias in aliases:
                    self._upsert_person_alias(
                        connection,
                        chat_name,
                        person_id,
                        alias,
                        external_id=str(person.get("external_id") or ""),
                        source="memory_stage",
                        confidence=float(person.get("confidence", 0.5) or 0.5),
                        observed_at=str(person.get("observed_at") or ""),
                        status="suggested",
                    )
                structured_facts = [
                    value
                    for value in (person.get("facts") or [])
                    if isinstance(value, dict)
                ]
                for fact in structured_facts:
                    self._upsert_person_fact(
                        connection,
                        chat_name,
                        person_id,
                        fact,
                        default_source_event_id=max(0, int(source_event_id)),
                    )
                if profile_text and not structured_facts:
                    self._upsert_person_fact(
                        connection,
                        chat_name,
                        person_id,
                        {
                            "field": "legacy_summary",
                            "value": profile_text,
                            "status": "uncertain",
                            "confidence": float(
                                person.get("confidence", 0.5) or 0.5
                            ),
                            "observed_at": str(person.get("observed_at") or now),
                            "source_event_id": max(0, int(source_event_id)),
                        },
                        default_source_event_id=max(0, int(source_event_id)),
                    )
                projection_text = (
                    profile_text
                    or self._render_person_profile(
                        self._list_person_facts_connection(
                            connection,
                            person_id,
                        )
                    )
                )
                connection.execute(
                    """
                INSERT INTO memory_people(
                    chat_name, person_name, profile_json, profile_text,
                    source_event_id, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_name, person_name) DO UPDATE SET
                        profile_json = excluded.profile_json,
                    profile_text = excluded.profile_text,
                    source_event_id = excluded.source_event_id,
                    updated_at = excluded.updated_at
                WHERE memory_people.manual_override = 0
                    """,
                    (
                        chat_name,
                        name,
                        _json_dump(person),
                        projection_text,
                        max(0, int(source_event_id)),
                        now,
                    ),
                )
                count += 1
        return count

    def observe_message_identities(
        self,
        chat_name: str,
        messages: Iterable[Dict[str, Any]],
        *,
        source: str = "message",
    ) -> int:
        observed = 0
        with self._connection() as connection:
            for message in messages:
                if not isinstance(message, dict):
                    continue
                name = str(message.get("sender") or "").strip()
                external_id = str(message.get("sender_id") or "").strip()
                sender_remark = str(
                    message.get("sender_remark") or ""
                ).strip()
                if self.is_non_person_sender(
                    chat_name,
                    name,
                    external_id,
                ):
                    continue
                if (
                    not external_id
                    and sender_remark
                    and sender_remark.casefold() != name.casefold()
                ):
                    hinted = connection.execute(
                        """
                        SELECT DISTINCT identity.id
                        FROM memory_person_aliases AS alias
                        JOIN memory_person_identities AS identity
                          ON identity.id = alias.person_id
                        WHERE alias.chat_name = ?
                          AND alias.alias_name = ?
                          AND alias.status = 'confirmed'
                          AND identity.status = 'active'
                        ORDER BY identity.id
                        """,
                        (chat_name, sender_remark),
                    ).fetchall()
                    name_owners = connection.execute(
                        """
                        SELECT DISTINCT identity.id
                        FROM memory_person_aliases AS alias
                        JOIN memory_person_identities AS identity
                          ON identity.id = alias.person_id
                        WHERE alias.chat_name = ?
                          AND alias.alias_name = ?
                          AND alias.status = 'confirmed'
                          AND identity.status = 'active'
                        ORDER BY identity.id
                        """,
                        (chat_name, name),
                    ).fetchall()
                    if len(hinted) == 1 and (
                        not name_owners
                        or {
                            int(row["id"]) for row in name_owners
                        }
                        == {int(hinted[0]["id"])}
                    ):
                        self._upsert_person_alias(
                            connection,
                            chat_name,
                            int(hinted[0]["id"]),
                            name,
                            source="live_sender_remark",
                            confidence=0.98,
                            status="confirmed",
                            observed_at=str(message.get("time") or ""),
                        )
                        observed += 1
                        continue
                self._ensure_person_identity(
                    connection,
                    chat_name,
                    name,
                    external_id=external_id,
                    source=source,
                    confidence=1.0 if message.get("sender_id") else 0.8,
                    observed_at=str(message.get("time") or ""),
                )
                observed += 1
        return observed

    def resolve_person(
        self,
        chat_name: str,
        name: str,
        *,
        external_id: str = "",
        create: bool = False,
    ) -> Optional[Dict[str, Any]]:
        alias = str(name or "").strip()
        stable_id = str(external_id or "").strip()
        if not alias and not stable_id:
            return None
        with self._connection() as connection:
            if create and alias:
                person_id = self._ensure_person_identity(
                    connection,
                    chat_name,
                    alias,
                    external_id=stable_id,
                    source="resolver",
                )
            else:
                params: List[Any] = [chat_name]
                clauses = ["alias.chat_name = ?", "alias.status = 'confirmed'"]
                if stable_id:
                    clauses.append("alias.external_id = ?")
                    params.append(stable_id)
                else:
                    clauses.append("alias.alias_name = ?")
                    params.append(alias)
                rows = connection.execute(
                    """
                    SELECT DISTINCT identity.*
                    FROM memory_person_aliases AS alias
                    JOIN memory_person_identities AS identity
                      ON identity.id = alias.person_id
                    WHERE """
                    + " AND ".join(clauses)
                    + " AND identity.status = 'active' ORDER BY identity.id",
                    params,
                ).fetchall()
                if len(rows) != 1:
                    return None
                person_id = int(rows[0]["id"])
            row = connection.execute(
                """
                SELECT * FROM memory_person_identities
                WHERE id = ? AND chat_name = ? AND status = 'active'
                """,
                (person_id, chat_name),
            ).fetchone()
            if row is None:
                return None
            value = dict(row)
            value["aliases"] = self._list_person_aliases_connection(
                connection,
                person_id,
            )
            return value

    def canonicalize_observed_people(
        self,
        chat_name: str,
        people: Iterable[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Route automatic updates only to identities observed as senders.

        Event summaries also mention relatives, public figures, shops and
        joking nicknames. Those subjects can remain inside a member's facts,
        but must not silently become first-class group-member profiles.
        Suggested aliases may route a nickname to an observed sender only
        when the mapping is unique.
        """

        candidates = [
            dict(person)
            for person in people
            if isinstance(person, dict)
            and str(person.get("name") or "").strip()
        ]
        if not candidates:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT
                    identity.id AS person_id,
                    identity.canonical_name,
                    alias.alias_name
                FROM memory_person_identities AS identity
                JOIN memory_person_aliases AS observed
                  ON observed.person_id = identity.id
                 AND observed.status = 'confirmed'
                JOIN memory_person_aliases AS alias
                  ON alias.person_id = identity.id
                 AND alias.status IN('confirmed', 'suggested')
                WHERE identity.chat_name = ?
                  AND identity.status = 'active'
                  AND identity.canonical_name != ?
                  AND (
                    (
                      observed.external_id != ''
                      AND observed.external_id NOT LIKE '%@chatroom'
                    )
                    OR observed.source LIKE '%message%'
                    OR observed.source = 'historical_sender_id'
                  )
                """,
                (chat_name, chat_name),
            ).fetchall()
        exact: Dict[str, set[tuple[int, str]]] = {}
        normalized: Dict[str, set[tuple[int, str]]] = {}
        for row in rows:
            target = (int(row["person_id"]), str(row["canonical_name"]))
            alias = str(row["alias_name"] or "").strip()
            if not alias:
                continue
            exact.setdefault(alias.casefold(), set()).add(target)
            key = re.sub(r"[\W_]+", "", alias, flags=re.UNICODE).casefold()
            if key:
                normalized.setdefault(key, set()).add(target)

        result: List[Dict[str, Any]] = []
        for person in candidates:
            name = str(person.get("name") or "").strip()
            matches = exact.get(name.casefold(), set())
            if len(matches) != 1:
                simplified = re.sub(
                    r"[（(][^）)]*[）)]$",
                    "",
                    name,
                ).strip()
                key = re.sub(
                    r"[\W_]+",
                    "",
                    simplified,
                    flags=re.UNICODE,
                ).casefold()
                matches = normalized.get(key, set()) if key else set()
            if len(matches) != 1:
                continue
            _, canonical_name = next(iter(matches))
            person["name"] = canonical_name
            result.append(person)
        return result

    @staticmethod
    def _list_person_aliases_connection(
        connection: sqlite3.Connection,
        person_id: int,
    ) -> List[Dict[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM memory_person_aliases
                WHERE person_id = ?
                ORDER BY status = 'confirmed' DESC, confidence DESC,
                         last_seen_at DESC, id
                """,
                (int(person_id),),
            ).fetchall()
        ]

    @staticmethod
    def _list_person_facts_connection(
        connection: sqlite3.Connection,
        person_id: int,
        *,
        include_deleted: bool = False,
    ) -> List[Dict[str, Any]]:
        deleted_clause = "" if include_deleted else "AND deleted_at IS NULL"
        rows = connection.execute(
            f"""
            SELECT * FROM memory_person_facts
            WHERE person_id = ? {deleted_clause}
              AND (
                source_event_id = 0
                OR EXISTS(
                    SELECT 1 FROM memory_events AS source_event
                    WHERE source_event.id = memory_person_facts.source_event_id
                      AND source_event.is_invalidated = 0
                      AND source_event.superseded_by_event_id = 0
                      AND source_event.verification_status != 'quarantined'
                )
              )
            ORDER BY
                CASE status
                    WHEN 'current' THEN 0
                    WHEN 'planned' THEN 1
                    WHEN 'uncertain' THEN 2
                    WHEN 'disputed' THEN 3
                    ELSE 4
                END,
                COALESCE(NULLIF(last_seen_at, ''), observed_at, updated_at) DESC,
                id DESC
            """,
            (int(person_id),),
        ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["source_event_ids"] = _json_load(
                value.pop("source_event_ids_json", None),
                [],
            )
            value["evidence"] = _json_load(
                value.pop("evidence_json", None),
                [],
            )
            result.append(value)
        return result

    @staticmethod
    def _render_person_profile(facts: Iterable[Dict[str, Any]]) -> str:
        values = [dict(fact) for fact in facts]
        structured = [
            fact
            for fact in values
            if fact.get("field_name") != "legacy_summary"
            and not fact.get("deleted_at")
        ]
        if not structured:
            legacy = next(
                (
                    fact
                    for fact in values
                    if fact.get("field_name") == "legacy_summary"
                    and not fact.get("deleted_at")
                ),
                None,
            )
            return str(legacy.get("value") or "") if legacy else ""
        lines: List[str] = []
        for fact in structured:
            label = PERSON_FIELD_LABELS.get(
                str(fact.get("field_name") or "other"),
                "其他",
            )
            time_parts = []
            if fact.get("valid_from"):
                time_parts.append(f"自 {fact['valid_from']}")
            if fact.get("valid_to"):
                time_parts.append(f"至 {fact['valid_to']}")
            elif fact.get("last_seen_at") or fact.get("observed_at"):
                time_parts.append(
                    f"最后确认 {fact.get('last_seen_at') or fact.get('observed_at')}"
                )
            status = str(fact.get("status") or "current")
            status_note = {
                "historical": "历史",
                "planned": "计划",
                "uncertain": "待确认",
                "disputed": "有争议",
            }.get(status, "")
            suffix = "；".join(
                value for value in [status_note, *time_parts] if value
            )
            lines.append(
                f"{label}：{fact.get('value') or ''}"
                + (f"（{suffix}）" if suffix else "")
            )
        return "\n".join(lines)

    def list_people(
        self,
        chat_name: str,
        *,
        include_empty: bool = False,
    ) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_person_identities
                WHERE chat_name = ? AND status = 'active'
                ORDER BY canonical_name
                """,
                (chat_name,),
            ).fetchall()
            result = []
            for row in rows:
                identity = dict(row)
                person_id = int(identity["id"])
                facts = self._list_person_facts_connection(
                    connection,
                    person_id,
                )
                legacy = connection.execute(
                    """
                    SELECT * FROM memory_people
                    WHERE chat_name = ? AND person_name = ?
                    """,
                    (chat_name, identity["canonical_name"]),
                ).fetchone()
                if not facts and legacy is None and not include_empty:
                    continue
                aliases = self._list_person_aliases_connection(
                    connection,
                    person_id,
                )
                legacy_value = dict(legacy) if legacy is not None else {}
                structured_facts = [
                    fact
                    for fact in facts
                    if fact.get("field_name") != "legacy_summary"
                ]
                profile_text = (
                    self._render_person_profile(structured_facts)
                    if structured_facts
                    else str(legacy_value.get("profile_text") or "")
                    or self._render_person_profile(facts)
                )
                source_event_id = max(
                    [
                        int(fact.get("source_event_id") or 0)
                        for fact in facts
                    ]
                    + [int(legacy_value.get("source_event_id") or 0)]
                )
                updated_at = max(
                    [
                        str(fact.get("updated_at") or "")
                        for fact in facts
                    ]
                    + [
                        str(identity.get("updated_at") or ""),
                        str(legacy_value.get("updated_at") or ""),
                    ]
                )
                result.append(
                    {
                        "person_id": person_id,
                        "person_name": str(identity["canonical_name"]),
                        "canonical_name": str(identity["canonical_name"]),
                        "aliases": aliases,
                        "facts": structured_facts,
                        "legacy_facts": [
                            fact
                            for fact in facts
                            if fact.get("field_name") == "legacy_summary"
                        ],
                        "profile_text": profile_text,
                        "profile": {
                            "schema_version": 2,
                            "name": str(identity["canonical_name"]),
                            "aliases": [
                                alias["alias_name"]
                                for alias in aliases
                                if alias.get("status") == "confirmed"
                            ],
                            "facts": structured_facts,
                        },
                        "source_event_id": source_event_id,
                        "manual_override": max(
                            [int(fact.get("manual_override") or 0) for fact in facts]
                            + [int(legacy_value.get("manual_override") or 0)]
                        ),
                        "manual_note": str(
                            legacy_value.get("manual_note") or ""
                        ),
                        "updated_at": updated_at,
                    }
                )
        return result

    def list_person_directory(self, chat_name: str) -> List[Dict[str, Any]]:
        """Return every active identity, including identities without a profile."""
        people = self.list_people(chat_name, include_empty=True)
        with self._connection() as connection:
            available_tables = {
                str(row["name"])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name LIKE 'memory_person_%'
                    """
                ).fetchall()
            }
            count_specs = (
                ("memory_person_observations", "observation_count"),
                ("memory_person_message_links", "source_link_count"),
                ("memory_person_claim_candidates", "candidate_count"),
            )
            for person in people:
                person_id = int(person.get("person_id") or 0)
                person["has_stable_external_id"] = any(
                    str(alias.get("external_id") or "").strip()
                    and alias.get("status") == "confirmed"
                    for alias in person.get("aliases") or []
                )
                person["v2_fact_count"] = len(person.get("facts") or [])
                for table, key in count_specs:
                    if table not in available_tables:
                        person[key] = 0
                        continue
                    row = connection.execute(
                        f"""
                        SELECT COUNT(*) AS value FROM {table}
                        WHERE chat_name = ? AND person_id = ?
                        """,
                        (chat_name, person_id),
                    ).fetchone()
                    person[key] = int(row["value"] if row else 0)
        return people

    @staticmethod
    def _person_identity_strength(person: Dict[str, Any]) -> float:
        return (
            (100000.0 if person.get("has_stable_external_id") else 0.0)
            + float(person.get("source_link_count") or 0) * 4.0
            + float(person.get("observation_count") or 0) * 8.0
            + float(person.get("candidate_count") or 0) * 2.0
            + float(person.get("v2_fact_count") or 0) * 10.0
        )

    def list_person_merge_suggestions(
        self,
        chat_name: str,
    ) -> List[Dict[str, Any]]:
        """Suggest high-precision identity merges without applying them."""
        people = self.list_person_directory(chat_name)
        by_id = {
            int(person.get("person_id") or 0): person
            for person in people
        }
        canonical_lookup = {
            str(person.get("canonical_name") or "").strip().casefold(): person
            for person in people
            if str(person.get("canonical_name") or "").strip()
        }
        suggestions: Dict[tuple[int, int], Dict[str, Any]] = {}

        def add(
            left: Dict[str, Any],
            right: Dict[str, Any],
            *,
            reason: str,
            confidence: float,
            preferred_target_id: int = 0,
        ) -> None:
            left_id = int(left.get("person_id") or 0)
            right_id = int(right.get("person_id") or 0)
            if not left_id or not right_id or left_id == right_id:
                return
            if preferred_target_id in {left_id, right_id}:
                target = by_id[preferred_target_id]
                source = right if preferred_target_id == left_id else left
            else:
                left_strength = self._person_identity_strength(left)
                right_strength = self._person_identity_strength(right)
                if (right_strength, -right_id) > (left_strength, -left_id):
                    source, target = left, right
                else:
                    source, target = right, left
            key = (
                int(source.get("person_id") or 0),
                int(target.get("person_id") or 0),
            )
            current = suggestions.get(key)
            if current is None:
                suggestions[key] = {
                    "source_person_id": key[0],
                    "source_name": str(source.get("canonical_name") or ""),
                    "target_person_id": key[1],
                    "target_name": str(target.get("canonical_name") or ""),
                    "confidence": float(confidence),
                    "reasons": [reason],
                }
                return
            current["confidence"] = max(
                float(current.get("confidence") or 0.0),
                float(confidence),
            )
            if reason not in current["reasons"]:
                current["reasons"].append(reason)

        for owner in people:
            owner_id = int(owner.get("person_id") or 0)
            for alias in owner.get("aliases") or []:
                alias_name = str(alias.get("alias_name") or "").strip()
                other = canonical_lookup.get(alias_name.casefold())
                if other is None or int(other.get("person_id") or 0) == owner_id:
                    continue
                alias_status = str(alias.get("status") or "")
                confidence = (
                    0.99
                    if alias_status == "confirmed"
                    and str(alias.get("external_id") or "").strip()
                    else 0.94
                    if alias_status == "confirmed"
                    else 0.86
                )
                add(
                    other,
                    owner,
                    reason=(
                        f"“{alias_name}”既是独立身份名称，也是"
                        f"“{owner.get('canonical_name')}”的{alias_status or '候选'}别名"
                    ),
                    confidence=confidence,
                    preferred_target_id=owner_id,
                )

        bracket_pattern = re.compile(r"^\s*(.*?)\s*[（(]([^）)]+)[）)]\s*$")
        for composite in people:
            name = str(composite.get("canonical_name") or "").strip()
            match = bracket_pattern.match(name)
            if match is None:
                continue
            outer = match.group(1).strip()
            inner = match.group(2).strip()
            matches: List[Dict[str, Any]] = []
            for token in (outer, inner):
                direct = canonical_lookup.get(token.casefold())
                if direct is not None and direct is not composite:
                    matches.append(direct)
                for owner in people:
                    if owner is composite:
                        continue
                    if any(
                        str(alias.get("alias_name") or "").strip().casefold()
                        == token.casefold()
                        for alias in owner.get("aliases") or []
                    ):
                        matches.append(owner)
            unique = {
                int(person.get("person_id") or 0): person
                for person in matches
            }
            if not unique:
                continue
            target = max(
                unique.values(),
                key=lambda item: (
                    self._person_identity_strength(item),
                    -int(item.get("person_id") or 0),
                ),
            )
            add(
                composite,
                target,
                reason=(
                    f"复合昵称“{name}”可拆为姓名“{outer}”和昵称“{inner}”，"
                    f"与现有身份“{target.get('canonical_name')}”重合"
                ),
                confidence=0.88,
                preferred_target_id=int(target.get("person_id") or 0),
            )

        return sorted(
            suggestions.values(),
            key=lambda item: (
                float(item.get("confidence") or 0.0),
                self._person_identity_strength(
                    by_id.get(int(item.get("target_person_id") or 0), {})
                ),
            ),
            reverse=True,
        )

    def count_people(self, chat_name: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS value
                FROM memory_person_identities AS identity
                WHERE identity.chat_name = ? AND identity.status = 'active'
                  AND (
                    EXISTS(
                        SELECT 1 FROM memory_person_facts AS fact
                        WHERE fact.person_id = identity.id
                          AND fact.deleted_at IS NULL
                    )
                    OR EXISTS(
                        SELECT 1 FROM memory_people AS legacy
                        WHERE legacy.chat_name = identity.chat_name
                          AND legacy.person_name = identity.canonical_name
                    )
                  )
                """,
                (chat_name,),
            ).fetchone()
        return int(row["value"] if row else 0)

    def clear_generated_person_facts(self, chat_name: str) -> int:
        """Clear rebuildable facts while retaining manual edits and legacy input."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM memory_person_facts
                WHERE chat_name = ? AND manual_override = 0
                  AND field_name != 'legacy_summary'
                """,
                (chat_name,),
            )
            connection.execute(
                """
                DELETE FROM memory_person_aliases
                WHERE chat_name = ? AND status = 'suggested'
                  AND source IN('memory_stage', 'person_rebuild')
                """,
                (chat_name,),
            )
        return max(0, int(cursor.rowcount or 0))

    def retire_legacy_person_profiles(self, chat_name: str) -> Dict[str, int]:
        """Remove legacy prose projections after structured facts are ready."""

        with self._connection() as connection:
            facts = connection.execute(
                """
                DELETE FROM memory_person_facts
                WHERE chat_name = ? AND field_name = 'legacy_summary'
                """,
                (chat_name,),
            )
            profiles = connection.execute(
                """
                DELETE FROM memory_people
                WHERE chat_name = ?
                """,
                (chat_name,),
            )
        return {
            "facts": max(0, int(facts.rowcount or 0)),
            "profiles": max(0, int(profiles.rowcount or 0)),
        }

    def finalize_person_fact_temporality(
        self,
        chat_name: str,
        *,
        reference_time: str = "",
    ) -> int:
        """Mark older single-value and stale volatile observations historical."""
        reference = str(reference_time or "").strip()
        try:
            reference_date = datetime.fromisoformat(reference).replace(tzinfo=None)
        except ValueError:
            reference_date = datetime.now()
        changed = 0
        with self._connection() as connection:
            identities = connection.execute(
                """
                SELECT id FROM memory_person_identities
                WHERE chat_name = ? AND status = 'active'
                """,
                (chat_name,),
            ).fetchall()
            now = self._now()
            for identity in identities:
                person_id = int(identity["id"])
                facts = connection.execute(
                    """
                    SELECT * FROM memory_person_facts
                    WHERE chat_name = ? AND person_id = ?
                      AND deleted_at IS NULL AND manual_override = 0
                      AND field_name != 'legacy_summary'
                    ORDER BY COALESCE(
                        NULLIF(last_seen_at, ''),
                        NULLIF(observed_at, ''),
                        NULLIF(valid_from, ''),
                        created_at
                    ) DESC, id DESC
                    """,
                    (chat_name, person_id),
                ).fetchall()
                by_field: Dict[str, List[sqlite3.Row]] = {}
                for fact in facts:
                    by_field.setdefault(str(fact["field_name"]), []).append(fact)
                for field_name in (
                    "employer",
                    "location",
                    "current_status",
                ):
                    current_values = [
                        fact
                        for fact in by_field.get(field_name, [])
                        if str(fact["status"]) == "current"
                    ]
                    if len(current_values) <= 1:
                        continue
                    for fact in current_values[1:]:
                        cursor = connection.execute(
                            """
                            UPDATE memory_person_facts SET
                                status = 'historical',
                                valid_to = COALESCE(
                                    valid_to,
                                    NULLIF(?, '')
                                ),
                                updated_at = ?
                            WHERE id = ? AND manual_override = 0
                            """,
                            (
                                str(
                                    current_values[0]["valid_from"]
                                    or current_values[0]["observed_at"]
                                    or ""
                                ),
                                now,
                                int(fact["id"]),
                            ),
                        )
                        changed += max(0, int(cursor.rowcount or 0))
                for field_name in (
                    "health",
                    "current_status",
                    "plan",
                    "asset",
                ):
                    for fact in by_field.get(field_name, []):
                        if str(fact["status"]) not in {"current", "planned"}:
                            continue
                        time_text = str(
                            fact["last_seen_at"]
                            or fact["observed_at"]
                            or fact["valid_from"]
                            or ""
                        ).strip()
                        try:
                            observed = datetime.fromisoformat(
                                time_text
                            ).replace(tzinfo=None)
                        except ValueError:
                            continue
                        stale_days = {
                            "current_status": 90,
                            "plan": 180,
                            "health": 365,
                            "asset": 730,
                        }[field_name]
                        if (reference_date - observed).days <= stale_days:
                            continue
                        cursor = connection.execute(
                            """
                            UPDATE memory_person_facts SET
                                status = 'historical',
                                valid_to = COALESCE(valid_to, ?),
                                temporal_note = CASE
                                    WHEN temporal_note = '' THEN ?
                                    ELSE temporal_note
                                END,
                                updated_at = ?
                            WHERE id = ? AND manual_override = 0
                            """,
                            (
                                time_text,
                                "长期未再次确认，按历史状态保留",
                                now,
                                int(fact["id"]),
                            ),
                        )
                        changed += max(0, int(cursor.rowcount or 0))
                for fact in facts:
                    if str(fact["status"]) != "planned":
                        continue
                    time_text = str(
                        fact["last_seen_at"]
                        or fact["observed_at"]
                        or fact["valid_from"]
                        or ""
                    ).strip()
                    try:
                        observed = datetime.fromisoformat(
                            time_text
                        ).replace(tzinfo=None)
                    except ValueError:
                        continue
                    if (reference_date - observed).days <= 180:
                        continue
                    cursor = connection.execute(
                        """
                        UPDATE memory_person_facts SET
                            status = 'historical',
                            valid_to = COALESCE(valid_to, ?),
                            temporal_note = CASE
                                WHEN temporal_note = '' THEN ?
                                ELSE temporal_note
                            END,
                            updated_at = ?
                        WHERE id = ? AND manual_override = 0
                        """,
                        (
                            time_text,
                            "计划已超过时效窗口，按历史状态保留",
                            now,
                            int(fact["id"]),
                        ),
                    )
                    changed += max(0, int(cursor.rowcount or 0))
        return changed

    def apply_person_fact_selection(
        self,
        chat_name: str,
        person_id: int,
        *,
        keep_fact_ids: Iterable[int],
        status_updates: Optional[Dict[int, str]] = None,
        reason: str = "人物资料质量压缩",
    ) -> Dict[str, int]:
        keep_ids = {
            int(value)
            for value in keep_fact_ids
            if int(value) > 0
        }
        updates = status_updates or {}
        now = self._now()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id FROM memory_person_facts
                WHERE chat_name = ? AND person_id = ?
                  AND field_name != 'legacy_summary'
                  AND manual_override = 0
                """,
                (chat_name, int(person_id)),
            ).fetchall()
            all_ids = {int(row["id"]) for row in rows}
            remove_ids = all_ids - keep_ids
            if remove_ids:
                placeholders = ",".join("?" for _ in remove_ids)
                connection.execute(
                    f"""
                    UPDATE memory_person_facts SET
                        deleted_at = ?,
                        temporal_note = CASE
                            WHEN temporal_note = '' THEN ?
                            ELSE temporal_note
                        END,
                        updated_at = ?
                    WHERE id IN({placeholders})
                    """,
                    (
                        now,
                        str(reason or "人物资料质量压缩"),
                        now,
                        *sorted(remove_ids),
                    ),
                )
            if keep_ids:
                placeholders = ",".join("?" for _ in keep_ids)
                connection.execute(
                    f"""
                    UPDATE memory_person_facts
                    SET deleted_at = NULL, updated_at = ?
                    WHERE id IN({placeholders})
                    """,
                    (now, *sorted(keep_ids)),
                )
            changed_status = 0
            for fact_id, status in updates.items():
                normalized = str(status or "").strip().lower()
                if int(fact_id) not in keep_ids:
                    continue
                if normalized not in PERSON_FACT_STATUSES:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE memory_person_facts
                    SET status = ?, updated_at = ?
                    WHERE chat_name = ? AND person_id = ? AND id = ?
                      AND manual_override = 0
                    """,
                    (
                        normalized,
                        now,
                        chat_name,
                        int(person_id),
                        int(fact_id),
                    ),
                )
                changed_status += max(0, int(cursor.rowcount or 0))
        return {
            "kept": len(all_ids & keep_ids),
            "pruned": len(remove_ids),
            "status_updates": changed_status,
        }

    def _person_snapshot(
        self,
        connection: sqlite3.Connection,
        chat_name: str,
        person_ids: Iterable[int],
        *,
        include_v3_derived: bool = False,
    ) -> Dict[str, Any]:
        ids = sorted({int(value) for value in person_ids if int(value) > 0})
        if not ids:
            return {
                "identities": [],
                "aliases": [],
                "facts": [],
            }
        placeholders = ",".join("?" for _ in ids)
        identities = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT * FROM memory_person_identities
                WHERE chat_name = ? AND id IN({placeholders})
                ORDER BY id
                """,
                (chat_name, *ids),
            ).fetchall()
        ]
        aliases = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT * FROM memory_person_aliases
                WHERE chat_name = ? AND person_id IN({placeholders})
                ORDER BY id
                """,
                (chat_name, *ids),
            ).fetchall()
        ]
        facts = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT * FROM memory_person_facts
                WHERE chat_name = ? AND person_id IN({placeholders})
                ORDER BY id
                """,
                (chat_name, *ids),
            ).fetchall()
        ]
        result = {
            "identities": identities,
            "aliases": aliases,
            "facts": facts,
        }
        if not include_v3_derived:
            return result

        table_names = {
            str(row["name"])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name LIKE 'memory_person_%'
                """
            ).fetchall()
        }
        v3_tables: Dict[str, List[Dict[str, Any]]] = {}
        for table in (
            "memory_person_fact_versions",
            "memory_person_patterns",
            "memory_person_period_summaries",
            "memory_person_snapshots",
            "memory_person_refresh_state",
            "memory_person_v3_suppressions",
            "memory_person_pipeline_state",
        ):
            if table not in table_names:
                continue
            v3_tables[table] = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT * FROM {table}
                    WHERE chat_name = ? AND person_id IN({placeholders})
                    ORDER BY person_id
                    """,
                    (chat_name, *ids),
                ).fetchall()
            ]
        if "memory_person_relationships" in table_names:
            v3_tables["memory_person_relationships"] = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT * FROM memory_person_relationships
                    WHERE chat_name = ?
                      AND (
                        person_id IN({placeholders})
                        OR target_person_id IN({placeholders})
                      )
                    ORDER BY id
                    """,
                    (chat_name, *ids, *ids),
                ).fetchall()
            ]
        result["v3_derived"] = v3_tables
        return result

    @staticmethod
    def _restore_rows(
        connection: sqlite3.Connection,
        table: str,
        rows: Iterable[Dict[str, Any]],
    ) -> None:
        for row in rows:
            columns = list(row)
            if not columns:
                continue
            connection.execute(
                f"""
                INSERT INTO {table}({",".join(columns)})
                VALUES({",".join("?" for _ in columns)})
                """,
                [row[column] for column in columns],
            )

    def _record_person_audit(
        self,
        connection: sqlite3.Connection,
        chat_name: str,
        *,
        action: str,
        reason: str,
        affected_person_ids: Iterable[int],
        before: Dict[str, Any],
        after: Dict[str, Any],
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO memory_person_audit(
                chat_name, action, reason, affected_person_ids_json,
                before_json, after_json, status, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                chat_name,
                str(action or "person_change"),
                str(reason or "").strip(),
                _json_dump(
                    sorted(
                        {
                            int(value)
                            for value in affected_person_ids
                            if int(value) > 0
                        }
                    )
                ),
                _json_dump(before),
                _json_dump(after),
                self._now(),
            ),
        )
        return int(cursor.lastrowid)

    def add_person_alias(
        self,
        chat_name: str,
        person_id: int,
        *,
        alias_name: str,
        reason: str,
        external_id: str = "",
    ) -> Dict[str, Any]:
        alias = str(alias_name or "").strip()
        note = str(reason or "").strip()
        if not alias or not note:
            raise ValueError("alias and reason are required")
        with self._connection() as connection:
            identity = connection.execute(
                """
                SELECT * FROM memory_person_identities
                WHERE chat_name = ? AND id = ? AND status = 'active'
                """,
                (chat_name, int(person_id)),
            ).fetchone()
            if identity is None:
                raise ValueError("person does not exist")
            conflicts = connection.execute(
                """
                SELECT DISTINCT person_id FROM memory_person_aliases
                WHERE chat_name = ? AND alias_name = ?
                  AND status = 'confirmed' AND person_id != ?
                """,
                (chat_name, alias, int(person_id)),
            ).fetchall()
            if conflicts:
                raise ValueError("该别名已属于其他人物，请使用人物合并")
            before = self._person_snapshot(
                connection,
                chat_name,
                [int(person_id)],
            )
            self._upsert_person_alias(
                connection,
                chat_name,
                int(person_id),
                alias,
                external_id=external_id,
                source="manual",
                confidence=1.0,
                status="confirmed",
            )
            after = self._person_snapshot(
                connection,
                chat_name,
                [int(person_id)],
            )
            audit_id = self._record_person_audit(
                connection,
                chat_name,
                action="add_alias",
                reason=note,
                affected_person_ids=[int(person_id)],
                before=before,
                after=after,
            )
        return self.get_person_audit(chat_name, audit_id) or {}

    def move_person_alias(
        self,
        chat_name: str,
        source_person_id: int,
        target_person_id: int,
        *,
        alias_name: str,
        reason: str,
    ) -> Dict[str, Any]:
        """Move a wrongly assigned alias with a reversible audit record."""

        source_id = int(source_person_id)
        target_id = int(target_person_id)
        alias = str(alias_name or "").strip()
        note = str(reason or "").strip()
        if (
            source_id <= 0
            or target_id <= 0
            or source_id == target_id
        ):
            raise ValueError("source and target people must be different")
        if not alias or not note:
            raise ValueError("alias and reason are required")
        affected = [source_id, target_id]
        with self._connection() as connection:
            identities = connection.execute(
                """
                SELECT id FROM memory_person_identities
                WHERE chat_name = ? AND id IN(?, ?) AND status = 'active'
                """,
                (chat_name, source_id, target_id),
            ).fetchall()
            if len(identities) != 2:
                raise ValueError("source or target person does not exist")
            source_alias = connection.execute(
                """
                SELECT * FROM memory_person_aliases
                WHERE chat_name = ? AND person_id = ? AND alias_name = ?
                """,
                (chat_name, source_id, alias),
            ).fetchone()
            if source_alias is None:
                raise ValueError("source person does not own this alias")
            other_owners = connection.execute(
                """
                SELECT DISTINCT person_id FROM memory_person_aliases
                WHERE chat_name = ? AND alias_name = ?
                  AND status = 'confirmed'
                  AND person_id NOT IN(?, ?)
                """,
                (chat_name, alias, source_id, target_id),
            ).fetchall()
            if other_owners:
                raise ValueError("alias is also owned by another person")
            before = self._person_snapshot(
                connection,
                chat_name,
                affected,
            )
            target_alias = connection.execute(
                """
                SELECT * FROM memory_person_aliases
                WHERE chat_name = ? AND person_id = ? AND alias_name = ?
                """,
                (chat_name, target_id, alias),
            ).fetchone()
            now = self._now()
            if target_alias is None:
                connection.execute(
                    """
                    UPDATE memory_person_aliases SET
                        person_id = ?,
                        source = 'manual_alias_correction',
                        confidence = 1.0,
                        status = 'confirmed',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (target_id, now, int(source_alias["id"])),
                )
            else:
                connection.execute(
                    """
                    UPDATE memory_person_aliases SET
                        external_id = CASE
                            WHEN external_id = '' THEN ? ELSE external_id
                        END,
                        source = 'manual_alias_correction',
                        confidence = 1.0,
                        status = 'confirmed',
                        first_seen_at = COALESCE(
                            first_seen_at, ?
                        ),
                        last_seen_at = MAX(
                            COALESCE(last_seen_at, ''),
                            COALESCE(?, '')
                        ),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(source_alias["external_id"] or ""),
                        source_alias["first_seen_at"],
                        source_alias["last_seen_at"],
                        now,
                        int(target_alias["id"]),
                    ),
                )
                connection.execute(
                    "DELETE FROM memory_person_aliases WHERE id = ?",
                    (int(source_alias["id"]),),
                )
            connection.execute(
                """
                UPDATE memory_person_identities
                SET updated_at = ? WHERE id IN(?, ?)
                """,
                (now, source_id, target_id),
            )
            after = self._person_snapshot(
                connection,
                chat_name,
                affected,
            )
            audit_id = self._record_person_audit(
                connection,
                chat_name,
                action="move_alias",
                reason=note,
                affected_person_ids=affected,
                before=before,
                after=after,
            )
        return self.get_person_audit(chat_name, audit_id) or {}

    def merge_people(
        self,
        chat_name: str,
        source_person_id: int,
        target_person_id: int,
        *,
        reason: str,
    ) -> Dict[str, Any]:
        source_id = int(source_person_id)
        target_id = int(target_person_id)
        note = str(reason or "").strip()
        if source_id <= 0 or target_id <= 0 or source_id == target_id:
            raise ValueError("source and target people must be different")
        if not note:
            raise ValueError("merge reason is required")
        affected = [source_id, target_id]
        with self._connection() as connection:
            identities = connection.execute(
                """
                SELECT * FROM memory_person_identities
                WHERE chat_name = ? AND id IN(?, ?) AND status = 'active'
                """,
                (chat_name, source_id, target_id),
            ).fetchall()
            if len(identities) != 2:
                raise ValueError("source or target person does not exist")
            before = self._person_snapshot(
                connection,
                chat_name,
                affected,
                include_v3_derived=True,
            )
            table_names = {
                str(row["name"])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table'
                    """
                ).fetchall()
            }
            source_aliases = connection.execute(
                """
                SELECT * FROM memory_person_aliases
                WHERE chat_name = ? AND person_id = ? ORDER BY id
                """,
                (chat_name, source_id),
            ).fetchall()
            for alias in source_aliases:
                target_alias = connection.execute(
                    """
                    SELECT * FROM memory_person_aliases
                    WHERE chat_name = ? AND person_id = ? AND alias_name = ?
                    """,
                    (chat_name, target_id, str(alias["alias_name"])),
                ).fetchone()
                if target_alias is not None:
                    if (
                        str(alias["status"] or "") == "confirmed"
                        and str(target_alias["status"] or "") != "confirmed"
                    ):
                        connection.execute(
                            """
                            UPDATE memory_person_aliases SET
                                external_id = CASE
                                    WHEN external_id = '' THEN ?
                                    ELSE external_id
                                END,
                                source = 'identity_merge',
                                confidence = MAX(confidence, ?),
                                status = 'confirmed',
                                first_seen_at = CASE
                                    WHEN first_seen_at IS NULL THEN ?
                                    WHEN ? IS NULL THEN first_seen_at
                                    ELSE MIN(first_seen_at, ?)
                                END,
                                last_seen_at = CASE
                                    WHEN last_seen_at IS NULL THEN ?
                                    WHEN ? IS NULL THEN last_seen_at
                                    ELSE MAX(last_seen_at, ?)
                                END,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                str(alias["external_id"] or ""),
                                float(alias["confidence"] or 1.0),
                                alias["first_seen_at"],
                                alias["first_seen_at"],
                                alias["first_seen_at"],
                                alias["last_seen_at"],
                                alias["last_seen_at"],
                                alias["last_seen_at"],
                                self._now(),
                                int(target_alias["id"]),
                            ),
                        )
                    connection.execute(
                        "DELETE FROM memory_person_aliases WHERE id = ?",
                        (int(alias["id"]),),
                    )
                    continue
                connection.execute(
                    """
                    UPDATE memory_person_aliases
                    SET person_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (target_id, self._now(), int(alias["id"])),
                )
            source_facts = connection.execute(
                """
                SELECT * FROM memory_person_facts
                WHERE chat_name = ? AND person_id = ? ORDER BY id
                """,
                (chat_name, source_id),
            ).fetchall()
            for fact in source_facts:
                target_fact = connection.execute(
                    """
                    SELECT * FROM memory_person_facts
                    WHERE chat_name = ? AND person_id = ? AND fact_key = ?
                    """,
                    (chat_name, target_id, str(fact["fact_key"])),
                ).fetchone()
                if target_fact is None:
                    connection.execute(
                        """
                        UPDATE memory_person_facts
                        SET person_id = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (target_id, self._now(), int(fact["id"])),
                    )
                    continue
                source_events = set(
                    _json_load(fact["source_event_ids_json"], [])
                )
                target_events = set(
                    _json_load(target_fact["source_event_ids_json"], [])
                )
                source_evidence = list(
                    _json_load(fact["evidence_json"], [])
                )
                target_evidence = list(
                    _json_load(target_fact["evidence_json"], [])
                )
                for item in source_evidence:
                    if item not in target_evidence:
                        target_evidence.append(item)
                connection.execute(
                    """
                    UPDATE memory_person_facts SET
                        confidence = MAX(confidence, ?),
                        source_event_id = MAX(source_event_id, ?),
                        source_event_ids_json = ?,
                        evidence_json = ?,
                        mention_count = mention_count + ?,
                        first_seen_at = CASE
                            WHEN first_seen_at IS NULL THEN ?
                            WHEN ? IS NULL THEN first_seen_at
                            ELSE MIN(first_seen_at, ?)
                        END,
                        last_seen_at = CASE
                            WHEN last_seen_at IS NULL THEN ?
                            WHEN ? IS NULL THEN last_seen_at
                            ELSE MAX(last_seen_at, ?)
                        END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        float(fact["confidence"] or 0.0),
                        int(fact["source_event_id"] or 0),
                        _json_dump(sorted(source_events | target_events)),
                        _json_dump(target_evidence[:40]),
                        int(fact["mention_count"] or 1),
                        fact["first_seen_at"],
                        fact["first_seen_at"],
                        fact["first_seen_at"],
                        fact["last_seen_at"],
                        fact["last_seen_at"],
                        fact["last_seen_at"],
                        self._now(),
                        int(target_fact["id"]),
                    ),
                )
                connection.execute(
                    "DELETE FROM memory_person_facts WHERE id = ?",
                    (int(fact["id"]),),
                )

            copied_artifacts: Dict[str, List[int]] = {
                "observation": [],
                "message_link": [],
                "suppression": [],
            }
            if "memory_person_observations" in table_names:
                source_observations = connection.execute(
                    """
                    SELECT * FROM memory_person_observations
                    WHERE chat_name = ? AND person_id = ?
                    ORDER BY id
                    """,
                    (chat_name, source_id),
                ).fetchall()
                for row in source_observations:
                    value = dict(row)
                    evidence_cursors = _json_load(
                        value.get("evidence_cursors_json"),
                        [],
                    )
                    fingerprint_material = "|".join(
                        (
                            str(target_id),
                            str(
                                value.get("observation_type")
                                or "objective_fact"
                            ),
                            str(value.get("field_name") or "other"),
                            str(value.get("normalized_statement") or ""),
                            str(
                                value.get("source_namespace")
                                or "live_chat_log"
                            ),
                            ",".join(
                                str(int(cursor))
                                for cursor in evidence_cursors
                                if int(cursor) > 0
                            ),
                        )
                    )
                    value["person_id"] = target_id
                    value["fingerprint"] = hashlib.sha256(
                        fingerprint_material.encode("utf-8")
                    ).hexdigest()
                    context = _json_load(value.get("context_json"), {})
                    if not isinstance(context, dict):
                        context = {}
                    context["identity_merge"] = {
                        "source_person_id": source_id,
                        "source_observation_id": int(value["id"]),
                    }
                    value["context_json"] = _json_dump(context)
                    value["extractor_version"] = (
                        str(value.get("extractor_version") or "person-v3")
                        + "+identity-merge"
                    )[:100]
                    value["updated_at"] = self._now()
                    value.pop("id", None)
                    columns = list(value)
                    cursor = connection.execute(
                        f"""
                        INSERT OR IGNORE INTO memory_person_observations(
                            {",".join(columns)}
                        ) VALUES({",".join("?" for _ in columns)})
                        """,
                        [value[column] for column in columns],
                    )
                    if int(cursor.rowcount or 0) > 0:
                        copied_artifacts["observation"].append(
                            int(cursor.lastrowid)
                        )

            if "memory_person_message_links" in table_names:
                source_links = connection.execute(
                    """
                    SELECT * FROM memory_person_message_links
                    WHERE chat_name = ? AND person_id = ?
                    ORDER BY id
                    """,
                    (chat_name, source_id),
                ).fetchall()
                touched_namespaces: set[str] = set()
                for row in source_links:
                    namespace = str(row["source_namespace"] or "")
                    touched_namespaces.add(namespace)
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO memory_person_message_links(
                            chat_name, person_id, source_namespace,
                            source_message_id, relation, matched_alias,
                            created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chat_name,
                            target_id,
                            namespace,
                            int(row["source_message_id"]),
                            str(row["relation"] or ""),
                            str(row["matched_alias"] or ""),
                            self._now(),
                        ),
                    )
                    if int(cursor.rowcount or 0) > 0:
                        copied_artifacts["message_link"].append(
                            int(cursor.lastrowid)
                        )
                if "memory_person_pipeline_state" in table_names:
                    for namespace in touched_namespaces:
                        existing = connection.execute(
                            """
                            SELECT processed_link_id
                            FROM memory_person_pipeline_state
                            WHERE chat_name = ? AND person_id = ?
                              AND source_namespace = ?
                            """,
                            (chat_name, target_id, namespace),
                        ).fetchone()
                        processed_link_id = int(
                            existing["processed_link_id"]
                            if existing is not None
                            else 0
                        )
                        pending = int(
                            connection.execute(
                                """
                                SELECT COUNT(*) AS value
                                FROM memory_person_message_links
                                WHERE chat_name = ? AND person_id = ?
                                  AND source_namespace = ? AND id > ?
                                """,
                                (
                                    chat_name,
                                    target_id,
                                    namespace,
                                    processed_link_id,
                                ),
                            ).fetchone()["value"]
                        )
                        connection.execute(
                            """
                            INSERT INTO memory_person_pipeline_state(
                                chat_name, person_id, source_namespace,
                                processed_link_id, pending_link_count,
                                last_indexed_at, updated_at
                            ) VALUES(?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(
                                chat_name, person_id, source_namespace
                            ) DO UPDATE SET
                                pending_link_count =
                                    excluded.pending_link_count,
                                last_indexed_at = excluded.last_indexed_at,
                                updated_at = excluded.updated_at
                            """,
                            (
                                chat_name,
                                target_id,
                                namespace,
                                processed_link_id,
                                pending,
                                self._now(),
                                self._now(),
                            ),
                        )

            if "memory_person_v3_suppressions" in table_names:
                source_suppressions = connection.execute(
                    """
                    SELECT * FROM memory_person_v3_suppressions
                    WHERE chat_name = ? AND person_id = ?
                    ORDER BY id
                    """,
                    (chat_name, source_id),
                ).fetchall()
                for row in source_suppressions:
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO memory_person_v3_suppressions(
                            chat_name, person_id, target_type, target_key,
                            reason, status, created_at, reverted_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chat_name,
                            target_id,
                            str(row["target_type"] or ""),
                            str(row["target_key"] or ""),
                            str(row["reason"] or ""),
                            str(row["status"] or "active"),
                            str(row["created_at"] or self._now()),
                            row["reverted_at"],
                        ),
                    )
                    if int(cursor.rowcount or 0) > 0:
                        copied_artifacts["suppression"].append(
                            int(cursor.lastrowid)
                        )

            if "memory_person_relationships" in table_names:
                connection.execute(
                    """
                    UPDATE memory_person_relationships
                    SET target_person_id = ?, updated_at = ?
                    WHERE chat_name = ? AND target_person_id = ?
                    """,
                    (target_id, self._now(), chat_name, source_id),
                )

            if (
                copied_artifacts["observation"]
                and "memory_person_refresh_state" in table_names
            ):
                latest = connection.execute(
                    """
                    SELECT MAX(COALESCE(observed_at, created_at)) AS value
                    FROM memory_person_observations
                    WHERE chat_name = ? AND id IN({})
                    """.format(
                        ",".join(
                            "?" for _ in copied_artifacts["observation"]
                        )
                    ),
                    (chat_name, *copied_artifacts["observation"]),
                ).fetchone()
                pending_count = len(copied_artifacts["observation"])
                connection.execute(
                    """
                    INSERT INTO memory_person_refresh_state(
                        chat_name, person_id, pending_observation_count,
                        last_observation_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(chat_name, person_id) DO UPDATE SET
                        pending_observation_count =
                            memory_person_refresh_state.pending_observation_count
                            + excluded.pending_observation_count,
                        last_observation_at = MAX(
                            COALESCE(
                                memory_person_refresh_state.last_observation_at,
                                ''
                            ),
                            COALESCE(excluded.last_observation_at, '')
                        ),
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_name,
                        target_id,
                        pending_count,
                        str(latest["value"] or self._now()),
                        self._now(),
                    ),
                )
            connection.execute(
                """
                UPDATE memory_person_identities SET
                    status = 'merged', merged_into_person_id = ?,
                    updated_at = ?
                WHERE chat_name = ? AND id = ?
                """,
                (target_id, self._now(), chat_name, source_id),
            )
            after = self._person_snapshot(connection, chat_name, affected)
            observation_max_id = 0
            if "memory_person_observations" in table_names:
                row = connection.execute(
                    """
                    SELECT MAX(id) AS value
                    FROM memory_person_observations
                    WHERE chat_name = ?
                    """,
                    (chat_name,),
                ).fetchone()
                observation_max_id = int(row["value"] or 0)
            after["v3_merge"] = {
                "copied_observations": len(
                    copied_artifacts["observation"]
                ),
                "copied_message_links": len(
                    copied_artifacts["message_link"]
                ),
                "copied_suppressions": len(
                    copied_artifacts["suppression"]
                ),
                "refresh_required": bool(
                    copied_artifacts["observation"]
                    or copied_artifacts["message_link"]
                ),
                "observation_max_id": observation_max_id,
            }
            audit_id = self._record_person_audit(
                connection,
                chat_name,
                action="merge_people",
                reason=note,
                affected_person_ids=affected,
                before=before,
                after=after,
            )
            for artifact_type, artifact_ids in copied_artifacts.items():
                connection.executemany(
                    """
                    INSERT INTO memory_person_merge_artifacts(
                        chat_name, audit_id, artifact_type,
                        artifact_id, created_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            chat_name,
                            audit_id,
                            artifact_type,
                            int(artifact_id),
                            self._now(),
                        )
                        for artifact_id in artifact_ids
                    ],
                )
        return self.get_person_audit(chat_name, audit_id) or {}

    def upsert_person_fact_manual(
        self,
        chat_name: str,
        person_id: int,
        fact: Dict[str, Any],
        *,
        reason: str,
    ) -> Dict[str, Any]:
        note = str(reason or "").strip()
        if not note:
            raise ValueError("fact edit reason is required")
        with self._connection() as connection:
            identity = connection.execute(
                """
                SELECT id FROM memory_person_identities
                WHERE chat_name = ? AND id = ? AND status = 'active'
                """,
                (chat_name, int(person_id)),
            ).fetchone()
            if identity is None:
                raise ValueError("person does not exist")
            before = self._person_snapshot(
                connection,
                chat_name,
                [int(person_id)],
            )
            value = dict(fact)
            value["manual_override"] = True
            self._upsert_person_fact(
                connection,
                chat_name,
                int(person_id),
                value,
            )
            after = self._person_snapshot(
                connection,
                chat_name,
                [int(person_id)],
            )
            audit_id = self._record_person_audit(
                connection,
                chat_name,
                action="upsert_fact",
                reason=note,
                affected_person_ids=[int(person_id)],
                before=before,
                after=after,
            )
        return self.get_person_audit(chat_name, audit_id) or {}

    def delete_person_fact_manual(
        self,
        chat_name: str,
        person_id: int,
        fact_id: int,
        *,
        reason: str,
    ) -> Dict[str, Any]:
        note = str(reason or "").strip()
        if not note:
            raise ValueError("fact delete reason is required")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM memory_person_facts
                WHERE chat_name = ? AND person_id = ? AND id = ?
                  AND deleted_at IS NULL
                """,
                (chat_name, int(person_id), int(fact_id)),
            ).fetchone()
            if row is None:
                raise ValueError("person fact does not exist")
            before = self._person_snapshot(
                connection,
                chat_name,
                [int(person_id)],
            )
            now = self._now()
            connection.execute(
                """
                UPDATE memory_person_facts
                SET deleted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, int(fact_id)),
            )
            after = self._person_snapshot(
                connection,
                chat_name,
                [int(person_id)],
            )
            audit_id = self._record_person_audit(
                connection,
                chat_name,
                action="delete_fact",
                reason=note,
                affected_person_ids=[int(person_id)],
                before=before,
                after=after,
            )
        return self.get_person_audit(chat_name, audit_id) or {}

    @staticmethod
    def _person_audit_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        value = dict(row)
        value["affected_person_ids"] = _json_load(
            value.pop("affected_person_ids_json", None),
            [],
        )
        value["before"] = _json_load(value.pop("before_json", None), {})
        value["after"] = _json_load(value.pop("after_json", None), {})
        return value

    def get_person_audit(
        self,
        chat_name: str,
        audit_id: int,
    ) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_person_audit
                WHERE chat_name = ? AND id = ?
                """,
                (chat_name, int(audit_id)),
            ).fetchone()
        return self._person_audit_from_row(row) if row is not None else None

    def list_person_audits(
        self,
        chat_name: str,
        *,
        include_snapshots: bool = True,
    ) -> List[Dict[str, Any]]:
        projection = "*" if include_snapshots else """
            id, chat_name, action, reason, affected_person_ids_json,
            status, created_at, reverted_at
        """
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {projection} FROM memory_person_audit
                WHERE chat_name = ? ORDER BY id DESC
                """,
                (chat_name,),
            ).fetchall()
        if include_snapshots:
            return [self._person_audit_from_row(row) for row in rows]
        result = []
        for row in rows:
            value = dict(row)
            value["affected_person_ids"] = _json_load(
                value.pop("affected_person_ids_json", None),
                [],
            )
            result.append(value)
        return result

    def revert_person_audit(
        self,
        chat_name: str,
        audit_id: int,
    ) -> Dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_person_audit
                WHERE chat_name = ? AND id = ? AND status = 'active'
                """,
                (chat_name, int(audit_id)),
            ).fetchone()
            if row is None:
                raise ValueError("active person change does not exist")
            audit = self._person_audit_from_row(row)
            affected = {
                int(value)
                for value in audit.get("affected_person_ids") or []
                if int(value) > 0
            }
            later = connection.execute(
                """
                SELECT id, affected_person_ids_json
                FROM memory_person_audit
                WHERE chat_name = ? AND status = 'active' AND id > ?
                ORDER BY id DESC
                """,
                (chat_name, int(audit_id)),
            ).fetchall()
            for item in later:
                later_ids = {
                    int(value)
                    for value in _json_load(
                        item["affected_person_ids_json"],
                        [],
                    )
                    if int(value) > 0
                }
                if affected & later_ids:
                    raise ValueError(
                        "存在影响同一人物的更晚修改，"
                        f"请先撤销人物修改 #{int(item['id'])}"
                    )
            table_names = {
                str(item["name"])
                for item in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            merge_artifacts: Dict[str, List[int]] = {}
            if "memory_person_merge_artifacts" in table_names:
                artifact_rows = connection.execute(
                    """
                    SELECT artifact_type, artifact_id
                    FROM memory_person_merge_artifacts
                    WHERE chat_name = ? AND audit_id = ?
                    ORDER BY artifact_type, artifact_id
                    """,
                    (chat_name, int(audit_id)),
                ).fetchall()
                for item in artifact_rows:
                    merge_artifacts.setdefault(
                        str(item["artifact_type"]),
                        [],
                    ).append(int(item["artifact_id"]))
            if (
                audit.get("action") == "merge_people"
                and affected
                and "memory_person_v3_audit" in table_names
            ):
                placeholders = ",".join("?" for _ in affected)
                newer_v3 = connection.execute(
                    f"""
                    SELECT id FROM memory_person_v3_audit
                    WHERE chat_name = ? AND status = 'active'
                      AND person_id IN({placeholders})
                      AND created_at > ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        chat_name,
                        *sorted(affected),
                        str(audit.get("created_at") or ""),
                    ),
                ).fetchone()
                if newer_v3 is not None:
                    raise ValueError(
                        "合并后存在更晚的 v3 人工修改，"
                        f"请先处理 v3 修改 #{int(newer_v3['id'])}"
                    )
            if (
                audit.get("action") == "merge_people"
                and affected
                and "memory_person_observations" in table_names
            ):
                placeholders = ",".join("?" for _ in affected)
                merge_meta = audit.get("after", {}).get("v3_merge", {})
                observation_max_id = int(
                    merge_meta.get("observation_max_id") or 0
                )
                newer_rows = connection.execute(
                    f"""
                    SELECT id FROM memory_person_observations
                    WHERE chat_name = ? AND person_id IN({placeholders})
                      AND id > ?
                    ORDER BY id
                    """,
                    (
                        chat_name,
                        *sorted(affected),
                        observation_max_id,
                    ),
                ).fetchall()
                copied_ids = set(merge_artifacts.get("observation") or [])
                newer_external = [
                    int(item["id"])
                    for item in newer_rows
                    if int(item["id"]) not in copied_ids
                ]
                if newer_external:
                    raise ValueError(
                        "人物合并后已有新的观察进入记忆，"
                        "为避免丢失增量资料，不能直接撤销该合并"
                    )

            artifact_tables = {
                "observation": "memory_person_observations",
                "message_link": "memory_person_message_links",
                "suppression": "memory_person_v3_suppressions",
            }
            for artifact_type, artifact_ids in merge_artifacts.items():
                table = artifact_tables.get(artifact_type)
                if table not in table_names or not artifact_ids:
                    continue
                for start in range(0, len(artifact_ids), 800):
                    values = artifact_ids[start : start + 800]
                    placeholders = ",".join("?" for _ in values)
                    connection.execute(
                        f"DELETE FROM {table} WHERE id IN({placeholders})",
                        values,
                    )

            before = audit.get("before") or {}
            v3_before = before.get("v3_derived") or {}
            if affected and v3_before:
                placeholders = ",".join("?" for _ in affected)
                params = (chat_name, *sorted(affected))
                for table in (
                    "memory_person_fact_versions",
                    "memory_person_patterns",
                    "memory_person_period_summaries",
                    "memory_person_snapshots",
                    "memory_person_refresh_state",
                    "memory_person_v3_suppressions",
                    "memory_person_pipeline_state",
                ):
                    if table not in table_names:
                        continue
                    connection.execute(
                        f"""
                        DELETE FROM {table}
                        WHERE chat_name = ?
                          AND person_id IN({placeholders})
                        """,
                        params,
                    )
                if "memory_person_relationships" in table_names:
                    connection.execute(
                        f"""
                        DELETE FROM memory_person_relationships
                        WHERE chat_name = ?
                          AND (
                            person_id IN({placeholders})
                            OR target_person_id IN({placeholders})
                          )
                        """,
                        (
                            chat_name,
                            *sorted(affected),
                            *sorted(affected),
                        ),
                    )
                restore_order = (
                    "memory_person_fact_versions",
                    "memory_person_patterns",
                    "memory_person_relationships",
                    "memory_person_period_summaries",
                    "memory_person_snapshots",
                    "memory_person_refresh_state",
                    "memory_person_v3_suppressions",
                    "memory_person_pipeline_state",
                )
                for table in restore_order:
                    if table not in table_names:
                        continue
                    self._restore_rows(
                        connection,
                        table,
                        v3_before.get(table) or [],
                    )
            if affected:
                placeholders = ",".join("?" for _ in affected)
                params = (chat_name, *sorted(affected))
                connection.execute(
                    f"""
                    DELETE FROM memory_person_aliases
                    WHERE chat_name = ? AND person_id IN({placeholders})
                    """,
                    params,
                )
                connection.execute(
                    f"""
                    DELETE FROM memory_person_facts
                    WHERE chat_name = ? AND person_id IN({placeholders})
                    """,
                    params,
                )
                connection.execute(
                    f"""
                    DELETE FROM memory_person_identities
                    WHERE chat_name = ? AND id IN({placeholders})
                    """,
                    params,
                )
            for identity in before.get("identities") or []:
                columns = list(identity)
                connection.execute(
                    f"""
                    INSERT INTO memory_person_identities(
                        {",".join(columns)}
                    ) VALUES({",".join("?" for _ in columns)})
                    """,
                    [identity[column] for column in columns],
                )
            for alias in before.get("aliases") or []:
                columns = list(alias)
                connection.execute(
                    f"""
                    INSERT INTO memory_person_aliases(
                        {",".join(columns)}
                    ) VALUES({",".join("?" for _ in columns)})
                    """,
                    [alias[column] for column in columns],
                )
            for fact in before.get("facts") or []:
                columns = list(fact)
                connection.execute(
                    f"""
                    INSERT INTO memory_person_facts(
                        {",".join(columns)}
                    ) VALUES({",".join("?" for _ in columns)})
                    """,
                    [fact[column] for column in columns],
                )
            now = self._now()
            connection.execute(
                """
                UPDATE memory_person_audit
                SET status = 'reverted', reverted_at = ?
                WHERE id = ?
                """,
                (now, int(audit_id)),
            )
            if "memory_person_merge_artifacts" in table_names:
                connection.execute(
                    """
                    DELETE FROM memory_person_merge_artifacts
                    WHERE chat_name = ? AND audit_id = ?
                    """,
                    (chat_name, int(audit_id)),
                )
        return self.get_person_audit(chat_name, audit_id) or {}

    def list_corrections(
        self,
        chat_name: str,
        *,
        active_only: bool = False,
        include_snapshots: bool = True,
    ) -> List[Dict[str, Any]]:
        clauses = ["chat_name = ?"]
        params: List[Any] = [chat_name]
        if active_only:
            clauses.append("status = 'active'")
        projection = "*" if include_snapshots else """
            id, chat_name, target_event_id, action, reason,
            false_claims_json, corrected_claim, affected_people_json,
            replacement_event_id, status, created_at, reverted_at
        """
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {projection} FROM memory_corrections
                WHERE """
                + " AND ".join(clauses)
                + " ORDER BY id DESC",
                params,
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            json_fields = [
                ("false_claims_json", "false_claims", []),
                ("affected_people_json", "affected_people", []),
            ]
            if include_snapshots:
                json_fields.extend(
                    [
                        ("before_json", "before", {}),
                        ("after_json", "after", {}),
                    ]
                )
            for source, target, default in json_fields:
                value[target] = _json_load(value.pop(source, None), default)
            result.append(value)
        return result

    def apply_event_correction(
        self,
        chat_name: str,
        *,
        target_event_id: int,
        action: str,
        reason: str,
        false_claims: Iterable[str],
        corrected_claim: str,
        affected_people: Iterable[str],
        corrected_event: Optional[Dict[str, Any]] = None,
        existing_replacement_event_id: int = 0,
        stage_after: Optional[Dict[str, Any]] = None,
        people_after: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {
            "invalidate",
            "delete",
            "replace_existing",
            "create_revision",
            "approve_review",
        }:
            raise ValueError("unsupported correction action")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ValueError("correction reason is required")
        claims = [
            str(value or "").strip()
            for value in false_claims
            if str(value or "").strip()
        ][:20]
        people_names = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in affected_people
                if str(value or "").strip()
            )
        )[:30]
        now = self._now()
        with self._connection() as connection:
            target = connection.execute(
                """
                SELECT * FROM memory_events
                WHERE chat_name = ? AND id = ?
                """,
                (chat_name, int(target_event_id)),
            ).fetchone()
            if target is None:
                raise ValueError("target memory event does not exist")
            if int(target["is_invalidated"] or 0):
                raise ValueError("target memory event is already invalidated")
            if int(target["superseded_by_event_id"] or 0):
                raise ValueError("target memory event is already superseded")
            if (
                normalized_action == "approve_review"
                and str(target["verification_status"] or "") != "quarantined"
            ):
                raise ValueError("target memory event is not awaiting review")

            state_row = connection.execute(
                "SELECT * FROM memory_state WHERE chat_name = ?",
                (chat_name,),
            ).fetchone()
            person_rows = {
                str(row["person_name"]): dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM memory_people
                    WHERE chat_name = ?
                    """,
                    (chat_name,),
                ).fetchall()
                if str(row["person_name"]) in people_names
            }
            before: Dict[str, Any] = {
                "target": {
                    "id": int(target["id"]),
                    "superseded_by_event_id": int(
                        target["superseded_by_event_id"] or 0
                    ),
                    "is_invalidated": int(
                        target["is_invalidated"] or 0
                    ),
                    "manual_revision": int(
                        target["manual_revision"] or 0
                    ),
                    "correction_id": int(target["correction_id"] or 0),
                    "verification_status": str(
                        target["verification_status"] or "not_required"
                    ),
                    "verification_note": str(target["verification_note"] or ""),
                },
                "state": dict(state_row) if state_row is not None else None,
                "people": {
                    name: person_rows.get(name)
                    for name in people_names
                },
            }
            person_v3_before = (
                {}
                if normalized_action == "approve_review"
                else self._event_correction_v3_snapshot(
                    connection,
                    chat_name,
                    target,
                    people_names,
                )
            )
            if person_v3_before:
                before["person_v3"] = person_v3_before

            replacement = None
            if normalized_action == "replace_existing":
                replacement = connection.execute(
                    """
                    SELECT * FROM memory_events
                    WHERE chat_name = ? AND id = ? AND id != ?
                      AND superseded_by_event_id = 0
                      AND is_invalidated = 0
                      AND verification_status != 'quarantined'
                    """,
                    (
                        chat_name,
                        int(existing_replacement_event_id),
                        int(target_event_id),
                    ),
                ).fetchone()
                if replacement is None:
                    raise ValueError(
                        "replacement memory event does not exist or is inactive"
                    )
                before["replacement"] = {
                    "id": int(replacement["id"]),
                    "supersedes_event_id": int(
                        replacement["supersedes_event_id"] or 0
                    ),
                    "relation_reason": str(
                        replacement["relation_reason"] or ""
                    ),
                    "manual_revision": int(
                        replacement["manual_revision"] or 0
                    ),
                    "correction_id": int(
                        replacement["correction_id"] or 0
                    ),
                }

            correction_cursor = connection.execute(
                """
                INSERT INTO memory_corrections(
                    chat_name, target_event_id, replacement_event_id,
                    action, reason, false_claims_json, corrected_claim,
                    affected_people_json, before_json, after_json,
                    status, created_at
                ) VALUES(?, ?, 0, ?, ?, ?, ?, ?, ?, '{}', 'active', ?)
                """,
                (
                    chat_name,
                    int(target_event_id),
                    normalized_action,
                    normalized_reason,
                    _json_dump(claims),
                    str(corrected_claim or "").strip(),
                    _json_dump(people_names),
                    _json_dump(before),
                    now,
                ),
            )
            correction_id = int(correction_cursor.lastrowid)
            replacement_event_id = 0

            if normalized_action == "create_revision":
                if not isinstance(corrected_event, dict):
                    raise ValueError("corrected event is required")
                vector = corrected_event.get("embedding")
                if vector is not None:
                    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
                    embedding_blob = vector.tobytes()
                    embedding_dim = int(vector.size)
                else:
                    embedding_blob = None
                    embedding_dim = 0
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
                        verification_note, is_invalidated, manual_revision,
                        correction_id, created_at
                    ) VALUES(
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, 0, ?, 'passed', ?, 0, 1, ?, ?
                    )
                    """,
                    (
                        chat_name,
                        str(
                            corrected_event.get("source_namespace")
                            or target["source_namespace"]
                            or "live_chat_log"
                        ),
                        int(corrected_event["source_start_cursor"]),
                        int(corrected_event["source_end_cursor"]),
                        str(corrected_event.get("start_time") or ""),
                        str(corrected_event.get("end_time") or ""),
                        str(
                            corrected_event.get("title")
                            or "人工修正版事件"
                        ),
                        str(corrected_event.get("summary") or ""),
                        _json_dump(
                            corrected_event.get("participants") or []
                        ),
                        _json_dump(corrected_event.get("keywords") or []),
                        _json_dump(corrected_event.get("opinions") or []),
                        _json_dump(corrected_event.get("decisions") or []),
                        _json_dump(corrected_event.get("open_items") or []),
                        float(corrected_event.get("importance") or 0.5),
                        _json_dump(corrected_event.get("card") or {}),
                        str(corrected_event.get("search_text") or ""),
                        embedding_blob,
                        embedding_dim,
                        int(target_event_id),
                        f"人工纠错：{normalized_reason}",
                        "管理员人工核对",
                        correction_id,
                        now,
                    ),
                )
                replacement_event_id = int(cursor.lastrowid)
                self._insert_event_messages(
                    connection,
                    replacement_event_id,
                    corrected_event.get("source_messages") or [],
                    created_at=now,
                )
            elif normalized_action == "replace_existing":
                replacement_event_id = int(replacement["id"])
                connection.execute(
                    """
                    UPDATE memory_events SET
                        supersedes_event_id = ?,
                        relation_reason = ?,
                        manual_revision = 1,
                        correction_id = ?
                    WHERE chat_name = ? AND id = ?
                    """,
                    (
                        int(target_event_id),
                        f"人工纠错：{normalized_reason}",
                        correction_id,
                        chat_name,
                        replacement_event_id,
                    ),
                )

            if normalized_action == "approve_review":
                connection.execute(
                    """
                    UPDATE memory_events SET
                        verification_status = 'passed',
                        verification_note = ?,
                        manual_revision = 1,
                        correction_id = ?
                    WHERE chat_name = ? AND id = ?
                    """,
                    (
                        f"人工复核通过：{normalized_reason}",
                        correction_id,
                        chat_name,
                        int(target_event_id),
                    ),
                )
            elif replacement_event_id:
                connection.execute(
                    """
                    UPDATE memory_events SET
                        superseded_by_event_id = ?,
                        correction_id = ?
                    WHERE chat_name = ? AND id = ?
                    """,
                    (
                        replacement_event_id,
                        correction_id,
                        chat_name,
                        int(target_event_id),
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE memory_events SET
                        is_invalidated = 1,
                        manual_revision = 1,
                        correction_id = ?
                    WHERE chat_name = ? AND id = ?
                    """,
                    (correction_id, chat_name, int(target_event_id)),
                )

            if stage_after is not None:
                connection.execute(
                    """
                    UPDATE memory_state SET
                        stage_summary = ?,
                        stage_json = ?,
                        stage_updated_at = ?,
                        updated_at = ?
                    WHERE chat_name = ?
                    """,
                    (
                        str(stage_after.get("summary") or ""),
                        _json_dump(stage_after.get("structured") or {}),
                        now,
                        now,
                        chat_name,
                    ),
                )

            for person in people_after or []:
                name = str(person.get("name") or "").strip()
                if not name:
                    continue
                profile_text = str(person.get("profile") or "").strip()
                connection.execute(
                    """
                    INSERT INTO memory_people(
                        chat_name, person_name, profile_json, profile_text,
                        source_event_id, manual_override, manual_note,
                        updated_at
                    ) VALUES(?, ?, ?, ?, ?, 0, '', ?)
                    ON CONFLICT(chat_name, person_name) DO UPDATE SET
                        profile_json = excluded.profile_json,
                        profile_text = excluded.profile_text,
                        source_event_id = excluded.source_event_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_name,
                        name,
                        _json_dump(person),
                        profile_text,
                        replacement_event_id or int(target_event_id),
                        now,
                    ),
                )

            person_v3_after = (
                {}
                if normalized_action == "approve_review"
                else self._apply_event_correction_to_v3(
                    connection,
                    chat_name,
                    int(target_event_id),
                    person_v3_before,
                    correction_id=correction_id,
                    reason=normalized_reason,
                    now=now,
                )
            )

            after = {
                "target_event_id": int(target_event_id),
                "replacement_event_id": replacement_event_id,
                "stage_changed": stage_after is not None,
                "people_changed": [
                    str(person.get("name") or "")
                    for person in people_after or []
                    if str(person.get("name") or "")
                ],
                "person_v3": person_v3_after,
            }
            connection.execute(
                """
                UPDATE memory_corrections SET
                    replacement_event_id = ?, after_json = ?
                WHERE id = ?
                """,
                (
                    replacement_event_id,
                    _json_dump(after),
                    correction_id,
                ),
            )
        return self.get_correction(chat_name, correction_id) or {}

    def get_correction(
        self,
        chat_name: str,
        correction_id: int,
    ) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_corrections
                WHERE chat_name = ? AND id = ?
                """,
                (chat_name, int(correction_id)),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        for source, target, default in (
            ("false_claims_json", "false_claims", []),
            ("affected_people_json", "affected_people", []),
            ("before_json", "before", {}),
            ("after_json", "after", {}),
        ):
            value[target] = _json_load(value.pop(source, None), default)
        return value

    def update_person_manual(
        self,
        chat_name: str,
        person_name: str,
        *,
        profile_text: str,
        reason: str,
        keep_override: bool = True,
    ) -> Dict[str, Any]:
        name = str(person_name or "").strip()
        note = str(reason or "").strip()
        if not name or not note:
            raise ValueError("person name and edit reason are required")
        now = self._now()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_people
                WHERE chat_name = ? AND person_name = ?
                """,
                (chat_name, name),
            ).fetchone()
            before_person = dict(row) if row is not None else None
            connection.execute(
                """
                INSERT INTO memory_people(
                    chat_name, person_name, profile_json, profile_text,
                    source_event_id, manual_override, manual_note, updated_at
                ) VALUES(?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(chat_name, person_name) DO UPDATE SET
                    profile_json = excluded.profile_json,
                    profile_text = excluded.profile_text,
                    manual_override = excluded.manual_override,
                    manual_note = excluded.manual_note,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_name,
                    name,
                    _json_dump(
                        {
                            "name": name,
                            "profile": str(profile_text or "").strip(),
                            "manually_edited": True,
                        }
                    ),
                    str(profile_text or "").strip(),
                    int(bool(keep_override)),
                    note,
                    now,
                ),
            )
            after_person = dict(
                connection.execute(
                    """
                    SELECT * FROM memory_people
                    WHERE chat_name = ? AND person_name = ?
                    """,
                    (chat_name, name),
                ).fetchone()
            )
            correction_cursor = connection.execute(
                """
                INSERT INTO memory_corrections(
                    chat_name, target_event_id, replacement_event_id,
                    action, reason, false_claims_json, corrected_claim,
                    affected_people_json, before_json, after_json,
                    status, created_at
                ) VALUES(
                    ?, 0, 0, 'person_edit', ?, '[]', '', ?, ?, ?,
                    'active', ?
                )
                """,
                (
                    chat_name,
                    note,
                    _json_dump([name]),
                    _json_dump({"people": {name: before_person}}),
                    _json_dump({"people": {name: after_person}}),
                    now,
                ),
            )
            correction_id = int(correction_cursor.lastrowid)
        return self.get_correction(chat_name, correction_id) or {}

    def revert_correction(
        self,
        chat_name: str,
        correction_id: int,
    ) -> Dict[str, Any]:
        now = self._now()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_corrections
                WHERE chat_name = ? AND id = ? AND status = 'active'
                """,
                (chat_name, int(correction_id)),
            ).fetchone()
            if row is None:
                raise ValueError("active correction does not exist")
            current_people = set(
                _json_load(row["affected_people_json"], [])
            )
            current_event_ids = {
                int(row["target_event_id"] or 0),
                int(row["replacement_event_id"] or 0),
            }
            current_event_ids.discard(0)
            later_rows = connection.execute(
                """
                SELECT id, target_event_id, replacement_event_id,
                       affected_people_json
                FROM memory_corrections
                WHERE chat_name = ? AND status = 'active' AND id > ?
                ORDER BY id DESC
                """,
                (chat_name, int(correction_id)),
            ).fetchall()
            for later in later_rows:
                later_people = set(
                    _json_load(later["affected_people_json"], [])
                )
                later_event_ids = {
                    int(later["target_event_id"] or 0),
                    int(later["replacement_event_id"] or 0),
                }
                later_event_ids.discard(0)
                if (
                    current_people & later_people
                    or current_event_ids & later_event_ids
                ):
                    raise ValueError(
                        "存在影响同一事件或人物的更晚纠错，"
                        f"请先撤销纠错 #{int(later['id'])}"
                    )
            before = _json_load(row["before_json"], {})
            after = _json_load(row["after_json"], {})
            target = before.get("target") or {}
            if target:
                connection.execute(
                    """
                    UPDATE memory_events SET
                        superseded_by_event_id = ?,
                        is_invalidated = ?,
                        manual_revision = ?,
                        correction_id = ?,
                        verification_status = ?,
                        verification_note = ?
                    WHERE chat_name = ? AND id = ?
                    """,
                    (
                        int(target.get("superseded_by_event_id") or 0),
                        int(target.get("is_invalidated") or 0),
                        int(target.get("manual_revision") or 0),
                        int(target.get("correction_id") or 0),
                        str(target.get("verification_status") or "not_required"),
                        str(target.get("verification_note") or ""),
                        chat_name,
                        int(target.get("id") or 0),
                    ),
                )
            replacement_event_id = int(
                row["replacement_event_id"] or 0
            )
            replacement_before = before.get("replacement")
            if replacement_event_id and replacement_before:
                connection.execute(
                    """
                    UPDATE memory_events SET
                        supersedes_event_id = ?,
                        relation_reason = ?,
                        manual_revision = ?,
                        correction_id = ?
                    WHERE chat_name = ? AND id = ?
                    """,
                    (
                        int(
                            replacement_before.get(
                                "supersedes_event_id"
                            )
                            or 0
                        ),
                        str(
                            replacement_before.get("relation_reason")
                            or ""
                        ),
                        int(
                            replacement_before.get("manual_revision")
                            or 0
                        ),
                        int(
                            replacement_before.get("correction_id")
                            or 0
                        ),
                        chat_name,
                        replacement_event_id,
                    ),
                )
            elif replacement_event_id:
                connection.execute(
                    """
                    DELETE FROM memory_event_messages
                    WHERE event_id = ?
                    """,
                    (replacement_event_id,),
                )
                connection.execute(
                    """
                    DELETE FROM memory_events
                    WHERE chat_name = ? AND id = ? AND correction_id = ?
                    """,
                    (chat_name, replacement_event_id, int(correction_id)),
                )

            state = before.get("state")
            if state:
                connection.execute(
                    """
                    INSERT INTO memory_state(
                        chat_name, source_cursor, source_message_count,
                        stage_source_event_id, stage_summary, stage_json,
                        stage_updated_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_name) DO UPDATE SET
                        source_cursor = excluded.source_cursor,
                        source_message_count = excluded.source_message_count,
                        stage_source_event_id = excluded.stage_source_event_id,
                        stage_summary = excluded.stage_summary,
                        stage_json = excluded.stage_json,
                        stage_updated_at = excluded.stage_updated_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_name,
                        int(state.get("source_cursor") or 0),
                        int(state.get("source_message_count") or 0),
                        int(state.get("stage_source_event_id") or 0),
                        str(state.get("stage_summary") or ""),
                        str(state.get("stage_json") or "{}"),
                        state.get("stage_updated_at"),
                        str(state.get("updated_at") or now),
                    ),
                )

            for name, person in (before.get("people") or {}).items():
                if person is None:
                    connection.execute(
                        """
                        DELETE FROM memory_people
                        WHERE chat_name = ? AND person_name = ?
                        """,
                        (chat_name, name),
                    )
                    continue
                connection.execute(
                    """
                    INSERT INTO memory_people(
                        chat_name, person_name, profile_json, profile_text,
                        source_event_id, manual_override, manual_note,
                        updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_name, person_name) DO UPDATE SET
                        profile_json = excluded.profile_json,
                        profile_text = excluded.profile_text,
                        source_event_id = excluded.source_event_id,
                        manual_override = excluded.manual_override,
                        manual_note = excluded.manual_note,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_name,
                        name,
                        str(person.get("profile_json") or "{}"),
                        str(person.get("profile_text") or ""),
                        int(person.get("source_event_id") or 0),
                        int(person.get("manual_override") or 0),
                        str(person.get("manual_note") or ""),
                        str(person.get("updated_at") or now),
                    ),
                )
            if before.get("person_v3"):
                self._restore_event_correction_v3(
                    connection,
                    chat_name,
                    before["person_v3"],
                    after,
                    reverted_at=now,
                )
            connection.execute(
                """
                UPDATE memory_corrections
                SET status = 'reverted', reverted_at = ?
                WHERE id = ?
                """,
                (now, int(correction_id)),
            )
        return self.get_correction(chat_name, int(correction_id)) or {}

    def clear_chat(
        self,
        chat_name: str,
        *,
        scope: str,
        reset_cursor: Optional[int] = None,
        source_message_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        normalized = str(scope or "all").strip().lower()
        if normalized not in {"all", "stage", "events", "people"}:
            raise ValueError(f"Unsupported memory scope: {scope}")

        now = self._now()
        with self._connection() as connection:
            if normalized in {"all", "events"}:
                connection.execute(
                    """
                    UPDATE memory_corrections
                    SET status = 'cleared', reverted_at = ?
                    WHERE chat_name = ? AND status = 'active'
                    """,
                    (now, chat_name),
                )
            if normalized in {"all", "events", "stage"}:
                connection.execute(
                    """
                    UPDATE memory_stage_audit
                    SET status = 'cleared', reverted_at = ?
                    WHERE chat_name = ? AND status = 'active'
                    """,
                    (now, chat_name),
                )
            if normalized in {"all", "people"}:
                connection.execute(
                    """
                    UPDATE memory_person_audit
                    SET status = 'cleared', reverted_at = ?
                    WHERE chat_name = ? AND status = 'active'
                    """,
                    (now, chat_name),
                )
                connection.execute(
                    """
                    UPDATE memory_person_v3_audit
                    SET status = 'cleared', reverted_at = ?
                    WHERE chat_name = ? AND status = 'active'
                    """,
                    (now, chat_name),
                )
            if normalized in {"all", "events"}:
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
                if normalized == "all":
                    for table in PERSON_V3_CHAT_TABLES:
                        exists = connection.execute(
                            """
                            SELECT 1 FROM sqlite_master
                            WHERE type = 'table' AND name = ?
                            """,
                            (table,),
                        ).fetchone()
                        if exists is not None:
                            connection.execute(
                                f"DELETE FROM {table} WHERE chat_name = ?",
                                (chat_name,),
                            )
                    connection.execute(
                        """
                        DELETE FROM memory_person_merge_artifacts
                        WHERE chat_name = ?
                        """,
                        (chat_name,),
                    )
                    connection.execute(
                        "DELETE FROM memory_people WHERE chat_name = ?",
                        (chat_name,),
                    )
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
                    """
                    UPDATE memory_state SET
                        stage_source_event_id = 0,
                        stage_summary = '',
                        stage_json = '{}',
                        stage_mode = 'auto',
                        stage_manual_note = '',
                        stage_manual_updated_at = NULL,
                        stage_updated_at = NULL,
                        updated_at = ?
                    WHERE chat_name = ?
                    """,
                    (self._now(), chat_name),
                )
            elif normalized == "people":
                for table in PERSON_V3_CHAT_TABLES:
                    exists = connection.execute(
                        """
                        SELECT 1 FROM sqlite_master
                        WHERE type = 'table' AND name = ?
                        """,
                        (table,),
                    ).fetchone()
                    if exists is not None:
                        connection.execute(
                            f"DELETE FROM {table} WHERE chat_name = ?",
                            (chat_name,),
                        )
                connection.execute(
                    """
                    DELETE FROM memory_person_merge_artifacts
                    WHERE chat_name = ?
                    """,
                    (chat_name,),
                )
                connection.execute(
                    "DELETE FROM memory_people WHERE chat_name = ?",
                    (chat_name,),
                )
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
            elif normalized == "stage":
                connection.execute(
                    """
                    UPDATE memory_state SET
                        stage_source_event_id = 0,
                        stage_summary = '',
                        stage_json = '{}',
                        stage_mode = 'auto',
                        stage_manual_note = '',
                        stage_manual_updated_at = NULL,
                        stage_updated_at = NULL,
                        updated_at = ?
                    WHERE chat_name = ?
                    """,
                    (self._now(), chat_name),
                )

            if normalized == "all":
                connection.execute(
                    """
                    INSERT INTO memory_state(
                        chat_name, source_cursor, source_message_count, updated_at
                    ) VALUES(?, ?, ?, ?)
                    ON CONFLICT(chat_name) DO UPDATE SET
                        source_cursor = excluded.source_cursor,
                        source_message_count = excluded.source_message_count,
                        stage_source_event_id = 0,
                        stage_summary = '',
                        stage_json = '{}',
                        stage_mode = 'auto',
                        stage_manual_note = '',
                        stage_manual_updated_at = NULL,
                        stage_updated_at = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_name,
                        max(0, int(reset_cursor or 0)),
                        max(0, int(source_message_count or 0)),
                        self._now(),
                    ),
                )
        return self.get_state(chat_name)
