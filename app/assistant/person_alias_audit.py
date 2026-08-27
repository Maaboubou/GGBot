"""Evidence-backed alias audit for Assistant person memory.

The historical person rebuild primarily learns facts about message authors.
Group nicknames such as "熊猫" often appear only in other members' messages,
so they need a separate cross-conversation identity pass.

This tool is deliberately two-phase:

1. build a resumable, isolated audit report with DeepSeek;
2. explicitly activate only high-confidence, conflict-free aliases after a
   full production database backup.

Ambiguous candidates remain in the report and are never silently promoted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import litellm

from app.assistant.context_manager import ChatContextManager
from app.assistant.memory_service import ChatMemoryService
from app.assistant.memory_store import MemoryStore
from app.assistant.person_memory import (
    PersonMemoryEngine,
    _clean_text,
    _json_load,
)
from app.assistant.person_memory_rebuild import (
    DeepSeekBudget,
    _NoChatLog,
    _backup_database,
    _configure_deepseek_manager,
)


GENERIC_ALIAS_BLOCKLIST = {
    "你",
    "他",
    "她",
    "它",
    "哥",
    "姐",
    "总",
    "老板",
    "大佬",
    "老师",
    "兄弟",
    "群友",
    "朋友",
    "书记",
    "局长",
    "主任",
}
ALIAS_CUES = (
    "外号",
    "绰号",
    "又叫",
    "也叫",
    "人称",
    "就是",
    "原名",
    "真名",
    "改名",
    "昵称",
    "叫他",
    "叫她",
    "哥",
    "姐",
    "总",
    "桑",
    "giegie",
)
BOT_OR_DERIVED_SENDERS = {
    "微信助手",
    "AI助手",
    "OCR",
    "链接摘要",
    "总结Bot",
}


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


def _load_identity_merge_plan(path: Optional[Path]) -> List[Dict[str, Any]]:
    if path is None:
        return []
    if not path.is_file():
        raise FileNotFoundError(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value.get("merges") if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise ValueError("identity merge plan must contain a merges array")
    result = []
    seen_sources = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("identity merge item must be an object")
        source_id = int(raw.get("source_person_id") or 0)
        target_id = int(raw.get("target_person_id") or 0)
        if source_id <= 0 or target_id <= 0 or source_id == target_id:
            raise ValueError("identity merge ids must be different positives")
        if source_id in seen_sources:
            raise ValueError(f"duplicate identity merge source: {source_id}")
        seen_sources.add(source_id)
        result.append(
            {
                **raw,
                "source_person_id": source_id,
                "target_person_id": target_id,
                "reason": _clean_text(raw.get("reason"), 1000),
            }
        )
    return result


def _load_alias_policy(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {"overrides": [], "additions": []}
    if not path.is_file():
        raise FileNotFoundError(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("alias policy must be a JSON object")
    result = {}
    for key in ("overrides", "additions"):
        rows = value.get(key) or []
        if not isinstance(rows, list) or not all(
            isinstance(item, dict) for item in rows
        ):
            raise ValueError(f"alias policy {key} must be an object array")
        result[key] = rows
    return result


def _safe_filename(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z\u3400-\u9fff_.-]+", "_", value)
    return normalized.strip("._")[:80] or "person"


def _normalize_alias(value: Any) -> str:
    alias = re.sub(r"\s+", " ", str(value or "")).strip()
    alias = alias.strip("“”\"'`，,。.!！?？:：;；()（）[]【】")
    return alias[:60]


def _bounded_confidence(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(0.0, min(1.0, parsed))


def _valid_alias(alias: str, canonical_name: str) -> bool:
    if not alias or alias.casefold() == canonical_name.casefold():
        return False
    if alias in GENERIC_ALIAS_BLOCKLIST:
        return False
    if len(alias) < 2 or len(alias) > 40:
        return False
    if re.fullmatch(r"[\W_]+", alias, flags=re.UNICODE):
        return False
    if "\n" in alias or "\r" in alias:
        return False
    return True


def _response_items(
    response: Dict[str, Any],
    *keys: str,
) -> List[Any]:
    """Accept common model wrappers while keeping the persisted response."""
    containers: List[Dict[str, Any]] = [response]
    for wrapper in ("result", "output", "data"):
        nested = response.get(wrapper)
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        for key in keys:
            value = container.get(key)
            if isinstance(value, list):
                return value
    return []


def _response_value(
    response: Dict[str, Any],
    *keys: str,
) -> Any:
    containers: List[Dict[str, Any]] = [response]
    for wrapper in ("result", "output", "data"):
        nested = response.get(wrapper)
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        for key in keys:
            if key in container:
                return container[key]
    return None


def _message_reference(message: Dict[str, Any]) -> str:
    return (
        f"M:{message.get('source_namespace')}:"
        f"{int(message.get('source_cursor') or 0)}"
    )


def _event_reference(event: Dict[str, Any]) -> str:
    return f"E:{int(event.get('id') or 0)}"


def _mentions_person(content: Any, canonical_name: str) -> bool:
    """Match a complete WeChat @ mention, not a short-name prefix.

    Without the boundary, canonical names such as ``A`` and ``Y`` matched
    ``@AAA...`` and ``@yuga-khan`` respectively, creating convincing but
    false cross-person aliases.
    """
    name = str(canonical_name or "").strip()
    if not name:
        return False
    suffix_boundary = r"(?=$|[\s\u2005,，。.!！?？:：;；()（）\[\]【】])"
    pattern = rf"@{re.escape(name)}(?:#\d+)?{suffix_boundary}"
    return re.search(pattern, str(content or ""), flags=re.IGNORECASE) is not None


class AliasAuditCorpus:
    def __init__(self, database: Path, chat_name: str) -> None:
        self.database = database
        self.chat_name = chat_name
        connection = sqlite3.connect(database, timeout=60)
        connection.row_factory = sqlite3.Row
        try:
            self.people = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        identity.id AS person_id,
                        identity.canonical_name,
                        snapshot.rendered_text AS profile_text,
                        (
                          SELECT COUNT(*)
                          FROM memory_person_observations AS observation
                          WHERE observation.chat_name = identity.chat_name
                            AND observation.person_id = identity.id
                            AND observation.quality_status = 'active'
                        ) AS observation_count
                    FROM memory_person_identities AS identity
                    JOIN memory_person_snapshots AS snapshot
                      ON snapshot.chat_name = identity.chat_name
                     AND snapshot.person_id = identity.id
                     AND snapshot.is_active = 1
                    WHERE identity.chat_name = ?
                      AND identity.status = 'active'
                    ORDER BY observation_count DESC, identity.id
                    """,
                    (chat_name,),
                ).fetchall()
            ]
            self.active_people = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id AS person_id, canonical_name
                    FROM memory_person_identities
                    WHERE chat_name = ? AND status = 'active'
                    ORDER BY id
                    """,
                    (chat_name,),
                ).fetchall()
            ]
            self.active_person_ids = {
                int(person["person_id"])
                for person in self.active_people
            }
            alias_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT person_id, alias_name, external_id, source,
                           confidence, status, first_seen_at, last_seen_at
                    FROM memory_person_aliases
                    WHERE chat_name = ?
                    ORDER BY person_id, id
                    """,
                    (chat_name,),
                ).fetchall()
            ]
            self.aliases_by_person: Dict[int, List[Dict[str, Any]]] = {}
            for row in alias_rows:
                self.aliases_by_person.setdefault(
                    int(row["person_id"]),
                    [],
                ).append(row)
            self.messages = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, source_namespace, source_cursor, source_id,
                           message_time, sender_name, sender_external_id,
                           content
                    FROM memory_person_source_messages
                    WHERE chat_name = ?
                    ORDER BY source_namespace, source_cursor, id
                    """,
                    (chat_name,),
                ).fetchall()
            ]
            self.events = []
            for row in connection.execute(
                """
                SELECT id, start_time, end_time, title, summary,
                       participants_json, search_text
                FROM memory_events
                WHERE chat_name = ?
                  AND is_invalidated = 0
                  AND superseded_by_event_id = 0
                  AND COALESCE(verification_status, 'not_required')
                      != 'quarantined'
                ORDER BY id DESC
                """,
                (chat_name,),
            ).fetchall():
                value = dict(row)
                value["participants"] = _json_load(
                    value.pop("participants_json", "[]"),
                    [],
                )
                self.events.append(value)
        finally:
            connection.close()

        self.message_by_reference = {
            _message_reference(message): message
            for message in self.messages
        }
        self.event_by_reference = {
            _event_reference(event): event
            for event in self.events
        }
        self.valid_references = (
            set(self.message_by_reference) | set(self.event_by_reference)
        )
        self._messages_by_namespace: Dict[str, List[Dict[str, Any]]] = {}
        self._message_positions: Dict[Tuple[str, int], int] = {}
        for message in self.messages:
            namespace = str(message.get("source_namespace") or "")
            bucket = self._messages_by_namespace.setdefault(namespace, [])
            self._message_positions[
                (namespace, int(message.get("source_cursor") or 0))
            ] = len(bucket)
            bucket.append(message)

    def aliases_for(self, person_id: int) -> List[Dict[str, Any]]:
        return list(self.aliases_by_person.get(int(person_id), []))

    def covering_confirmed_owners(
        self,
        alias_name: str,
        *,
        excluded_person_id: int = 0,
    ) -> List[int]:
        """Return owners of longer confirmed names containing this alias.

        A short candidate embedded in an existing complete display name is
        not free for reply-adjacency guessing.  ``黄工`` inside
        ``AAA 专业炒粉画图黄工`` is the motivating example.
        """

        alias = _normalize_alias(alias_name).casefold()
        if len(alias) < 2:
            return []
        owners = set()
        for person_id in self.active_person_ids:
            if person_id == int(excluded_person_id or 0):
                continue
            for item in self.aliases_for(person_id):
                if str(item.get("status") or "") != "confirmed":
                    continue
                full_name = _normalize_alias(
                    item.get("alias_name")
                ).casefold()
                if (
                    len(full_name) > len(alias)
                    and alias in full_name
                ):
                    owners.add(person_id)
                    break
        return sorted(owners)

    def confirmed_names_for(self, person_id: int) -> List[str]:
        names: List[str] = []
        person = next(
            (
                item
                for item in self.people
                if int(item["person_id"]) == int(person_id)
            ),
            None,
        )
        if person is not None:
            names.append(str(person["canonical_name"]))
        names.extend(
            str(alias.get("alias_name") or "")
            for alias in self.aliases_for(person_id)
            if str(alias.get("status") or "") == "confirmed"
        )
        return [
            value
            for value in dict.fromkeys(
                name.strip() for name in names if name.strip()
            )
        ]

    @staticmethod
    def _message_line(message: Dict[str, Any]) -> str:
        content = re.sub(
            r"\s+",
            " ",
            str(message.get("content") or ""),
        ).strip()
        return (
            f"[{_message_reference(message)}] "
            f"[{message.get('message_time') or '?'}] "
            f"[{message.get('sender_name') or '?'}] {content[:700]}"
        )

    @staticmethod
    def _event_line(event: Dict[str, Any]) -> str:
        participants = "、".join(
            str(value) for value in event.get("participants") or []
        )
        return (
            f"[{_event_reference(event)}] "
            f"[{event.get('start_time') or '?'}] "
            f"参与者={participants}；"
            f"{event.get('title') or ''}：{event.get('summary') or ''}"
        )[:1800]

    def _message_neighborhood(
        self,
        message: Dict[str, Any],
        radius: int = 1,
    ) -> List[Dict[str, Any]]:
        namespace = str(message.get("source_namespace") or "")
        cursor = int(message.get("source_cursor") or 0)
        position = self._message_positions.get((namespace, cursor))
        bucket = self._messages_by_namespace.get(namespace, [])
        if position is None:
            return [message]
        return bucket[
            max(0, position - radius) : min(
                len(bucket),
                position + radius + 1,
            )
        ]

    def _following_messages(
        self,
        message: Dict[str, Any],
        radius: int = 2,
    ) -> List[Dict[str, Any]]:
        namespace = str(message.get("source_namespace") or "")
        cursor = int(message.get("source_cursor") or 0)
        position = self._message_positions.get((namespace, cursor))
        bucket = self._messages_by_namespace.get(namespace, [])
        if position is None:
            return []
        return bucket[position + 1 : min(len(bucket), position + radius + 1)]

    def discovery_evidence(
        self,
        person: Dict[str, Any],
        *,
        maximum_messages: int = 240,
        maximum_events: int = 80,
    ) -> Dict[str, Any]:
        name = str(person["canonical_name"])
        target_names = self.confirmed_names_for(int(person["person_id"]))
        folded_names = [
            value.casefold() for value in target_names if len(value) >= 3
        ]
        direct: List[Tuple[int, Dict[str, Any]]] = []
        for message in self.messages:
            content = str(message.get("content") or "")
            content_folded = content.casefold()
            direct_at = any(
                _mentions_person(content, target_name)
                for target_name in target_names
            )
            literal = any(
                folded_name in content_folded
                for folded_name in folded_names
            )
            if not direct_at and not literal:
                continue
            score = 120 if direct_at else 30
            if str(message.get("sender_name") or "") not in target_names:
                score += 20
            if any(cue.casefold() in content_folded for cue in ALIAS_CUES):
                score += 35
            if str(message.get("sender_name") or "") in BOT_OR_DERIVED_SENDERS:
                score -= 45
            direct.append((score, message))
        direct.sort(
            key=lambda item: (
                item[0],
                str(item[1].get("message_time") or ""),
            ),
            reverse=True,
        )
        selected_messages: Dict[str, Dict[str, Any]] = {}
        direct_messages: List[Dict[str, Any]] = []
        for _, message in direct[:maximum_messages]:
            direct_messages.append(message)
            selected_messages[_message_reference(message)] = message
            # A target's immediate reply is valuable co-reference evidence, but
            # unrelated surrounding chatter dilutes nickname extraction.
            for neighbor in self._message_neighborhood(message, radius=2):
                if str(neighbor.get("sender_name") or "") in target_names:
                    selected_messages[_message_reference(neighbor)] = neighbor

        relevant_events: List[Tuple[int, Dict[str, Any]]] = []
        for event in self.events:
            participants = {
                str(value) for value in event.get("participants") or []
            }
            if not participants.intersection(target_names):
                continue
            text = " ".join(
                (
                    str(event.get("title") or ""),
                    str(event.get("summary") or ""),
                )
            )
            text_folded = text.casefold()
            score = 20
            if any(
                folded_name in text_folded
                for folded_name in folded_names
            ):
                score += 50
            if any(cue.casefold() in text_folded for cue in ALIAS_CUES):
                score += 20
            relevant_events.append((score, event))
        relevant_events.sort(
            key=lambda item: (
                item[0],
                int(item[1].get("id") or 0),
            ),
            reverse=True,
        )
        events = [
            event for _, event in relevant_events[:maximum_events]
        ]
        return {
            "messages": list(selected_messages.values()),
            "direct_messages": direct_messages,
            "events": events,
        }

    def verification_evidence(
        self,
        person: Dict[str, Any],
        aliases: Sequence[str],
        *,
        maximum_messages_per_alias: int = 140,
        maximum_events_per_alias: int = 80,
    ) -> Dict[str, Any]:
        selected_messages: Dict[str, Dict[str, Any]] = {}
        selected_events: Dict[str, Dict[str, Any]] = {}
        alias_stats = {}
        for alias in aliases:
            folded = alias.casefold()
            matches = [
                message
                for message in self.messages
                if folded in str(message.get("content") or "").casefold()
            ]
            target_name = str(person.get("canonical_name") or "")
            target_names = set(
                self.confirmed_names_for(int(person["person_id"]))
            )
            ranked_matches: List[Tuple[int, Dict[str, Any], bool]] = []
            human_matches = []
            target_reply_contexts = 0
            target_reply_senders = set()
            direct_target_mentions = 0
            for message in matches:
                sender = str(message.get("sender_name") or "")
                if sender in BOT_OR_DERIVED_SENDERS:
                    continue
                human_matches.append(message)
                following = self._following_messages(message, radius=2)
                target_reply = sender not in target_names and any(
                    str(item.get("sender_name") or "") in target_names
                    for item in following
                )
                content_folded = str(
                    message.get("content") or ""
                ).casefold()
                direct_target = any(
                    _mentions_person(content_folded, candidate_name)
                    for candidate_name in target_names
                )
                score = 50
                if direct_target:
                    score += 180
                    direct_target_mentions += 1
                if target_reply:
                    score += 90
                    target_reply_contexts += 1
                    target_reply_senders.add(sender)
                if any(
                    cue.casefold() in content_folded
                    for cue in ALIAS_CUES
                ):
                    score += 25
                ranked_matches.append((score, message, target_reply))
            ranked_matches.sort(
                key=lambda item: (
                    item[0],
                    str(item[1].get("message_time") or ""),
                ),
                reverse=True,
            )
            ranked_references = [
                _message_reference(message)
                for _, message, _ in ranked_matches[:16]
            ]
            for _, message, _ in ranked_matches[
                :maximum_messages_per_alias
            ]:
                neighborhood = self._message_neighborhood(message, radius=2)
                for neighbor in neighborhood:
                    selected_messages[_message_reference(neighbor)] = neighbor
            event_matches = [
                event
                for event in self.events
                if folded
                in (
                    str(event.get("title") or "")
                    + " "
                    + str(event.get("summary") or "")
                ).casefold()
            ]
            for event in event_matches[:maximum_events_per_alias]:
                selected_events[_event_reference(event)] = event
            ranked_references.extend(
                _event_reference(event) for event in event_matches[:8]
            )
            alias_stats[alias] = {
                "raw_message_occurrences": len(matches),
                "human_message_occurrences": len(human_matches),
                "event_occurrences": len(event_matches),
                "direct_target_mentions": direct_target_mentions,
                "target_reply_contexts": target_reply_contexts,
                "target_reply_senders": sorted(target_reply_senders)[:30],
                "human_senders": sorted(
                    {
                        str(message.get("sender_name") or "")
                        for message in human_matches
                        if str(message.get("sender_name") or "")
                    }
                )[:30],
                "top_evidence_refs": list(
                    dict.fromkeys(ranked_references)
                ),
            }
        return {
            "messages": list(selected_messages.values()),
            "events": list(selected_events.values()),
            "alias_stats": alias_stats,
        }

    def render_evidence(
        self,
        evidence: Dict[str, Any],
        *,
        context_manager: ChatContextManager,
        token_budget: int,
    ) -> str:
        sections = []
        messages = list(evidence.get("messages") or [])
        events = list(evidence.get("events") or [])
        if messages:
            sections.append(
                "## 原始消息与相邻接话\n"
                + "\n".join(self._message_line(item) for item in messages)
            )
        if events:
            sections.append(
                "## 已验证事件摘要\n"
                + "\n".join(self._event_line(item) for item in events)
            )
        if evidence.get("alias_stats"):
            sections.append(
                "## 候选词全局统计\n"
                + json.dumps(
                    evidence["alias_stats"],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return context_manager.truncate_text_to_budget(
            "\n\n".join(sections),
            max(3000, int(token_budget)),
            notice="别名审计证据达到输入预算上限",
        )


def _configure_alias_manager(workspace: Path):
    litellm.disable_aiohttp_transport = True
    manager = _configure_deepseek_manager(workspace)
    assistant_routes = manager.config.setdefault("plugin_mappings", {}).setdefault(
        "assistant",
        {},
    )
    for call_type in ("memory_generate", "memory_review"):
        mapping = assistant_routes.setdefault(call_type, {})
        mapping["primary"] = "deepseek"
        mapping["fallback"] = []
        mapping["override_params"] = {}
    return manager


def _build_call_json(
    *,
    database: Path,
    chat_name: str,
    workspace: Path,
    budget: DeepSeekBudget,
) -> Tuple[ChatMemoryService, Any, ChatContextManager]:
    context_manager = ChatContextManager()
    manager = _configure_alias_manager(workspace)
    service = ChatMemoryService(
        _NoChatLog(),
        context_manager,
        store=MemoryStore(database),
        llm_manager=manager,
        llm_history_chat_name=chat_name,
        llm_history_mode="summary",
        llm_usage_callback=budget.record_usage,
    )

    def call_json(**kwargs: Any) -> Dict[str, Any]:
        messages = kwargs.get("messages") or []
        estimated_input = sum(
            context_manager.estimate_tokens(message.get("content") or "")
            for message in messages
        ) + 500
        reservation = budget.reserve(
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=5000,
        )
        try:
            return service._call_memory_json(**kwargs)
        finally:
            budget.release(reservation)

    return service, call_json, context_manager


def _discover_person(
    corpus: AliasAuditCorpus,
    person: Dict[str, Any],
    *,
    call_json: Any,
    context_manager: ChatContextManager,
    token_budget: int,
) -> Dict[str, Any]:
    person_id = int(person["person_id"])
    name = str(person["canonical_name"])
    aliases = corpus.aliases_for(person_id)
    evidence = corpus.discovery_evidence(person)
    rendered = corpus.render_evidence(
        evidence,
        context_manager=context_manager,
        token_budget=token_budget,
    )
    response = call_json(
        call_type="memory_person_alias_discover",
        chat_name=corpus.chat_name,
        schema_hint=(
            '根对象必须是 {"person_name":"...","aliases":[{"alias":"...",'
            '"decision":"confirm|review|reject","confidence":0.0,'
            '"evidence_refs":["M:namespace:cursor","E:id"],'
            '"reason":"..."}],"none_reason":"..."}，候选数组字段名必须是aliases'
        ),
        messages=[
            {
                "role": "system",
                "content": (
                    "你负责高召回地发现微信群人物别名候选，本阶段只负责提名，不负责"
                    "最终确证。逐条查看直接@目标的消息，提取@目标前后被当作称呼使用的"
                    "昵称、绰号、简称、旧称和带姓/字母的尊称；例如“@甲 熊猫哥”应提名"
                    "核心称呼“熊猫”，“王总 @甲”可提名“王总”。消息后目标立即接话是"
                    "重要线索。证据稍弱也输出review，留给下一阶段全库消歧；不要因未达到"
                    "确证门槛而返回空数组。排除纯粹的哥/姐/总/老板等无区分度泛称，以及"
                    "动物、游戏角色、物品和明显指向其他群员的名字。机器人/OCR只能作"
                    "线索。根对象必须含aliases数组，每项必须含alias、decision、"
                    "confidence、evidence_refs、reason；没有候选时也必须给none_reason。"
                    "只输出JSON。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"群：{corpus.chat_name}\n"
                    f"目标人物：{name}（person_id={person_id}）\n"
                    f"当前身份别名："
                    f"{json.dumps(aliases, ensure_ascii=False)}\n"
                    f"活跃观察数：{person.get('observation_count') or 0}\n\n"
                    f"{rendered}"
                ),
            },
        ],
    )
    candidates = []
    for raw in _response_items(
        response,
        "aliases",
        "candidates",
        "nicknames",
        "results",
    ):
        if isinstance(raw, str):
            raw = {"alias": raw}
        if not isinstance(raw, dict):
            continue
        alias = _normalize_alias(
            raw.get("alias")
            or raw.get("nickname")
            or raw.get("name")
            or raw.get("candidate")
        )
        if not _valid_alias(alias, name):
            continue
        references = [
            str(value)
            for value in raw.get("evidence_refs") or []
            if str(value) in corpus.valid_references
        ]
        candidates.append(
            {
                "alias": alias,
                "proposal_decision": str(
                    raw.get("decision") or "review"
                ).lower(),
                "proposal_confidence": _bounded_confidence(
                    raw.get("confidence"),
                    0.65,
                ),
                "proposal_evidence_refs": list(dict.fromkeys(references)),
                "proposal_reason": _clean_text(raw.get("reason"), 500),
            }
        )
        if alias.endswith(("哥", "姐")):
            core_alias = _normalize_alias(alias[:-1])
            if _valid_alias(core_alias, name):
                candidates.append(
                    {
                        "alias": core_alias,
                        "proposal_decision": "review",
                        "proposal_confidence": max(
                            0.5,
                            _bounded_confidence(
                                raw.get("confidence"),
                                0.65,
                            )
                            - 0.05,
                        ),
                        "proposal_evidence_refs": list(
                            dict.fromkeys(references)
                        ),
                        "proposal_reason": (
                            f"由带称谓候选“{alias}”派生的核心称呼，"
                            "需独立全库复核。"
                        ),
                    }
                )
    deduplicated = {}
    for candidate in candidates:
        key = candidate["alias"].casefold()
        current = deduplicated.get(key)
        if (
            current is None
            or candidate["proposal_confidence"]
            > current["proposal_confidence"]
        ):
            deduplicated[key] = candidate
    return {
        "person_id": person_id,
        "canonical_name": name,
        "existing_aliases": aliases,
        "candidates": list(deduplicated.values()),
        "none_reason": _clean_text(
            _response_value(
                response,
                "none_reason",
                "reason",
                "explanation",
            ),
            500,
        ),
        "model_response": response,
        "evidence_counts": {
            "messages": len(evidence.get("messages") or []),
            "direct_messages": len(
                evidence.get("direct_messages") or []
            ),
            "events": len(evidence.get("events") or []),
        },
    }


def _verify_person(
    corpus: AliasAuditCorpus,
    person: Dict[str, Any],
    discovery: Dict[str, Any],
    *,
    call_json: Any,
    context_manager: ChatContextManager,
    token_budget: int,
) -> Dict[str, Any]:
    candidates = [
        item
        for item in discovery.get("candidates") or []
        if float(item.get("proposal_confidence") or 0.0) >= 0.30
        and item.get("proposal_decision") != "reject"
    ]
    if not candidates:
        return {
            "person_id": int(person["person_id"]),
            "canonical_name": str(person["canonical_name"]),
            "results": [],
        }
    results = []
    model_responses = {}
    for proposal in candidates:
        alias = str(proposal["alias"])
        evidence = corpus.verification_evidence(person, [alias])
        rendered = corpus.render_evidence(
            evidence,
            context_manager=context_manager,
            token_budget=token_budget,
        )
        schema_hint = (
            '根对象必须是 {"alias":"...","decision":'
            '"confirm|review|reject","confidence":0.0,'
            '"evidence_refs":["M:namespace:cursor","E:id"],'
            '"reason":"..."}'
        )

        def verification_messages(evidence_text: str) -> List[Dict[str, str]]:
            return [
                {
                    "role": "system",
                    "content": (
                        "你是独立的微信群人物别名复核员。每次只复核一个候选词。检查它"
                        "是否稳定指向目标人物，还是动物/作品/临时玩梗/泛称/其他群员。"
                        "优先级最高的是多个日期、多个真人直接@目标并同时使用候选，以及"
                        "候选出现后目标立即接话。带姓氏、字母或独特词根的尊称可作为"
                        "别名；纯哥/姐/总/老板不可。机器人生成文本不能单独确证。若同一"
                        "词明显指向多人或仅偶发玩梗，必须review/reject。confirm代表可以"
                        "安全写入身份目录，置信度须至少0.94。只输出一个JSON对象，必须"
                        "原样返回alias，并输出decision、confidence、evidence_refs、"
                        "reason；evidence_refs最多6条，reason最多200字；不要输出数组，"
                        "不补造证据编号。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"群：{corpus.chat_name}\n"
                        f"目标人物：{person['canonical_name']}"
                        f"（person_id={person['person_id']}）\n"
                        f"唯一候选：{json.dumps(proposal, ensure_ascii=False)}"
                        f"\n\n{evidence_text}"
                    ),
                },
            ]

        try:
            response = call_json(
                call_type="memory_person_alias_review",
                chat_name=corpus.chat_name,
                schema_hint=schema_hint,
                messages=verification_messages(rendered),
            )
        except Exception as first_error:
            compact_evidence = context_manager.truncate_text_to_budget(
                rendered,
                min(4500, max(2500, int(token_budget) // 2)),
                notice="别名复核首次格式异常，已缩短证据重试",
            )
            try:
                response = call_json(
                    call_type="memory_person_alias_review",
                    chat_name=corpus.chat_name,
                    schema_hint=schema_hint,
                    messages=verification_messages(compact_evidence),
                )
            except Exception as second_error:
                response = {
                    "alias": alias,
                    "decision": "review",
                    "confidence": 0.0,
                    "evidence_refs": [],
                    "reason": (
                        "模型复核两次均未返回合法JSON，保留人工复核："
                        f"{type(first_error).__name__}/"
                        f"{type(second_error).__name__}"
                    ),
                }
        model_responses[alias] = response
        raw: Any = response
        wrapped = _response_items(
            response,
            "results",
            "aliases",
            "verifications",
            "candidates",
        )
        if wrapped:
            matching = [
                item
                for item in wrapped
                if isinstance(item, dict)
                and _normalize_alias(
                    item.get("alias")
                    or item.get("nickname")
                    or item.get("name")
                ).casefold()
                == alias.casefold()
            ]
            if matching:
                raw = matching[0]
        if not isinstance(raw, dict):
            continue
        returned_alias = _normalize_alias(
            raw.get("alias")
            or raw.get("nickname")
            or raw.get("name")
        )
        if returned_alias.casefold() != alias.casefold():
            continue
        stats = (evidence.get("alias_stats") or {}).get(alias, {})
        references = list(
            dict.fromkeys(
                str(value)
                for value in raw.get("evidence_refs") or []
                if str(value) in corpus.valid_references
            )
        )
        if not references:
            references = [
                str(value)
                for value in stats.get("top_evidence_refs") or []
                if str(value) in corpus.valid_references
            ][:8]
        results.append(
            {
                **proposal,
                "verification_decision": str(
                    raw.get("decision") or "review"
                ).lower(),
                "verification_confidence": _bounded_confidence(
                    raw.get("confidence"),
                    0.0,
                ),
                "verification_evidence_refs": references,
                "verification_reason": _clean_text(
                    raw.get("reason"),
                    700,
                ),
                "corpus_stats": stats,
            }
        )
    return {
        "person_id": int(person["person_id"]),
        "canonical_name": str(person["canonical_name"]),
        "results": results,
        "model_responses": model_responses,
    }


def _load_candidate_sender_ids(
    candidate_database: Optional[Path],
    corpus: AliasAuditCorpus,
) -> List[Dict[str, Any]]:
    if candidate_database is None or not candidate_database.is_file():
        return []
    connection = sqlite3.connect(candidate_database, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        candidate_rows = connection.execute(
            """
            SELECT identity.canonical_name, alias.alias_name,
                   alias.external_id, alias.source, alias.confidence,
                   alias.first_seen_at, alias.last_seen_at
            FROM memory_person_identities AS identity
            JOIN memory_person_aliases AS alias
              ON alias.person_id = identity.id
            WHERE identity.chat_name = ?
              AND identity.status = 'active'
              AND alias.status = 'confirmed'
              AND alias.external_id != ''
            ORDER BY identity.id, alias.id
            """,
            (corpus.chat_name,),
        ).fetchall()
    finally:
        connection.close()
    production_by_name = {
        str(person["canonical_name"]).casefold(): person
        for person in corpus.people
    }
    result = []
    seen_external_ids = set()
    for row in candidate_rows:
        names = (
            str(row["canonical_name"] or "").strip(),
            str(row["alias_name"] or "").strip(),
        )
        person = next(
            (
                production_by_name.get(name.casefold())
                for name in names
                if name and production_by_name.get(name.casefold())
            ),
            None,
        )
        external_id = str(row["external_id"] or "").strip()
        if person is None or not external_id:
            continue
        key = (int(person["person_id"]), external_id)
        if key in seen_external_ids:
            continue
        seen_external_ids.add(key)
        result.append(
            {
                "person_id": int(person["person_id"]),
                "canonical_name": str(person["canonical_name"]),
                "alias_name": str(row["alias_name"] or ""),
                "external_id": external_id,
                "source": str(row["source"] or "historical_sender_id"),
                "confidence": float(row["confidence"] or 1.0),
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
            }
        )
    return result


def prepare_audit_database(
    *,
    source_database: Path,
    output_database: Path,
    chat_name: str,
    identity_merge_plan: Path,
) -> Dict[str, Any]:
    """Create an isolated production snapshot and apply only merge staging."""
    if output_database.exists():
        raise FileExistsError(output_database)
    merges = _load_identity_merge_plan(identity_merge_plan)
    if not merges:
        raise ValueError("identity merge plan is empty")
    output_database.parent.mkdir(parents=True, exist_ok=True)
    _backup_database(source_database, output_database)
    store = MemoryStore(output_database)
    audits = []
    for item in merges:
        audits.append(
            store.merge_people(
                chat_name,
                int(item["source_person_id"]),
                int(item["target_person_id"]),
                reason=str(
                    item.get("reason")
                    or "isolated alias-audit identity merge staging"
                ),
            )
        )
    with store._connection() as connection:
        active_profiles = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM memory_person_identities
                WHERE chat_name = ? AND status = 'active'
                """,
                (chat_name,),
            ).fetchone()[0]
        )
        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
    if quick_check != "ok":
        raise RuntimeError(
            f"prepared audit database quick_check failed: {quick_check}"
        )
    return {
        "status": "prepared",
        "chat_name": chat_name,
        "source_database": str(source_database),
        "output_database": str(output_database),
        "identity_merge_count": len(merges),
        "active_profile_count": active_profiles,
        "merge_audit_ids": [
            int(item.get("id") or 0) for item in audits
        ],
        "quick_check": quick_check,
        "prepared_at": _now(),
    }


