"""
微信管理API端点
"""

from typing import Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.wechat_manager import WeChatManager
from app.models import user_permission as models_permission
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


def _set_listening_preference(db: Session, chat_name: str, enabled: bool) -> bool:
    """保存管理列表中聊天的监听意图；未纳入管理的临时监听不建库。"""
    user = (
        db.query(models_permission.WeChatUser)
        .filter(models_permission.WeChatUser.chat_name == chat_name)
        .first()
    )
    if user is None:
        return False
    user.listening_enabled = bool(enabled)
    db.commit()
    return True


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
async def add_listen_chat(
    request: ListenChatRequest,
    wechat_manager: WeChatManager = Depends(get_wechat_manager_instance),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """添加监听聊天"""
    try:
        preference_saved = _set_listening_preference(db, request.chat_name, True)
        if not wechat_manager:
            return {
                "success": preference_saved,
                "runtime_success": False,
                "preference_saved": preference_saved,
                "message": "已启用自动监听，等待微信服务连接" if preference_saved else "WeChat manager not initialized",
                "chat_name": request.chat_name,
            }

        runtime_success = wechat_manager.add_listen_chat(request.chat_name, request.exact)
        return {
            "success": runtime_success or preference_saved,
            "runtime_success": runtime_success,
            "preference_saved": preference_saved,
            "message": (
                f"Successfully added listen chat: {request.chat_name}"
                if runtime_success
                else "已启用自动监听，当前连接恢复后会自动开始监听"
            ),
            "chat_name": request.chat_name,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/remove-listen-chat/{chat_name}")
async def remove_listen_chat(
    chat_name: str,
    wechat_manager: WeChatManager = Depends(get_wechat_manager_instance),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """移除监听聊天"""
    try:
        # 先持久化暂停意图。即使底层窗口正忙或已经断线，自动恢复也不能再加回来。
        preference_saved = _set_listening_preference(db, chat_name, False)
        if not wechat_manager:
            return {
                "success": preference_saved,
                "runtime_success": False,
                "preference_saved": preference_saved,
                "message": "已暂停自动监听；微信服务当前未连接" if preference_saved else "WeChat manager not initialized",
                "chat_name": chat_name,
            }

        runtime_success = wechat_manager.remove_listen_chat(chat_name)
        return {
            "success": runtime_success or preference_saved,
            "runtime_success": runtime_success,
            "preference_saved": preference_saved,
            "message": (
                f"Successfully removed listen chat: {chat_name}"
                if runtime_success
                else "已保存暂停状态；当前监听窗口未能立即关闭"
            ),
            "chat_name": chat_name,
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
