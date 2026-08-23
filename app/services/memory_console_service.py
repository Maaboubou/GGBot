"""Task-oriented administration service for conversational memory."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.user_permission import UserPermission, WeChatUser
from app.plugins.builtin_chatbot.memory_store import MemoryStore
from app.plugins.builtin_chatbot.memory_config import upgrade_memory_config_keys
from app.plugins.builtin_chatbot.person_memory import PersonMemoryStore
from app.utils.plugin_config import get_plugin_config


class MemoryConsoleError(ValueError):
    pass


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class MemoryConsoleService:
    def __init__(self, db: Session, *, store: Optional[MemoryStore] = None):
        self.db = db
        self.store = store or MemoryStore()
        self.person = PersonMemoryStore(self.store)

    def _user(self, user_id: int) -> WeChatUser:
        user = self.db.query(WeChatUser).filter(WeChatUser.id == int(user_id)).first()
        if user is None:
            raise MemoryConsoleError("聊天不存在")
        return user

    def _permission(self, user_id: int) -> Optional[UserPermission]:
        return (
            self.db.query(UserPermission)
            .filter(
                UserPermission.user_id == int(user_id),
                UserPermission.plugin_name == "builtin_chatbot",
            )
            .first()
        )

    @staticmethod
    def _invalidate_live_memory(chat_name: str) -> None:
        """Make explicit administration changes visible on the next reply."""
        try:
            from app.plugins.builtin_chatbot.main import get_chatbot_plugin

            plugin = get_chatbot_plugin()
            if plugin and hasattr(plugin, "invalidate_memory_context"):
                plugin.invalidate_memory_context(chat_name)
        except Exception:
            # The database remains authoritative. A stopped plugin will load
            # the new value when it starts.
            pass

    @staticmethod
    def global_memory_config() -> Dict[str, Any]:
        plugin_config = get_plugin_config("builtin_chatbot")
        schema = plugin_config.get("config_schema") or {}
        current = plugin_config.get("config") or {}
        values: Dict[str, Any] = {}
        for key, definition in schema.items():
            if not str(key).startswith("memory_"):
                continue
            values[key] = current.get(key, definition.get("default"))
        return values

    @staticmethod
    def validate_memory_overrides(overrides: Dict[str, Any]) -> Dict[str, Any]:
        overrides = upgrade_memory_config_keys(overrides)
        plugin_config = get_plugin_config("builtin_chatbot")
        schema = plugin_config.get("config_schema") or {}
        result: Dict[str, Any] = {}
        for key, value in overrides.items():
            definition = schema.get(key)
            if not str(key).startswith("memory_") or not isinstance(definition, dict):
                continue
            value_type = definition.get("type")
            if value_type == "boolean":
                if not isinstance(value, bool):
                    raise MemoryConsoleError(f"{key} 必须是布尔值")
            elif value_type == "integer":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise MemoryConsoleError(f"{key} 必须是整数")
                minimum = definition.get("minimum")
                maximum = definition.get("maximum")
                if minimum is not None and value < int(minimum):
                    raise MemoryConsoleError(f"{key} 不能小于 {minimum}")
                if maximum is not None and value > int(maximum):
                    raise MemoryConsoleError(f"{key} 不能大于 {maximum}")
            elif value_type == "number":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise MemoryConsoleError(f"{key} 必须是数字")
                minimum = definition.get("minimum")
                maximum = definition.get("maximum")
                if minimum is not None and float(value) < float(minimum):
                    raise MemoryConsoleError(f"{key} 不能小于 {minimum}")
                if maximum is not None and float(value) > float(maximum):
                    raise MemoryConsoleError(f"{key} 不能大于 {maximum}")
            elif value_type == "string":
                if not isinstance(value, str):
                    raise MemoryConsoleError(f"{key} 必须是文本")
            else:
                continue
            result[key] = value
        return result

    def effective_memory_config(self, user_id: int) -> Dict[str, Any]:
        global_config = self.global_memory_config()
        permission = self._permission(user_id)
        profile = _json_object(permission.memory_profile if permission else None)
        overrides = profile.get("overrides")
        if not isinstance(overrides, dict):
            overrides = {
                key: value
                for key, value in profile.items()
                if str(key).startswith("memory_")
            }
        overrides = upgrade_memory_config_keys(overrides)
        overrides = {
            key: value
            for key, value in overrides.items()
            if key in global_config
        }
        if not bool(profile.get("enabled")):
            mode = "inherit"
            effective = dict(global_config)
        elif overrides.get("memory_enabled") is False:
            mode = "off"
            effective = {**global_config, **overrides, "memory_enabled": False}
        else:
            mode = "custom"
            effective = {**global_config, **overrides}
        return {
            "mode": mode,
            "source": "global" if mode == "inherit" else "chat",
            "global": global_config,
            "overrides": overrides,
            "effective": effective,
        }

    def overview(self, user_id: int) -> Dict[str, Any]:
        user = self._user(user_id)
        from app.plugins.builtin_chatbot.chat_log import ChatLogManager
        from app.plugins.builtin_chatbot.context_manager import ChatContextManager
        from app.plugins.builtin_chatbot.memory_service import ChatMemoryService

        memory_service = ChatMemoryService(ChatLogManager(), ChatContextManager())
        try:
            document = memory_service.get_memory_document(user.chat_name)
        finally:
            memory_service.close()
        state = self.store.get_state(user.chat_name)
        try:
            message_count = int(ChatLogManager().count_messages(user.chat_name) or 0)
        except Exception:
            message_count = int(state.get("source_message_count") or 0)
        embedding = {
            "ready": False,
            "fallback": True,
            "last_error": "",
        }
        try:
            from app.plugins.builtin_chatbot.main import get_chatbot_plugin

            plugin = get_chatbot_plugin()
            service = getattr(plugin, "memory_service", None) if plugin else None
            embedder = getattr(service, "embedding_service", None)
            if embedder is not None:
                embedding = {
                    "ready": bool(embedder.ready),
                    "fallback": not bool(embedder.ready),
                    "last_error": str(embedder.last_error or ""),
                }
        except Exception:
            pass
        return {
            **document,
            "user": {
                "id": user.id,
                "chat_name": user.chat_name,
                "remark": user.remark or "",
                "is_group": bool(user.is_group),
            },
            "configuration": self.effective_memory_config(user_id),
            "health": {
                "pending_messages": max(
                    0,
                    message_count - int(state.get("source_message_count") or 0),
                ),
                "embedding": embedding,
                "integrity": self.store.integrity_report(user.chat_name),
            },
            "storage": self.store.chat_storage_stats(user.chat_name),
        }

    def events(
        self,
        user_id: int,
        *,
        query: str = "",
        date_from: str = "",
        date_to: str = "",
        status: str = "all",
        offset: int = 0,
        limit: int = 20,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        items, total = self.store.browse_events(
            user.chat_name,
            query=query,
            date_from=date_from,
            date_to=date_to,
            status=status,
            offset=offset,
            limit=limit,
        )
        for item in items:
            item.pop("embedding", None)
            item.pop("search_text", None)
            item["has_embedding"] = int(item.get("embedding_dim") or 0) > 0
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def event_detail(self, user_id: int, event_id: int) -> Dict[str, Any]:
        user = self._user(user_id)
        event = self.store.get_event(user.chat_name, int(event_id))
        if event is None:
            raise MemoryConsoleError("记忆事件不存在")
        event.pop("embedding", None)
        event.pop("search_text", None)
        event["has_embedding"] = int(event.get("embedding_dim") or 0) > 0
        from app.plugins.builtin_chatbot.memory_source import read_event_source

        return {
            "event": event,
            "messages": read_event_source(self.store, event, limit=200),
        }

    def people(
        self,
        user_id: int,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 20,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        state = self.person.get_chat_state(user.chat_name)
        items, total = self.person.browse_profiles(
            user.chat_name,
            query=query,
            offset=offset,
            limit=limit,
        )
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "state": state,
        }

    def person_detail(self, user_id: int, person_id: int) -> Dict[str, Any]:
        user = self._user(user_id)
        profile = self.person.get_profile(user.chat_name, int(person_id))
        if profile is None:
            raise MemoryConsoleError("人物资料不存在")
        return {"profile": profile}

    def update_stage(
        self,
        user_id: int,
        *,
        summary: str,
        mode: str,
        reason: str,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        from app.plugins.builtin_chatbot.chat_log import ChatLogManager
        from app.plugins.builtin_chatbot.context_manager import ChatContextManager
        from app.plugins.builtin_chatbot.memory_service import ChatMemoryService

        service = ChatMemoryService(ChatLogManager(), ChatContextManager())
        try:
            result = service.update_stage_manual(
                user.chat_name,
                summary,
                mode=mode,
                reason=reason,
            )
        finally:
            service.close()
        self._invalidate_live_memory(user.chat_name)
        return result

    def correct_event(
        self,
        user_id: int,
        event_id: int,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        from app.plugins.builtin_chatbot.chat_log import ChatLogManager
        from app.plugins.builtin_chatbot.context_manager import ChatContextManager
        from app.plugins.builtin_chatbot.memory_service import ChatMemoryService

        service = ChatMemoryService(ChatLogManager(), ChatContextManager())
        try:
            result = service.correct_event_manual(
                user.chat_name,
                event_id=int(event_id),
                action=str(payload.get("action") or "invalidate"),
                reason=str(payload.get("reason") or ""),
                false_claims=list(payload.get("false_claims") or []),
                corrected_claim=str(payload.get("corrected_claim") or ""),
                affected_people=list(payload.get("affected_people") or []),
                existing_replacement_event_id=int(
                    payload.get("existing_replacement_event_id") or 0
                ),
                corrected_event_fields=payload.get("corrected_event"),
            )
        finally:
            service.close()
        self._invalidate_live_memory(user.chat_name)
        return result

    def delete_event(self, user_id: int, event_id: int, *, reason: str) -> Dict[str, Any]:
        user = self._user(user_id)
        from app.plugins.builtin_chatbot.chat_log import ChatLogManager
        from app.plugins.builtin_chatbot.context_manager import ChatContextManager
        from app.plugins.builtin_chatbot.memory_service import ChatMemoryService

        service = ChatMemoryService(ChatLogManager(), ChatContextManager())
        try:
            result = service.delete_event_manual(
                user.chat_name,
                event_id=int(event_id),
                reason=reason,
            )
        finally:
            service.close()
        self._invalidate_live_memory(user.chat_name)
        return result

    def review_event(
        self,
        user_id: int,
        event_id: int,
        *,
        decision: str,
        reason: str,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        if decision == "reject":
            return self.delete_event(user_id, event_id, reason=reason)
        if decision != "approve":
            raise MemoryConsoleError("不支持的事件复核结果")
        result = self.store.apply_event_correction(
            user.chat_name,
            target_event_id=int(event_id),
            action="approve_review",
            reason=reason,
            false_claims=[],
            corrected_claim="",
            affected_people=[],
        )
        self._invalidate_live_memory(user.chat_name)
        return result

    def review_observation(
        self,
        user_id: int,
        person_id: int,
        observation_id: int,
        *,
        quality_status: str,
        reason: str,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        observation = self.person.get_observation(
            user.chat_name,
            int(observation_id),
        )
        if observation is None or int(observation.get("person_id") or 0) != int(person_id):
            raise MemoryConsoleError("观察证据不属于该人物")
        result = self.person.review_observation(
            user.chat_name,
            int(observation_id),
            quality_status=quality_status,
            reason=reason,
        )
        self._invalidate_live_memory(user.chat_name)
        return result

    def add_person_fact(
        self,
        user_id: int,
        person_id: int,
        fact: Dict[str, Any],
        *,
        reason: str,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        result = self.person.add_manual_fact(
            user.chat_name,
            int(person_id),
            fact,
            reason=reason,
        )
        self._invalidate_live_memory(user.chat_name)
        return result

    def delete_person_fact(
        self,
        user_id: int,
        person_id: int,
        fact_id: int,
        *,
        reason: str,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        result = self.person.delete_fact(
            user.chat_name,
            int(person_id),
            int(fact_id),
            reason=reason,
        )
        self._invalidate_live_memory(user.chat_name)
        return result

    def add_person_alias(
        self,
        user_id: int,
        person_id: int,
        *,
        alias_name: str,
        reason: str,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        result = self.store.add_person_alias(
            user.chat_name,
            int(person_id),
            alias_name=alias_name,
            reason=reason,
        )
        self._invalidate_live_memory(user.chat_name)
        return result

    def merge_person(
        self,
        user_id: int,
        person_id: int,
        *,
        target_person_name: str,
        reason: str,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        target = self.store.resolve_person(user.chat_name, target_person_name)
        if target is None:
            raise MemoryConsoleError("没有找到唯一的目标人物，请填写其准确姓名或确认过的别名")
        target_id = int(target.get("id") or 0)
        if target_id == int(person_id):
            raise MemoryConsoleError("源人物和目标人物不能相同")
        result = self.store.merge_people(
            user.chat_name,
            int(person_id),
            target_id,
            reason=reason,
        )
        self._invalidate_live_memory(user.chat_name)
        return result

    def revert_change(
        self,
        user_id: int,
        *,
        category: str,
        change_id: int,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        if category == "stage":
            result = self.store.revert_stage_audit(user.chat_name, int(change_id))
            self._invalidate_live_memory(user.chat_name)
            return result
        if category == "person_identity":
            result = self.store.revert_person_audit(user.chat_name, int(change_id))
            self._invalidate_live_memory(user.chat_name)
            return result
        if category == "event":
            from app.plugins.builtin_chatbot.chat_log import ChatLogManager
            from app.plugins.builtin_chatbot.context_manager import ChatContextManager
            from app.plugins.builtin_chatbot.memory_service import ChatMemoryService

            service = ChatMemoryService(ChatLogManager(), ChatContextManager())
            try:
                result = service.revert_manual_correction(
                    user.chat_name,
                    int(change_id),
                )
            finally:
                service.close()
            self._invalidate_live_memory(user.chat_name)
            return result
        raise MemoryConsoleError("该类型的变更不能自动撤销")

    def reviews(
        self,
        user_id: int,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(100, int(limit)))
        events, event_total = self.store.browse_events(
            user.chat_name,
            status="quarantined",
            offset=safe_offset,
            limit=safe_limit,
        )
        for event in events:
            event.pop("embedding", None)
            event.pop("search_text", None)
        with self.store._connection() as connection:
            total_row = connection.execute(
                """
                SELECT COUNT(*) AS value FROM memory_person_observations
                WHERE chat_name = ? AND quality_status = 'quarantined'
                """,
                (user.chat_name,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT observation.*, identity.canonical_name AS person_name
                FROM memory_person_observations AS observation
                LEFT JOIN memory_person_identities AS identity
                  ON identity.id = observation.person_id
                WHERE observation.chat_name = ?
                  AND observation.quality_status = 'quarantined'
                ORDER BY observation.id DESC LIMIT ? OFFSET ?
                """,
                (user.chat_name, safe_limit, safe_offset),
            ).fetchall()
        observations = []
        for row in rows:
            value = dict(row)
            for key in (
                "evidence_cursors",
                "subject_evidence_cursors",
                "evidence_source_ids",
                "evidence_senders",
                "evidence_excerpt",
            ):
                value[key] = _json_value(value.pop(f"{key}_json", "[]"), [])
            observations.append(value)
        return {
            "events": {"items": events, "total": event_total},
            "observations": {
                "items": observations,
                "total": int(total_row["value"] or 0),
            },
            "offset": safe_offset,
            "limit": safe_limit,
        }

    def changes(
        self,
        user_id: int,
        *,
        offset: int = 0,
        limit: int = 30,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(100, int(limit)))
        union = """
            SELECT 'event' AS category, id, action, reason, status,
                   target_event_id AS target_id, created_at, reverted_at
            FROM memory_corrections WHERE chat_name = ?
            UNION ALL
            SELECT 'stage', id, action, reason, status, 0, created_at, reverted_at
            FROM memory_stage_audit WHERE chat_name = ?
            UNION ALL
            SELECT 'person_identity', id, action, reason, status, 0,
                   created_at, reverted_at
            FROM memory_person_audit WHERE chat_name = ?
            UNION ALL
            SELECT 'person_memory', id, action, reason, status, target_id,
                   created_at, reverted_at
            FROM memory_person_projection_audit WHERE chat_name = ?
            UNION ALL
            SELECT 'maintenance', id, action, reason, 'completed', 0,
                   created_at, NULL
            FROM memory_maintenance_audit WHERE chat_name = ?
        """
        params = (user.chat_name,) * 5
        with self.store._connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM ({union})",
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM ({union})
                ORDER BY created_at DESC, category, id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, safe_limit, safe_offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "offset": safe_offset,
            "limit": safe_limit,
        }

    def maintenance(self, user_id: int, *, retention_days: int = 90) -> Dict[str, Any]:
        user = self._user(user_id)
        return {
            "storage": self.store.chat_storage_stats(user.chat_name),
            "integrity": self.store.integrity_report(user.chat_name),
            "candidate_cleanup": self.store.prune_transient_candidates(
                user.chat_name,
                rejected_older_than_days=retention_days,
                dry_run=True,
            ),
            "clear_preview": self.clear_preview(user_id),
            "backup": {
                "directory": str(self.store.path.parent / "memory_backups"),
                "scope": "entire_memory_database",
            },
        }

    def backup_database(self, user_id: int, *, confirmation: str) -> Dict[str, Any]:
        user = self._user(user_id)
        if str(confirmation or "") != user.chat_name:
            raise MemoryConsoleError("确认文字必须与聊天名称完全一致")
        timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        chat_hash = hashlib.sha256(user.chat_name.encode("utf-8")).hexdigest()[:10]
        destination = self.store.path.parent / "memory_backups" / (
            f"chat-memory-{timestamp}-{chat_hash}.db"
        )
        result = self.store.backup_database(destination)
        audit_id = self.store.record_maintenance_action(
            user.chat_name,
            action="backup_database",
            reason="管理员在记忆维护区创建清理前备份",
            result={
                "filename": result["filename"],
                "bytes": result["bytes"],
                "quick_check": result["quick_check"],
            },
        )
        result["maintenance_audit_id"] = audit_id
        return result

    def cleanup_candidates(
        self,
        user_id: int,
        *,
        retention_days: int,
        confirmation: str,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        if str(confirmation or "") != user.chat_name:
            raise MemoryConsoleError("确认文字必须与聊天名称完全一致")
        result = self.store.prune_transient_candidates(
            user.chat_name,
            rejected_older_than_days=retention_days,
            dry_run=False,
            reason="管理员从记忆维护区清理过期的已拒绝候选",
        )
        self._invalidate_live_memory(user.chat_name)
        return result

    def clear_preview(self, user_id: int) -> Dict[str, Any]:
        user = self._user(user_id)
        stats = self.store.chat_storage_stats(user.chat_name)
        categories = stats.get("categories") or {}
        preview = {
            "stage": {"stage": 1 if self.store.get_state(user.chat_name).get("stage_summary") else 0},
            "events": {
                "events": int((categories.get("events") or {}).get("count") or 0),
                "event_messages": int(
                    (categories.get("event_messages") or {}).get("count") or 0
                ),
                "stage": 1 if self.store.get_state(user.chat_name).get("stage_summary") else 0,
            },
            "people": {
                key: int((categories.get(key) or {}).get("count") or 0)
                for key in (
                    "people",
                    "source_messages",
                    "message_links",
                    "candidates",
                    "observations",
                    "person_snapshots",
                )
            },
            "confirmation": user.chat_name,
        }
        preview["all"] = {
            **preview["events"],
            **preview["people"],
        }
        return preview

    def clear_memory(
        self,
        user_id: int,
        *,
        scope: str,
        confirmation: str,
    ) -> Dict[str, Any]:
        user = self._user(user_id)
        if str(confirmation or "") != user.chat_name:
            raise MemoryConsoleError("确认文字必须与聊天名称完全一致")
        if scope not in {"stage", "events", "people", "all"}:
            raise MemoryConsoleError("不支持的清理范围")
        preview = self.clear_preview(user_id).get(scope) or {}
        from app.plugins.builtin_chatbot.chat_log import ChatLogManager
        from app.plugins.builtin_chatbot.context_manager import ChatContextManager
        from app.plugins.builtin_chatbot.memory_service import ChatMemoryService

        service = ChatMemoryService(ChatLogManager(), ChatContextManager())
        try:
            result = service.clear_memory(user.chat_name, scope)
        finally:
            service.close()
        audit_id = self.store.record_maintenance_action(
            user.chat_name,
            action=f"clear_{scope}",
            reason="管理员从记忆维护区执行清理",
            result={"scope": scope, "deleted": preview},
        )
        result["maintenance_audit_id"] = audit_id
        self._invalidate_live_memory(user.chat_name)
        return result
