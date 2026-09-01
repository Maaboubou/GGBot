"""
图片编辑插件主模块
使用Gemini API进行图片编辑，支持多张图片合成和文字描述
"""

import json
import base64
import mimetypes
import os
import time
import threading
import logging
import tempfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from app.core.event_bus import Event, EventType
from app.services.config_service import get_setting
from app.utils.plugin_config import get_config

# 导入Gemini相关模块
try:
    from google import genai
    from google.genai import types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    logging.warning("google-genai 包未安装，图片编辑插件将无法正常工作")

try:
    from . import apiqik_image2 as apiqik_core
    APIQIK_AVAILABLE = True
except ImportError:
    apiqik_core = None
    APIQIK_AVAILABLE = False
    logging.warning("apiqik_image2 模块不可用，APIQIK 图片模式将无法正常工作")

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@dataclass
class ImageDownload:
    """图片下载任务"""
    message_id: str
    status: str = "pending"  # pending, downloading, completed, failed
    file_path: Optional[str] = None
    error: Optional[str] = None

@dataclass
class EditSession:
    """图片编辑会话"""
    user_sender: str
    chat_name: str
    wx_manager: Any  # 保存wx_manager引用用于发送消息
    required_images: int  # 需要收集的图片数量
    target_images: Optional[int] = None  # 本次最多生成图片数量；None表示使用配置默认值
    images: List[str] = None  # 存储base64编码的图片
    text_description: Optional[str] = None  # 存储用户输入的文字描述（只有一条）
    image_downloads: Dict[str, ImageDownload] = None  # 图片下载任务
    status: str = "collecting"  # collecting, processing, completed
    processed_immediately: bool = False  # 是否已被立即处理

    def __post_init__(self):
        if self.images is None:
            self.images = []
        if self.image_downloads is None:
            self.image_downloads = {}

    def is_complete(self) -> bool:
        """检查是否收集完成"""
        return (len(self.images) >= self.required_images and
                self.text_description is not None)

    def can_add_image(self) -> bool:
        """是否可以添加更多图片"""
        return len(self.images) < self.required_images


