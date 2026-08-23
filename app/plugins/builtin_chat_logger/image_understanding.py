"""Image-content enrichment owned by the chat-log plugin."""

from __future__ import annotations

import logging
from typing import Optional

from app.services.llm_manager import get_llm_manager
from app.utils.plugin_config import get_config


logger = logging.getLogger(__name__)


DEFAULT_IMAGE_UNDERSTANDING_PROMPT = """你是聊天记录的图片内容补充工具。
请客观描述图片中的主要场景、对象、界面和可见文字，不要推测看不见的信息。
如果图片包含文字，尽量准确抄录；如果没有可辨认文字，也要描述视觉内容。
输出纯文本，先写“内容描述：”，需要时再写“可见文字：”。总长度控制在 500 字以内。"""


def understand_image(image_base64: str) -> Optional[str]:
    """Return a reusable textual enrichment for one image."""
    image_value = str(image_base64 or "").strip()
    if not image_value:
        return None
    if not image_value.startswith("data:image/"):
        image_value = f"data:image/jpeg;base64,{image_value}"

    prompt = str(
        get_config(
            "image_understanding_prompt",
            DEFAULT_IMAGE_UNDERSTANDING_PROMPT,
            plugin_name="builtin_chat_logger",
        )
        or DEFAULT_IMAGE_UNDERSTANDING_PROMPT
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_value}},
            ],
        }
    ]
    try:
        result = get_llm_manager().call(
            plugin_name="builtin_chat_logger",
            call_type="image_understanding",
            messages=messages,
            _wxautox_allow_image_input=True,
            _wxautox_require_image_input=True,
            _wxautox_disable_model_web_search=True,
            _wxautox_history_mode="image_enrichment",
        )
        text = str(result or "").strip()
        if text:
            logger.info("✅ 图片内容补充完成: %s 字符", len(text))
            return text[:4000]
        logger.warning("⚠️ 图片内容补充模型返回空结果")
    except Exception as exc:
        logger.warning("⚠️ 图片内容补充失败: %s", exc)
    return None
