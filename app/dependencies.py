#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
依赖注入 - 提供共享实例
"""
from fastapi import Request
from typing import Optional

from .core.event_bus import EventBus
from .core.plugin_manager import PluginManager
from .core.wechat_manager import WeChatManager
from .models.base import SessionLocal

def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_event_bus_instance(request: Request) -> Optional[EventBus]:
    """获取事件总线实例"""
    return request.app.state.event_bus if hasattr(request.app.state, 'event_bus') else None

def get_plugin_manager_instance(request: Request) -> Optional[PluginManager]:
    """获取插件管理器实例"""
    return request.app.state.plugin_manager if hasattr(request.app.state, 'plugin_manager') else None

def get_wechat_manager_instance(request: Request) -> Optional[WeChatManager]:
    """获取微信管理器实例"""
    return request.app.state.wechat_manager if hasattr(request.app.state, 'wechat_manager') else None