def build_alias_audit(
    *,
    database: Path,
    chat_name: str,
    workspace: Path,
    candidate_database: Optional[Path],
    concurrency: int,
    budget_cny: float,
    input_token_budget: int,
    only_people: Optional[Sequence[str]] = None,
    expected_profile_count: Optional[int] = 64,
    identity_merge_plan: Optional[Path] = None,
    alias_policy_path: Optional[Path] = None,
) -> Dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    discovery_directory = workspace / "discovery"
    verification_directory = workspace / "verification"
    discovery_directory.mkdir(exist_ok=True)
    verification_directory.mkdir(exist_ok=True)
    corpus = AliasAuditCorpus(database, chat_name)
    if (
        expected_profile_count is not None
        and len(corpus.people) != int(expected_profile_count)
    ):
        raise RuntimeError(
            f"expected {int(expected_profile_count)} active profiles, "
            f"found {len(corpus.people)}"
        )
    requested_people = {
        str(value).strip().casefold()
        for value in (only_people or [])
        if str(value).strip()
    }
    audit_people = list(corpus.people)
    if requested_people:
        audit_people = [
            person
            for person in corpus.people
            if str(person["canonical_name"]).casefold()
            in requested_people
            or str(person["person_id"]) in requested_people
        ]
        found = {
            str(person["canonical_name"]).casefold()
            for person in audit_people
        } | {str(person["person_id"]) for person in audit_people}
        missing = sorted(requested_people - found)
        if missing:
            raise RuntimeError(
                "requested people were not found: " + ", ".join(missing)
            )
        if not audit_people:
            raise RuntimeError("no people selected for alias audit")
    is_partial = len(audit_people) != len(corpus.people)
    budget = DeepSeekBudget(
        workspace / "cost.json",
        limit_cny=budget_cny,
    )
    service, call_json, context_manager = _build_call_json(
        database=database,
        chat_name=chat_name,
        workspace=workspace,
        budget=budget,
    )
    output_lock = threading.Lock()

    def discovery_path(person: Dict[str, Any]) -> Path:
        return discovery_directory / (
            f"{int(person['person_id'])}-"
            f"{_safe_filename(str(person['canonical_name']))}.json"
        )

    def verification_path(person: Dict[str, Any]) -> Path:
        return verification_directory / (
            f"{int(person['person_id'])}-"
            f"{_safe_filename(str(person['canonical_name']))}.json"
        )

    try:
        discovery_results: Dict[int, Dict[str, Any]] = {}
        discovery_pending = []
        for person in audit_people:
            path = discovery_path(person)
            if path.is_file():
                discovery_results[int(person["person_id"])] = json.loads(
                    path.read_text(encoding="utf-8")
                )
            else:
                discovery_pending.append(person)
        with ThreadPoolExecutor(
            max_workers=max(1, min(24, int(concurrency))),
            thread_name_prefix="alias-discovery",
        ) as executor:
            futures = {
                executor.submit(
                    _discover_person,
                    corpus,
                    person,
                    call_json=call_json,
                    context_manager=context_manager,
                    token_budget=input_token_budget,
                ): person
                for person in discovery_pending
            }
            completed = len(discovery_results)
            for future in as_completed(futures):
                person = futures[future]
                result = future.result()
                with output_lock:
                    _write_json(discovery_path(person), result)
                    discovery_results[int(person["person_id"])] = result
                    completed += 1
                    print(
                        "[alias-audit] discovery "
                        f"{completed}/{len(audit_people)} "
                        f"{person['canonical_name']}: "
                        f"{len(result.get('candidates') or [])} candidates",
                        flush=True,
                    )

        verification_results: Dict[int, Dict[str, Any]] = {}
        verification_pending = []
        people_by_id = {
            int(person["person_id"]): person
            for person in audit_people
        }
        for person_id, discovery in discovery_results.items():
            person = people_by_id[person_id]
            path = verification_path(person)
            if path.is_file():
                existing_verification = json.loads(
                    path.read_text(encoding="utf-8")
                )
                eligible_aliases = {
                    str(item.get("alias") or "").casefold()
                    for item in discovery.get("candidates") or []
                    if float(
                        item.get("proposal_confidence") or 0.0
                    )
                    >= 0.30
                    and item.get("proposal_decision") != "reject"
                }
                reviewed_aliases = {
                    str(value).casefold()
                    for value in (
                        existing_verification.get("model_responses") or {}
                    )
                }
                reviewed_aliases.update(
                    str(item.get("alias") or "").casefold()
                    for item in existing_verification.get("results") or []
                )
                if eligible_aliases.issubset(reviewed_aliases):
                    verification_results[person_id] = (
                        existing_verification
                    )
                else:
                    verification_pending.append((person, discovery))
            else:
                verification_pending.append((person, discovery))
        with ThreadPoolExecutor(
            max_workers=max(1, min(16, int(concurrency))),
            thread_name_prefix="alias-verification",
        ) as executor:
            futures = {
                executor.submit(
                    _verify_person,
                    corpus,
                    person,
                    discovery,
                    call_json=call_json,
                    context_manager=context_manager,
                    token_budget=input_token_budget,
                ): person
                for person, discovery in verification_pending
            }
            completed = len(verification_results)
            for future in as_completed(futures):
                person = futures[future]
                result = future.result()
                with output_lock:
                    _write_json(verification_path(person), result)
                    verification_results[int(person["person_id"])] = result
                    completed += 1
                    print(
                        "[alias-audit] verification "
                        f"{completed}/{len(audit_people)} "
                        f"{person['canonical_name']}: "
                        f"{len(result.get('results') or [])} reviewed",
                        flush=True,
                    )
    finally:
        service.close()

    existing_owners: Dict[str, List[int]] = {}
    for person_id in corpus.active_person_ids:
        for alias in corpus.aliases_for(person_id):
            if alias.get("status") != "confirmed":
                continue
            existing_owners.setdefault(
                str(alias.get("alias_name") or "").casefold(),
                [],
            ).append(int(person_id))

    candidates = []
    for person_id, verification in verification_results.items():
        for item in verification.get("results") or []:
            alias = str(item.get("alias") or "")
            refreshed_evidence = corpus.verification_evidence(
                people_by_id[int(person_id)],
                [alias],
            )
            item = {
                **item,
                "corpus_stats": (
                    refreshed_evidence.get("alias_stats") or {}
                ).get(alias, {}),
            }
            references = list(
                dict.fromkeys(
                    item.get("verification_evidence_refs") or []
                )
            )
            decision = str(
                item.get("verification_decision") or "review"
            )
            confidence = float(
                item.get("verification_confidence") or 0.0
            )
            stats = (
                item.get("corpus_stats")
                if isinstance(item.get("corpus_stats"), dict)
                else {}
            )
            target_reply_senders = {
                str(value)
                for value in stats.get("target_reply_senders") or []
                if str(value)
            }
            deterministic_support = (
                int(stats.get("direct_target_mentions") or 0) >= 2
                or (
                    int(stats.get("target_reply_contexts") or 0) >= 3
                    and len(target_reply_senders) >= 2
                    and int(
                        stats.get("human_message_occurrences") or 0
                    )
                    >= 3
                )
            )
            classification = "review"
            if (
                decision == "confirm"
                and confidence >= 0.94
                and len(references) >= 2
                and deterministic_support
            ):
                classification = "confirmed"
            elif decision == "reject":
                classification = "rejected"
            owners = sorted(
                set(existing_owners.get(alias.casefold(), []))
                - {int(person_id)}
            )
            covering_owners = corpus.covering_confirmed_owners(
                alias,
                excluded_person_id=int(person_id),
            )
            owners = sorted(set(owners) | set(covering_owners))
            if owners:
                classification = "identity_conflict"
            elif int(person_id) in set(
                existing_owners.get(alias.casefold(), [])
            ):
                classification = "already_confirmed"
            candidates.append(
                {
                    "person_id": int(person_id),
                    "canonical_name": str(
                        verification.get("canonical_name") or ""
                    ),
                    **item,
                    "classification": classification,
                    "existing_other_person_ids": owners,
                    "covering_other_person_ids": covering_owners,
                }
            )

    # A common title may be proposed for several nearby speakers. Resolve only
    # when one owner dominates by exact @ evidence or by a large reply-context
    # margin; otherwise keep every contender out of production.
    credible_by_alias: Dict[str, List[Dict[str, Any]]] = {}
    for item in candidates:
        if (
            str(item.get("verification_decision") or "") == "confirm"
            and float(item.get("verification_confidence") or 0.0) >= 0.94
            and item.get("classification") != "identity_conflict"
        ):
            credible_by_alias.setdefault(
                str(item.get("alias") or "").casefold(),
                [],
            ).append(item)
    for group in credible_by_alias.values():
        owners = {int(item["person_id"]) for item in group}
        if len(owners) <= 1:
            continue
        direct_ranked = sorted(
            group,
            key=lambda item: int(
                (item.get("corpus_stats") or {}).get(
                    "direct_target_mentions"
                )
                or 0
            ),
            reverse=True,
        )
        direct_top = int(
            (direct_ranked[0].get("corpus_stats") or {}).get(
                "direct_target_mentions"
            )
            or 0
        )
        direct_runner = int(
            (direct_ranked[1].get("corpus_stats") or {}).get(
                "direct_target_mentions"
            )
            or 0
        )
        winner: Optional[Dict[str, Any]] = None
        if direct_top >= 2 and direct_top >= direct_runner + 2:
            winner = direct_ranked[0]
        else:
            reply_ranked = sorted(
                group,
                key=lambda item: int(
                    (item.get("corpus_stats") or {}).get(
                        "target_reply_contexts"
                    )
                    or 0
                ),
                reverse=True,
            )
            reply_top = int(
                (reply_ranked[0].get("corpus_stats") or {}).get(
                    "target_reply_contexts"
                )
                or 0
            )
            reply_runner = int(
                (reply_ranked[1].get("corpus_stats") or {}).get(
                    "target_reply_contexts"
                )
                or 0
            )
            reply_senders = len(
                (reply_ranked[0].get("corpus_stats") or {}).get(
                    "target_reply_senders"
                )
                or []
            )
            if (
                reply_top >= 5
                and reply_senders >= 3
                and reply_top >= max(1, reply_runner) * 2.5
            ):
                winner = reply_ranked[0]
        for item in group:
            if winner is not None and int(item["person_id"]) == int(
                winner["person_id"]
            ):
                if item.get("classification") == "cross_person_conflict":
                    item["classification"] = "confirmed"
                continue
            if winner is not None:
                item["classification"] = "cross_person_rejected"
                item["conflict_winner_person_id"] = int(
                    winner["person_id"]
                )
            elif item.get("classification") == "confirmed":
                item["classification"] = "cross_person_conflict"

    alias_policy = _load_alias_policy(alias_policy_path)
    candidate_index = {
        (
            int(item["person_id"]),
            str(item.get("alias") or "").casefold(),
        ): item
        for item in candidates
    }
    for override in alias_policy["overrides"]:
        key = (
            int(override.get("person_id") or 0),
            str(override.get("alias") or "").strip().casefold(),
        )
        item = candidate_index.get(key)
        if item is None:
            raise RuntimeError(
                "alias policy override target was not found: "
                f"{key[0]}/{override.get('alias')}"
            )
        classification = str(
            override.get("classification") or ""
        ).strip()
        if classification not in {"confirmed", "review", "rejected"}:
            raise ValueError(
                f"invalid alias policy classification: {classification}"
            )
        item["classification"] = classification
        item["manual_policy_reason"] = _clean_text(
            override.get("reason"),
            1000,
        )
        item["context_sensitive"] = bool(
            override.get("context_sensitive")
        )
    for addition in alias_policy["additions"]:
        person_id = int(addition.get("person_id") or 0)
        person = people_by_id.get(person_id)
        if person is None:
            raise RuntimeError(
                f"alias policy addition person was not found: {person_id}"
            )
        alias = _normalize_alias(addition.get("alias"))
        if not _valid_alias(alias, str(person["canonical_name"])):
            raise ValueError(
                f"invalid policy alias {alias!r} for "
                f"{person['canonical_name']!r}"
            )
        key = (person_id, alias.casefold())
        if key in candidate_index:
            raise RuntimeError(
                f"alias policy addition already exists as candidate: {alias}"
            )
        evidence = corpus.verification_evidence(person, [alias])
        stats = (evidence.get("alias_stats") or {}).get(alias, {})
        item = {
            "person_id": person_id,
            "canonical_name": str(person["canonical_name"]),
            "alias": alias,
            "proposal_decision": "manual",
            "proposal_confidence": 1.0,
            "proposal_evidence_refs": list(
                stats.get("top_evidence_refs") or []
            )[:8],
            "proposal_reason": _clean_text(
                addition.get("reason"),
                1000,
            ),
            "verification_decision": "confirm",
            "verification_confidence": float(
                addition.get("confidence") or 0.96
            ),
            "verification_evidence_refs": list(
                stats.get("top_evidence_refs") or []
            )[:8],
            "verification_reason": _clean_text(
                addition.get("reason"),
                1000,
            ),
            "corpus_stats": stats,
            "classification": "confirmed",
            "existing_other_person_ids": [],
            "manual_policy_reason": _clean_text(
                addition.get("reason"),
                1000,
            ),
            "context_sensitive": bool(
                addition.get("context_sensitive")
            ),
        }
        candidates.append(item)
        candidate_index[key] = item

    confirmed_owners: Dict[str, set[int]] = {}
    for item in candidates:
        if item.get("classification") == "confirmed":
            confirmed_owners.setdefault(
                str(item.get("alias") or "").casefold(),
                set(),
            ).add(int(item["person_id"]))
    unresolved = {
        alias: sorted(owners)
        for alias, owners in confirmed_owners.items()
        if len(owners) > 1
    }
    if unresolved:
        raise RuntimeError(
            "alias policy left confirmed cross-person conflicts: "
            + json.dumps(unresolved, ensure_ascii=False)
        )

    people_report = []
    candidates_by_person: Dict[int, List[Dict[str, Any]]] = {}
    for item in candidates:
        candidates_by_person.setdefault(int(item["person_id"]), []).append(
            item
        )
    for person in audit_people:
        person_id = int(person["person_id"])
        discovery = discovery_results[person_id]
        people_report.append(
            {
                "person_id": person_id,
                "canonical_name": str(person["canonical_name"]),
                "observation_count": int(
                    person.get("observation_count") or 0
                ),
                "existing_aliases": corpus.aliases_for(person_id),
                "candidates": sorted(
                    candidates_by_person.get(person_id, []),
                    key=lambda item: (
                        item.get("classification") == "confirmed",
                        float(
                            item.get("verification_confidence") or 0.0
                        ),
                    ),
                    reverse=True,
                ),
                "none_reason": discovery.get("none_reason") or "",
                "audited": True,
            }
        )
    sender_id_sync = _load_candidate_sender_ids(
        candidate_database,
        corpus,
    ) if not is_partial else []
    report_identity_merges = _load_identity_merge_plan(
        identity_merge_plan
    )
    report = {
        "status": "candidate_ready",
        "chat_name": chat_name,
        "database": str(database),
        "candidate_database": (
            str(candidate_database) if candidate_database else ""
        ),
        "profile_count": len(corpus.people),
        "audited_profile_count": sum(
            bool(item.get("audited")) for item in people_report
        ),
        "partial": is_partial,
        "selected_people": [
            str(person["canonical_name"]) for person in audit_people
        ],
        "confirmed_alias_count": sum(
            item.get("classification") == "confirmed"
            for item in candidates
        ),
        "review_alias_count": sum(
            item.get("classification") == "review"
            for item in candidates
        ),
        "rejected_alias_count": sum(
            item.get("classification") == "rejected"
            for item in candidates
        ),
        "conflict_alias_count": sum(
            item.get("classification")
            in {"identity_conflict", "cross_person_conflict"}
            for item in candidates
        ),
        "sender_id_sync_count": len(sender_id_sync),
        "sender_id_sync": sender_id_sync,
        "identity_merge_count": len(report_identity_merges),
        "identity_merges": report_identity_merges,
        "alias_policy": str(alias_policy_path or ""),
        "people": people_report,
        "cost": budget.snapshot(),
        "completed_at": _now(),
    }
    _write_json(workspace / "report.json", report)
    return report


