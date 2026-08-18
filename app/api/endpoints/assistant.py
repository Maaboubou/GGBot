"""First-class Chatbot console endpoints."""

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.plugin_manager import PluginManager
from app.core.wechat_manager import WeChatManager
from app.dependencies import get_plugin_manager_instance, get_wechat_manager_instance
from app.models.base import get_db
from app.services.assistant_console_service import AssistantConsoleError, AssistantConsoleService
from app.services.memory_console_service import MemoryConsoleError, MemoryConsoleService


router = APIRouter()


class AssistantChatUpdate(BaseModel):
    enabled: Optional[bool] = None
    proactive_enabled: Optional[bool] = None
    followup_enabled: Optional[bool] = None
    followup_window_seconds: Optional[int] = Field(default=None, ge=10, le=600)
    followup_merge_seconds: Optional[int] = Field(default=None, ge=1, le=30)
    followup_max_turns: Optional[int] = Field(default=None, ge=1, le=10)
    ignored_senders: Optional[List[str]] = None
    role_id: Optional[int] = Field(default=None, ge=1)
    judge_id: Optional[int] = Field(default=None, ge=1)
    bot_group_nickname: Optional[str] = Field(default=None, max_length=128)
    bot_group_nickname_auto_enabled: Optional[bool] = None
    memory_mode: Optional[Literal["inherit", "off", "custom"]] = None
    memory_overrides: Optional[Dict[str, Any]] = None


class MemoryCandidateCleanupRequest(BaseModel):
    retention_days: int = Field(default=90, ge=7, le=3650)
    confirmation: str = Field(min_length=1, max_length=255)


class MemoryBackupRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=255)


class MemoryClearRequest(BaseModel):
    scope: Literal["stage", "events", "people", "all"]
    confirmation: str = Field(min_length=1, max_length=255)


class MemoryStageUpdate(BaseModel):
    summary: str = Field(default="", max_length=12000)
    mode: Literal["auto", "manual"] = "manual"
    reason: str = Field(min_length=2, max_length=1000)


class MemoryEventCorrectionRequest(BaseModel):
    action: Literal["invalidate", "replace_existing", "create_revision"]
    reason: str = Field(min_length=2, max_length=1000)
    false_claims: List[str] = Field(default_factory=list, max_length=20)
    corrected_claim: str = Field(default="", max_length=2000)
    affected_people: List[str] = Field(default_factory=list, max_length=30)
    existing_replacement_event_id: int = Field(default=0, ge=0)
    corrected_event: Optional[Dict[str, Any]] = None


class MemoryReasonRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)


class MemoryObservationReviewRequest(BaseModel):
    quality_status: Literal["active", "quarantined", "rejected"]
    reason: str = Field(min_length=2, max_length=1000)


class MemoryEventReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=2, max_length=1000)


class MemoryPersonFactRequest(BaseModel):
    field: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=600)
    slot_key: str = Field(default="", max_length=120)
    status: Literal["current", "historical", "planned", "uncertain", "disputed"] = "current"
    valid_from: str = Field(default="", max_length=40)
    valid_to: str = Field(default="", max_length=40)
    observed_at: str = Field(default="", max_length=40)
    sensitivity: Literal["low", "medium", "high"] = "low"
    reason: str = Field(min_length=2, max_length=1000)


class MemoryPersonAliasRequest(BaseModel):
    alias_name: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=2, max_length=1000)


class MemoryPersonMergeRequest(BaseModel):
    target_person_name: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=2, max_length=1000)


class MemoryChangeRevertRequest(BaseModel):
    category: Literal["event", "stage", "person_identity"]


def get_assistant_service(
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance),
    wechat_manager: WeChatManager = Depends(get_wechat_manager_instance),
    db: Session = Depends(get_db),
) -> AssistantConsoleService:
    return AssistantConsoleService(plugin_manager, wechat_manager, db)


def get_memory_console_service(
    db: Session = Depends(get_db),
) -> MemoryConsoleService:
    return MemoryConsoleService(db)


def _memory_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MemoryConsoleError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail=f"记忆库操作失败：{exc}")


@router.get("/overview")
def get_assistant_overview(
    service: AssistantConsoleService = Depends(get_assistant_service),
) -> Dict[str, Any]:
    return service.overview()


@router.patch("/chats/{user_id}")
def update_assistant_chat(
    user_id: int,
    request: AssistantChatUpdate,
    service: AssistantConsoleService = Depends(get_assistant_service),
) -> Dict[str, Any]:
    fields_set = getattr(request, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(request, "__fields_set__", set())
    changes = {field: getattr(request, field) for field in fields_set}
    try:
        service.update_chat(user_id, changes)
    except AssistantConsoleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"聊天配置未保存：{exc}") from exc
    return {"message": "聊天的 Chatbot 配置已保存"}


