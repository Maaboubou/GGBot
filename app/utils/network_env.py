"""启动阶段的网络环境修正。"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Iterable


LOCAL_BYPASS_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")

# LiteLLM 默认从该域名获取 model_prices_and_context_window.json。
# 只让这个静态成本表地址直连，其他外部请求仍沿用系统代理。
LITELLM_DIRECT_HOSTS = ("raw.githubusercontent.com",)


def ensure_proxy_bypass(hosts: Iterable[str]) -> None:
    """把指定主机幂等加入大小写两套 NO_PROXY 环境变量。"""
    requested_hosts = [host.strip() for host in hosts if host and host.strip()]

    for key in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(key, "")
        parts = [part.strip() for part in existing.split(",") if part.strip()]
        known = {part.casefold() for part in parts}

        for host in requested_hosts:
            normalized = host.casefold()
            if normalized not in known:
                parts.append(host)
                known.add(normalized)

        os.environ[key] = ",".join(parts)


def configure_startup_network_environment() -> None:
    """配置 Web 启动所需的本机访问和 LiteLLM 成本表直连。"""
    ensure_proxy_bypass((*LOCAL_BYPASS_HOSTS, *LITELLM_DIRECT_HOSTS))


def preload_litellm_cost_map_direct() -> None:
    """首次导入 LiteLLM 时，仅让成本表下载显式忽略环境代理。"""
    if "litellm" in sys.modules:
        return

    import httpx

    original_get = httpx.get
    # LiteLLM 在导入阶段通过 httpx.get() 下载成本表。这一小段启动代码尚未
    # 创建工作线程，因此可以安全地临时替换顶层调用，并在导入后立即恢复。
    with httpx.Client(trust_env=False) as direct_client:
        httpx.get = direct_client.get
        try:
            importlib.import_module("litellm")
        finally:
            httpx.get = original_get
