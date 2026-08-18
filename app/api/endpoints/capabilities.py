"""Product-oriented capability endpoints for the management console."""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.plugin_manager import PluginManager
from app.dependencies import get_plugin_manager_instance
from app.models.base import get_db
from app.services.capability_service import CapabilityConfigError, CapabilityService


router = APIRouter()


class CapabilitySettingsPatch(BaseModel):
    values: Dict[str, Any] = Field(default_factory=dict)


def get_capability_service(
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance),
    db: Session = Depends(get_db),
) -> CapabilityService:
    return CapabilityService(plugin_manager, db)


@router.get("/")
def list_capabilities(
    service: CapabilityService = Depends(get_capability_service),
) -> Dict[str, Any]:
    capabilities = service.list_capabilities()
    return {"capabilities": capabilities, "total": len(capabilities)}


@router.get("/settings/{capability_id:path}")
def get_capability_settings(
    capability_id: str,
    service: CapabilityService = Depends(get_capability_service),
) -> Dict[str, Any]:
    result = service.get_settings(capability_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Capability not found")
    return result


@router.put("/settings/{capability_id:path}")
def update_capability_settings(
    capability_id: str,
    request: CapabilitySettingsPatch,
    service: CapabilityService = Depends(get_capability_service),
) -> Dict[str, Any]:
    try:
        result = service.update_settings(capability_id, request.values)
    except CapabilityConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"设置未应用：{exc}") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Capability not found")
    return {"message": "设置已保存并应用", "settings": result}


@router.get("/{capability_id:path}")
def get_capability(
    capability_id: str,
    service: CapabilityService = Depends(get_capability_service),
) -> Dict[str, Any]:
    capability = service.get_capability(capability_id)
    if capability is None:
        raise HTTPException(status_code=404, detail="Capability not found")
    return {"capability": capability}
