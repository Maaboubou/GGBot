"""
插件管理API端点
"""

from typing import Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.plugin_manager import PluginManager
from app.core.wechat_manager import WeChatManager
from app.dependencies import get_plugin_manager_instance, get_wechat_manager_instance
from app.services.capability_service import (
    CapabilityConfigError,
    CapabilityService,
    public_plugin_config,
)


router = APIRouter()


class PluginToggleRequest(BaseModel):
    enabled: bool


class PluginConfigUpdateRequest(BaseModel):
    config: Dict[str, Any]


@router.get("/")
async def list_plugins(
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance)
) -> Dict[str, Any]:
    """列出所有插件"""
    try:
        plugins = plugin_manager.list_plugins()
        return {
            "plugins": plugins,
            "total": len(plugins)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats")
async def get_plugin_stats(
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance),
    wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)
) -> Dict[str, Any]:
    """获取插件统计信息，包含用户级的监听详情"""
    try:
        # 1. 获取全局插件统计 (Global: EventType -> [PluginName])
        stats = plugin_manager.get_stats()
        
        # 2. 如果缺少监听器详情（可能PluginManager未更新），则只返回基本信息
        if 'listeners_by_type' not in stats:
            return {"stats": stats}

        global_event_plugins = stats['listeners_by_type'] # { 'text_message': ['pluginA', 'pluginB'] }
        
        # 3. 获取所有用户权限和活跃监听列表
        # 需要访问数据库，这里使用临时Session
        from app.models.base import SessionLocal
        from app.models.user_permission import WeChatUser
        
        user_listeners_by_type = {} # { 'text_message': { 'UserA': ['pluginA'] } }
        
        db = SessionLocal()
        try:
            # 获取所有用户及其权限
            users = db.query(WeChatUser).all()
            
            # 获取当前实际正在监听的聊天列表 (StringSet)
            active_chats = set()
            if wechat_manager:
                active_list = wechat_manager.get_listened_chats()
                # active_list 可能是 dict(chat_name: info) 或 list
                if isinstance(active_list, dict):
                    active_chats = set(active_list.keys())
                elif isinstance(active_list, list):
                    active_chats = set(active_list)

            # 遍历每个事件类型
            for event_type, plugins in global_event_plugins.items():
                user_listeners_by_type[event_type] = {}
                
                # 特殊处理：系统事件不属于特定用户
                if any(x in event_type for x in ['system', 'plugin', 'shutdown', 'startup']):
                    # 这些事件归类为 "System"
                    user_listeners_by_type[event_type]['System'] = plugins
                    continue

                # 遍历用户，检查是否有权限
                for user in users:
                    # 必须是活跃监听的聊天
                    if user.chat_name not in active_chats:
                        continue
                        
                    user_perms = {p.plugin_name for p in user.permissions}
                    allowed_plugins = []
                    
                    for plugin_name in plugins:
                        # 简单的权限检查逻辑 (需与 EventBus 保持一致)
                        if plugin_name in user_perms:
                            allowed_plugins.append(plugin_name)
                        # 支持多级目录简名匹配 (e.g. feishu/demo -> demo)
                        elif plugin_name.split('/')[-1] in user_perms:
                            allowed_plugins.append(plugin_name)
                            
                    if allowed_plugins:
                        user_listeners_by_type[event_type][user.chat_name] = allowed_plugins
        finally:
            db.close()
            
        # 将新的聚合数据添加到 stats
        stats['user_listeners_by_type'] = user_listeners_by_type
        
        return {"stats": stats}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{plugin_name:path}/config")