def activate_alias_audit(
    *,
    database: Path,
    chat_name: str,
    workspace: Path,
) -> Dict[str, Any]:
    report_path = workspace / "report.json"
    if not report_path.is_file():
        raise FileNotFoundError(report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "candidate_ready":
        raise RuntimeError("alias audit report is not ready")
    if report.get("chat_name") != chat_name:
        raise RuntimeError("alias audit chat does not match activation chat")
    if report.get("partial"):
        raise RuntimeError("partial alias audit cannot be activated")
    if int(report.get("audited_profile_count") or 0) != int(
        report.get("profile_count") or 0
    ):
        raise RuntimeError("not every active profile was audited")
    confirmed = [
        candidate
        for person in report.get("people") or []
        for candidate in person.get("candidates") or []
        if candidate.get("classification") == "confirmed"
    ]
    sender_sync = list(report.get("sender_id_sync") or [])
    identity_merges = list(report.get("identity_merges") or [])
    store = MemoryStore(database)
    merge_map = {
        int(item.get("source_person_id") or 0): int(
            item.get("target_person_id") or 0
        )
        for item in identity_merges
    }
    preflight_conflicts = []
    with store._connection() as connection:
        identity_status = {
            int(row["id"]): dict(row)
            for row in connection.execute(
                """
                SELECT id, status, merged_into_person_id
                FROM memory_person_identities
                WHERE chat_name = ?
                """,
                (chat_name,),
            ).fetchall()
        }
        for source_id, target_id in merge_map.items():
            source = identity_status.get(source_id)
            target = identity_status.get(target_id)
            if source is None or target is None:
                preflight_conflicts.append(
                    f"merge identity missing: {source_id}->{target_id}"
                )
                continue
            if str(target.get("status") or "") != "active":
                preflight_conflicts.append(
                    f"merge target is not active: {target_id}"
                )
            source_status = str(source.get("status") or "")
            if source_status == "merged":
                if int(source.get("merged_into_person_id") or 0) != target_id:
                    preflight_conflicts.append(
                        f"merge source {source_id} already points elsewhere"
                    )
            elif source_status != "active":
                preflight_conflicts.append(
                    f"merge source has invalid status: {source_id}/"
                    f"{source_status}"
                )
        for item in confirmed:
            person_id = int(item.get("person_id") or 0)
            alias = _normalize_alias(item.get("alias"))
            owners = {
                merge_map.get(int(row["person_id"]), int(row["person_id"]))
                for row in connection.execute(
                    """
                    SELECT alias.person_id
                    FROM memory_person_aliases AS alias
                    JOIN memory_person_identities AS identity
                      ON identity.id = alias.person_id
                    WHERE alias.chat_name = ? AND alias.alias_name = ?
                      AND alias.status = 'confirmed'
                      AND identity.status IN('active', 'merged')
                    """,
                    (chat_name, alias),
                ).fetchall()
            }
            owners.discard(person_id)
            if owners:
                preflight_conflicts.append(
                    f"alias {alias} belongs to person ids "
                    f"{sorted(owners)} instead of {person_id}"
                )
        for item in sender_sync:
            person_id = int(item.get("person_id") or 0)
            external_id = str(item.get("external_id") or "").strip()
            owners = {
                merge_map.get(int(row["person_id"]), int(row["person_id"]))
                for row in connection.execute(
                    """
                    SELECT alias.person_id
                    FROM memory_person_aliases AS alias
                    JOIN memory_person_identities AS identity
                      ON identity.id = alias.person_id
                    WHERE alias.chat_name = ? AND alias.external_id = ?
                      AND alias.status = 'confirmed'
                      AND identity.status IN('active', 'merged')
                    """,
                    (chat_name, external_id),
                ).fetchall()
            }
            owners.discard(person_id)
            if owners:
                preflight_conflicts.append(
                    f"sender_id {external_id} belongs to person ids "
                    f"{sorted(owners)} instead of {person_id}"
                )
    if preflight_conflicts:
        raise RuntimeError(
            "alias activation preflight failed:\n- "
            + "\n- ".join(preflight_conflicts)
        )
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = workspace / f"production-before-alias-activation-{timestamp}.db"
    _backup_database(database, backup)
    merge_audits = []
    for item in identity_merges:
        source_id = int(item.get("source_person_id") or 0)
        target_id = int(item.get("target_person_id") or 0)
        source = identity_status[source_id]
        if str(source.get("status") or "") == "merged":
            continue
        merge_audits.append(
            store.merge_people(
                chat_name,
                source_id,
                target_id,
                reason=str(
                    item.get("reason")
                    or "别名审计确认改名前后为同一人物"
                ),
            )
        )
    affected_person_ids = sorted(
        {
            int(item.get("person_id") or 0)
            for item in [*confirmed, *sender_sync]
            if int(item.get("person_id") or 0) > 0
        }
    )
    applied_aliases = []
    synced_sender_ids = []
    with store._connection() as connection:
        identities = {
            int(row["id"]): dict(row)
            for row in connection.execute(
                """
                SELECT * FROM memory_person_identities
                WHERE chat_name = ? AND status = 'active'
                """,
                (chat_name,),
            ).fetchall()
        }
        before = store._person_snapshot(
            connection,
            chat_name,
            affected_person_ids,
        )
        for item in sender_sync:
            person_id = int(item.get("person_id") or 0)
            if person_id not in identities:
                raise RuntimeError(
                    f"sender-id target person is not active: {person_id}"
                )
            external_id = str(item.get("external_id") or "").strip()
            owner = connection.execute(
                """
                SELECT person_id FROM memory_person_aliases
                WHERE chat_name = ? AND external_id = ?
                  AND status = 'confirmed'
                """,
                (chat_name, external_id),
            ).fetchone()
            if owner is not None and int(owner["person_id"]) != person_id:
                raise RuntimeError(
                    f"sender_id {external_id} belongs to another person"
                )
            store._upsert_person_alias(
                connection,
                chat_name,
                person_id,
                str(item.get("alias_name") or ""),
                external_id=external_id,
                source="historical_sender_id",
                confidence=float(item.get("confidence") or 1.0),
                status="confirmed",
                observed_at=str(item.get("last_seen_at") or ""),
            )
            synced_sender_ids.append(
                {
                    "person_id": person_id,
                    "canonical_name": str(
                        item.get("canonical_name") or ""
                    ),
                    "external_id": external_id,
                }
            )
        for item in confirmed:
            person_id = int(item.get("person_id") or 0)
            alias = _normalize_alias(item.get("alias"))
            if person_id not in identities:
                raise RuntimeError(
                    f"alias target person is not active: {person_id}"
                )
            conflicts = connection.execute(
                """
                SELECT DISTINCT person_id
                FROM memory_person_aliases
                WHERE chat_name = ? AND alias_name = ?
                  AND status = 'confirmed' AND person_id != ?
                """,
                (chat_name, alias, person_id),
            ).fetchall()
            if conflicts:
                raise RuntimeError(
                    f"alias {alias} already belongs to another person"
                )
            store._upsert_person_alias(
                connection,
                chat_name,
                person_id,
                alias,
                source=(
                    "manual_alias_audit_contextual"
                    if item.get("context_sensitive")
                    else "manual_alias_audit"
                ),
                confidence=1.0,
                status="confirmed",
            )
            applied_aliases.append(
                {
                    "person_id": person_id,
                    "canonical_name": str(
                        item.get("canonical_name") or ""
                    ),
                    "alias": alias,
                    "confidence": float(
                        item.get("verification_confidence") or 0.0
                    ),
                    "evidence_refs": list(
                        item.get("verification_evidence_refs") or []
                    ),
                }
            )
        after = store._person_snapshot(
            connection,
            chat_name,
            affected_person_ids,
        )
        audit_id = store._record_person_audit(
            connection,
            chat_name,
            action="bulk_alias_audit",
            reason=(
                "逐一审计全部活跃人物资料；只确认多条真人证据一致且"
                "跨人物无冲突的群内别名，并同步历史稳定sender_id"
            ),
            affected_person_ids=affected_person_ids,
            before=before,
            after=after,
        )
        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
    if quick_check != "ok":
        raise RuntimeError(f"database quick_check failed: {quick_check}")
    result = {
        "status": "activated",
        "chat_name": chat_name,
        "database": str(database),
        "backup_database": str(backup),
        "identity_merge_count": len(identity_merges),
        "new_identity_merge_count": len(merge_audits),
        "identity_merge_audit_ids": [
            int(item.get("id") or 0) for item in merge_audits
        ],
        "audit_id": audit_id,
        "applied_alias_count": len(applied_aliases),
        "applied_aliases": applied_aliases,
        "synced_sender_id_count": len(synced_sender_ids),
        "synced_sender_ids": synced_sender_ids,
        "quick_check": quick_check,
        "activated_at": _now(),
    }
    _write_json(workspace / "activation.json", result)
    return result


def refresh_merged_profiles(
    *,
    database: Path,
    chat_name: str,
    workspace: Path,
    budget_cny: float,
) -> Dict[str, Any]:
    report_path = workspace / "report.json"
    if not report_path.is_file():
        raise FileNotFoundError(report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    target_ids = sorted(
        {
            int(item.get("target_person_id") or 0)
            for item in report.get("identity_merges") or []
            if int(item.get("target_person_id") or 0) > 0
        }
    )
    budget = DeepSeekBudget(
        workspace / "refresh-cost.json",
        limit_cny=budget_cny,
    )
    service, call_json, context_manager = _build_call_json(
        database=database,
        chat_name=chat_name,
        workspace=workspace / "refresh",
        budget=budget,
    )
    engine = PersonMemoryEngine(
        MemoryStore(database),
        context_manager,
        call_json,
        excluded_person_names=BOT_OR_DERIVED_SENDERS,
    )
    results = []
    try:
        for person_id in target_ids:
            result = engine.consolidate_person(
                chat_name,
                person_id,
                force=True,
            )
            results.append(
                {
                    "person_id": person_id,
                    "refreshed": result is not None,
                    "result": result or {},
                }
            )
            print(
                "[alias-audit] merged profile refresh "
                f"{len(results)}/{len(target_ids)} person_id={person_id} "
                f"refreshed={result is not None}",
                flush=True,
            )
    finally:
        service.close()
    store = MemoryStore(database)
    with store._connection() as connection:
        remaining_pending = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(pending_observation_count), 0)
                FROM memory_person_refresh_state
                WHERE chat_name = ? AND person_id IN({})
                """.format(",".join("?" for _ in target_ids)),
                (chat_name, *target_ids),
            ).fetchone()[0]
        )
        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
    if quick_check != "ok":
        raise RuntimeError(
            f"database quick_check failed after profile refresh: {quick_check}"
        )
    value = {
        "status": "refreshed",
        "chat_name": chat_name,
        "target_count": len(target_ids),
        "refreshed_count": sum(
            bool(item["refreshed"]) for item in results
        ),
        "remaining_pending_observations": remaining_pending,
        "results": results,
        "cost": budget.snapshot(),
        "quick_check": quick_check,
        "completed_at": _now(),
    }
    _write_json(workspace / "refresh.json", value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit and activate evidence-backed person aliases",
    )
    parser.add_argument("--chat", required=True)
    parser.add_argument("--database", default="data/chat_memory.db")
    parser.add_argument(
        "--workspace",
        default="data/person_alias_audits/person-alias-v1",
    )
    parser.add_argument("--candidate-database", default="")
    parser.add_argument("--identity-merge-plan", default="")
    parser.add_argument("--alias-policy", default="")
    parser.add_argument(
        "--expected-profile-count",
        type=int,
        default=64,
    )
    parser.add_argument(
        "--prepare-database",
        default="",
        help="create an isolated DB snapshot with the merge plan, then exit",
    )
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--budget-cny", type=float, default=10.0)
    parser.add_argument("--input-token-budget", type=int, default=22000)
    parser.add_argument(
        "--only-person",
        action="append",
        default=[],
        help="audit one canonical name/person id; repeatable; never activatable",
    )
    parser.add_argument("--activate-only", action="store_true")
    parser.add_argument("--refresh-merged-only", action="store_true")
    args = parser.parse_args()

    database = Path(args.database).resolve()
    workspace = Path(args.workspace).resolve()
    identity_merge_plan = (
        Path(args.identity_merge_plan).resolve()
        if args.identity_merge_plan
        else None
    )
    alias_policy_path = (
        Path(args.alias_policy).resolve()
        if args.alias_policy
        else None
    )
    if args.prepare_database:
        if identity_merge_plan is None:
            raise ValueError(
                "--prepare-database requires --identity-merge-plan"
            )
        result = prepare_audit_database(
            source_database=database,
            output_database=Path(args.prepare_database).resolve(),
            chat_name=args.chat,
            identity_merge_plan=identity_merge_plan,
        )
    elif args.refresh_merged_only:
        result = refresh_merged_profiles(
            database=database,
            chat_name=args.chat,
            workspace=workspace,
            budget_cny=args.budget_cny,
        )
    elif args.activate_only:
        result = activate_alias_audit(
            database=database,
            chat_name=args.chat,
            workspace=workspace,
        )
    else:
        result = build_alias_audit(
            database=database,
            chat_name=args.chat,
            workspace=workspace,
            candidate_database=(
                Path(args.candidate_database).resolve()
                if args.candidate_database
                else None
            ),
            concurrency=args.concurrency,
            budget_cny=args.budget_cny,
            input_token_budget=args.input_token_budget,
            only_people=args.only_person,
            expected_profile_count=args.expected_profile_count,
            identity_merge_plan=identity_merge_plan,
            alias_policy_path=alias_policy_path,
        )
    print(
        json.dumps(
            {
                "status": result.get("status"),
                "chat_name": result.get("chat_name"),
                "profile_count": result.get("profile_count"),
                "audited_profile_count": result.get(
                    "audited_profile_count"
                ),
                "partial": result.get("partial", False),
                "confirmed_alias_count": result.get(
                    "confirmed_alias_count"
                ),
                "review_alias_count": result.get("review_alias_count"),
                "rejected_alias_count": result.get(
                    "rejected_alias_count"
                ),
                "conflict_alias_count": result.get(
                    "conflict_alias_count"
                ),
                "sender_id_sync_count": result.get(
                    "sender_id_sync_count"
                ),
                "identity_merge_count": result.get(
                    "identity_merge_count"
                ),
                "active_profile_count": result.get(
                    "active_profile_count"
                ),
                "output_database": result.get("output_database"),
                "applied_alias_count": result.get(
                    "applied_alias_count"
                ),
                "synced_sender_id_count": result.get(
                    "synced_sender_id_count"
                ),
                "quick_check": result.get("quick_check"),
                "target_count": result.get("target_count"),
                "refreshed_count": result.get("refreshed_count"),
                "remaining_pending_observations": result.get(
                    "remaining_pending_observations"
                ),
                "cost": result.get("cost"),
                "report": str(workspace / "report.json"),
                "activation": str(workspace / "activation.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
