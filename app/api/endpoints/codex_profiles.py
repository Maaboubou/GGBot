"""Codex Profile management without global runtime switching."""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.codex_profile_service import CodexProfileError, get_codex_profile_service


router = APIRouter()


class CodexProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    auth_type: Literal["api_key", "chatgpt"] = "api_key"
    model: str = Field(min_length=1, max_length=128)
    provider_name: str = Field(default="OpenAI Responses compatible", max_length=100)
    base_url: str = Field(default="", max_length=1000)
    api_key: str = Field(default="", max_length=4096)
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "high"
    model_verbosity: Literal["inherit", "low", "medium", "high"] = "inherit"
    context_window: int = Field(default=128000, ge=4096, le=10_000_000)
    make_default: bool = True


class CodexProfileUpdate(BaseModel):
    model: Optional[str] = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: Optional[Literal["minimal", "low", "medium", "high", "xhigh"]] = None
    context_window: Optional[int] = Field(default=None, ge=4096, le=10_000_000)


class DefaultProfileUpdate(BaseModel):
    profile_id: str = Field(default="", max_length=48)


def _dump_set(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


def _error(exc: CodexProfileError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.get("")
def list_profiles() -> Dict[str, Any]:
    try:
        return get_codex_profile_service().list_profiles()
    except CodexProfileError as exc:
        raise _error(exc) from exc

@router.post("")
def create_profile(request: CodexProfileCreate) -> Dict[str, Any]:
    from app.services.agent_runtime import get_agent_runtime

    runtime = get_agent_runtime()
    codex_bin = str(runtime.probe.codex_bin or "").strip()
    if not codex_bin:
        raise HTTPException(status_code=409, detail="请先完成 Codex 可执行文件配置")
    try:
        profile = get_codex_profile_service().create_profile(
            _dump_set(request),
            codex_bin=codex_bin,
        )
    except CodexProfileError as exc:
        raise _error(exc) from exc
    return {"profile": profile, "requires_login": request.auth_type == "chatgpt"}


@router.patch("/{profile_id}")
def update_profile(profile_id: str, request: CodexProfileUpdate) -> Dict[str, Any]:
    try:
        return get_codex_profile_service().update_profile(profile_id, _dump_set(request))
    except CodexProfileError as exc:
        raise _error(exc) from exc


@router.put("/default/selection")
def select_default_profile(request: DefaultProfileUpdate) -> Dict[str, Any]:
    try:
        service = get_codex_profile_service()
        service.set_default_profile(request.profile_id)
        return {"default_profile_id": service.default_profile_id()}
    except CodexProfileError as exc:
        raise _error(exc) from exc
