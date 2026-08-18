from __future__ import annotations

import logging
import os
from typing import Any, Dict

from fastapi import APIRouter, Header, HTTPException, Request

from app.services.codex_proxy.client import CodexCliClient, CodexProxyError, get_running_codex_requests
from app.services.config_service import get_setting

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/codex/v1", tags=["Codex Proxy"])


def _configured_proxy_key() -> str:
    return get_setting("CODEX_PROXY_KEY") or os.getenv("CODEX_PROXY_KEY", "")


def _verify_proxy_auth(authorization: str | None) -> None:
    expected = _configured_proxy_key()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="CODEX_PROXY_KEY is not configured",
        )

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or token != expected:
        raise HTTPException(status_code=401, detail="Invalid Codex proxy API key")


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "auth_configured": bool(_configured_proxy_key()),
        "backend": "codex-cli",
        "running_requests": len(get_running_codex_requests()),
    }


@router.get("/running")
async def running(authorization: str | None = Header(default=None)):
    _verify_proxy_auth(authorization)
    return {"data": get_running_codex_requests()}


@router.get("/models")
async def models(authorization: str | None = Header(default=None)):
    _verify_proxy_auth(authorization)
    model = os.getenv("CODEX_PROXY_MODEL", "gpt-5.6-sol")
    return {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "owned_by": "codex",
            }
        ],
    }


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _verify_proxy_auth(authorization)
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    if payload.get("stream"):
        raise HTTPException(status_code=400, detail="stream=true is not supported by Codex proxy yet")

    timeout = int(payload.get("timeout") or os.getenv("CODEX_PROXY_TIMEOUT", "600"))
    client = CodexCliClient(timeout_seconds=timeout)
    try:
        return await client.chat(payload)
    except CodexProxyError as exc:
        logger.warning("Codex proxy request failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected Codex proxy error")
        raise HTTPException(status_code=502, detail=f"Unexpected Codex proxy error: {exc}") from exc
