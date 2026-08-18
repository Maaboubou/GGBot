"""Message routing workbench endpoints."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.plugin_manager import PluginManager
from app.dependencies import get_db, get_plugin_manager_instance
from app.services.automation_routing_service import (
    AutomationRoutingError,
    AutomationRoutingService,
)


router = APIRouter()


class RouteOrderRequest(BaseModel):
    listener_keys: List[str] = Field(default_factory=list)
    expected_signature: Optional[str] = None


def get_routing_service(
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance),
    db: Session = Depends(get_db),
) -> AutomationRoutingService:
    return AutomationRoutingService(plugin_manager, db)


@router.get("/overview")
def get_automation_routing_overview(
    chat_id: Optional[int] = Query(default=None),
    mentioned: bool = Query(default=True),
    service: AutomationRoutingService = Depends(get_routing_service),
) -> Dict[str, Any]:
    try:
        return service.overview(chat_id=chat_id, mentioned=mentioned)
    except AutomationRoutingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/events/{event_type}/order")
def update_automation_route_order(
    event_type: str,
    request: RouteOrderRequest,
    service: AutomationRoutingService = Depends(get_routing_service),
) -> Dict[str, Any]:
    try:
        return service.apply_order(
            event_type,
            request.listener_keys,
            expected_signature=request.expected_signature,
        )
    except AutomationRoutingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"执行顺序未应用：{exc}") from exc
