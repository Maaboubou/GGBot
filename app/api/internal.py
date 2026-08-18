#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
内部API - 用于服务间通信
"""

import logging
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.core.event_bus import EventBus, Event, EventType
from app.dependencies import get_event_bus_instance, get_wechat_manager_instance
from app.core.wechat_manager import WeChatManager
from app.models.base import SessionLocal
from app.models.user_permission import WeChatUser

router = APIRouter()
logger = logging.getLogger(__name__)

def check_summary_permission(chat_name: str) -> bool:
    """
    检查聊天是否启用了摘要功能
    
    Args:
        chat_name: 聊天名称
        
    Returns:
        是否启用摘要功能
    """
    try:
        # 从数据库查询用户权限
        db = SessionLocal()
        try:
            user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
            if user:
                # 检查用户是否有摘要插件权限
                allowed_plugins = {p.plugin_name for p in user.permissions}
                summary_plugins = {"builtin_summary", "summary_plus"}
                # 支持多级目录插件名匹配（完整键）
                if allowed_plugins.intersection(summary_plugins):
                    return True
                # 检查末级简名匹配
                base_plugins = {p.rsplit('/', 1)[-1] for p in allowed_plugins}
                if base_plugins.intersection(summary_plugins):
                    return True
                return False
            else:
                # 如果用户不在权限表中，默认允许（保持向后兼容）
                logger.debug(f"用户 '{chat_name}' 不在权限表中，默认允许摘要功能")
                return True
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"⚠️ 检查摘要权限失败: {e}")
        # 如果检查失败，默认允许（保持向后兼容）
        return True

class WeChatMessage(BaseModel):
    content: str
    sender: str
    sender_id: Optional[str] = None
    sender_remark: Optional[str] = None
    chat_name: str
    is_group: bool
    type: str
    mtype: str
    message_id: Optional[str] = None
    url: Optional[str] = None
    quote_image_path: Optional[str] = None
    quote_content: Optional[str] = None
    has_quote_image: Optional[bool] = False
    timestamp: float

@router.post("/wechat_message")
async def receive_wechat_message(
    message: WeChatMessage,
    event_bus: EventBus = Depends(get_event_bus_instance),
    wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)
):
    """接收来自wx_bot的消息并发布到事件总线"""
    logger.debug(f"Received internal message from wx_bot: {message.content[:50]}")
    
    if not event_bus or not wechat_manager:
        logger.error("Event bus or WeChat manager not available")
        return {"status": "error", "message": "Core components not available"}

    # 根据消息类型确定事件类型
    # 优先根据显式URL判断为链接事件；其次检查mtype
    is_link_message = (message.url and isinstance(message.url, str) and message.url.startswith('http')) \
        or message.mtype == 'link' \
        or 'http' in message.content \
        or (message.type == 'other' and (message.content.strip().startswith('[链接]')))
    
    if is_link_message:
        # 对于链接消息，先检查用户是否启用了摘要功能
        if check_summary_permission(message.chat_name):
            event_type = EventType.LINK_MESSAGE_RECEIVED
            logger.info(f"🔗 检测到URL内容，用户已启用摘要功能，发布LINK_MESSAGE_RECEIVED事件")
        else:
            logger.info(f"🔗 检测到URL内容，但聊天 '{message.chat_name}' 未启用摘要功能，跳过链接事件发布")
            # 将链接消息当作普通文本消息处理
            event_type = EventType.TEXT_MESSAGE_RECEIVED
    elif message.mtype == 'text':
        event_type = EventType.TEXT_MESSAGE_RECEIVED
    elif message.mtype == 'image':
        event_type = EventType.IMAGE_MESSAGE_RECEIVED
    elif message.mtype == 'quote':
        # 细分引用消息类型
        quote_content = message.quote_content or ""
        if "[图片]" in quote_content:
            event_type = EventType.QUOTE_IMAGE_MESSAGE_RECEIVED
        else:
            event_type = EventType.QUOTE_TEXT_MESSAGE_RECEIVED
        
        # 同时发送通用的QUOTE事件以保持向后兼容
        general_quote_event = Event(
            type=EventType.QUOTE_MESSAGE_RECEIVED,
            source="wx_bot_internal",
            data={
                "message": message.content,
                "sender": message.sender,
                "sender_id": message.sender_id,
                "sender_remark": message.sender_remark,
                "chat_name": message.chat_name,
                "chat_type": "group" if message.is_group else "user",
                "message_type": message.mtype,
                "message_id": message.message_id,
                "url": message.url,
                "quote_image_path": message.quote_image_path,
                "quote_content": message.quote_content,
                "timestamp": message.timestamp
            },
            context={
                "wx": wechat_manager
            }
        )
        await event_bus.publish_async(general_quote_event)
    elif message.mtype == 'emotion':
        event_type = EventType.EMOTION_MESSAGE_RECEIVED
    elif message.mtype == 'voice':
        event_type = EventType.VOICE_MESSAGE_RECEIVED
    elif message.mtype == 'video':
        event_type = EventType.VIDEO_MESSAGE_RECEIVED
    elif message.mtype == 'file':
        event_type = EventType.FILE_MESSAGE_RECEIVED
    elif message.mtype == 'location':
        event_type = EventType.LOCATION_MESSAGE_RECEIVED
    elif message.mtype == 'merge':
        event_type = EventType.MERGE_MESSAGE_RECEIVED
    elif message.mtype == 'personal_card':
        event_type = EventType.PERSONAL_CARD_MESSAGE_RECEIVED
    elif message.mtype == 'note':
        event_type = EventType.NOTE_MESSAGE_RECEIVED
    elif message.mtype == 'other':
        event_type = EventType.OTHER_MESSAGE_RECEIVED
    else:
        # 对于未知类型，发布通用OTHER事件而不是默认为TEXT
        logger.warning(f"未知消息类型 '{message.mtype}', 发布为OTHER_MESSAGE_RECEIVED事件")
        event_type = EventType.OTHER_MESSAGE_RECEIVED

    event = Event(
        type=event_type,
        source="wx_bot_internal",
        data={
            "message": message.content,
            "sender": message.sender,
            "sender_id": message.sender_id,
            "sender_remark": message.sender_remark,
            "chat_name": message.chat_name,
            "chat_type": "group" if message.is_group else "user",
            "message_type": message.mtype,
            "message_id": message.message_id,
            "url": message.url,
            "quote_image_path": message.quote_image_path,
            "quote_content": message.quote_content,
            "has_quote_image": message.has_quote_image,
            "timestamp": message.timestamp
        },
        context={
            "wx": wechat_manager
        }
    )
    
    # 异步发布事件，避免阻塞请求
    await event_bus.publish_async(event)
    
    # 更新WeChatManager统计信息
    if wechat_manager:
        wechat_manager._stats['messages_received'] += 1
        wechat_manager._stats['last_message_time'] = message.timestamp
        
        # 更新聊天统计
        if message.chat_name in wechat_manager._listened_chats:
            wechat_manager._listened_chats[message.chat_name]['message_count'] += 1
    
    logger.info(f"Published {event.type.value} event for chat: {message.chat_name}")
    
    # 添加调试信息：检查用户权限
    from app.models.user_permission import WeChatUser
    from app.models.base import SessionLocal
    
    try:
        db = SessionLocal()
        user = db.query(WeChatUser).filter(WeChatUser.chat_name == message.chat_name).first()
        if user:
            plugin_permissions = [p.plugin_name for p in user.permissions]
            logger.info(f"🔧 User '{message.chat_name}' has permissions: {plugin_permissions}")
        else:
            logger.warning(f"⚠️ User '{message.chat_name}' not found in permissions table!")
    except Exception as e:
        logger.error(f"Error checking user permissions: {e}")
    finally:
        db.close()
        
    return {"status": "success"}

@router.get("/connectivity/websearch")
async def test_websearch_connectivity():
    """测试网络搜索相关模型的连通性"""
    import asyncio
    import litellm
    from litellm import completion
    from app.services.llm_manager import get_llm_manager

    llm_manager = get_llm_manager()
    mapping = llm_manager.config.get("plugin_mappings", {}).get("builtin_chatbot", {}).get("web_search", {})
    
    if not mapping:
        return {"status": "error", "message": "未找到网络搜索(builtin_chatbot.web_search)配置"}
        
    models_to_test = []
    
    # 提取Primary模型
    primary_id = mapping.get("primary")
    if primary_id:
        models_to_test.append({"role": "primary", "id": primary_id})
        
    # 提取Fallback模型
    for fb_id in mapping.get("fallback", []):
         models_to_test.append({"role": "fallback", "id": fb_id})

    if not models_to_test:
        return {"status": "error", "message": "网络搜索未配置任何模型(Primary/Fallback)"}

    results = []

    async def _test_single_model(model_info):
        model_id = model_info["id"]
        role = model_info["role"]
        
        model_cfg = llm_manager.config.get("models", {}).get(model_id)
        if not model_cfg:
            return {
                "role": role,
                "id": model_id,
                "model_name": "Unknown",
                "ok": False,
                "latency_ms": 0,
                "message": "模型配置不存在"
            }
            
        # 提取参数
        params = {
            "model": model_cfg["model"],
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 1,
            "timeout": 10
        }
        
        # 认证信息
        if "api_key" in model_cfg:
            resolved_key = llm_manager._resolve_env(model_cfg["api_key"])
            if resolved_key:
                params["api_key"] = resolved_key
        if "api_base" in model_cfg:
            resolved_base = llm_manager._resolve_env(model_cfg["api_base"])
            if resolved_base:
                params["api_base"] = resolved_base
                
        # 确保代理环境变量已生效
        llm_manager.apply_proxy_env_vars()
                
        import time
        t0 = time.perf_counter()
        try:
            # 运行在线程池中，因为litellm.completion是同步操作
            resp = await asyncio.to_thread(completion, **params)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            
            return {
                "role": role,
                "id": model_id,
                "model_name": model_cfg["model"],
                "ok": True,
                "latency_ms": latency_ms,
                "message": "Success"
            }
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            error_msg = str(e)
            # 简短化错误信息，以便在前端展示
            if len(error_msg) > 100:
                error_msg = error_msg[:100] + "..."
            
            return {
                "role": role,
                "id": model_id,
                "model_name": model_cfg["model"],
                "ok": False,
                "latency_ms": latency_ms,
                "message": f"{type(e).__name__}: {error_msg}"
            }

    # 并发测试所有模型
    tasks = [_test_single_model(info) for info in models_to_test]
    results = await asyncio.gather(*tasks)

    return {
        "status": "success",
        "data": results
    }

    # 并发测试所有模型
    tasks = [_test_single_model(info) for info in models_to_test]
    results = await asyncio.gather(*tasks)

    return {
        "status": "success",
        "data": results
    }
