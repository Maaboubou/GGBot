#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ChatBot角色配置的数据模型
"""

from sqlalchemy import Boolean, Column, Float, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .base import Base


class ChatBotRole(Base):
    """ChatBot角色定义"""
    __tablename__ = "chatbot_roles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)  # 角色名称
    display_name = Column(String, nullable=False)  # 显示名称
    prompt = Column(Text, nullable=False)  # 角色提示词
    description = Column(String, nullable=True)  # 角色描述
    output_split_enabled = Column(Boolean, default=False, nullable=False)  # 是否启用拟人化分条回复
    output_max_chars = Column(Integer, default=120, nullable=False)  # 单条回复建议最大字数
    output_max_count = Column(Integer, default=3, nullable=False)  # 最多发送条数
    output_strip_trailing_period = Column(Boolean, default=True, nullable=False)  # 去掉结尾句号
    output_interval_seconds = Column(Float, default=1.0, nullable=False)  # 分条发送间隔
    is_builtin = Column(String, default="false", nullable=False)  # 是否为内置角色
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 关联用户角色配置
    user_roles = relationship("UserChatBotRole", back_populates="role")

    def __repr__(self):
        return f"<ChatBotRole(name='{self.name}', display_name='{self.display_name}')>"


class UserChatBotRole(Base):
    """用户ChatBot角色配置"""
    __tablename__ = "user_chatbot_roles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("wechat_users.id"), nullable=False)  # 用户ID
    role_id = Column(Integer, ForeignKey("chatbot_roles.id"), nullable=False)  # 角色ID
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 关联 - 暂时注释掉以解决循环导入问题
    # user = relationship("WeChatUser", back_populates="chatbot_role")
    role = relationship("ChatBotRole", back_populates="user_roles")

    def __repr__(self):
        return f"<UserChatBotRole(user_id={self.user_id}, role_id={self.role_id})>"
