"""Atomic chat policy API used by the redesigned administration console."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.plugin_manager import PluginManager
from app.core.wechat_manager import WeChatManager
from app.dependencies import get_plugin_manager_instance, get_wechat_manager_instance
from app.models.base import get_db
from app.schemas.chat_policy import ChatPolicyPatch
from app.services.chat_policy_service import (
    ChatPolicyConflict,
    ChatPolicyError,
    ChatPolicyNotFound,
    ChatPolicyService,
)


router = APIRouter()


def get_service(
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance),
    wechat_manager: WeChatManager = Depends(get_wechat_manager_instance),
    db: Session = Depends(get_db),
) -> ChatPolicyService:
    return ChatPolicyService(plugin_manager, wechat_manager, db)


def _raise_policy_error(exc: ChatPolicyError) -> None:
    if isinstance(exc, ChatPolicyNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ChatPolicyConflict):
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "current_version": exc.current_version},
        ) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{user_id}/policy")
def get_chat_policy(
    user_id: int,
    service: ChatPolicyService = Depends(get_service),
) -> Dict[str, Any]:
    try:
        return service.get(user_id)
    except ChatPolicyError as exc:
        _raise_policy_error(exc)

@router.patch("/{user_id}/policy")
def update_chat_policy(
    user_id: int,
    request: ChatPolicyPatch,
    service: ChatPolicyService = Depends(get_service),
) -> Dict[str, Any]:
    try:
        return service.update(user_id, request)
    except ChatPolicyError as exc:
        _raise_policy_error(exc)