@router.get("/memory/chats/{user_id}/overview")
def get_memory_overview(
    user_id: int,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.overview(user_id)
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.get("/memory/chats/{user_id}/events")
def get_memory_events(
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
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.events(
            user_id,
            query=q,
            date_from=date_from,
            date_to=date_to,
            status=status,
            offset=offset,
            limit=limit,
        )
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.get("/memory/chats/{user_id}/events/{event_id}")
def get_memory_event_detail(
    user_id: int,
    event_id: int,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.event_detail(user_id, event_id)
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.put("/memory/chats/{user_id}/stage")
def update_memory_stage(
    user_id: int,
    request: MemoryStageUpdate,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.update_stage(
            user_id,
            summary=request.summary,
            mode=request.mode,
            reason=request.reason,
        )
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.post("/memory/chats/{user_id}/events/{event_id}/corrections")
def correct_memory_event(
    user_id: int,
    event_id: int,
    request: MemoryEventCorrectionRequest,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    fields_set = getattr(request, "model_dump", None)
    payload = request.model_dump() if fields_set else request.dict()
    try:
        return service.correct_event(user_id, event_id, payload)
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.post("/memory/chats/{user_id}/events/{event_id}/delete")
def delete_memory_event(
    user_id: int,
    event_id: int,
    request: MemoryReasonRequest,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.delete_event(user_id, event_id, reason=request.reason)
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.post("/memory/chats/{user_id}/events/{event_id}/review")
def review_memory_event(
    user_id: int,
    event_id: int,
    request: MemoryEventReviewRequest,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.review_event(
            user_id,
            event_id,
            decision=request.decision,
            reason=request.reason,
        )
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.get("/memory/chats/{user_id}/people")
def get_memory_people(
    user_id: int,
    q: str = Query("", max_length=200),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.people(user_id, query=q, offset=offset, limit=limit)
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.get("/memory/chats/{user_id}/people/{person_id}")
def get_memory_person_detail(
    user_id: int,
    person_id: int,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.person_detail(user_id, person_id)
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.post(
    "/memory/chats/{user_id}/people/{person_id}/observations/"
    "{observation_id}/review"
)
def review_memory_observation(
    user_id: int,
    person_id: int,
    observation_id: int,
    request: MemoryObservationReviewRequest,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.review_observation(
            user_id,
            person_id,
            observation_id,
            quality_status=request.quality_status,
            reason=request.reason,
        )
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.post("/memory/chats/{user_id}/people/{person_id}/facts")
def add_memory_person_fact(
    user_id: int,
    person_id: int,
    request: MemoryPersonFactRequest,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    payload = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    reason = str(payload.pop("reason"))
    try:
        return service.add_person_fact(
            user_id,
            person_id,
            payload,
            reason=reason,
        )
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.post("/memory/chats/{user_id}/people/{person_id}/facts/{fact_id}/delete")
def delete_memory_person_fact(
    user_id: int,
    person_id: int,
    fact_id: int,
    request: MemoryReasonRequest,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.delete_person_fact(
            user_id,
            person_id,
            fact_id,
            reason=request.reason,
        )
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.post("/memory/chats/{user_id}/people/{person_id}/aliases")
def add_memory_person_alias(
    user_id: int,
    person_id: int,
    request: MemoryPersonAliasRequest,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.add_person_alias(
            user_id,
            person_id,
            alias_name=request.alias_name,
            reason=request.reason,
        )
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.post("/memory/chats/{user_id}/people/{person_id}/merge")
def merge_memory_person(
    user_id: int,
    person_id: int,
    request: MemoryPersonMergeRequest,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.merge_person(
            user_id,
            person_id,
            target_person_name=request.target_person_name,
            reason=request.reason,
        )
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.get("/memory/chats/{user_id}/reviews")
def get_memory_reviews(
    user_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.reviews(user_id, offset=offset, limit=limit)
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.get("/memory/chats/{user_id}/changes")
def get_memory_changes(
    user_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=100),
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.changes(user_id, offset=offset, limit=limit)
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.post("/memory/chats/{user_id}/changes/{change_id}/revert")
def revert_memory_change(
    user_id: int,
    change_id: int,
    request: MemoryChangeRevertRequest,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.revert_change(
            user_id,
            category=request.category,
            change_id=change_id,
        )
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.get("/memory/chats/{user_id}/maintenance")
def get_memory_maintenance(
    user_id: int,
    retention_days: int = Query(90, ge=7, le=3650),
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.maintenance(user_id, retention_days=retention_days)
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.post("/memory/chats/{user_id}/maintenance/cleanup-candidates")
def cleanup_memory_candidates(
    user_id: int,
    request: MemoryCandidateCleanupRequest,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.cleanup_candidates(
            user_id,
            retention_days=request.retention_days,
            confirmation=request.confirmation,
        )
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.post("/memory/chats/{user_id}/maintenance/backup")
def backup_memory_database(
    user_id: int,
    request: MemoryBackupRequest,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.backup_database(
            user_id,
            confirmation=request.confirmation,
        )
    except Exception as exc:
        raise _memory_error(exc) from exc


@router.post("/memory/chats/{user_id}/maintenance/clear")
def clear_memory(
    user_id: int,
    request: MemoryClearRequest,
    service: MemoryConsoleService = Depends(get_memory_console_service),
) -> Dict[str, Any]:
    try:
        return service.clear_memory(
            user_id,
            scope=request.scope,
            confirmation=request.confirmation,
        )
    except Exception as exc:
        raise _memory_error(exc) from exc