class ImageEditorPlugin:
    """图片编辑器插件主类"""

    def _parse_trigger_word(self, message: str) -> tuple[int, Optional[int], Optional[str], Optional[str]]:
        """解析触发词。

        返回(参考图片数量, 生成目标数量, 触发词, 剩余文字)，不匹配则返回
        (0, None, None, None)。

        生成目标数量使用“+数字”表达，例如：
        - "P图0 +5"  => 参考图0张，最多生成5张
        - "P图 2+5" => 参考图2张，最多生成5张
        “+数字”会从剩余文字里移除，避免进入图片提示词。
        """
        import re

        # 匹配模式：触发词 + 可选参考图数量 + 可选剩余文字
        pattern = rf"^{re.escape(self.trigger_keyword)}\s*(\d*)\s*(.*)$"
        match = re.match(pattern, message.strip(), re.DOTALL)

        if match:
            count = 1  # 默认1张参考图
            target_images = None
            number_str = match.group(1).strip()
            remaining_text = match.group(2).strip()

            # 可选生成数量：识别任意位置的“+数字”，如 “+5”、“ +5”、“描述 +5”
            target_match = re.search(r"\+(\d+)", remaining_text)
            if target_match:
                try:
                    target_images = max(1, int(target_match.group(1)))
                except ValueError:
                    target_images = None
                # 从提示词中移除第一个 +数字 参数，并整理空白
                remaining_text = (
                    remaining_text[:target_match.start()] + " " + remaining_text[target_match.end():]
                )
                remaining_text = re.sub(r"\s+", " ", remaining_text).strip()

            if number_str:
                try:
                    count = int(number_str)
                    # 限制在0到max_images张参考图之间
                    if count < 0:
                        count = 0
                    elif count > self.max_images:  # 使用配置的最大参考图数
                        count = self.max_images
                except ValueError:
                    pass  # 如果转换失败，则使用默认值1
            return count, target_images, self.trigger_keyword, (remaining_text or None)

        # 如果不匹配，则返回 None
        return 0, None, None, None

    def __init__(self, context=None):
        self.context = context
        # 从插件配置读取参数
        # 注意：enabled_chats 权限检查已移至 EventBus 统一管理
        plugin_name = "image_editor"
        self.trigger_keyword = get_config("trigger_keyword", plugin_name=plugin_name)
        self.collect_timeout = int(get_config("collect_timeout", plugin_name=plugin_name))
        self.max_images = int(get_config("max_images", plugin_name=plugin_name))
        self.mode = str(get_config("mode", "gemini", plugin_name=plugin_name) or "gemini").lower()
        self.model_name = get_config("model_name", plugin_name=plugin_name)
        self.processing_timeout = int(get_config("processing_timeout", plugin_name=plugin_name))
        self.apiqik_models = list(
            get_config(
                "apiqik_models",
                apiqik_core.DEFAULT_MODEL_SEQUENCE if apiqik_core else [],
                plugin_name=plugin_name,
            )
            or []
        )
        self.apiqik_concurrency = int(get_config("apiqik_concurrency", 3, plugin_name=plugin_name))
        self.apiqik_target_images = int(get_config("apiqik_target_images", 3, plugin_name=plugin_name))
        self.apiqik_total_timeout = int(
            get_config(
                "apiqik_total_timeout",
                get_config("apiqik_round_timeout", 300, plugin_name=plugin_name),
                plugin_name=plugin_name,
            )
        )
        self.apiqik_group = get_config("apiqik_group", "mabobot-image-editor", plugin_name=plugin_name)
        self.apiqik_base_url = get_config(
            "apiqik_base_url",
            apiqik_core.DEFAULT_BASE_URL if apiqik_core else "",
            plugin_name=plugin_name,
        )
        self.apiqik_env_file = get_config("apiqik_env_file", ".env", plugin_name=plugin_name)
        self.apiqik_upload_cache_path = Path(
            get_config(
                "apiqik_upload_cache_path",
                str(apiqik_core.DEFAULT_UPLOAD_CACHE_PATH) if apiqik_core else "data/image_editor_r2_upload_cache.json",
                plugin_name=plugin_name,
            )
        )
        self.apiqik_phash_distance = int(get_config("apiqik_phash_distance", 0, plugin_name=plugin_name))
        self.apiqik_output_dir = Path(
            get_config("apiqik_output_dir", "temp_apiqik_images", plugin_name=plugin_name)
        )
        if context is not None:
            migration_notes = []
            migration_notes.extend(context.storage.migrate_legacy_directory(
                self.apiqik_upload_cache_path,
                storage_class="cache",
                relative="r2_upload_cache.json",
            ))
            migration_notes.extend(context.storage.migrate_legacy_directory(
                self.apiqik_output_dir,
                storage_class="generated",
                relative="images",
            ))
            self.apiqik_upload_cache_path = context.storage.cache_path("r2_upload_cache.json")
            self.apiqik_output_dir = context.storage.generated_root / "images"
            if migration_notes:
                context.audit.record(
                    "storage_migration",
                    summary="图片编辑缓存与生成结果已迁移到插件标准存储目录",
                    details={"moved_files": len(migration_notes)},
                )
        self.client = None
        self.apiqik_api_key = None

        # 状态管理
        self._sessions: Dict[tuple[str, str], EditSession] = {}
        self._session_lock = threading.RLock()

        # 初始化客户端
        if self.mode == "apiqik":
            self._init_apiqik_client()
        elif GEMINI_AVAILABLE:
            self._init_gemini_client()
        else:
            logger.error("❌ google-genai 包未安装，Gemini 图片模式不可用")
        logger.info("🖼️ 图片编辑插件初始化完成")
        logger.info(f"   处理模式: {self.mode}")
        logger.info(f"   触发词: {self.trigger_keyword}")
        logger.info(f"   收集超时: {self.collect_timeout}秒")
        logger.info(f"   最大图片数: {self.max_images}张")

        # 调试：检查配置读取结果
        logger.debug(f"🖼️ 调试配置 - trigger_keyword: {self.trigger_keyword}")
        logger.debug(f"🖼️ 调试配置 - collect_timeout: {self.collect_timeout}")
        logger.debug(f"🖼️ 调试配置 - max_images: {self.max_images}")
        logger.debug(f"🖼️ 调试配置 - model_name: {self.model_name}")
        logger.debug(f"🖼️ 调试配置 - processing_timeout: {self.processing_timeout}")
        logger.debug(f"🖼️ 调试配置 - apiqik_models: {self.apiqik_models}")
        logger.debug(f"🖼️ 调试配置 - apiqik_total_timeout: {self.apiqik_total_timeout}")

    def _init_gemini_client(self):
        """初始化Gemini客户端"""
        try:
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                logger.error("❌ 未找到 GEMINI_API_KEY 环境变量")
                self.client = None
                return

            self.client = genai.Client(api_key=api_key)
            logger.info("✅ Gemini客户端初始化成功")
        except Exception as e:
            logger.error(f"❌ Gemini客户端初始化失败: {e}")
            self.client = None

    def _init_apiqik_client(self):
        """初始化APIQIK配置。"""
        if not APIQIK_AVAILABLE or apiqik_core is None:
            logger.error("❌ APIQIK模块不可用，无法初始化APIQIK图片模式")
            self.apiqik_api_key = None
            return

        env_path = Path(self.apiqik_env_file)
        api_key = apiqik_core.load_env_value("APIQIK_KEY", env_path)
        if not api_key:
            logger.error(f"❌ 未找到 APIQIK_KEY，请检查环境变量或 {env_path}")
            self.apiqik_api_key = None
            return

        self.apiqik_api_key = api_key
        if not self.apiqik_models:
            self.apiqik_models = list(apiqik_core.DEFAULT_MODEL_SEQUENCE)

        self.apiqik_concurrency = max(1, self.apiqik_concurrency)
        self.apiqik_target_images = max(1, self.apiqik_target_images)
        self.apiqik_total_timeout = max(30, self.apiqik_total_timeout)
        logger.info("✅ APIQIK图片模式初始化成功")

    def _session_key(self, chat_name: str, user_sender: str) -> tuple[str, str]:
        return chat_name, user_sender

    def _get_session(self, chat_name: str, user_sender: str) -> Optional[EditSession]:
        return self._sessions.get(self._session_key(chat_name, user_sender))

    def _remove_session(self, session: EditSession):
        key = self._session_key(session.chat_name, session.user_sender)
        if self._sessions.get(key) is session:
            self._sessions.pop(key, None)

    def _start_edit_session(
        self,
        user_sender: str,
        chat_name: str,
        wx_manager,
        required_images: int,
        target_images: Optional[int] = None,
    ) -> bool:
        """开始图片编辑会话"""
        with self._session_lock:
            target_log = target_images if target_images is not None else self.apiqik_target_images
            logger.info(
                f"🖼️ 尝试开始新会话 - 用户: {user_sender}, 聊天: {chat_name}, "
                f"需要图片: {required_images}张, 最多生成: {target_log}张"
            )
            existing_session = self._get_session(chat_name, user_sender)
            if existing_session and existing_session.status == "collecting":
                logger.info(f"🖼️ 用户已有收集中会话，继续使用 - 用户: {user_sender}, 聊天: {chat_name}")
                return True

            # 创建新会话
            session = EditSession(
                user_sender=user_sender,
                chat_name=chat_name,
                wx_manager=wx_manager,
                required_images=required_images,
                target_images=target_images,
            )
            self._sessions[self._session_key(chat_name, user_sender)] = session

            logger.info(f"🖼️ 新会话创建成功 - 用户: {user_sender}, 配置超时: {self.collect_timeout}秒")

            # 立即开始倒计时，如果超时则取消会话
            self.context.workers.start(
                f"session-timeout-{chat_name}-{user_sender}-{time.time_ns()}",
                self._start_timeout_check,
                args=(chat_name, user_sender),
            )

            logger.info(f"🖼️ 开始图片编辑会话 - 用户: {user_sender}, 聊天: {chat_name}, 需要图片: {required_images}张")
            return True

    def _start_timeout_check(self, chat_name: str, user_sender: str):
        """开始倒计时检查，如果超时则取消会话"""
        logger.info(f"🖼️ 开始超时检查 - 用户: {user_sender}, 超时时间: {self.collect_timeout}秒")

        # 使用循环检查而不是阻塞sleep，可以被中断
        start_time = time.time()
        check_interval = 1.0  # 每秒检查一次

        while True:
            time.sleep(check_interval)
            elapsed = time.time() - start_time

            # 检查是否超时
            if elapsed >= self.collect_timeout:
                break

            # 检查会话状态，如果已被处理或取消，则提前退出
            with self._session_lock:
                session = self._get_session(chat_name, user_sender)
                if (not session or
                    session.status != "collecting" or
                    session.processed_immediately):
                    logger.info(f"🖼️ 超时检查提前结束 - 用户: {user_sender}, 原因: 会话已处理或取消")
                    return

        # 超时处理
        with self._session_lock:
            session = self._get_session(chat_name, user_sender)
            if (session and
                session.status == "collecting" and
                not session.processed_immediately):

                logger.info(f"🖼️ 收集时间结束 - 用户: {user_sender}, 会话将被取消")

                # 发送超时提示
                if session.wx_manager:
                    collected_images = len(session.images)
                    has_description = 1 if session.text_description else 0

                    if collected_images < session.required_images and not has_description:
                        session.wx_manager.send_message(
                            session.chat_name,
                            f"⏰ 收集时间结束，您只发送了{collected_images}张图片，缺少文字描述"
                        )
                    elif collected_images < session.required_images:
                        session.wx_manager.send_message(
                            session.chat_name,
                            f"⏰ 收集时间结束，您只发送了{collected_images}张图片，还需要{session.required_images - collected_images}张图片"
                        )
                    elif not has_description:
                        session.wx_manager.send_message(
                            session.chat_name,
                            f"⏰ 收集时间结束，您发送了{collected_images}张图片但缺少文字描述"
                        )

                # 清理会话
                self._remove_session(session)
            else:
                logger.info(f"🖼️ 超时检查完成但会话已处理 - 用户: {user_sender}")

    def _start_processing_immediately(self, session: EditSession):
        """立即开始处理（当收集完成时调用）"""
        if not session:
            return

        with self._session_lock:
            session.status = "processing"
            session.processed_immediately = True  # 标记为已立即处理

            logger.info(f"🖼️ 收集完成，立即开始处理 - 图片: {len(session.images)}张, 描述: {session.text_description[:50] if session.text_description else 'None'}...")

            if session.wx_manager:
                session.wx_manager.send_message(
                    session.chat_name,
                    f"🎨 已收集完成，开始编辑处理..."
                )

            # 启动处理线程
            self.context.workers.start(
                f"process-{session.chat_name}-{session.user_sender}-{time.time_ns()}",
                self._process_images,
                args=(session,),
            )

    def _detect_image_format(self, image_data: bytes) -> str:
        """检测图片格式"""
        if image_data.startswith(b'\xff\xd8\xff'):
            return 'JPEG'
        elif image_data.startswith(b'\x89PNG'):
            return 'PNG'
        elif image_data.startswith(b'GIF8'):
            return 'GIF'
        elif image_data.startswith(b'BM'):
            return 'BMP'
        elif image_data.startswith(b'RIFF') and image_data[8:12] == b'WEBP':
            return 'WEBP'
        else:
            return 'UNKNOWN'

    def _add_image_to_session(self, image_base64: str, chat_name: str, user_sender: str) -> bool:
        """向会话添加图片（静默处理）"""
        with self._session_lock:
            session = self._get_session(chat_name, user_sender)
            if not session or session.status != "collecting":
                return False

            if len(session.images) >= self.max_images:
                logger.info(f"🖼️ 已达到最大图片数量: {self.max_images}")
                return False

            session.images.append(image_base64)
            logger.info(f"🖼️ 静默添加图片 - 用户: {user_sender}, 当前图片数: {len(session.images)}")
            return True



    def _process_images(self, session: Optional[EditSession] = None):
        """处理图片并生成编辑结果"""
        if session is None:
            return

        if self.mode == "apiqik":
            self._process_images_apiqik(session)
            return

        if not self.client:
            logger.error("🖼️ Gemini客户端不可用，无法处理图片")
            self._send_error_message(session, "Gemini客户端不可用，请检查配置。")
            with self._session_lock:
                self._remove_session(session)
            return

        try:
            logger.info(f"🖼️ 开始处理图片 - 用户: {session.user_sender}, 图片数: {len(session.images)}")

            original_description = session.text_description
            logger.info(f"🖼️ 原始用户描述: {original_description}")

            system_instruction = """
You are a master image editor and creator.
Your task is to meticulously follow the user's instructions to edit, combine, or generate images.
- Analyze all provided images and the text description carefully.
- Your output must be the final generated image.
- Do not respond with text if an image is expected.
"""
            final_prompt = original_description
            logger.info(f"🖼️ 处理提示词: {final_prompt}")

            # 构建Gemini请求
            try:
                parts = [types.Part.from_text(text=final_prompt)]

                # 添加所有收集的图片
                for i, img_base64 in enumerate(session.images):
                    try:
                        img_data = base64.b64decode(img_base64)
                        # 检测图片格式并设置正确的MIME类型
                        img_format = self._detect_image_format(img_data)
                        mime_type = f"image/{img_format.lower()}" if img_format != 'UNKNOWN' else "image/jpeg"
                        logger.info(f"🖼️ 图片 {i+1}: 格式={img_format}, MIME={mime_type}, 大小={len(img_data)} bytes")

                        parts.append(types.Part.from_bytes(data=img_data, mime_type=mime_type))
                    except Exception as e:
                        logger.error(f"🖼️ 处理图片 {i+1} 失败: {e}")
                        continue

                contents = [
                    types.Content(
                        role="user",
                        parts=parts,
                    ),
                ]
                logger.info(f"🖼️ 成功构建Gemini请求内容，包含 {len(parts)-1} 张图片")
            except Exception as e:
                logger.error(f"🖼️ 构建Gemini请求失败: {e}")
                raise

            # 尝试使用支持图片生成的配置
            try:
                generate_content_config = types.GenerateContentConfig(
                    response_modalities=["IMAGE", "TEXT"],
                    safety_settings=[
                        types.SafetySetting(
                            category="HARM_CATEGORY_HARASSMENT",
                            threshold="BLOCK_NONE",  # Block none
                        ),
                        types.SafetySetting(
                            category="HARM_CATEGORY_HATE_SPEECH",
                            threshold="BLOCK_NONE",  # Block none
                        ),
                        types.SafetySetting(
                            category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                            threshold="BLOCK_NONE",  # Block none
                        ),
                        types.SafetySetting(
                            category="HARM_CATEGORY_DANGEROUS_CONTENT",
                            threshold="BLOCK_NONE",  # Block none
                        ),
        ],
                )
                logger.info(f"🖼️ 使用图片生成配置: {self.model_name}")
            except Exception as config_error:
                logger.warning(f"🖼️ 图片生成配置失败，回退到文本模式: {config_error}")
                generate_content_config = types.GenerateContentConfig(
                    response_modalities=["TEXT"],
                )
                logger.info(f"🖼️ 使用文本模式配置: {self.model_name}")

            # 调用Gemini API（同步调用）
            response_text = ""
            generated_images = []

            logger.info(f"🖼️ 开始调用Gemini API（同步模式）...")

            try:
                # 使用同步调用而不是流式调用
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=generate_content_config,
                )

                # 分级记录API响应：DEBUG级别记录完整响应，INFO级别记录摘要
                import logging
                if logger.level <= logging.DEBUG:
                    # DEBUG模式：打印包含图片数据的完整响应
                    logger.debug(f"🖼️ [DEBUG] 完整的Gemini API响应: {response}")
                else:
                    # INFO模式：打印不含图片数据的摘要信息
                    summary_parts = []
                    if response.candidates and response.candidates[0].content:
                        for part in response.candidates[0].content.parts:
                            if part.text:
                                # 截断文本，避免过长
                                summary_parts.append(f"TextPart(text='{part.text[:30]}...')")
                            elif part.inline_data:
                                # 只记录图片大小，不记录具体内容
                                summary_parts.append(f"ImagePart(size={len(part.inline_data.data)} bytes)")

                    summary = (
                        f"candidates=[finish_reason={response.candidates[0].finish_reason.name if response.candidates and response.candidates[0].finish_reason else 'N/A'}, "
                        f"parts_summary={summary_parts}]"
                    )
                    logger.info(f"🖼️ Gemini API响应摘要: {summary}")

                logger.info(f"🖼️ 收到Gemini API完整响应")

                # 检查是否有候选结果以及finish_reason
                if response.candidates:
                    candidate = response.candidates[0]

                    # 检查是否因内容策略被阻止
                    if candidate.finish_reason == types.FinishReason.PROHIBITED_CONTENT:
                        logger.warning("🖼️ 请求因内容策略被阻止。")
                        self._send_error_message(session, "请求因官方内容策略被阻止，请尝试更换图片或修改描述。")
                        return  # 处理完毕，直接返回

                    # 检查是否有内容
                    if candidate.content:
                        logger.info(f"🖼️ 响应包含 {len(candidate.content.parts)} 个部分")

                        for part_idx, part in enumerate(candidate.content.parts):
                            if part.inline_data and part.inline_data.data:
                                # 保存生成的图片
                                img_data = part.inline_data.data
                                logger.info(f"🖼️ 收到生成的图片数据，大小: {len(img_data)} bytes")

                                # 检查数据是否是Base64编码的
                                try:
                                    # 尝试解码Base64
                                    decoded_data = base64.b64decode(img_data, validate=True)
                                    logger.info(f"🖼️ 数据是Base64编码的，已解码，大小: {len(decoded_data)} bytes")
                                    img_data = decoded_data
                                except Exception as e:
                                    # 如果解码失败，说明已经是原始数据
                                    logger.info(f"🖼️ 数据是原始格式，大小: {len(img_data)} bytes (解码失败: {e})")

                                generated_images.append(img_data)
                                logger.info(f"🖼️ 已保存第 {len(generated_images)} 张生成图片")
                            elif part.text:
                                response_text += part.text
                                logger.info(f"🖼️ 收到文本响应: {part.text[:100]}...")
                            else:
                                logger.warning(f"🖼️ 未知的部分类型: {type(part)}")
                    else:
                        # 当candidate存在但content为空时，检查finish_reason
                        logger.warning(f"🖼️ API响应的候选结果中没有内容，finish_reason: {candidate.finish_reason}")

                        if candidate.finish_reason == types.FinishReason.RECITATION:
                            self._send_error_message(session, "请求失败：AI模型检测到内容重复，请尝试使用不同的图片或描述。")
                        elif candidate.finish_reason == types.FinishReason.SAFETY:
                            self._send_error_message(session, "请求失败：内容被安全过滤器阻止，请尝试调整图片或描述。")
                        elif candidate.finish_reason == types.FinishReason.OTHER:
                            self._send_error_message(session, "请求失败：AI模型返回未知错误，请稍后重试。")
                        else:
                            self._send_error_message(session, f"请求失败：AI模型未生成内容 (原因: {candidate.finish_reason})")
                        return  # 处理完毕，直接返回
                else:
                    logger.warning(f"🖼️ API响应没有候选结果")
                    self._send_error_message(session, "AI模型未返回任何结果，请稍后重试。")
                    return

                logger.info(f"🖼️ Gemini API调用完成")
                logger.info(f"🖼️ 生成图片数量: {len(generated_images)}")
                logger.info(f"🖼️ 响应文本长度: {len(response_text)}")

            except Exception as api_error:
                logger.error(f"🖼️ Gemini API调用失败: {api_error}")
                # 如果可能，记录下错误响应的内容
                logger.error(f"🖼️ 失败的API响应详情: {type(api_error).__name__}")
                if hasattr(api_error, 'response'):
                    logger.error(f"🖼️ 错误响应内容: {api_error.response}")
                if hasattr(api_error, 'status_code'):
                    logger.error(f"🖼️ HTTP状态码: {api_error.status_code}")
                if hasattr(api_error, 'message'):
                    logger.error(f"🖼️ 错误消息: {api_error.message}")
                # 记录完整的异常信息
                import traceback
                logger.error(f"🖼️ 完整的异常信息:\n{traceback.format_exc()}")
                raise

            # 发送结果
            self._send_result(session, generated_images, response_text, final_prompt)

        except Exception as e:
            logger.error(f"🖼️ 图片处理失败: {e}")
            logger.error(f"🖼️ 异常类型: {type(e).__name__}")
            import traceback
            logger.error(f"🖼️ 完整错误信息:\n{traceback.format_exc()}")

            # 发送更详细的错误信息
            error_details = []
            if "API_KEY" in str(e).upper():
                error_details.append("API密钥配置问题")
            elif "quota" in str(e).lower():
                error_details.append("API配额不足")
            elif "model" in str(e).lower():
                error_details.append("模型不支持或不存在")
            elif "image" in str(e).lower():
                error_details.append("图片处理相关错误")
            elif "safety" in str(e).lower():
                error_details.append("内容被安全策略阻止")
            elif "blocked" in str(e).lower():
                error_details.append("请求被阻止")

            if error_details:
                error_msg = f"{str(e)} (可能原因: {', '.join(error_details)})"
            else:
                error_msg = f"{str(e)}"

            # 传递更多上下文信息
            context_info = f"处理了 {len(session.images)} 张图片，使用描述: {session.text_description[:50]}..."
            self._send_error_message(session, error_msg, context_info)
        finally:
            # 清理会话
            with self._session_lock:
                self._remove_session(session)

    def _process_images_apiqik(self, session: EditSession):
        """使用APIQIK模式处理图片，按模型顺序并发生成配置数量的结果。"""
        try:
            if not APIQIK_AVAILABLE or apiqik_core is None:
                self._send_error_message(session, "APIQIK模块不可用。")
                return

            if not self.apiqik_api_key:
                self._init_apiqik_client()
            if not self.apiqik_api_key:
                self._send_error_message(session, "APIQIK_KEY未配置。")
                return

            target_images = session.target_images if session.target_images is not None else self.apiqik_target_images
            target_images = max(1, int(target_images))
            logger.info(
                f"🖼️ APIQIK模式开始处理 - 用户: {session.user_sender}, "
                f"参考图片数: {len(session.images)}, 最多生成: {target_images}张"
            )
            original_description = session.text_description or ""
            final_prompt = original_description
            sent_count = 0
            deadline = time.monotonic() + self.apiqik_total_timeout

            with tempfile.TemporaryDirectory(prefix="apiqik_refs_") as temp_dir_name:
                reference_paths = self._write_session_images_to_temp_files(session, Path(temp_dir_name))

                image_urls = []
                if reference_paths:
                    logger.info(f"🖼️ APIQIK准备上传 {len(reference_paths)} 张参考图到Cloudflare R2")
                    image_urls = apiqik_core.resolve_image_inputs(
                        [str(path) for path in reference_paths],
                        env_path=Path(self.apiqik_env_file),
                        timeout=min(self._remaining_apiqik_seconds(deadline), 300),
                        upload_cache_path=self.apiqik_upload_cache_path,
                        phash_distance=self.apiqik_phash_distance,
                    )
                    logger.info(f"🖼️ APIQIK参考图上传完成，URL数量: {len(image_urls)}")

            half_target_images = (target_images + 1) // 2
            for round_index, model in enumerate(self.apiqik_models, start=1):
                remaining = target_images - sent_count
                if remaining <= 0:
                    break
                if self._remaining_apiqik_seconds(deadline) <= 0:
                    break

                round_needed = target_images
                # 成本优化：首轮没有凑齐，但已生成数量达到目标半数时，后续模型每轮最多请求半数目标。
                # 若未达到半数，则保持原策略，继续按原始目标数量请求。
                # 例如 +10 首轮出 9 张，第二轮只请求 5 张；首轮出 4 张，第二轮仍请求 10 张。
                if round_index > 1 and sent_count >= half_target_images:
                    round_needed = half_target_images

                logger.info(
                    f"🖼️ APIQIK开始模型轮次: {model}, 并发上限: {self.apiqik_concurrency}, "
                    f"本轮目标: {round_needed}, 总剩余目标: {remaining}, "
                    f"剩余总时间: {self._remaining_apiqik_seconds(deadline)}秒"
                )
                round_sent = self._run_apiqik_model_round(
                    session=session,
                    prompt=final_prompt,
                    model=model,
                    image_urls=image_urls,
                    needed=round_needed,
                    deadline=deadline,
                )
                sent_count += round_sent

                if sent_count >= target_images:
                    break

                logger.info(
                    f"🖼️ APIQIK模型 {model} 未凑齐目标图片，当前累计已发送: {sent_count}，切换下一个模型"
                )

            if sent_count == 0 and session.wx_manager:
                session.wx_manager.send_message(session.chat_name, "响应超时")
            logger.info(f"🖼️ APIQIK模式处理完成，已发送图片数: {sent_count}")

        except Exception as e:
            logger.error(f"🖼️ APIQIK模式处理失败: {e}")
            import traceback
            logger.error(f"🖼️ APIQIK完整错误信息:\n{traceback.format_exc()}")
            if session.wx_manager:
                session.wx_manager.send_message(session.chat_name, "响应超时")
        finally:
            with self._session_lock:
                self._remove_session(session)

    def _write_session_images_to_temp_files(self, session: EditSession, temp_dir: Path) -> List[Path]:
        """把会话中的base64图片写成临时文件，交给APIQIK的R2上传逻辑处理。"""
        paths: List[Path] = []
        temp_dir.mkdir(parents=True, exist_ok=True)
        extension_by_format = {
            "JPEG": ".jpg",
            "PNG": ".png",
            "GIF": ".gif",
            "BMP": ".bmp",
            "WEBP": ".webp",
        }

        for index, image_base64 in enumerate(session.images, start=1):
            try:
                image_data = base64.b64decode(image_base64)
                image_format = self._detect_image_format(image_data)
                suffix = extension_by_format.get(image_format, ".jpg")
                path = temp_dir / f"reference_{index}{suffix}"
                path.write_bytes(image_data)
                paths.append(path)
            except Exception as e:
                logger.error(f"🖼️ APIQIK参考图临时文件写入失败: {e}")

        if not paths and session.required_images > 0:
            raise ValueError("没有可用的参考图片")
        return paths

    def _run_apiqik_model_round(
        self,
        *,
        session: EditSession,
        prompt: str,
        model: str,
        image_urls: List[str],
        needed: int,
        deadline: float,
    ) -> int:
        """对单个APIQIK模型发起一轮并发请求，拿到图片后立即发送。"""
        sent_count = 0
        self.apiqik_output_dir.mkdir(parents=True, exist_ok=True)
        request_timeout = min(self._remaining_apiqik_seconds(deadline), 300)
        if request_timeout <= 0 or needed <= 0:
            return sent_count

        worker_count = max(1, min(self.apiqik_concurrency, needed))
        executor = ThreadPoolExecutor(max_workers=worker_count)
        futures = {
            executor.submit(
                self._apiqik_single_worker,
                prompt,
                model,
                image_urls,
                request_index,
                request_timeout,
            ): request_index
            for request_index in range(1, worker_count + 1)
        }
        pending = set(futures.keys())

        try:
            while pending and sent_count < needed:
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    break

                done, pending = wait(
                    pending,
                    timeout=min(1.0, remaining_seconds),
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    continue

                for future in done:
                    request_index = futures[future]
                    try:
                        success, paths_or_error, duration, content_text = future.result()
                    except Exception as e:
                        logger.error(f"🖼️ APIQIK任务崩溃 - 模型: {model}, 序号: {request_index}, 错误: {e}")
                        continue

                    if not success:
                        logger.warning(
                            f"🖼️ APIQIK任务失败 - 模型: {model}, 序号: {request_index}, "
                            f"耗时: {duration:.1f}s, 错误: {paths_or_error}"
                        )
                        if content_text:
                            logger.debug(f"🖼️ APIQIK失败响应文本: {content_text[:500]}")
                        continue

                    for path in paths_or_error:
                        if sent_count >= needed:
                            logger.info(f"🖼️ APIQIK生成图超过本轮目标，保留文件不删除: {path}")
                            continue
                        if self._send_apiqik_image_file(session, path):
                            sent_count += 1
                            logger.info(f"🖼️ APIQIK生成图已发送并保留文件: {path}")
                        else:
                            logger.warning(f"🖼️ APIQIK生成图未成功发送，保留文件供重试/排查: {path}")

            return sent_count
        finally:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

    def _remaining_apiqik_seconds(self, deadline: float) -> int:
        return max(0, int(deadline - time.monotonic()))

    def _apiqik_single_worker(
        self,
        prompt: str,
        model: str,
        image_urls: List[str],
        request_index: int,
        request_timeout: int,
    ):
        """执行单个APIQIK生成请求。"""
        start_time = time.time()
        timestamp = int(start_time * 1000)
        output_path = self.apiqik_output_dir / f"apiqik_{model}_{timestamp}_{request_index}.png"
        response = None
        content_text = ""

        try:
            response = apiqik_core.generate_image(
                api_key=self.apiqik_api_key,
                prompt=prompt,
                model=model,
                n=1,
                size=None,
                ratio=None,
                quality="high",
                image_urls=image_urls,
                base_url=self.apiqik_base_url,
                timeout=request_timeout,
                group=self.apiqik_group,
            )

            choices = response.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    text = choice.get("message", {}).get("content", "")
                    if text:
                        content_text += text + "\n"

            saved_paths = apiqik_core.save_generation_result(response, output_path)
            duration = time.time() - start_time
            logger.info(
                f"🖼️ APIQIK任务成功 - 模型: {model}, 序号: {request_index}, "
                f"图片数: {len(saved_paths)}, 耗时: {duration:.1f}s"
            )
            return True, saved_paths, duration, content_text
        except Exception as e:
            duration = time.time() - start_time
            if not content_text and isinstance(response, dict):
                choices = response.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        text = choice.get("message", {}).get("content", "")
                        if text:
                            content_text += text + "\n"
            return False, str(e), duration, content_text

    def _send_apiqik_image_file(self, session: EditSession, path: Path) -> bool:
        """逐张发送APIQIK生成的图片文件。"""
        try:
            if not session.wx_manager:
                return False
            if not path.exists() or path.stat().st_size <= 0:
                logger.warning(f"🖼️ APIQIK生成文件不存在或为空: {path}")
                return False

            for attempt in range(1, 4):
                ok = session.wx_manager.send_files(session.chat_name, [str(path)])
                if ok:
                    logger.info(f"🖼️ APIQIK已发送生成图片: {path} (第{attempt}次)")
                    return True
                if attempt < 3:
                    wait_seconds = attempt * 2
                    logger.warning(
                        f"🖼️ APIQIK图片发送失败，将在{wait_seconds}秒后重试 "
                        f"(第{attempt}/3次): {path}"
                    )
                    time.sleep(wait_seconds)

            logger.error(f"🖼️ APIQIK图片发送最终失败，文件保留: {path}")
            return False
        except Exception as e:
            logger.error(f"🖼️ APIQIK图片发送失败: {e}")
            return False

    def _cleanup_temp_file(self, path: Path):
        """保留生成图片文件，避免发送 API 尚未读完文件时被过早删除。"""
        logger.info(f"🖼️ 保留临时图片文件，不删除: {path}")

    def _send_result(self, session: EditSession, images: List[bytes], text: str, prompt: str):
        """发送处理结果 - 只发送图片，不发送文字消息（除非出错）"""
        try:
            logger.info(f"🖼️ 图片处理完成 - 生成图片数: {len(images)}")

            if session.wx_manager:
                # 只发送生成的图片，不发送任何文字消息
                if images:
                    try:
                        img_data = images[0]  # 只取第一张图片
                        import imghdr
                        temp_filename = f"generated_image_{int(time.time())}"
                        temp_path = None

                        # 尝试检测图片格式
                        try:
                            img_format = imghdr.what(None, img_data)
                            logger.info(f"🖼️ 检测到图片格式: {img_format}, 数据前64字节: {img_data[:64].hex()}")

                            if img_format:
                                temp_path = Path(f"temp_{temp_filename}.{img_format}")
                            else:
                                # 如果检测不到格式，检查数据头部特征
                                if img_data.startswith(b'\xff\xd8\xff'):
                                    temp_path = Path(f"temp_{temp_filename}.jpg")
                                    logger.info("🖼️ 根据数据头部识别为JPEG格式")
                                elif img_data.startswith(b'\x89PNG'):
                                    temp_path = Path(f"temp_{temp_filename}.png")
                                    logger.info("🖼️ 根据数据头部识别为PNG格式")
                                elif img_data.startswith(b'GIF8'):
                                    temp_path = Path(f"temp_{temp_filename}.gif")
                                    logger.info("🖼️ 根据数据头部识别为GIF格式")
                                else:
                                    temp_path = Path(f"temp_{temp_filename}.png")
                                    logger.info("🖼️ 无法识别格式，使用默认PNG格式")
                        except Exception as e:
                            logger.warning(f"🖼️ 格式检测失败: {e}, 使用PNG格式")
                            temp_path = Path(f"temp_{temp_filename}.png")

                        # 保存图片数据
                        with open(temp_path, "wb") as f:
                            f.write(img_data)

                        # 验证文件是否正确保存
                        if temp_path.exists() and temp_path.stat().st_size > 0:
                            logger.info(f"🖼️ 图片保存成功: {temp_path}, 大小: {temp_path.stat().st_size} bytes")

                            # 只发送图片文件，不发送任何文字消息
                            session.wx_manager.send_files(session.chat_name, [str(temp_path)])
                            logger.info("🖼️ 已发送生成的图片")
                        else:
                            raise Exception("文件保存失败或文件为空")

                        # 保留临时文件，避免发送API尚未读完文件时被过早删除
                        logger.info(f"🖼️ 已发送图片并保留临时文件: {temp_path}")

                    except Exception as e:
                        logger.error(f"发送图片失败: {e}")
                        # 只有出错时才发送文字消息
                        if session.wx_manager:
                            session.wx_manager.send_message(session.chat_name, f"⚠️ 图片发送失败: {str(e)}")
                else:
                    logger.warning("🖼️ 没有生成任何图片")
                    # 没有生成图片时发送错误提示
                    if session.wx_manager:
                        if text and text.strip():
                            # 将模型的回复发给用户，让他知道为什么失败
                            session.wx_manager.send_message(session.chat_name, f"🤔 图片生成失败。AI的回复是：\n\n\"{text.strip()}\"")
                        else:
                            session.wx_manager.send_message(session.chat_name, "⚠️ 图片编辑失败：模型没有生成图片，也没有提供任何文字说明。")

        except Exception as e:
            logger.error(f"发送结果失败: {e}")

    def _send_error_message(self, session: EditSession, error_msg: str, context_info: str = None):
        """发送错误消息"""
        try:
            logger.error(f"🖼️ 处理失败 - 用户: {session.user_sender}, 错误: {error_msg}")
            if session.wx_manager:
                if context_info:
                    full_msg = f"⚠️ 图片编辑失败：{error_msg}\n💡 上下文：{context_info}"
                else:
                    full_msg = f"⚠️ 图片编辑失败：{error_msg}"
                session.wx_manager.send_message(
                    session.chat_name,
                    full_msg
                )
        except Exception as e:
            logger.error(f"发送错误消息失败: {e}")

    def handle_text_message(self, event: Event) -> bool:
        """处理文本消息事件"""
        try:
            message = event.data.get("message", "").strip()
            sender = event.data.get("sender", "")
            chat_name = event.data.get("chat_name", "")
            wx_manager = event.context.get("wx")

            # 注意：权限检查已移至 EventBus 统一管理，此处不再检查 enabled_chats

                        # 检查是否在图片编辑会话中
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if session and session.status == "collecting":

                    # 在会话中，收集第一条文字描述
                    if session.text_description is None:
                        session.text_description = message.strip()
                        logger.info(f"🖼️ 收集到文字描述 - 用户: {sender}, 描述: {message[:50]}...")

                        # 检查是否可以开始处理
                        if session.is_complete():
                            self._start_processing_immediately(session)
                        return True  # 静默处理，不回复
                    else:
                        # 已经有了描述，忽略后续描述
                        return True
            # 解析触发词
            required_images, target_images, clean_trigger, immediate_text = self._parse_trigger_word(message)

            # 如果 clean_trigger 是 None，说明没有匹配到触发词
            if not clean_trigger:
                return False
            target_log = target_images if target_images is not None else self.apiqik_target_images
            logger.info(
                f"🖼️ 检测到触发词 - 用户: {sender}, 聊天: {chat_name}, "
                f"需要图片: {required_images}张, 最多生成: {target_log}张"
            )

            # 开始新会话
            if self._start_edit_session(sender, chat_name, wx_manager, required_images, target_images=target_images):
                # 处理即时包含在触发消息中的文本
                if immediate_text:
                    with self._session_lock:
                        session = self._get_session(chat_name, sender)
                        if session:
                            session.text_description = immediate_text
                            logger.info(f"🖼️ 从触发消息中提取描述: {immediate_text[:50]}...")
                            if session.is_complete():
                                self._start_processing_immediately(session)
                                return True

                if wx_manager:
                    if required_images == 0:
                        wx_manager.send_message(
                            chat_name,
                            f"🎨 请输入您想要生成的图片描述\n"
                        )
                    elif required_images == 1:
                        wx_manager.send_message(
                            chat_name,
                            f"🎨 请发送需要编辑的图片和修改描述\n"
                        )
                    else:
                        wx_manager.send_message(
                            chat_name,
                            f"🎨 请发送{required_images}张需要编辑的图片和修改描述\n"
                        )
                return True

            return False

        except Exception as e:
            logger.error(f"🖼️ 处理文本消息失败: {e}")
            return False

    def handle_image_message(self, event: Event) -> bool:
        """处理图片消息事件"""
        try:
            sender = event.data.get("sender", "")
            chat_name = event.data.get("chat_name", "")

            logger.info(f"🖼️ handle_image_message called - sender: {sender}, chat: {chat_name}")

            # 检查是否有活跃会话且是同一用户
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                logger.info(f"🖼️ 检查session - exists: {session is not None}, status: {session.status if session else 'None'}")

                if not session or session.status != "collecting":
                    logger.info(f"🖼️ Session check failed - sender: {sender}, status: {session.status if session else 'None'}")
                    return False

            # 获取图片消息ID
            message_id = event.data.get("message_id")

            if not message_id:
                logger.warning(f"🖼️ 图片消息缺少message_id")
                return False

            # 检查是否还可以添加图片
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if not session or not session.can_add_image():
                    logger.info(f"🖼️ 已达到所需图片数量或会话不存在，忽略此图片")
                    return True  # 静默忽略，不回复

                # 立即确认收到图片，不等待下载完成
                logger.info(f"🖼️ 收到图片消息 - 用户: {sender}, 消息ID: {message_id}")

                # 创建下载任务
                download_task = ImageDownload(message_id=message_id, status="pending")
                session.image_downloads[message_id] = download_task

            # 启动后台下载线程
            self.context.workers.start(
                f"download-{chat_name}-{message_id}-{time.time_ns()}",
                self._download_image_async,
                args=(chat_name, message_id, sender),
            )

            return True  # 静默处理，不回复

        except Exception as e:
            logger.error(f"🖼️ 处理图片消息失败: {e}")
            return False

    def handle_quote_image_message(self, event: Event) -> bool:
        """处理引用图片消息事件"""
        try:
            data = event.data
            context = event.context

            chat_name = data.get("chat_name", "")
            content = data.get("message", "").strip()
            quote_content = data.get("quote_content", "")
            sender = data.get("sender", "")
            wx_manager = context.get("wx")

            # 注意：权限检查已移至 EventBus 统一管理，此处不再检查 enabled_chats

            # 仅当引用里含图片时触发
            if "[图片]" not in quote_content:
                return False

            # 检查是否在图片编辑会话中
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if session and session.status == "collecting":

                    # 在会话中，处理引用图片和描述
                    if session.text_description is None:
                        # 设置描述
                        session.text_description = content.strip()
                        logger.info(f"🖼️ 收集到引用图片和文字描述 - 用户: {sender}, 描述: {content[:50]}...")

                        # 下载引用图片
                        message_id = data.get("message_id")
                        if message_id:
                            self.context.workers.start(
                                f"quote-download-{chat_name}-{message_id}-{time.time_ns()}",
                                self._download_quote_image_async,
                                args=(chat_name, message_id, sender),
                            )

                        # 检查是否可以开始处理
                        if session.is_complete():
                            self._start_processing_immediately(session)
                        return True  # 静默处理，不回复
                    else:
                        # 已经有了描述，忽略
                        return True
                else:
                    # 不在会话中，忽略
                    return False

        except Exception as e:
            logger.error(f"🖼️ 处理引用图片消息失败: {e}")
            return False

    def _download_quote_image_async(self, chat_name: str, message_id: str, sender: str):
        """异步下载引用图片"""
        try:
            logger.info(f"🖼️ 开始后台下载引用图片 - 消息ID: {message_id}")

            # 获取WeChat管理器
            wx_manager = None
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if session:
                    wx_manager = session.wx_manager

            if wx_manager:
                # 下载引用图片
                image_path = wx_manager.download_quote_image(chat_name, message_id=message_id)
                if image_path and Path(image_path).exists():
                    # 读取并编码图片
                    try:
                        with open(image_path, "rb") as f:
                            image_data = f.read()

                        # 检查图片大小
                        image_size_mb = len(image_data) / (1024 * 1024)
                        logger.info(f"🖼️ 引用图片大小: {image_size_mb:.2f} MB")

                        # 如果图片太大，可能需要压缩或拒绝处理
                        if image_size_mb > 10:  # 10MB限制
                            logger.warning(f"🖼️ 引用图片过大 ({image_size_mb:.2f} MB)，可能导致API调用失败")
                            return

                        # 检测图片格式
                        image_format = self._detect_image_format(image_data)
                        logger.info(f"🖼️ 检测到引用图片格式: {image_format}")

                        image_base64 = base64.b64encode(image_data).decode("utf-8")

                        # 添加图片到会话
                        with self._session_lock:
                            session = self._get_session(chat_name, sender)
                            if session and session.can_add_image():
                                session.images.append(image_base64)
                                logger.info(f"🖼️ 引用图片已添加到session - 当前图片数: {len(session.images)}")

                                # 检查是否可以开始处理
                                if session.is_complete():
                                    self._start_processing_immediately(session)

                    except Exception as e:
                        logger.error(f"🖼️ 读取下载的引用图片失败: {e}")
                else:
                    logger.warning(f"🖼️ 下载引用图片失败或文件不存在 - 消息ID: {message_id}")
            else:
                logger.warning(f"🖼️ WeChat管理器不可用，无法下载引用图片 - 消息ID: {message_id}")

        except Exception as e:
            logger.error(f"🖼️ 异步下载引用图片失败 - 消息ID: {message_id}, 错误: {e}")

    def _download_image_async(self, chat_name: str, message_id: str, sender: str):
        """异步下载图片"""
        try:
            logger.info(f"🖼️ 开始后台下载图片 - 消息ID: {message_id}")

            # 更新下载状态
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if session and message_id in session.image_downloads:
                    session.image_downloads[message_id].status = "downloading"

            # 调用下载API
            wx_manager = None
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if session:
                    wx_manager = session.wx_manager

            if wx_manager:
                image_path = wx_manager.download_image_message(chat_name, message_id)
                if image_path and Path(image_path).exists():
                    # 读取并编码图片
                    try:
                        with open(image_path, "rb") as f:
                            image_data = f.read()

                        # 检查图片大小
                        image_size_mb = len(image_data) / (1024 * 1024)
                        logger.info(f"🖼️ 图片大小: {image_size_mb:.2f} MB")

                        # 如果图片太大，可能需要压缩或拒绝处理
                        if image_size_mb > 10:  # 10MB限制
                            logger.warning(f"🖼️ 图片过大 ({image_size_mb:.2f} MB)，可能导致API调用失败")
                            with self._session_lock:
                                session = self._get_session(chat_name, sender)
                                if session and message_id in session.image_downloads:
                                    session.image_downloads[message_id].status = "failed"
                                    session.image_downloads[message_id].error = f"图片过大 ({image_size_mb:.2f} MB)"
                            return True

                        # 检测图片格式
                        image_format = self._detect_image_format(image_data)
                        logger.info(f"🖼️ 检测到图片格式: {image_format}")

                        image_base64 = base64.b64encode(image_data).decode("utf-8")

                        # 更新下载状态和添加图片
                        with self._session_lock:
                            session = self._get_session(chat_name, sender)
                            if session and message_id in session.image_downloads:
                                session.image_downloads[message_id].status = "completed"
                                session.image_downloads[message_id].file_path = image_path

                                # 再次检查是否可以添加图片（防止并发问题）
                                if session.can_add_image():
                                    session.images.append(image_base64)
                                    logger.info(f"🖼️ 图片已添加到session - 当前图片数: {len(session.images)}")
                                else:
                                    logger.info(f"🖼️ 图片数量已达上限，忽略此图片 - 消息ID: {message_id}")
                                    # 标记为失败但不添加到images列表
                                    session.image_downloads[message_id].status = "ignored"
                                    return

                        # 检查是否可以开始处理
                        with self._session_lock:
                            session = self._get_session(chat_name, sender)
                            if session and session.is_complete():
                                logger.info(f"🖼️ 图片下载完成 - 消息ID: {message_id}, 大小: {len(image_data)} bytes, Base64长度: {len(image_base64)}")
                                self._start_processing_immediately(session)
                            else:
                                logger.info(f"🖼️ 图片下载完成 - 消息ID: {message_id}, 大小: {len(image_data)} bytes, Base64长度: {len(image_base64)}")

                    except Exception as e:
                        logger.error(f"🖼️ 读取下载的图片失败: {e}")
                        with self._session_lock:
                            session = self._get_session(chat_name, sender)
                            if session and message_id in session.image_downloads:
                                session.image_downloads[message_id].status = "failed"
                                session.image_downloads[message_id].error = str(e)
                else:
                    logger.warning(f"🖼️ 下载图片失败或文件不存在 - 消息ID: {message_id}")
                    with self._session_lock:
                        session = self._get_session(chat_name, sender)
                        if session and message_id in session.image_downloads:
                            session.image_downloads[message_id].status = "failed"
                            session.image_downloads[message_id].error = "下载失败或文件不存在"
            else:
                logger.warning(f"🖼️ WeChat管理器不可用，无法下载图片 - 消息ID: {message_id}")
                with self._session_lock:
                    session = self._get_session(chat_name, sender)
                    if session and message_id in session.image_downloads:
                        session.image_downloads[message_id].status = "failed"
                        session.image_downloads[message_id].error = "WeChat管理器不可用"

        except Exception as e:
            logger.error(f"🖼️ 异步下载图片失败 - 消息ID: {message_id}, 错误: {e}")
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if session and message_id in session.image_downloads:
                    session.image_downloads[message_id].status = "failed"
                    session.image_downloads[message_id].error = str(e)

# 全局实例
image_editor_plugin = None


def handle_text_message(event: Event) -> bool:
    """处理文本消息事件"""
    global image_editor_plugin
    if image_editor_plugin:
        return image_editor_plugin.handle_text_message(event)
    return False


def handle_image_message(event: Event) -> bool:
    """处理图片消息事件"""
    global image_editor_plugin
    if image_editor_plugin:
        return image_editor_plugin.handle_image_message(event)
    return False


def handle_quote_image_message(event: Event) -> bool:
    """处理引用图片消息事件"""
    global image_editor_plugin
    if image_editor_plugin:
        return image_editor_plugin.handle_quote_image_message(event)
    return False


def register(event_bus, subscribe, context):
    """插件注册函数"""
    global image_editor_plugin

    logger.info("🖼️ 注册图片编辑插件...")

    # 初始化插件
    image_editor_plugin = ImageEditorPlugin(context)
    context.health.register(lambda: {
        "status": "healthy" if image_editor_plugin is not None else "unhealthy",
        "message": "图片编辑会话服务已就绪" if image_editor_plugin is not None else "图片编辑服务未初始化",
        "active_sessions": len(image_editor_plugin._sessions) if image_editor_plugin is not None else 0,
    })
    context.register_cleanup(unregister)

        # 订阅事件
    subscribe(
        event_type=EventType.TEXT_MESSAGE_RECEIVED,
        handler=handle_text_message
    )

    subscribe(
        event_type=EventType.IMAGE_MESSAGE_RECEIVED,
        handler=handle_image_message
    )

    subscribe(
        event_type=EventType.QUOTE_IMAGE_MESSAGE_RECEIVED,
        handler=handle_quote_image_message
    )

    logger.info("✅ 图片编辑插件注册成功")


def unregister():
    """取消注册插件"""
    global image_editor_plugin

    logger.info("🖼️ 取消注册图片编辑插件...")
    image_editor_plugin = None
    logger.info("✅ 图片编辑插件已取消注册")
