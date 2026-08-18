#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
系统设置服务 - 读写数据库中的配置
"""

from sqlalchemy.orm import Session
from app.models.setting import Setting
from functools import lru_cache

class SettingsService:
    def __init__(self, db_session: Session):
        self.db = db_session

    def get(self, key: str, default: str = None) -> str:
        """获取单个设置项，带缓存"""
        return self._get_from_db_cached(key, default)

    @lru_cache(maxsize=128)
    def _get_from_db_cached(self, key: str, default: str = None) -> str:
        """从数据库获取并缓存"""
        setting = self.db.query(Setting).filter(Setting.key == key).first()
        return setting.value if setting else default

    def set(self, key: str, value: str, description: str = None, category: str = "default"):
        """创建或更新设置项"""
        setting = self.db.query(Setting).filter(Setting.key == key).first()
        if setting:
            setting.value = value
            if description:
                setting.description = description
            if category:
                setting.category = category
        else:
            setting = Setting(key=key, value=value, description=description, category=category)
            self.db.add(setting)
        
        self.db.commit()
        self.db.refresh(setting)
        
        # 清除此键的缓存
        self._get_from_db_cached.cache_clear()
        
        return setting

    def delete(self, key: str) -> bool:
        """删除指定键的设置项"""
        setting = self.db.query(Setting).filter(Setting.key == key).first()
        if setting:
            self.db.delete(setting)
            self.db.commit()
            self._get_from_db_cached.cache_clear()
            # 同时也清除 config_service.py 中的全局缓存
            try:
                from app.services.config_service import _settings_cache
                if key in _settings_cache:
                    del _settings_cache[key]
            except Exception:
                pass
            return True
        return False

    def get_all(self) -> list[Setting]:
        """获取所有设置项"""
        return self.db.query(Setting).all()

def get_settings_service(db: Session):
    """依赖注入函数"""
    return SettingsService(db)
