#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Feishu 客户端（轻量SDK）
 - 鉴权（tenant/app access token）
 - Bitable 基础读写（分页获取、批量创建）
"""

from datetime import datetime, timedelta
from typing import List, Tuple, Optional, Dict, Any
import requests


class FeishuClient:
    """飞书轻量客户端"""

    def __init__(self, app_id: str, app_secret: str, app_type: str = "custom"):
        self.base_url = "https://open.feishu.cn/open-apis"
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_type = app_type  # "custom" or "store"
        self._token: Optional[str] = None
        self._expire_at: Optional[datetime] = None

    def get_access_token(self) -> str:
        if self._token and self._expire_at and datetime.now() < self._expire_at:
            return self._token

        path = "tenant_access_token" if self.app_type == "custom" else "app_access_token"
        url = f"{self.base_url}/auth/v3/{path}/internal"
        resp = requests.post(url, json={"app_id": self.app_id, "app_secret": self.app_secret}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Get token failed: {data}")

        key = "tenant_access_token" if self.app_type == "custom" else "app_access_token"
        token = data.get(key)
        if not token:
            raise RuntimeError("Empty token in response")

        expire_seconds = data.get("expire", 7200)
        self._token = token
        self._expire_at = datetime.now() + timedelta(seconds=expire_seconds - 300)
        return self._token

    def bitable_get_records(
        self,
        app_token: str,
        table_id: str,
        view_id: Optional[str] = None,
        page_size: int = 500,
        page_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], bool, str]:
        """获取一页记录"""
        token = self.get_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        params: Dict[str, Any] = {"page_size": page_size}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token
        url = f"{self.base_url}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Get records failed: {data}")
        d = data.get("data", {})
        return d.get("items", []), d.get("has_more", False), d.get("page_token", "")

    def bitable_batch_create(self, app_token: str, table_id: str, records: List[Dict[str, Any]]) -> bool:
        token = self.get_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        url = f"{self.base_url}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
        resp = requests.post(url, headers=headers, json={"records": records}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Batch create failed: {data}")
        return True

    def bitable_delete_record(self, app_token: str, table_id: str, record_id: str) -> bool:
        """删除单条记录"""
        token = self.get_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        url = f"{self.base_url}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
        resp = requests.delete(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Delete record failed: {data}")
        return True

    def bitable_update_record(self, app_token: str, table_id: str, record_id: str, fields: Dict[str, Any]) -> bool:
        """更新单条记录"""
        token = self.get_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        url = f"{self.base_url}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
        resp = requests.put(url, headers=headers, json={"fields": fields}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Update record failed: {data}")
        return True


