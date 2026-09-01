"""HTTP API for Profile-local Codex Skills."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.services.codex_skill_service import CodexSkillError, get_codex_skill_service


router = APIRouter()


class CodexSkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    instructions: str = Field(min_length=1, max_length=240_000)


class CodexSkillUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=262_144)


class CodexSkillGithubInstall(BaseModel):
    repository_url: str = Field(min_length=1, max_length=512)
    skill_path: str = Field(min_length=1, max_length=512)
    ref: str = Field(default="main", min_length=1, max_length=128)


class CodexSkillEnabledUpdate(BaseModel):
    enabled: bool


def _error(exc: CodexSkillError) -> HTTPException:
    status_codes = {
        "not_found": status.HTTP_404_NOT_FOUND,
        "conflict": status.HTTP_409_CONFLICT,
        "unsafe": status.HTTP_403_FORBIDDEN,
        "readonly": status.HTTP_403_FORBIDDEN,
        "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
        "io_error": status.HTTP_503_SERVICE_UNAVAILABLE,
        "download_failed": status.HTTP_502_BAD_GATEWAY,
        "timeout": status.HTTP_504_GATEWAY_TIMEOUT,
    }
    return HTTPException(status_code=status_codes.get(exc.code, 422), detail=str(exc))


@router.get("/{profile_id}/skills")
async def list_profile_skills(profile_id: str) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(get_codex_skill_service().list_skills, profile_id)
    except CodexSkillError as exc:
        raise _error(exc) from exc


@router.post("/{profile_id}/skills", status_code=status.HTTP_201_CREATED)
async def create_profile_skill(profile_id: str, request: CodexSkillCreate) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(
            get_codex_skill_service().create_skill,
            profile_id,
            name=request.name,
            description=request.description,
            instructions=request.instructions,
        )
    except CodexSkillError as exc:
        raise _error(exc) from exc


@router.post("/{profile_id}/skills/install/github", status_code=status.HTTP_201_CREATED)
async def install_profile_skill_from_github(
    profile_id: str,
    request: CodexSkillGithubInstall,
) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(
            get_codex_skill_service().install_github_skill,
            profile_id,
            repository_url=request.repository_url,
            skill_path=request.skill_path,
            ref=request.ref,
        )
    except CodexSkillError as exc:
        raise _error(exc) from exc


@router.get("/{profile_id}/skills/trash")
async def list_profile_skill_trash(profile_id: str) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(get_codex_skill_service().list_trash, profile_id)
    except CodexSkillError as exc:
        raise _error(exc) from exc


@router.post("/{profile_id}/skills/trash/{trash_id}/restore")
async def restore_profile_skill(profile_id: str, trash_id: str) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(
            get_codex_skill_service().restore_skill,
            profile_id,
            trash_id,
        )
    except CodexSkillError as exc:
        raise _error(exc) from exc


@router.get("/{profile_id}/skills/{scope}/{skill_name}")
async def get_profile_skill(
    profile_id: str,
    scope: Literal["profile", "system"],
    skill_name: str,
) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(
            get_codex_skill_service().get_skill,
            profile_id,
            scope,
            skill_name,
        )
    except CodexSkillError as exc:
        raise _error(exc) from exc


@router.put("/{profile_id}/skills/profile/{skill_name}")
async def update_profile_skill(
    profile_id: str,
    skill_name: str,
    request: CodexSkillUpdate,
) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(
            get_codex_skill_service().update_skill,
            profile_id,
            skill_name,
            content=request.content,
        )
    except CodexSkillError as exc:
        raise _error(exc) from exc


@router.put("/{profile_id}/skills/profile/{skill_name}/enabled")
async def set_profile_skill_enabled(
    profile_id: str,
    skill_name: str,
    request: CodexSkillEnabledUpdate,
) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(
            get_codex_skill_service().set_enabled,
            profile_id,
            skill_name,
            enabled=request.enabled,
        )
    except CodexSkillError as exc:
        raise _error(exc) from exc


@router.delete("/{profile_id}/skills/profile/{skill_name}")
async def archive_profile_skill(profile_id: str, skill_name: str) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(
            get_codex_skill_service().archive_skill,
            profile_id,
            skill_name,
        )
    except CodexSkillError as exc:
        raise _error(exc) from exc
