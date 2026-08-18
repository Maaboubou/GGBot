"""
OpenAI服务封装
"""

import openai
from typing import Optional
from .config_service import get_setting


_openai_client_instance: Optional[openai.OpenAI] = None


def get_openai_client() -> openai.OpenAI:
    """获取OpenAI客户端单例"""
    global _openai_client_instance
    
    if _openai_client_instance is None:
        api_key = get_setting("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OpenAI API key not configured")
        
        _openai_client_instance = openai.OpenAI(api_key=api_key)
    
    return _openai_client_instance


def rebuild_client():
    """重建客户端（当配置更新时）"""
    global _openai_client_instance
    _openai_client_instance = None