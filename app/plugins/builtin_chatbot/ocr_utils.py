# -*- coding: utf-8 -*-

import logging
from typing import Optional

from app.services.llm_manager import get_llm_manager
from app.utils.plugin_config import get_config


logger = logging.getLogger(__name__)


def get_ocr_result(image_base64: str) -> Optional[str]:
    """使用大模型识别图片中的文字和内容。"""
    try:
        llm_manager = get_llm_manager()
        ocr_prompt = get_config(
            "ocr_prompt",
            default=(
                "你是一个专业的图片识别内容识别引擎。任务是准确描述图片内容。"
                "回复格式:上述图片内容为:[图片内容描述]"
            ),
            plugin_name="builtin_chatbot",
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": ocr_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                        },
                    },
                ],
            },
        ]

        logger.info("🕒 正在调用大模型进行OCR图像识别...")
        result = llm_manager.call("builtin_chatbot", "ocr", messages)

        if isinstance(result, str):
            result = result.strip()
            if result:
                logger.info("✅ 大模型OCR识别成功")
                return result
        logger.warning("⚠️ 大模型OCR未返回有效识别结果")
    except Exception as error:
        logger.warning("⚠️ 大模型OCR识别失败: %s", error)

    return None
