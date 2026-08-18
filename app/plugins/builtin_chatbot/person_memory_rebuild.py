"""Rebuild structured person memory from existing event cards.

The rebuild always runs against an isolated database copy. Activation replaces
only the person-identity/fact tables for one chat and creates a full SQLite
backup first, so event cards and other chats are never rewritten.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Iterable, List, Sequence

from app.plugins.builtin_chatbot.context_manager import ChatContextManager
from app.plugins.builtin_chatbot.memory_service import ChatMemoryService
from app.plugins.builtin_chatbot.memory_store import MemoryStore
from app.services.llm_manager import LLMManager


PERSON_TABLES = (
    "memory_person_identities",
    "memory_person_aliases",
    "memory_person_facts",
    "memory_person_audit",
)


class _NoChatLog:
    def count_messages(self, _chat_name: str) -> int:
        return 0

    def count_log_messages(self, _chat_name: str) -> int:
        return 0


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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


def _resolve_source_path(raw_path: str, project_root: Path) -> Path:
    direct = Path(raw_path)
    if direct.is_file():
        return direct
    windows_path = PureWindowsPath(raw_path)
    lowered = [part.casefold() for part in windows_path.parts]
    if "wxautox4" in lowered:
        relative = Path(*windows_path.parts[lowered.index("wxautox4") + 1 :])
        candidate = project_root / relative
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"historical source does not exist: {raw_path}")


def _registered_source(
    database: Path,
    chat_name: str,
    project_root: Path,
) -> Path:
    connection = sqlite3.connect(database, timeout=30)
    try:
        row = connection.execute(
            """
            SELECT source_path FROM memory_sources
            WHERE chat_name = ? AND source_type = 'jsonl_memory'
            ORDER BY created_at DESC LIMIT 1
            """,
            (chat_name,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError("chat has no registered historical JSONL source")
    return _resolve_source_path(str(row[0] or ""), project_root)


def _observe_historical_identities(
    store: MemoryStore,
    chat_name: str,
    source_path: Path,
) -> int:
    count = 0
    batch: List[Dict[str, Any]] = []
    with source_path.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            batch.append(message)
            if len(batch) >= 2000:
                count += store.observe_message_identities(
                    chat_name,
                    batch,
                    source="historical_sender_id",
                )
                batch = []
    if batch:
        count += store.observe_message_identities(
            chat_name,
            batch,
            source="historical_sender_id",
        )
    return count


def _chunked(values: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _load_historical_messages(
    source_path: Path,
) -> Dict[int, Dict[str, Any]]:
    messages: Dict[int, Dict[str, Any]] = {}
    with source_path.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            cursor = int(message.get("memory_cursor") or 0)
            if cursor > 0:
                messages[cursor] = message
    return messages


def _message_excerpt(
    event: Dict[str, Any],
    *,
    aliases: Sequence[str],
    fact_value: str,
    source_messages: Dict[int, Dict[str, Any]],
    maximum: int = 16,
) -> List[str]:
    start = int(event.get("source_start_cursor") or 0)
    end = int(event.get("source_end_cursor") or 0)
    if start <= 0 or end < start:
        return []
    messages = [
        source_messages[cursor]
        for cursor in range(start, end + 1)
        if cursor in source_messages
    ]
    if not messages:
        return []
    normalized_aliases = {
        str(alias or "").strip().casefold()
        for alias in aliases
        if str(alias or "").strip()
    }
    terms = [
        value
        for value in re.split(
            r"[\s，。！？；：、,.!?;:'\"“”‘’（）()【】\[\]/]+",
            str(fact_value or ""),
        )
        if 2 <= len(value) <= 20
    ][:8]
    hits: set[int] = set()
    for index, message in enumerate(messages):
        sender = str(message.get("sender") or "").strip().casefold()
        content = str(message.get("content") or "")
        folded_content = content.casefold()
        if sender in normalized_aliases or any(
            alias and alias in folded_content
            for alias in normalized_aliases
        ):
            hits.add(index)
            continue
        if any(term in content for term in terms):
            hits.add(index)
    if len(messages) <= maximum:
        selected = list(range(len(messages)))
    else:
        selected_set: set[int] = set()
        for hit in sorted(hits):
            selected_set.update(
                range(max(0, hit - 2), min(len(messages), hit + 3))
            )
        selected = sorted(selected_set)
        if not selected:
            selected = list(range(min(maximum, len(messages))))
        if len(selected) > maximum:
            # Retain evidence around both the first and last matching turns.
            head = maximum // 2
            selected = selected[:head] + selected[-(maximum - head) :]
    result = []
    for index in selected:
        message = messages[index]
        content = re.sub(
            r"\s+",
            " ",
            str(message.get("content") or "").strip(),
        )[:360]
        result.append(
            f"[{message.get('time') or ''}] "
            f"{message.get('sender') or '?'}：{content}"
        )
    return result


def _person_prompt(
    chat_name: str,
    events: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    event_text = "\n\n".join(
        ChatMemoryService._format_event_for_stage(event)
        for event in events
    )
    allowed_ids = "、".join(f"#{int(event['id'])}" for event in events)
    return [
        {
            "role": "system",
            "content": (
                "你是群聊人物事实档案员。只从给定事件提取对未来对话有帮助、可追溯的"
                "人物原子事实，不写人物简介，不总结聊天参与记录。禁止把‘参与某话题、"
                "询问某问题、转发新闻、一次玩笑、一次下注或一次随口评价’当成人物长期"
                "属性。工作、单位、地点、家庭、关系、健康、设备、经历、偏好、技能、"
                "习惯和计划可以保留，但必须保留当时的时间语境。第三方说法只能标记为"
                "uncertain；rumor_or_joke不得进入人物事实。相对年龄或‘最近’等表达不"
                "得推算成永久事实。aliases只有在事件明确证明两个名字指向同一人时才"
                "输出。每条事实必须引用本批次真实事件ID。只输出JSON对象。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"群聊：{chat_name}\n"
                f"允许引用的事件：{allowed_ids}\n\n"
                f"{event_text}\n\n"
                "严格输出："
                '{"people_updates":[{"name":"事件中的人物姓名","aliases":[],'
                '"confidence":0.0,"facts":[{"field":"identity|group_role|occupation|'
                'employer|education|location|family|relationship|health|preference|'
                'interest|skill|asset|experience|habit|plan|current_status|other",'
                '"value":"单一事实","status":"current|historical|planned|uncertain|disputed",'
                '"confidence":0.0,"valid_from":"ISO日期时间或空字符串",'
                '"valid_to":"ISO日期时间或空字符串","observed_at":"证据事件时间",'
                '"temporal_note":"时间或归因说明","source_event_ids":[1],'
                '"replaces_fact_ids":[]}]}]}'
            ),
        },
    ]


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


_PERSON_NOISE_PATTERNS = (
    re.compile(r"参与.{0,20}讨论"),
    re.compile(r"询问"),
    re.compile(r"(?:分享|转发).{0,30}(?:新闻|链接|消息|传闻|论文|视频|截图)"),
    re.compile(r"(?:发|抢|领取)红包"),
    re.compile(r"(?:世界杯|比赛|电竞).{0,30}(?:下注|投注|买了|押注)"),
    re.compile(r"(?:下注|投注|押注).{0,30}(?:世界杯|比赛|战队|球队)"),
    re.compile(r"与.{1,30}互动(?:频繁)?"),
    re.compile(r"(?:收到|向|给).{0,24}(?:转账|下注|赌资)"),
    re.compile(r"(?:参与|购买|决定|投).{0,24}(?:赌球|投注|独赢|波胆)"),
)


def _is_obvious_person_noise(fact: Dict[str, Any]) -> bool:
    field_name = str(fact.get("field_name") or "")
    value = str(fact.get("value") or "")
    if field_name == "other":
        return True
    if re.search(r"(?:推测|疑似|可能是.+(?:工作|职业|身份))", value):
        return True
    if field_name == "preference":
        if re.search(r"关注.+(?:提及|评论)", value):
            return True
        if re.search(r"(?:认为|推荐|建议|批评|对.+持.+态度)", value) and not re.search(
            r"(?:喜欢|偏好|最爱|长期|经常|球迷)",
            value,
        ):
            return True
        if re.search(r"(?:下注|投注|独赢|波胆|比分)", value):
            return True
    if field_name == "relationship" and re.search(
        r"(?:互动|调侃|辩论|被.+称为.+(?:父亲|老豆)|自称.+(?:父亲|老豆)|"
        r"委托投注|作为庄家|与.+有关联|认识.+老板|有亲戚|金钱纠纷|发烧)",
        value,
    ):
        return True
    if field_name == "experience" and re.search(
        r"(?:赌球|投注|下注|转账|彩票|体彩店|波胆|赔率|咨询|分享|"
        r"评论|提醒|回答|教导|列举)",
        value,
    ):
        return True
    if field_name == "plan" and re.search(
        r"(?:赌球|投注|下注|独赢|波胆|赔率)",
        value,
    ):
        return True
    if field_name == "asset" and re.search(
        r"(?:赌博|博彩|bet).*(?:账号|账户)",
        value,
        flags=re.IGNORECASE,
    ):
        return True
    if field_name == "family" and re.search(r"(?:不让他|认为奶粉)", value):
        return True
    if field_name == "skill" and value.startswith("知道利用"):
        return True
    if field_name == "group_role" and "车主群成员" in value:
        return True
    if field_name == "current_status" and re.search(
        r"(?:永不超生|被封禁)",
        value,
    ):
        return True
    return any(pattern.search(value) for pattern in _PERSON_NOISE_PATTERNS)


def _balanced_fact_pool(
    facts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Bound long histories without losing recent facts from any field."""

    quotas = {
        "identity": 3,
        "group_role": 3,
        "occupation": 7,
        "employer": 4,
        "education": 3,
        "location": 5,
        "family": 8,
        "relationship": 5,
        "health": 5,
        "preference": 7,
        "interest": 4,
        "skill": 5,
        "asset": 8,
        "experience": 7,
        "habit": 3,
        "plan": 5,
        "current_status": 5,
    }
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for fact in facts:
        grouped.setdefault(str(fact.get("field_name") or ""), []).append(fact)
    selected: List[Dict[str, Any]] = []
    for field_name, values in grouped.items():
        values.sort(
            key=lambda fact: (
                str(
                    fact.get("last_seen_at")
                    or fact.get("observed_at")
                    or fact.get("valid_from")
                    or ""
                ),
                float(fact.get("confidence") or 0.0),
            ),
            reverse=True,
        )
        selected.extend(values[: quotas.get(field_name, 3)])
    selected.sort(
        key=lambda fact: str(
            fact.get("last_seen_at")
            or fact.get("observed_at")
            or fact.get("valid_from")
            or ""
        ),
        reverse=True,
    )
    return selected


