#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微信用户和权限的数据模型
"""

from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Text
from sqlalchemy.orm import relationship
from .base import Base

class WeChatUser(Base):
    __tablename__ = "wechat_users"

    id = Column(Integer, primary_key=True, index=True)
    chat_name = Column(String, unique=True, index=True, nullable=False)
    remark = Column(String, nullable=True)
    is_group = Column(Boolean, default=False)
    sender_blacklist = Column(Text, nullable=True)  # 当前群/私聊内全局 sender 黑名单（JSON array）
    # 群内 @ 名称与微信账号的全局显示名并不总是相同。手动值始终作为
    # 备用别名；自动值只从微信群详情的“我在本群的昵称”字段读取。
    bot_group_nickname = Column(String, nullable=True)
    bot_group_nickname_auto_enabled = Column(Boolean, default=True)
    bot_group_nickname_detected = Column(String, nullable=True)
    bot_group_nickname_checked_at = Column(String, nullable=True)

    permissions = relationship("UserPermission", back_populates="user", cascade="all, delete-orphan")
    # chatbot_role = relationship("UserChatBotRole", back_populates="user", uselist=False, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<WeChatUser(chat_name='{self.chat_name}', remark='{self.remark}')>"

class UserPermission(Base):
    __tablename__ = "user_permissions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("wechat_users.id"), nullable=False)
    plugin_name = Column(String, nullable=False)
    require_mention = Column(Boolean, default=False)  # 是否需要@机器人才能触发
    proactive_enabled = Column(Boolean, default=False)  # 是否启用主动回复
    followup_enabled = Column(Boolean, default=False)  # Bot 回复后的无 @ 连续对话
    followup_window_seconds = Column(Integer, default=60)  # 回复后续聊有效窗口
    followup_merge_seconds = Column(Integer, default=3)  # 连续消息合并等待
    followup_max_turns = Column(Integer, default=3)  # 最多连续无 @ 回复轮数

    memory_profile = Column(Text, nullable=True)  # builtin_chatbot 每群记忆覆盖配置（JSON）
    ignored_senders = Column(Text, nullable=True)  # builtin_chatbot 每群 sender 黑名单（JSON array）

    user = relationship("WeChatUser", back_populates="permissions")

    def __repr__(self):
        return f"<UserPermission(user_id={self.user_id}, plugin='{self.plugin_name}', require_mention={self.require_mention})>"
