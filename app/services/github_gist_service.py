#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
GitHub Gist 服务（新架构）
- 读取 Token 优先来自数据库配置，其次 .env 环境变量
- 提供创建搜索结果 Gist 的能力
"""

from __future__ import annotations

import os
import time
import logging
from typing import Optional, List, Dict

import requests
from dotenv import load_dotenv

from app.services.config_service import get_setting


logger = logging.getLogger(__name__)


class GitHubGistService:
    """GitHub Gist 服务，用于创建和管理搜索结果分享"""

    def __init__(self) -> None:
        load_dotenv()

        # 优先从数据库读取，其次 .env
        github_token = get_setting("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")

        self.github_token = github_token or ""
        self.available = bool(self.github_token)

        if self.available:
            logger.info("✅ GitHub Gist 服务已启用")
        else:
            logger.warning("⚠️ GITHUB_TOKEN 未配置，Gist功能不可用")

        self.api_base = "https://api.github.com"
        self.headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
        }

        self.recent_gist_ids: List[str] = []
        self.max_keep_gists: int = 10

    # -------- Public APIs --------
    def create_search_result_gist(
        self,
        *,
        search_results: List[str],
        urls: List[str],
        ai_keywords_text: Optional[str] = None,
        chat_name: str = "用户",
        image_info: str = "",
        timeout: int = 30,
    ) -> Optional[str]:
        """
        创建包含搜索结果的 Secret Gist
        返回 Gist URL，失败返回 None
        """
        if not self.available:
            logger.error("❌ GitHub Gist 未启用（缺少 Token）")
            return None

        try:
            self._cleanup_old_gists()

            payload = {
                "description": "图片搜索结果",
                "public": False,
                "files": {
                    "image_search_results.md": {
                        "content": self._generate_markdown_content(
                            search_results=search_results,
                            urls=urls,
                            ai_keywords_text=ai_keywords_text,
                            chat_name=chat_name,
                            image_info=image_info,
                        )
                    }
                },
            }

            resp = self._request_with_retry(
                method="post",
                url=f"{self.api_base}/gists",
                headers=self.headers,
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 201:
                data = resp.json()
                gist_id = data.get("id", "")
                gist_url = data.get("html_url")
                if gist_id:
                    self.recent_gist_ids.append(gist_id)
                logger.info("✅ 成功创建 Gist: %s", gist_url)
                return gist_url
            logger.error("❌ 创建 Gist 失败: %s - %s", resp.status_code, resp.text)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error("❌ 创建 Gist 异常: %s", exc)
            return None

    def check_api_quota(self) -> Optional[Dict[str, int]]:
        if not self.available:
            return None
        try:
            resp = self._request_with_retry(
                method="get",
                url=f"{self.api_base}/rate_limit",
                headers=self.headers,
                timeout=10,
            )
            if resp.status_code == 200:
                info = resp.json().get("rate", {})
                return {
                    "remaining": info.get("remaining", 0),
                    "limit": info.get("limit", 0),
                    "reset_time": info.get("reset", 0),
                }
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error("检查 GitHub API 配额失败: %s", exc)
            return None

    # -------- Internal Helpers --------
    def _generate_markdown_content(
        self,
        *,
        search_results: List[str],
        urls: List[str],
        ai_keywords_text: Optional[str],
        chat_name: str,
        image_info: str,
    ) -> str:
        from datetime import datetime
        lines: List[str] = []
        lines.append("# 🔍 图片搜索结果报告")
        lines.append("")
        lines.append("## 📋 基本信息")
        lines.append(f"- **搜索时间**: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}")
        lines.append(f"- **来源群组**: {chat_name}")
        lines.append("- **生成方式**: AI图片识别搜索")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## 🏷️ 识别到的关键词")
        lines.append("")
        if ai_keywords_text and ai_keywords_text.strip():
            lines.append(ai_keywords_text.strip())
        else:
            if search_results:
                for idx, kw in enumerate(search_results, 1):
                    lines.append(f"{idx}. **{kw}**")
            else:
                lines.append("*未识别到明确关键词*")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## 🔗 相关链接")
        lines.append("")
        if urls:
            for idx, url in enumerate(urls, 1):
                domain = self._extract_domain(url)
                lines.append(f"{idx}. [{domain}]({url})")
        else:
            lines.append("*未找到相关链接*")
        if image_info:
            lines.append("")
            lines.append("---")
            lines.append("")
            lines.append("## ℹ️ 额外信息")
            lines.append("")
            lines.append(image_info)
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## ⚠️ 使用说明")
        lines.append("")
        lines.extend([
            "1. 内容来源: 本报告通过AI图片识别技术自动生成",
            "2. 准确性: 搜索结果仅供参考，请自行验证信息准确性",
            "3. 隐私: 此页面为私密链接，仅限知晓URL的用户访问",
            "4. 保留: 最多保存10次搜索结果",
        ])
        return "\n".join(lines)

    def _extract_domain(self, url: str) -> str:
        try:
            import re
            m = re.search(r"https?://([^/]+)", url)
            if m:
                domain = m.group(1)
                return re.sub(r"^www\\.", "", domain)
            return url
        except Exception:
            return url

    def _cleanup_old_gists(self) -> None:
        if len(self.recent_gist_ids) <= self.max_keep_gists:
            return
        need_delete = len(self.recent_gist_ids) - self.max_keep_gists
        to_delete = self.recent_gist_ids[:need_delete]
        deleted = 0
        for gist_id in to_delete:
            try:
                resp = self._request_with_retry(
                    method="delete",
                    url=f"{self.api_base}/gists/{gist_id}",
                    headers=self.headers,
                    timeout=10,
                )
                if resp.status_code == 204:
                    deleted += 1
                else:
                    logger.warning("删除 Gist 失败: %s (%s)", gist_id, resp.status_code)
            except Exception as exc:  # noqa: BLE001
                logger.warning("删除 Gist 异常: %s (%s)", gist_id, exc)
        if deleted:
            logger.info("🧹 已清理 %s 个旧 Gist", deleted)
        # 滚动窗口
        self.recent_gist_ids = self.recent_gist_ids[need_delete:]

    def _request_with_retry(
        self,
        *,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 10,
        max_retries: int = 3,
        backoff_factor: float = 1.5,
        **kwargs,
    ) -> requests.Response:
        method = method.lower().strip()
        last_error: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
                if 500 <= resp.status_code < 600:
                    raise requests.HTTPError(f"server error {resp.status_code}")
                return resp
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("request retry %s/%s failed: %s", attempt, max_retries, exc)
                if attempt < max_retries:
                    time.sleep(backoff_factor * attempt)
        assert last_error is not None
        raise last_error


