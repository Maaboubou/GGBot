#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
配置服务 - 新架构实现
从数据库中读取设置
"""

import logging
import time
import os
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from app.models.base import SessionLocal
from app.models.setting import Setting

logger = logging.getLogger(__name__)

_settings_cache = {}
_cache_expiry = 5 * 60  # 5 minutes
_last_cache_time = 0

def get_setting(key: str, default=None, db: Session = None):
    """
    从数据库中获取一个设置项。
    为了性能，这里使用一个简单的缓存。
    """
    global _last_cache_time

    # 简单缓存逻辑
    if time.time() - _last_cache_time > _cache_expiry:
        _settings_cache.clear()
        _last_cache_time = time.time()

    if key in _settings_cache:
        return _settings_cache[key]

    # 如果没有传入db session，则创建一个新的
    db_needs_close = False
    if db is None:
        db = SessionLocal()
        db_needs_close = True

    try:
        setting = db.query(Setting).filter(Setting.key == key).first()
        if setting:
            _settings_cache[key] = setting.get_value()
            return setting.get_value()
        else:
            return default
    except Exception as e:
        logger.error(f"Error getting setting '{key}' from database: {e}")
        return default
    finally:
        if db_needs_close:
            db.close()

def update_setting(key: str, value, db: Session = None):
    """
    更新或创建一个设置项。
    """
    # 如果没有传入db session，则创建一个新的
    db_needs_close = False
    if db is None:
        db = SessionLocal()
        db_needs_close = True

    try:
        setting = db.query(Setting).filter(Setting.key == key).first()
        if setting:
            setting.set_value(value)
        else:
            setting = Setting(key=key)
            setting.set_value(value)
            db.add(setting)
        
        db.commit()
        
        # 更新缓存
        _settings_cache[key] = value
        
        return True
    except Exception as e:
        logger.error(f"Error updating setting '{key}': {e}")
        db.rollback()
        return False
    finally:
        if db_needs_close:
            db.close()

def reload_from_env():
    """
    从.env文件重新加载配置到数据库
    用于热重载配置，会读取 .env 中的所有非空键值对
    """
    logger.info("🔄 正在从.env文件重新加载配置...")
    
    # 加载.env文件
    env_path = os.path.join(os.getcwd(), ".env")
    
    if not os.path.exists(env_path):
        logger.warning(f"⚠️ 找不到 .env 文件: {env_path}")
        return 0
        
    # 直接使用 dotenv_values 读取字典形式，避免硬编码列表
    from dotenv import dotenv_values
    env_dict = dotenv_values(env_path)
    
    updated_count = 0
    if not env_dict:
        logger.warning("⚠️ .env 文件为空或解析失败")
        return 0
        
    for key, value in env_dict.items():
        if value is not None and value.strip() != "":
            if update_setting(key, value):
                logger.info(f"✅ 重新加载 {key}")
                updated_count += 1
            else:
                logger.error(f"❌ 重新加载 {key} 失败")
    
    # 清除缓存
    global _settings_cache, _last_cache_time
    _settings_cache.clear()
    _last_cache_time = 0
    
    logger.info(f"✅ 重新加载完成，更新了 {updated_count} 个配置项")
    return updated_count
