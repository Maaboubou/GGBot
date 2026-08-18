#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
权限管理相关的数据校验模型 (Pydantic)
"""
from pydantic import BaseModel
from typing import List, Optional

# --- UserPermission ---
class UserPermissionBase(BaseModel):
    plugin_name: str
    require_mention: bool = False
    proactive_enabled: bool = False
    followup_enabled: bool = False
    followup_window_seconds: int = 60
    followup_merge_seconds: int = 3
    followup_max_turns: int = 3
    memory_profile: Optional[str] = None
    ignored_senders: Optional[str] = None


class UserPermissionCreate(UserPermissionBase):
    pass

class UserPermission(UserPermissionBase):
    id: int
    user_id: int

    class Config:
        from_attributes = True


# --- WeChatUser ---
class WeChatUserBase(BaseModel):
    chat_name: str
    remark: Optional[str] = None
    is_group: bool = False
    sender_blacklist: Optional[str] = None
    bot_group_nickname: Optional[str] = None
    bot_group_nickname_auto_enabled: bool = True
    bot_group_nickname_detected: Optional[str] = None
    bot_group_nickname_checked_at: Optional[str] = None

class WeChatUserCreate(WeChatUserBase):
    pass

class WeChatUserUpdate(BaseModel):
    remark: Optional[str] = None
    is_group: Optional[bool] = None
    sender_blacklist: Optional[str] = None
    bot_group_nickname: Optional[str] = None
    bot_group_nickname_auto_enabled: Optional[bool] = None

class WeChatUser(WeChatUserBase):
    id: int
    permissions: List[UserPermission] = []

    class Config:
        from_attributes = True


# --- For updating permissions ---
class PermissionItem(BaseModel):
    plugin_name: str
    require_mention: bool = False
    proactive_enabled: bool = False
    followup_enabled: bool = False
    followup_window_seconds: int = 60
    followup_merge_seconds: int = 3
    followup_max_turns: int = 3
    memory_profile: Optional[str] = None
    ignored_senders: Optional[str] = None

class PermissionsUpdateRequest(BaseModel):
    permissions: List[PermissionItem]
