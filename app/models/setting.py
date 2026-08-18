#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
系统设置的数据模型
"""

import json
from sqlalchemy import Column, Integer, String, Text
from .base import Base

class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True, nullable=False)
    value = Column(Text, nullable=True)
    description = Column(String, nullable=True)
    category = Column(String, default="default", nullable=False) # 用于在UI中分组

    def get_value(self):
        """解析存储的值"""
        if self.value is None:
            return None
        try:
            return json.loads(self.value)
        except json.JSONDecodeError:
            return self.value

    def set_value(self, new_value):
        """设置并序列化值"""
        if isinstance(new_value, (dict, list, bool)) or new_value is None:
            self.value = json.dumps(new_value, ensure_ascii=False)
        else:
            self.value = str(new_value)

    def __repr__(self):
        return f"<Setting(key='{self.key}', value='{self.value[:30]}...')>"