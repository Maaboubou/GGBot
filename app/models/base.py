"""
数据库基础模型
"""

import logging
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker


logger = logging.getLogger(__name__)

# 数据库配置
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/database.db")

engine = create_engine(
    DATABASE_URL, 
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def drop_legacy_knowledge_base_columns(bind=None):
    """Permanently remove the retired per-chat knowledge-base configuration."""
    active_engine = bind or engine
    inspector = inspect(active_engine)
    if not inspector.has_table("user_permissions"):
        return []

    existing = {column["name"] for column in inspector.get_columns("user_permissions")}
    obsolete = [name for name in ("kb_enabled", "kb_id") if name in existing]
    if not obsolete:
        return []

    quote = active_engine.dialect.identifier_preparer.quote
    table_name = quote("user_permissions")
    with active_engine.begin() as connection:
        for column_name in obsolete:
            connection.execute(
                text(f"ALTER TABLE {table_name} DROP COLUMN {quote(column_name)}")
            )
            logger.info("已删除弃用的知识库字段: user_permissions.%s", column_name)
    return obsolete


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    """创建所有表"""
    # 导入所有模型以确保它们被注册
    from . import setting
    from . import user_permission  # 先导入WeChatUser
    from . import chatbot_role    # 后导入引用WeChatUser的模型
    from . import chatbot_judge   # Judge 配置与用户绑定
    
    Base.metadata.create_all(bind=engine)
    drop_legacy_knowledge_base_columns(engine)
