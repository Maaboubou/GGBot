"""选择器 profile 加载。

选择器必须以微信版本和语言为维度维护，V1 先固化 4.1.12 简体中文。
"""

from __future__ import annotations

import importlib.resources
from functools import lru_cache
from typing import Any

import yaml


@lru_cache(maxsize=None)
def load_profile(version: str = "4.1.12", locale: str = "cn") -> dict[str, Any]:
    """加载内置选择器 profile。"""
    filename = f"wechat_{version}_{locale}.yaml"
    try:
        text = importlib.resources.files("mabowx.selectors").joinpath(filename).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise KeyError(f"没有找到选择器 profile: {filename}") from exc
    return yaml.safe_load(text)


def get_selector(path: str, version: str = "4.1.12", locale: str = "cn") -> dict[str, Any]:
    """按点分路径读取选择器，例如 ``chat.input``。"""
    profile = load_profile(version, locale)
    node: Any = profile
    for part in path.split("."):
        node = node[part]
    if not isinstance(node, dict):
        raise TypeError(f"选择器路径 {path!r} 不是配置对象")
    return dict(node)