def _consolidation_prompt(
    person: Dict[str, Any],
    facts: List[Dict[str, Any]],
    *,
    maximum: int,
) -> List[Dict[str, str]]:
    fact_text = json.dumps(
        [
            {
                "fact_id": int(fact.get("id") or 0),
                "field": fact.get("field_name"),
                "value": fact.get("value"),
                "status": fact.get("status"),
                "confidence": fact.get("confidence"),
                "valid_from": fact.get("valid_from"),
                "valid_to": fact.get("valid_to"),
                "observed_at": fact.get("observed_at"),
                "first_seen_at": fact.get("first_seen_at"),
                "last_seen_at": fact.get("last_seen_at"),
                "source_event_ids": fact.get("source_event_ids") or [],
            }
            for fact in facts
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return [
        {
            "role": "system",
            "content": (
                "你是人物档案质量审核员。给定同一人在多年群聊中抽出的候选事实，只保留"
                "未来对话中真正有帮助的高价值资料。优先保留身份、职业/单位变化、教育、"
                "长期地点、家庭和明确关系、重要健康情况、稳定偏好/兴趣/技能/习惯、重要"
                "设备资产、人生或职业里程碑及仍有效的计划。删除普通聊天参与、问过的问题、"
                "新闻链接、对一次事件的评论、一次下注/红包/购物、重复近义事实和没有长期"
                "意义的流水账。第三方传闻原则上删除；若对理解人物关系确有必要才保留并标"
                "uncertain。旧状态必须historical，过期计划必须historical；只有最近仍有"
                "证据的信息才标current/planned。不得新增或改写事实，只能返回已有fact_id。"
                "只输出合法JSON对象，不要代码块。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"人物：{person.get('person_name') or ''}\n"
                f"最多保留 {maximum} 条；不要求凑满，宁缺毋滥。\n"
                "同类额度建议：身份/职业/教育/地点合计不超过10，家庭/关系不超过10，"
                "健康不超过5，偏好/兴趣/技能/习惯合计不超过10，资产不超过5，"
                "重要经历不超过10，当前状态/计划不超过5。\n\n"
                f"候选事实：{fact_text}\n\n"
                "严格输出："
                '{"keep_fact_ids":[1],'
                '"status_updates":[{"fact_id":1,"status":"current|historical|'
                'planned|uncertain|disputed"}],'
                '"reason":"一句话说明筛选原则"}'
            ),
        },
    ]


def _evidence_review_prompt(
    person: Dict[str, Any],
    facts: List[Dict[str, Any]],
    evidence: Dict[int, List[Dict[str, Any]]],
    *,
    maximum: int,
    reference_time: str,
) -> List[Dict[str, str]]:
    candidates = []
    for fact in facts:
        fact_id = int(fact.get("id") or 0)
        candidates.append(
            {
                "fact_id": fact_id,
                "field": fact.get("field_name"),
                "value": fact.get("value"),
                "status": fact.get("status"),
                "confidence": fact.get("confidence"),
                "valid_from": fact.get("valid_from"),
                "valid_to": fact.get("valid_to"),
                "observed_at": fact.get("observed_at"),
                "source_event_ids": fact.get("source_event_ids") or [],
                "source_evidence": evidence.get(fact_id) or [],
            }
        )
    return [
        {
            "role": "system",
            "content": (
                "你是人物档案的最终证据审计员。事件摘要只用于定位，原始消息才是主要"
                "证据。逐条判断候选事实的主语、说话人、真假语气和时效，只能保留原始"
                "消息直接支持或多轮明确印证的事实。严禁把引用/回复中的他人经历归给"
                "发言人；严禁把玩笑、反讽、角色扮演、假设、群内互称父子、AI机器人"
                "生成的答案当成现实事实；严禁把普通互动当成关系、把回答过某问题当成"
                "技能、把一次评论/推荐/投注当成稳定偏好。家庭关系、职业单位、教育、"
                "长期地点、明确身份、重要健康/资产/经历可保留，但必须确有证据。"
                "同等可信时优先保留较新的事实，尤其不要用多年前的设备、旅行、短期"
                "计划挤掉近年的职业、家庭、教育、长期住址和仍持有的重要资产。"
                "current只表示在审计基准时间仍大概率有效；当时状态、旧设备、旧病情、"
                "已过日期的计划改为historical，无法确定则uncertain。不得新增、改写或"
                "合并事实，只能返回已有fact_id。宁缺毋滥，只输出合法JSON对象。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"人物：{person.get('person_name') or ''}\n"
                f"审计基准时间：{reference_time}\n"
                f"硬上限：{maximum} 条；理想为6-10条，不要求凑数，0条也允许。\n"
                "同一字段有时间演变时保留关键节点，最近状态可current，旧节点应"
                "historical。股票持仓、金额、临时位置和短期计划尤其强调时点。\n\n"
                "候选与原始证据："
                + json.dumps(
                    candidates,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n\n严格输出："
                '{"keep_fact_ids":[1],'
                '"status_updates":[{"fact_id":1,"status":"current|historical|'
                'planned|uncertain|disputed"}],'
                '"reason":"一句话说明主要删除了什么"}'
            ),
        },
    ]


def _fallback_fact_selection(
    facts: List[Dict[str, Any]],
    maximum: int,
) -> List[int]:
    priority = {
        "identity": 0,
        "group_role": 1,
        "employer": 2,
        "occupation": 3,
        "education": 4,
        "family": 5,
        "relationship": 6,
        "location": 7,
        "health": 8,
        "skill": 9,
        "habit": 10,
        "interest": 11,
        "preference": 12,
        "asset": 13,
        "current_status": 14,
        "plan": 15,
        "experience": 16,
    }
    ranked = sorted(
        facts,
        key=lambda fact: (
            str(
                fact.get("last_seen_at")
                or fact.get("observed_at")
                or ""
            ),
            float(fact.get("confidence") or 0.0),
        ),
        reverse=True,
    )
    ranked.sort(
        key=lambda fact: priority.get(
            str(fact.get("field_name") or ""),
            99,
        )
    )
    return [int(fact["id"]) for fact in ranked[:maximum]]


def _consolidate_person_facts(
    *,
    store: MemoryStore,
    service: ChatMemoryService,
    chat_name: str,
    workspace: Path,
    concurrency: int,
) -> Dict[str, int]:
    people = store.list_people(chat_name)
    result_directory = workspace / "consolidation"
    result_directory.mkdir(exist_ok=True)

    def consolidate(person: Dict[str, Any]) -> tuple[int, List[int], Dict[int, str]]:
        person_id = int(person.get("person_id") or 0)
        all_facts = [
            fact
            for fact in person.get("facts") or []
            if not _is_obvious_person_noise(fact)
        ]
        all_facts = _balanced_fact_pool(all_facts)
        maximum = 28 if len(all_facts) > 60 else 24 if len(all_facts) > 30 else 18
        maximum = min(maximum, len(all_facts))
        input_ids = [int(fact["id"]) for fact in all_facts]
        destination = result_directory / f"person-{person_id:05d}.json"
        record: Dict[str, Any] = {}
        if destination.is_file():
            record = json.loads(destination.read_text(encoding="utf-8"))
            if (
                record.get("policy_version") != 2
                or record.get("input_fact_ids") != input_ids
            ):
                record = {}
        if not record:
            # This pass only bounds the candidate pool by field and recency.
            # Asking an LLM to compress hundreds of facts here caused it to
            # fill the quota with older trivia before seeing raw evidence.
            # The next pass performs the actual semantic selection against
            # original messages.
            keep_ids = input_ids
            status_updates: Dict[int, str] = {}
            reason = "按字段与最近证据生成候选池，交由原始消息审计决定最终保留"
            record = {
                "policy_version": 2,
                "person_id": person_id,
                "person_name": person.get("person_name") or "",
                "input_fact_ids": input_ids,
                "keep_fact_ids": keep_ids,
                "status_updates": [
                    {"fact_id": fact_id, "status": status}
                    for fact_id, status in status_updates.items()
                ],
                "reason": reason,
                "completed_at": _now(),
            }
            _write_json(destination, record)
        return (
            person_id,
            [int(value) for value in record.get("keep_fact_ids") or []],
            {
                int(item.get("fact_id") or 0): str(item.get("status") or "")
                for item in record.get("status_updates") or []
                if isinstance(item, dict)
            },
        )

    selections = []
    with ThreadPoolExecutor(
        max_workers=max(1, min(24, int(concurrency))),
        thread_name_prefix="person-consolidate",
    ) as executor:
        futures = [
            executor.submit(consolidate, person)
            for person in people
            if person.get("facts")
        ]
        for future in as_completed(futures):
            selections.append(future.result())
    kept = 0
    pruned = 0
    status_updates = 0
    for person_id, keep_ids, updates in selections:
        result = store.apply_person_fact_selection(
            chat_name,
            person_id,
            keep_fact_ids=keep_ids,
            status_updates=updates,
        )
        kept += result["kept"]
        pruned += result["pruned"]
        status_updates += result["status_updates"]
    return {
        "people": len(selections),
        "kept": kept,
        "pruned": pruned,
        "status_updates": status_updates,
    }


def _fallback_evidence_selection(
    facts: List[Dict[str, Any]],
    maximum: int,
) -> List[int]:
    durable_fields = {
        "identity",
        "group_role",
        "occupation",
        "employer",
        "education",
        "location",
        "family",
        "health",
    }
    values = [
        fact
        for fact in facts
        if str(fact.get("field_name") or "") in durable_fields
        and float(fact.get("confidence") or 0.0) >= 0.8
    ]
    return _fallback_fact_selection(values, min(maximum, 10))


def _enforce_final_fact_quotas(
    facts: List[Dict[str, Any]],
    keep_ids: Sequence[int],
    maximum: int,
) -> List[int]:
    quotas = {
        "identity": 2,
        "group_role": 2,
        "occupation": 3,
        "employer": 2,
        "education": 2,
        "location": 2,
        "family": 4,
        "relationship": 3,
        "health": 3,
        "preference": 2,
        "interest": 2,
        "skill": 3,
        "asset": 3,
        "experience": 3,
        "habit": 2,
        "plan": 2,
        "current_status": 2,
    }
    fact_map = {int(fact.get("id") or 0): fact for fact in facts}
    is_bot_profile = any(
        "AI机器人" in str(fact.get("value") or "")
        for fact in facts
        if str(fact.get("field_name") or "") == "identity"
    )
    result: List[int] = []
    counts: Dict[str, int] = {}
    for fact_id in keep_ids:
        fact = fact_map.get(int(fact_id))
        if fact is None or _is_obvious_person_noise(fact):
            continue
        field_name = str(fact.get("field_name") or "")
        value = str(fact.get("value") or "")
        if is_bot_profile and field_name not in {
            "identity",
            "group_role",
            "skill",
        }:
            continue
        if is_bot_profile and field_name == "identity" and "非AI" in value:
            continue
        if counts.get(field_name, 0) >= quotas.get(field_name, 1):
            continue
        result.append(int(fact_id))
        counts[field_name] = counts.get(field_name, 0) + 1
        if len(result) >= maximum:
            break
    return result


def _review_person_facts_against_source(
    *,
    store: MemoryStore,
    service: ChatMemoryService,
    chat_name: str,
    workspace: Path,
    source_path: Path,
    events: Sequence[Dict[str, Any]],
    reference_time: str,
    concurrency: int,
) -> Dict[str, int]:
    people = store.list_people(chat_name)
    source_messages = _load_historical_messages(source_path)
    event_map = {
        int(event.get("id") or 0): event
        for event in events
        if int(event.get("id") or 0) > 0
    }
    result_directory = workspace / "evidence-review-v2"
    result_directory.mkdir(exist_ok=True)

    def review(
        person: Dict[str, Any],
    ) -> tuple[int, List[int], Dict[int, str]]:
        person_id = int(person.get("person_id") or 0)
        facts = [
            fact
            for fact in person.get("facts") or []
            if fact.get("field_name") != "legacy_summary"
            and not _is_obvious_person_noise(fact)
        ]
        input_ids = [int(fact.get("id") or 0) for fact in facts]
        aliases = [
            str(person.get("person_name") or ""),
            *[
                str(alias.get("alias_name") or "")
                for alias in person.get("aliases") or []
            ],
        ]
        evidence: Dict[int, List[Dict[str, Any]]] = {}
        for fact in facts:
            fact_id = int(fact.get("id") or 0)
            source_ids = [
                int(value)
                for value in fact.get("source_event_ids") or []
                if int(value) in event_map
            ][-3:]
            snippets = []
            for event_id in source_ids:
                event = event_map[event_id]
                card = (
                    event.get("card")
                    if isinstance(event.get("card"), dict)
                    else {}
                )
                snippets.append(
                    {
                        "event_id": event_id,
                        "event_type": card.get("event_type") or "",
                        "certainty": card.get("certainty") or "",
                        "event_time": event.get("end_time")
                        or event.get("start_time")
                        or "",
                        "raw_messages": _message_excerpt(
                            event,
                            aliases=aliases,
                            fact_value=str(fact.get("value") or ""),
                            source_messages=source_messages,
                        ),
                    }
                )
            evidence[fact_id] = snippets
        facts = [
            fact
            for fact in facts
            if any(
                str(item.get("event_type") or "") not in {"joke"}
                and item.get("raw_messages")
                for item in evidence.get(int(fact.get("id") or 0)) or []
            )
        ]
        input_ids = [int(fact.get("id") or 0) for fact in facts]
        maximum = min(12, len(facts))
        destination = result_directory / f"person-{person_id:05d}.json"
        record: Dict[str, Any] = {}
        if destination.is_file():
            record = json.loads(destination.read_text(encoding="utf-8"))
            if (
                record.get("policy_version") != 4
                or record.get("input_fact_ids") != input_ids
            ):
                record = {}
        if not record:
            if not facts:
                keep_ids: List[int] = []
                status_updates: Dict[int, str] = {}
                reason = "没有结构化候选事实"
            else:
                try:
                    payload = service._call_memory_json(
                        call_type="memory_stage",
                        messages=_evidence_review_prompt(
                            person,
                            facts,
                            evidence,
                            maximum=maximum,
                            reference_time=reference_time,
                        ),
                        schema_hint=(
                            "JSON根对象必须包含 keep_fact_ids、"
                            "status_updates 和 reason"
                        ),
                        chat_name=chat_name,
                    )
                    allowed_ids = set(input_ids)
                    keep_ids = list(
                        dict.fromkeys(
                            int(value)
                            for value in payload.get("keep_fact_ids") or []
                            if str(value or "").isdigit()
                            and int(value) in allowed_ids
                        )
                    )
                    keep_ids = _enforce_final_fact_quotas(
                        facts,
                        keep_ids,
                        maximum,
                    )
                    status_updates = {
                        int(item.get("fact_id") or 0): str(
                            item.get("status") or ""
                        )
                        for item in payload.get("status_updates") or []
                        if isinstance(item, dict)
                        and int(item.get("fact_id") or 0) in set(keep_ids)
                    }
                    reason = str(payload.get("reason") or "")
                except Exception as exc:
                    keep_ids = _fallback_evidence_selection(facts, maximum)
                    status_updates = {}
                    reason = f"证据审核失败，使用保守回退：{exc}"
            record = {
                "policy_version": 4,
                "person_id": person_id,
                "person_name": person.get("person_name") or "",
                "input_fact_ids": input_ids,
                "keep_fact_ids": keep_ids,
                "status_updates": [
                    {"fact_id": fact_id, "status": status}
                    for fact_id, status in status_updates.items()
                ],
                "reason": reason,
                "completed_at": _now(),
            }
            _write_json(destination, record)
        return (
            person_id,
            [int(value) for value in record.get("keep_fact_ids") or []],
            {
                int(item.get("fact_id") or 0): str(item.get("status") or "")
                for item in record.get("status_updates") or []
                if isinstance(item, dict)
            },
        )

    selections = []
    with ThreadPoolExecutor(
        max_workers=max(1, min(16, int(concurrency))),
        thread_name_prefix="person-evidence-review",
    ) as executor:
        futures = [
            executor.submit(review, person)
            for person in people
            if person.get("facts")
        ]
        for future in as_completed(futures):
            selections.append(future.result())
    kept = 0
    pruned = 0
    status_updates = 0
    for person_id, keep_ids, updates in selections:
        result = store.apply_person_fact_selection(
            chat_name,
            person_id,
            keep_fact_ids=keep_ids,
            status_updates=updates,
            reason="原始消息证据审核未通过",
        )
        kept += result["kept"]
        pruned += result["pruned"]
        status_updates += result["status_updates"]
    return {
        "people": len(selections),
        "kept": kept,
        "pruned": pruned,
        "status_updates": status_updates,
    }


def rebuild_candidate(
    *,
    production_database: Path,
    chat_name: str,
    workspace: Path,
    project_root: Path,
    source_path: Path | None = None,
    concurrency: int = 16,
    batch_events: int = 60,
) -> Dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    candidate = workspace / "candidate.db"
    if not candidate.exists():
        _backup_database(production_database, candidate)
    store = MemoryStore(candidate)
    historical_source = source_path or _registered_source(
        candidate,
        chat_name,
        project_root,
    )
    observed_messages = _observe_historical_identities(
        store,
        chat_name,
        historical_source,
    )
    events = store.list_events(chat_name, active_only=True)
    batches = list(_chunked(events, max(10, min(120, int(batch_events)))))
    result_directory = workspace / "batches"
    result_directory.mkdir(exist_ok=True)
    manager = LLMManager(
        config_dir="data",
        telemetry_dir=workspace / "telemetry",
    )
    model = (
        manager.config.get("models", {}).get("deepseek")
        if isinstance(manager.config, dict)
        else None
    )
    if isinstance(model, dict):
        model["temperature"] = 0.1
        model["extra_body"] = {"thinking": {"type": "disabled"}}
    stage_mapping = (
        manager.config.get("plugin_mappings", {})
        .get("builtin_chatbot", {})
        .get("memory_stage", {})
    )
    if isinstance(stage_mapping, dict):
        override = stage_mapping.setdefault("override_params", {})
        override["temperature"] = 0.1
        override["max_tokens"] = 8000
        override["timeout"] = 180
    service = ChatMemoryService(
        _NoChatLog(),
        ChatContextManager(),
        store=store,
        llm_manager=manager,
        llm_history_chat_name=chat_name,
        llm_history_mode="minimal",
    )
    output_lock = threading.Lock()

    def call_event_batch(
        event_batch: List[Dict[str, Any]],
        *,
        depth: int = 0,
    ) -> tuple[List[Dict[str, Any]], int, int]:
        try:
            payload = service._call_memory_json(
                call_type="memory_stage",
                messages=_person_prompt(chat_name, event_batch),
                schema_hint='根对象必须是 {"people_updates":[...]}',
                chat_name=chat_name,
            )
            updates = payload.get("people_updates")
            if not isinstance(updates, list):
                raise ValueError("people_updates is not an array")
            return updates, 1, depth
        except Exception:
            if len(event_batch) <= 15:
                raise
            middle = len(event_batch) // 2
            left, left_calls, left_depth = call_event_batch(
                event_batch[:middle],
                depth=depth + 1,
            )
            right, right_calls, right_depth = call_event_batch(
                event_batch[middle:],
                depth=depth + 1,
            )
            return (
                [*left, *right],
                1 + left_calls + right_calls,
                max(left_depth, right_depth),
            )

    def extract(index: int, event_batch: List[Dict[str, Any]]) -> Path:
        destination = result_directory / f"batch-{index:05d}.json"
        if destination.is_file():
            return destination
        people_updates, call_count, split_depth = call_event_batch(
            event_batch,
        )
        record = {
            "batch_index": index,
            "event_ids": [int(event["id"]) for event in event_batch],
            "event_start_time": event_batch[0].get("start_time") or "",
            "event_end_time": event_batch[-1].get("end_time") or "",
            "payload": {"people_updates": people_updates},
            "model_call_count": call_count,
            "split_depth": split_depth,
            "completed_at": _now(),
        }
        with output_lock:
            _write_json(destination, record)
        return destination

    completed = 0
    try:
        with ThreadPoolExecutor(
            max_workers=max(1, min(64, int(concurrency))),
            thread_name_prefix="person-rebuild",
        ) as executor:
            futures = {
                executor.submit(extract, index, event_batch): index
                for index, event_batch in enumerate(batches, start=1)
            }
            for future in as_completed(futures):
                future.result()
                completed += 1
                if completed % 10 == 0 or completed == len(batches):
                    print(
                        f"person rebuild extraction: {completed}/{len(batches)}",
                        flush=True,
                    )
    finally:
        service.close()

    cleared = store.clear_generated_person_facts(chat_name)
    people_updates = 0
    fact_updates = 0
    for result_path in sorted(result_directory.glob("batch-*.json")):
        record = json.loads(result_path.read_text(encoding="utf-8"))
        event_ids = {
            int(value)
            for value in record.get("event_ids") or []
            if int(value) > 0
        }
        normalized = ChatMemoryService._normalize_people_updates(
            (record.get("payload") or {}).get("people_updates"),
            available_event_ids=event_ids,
        )
        normalized = store.canonicalize_observed_people(
            chat_name,
            normalized,
        )
        for person in normalized:
            person["observed_at"] = str(record.get("event_end_time") or "")
            fact_updates += len(person.get("facts") or [])
        if normalized:
            store.upsert_people(
                chat_name,
                normalized,
                source_event_id=max(event_ids),
            )
            people_updates += len(normalized)
    reference_time = max(
        (
            str(event.get("end_time") or event.get("start_time") or "")
            for event in events
        ),
        default="",
    )
    consolidation_service = ChatMemoryService(
        _NoChatLog(),
        ChatContextManager(),
        store=store,
        llm_manager=manager,
        llm_history_chat_name=chat_name,
        llm_history_mode="minimal",
    )
    try:
        consolidation = _consolidate_person_facts(
            store=store,
            service=consolidation_service,
            chat_name=chat_name,
            workspace=workspace,
            concurrency=min(16, max(1, int(concurrency))),
        )
        evidence_review = _review_person_facts_against_source(
            store=store,
            service=consolidation_service,
            chat_name=chat_name,
            workspace=workspace,
            source_path=historical_source,
            events=events,
            reference_time=reference_time,
            concurrency=min(16, max(1, int(concurrency))),
        )
    finally:
        consolidation_service.close()
    temporal_changes = store.finalize_person_fact_temporality(
        chat_name,
        reference_time=reference_time,
    )
    retired_legacy = store.retire_legacy_person_profiles(chat_name)
    people = store.list_people(chat_name)
    report = {
        "status": "rebuilt",
        "chat_name": chat_name,
        "candidate_database": str(candidate),
        "source_path": str(historical_source),
        "events": len(events),
        "batches": len(batches),
        "identity_messages_observed": observed_messages,
        "cleared_generated_facts": cleared,
        "people_updates": people_updates,
        "facts_extracted": fact_updates,
        "consolidation": consolidation,
        "evidence_review": evidence_review,
        "temporal_status_changes": temporal_changes,
        "retired_legacy": retired_legacy,
        "people_count": len(people),
        "structured_people_count": sum(bool(person.get("facts")) for person in people),
        "structured_fact_count": sum(len(person.get("facts") or []) for person in people),
        "completed_at": _now(),
    }
    _write_json(workspace / "report.json", report)
    return report


def activate_candidate(
    *,
    production_database: Path,
    candidate_database: Path,
    chat_name: str,
    workspace: Path,
) -> Dict[str, Any]:
    MemoryStore(production_database)
    MemoryStore(candidate_database)
    backup = workspace / (
        "production-before-person-activation-"
        + datetime.now().strftime("%Y%m%d-%H%M%S")
        + ".db"
    )
    _backup_database(production_database, backup)
    connection = sqlite3.connect(production_database, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("ATTACH DATABASE ? AS candidate", (str(candidate_database),))
        candidate_rows = {
            table: [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT * FROM candidate.{table}
                    WHERE chat_name = ? ORDER BY id
                    """,
                    (chat_name,),
                ).fetchall()
            ]
            for table in PERSON_TABLES
        }
        connection.execute("BEGIN IMMEDIATE")
        for table in reversed(PERSON_TABLES):
            connection.execute(
                f"DELETE FROM main.{table} WHERE chat_name = ?",
                (chat_name,),
            )
        # The structured candidate is authoritative. Keeping old prose rows
        # would cause removed legacy-only identities to be migrated back on
        # the next process start.
        connection.execute(
            """
            DELETE FROM main.memory_people
            WHERE chat_name = ?
            """,
            (chat_name,),
        )

        id_maps: Dict[str, Dict[int, int]] = {}
        for table in PERSON_TABLES:
            next_id = int(
                connection.execute(
                    f"SELECT COALESCE(MAX(id), 0) FROM main.{table}"
                ).fetchone()[0]
            )
            id_maps[table] = {
                int(row["id"]): next_id + index
                for index, row in enumerate(
                    candidate_rows[table],
                    start=1,
                )
            }

        identity_ids = id_maps["memory_person_identities"]
        alias_ids = id_maps["memory_person_aliases"]
        fact_ids = id_maps["memory_person_facts"]
        audit_ids = id_maps["memory_person_audit"]

        def remap_snapshot(raw_value: Any) -> str:
            try:
                snapshot = json.loads(str(raw_value or "{}"))
            except (TypeError, ValueError):
                return "{}"
            if not isinstance(snapshot, dict):
                return "{}"
            for identity in snapshot.get("identities") or []:
                if not isinstance(identity, dict):
                    continue
                identity["id"] = identity_ids.get(
                    int(identity.get("id") or 0),
                    int(identity.get("id") or 0),
                )
                merged = int(identity.get("merged_into_person_id") or 0)
                identity["merged_into_person_id"] = identity_ids.get(
                    merged,
                    merged,
                )
            for alias in snapshot.get("aliases") or []:
                if not isinstance(alias, dict):
                    continue
                alias["id"] = alias_ids.get(
                    int(alias.get("id") or 0),
                    int(alias.get("id") or 0),
                )
                person_id = int(alias.get("person_id") or 0)
                alias["person_id"] = identity_ids.get(
                    person_id,
                    person_id,
                )
            for fact in snapshot.get("facts") or []:
                if not isinstance(fact, dict):
                    continue
                fact["id"] = fact_ids.get(
                    int(fact.get("id") or 0),
                    int(fact.get("id") or 0),
                )
                person_id = int(fact.get("person_id") or 0)
                fact["person_id"] = identity_ids.get(
                    person_id,
                    person_id,
                )
                superseded = int(
                    fact.get("superseded_by_fact_id") or 0
                )
                fact["superseded_by_fact_id"] = fact_ids.get(
                    superseded,
                    superseded,
                )
            return json.dumps(
                snapshot,
                ensure_ascii=False,
                separators=(",", ":"),
            )

        remapped_rows: Dict[str, List[Dict[str, Any]]] = {
            table: [] for table in PERSON_TABLES
        }
        for source in candidate_rows["memory_person_identities"]:
            row = dict(source)
            row["id"] = identity_ids[int(source["id"])]
            merged = int(source.get("merged_into_person_id") or 0)
            row["merged_into_person_id"] = identity_ids.get(merged, merged)
            remapped_rows["memory_person_identities"].append(row)
        for source in candidate_rows["memory_person_aliases"]:
            row = dict(source)
            row["id"] = alias_ids[int(source["id"])]
            row["person_id"] = identity_ids[int(source["person_id"])]
            remapped_rows["memory_person_aliases"].append(row)
        for source in candidate_rows["memory_person_facts"]:
            row = dict(source)
            row["id"] = fact_ids[int(source["id"])]
            row["person_id"] = identity_ids[int(source["person_id"])]
            superseded = int(source.get("superseded_by_fact_id") or 0)
            row["superseded_by_fact_id"] = fact_ids.get(
                superseded,
                superseded,
            )
            remapped_rows["memory_person_facts"].append(row)
        for source in candidate_rows["memory_person_audit"]:
            row = dict(source)
            row["id"] = audit_ids[int(source["id"])]
            try:
                affected_values = json.loads(
                    str(source.get("affected_person_ids_json") or "[]")
                )
            except (TypeError, ValueError):
                affected_values = []
            affected = [
                identity_ids.get(int(value), int(value))
                for value in affected_values
                if str(value or "").isdigit()
            ]
            row["affected_person_ids_json"] = json.dumps(
                affected,
                separators=(",", ":"),
            )
            row["before_json"] = remap_snapshot(source.get("before_json"))
            row["after_json"] = remap_snapshot(source.get("after_json"))
            remapped_rows["memory_person_audit"].append(row)

        for table in PERSON_TABLES:
            for row in remapped_rows[table]:
                columns = list(row)
                connection.execute(
                    f"""
                    INSERT INTO main.{table}({",".join(columns)})
                    VALUES({",".join("?" for _ in columns)})
                    """,
                    [row[column] for column in columns],
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        try:
            connection.execute("DETACH DATABASE candidate")
        except sqlite3.Error:
            pass
        connection.close()
    store = MemoryStore(production_database)
    result = {
        "status": "activated",
        "chat_name": chat_name,
        "backup_database": str(backup),
        "people_count": store.count_people(chat_name),
        "fact_count": sum(
            len(person.get("facts") or [])
            for person in store.list_people(chat_name)
        ),
        "activated_at": _now(),
    }
    _write_json(workspace / "activation.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-name", required=True)
    parser.add_argument(
        "--database",
        default="data/chat_memory.db",
        type=Path,
    )
    parser.add_argument(
        "--workspace",
        required=True,
        type=Path,
    )
    parser.add_argument("--source-jsonl", type=Path)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--batch-events", type=int, default=60)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument(
        "--activate-only",
        action="store_true",
        help="activate an already reviewed candidate without rebuilding it",
    )
    args = parser.parse_args()
    project_root = Path.cwd()
    if args.activate_only:
        candidate = args.workspace / "candidate.db"
        if not candidate.is_file():
            raise FileNotFoundError(f"candidate does not exist: {candidate}")
    else:
        report = rebuild_candidate(
            production_database=args.database,
            chat_name=args.chat_name,
            workspace=args.workspace,
            project_root=project_root,
            source_path=args.source_jsonl,
            concurrency=args.concurrency,
            batch_events=args.batch_events,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.activate or args.activate_only:
        activation = activate_candidate(
            production_database=args.database,
            candidate_database=args.workspace / "candidate.db",
            chat_name=args.chat_name,
            workspace=args.workspace,
        )
        print(json.dumps(activation, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
