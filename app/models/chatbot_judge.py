#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ChatBot Judge 配置的数据模型
"""

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base


class ChatBotJudge(Base):
    """ChatBot Judge 定义"""
    __tablename__ = "chatbot_judges"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    display_name = Column(String, nullable=False)
    prompt = Column(Text, nullable=False)
    prompt_mode = Column(String, nullable=False, default="simple")  # simple | template
    description = Column(String, nullable=True)
    trigger_msg_threshold = Column(Integer, default=5, nullable=False)  # 距离上次机器人回复后，至少积攒多少条消息才咨询 Judge
    trigger_interval_minutes = Column(Integer, default=1, nullable=False)  # 距离上次机器人回复后，至少间隔多少分钟才咨询 Judge
    cooldown_msg_threshold = Column(Integer, default=5, nullable=False)  # Judge 拒绝后，至少新增多少条消息才重试
    cooldown_minutes = Column(Integer, default=1, nullable=False)  # Judge 拒绝后，至少冷却多少分钟才重试
    is_builtin = Column(String, default="false", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user_judges = relationship("UserChatBotJudge", back_populates="judge")

    def __repr__(self):
        return f"<ChatBotJudge(name='{self.name}', display_name='{self.display_name}')>"


class UserChatBotJudge(Base):
    """用户 ChatBot Judge 绑定"""
    __tablename__ = "user_chatbot_judges"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_chatbot_judge_user_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("wechat_users.id"), nullable=False)
    judge_id = Column(Integer, ForeignKey("chatbot_judges.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    judge = relationship("ChatBotJudge", back_populates="user_judges")

    def __repr__(self):
        return f"<UserChatBotJudge(user_id={self.user_id}, judge_id={self.judge_id})>"
