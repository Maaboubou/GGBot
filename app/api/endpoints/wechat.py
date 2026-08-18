"""
微信管理API端点
"""

from typing import Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.wechat_manager import WeChatManager
# from app.core.chat_initializer import ChatInitializer # 暂时禁用
from app.dependencies import get_wechat_manager_instance, get_db
from app.services.config_service import get_setting, update_setting


router = APIRouter()


class ListenChatRequest(BaseModel):
    chat_name: str
    exact: bool = False


class SendMessageRequest(BaseModel):
    chat_name: str
    message: str
    at_users: List[str] = None


@router.get("/status")
async def get_wechat_status(wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)) -> Dict[str, Any]:
    """获取微信状态"""
    try:
        if not wechat_manager:
            return {
                "status": "not_initialized",
                "message": "WeChat manager not initialized"
            }
        
        stats = wechat_manager.get_stats()
        return {
            "status": "connected" if stats["connected"] else "disconnected",
            "running": stats["running"],
            "stats": stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/my-info")
async def get_my_info(wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)) -> Dict[str, Any]:
    """获取当前用户信息"""
    try:
        if not wechat_manager:
            return {}
        
        return wechat_manager.get_my_info()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/listened-chats")
async def get_listened_chats(wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)) -> Dict[str, Any]:
    """获取正在监听的聊天列表"""
    try:
        if not wechat_manager:
            return {"listened_chats": {}, "message": "WeChat manager not initialized"}
        
        listened_chats = wechat_manager.get_listened_chats()
        return {"listened_chats": listened_chats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/add-listen-chat")
async def add_listen_chat(request: ListenChatRequest, wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)) -> Dict[str, Any]:
    """添加监听聊天"""
    try:
        if not wechat_manager:
            return {
                "success": False,
                "message": "WeChat manager not initialized",
                "chat_name": request.chat_name
            }
        
        success = wechat_manager.add_listen_chat(request.chat_name, request.exact)
        return {
            "success": success,
            "message": f"Successfully added listen chat: {request.chat_name}" if success else f"Failed to add listen chat: {request.chat_name}",
            "chat_name": request.chat_name
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/remove-listen-chat/{chat_name}")
async def remove_listen_chat(chat_name: str, wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)) -> Dict[str, Any]:
    """移除监听聊天"""
    try:
        if not wechat_manager:
            return {
                "success": False,
                "message": "WeChat manager not initialized",
                "chat_name": chat_name
            }
        
        success = wechat_manager.remove_listen_chat(chat_name)
        return {
            "success": success,
            "message": f"Successfully removed listen chat: {chat_name}" if success else f"Failed to remove listen chat: {chat_name}",
            "chat_name": chat_name
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/send-message")
async def send_message(request: SendMessageRequest, wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)) -> Dict[str, Any]:
    """发送消息"""
    try:
        if not wechat_manager:
            return {
                "success": False,
                "message": "WeChat manager not initialized"
            }
        
        success = wechat_manager.send_message(request.chat_name, request.message, request.at_users)
        return {
            "success": success,
            "message": "Message sent successfully" if success else "Failed to send message"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/friends")
async def get_friends(keywords: str = None, wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)) -> List[Dict[str, Any]]:
    """获取好友列表"""
    try:
        if not wechat_manager:
            return []
        
        return wechat_manager.get_all_friends(keywords)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/groups")
async def get_groups(wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)) -> List[Dict[str, Any]]:
    """获取群聊列表"""
    try:
        if not wechat_manager:
            return []
        
        return wechat_manager.get_recent_groups()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/enable-translation")
async def enable_translation(
    request: ListenChatRequest, 
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """为指定聊天启用翻译功能"""
    try:
        chat_name = request.chat_name
        
        # 获取当前的翻译启用列表
        enabled_chats = get_setting("plugin_translation_chats", [], db)
        
        if chat_name not in enabled_chats:
            enabled_chats.append(chat_name)
            update_setting("plugin_translation_chats", enabled_chats, db)
            message = f"Successfully enabled translation for {chat_name}"
        else:
            message = f"Translation is already enabled for {chat_name}"
            
        return {"success": True, "message": message}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# @router.post("/sync-chats")
# async def sync_chats(chat_initializer: ChatInitializer = Depends(get_chat_initializer_instance)) -> Dict[str, Any]:
#     """同步监听聊天"""
#     try:
#         if not chat_initializer:
#             return {
#                 "success": False,
#                 "message": "Chat initializer not initialized"
#             }
#        
#         results = chat_initializer.initialize_from_config()
#         return {
#             "success": True,
#             "message": "Chat synchronization completed",
#             "results": results
#         }
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))