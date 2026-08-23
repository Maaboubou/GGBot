"""Management endpoints for the LiteLLM package used by model connections."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from app.services.litellm_update_service import (
    LiteLLMUpdateError,
    get_litellm_update_service,
)


router = APIRouter(prefix="/api/llm/litellm", tags=["LiteLLM Updates"])


@router.get("/status")
async def get_litellm_update_status() -> Dict[str, Any]:
    try:
        return {"status": "success", "data": get_litellm_update_service().status()}
    except LiteLLMUpdateError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/check")
async def check_litellm_update() -> Dict[str, Any]:
    try:
        result = await asyncio.to_thread(get_litellm_update_service().check_latest)
        return {"status": "success", "data": result}
    except LiteLLMUpdateError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/update")
async def start_litellm_update() -> Dict[str, Any]:
    try:
        result = get_litellm_update_service().start_update()
        return {"status": "success", "data": result}
    except LiteLLMUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
