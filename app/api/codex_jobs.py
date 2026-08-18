from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query

from app.services.codex_job_manager import codex_job_manager

router = APIRouter(prefix="/api/codex/jobs", tags=["Codex Jobs"])


@router.get("")
async def list_codex_jobs(recent_limit: int = Query(default=50, ge=0, le=200)) -> Dict[str, Any]:
    """Return active Codex CLI jobs plus recent completed/cancelled/failed jobs."""
    from app.services.codex_app_server import get_codex_app_server_manager

    active = codex_job_manager.list_active()
    manager = get_codex_app_server_manager()
    session_data = manager.session_snapshot(active)
    return {
        "status": "success",
        "data": {
            "active": active,
            "recent": codex_job_manager.list_recent(limit=recent_limit),
            "stats": codex_job_manager.stats(),
            "sessions": session_data["sessions"],
            "session_stats": session_data["stats"],
            "app_server": manager.status(),
        },
    }


@router.get("/running")
async def list_running_codex_jobs() -> Dict[str, Any]:
    """Compatibility endpoint for active jobs only."""
    return {"status": "success", "data": codex_job_manager.list_active()}


@router.get("/app-server")
async def get_codex_app_server_status() -> Dict[str, Any]:
    """Return lifecycle/protocol status for the shared Codex App Server."""
    from app.services.codex_app_server import get_codex_app_server_manager

    return {
        "status": "success",
        "data": get_codex_app_server_manager().status(),
    }


@router.get("/{request_id}")
async def get_codex_job(request_id: str) -> Dict[str, Any]:
    job = codex_job_manager.get_active(request_id)
    if job:
        return {"status": "success", "data": job}
    for recent in codex_job_manager.list_recent(limit=200):
        if recent.get("request_id") == request_id:
            return {"status": "success", "data": recent}
    raise HTTPException(status_code=404, detail="Codex job not found")


@router.post("/{request_id}/cancel")
async def cancel_codex_job(request_id: str) -> Dict[str, Any]:
    result = await codex_job_manager.cancel(request_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("message") or "Codex job not found")
    return {"status": "success", "data": result}
