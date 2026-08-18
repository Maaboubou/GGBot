#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
飞书 Bitable 高层服务
- 统一配置读取（数据库 settings）
- 统一缓存（按数据集 key 缓存记录）
- 统一读写入口（fetch_all / batch_create）

配置说明：
- 固定（来自 .env 并应已迁移至数据库）：
  FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TYPE, FEISHU_BITABLE_APP_TOKEN
- 可变（各插件可按需覆盖）：
  FEISHU_TABLE_ID, FEISHU_VIEW_ID（默认从数据库读；也可在调用时传参覆盖）

多插件隔离：
- fetch_all / batch_create 支持传入 table_id / view_id / app_token 覆盖；
- 缓存键包含覆盖参数，避免插件间串缓存。
"""

from datetime import datetime, timedelta
import time
from typing import Any, Dict, List, Optional

from app.services.config_service import get_setting
from app.integrations.feishu.client import FeishuClient


class FeishuBitableService:
    def __init__(self):
        self._cache: Dict[str, List[Dict[str, Any]]] = {}
        self._expire: Dict[str, datetime] = {}
        self._cache_ttl = timedelta(minutes=30)

        app_id = str(get_setting("FEISHU_APP_ID", "") or "")
        app_secret = str(get_setting("FEISHU_APP_SECRET", "") or "")
        app_type = str(get_setting("FEISHU_APP_TYPE", "custom") or "custom")
        if not app_id or not app_secret:
            # 懒初始化：若缺关键配置，等首次调用再报错
            self._client: Optional[FeishuClient] = None
        else:
            self._client = FeishuClient(app_id=app_id, app_secret=app_secret, app_type=app_type)

    def _get_client(self) -> FeishuClient:
        if self._client is None:
            app_id = str(get_setting("FEISHU_APP_ID", "") or "")
            app_secret = str(get_setting("FEISHU_APP_SECRET", "") or "")
            app_type = str(get_setting("FEISHU_APP_TYPE", "custom") or "custom")
            if not app_id or not app_secret:
                raise RuntimeError("缺少 FEISHU_APP_ID/FEISHU_APP_SECRET 配置")
            self._client = FeishuClient(app_id=app_id, app_secret=app_secret, app_type=app_type)
        return self._client

    def _get_app_token(self) -> str:
        app_token = str(get_setting("FEISHU_BITABLE_APP_TOKEN", "") or "")
        if not app_token:
            raise RuntimeError("缺少 FEISHU_BITABLE_APP_TOKEN 配置")
        return app_token

    def fetch_all(
        self,
        cache_key: str = "default",
        *,
        table_id: Optional[str] = None,
        view_id: Optional[str] = None,
        app_token: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """拉取表的所有记录。

        - 支持在调用时覆盖 table_id/view_id/app_token，避免全局配置冲突。
        - 缓存键会包含这些覆盖参数。
        """
        now = datetime.now()
        # 解析参数与缓存键
        resolved_app_token = str(app_token or self._get_app_token() or "")
        resolved_table_id = str(table_id or get_setting("FEISHU_TABLE_ID", "") or "")
        resolved_view_id = str(view_id or get_setting("FEISHU_VIEW_ID", "") or "") or None

        effective_cache_key = f"{cache_key}:{resolved_app_token}:{resolved_table_id}:{resolved_view_id or ''}"

        if effective_cache_key in self._cache and now < self._expire.get(effective_cache_key, now):
            return self._cache[effective_cache_key]

        if not resolved_table_id:
            raise RuntimeError("缺少 FEISHU_TABLE_ID 配置")

        client = self._get_client()

        items: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        while True:
            batch, has_more, page_token = client.bitable_get_records(
                app_token=resolved_app_token,
                table_id=resolved_table_id,
                view_id=resolved_view_id,
                page_size=500,
                page_token=page_token,
            )
            items.extend(batch)
            if not has_more:
                break
            time.sleep(0.1)

        self._cache[effective_cache_key] = items
        self._expire[effective_cache_key] = now + self._cache_ttl
        return items

    def batch_create(
        self,
        records: List[Dict[str, Any]],
        *,
        table_id: Optional[str] = None,
        app_token: Optional[str] = None,
    ) -> bool:
        """批量写入记录（支持覆盖表与 app_token）"""
        resolved_app_token = str(app_token or self._get_app_token() or "")
        resolved_table_id = str(table_id or get_setting("FEISHU_TABLE_ID", "") or "")
        if not resolved_table_id:
            raise RuntimeError("缺少 FEISHU_TABLE_ID 配置")
        client = self._get_client()
        return client.bitable_batch_create(app_token=resolved_app_token, table_id=resolved_table_id, records=records)

    def delete_record(
        self,
        record_id: str,
        *,
        table_id: Optional[str] = None,
        app_token: Optional[str] = None,
        clear_cache: bool = True,
    ) -> bool:
        """删除单条记录（支持覆盖表与 app_token）"""
        resolved_app_token = str(app_token or self._get_app_token() or "")
        resolved_table_id = str(table_id or get_setting("FEISHU_TABLE_ID", "") or "")
        if not resolved_table_id:
            raise RuntimeError("缺少 FEISHU_TABLE_ID 配置")
        client = self._get_client()
        
        # 删除记录
        result = client.bitable_delete_record(app_token=resolved_app_token, table_id=resolved_table_id, record_id=record_id)
        
        # 清除相关缓存，确保下次fetch_all能获取到最新数据
        if clear_cache and result:
            keys_to_remove = []
            for cache_key in self._cache.keys():
                if f":{resolved_app_token}:{resolved_table_id}:" in cache_key:
                    keys_to_remove.append(cache_key)
            for key in keys_to_remove:
                self._cache.pop(key, None)
                self._expire.pop(key, None)
        
        return result

    def update_record(
        self,
        record_id: str,
        fields: Dict[str, Any],
        *,
        table_id: Optional[str] = None,
        app_token: Optional[str] = None,
        clear_cache: bool = True,
    ) -> bool:
        """更新单条记录（支持覆盖表与 app_token）"""
        resolved_app_token = str(app_token or self._get_app_token() or "")
        resolved_table_id = str(table_id or get_setting("FEISHU_TABLE_ID", "") or "")
        if not resolved_table_id:
            raise RuntimeError("缺少 FEISHU_TABLE_ID 配置")
        client = self._get_client()

        # 更新记录
        result = client.bitable_update_record(app_token=resolved_app_token, table_id=resolved_table_id, record_id=record_id, fields=fields)

        # 清除相关缓存，确保下次fetch_all能获取到最新数据
        if clear_cache and result:
            keys_to_remove = []
            for cache_key in self._cache.keys():
                if f":{resolved_app_token}:{resolved_table_id}:" in cache_key:
                    keys_to_remove.append(cache_key)
            for key in keys_to_remove:
                self._cache.pop(key, None)
                self._expire.pop(key, None)

        return result


