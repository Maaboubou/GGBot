#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
权限管理API端点
- 管理微信用户（群组）
- 管理用户对应的插件权限
"""

from typing import Any, Dict, List, Literal, Optional
import json
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models import base as models_base
from app.models import user_permission as models_permission
from app.schemas import permission as schemas_permission
from app.core.wechat_manager import WeChatManager
from app.dependencies import get_wechat_manager_instance

router = APIRouter()


class ChatMemoryUpdate(BaseModel):
    stage_summary: Optional[str] = None
    stage_mode: Literal["auto", "manual"] = "manual"
    reason: str = Field(default="管理员编辑阶段记忆", min_length=2, max_length=1000)


class ChatMemoryEventCorrection(BaseModel):
    action: Literal["invalidate", "replace_existing", "create_revision"]
    reason: str = Field(min_length=2, max_length=1000)
    false_claims: List[str] = Field(default_factory=list, max_length=20)
    corrected_claim: str = Field(default="", max_length=2000)
    affected_people: List[str] = Field(default_factory=list, max_length=30)
    existing_replacement_event_id: int = Field(default=0, ge=0)
    corrected_event: Optional[Dict[str, Any]] = None


class ChatMemoryPersonUpdate(BaseModel):
    profile_text: str = Field(default="", max_length=12000)
    reason: str = Field(min_length=2, max_length=1000)
    keep_override: bool = True


class ChatMemoryPersonAliasCreate(BaseModel):
    alias_name: str = Field(min_length=1, max_length=80)
    external_id: str = Field(default="", max_length=160)
    reason: str = Field(min_length=2, max_length=1000)


class ChatMemoryPersonMerge(BaseModel):
    target_person_id: int = Field(gt=0)
    reason: str = Field(min_length=2, max_length=1000)


class ChatMemoryPersonFactUpdate(BaseModel):
    field: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=500)
    status: str = Field(default="current", max_length=20)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    valid_from: str = Field(default="", max_length=40)
    valid_to: str = Field(default="", max_length=40)
    observed_at: str = Field(default="", max_length=40)
    temporal_note: str = Field(default="", max_length=200)
    source_event_ids: List[int] = Field(default_factory=list, max_length=20)
    replaces_fact_ids: List[int] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=2, max_length=1000)


class ChatMemoryPersonV3ObservationReview(BaseModel):
    quality_status: Literal["active", "quarantined", "rejected"]
    reason: str = Field(min_length=2, max_length=1000)


class ChatMemoryPersonV3FactUpdate(BaseModel):
    field: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=600)
    slot_key: str = Field(default="", max_length=120)
    status: Literal[
        "current",
        "historical",
        "planned",
        "uncertain",
        "disputed",
    ] = "current"
    valid_from: str = Field(default="", max_length=40)
    valid_to: str = Field(default="", max_length=40)
    observed_at: str = Field(default="", max_length=40)
    sensitivity: Literal["low", "medium", "high"] = "low"
    reason: str = Field(min_length=2, max_length=1000)


def _normalize_sender_blacklist(raw_value: str | None) -> str | None:
    """Normalize textarea/plain/JSON blacklist input to a JSON string array."""
    if raw_value is None:
        return None

    if isinstance(raw_value, str):
        value = raw_value.strip()
        if not value:
            return None
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = value.replace(",", "\n").splitlines()
    else:
        parsed = raw_value

    if not isinstance(parsed, list):
        return None

    names = []
    seen = set()
    for item in parsed:
        name = str(item or "").strip()
        if name and name not in seen:
            names.append(name)
            seen.add(name)

    return json.dumps(names, ensure_ascii=False) if names else None


def _field_was_set(model, field_name: str) -> bool:
    fields_set = getattr(model, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(model, "__fields_set__", set())
    return field_name in fields_set


def _get_user_or_404(db: Session, user_id: int):
    db_user = (
        db.query(models_permission.WeChatUser)
        .filter(models_permission.WeChatUser.id == user_id)
        .first()
    )
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return db_user


def _invalidate_chatbot_memory_context(chat_name: str) -> None:
    try:
        from app.plugins.builtin_chatbot.main import get_chatbot_plugin

        plugin = get_chatbot_plugin()
        if plugin and hasattr(plugin, "invalidate_memory_context"):
            plugin.invalidate_memory_context(chat_name)
    except Exception:
        pass


def _reject_legacy_person_write_when_v3_active(store, chat_name: str) -> None:
    from app.plugins.builtin_chatbot.person_memory_v3 import PersonMemoryV3Store

    state = PersonMemoryV3Store(store).get_chat_state(chat_name)
    if state and state.get("mode") in {"building", "active"}:
        raise HTTPException(
            status_code=409,
            detail=(
                "该聊天已使用统一人物记忆，请通过人物观察与事实版本接口修改；"
                "旧版人物摘要写入口已停止使用"
            ),
        )

@router.post("/users", response_model=schemas_permission.WeChatUser)
def create_wechat_user(
    user: schemas_permission.WeChatUserCreate, 
    db: Session = Depends(models_base.get_db),
    wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)
):
    """
    添加一个新的微信用户或群组到权限管理列表，并确保其被监听。
    如果用户已存在，则直接返回用户信息。
    """
    db_user = db.query(models_permission.WeChatUser).filter(models_permission.WeChatUser.chat_name == user.chat_name).first()
    if db_user:
        # “添加”已有聊天等同于显式恢复监听，并允许后续断线自动恢复。
        db_user.listening_enabled = True
        db.commit()
        db.refresh(db_user)
        if wechat_manager and wechat_manager.is_connected():
            wechat_manager.add_listen_chat(db_user.chat_name)
        return db_user
    
    # 用户不存在，创建新用户
    new_user = models_permission.WeChatUser(**user.dict())
    new_user.listening_enabled = True
    new_user.sender_blacklist = _normalize_sender_blacklist(user.sender_blacklist)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    # 开始监听
    if wechat_manager and wechat_manager.is_connected():
        wechat_manager.add_listen_chat(new_user.chat_name)
    else:
        # 即使微信未连接，也允许添加用户，只是日志警告
        print("Warning: WeChat manager not connected. User added to DB but not listening.")

    return new_user

@router.get("/users", response_model=List[schemas_permission.WeChatUser])
def list_wechat_users(skip: int = 0, limit: int = 100, db: Session = Depends(models_base.get_db)):
    """列出所有在权限管理列表中的微信用户"""
    users = db.query(models_permission.WeChatUser).offset(skip).limit(limit).all()
    return users

@router.get("/users/{user_id}", response_model=schemas_permission.WeChatUser)
def get_wechat_user(user_id: int, db: Session = Depends(models_base.get_db)):
    """获取单个微信用户的详细信息和权限"""
    db_user = db.query(models_permission.WeChatUser).filter(models_permission.WeChatUser.id == user_id).first()
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return db_user


@router.get("/users/{user_id}/memory")
def get_user_memory(user_id: int, db: Session = Depends(models_base.get_db)):
    """Return event-memory statistics and the editable stage summary."""
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.context_manager import ChatContextManager
    from app.plugins.builtin_chatbot.chat_log import ChatLogManager
    from app.plugins.builtin_chatbot.memory_service import ChatMemoryService

    service = ChatMemoryService(ChatLogManager(), ChatContextManager())
    try:
        document = service.get_memory_document(db_user.chat_name)
    finally:
        service.close()
    return {"status": "success", "data": document}


@router.get("/users/{user_id}/memory/events")
def browse_user_memory_events(
    user_id: int,
    q: str = Query("", max_length=200),
    date_from: str = Query("", max_length=10),
    date_to: str = Query("", max_length=10),
    status: str = Query(
        "all",
        pattern="^(all|active|superseded|invalidated|quarantined)$",
    ),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(models_base.get_db),
):
    """Browse complete event cards without loading vector blobs."""
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore

    items, total = MemoryStore().browse_events(
        db_user.chat_name,
        query=q,
        date_from=date_from,
        date_to=date_to,
        status=status,
        offset=offset,
        limit=limit,
    )
    for item in items:
        item.pop("embedding", None)
        item["has_embedding"] = int(item.get("embedding_dim") or 0) > 0
        item.pop("search_text", None)
    return {
        "status": "success",
        "data": {
            "chat_name": db_user.chat_name,
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "query": q,
            "date_from": date_from,
            "date_to": date_to,
            "status": status,
        },
    }


@router.get("/users/{user_id}/memory/people")
def browse_user_memory_people(
    user_id: int,
    db: Session = Depends(models_base.get_db),
):
    """Return all current person profiles for the selected chat."""
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import (
        PERSON_FACT_FIELDS,
        PERSON_FACT_STATUSES,
        PERSON_FIELD_LABELS,
        MemoryStore,
    )

    store = MemoryStore()
    from app.plugins.builtin_chatbot.person_memory_v3 import (
        PersonMemoryV3Store,
    )

    v3 = PersonMemoryV3Store(store)
    v3_state = v3.get_chat_state(db_user.chat_name)
    identity_items = store.list_person_directory(db_user.chat_name)
    merge_suggestions = store.list_person_merge_suggestions(
        db_user.chat_name
    )
    v3_items = v3.list_profiles(db_user.chat_name) if v3_state else []
    if v3_items:
        items = v3_items
        schema_version = 3
        items.sort(
            key=lambda item: (
                int(item.get("observation_count") or 0),
                str(item.get("snapshot_created_at") or ""),
            ),
            reverse=True,
        )
    else:
        items = store.list_people(db_user.chat_name)
        schema_version = 2
        items.sort(
            key=lambda item: (
                str(item.get("updated_at") or ""),
                int(item.get("source_event_id") or 0),
            ),
            reverse=True,
        )
    return {
        "status": "success",
        "data": {
            "chat_name": db_user.chat_name,
            "items": items,
            "total": len(items),
            "identity_items": identity_items,
            "identity_total": len(identity_items),
            "merge_suggestions": merge_suggestions,
            "schema_version": schema_version,
            "v3_state": v3_state,
            "v3_observation_stats": (
                v3.observation_stats(db_user.chat_name)
                if v3_state
                else {
                    "total": 0,
                    "active": 0,
                    "quarantined": 0,
                    "rejected": 0,
                }
            ),
            "v3_candidate_stats": (
                v3.candidate_stats(db_user.chat_name)
                if v3_state
                else {
                    "total": 0,
                    "pending": 0,
                    "verified": 0,
                    "quarantined": 0,
                    "rejected": 0,
                }
            ),
            "v3_pipeline_stats": (
                v3.pipeline_stats(db_user.chat_name)
                if v3_state
                else {
                    "source_messages": 0,
                    "links": 0,
                    "pending_links": 0,
                    "pending_people": 0,
                }
            ),
            "fact_schema": {
                "fields": [
                    {
                        "value": field_name,
                        "label": PERSON_FIELD_LABELS.get(
                            field_name,
                            field_name,
                        ),
                    }
                    for field_name in sorted(PERSON_FACT_FIELDS)
                    if field_name != "legacy_summary"
                ],
                "statuses": sorted(PERSON_FACT_STATUSES),
            },
        },
    }


@router.get("/users/{user_id}/memory/people/v3/audits")
def browse_user_memory_person_v3_audits(
    user_id: int,
    db: Session = Depends(models_base.get_db),
):
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore
    from app.plugins.builtin_chatbot.person_memory_v3 import (
        PersonMemoryV3Store,
    )

    items = PersonMemoryV3Store(MemoryStore()).list_audits(
        db_user.chat_name,
        include_snapshots=False,
    )
    for item in items:
        item.pop("before", None)
        item.pop("after", None)
    return {
        "status": "success",
        "data": {
            "chat_name": db_user.chat_name,
            "items": items,
            "total": len(items),
        },
    }


@router.get("/users/{user_id}/memory/people/{person_id}/v3")
def get_user_memory_person_v3(
    user_id: int,
    person_id: int,
    db: Session = Depends(models_base.get_db),
):
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore
    from app.plugins.builtin_chatbot.person_memory_v3 import (
        PersonMemoryV3Store,
    )

    profile = PersonMemoryV3Store(MemoryStore()).get_profile(
        db_user.chat_name,
        person_id,
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="v3 person not found")
    return {
        "status": "success",
        "data": {
            "chat_name": db_user.chat_name,
            "profile": profile,
        },
    }


@router.post(
    "/users/{user_id}/memory/people/{person_id}/v3/observations/"
    "{observation_id}/review"
)
def review_user_memory_person_v3_observation(
    user_id: int,
    person_id: int,
    observation_id: int,
    payload: ChatMemoryPersonV3ObservationReview,
    db: Session = Depends(models_base.get_db),
):
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore
    from app.plugins.builtin_chatbot.person_memory_v3 import (
        PersonMemoryV3Store,
    )

    v3 = PersonMemoryV3Store(MemoryStore())
    observation = v3.get_observation(db_user.chat_name, observation_id)
    if (
        observation is None
        or int(observation.get("person_id") or 0) != int(person_id)
    ):
        raise HTTPException(
            status_code=400,
            detail="observation does not belong to this person",
        )
    try:
        result = v3.review_observation(
            db_user.chat_name,
            observation_id,
            quality_status=payload.quality_status,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": result}


@router.post("/users/{user_id}/memory/people/{person_id}/v3/facts")
def add_user_memory_person_v3_fact(
    user_id: int,
    person_id: int,
    payload: ChatMemoryPersonV3FactUpdate,
    db: Session = Depends(models_base.get_db),
):
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore
    from app.plugins.builtin_chatbot.person_memory_v3 import (
        PersonMemoryV3Store,
    )

    value = payload.dict(exclude={"reason"})
    try:
        result = PersonMemoryV3Store(MemoryStore()).add_manual_fact(
            db_user.chat_name,
            person_id,
            value,
            reason=payload.reason,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": result}


@router.delete(
    "/users/{user_id}/memory/people/{person_id}/v3/facts/{fact_id}"
)
def delete_user_memory_person_v3_fact(
    user_id: int,
    person_id: int,
    fact_id: int,
    reason: str = Query(min_length=2, max_length=1000),
    db: Session = Depends(models_base.get_db),
):
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore
    from app.plugins.builtin_chatbot.person_memory_v3 import (
        PersonMemoryV3Store,
    )

    try:
        result = PersonMemoryV3Store(MemoryStore()).delete_fact(
            db_user.chat_name,
            person_id,
            fact_id,
            reason=reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": result}


@router.post("/users/{user_id}/memory/people/{person_id}/v3/refresh")
def refresh_user_memory_person_v3(
    user_id: int,
    person_id: int,
    db: Session = Depends(models_base.get_db),
):
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.chat_log import ChatLogManager
    from app.plugins.builtin_chatbot.context_manager import ChatContextManager
    from app.plugins.builtin_chatbot.memory_service import ChatMemoryService

    service = ChatMemoryService(ChatLogManager(), ChatContextManager())
    try:
        result = service.person_memory_v3.consolidate_person(
            db_user.chat_name,
            person_id,
            force=True,
            observation_limit=500,
        )
    finally:
        service.close()
    if result is None:
        raise HTTPException(
            status_code=400,
            detail="person has no active observations to refresh",
        )
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": result}


@router.get("/users/{user_id}/memory/people/audits")
def browse_user_memory_person_audits(
    user_id: int,
    db: Session = Depends(models_base.get_db),
):
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore

    items = MemoryStore().list_person_audits(
        db_user.chat_name,
        include_snapshots=False,
    )
    for item in items:
        item.pop("before", None)
        item.pop("after", None)
    return {
        "status": "success",
        "data": {
            "chat_name": db_user.chat_name,
            "items": items,
            "total": len(items),
        },
    }


@router.post("/users/{user_id}/memory/people/{person_id}/aliases")
def add_user_memory_person_alias(
    user_id: int,
    person_id: int,
    payload: ChatMemoryPersonAliasCreate,
    db: Session = Depends(models_base.get_db),
):
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore

    try:
        audit = MemoryStore().add_person_alias(
            db_user.chat_name,
            person_id,
            alias_name=payload.alias_name,
            external_id=payload.external_id,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": {"audit": audit}}


@router.post("/users/{user_id}/memory/people/{person_id}/merge")
def merge_user_memory_person(
    user_id: int,
    person_id: int,
    payload: ChatMemoryPersonMerge,
    db: Session = Depends(models_base.get_db),
):
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore

    try:
        audit = MemoryStore().merge_people(
            db_user.chat_name,
            person_id,
            payload.target_person_id,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": {"audit": audit}}


@router.post("/users/{user_id}/memory/people/{person_id}/facts")
def upsert_user_memory_person_fact(
    user_id: int,
    person_id: int,
    payload: ChatMemoryPersonFactUpdate,
    db: Session = Depends(models_base.get_db),
):
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore

    fact = payload.dict(exclude={"reason"})
    store = MemoryStore()
    _reject_legacy_person_write_when_v3_active(store, db_user.chat_name)
    try:
        audit = store.upsert_person_fact_manual(
            db_user.chat_name,
            person_id,
            fact,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": {"audit": audit}}


@router.delete("/users/{user_id}/memory/people/{person_id}/facts/{fact_id}")
def delete_user_memory_person_fact(
    user_id: int,
    person_id: int,
    fact_id: int,
    reason: str = Query(min_length=2, max_length=1000),
    db: Session = Depends(models_base.get_db),
):
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore

    store = MemoryStore()
    _reject_legacy_person_write_when_v3_active(store, db_user.chat_name)
    try:
        audit = store.delete_person_fact_manual(
            db_user.chat_name,
            person_id,
            fact_id,
            reason=reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": {"audit": audit}}


@router.post("/users/{user_id}/memory/people/audits/{audit_id}/revert")
def revert_user_memory_person_audit(
    user_id: int,
    audit_id: int,
    db: Session = Depends(models_base.get_db),
):
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore

    try:
        audit = MemoryStore().revert_person_audit(
            db_user.chat_name,
            audit_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": {"audit": audit}}


@router.get("/users/{user_id}/memory/corrections")
def browse_user_memory_corrections(
    user_id: int,
    active_only: bool = Query(False),
    db: Session = Depends(models_base.get_db),
):
    """Return the auditable correction history for one chat."""
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore

    items = MemoryStore().list_corrections(
        db_user.chat_name,
        active_only=active_only,
        include_snapshots=False,
    )
    for item in items:
        item.pop("before", None)
        item.pop("after", None)
    return {
        "status": "success",
        "data": {
            "chat_name": db_user.chat_name,
            "items": items,
            "total": len(items),
        },
    }


@router.post("/users/{user_id}/memory/events/{event_id}/corrections")
def correct_user_memory_event(
    user_id: int,
    event_id: int,
    payload: ChatMemoryEventCorrection,
    db: Session = Depends(models_base.get_db),
):
    """Correct one event and repair its derived stage/person memories."""
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.context_manager import ChatContextManager
    from app.plugins.builtin_chatbot.chat_log import ChatLogManager
    from app.plugins.builtin_chatbot.memory_service import ChatMemoryService

    service = ChatMemoryService(ChatLogManager(), ChatContextManager())
    try:
        result = service.correct_event_manual(
            db_user.chat_name,
            event_id=event_id,
            action=payload.action,
            reason=payload.reason,
            false_claims=payload.false_claims,
            corrected_claim=payload.corrected_claim,
            affected_people=payload.affected_people,
            existing_replacement_event_id=(
                payload.existing_replacement_event_id
            ),
            corrected_event_fields=payload.corrected_event,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        service.close()
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": result}


@router.delete("/users/{user_id}/memory/events/{event_id}")
def delete_user_memory_event(
    user_id: int,
    event_id: int,
    reason: str = Query(
        "管理员从记忆库浏览器删除事件卡",
        min_length=2,
        max_length=1000,
    ),
    db: Session = Depends(models_base.get_db),
):
    """Soft-delete one event, repair derived memory, and keep an undo record."""
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.context_manager import ChatContextManager
    from app.plugins.builtin_chatbot.chat_log import ChatLogManager
    from app.plugins.builtin_chatbot.memory_service import ChatMemoryService

    service = ChatMemoryService(ChatLogManager(), ChatContextManager())
    try:
        result = service.delete_event_manual(
            db_user.chat_name,
            event_id=event_id,
            reason=reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        service.close()
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": result}


@router.post("/users/{user_id}/memory/corrections/{correction_id}/revert")
def revert_user_memory_correction(
    user_id: int,
    correction_id: int,
    db: Session = Depends(models_base.get_db),
):
    """Revert one active correction from its stored before-snapshot."""
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.context_manager import ChatContextManager
    from app.plugins.builtin_chatbot.chat_log import ChatLogManager
    from app.plugins.builtin_chatbot.memory_service import ChatMemoryService

    service = ChatMemoryService(ChatLogManager(), ChatContextManager())
    try:
        result = service.revert_manual_correction(
            db_user.chat_name,
            correction_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        service.close()
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": result}


@router.put("/users/{user_id}/memory/people/{person_name}")
def update_user_memory_person(
    user_id: int,
    person_name: str,
    payload: ChatMemoryPersonUpdate,
    db: Session = Depends(models_base.get_db),
):
    """Manually edit and optionally lock a person profile."""
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_store import MemoryStore

    store = MemoryStore()
    _reject_legacy_person_write_when_v3_active(store, db_user.chat_name)
    try:
        correction = store.update_person_manual(
            db_user.chat_name,
            person_name,
            profile_text=payload.profile_text,
            reason=payload.reason,
            keep_override=payload.keep_override,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {
        "status": "success",
        "data": {"correction": correction},
    }


@router.get("/users/{user_id}/memory/events/{event_id}/source")
def get_user_memory_event_source(
    user_id: int,
    event_id: int,
    db: Session = Depends(models_base.get_db),
):
    """Return the raw chat range referenced by one memory event."""
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.memory_source import read_event_source
    from app.plugins.builtin_chatbot.memory_store import MemoryStore

    store = MemoryStore()
    event = store.get_event(db_user.chat_name, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="记忆事件不存在")
    event.pop("embedding", None)
    event.pop("search_text", None)
    event["has_embedding"] = int(event.get("embedding_dim") or 0) > 0

    source_start = max(1, int(event.get("source_start_cursor") or 1))
    source_end = max(source_start, int(event.get("source_end_cursor") or source_start))
    messages = read_event_source(
        store,
        event,
        limit=min(200, source_end - source_start + 1),
    )
    safe_messages = [
        {
            key: message.get(key)
            for key in ("_log_cursor", "time", "sender", "content")
            if message.get(key) is not None
        }
        for message in messages
    ]
    return {
        "status": "success",
        "data": {
            "chat_name": db_user.chat_name,
            "event": event,
            "messages": safe_messages,
        },
    }


@router.put("/users/{user_id}/memory")
def update_user_memory(
    user_id: int,
    payload: ChatMemoryUpdate,
    db: Session = Depends(models_base.get_db),
):
    """Manually edit the stage summary."""
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.context_manager import ChatContextManager
    from app.plugins.builtin_chatbot.chat_log import ChatLogManager
    from app.plugins.builtin_chatbot.memory_service import ChatMemoryService

    service = ChatMemoryService(ChatLogManager(), ChatContextManager())
    try:
        document = service.update_stage_manual(
            db_user.chat_name,
            payload.stage_summary or "",
            mode=payload.stage_mode,
            reason=payload.reason,
        )
    finally:
        service.close()
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": document}


@router.delete("/users/{user_id}/memory")
def clear_user_memory(
    user_id: int,
    scope: str = Query("all", pattern="^(all|stage|events|people)$"),
    db: Session = Depends(models_base.get_db),
):
    """Clear an explicitly selected event-memory tier for one chat."""
    db_user = _get_user_or_404(db, user_id)
    from app.plugins.builtin_chatbot.context_manager import ChatContextManager
    from app.plugins.builtin_chatbot.chat_log import ChatLogManager
    from app.plugins.builtin_chatbot.memory_service import ChatMemoryService

    service = ChatMemoryService(ChatLogManager(), ChatContextManager())
    try:
        document = service.clear_memory(db_user.chat_name, scope)
    finally:
        service.close()
    _invalidate_chatbot_memory_context(db_user.chat_name)
    return {"status": "success", "data": document}

@router.put("/users/{user_id}", response_model=schemas_permission.WeChatUser)
def update_wechat_user(
    user_id: int,
    user_update: schemas_permission.WeChatUserUpdate,
    db: Session = Depends(models_base.get_db)
):
    """更新微信用户的基本信息（备注、聊天类型等）"""
    db_user = db.query(models_permission.WeChatUser).filter(models_permission.WeChatUser.id == user_id).first()
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")
    
    # 更新字段
    if user_update.remark is not None:
        db_user.remark = user_update.remark
    if user_update.is_group is not None:
        db_user.is_group = user_update.is_group
    if _field_was_set(user_update, "sender_blacklist"):
        db_user.sender_blacklist = _normalize_sender_blacklist(user_update.sender_blacklist)
    if _field_was_set(user_update, "bot_group_nickname"):
        nickname = str(user_update.bot_group_nickname or "").strip()
        db_user.bot_group_nickname = nickname or None
    if user_update.bot_group_nickname_auto_enabled is not None:
        db_user.bot_group_nickname_auto_enabled = user_update.bot_group_nickname_auto_enabled
    
    db.commit()
    db.refresh(db_user)
    return db_user

@router.put("/users/{user_id}/permissions", response_model=schemas_permission.WeChatUser)
def update_user_permissions(
    user_id: int, 
    permissions: schemas_permission.PermissionsUpdateRequest,
    db: Session = Depends(models_base.get_db),
    wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)
):
    """更新指定用户的插件权限列表，并确保其被监听"""
    db_user = db.query(models_permission.WeChatUser).filter(models_permission.WeChatUser.id == user_id).first()
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # 删除旧权限
    db.query(models_permission.UserPermission).filter(models_permission.UserPermission.user_id == user_id).delete()

    # 添加新权限
    for permission_item in permissions.permissions:
        db_permission = models_permission.UserPermission(
            user_id=user_id, 
            plugin_name=permission_item.plugin_name,
            require_mention=permission_item.require_mention,
            proactive_enabled=permission_item.proactive_enabled,
            followup_enabled=permission_item.followup_enabled,
            followup_window_seconds=max(10, min(600, permission_item.followup_window_seconds)),
            followup_merge_seconds=max(1, min(30, permission_item.followup_merge_seconds)),
            followup_max_turns=max(1, min(10, permission_item.followup_max_turns)),
            memory_profile=permission_item.memory_profile,
            ignored_senders=permission_item.ignored_senders,
        )
        db.add(db_permission)
    
    db.commit()
    db.refresh(db_user)

    _invalidate_chatbot_memory_context(db_user.chat_name)

    # 确保更新权限后，监听也处于激活状态
    if wechat_manager and wechat_manager.is_connected():
        wechat_manager.add_listen_chat(db_user.chat_name)
    
    return db_user

@router.delete("/users/{user_id}", response_model=schemas_permission.WeChatUser)
def delete_wechat_user(
    user_id: int, 
    db: Session = Depends(models_base.get_db),
    wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)
):
    """从权限管理列表中删除一个用户及其所有权限，并停止监听"""
    db_user = db.query(models_permission.WeChatUser).filter(models_permission.WeChatUser.id == user_id).first()
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")
    
    chat_name_to_remove = db_user.chat_name
    
    # 从数据库删除
    db.delete(db_user)
    db.commit()

    # 停止监听
    if wechat_manager and wechat_manager.is_connected():
        wechat_manager.remove_listen_chat(chat_name_to_remove)
    else:
        print(f"Warning: WeChat manager not connected. User {chat_name_to_remove} removed from DB but listener might still be active if bot was restarted.")

    return db_user
