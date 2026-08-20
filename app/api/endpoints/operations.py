"""Unified managed-operation and plugin-runtime observability endpoints."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services.plugin_runtime import get_plugin_runtime_registry
from app.services.incident_service import get_incident_service
from app.services.runtime_operations import get_runtime_operation_service
from app.services.storage_service import StorageError, get_storage_service


router = APIRouter()


class StorageCleanupRequest(BaseModel):
    retention_days: int = 7
    confirmation: str


@router.get("/")
def list_operations(
    limit: int = Query(100, ge=1, le=500),
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    service = get_runtime_operation_service()
    return {
        "operations": service.list(limit=limit, owner=owner),
        "stats": service.stats(),
    }


@router.get("/runtime")
def runtime_overview() -> Dict[str, Any]:
    registry = get_plugin_runtime_registry()
    plugins = registry.snapshot()
    return {
        "plugin_runtime_api_version": 2,
        "plugins": plugins,
        "summary": {
            "total": len(plugins),
            "managed": len(plugins),
            "unhealthy": sum(
                (item.get("health") or {}).get("status") in {"unhealthy", "failed"}
                for item in plugins
            ),
        },
    }


@router.get("/incidents")
def list_incidents(
    limit: int = Query(50, ge=1, le=200),
    scan_lines: int = Query(10000, ge=100, le=50000),
    level: Optional[str] = None,
) -> Dict[str, Any]:
    incidents = get_incident_service().list(
        limit=limit,
        scan_lines=scan_lines,
        level=level,
    )
    return {
        "incidents": incidents,
        "summary": {
            "groups": len(incidents),
            "errors": sum(item.get("level") in {"ERROR", "CRITICAL"} for item in incidents),
            "occurrences": sum(int(item.get("count") or 0) for item in incidents),
        },
    }


@router.get("/audit")
def list_audit(
    limit: int = Query(100, ge=1, le=500),
    category: Optional[str] = None,
) -> Dict[str, Any]:
    records = get_runtime_operation_service().list_audit(
        limit=limit,
        category=category,
    )
    return {"records": records, "count": len(records)}


@router.get("/storage")
def storage_overview() -> Dict[str, Any]:
    return get_storage_service().overview()


@router.post("/storage/scan")
def scan_storage() -> Dict[str, Any]:
    service = get_storage_service()
    operation = get_runtime_operation_service().submit(
        owner="system:storage",
        kind="storage_scan",
        title="扫描存储占用",
        target=lambda context: service.scan(context),
    )
    return {"operation": operation}


@router.get("/storage/cleanup-preview")
def storage_cleanup_preview(retention_days: int = Query(7, ge=0, le=3650)) -> Dict[str, Any]:
    return get_storage_service().cleanup_preview(retention_days)


@router.post("/storage/cleanup")
def cleanup_storage(request: StorageCleanupRequest) -> Dict[str, Any]:
    service = get_storage_service()
    try:
        result = service.cleanup_managed(
            retention_days=max(0, min(int(request.retention_days), 3650)),
            confirmation=request.confirmation,
        )
    except StorageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    get_runtime_operation_service().record_audit(
        category="storage",
        action="cleanup_managed_storage",
        target="managed_plugin_cache_and_temporary",
        summary=f"将 {result['moved_to_trash']} 个托管缓存文件移入回收区",
        after=result,
    )
    return result


@router.get("/{operation_id}")
def get_operation(operation_id: str) -> Dict[str, Any]:
    service = get_runtime_operation_service()
    operation = service.get(operation_id)
    if not operation:
        raise HTTPException(status_code=404, detail="操作不存在")
    return {
        "operation": operation,
        "events": service.events(operation_id),
    }


@router.post("/{operation_id}/cancel")
def cancel_operation(operation_id: str) -> Dict[str, Any]:
    result = get_runtime_operation_service().cancel(operation_id)
    if not result.get("success"):
        raise HTTPException(status_code=409, detail=result.get("message") or "无法取消操作")
    return result