async def get_plugin_config(
    plugin_name: str,
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance)
) -> Dict[str, Any]:
    """读取插件配置文件 config.json 内容"""
    try:
        settings = CapabilityService(plugin_manager).get_settings(plugin_name)
        if settings is None:
            raise HTTPException(status_code=404, detail=f"Config not found for plugin '{plugin_name}'")
        return {
            "plugin_name": plugin_name,
            "deprecated": True,
            "message": "Use /api/capabilities/settings/{id}",
            "settings": settings,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{plugin_name:path}/config")
async def update_plugin_config(
    plugin_name: str,
    update_request: PluginConfigUpdateRequest,
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance)
) -> Dict[str, Any]:
    """写入插件配置文件，并重载插件使其生效"""
    try:
        result = CapabilityService(plugin_manager).update_settings(
            plugin_name, update_request.config
        )
        if result is None:
            raise HTTPException(status_code=404, detail=f"Plugin '{plugin_name}' not found")
        return {
            "message": f"Config for '{plugin_name}' updated and reloaded",
            "plugin_name": plugin_name,
            "settings": result,
        }
    except CapabilityConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{plugin_name:path}")
async def get_plugin_info(
    plugin_name: str,
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance)
) -> Dict[str, Any]:
    """获取单个插件信息"""
    try:
        plugin_info = plugin_manager.get_plugin_info(plugin_name)
        if not plugin_info:
            raise HTTPException(status_code=404, detail=f"Plugin '{plugin_name}' not found")
        
        # 获取监听器信息
        listeners = plugin_manager.get_plugin_listeners(plugin_name)
        return {
            "plugin": {
                "name": plugin_info.name,
                "version": plugin_info.version,
                "description": plugin_info.description,
                "author": plugin_info.author,
                "enabled": plugin_info.enabled,
                "loaded": plugin_info.loaded,
                "listener_count": len(plugin_info.listener_ids),
                "listeners": listeners
            },
            "config": public_plugin_config(plugin_info.config or {})
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{plugin_name:path}/toggle")
async def toggle_plugin(
    plugin_name: str,
    toggle_request: PluginToggleRequest,
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance)
) -> Dict[str, Any]:
    """启用/禁用插件"""
    try:
        plugin_info = plugin_manager.get_plugin_info(plugin_name)
        if not plugin_info:
            raise HTTPException(status_code=404, detail=f"Plugin '{plugin_name}' not found")
        
        if toggle_request.enabled:
            success = plugin_manager.enable_plugin(plugin_name)
            action = "enabled"
        else:
            success = plugin_manager.disable_plugin(plugin_name)
            action = "disabled"
        
        if success:
            try:
                from app.services.runtime_operations import get_runtime_operation_service

                get_runtime_operation_service().record_audit(
                    category="plugin_lifecycle",
                    action="toggle_plugin",
                    target=plugin_name,
                    summary=f"插件已{action}",
                    before={"enabled": not toggle_request.enabled},
                    after={"enabled": toggle_request.enabled},
                )
            except Exception:
                pass
            return {
                "message": f"Plugin '{plugin_name}' {action} successfully",
                "plugin_name": plugin_name,
                "enabled": toggle_request.enabled
            }
        else:
            raise HTTPException(status_code=500, detail=f"Failed to {action.replace('ed', '')} plugin")
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{plugin_name:path}/reload")
async def reload_plugin(
    plugin_name: str,
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance)
) -> Dict[str, Any]:
    """重新加载插件"""
    try:
        success = plugin_manager.reload_plugin(plugin_name)
        
        if success:
            try:
                from app.services.runtime_operations import get_runtime_operation_service

                get_runtime_operation_service().record_audit(
                    category="plugin_lifecycle",
                    action="reload_plugin",
                    target=plugin_name,
                    summary="插件已重新加载",
                )
            except Exception:
                pass
            return {
                "message": f"Plugin '{plugin_name}' reloaded successfully",
                "plugin_name": plugin_name
            }
        else:
            raise HTTPException(status_code=500, detail=f"Failed to reload plugin '{plugin_name}'")
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{plugin_name:path}/listeners")
async def get_plugin_listeners(
    plugin_name: str,
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance)
) -> Dict[str, Any]:
    """获取插件的监听器信息"""
    try:
        plugin_info = plugin_manager.get_plugin_info(plugin_name)
        if not plugin_info:
            raise HTTPException(status_code=404, detail=f"Plugin '{plugin_name}' not found")
        
        listeners = plugin_manager.get_plugin_listeners(plugin_name)
        
        return {
            "plugin_name": plugin_name,
            "listeners": listeners,
            "total": len(listeners)
        }
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
