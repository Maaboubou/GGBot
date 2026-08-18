#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
系统设置相关的数据校验模型 (Pydantic)
"""
from pydantic import BaseModel
from typing import Optional, Any

class SettingBase(BaseModel):
    key: str
    value: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = "default"

class SettingCreate(SettingBase):
    pass

class SettingUpdate(BaseModel):
    value: str

class Setting(SettingBase):
    id: int
    
    class Config:
        from_attributes = True

class SettingPublic(SettingBase):
    """用于公开展示的模型，敏感信息会被处理"""
    value: Any # 可以是字符串或者隐藏提示
    is_sensitive: Optional[bool] = False  # 标识是否为敏感信息
