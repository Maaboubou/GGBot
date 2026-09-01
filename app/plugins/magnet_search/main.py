"""
磁力搜索插件
- 检测用户输入中的关键词"磁力"
- 提取其余内容作为搜索关键词
- 调用磁力搜索功能生成PDF
- 发送PDF文件给用户
"""

import re
import logging
import os
import time
import base64
import io
import importlib
import sys
import threading
from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, List

from PIL import Image, ImageOps

from app.core.event_bus import Event, EventType
from app.plugins.magnet_check.main import (
    NUDENET_INFERENCE_RESOLUTION,
    NUDENET_THRESHOLD,
    NudityDecision,
    blur_image_bytes,
    create_nudenet_detector,
    detect_screenshot_urls,
    query_whatslink,
    screenshot_urls,
    validate_magnet,
)
from app.utils.plugin_config import get_config

# 导入核心搜索功能。这里主动刷新子模块，兼容当前已运行、
# 尚未加载新 PluginManager 清理逻辑的进程。
_getmagnet_module_name = f"{__package__}.getmagnet"
_cached_getmagnet = sys.modules.pop(_getmagnet_module_name, None)
_plugin_package = sys.modules.get(__package__)
if (
    _cached_getmagnet is not None
    and _plugin_package is not None
    and getattr(_plugin_package, "getmagnet", None) is _cached_getmagnet
):
    delattr(_plugin_package, "getmagnet")
_getmagnet = importlib.import_module(_getmagnet_module_name)
fetch_magnet_links = _getmagnet.fetch_magnet_links
generate_html_content = _getmagnet.generate_html_content
render_html_to_pdf = _getmagnet.render_html_to_pdf

logger = logging.getLogger(__name__)


class SearchStatus(str, Enum):
    SUCCESS = "success"
    NO_RESULTS = "no_results"
    SEARCH_FAILED = "search_failed"
    REPORT_FAILED = "report_failed"


@dataclass(frozen=True)
class SearchGenerationResult:
    status: SearchStatus
    pdf_path: Optional[str] = None


class MagnetSearchPlugin:
    """磁力搜索插件"""

    def __init__(self, plugin_name="magnet_search", context=None):
        self.logger = logger
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        if context is not None:
            migration_notes = context.storage.migrate_legacy_directory(
                Path(self.plugin_dir) / "pdfs", storage_class="generated", relative="reports"
            )
            self.pdf_dir = str(context.storage.generated_root / "reports")
            if migration_notes:
                context.audit.record(
                    "storage_migration",
                    summary="磁力搜索报告已迁移到插件标准存储目录",
                    details={"moved_files": len(migration_notes)},
                )
        else:
            self.pdf_dir = os.path.join(self.plugin_dir, "pdfs")
        os.makedirs(self.pdf_dir, exist_ok=True)

        # 从配置文件读取设置
        self.trigger_keyword = str(
            get_config("trigger_keyword", "磁力", plugin_name=plugin_name) or "磁力"
        ).strip()
        self.entry_points: List[str] = get_config("entry_points", plugin_name=plugin_name) or [
            "https://磁力搜索.com/",
            "https://www.1024btso.com/",
        ]
        self.max_retries: int = int(get_config("max_retries", plugin_name=plugin_name) or 3)
        self.retry_delay: int = int(get_config("retry_delay", plugin_name=plugin_name) or 2)
        self.timeout: int = int(get_config("timeout", plugin_name=plugin_name) or 15)
        self.preview_result_limit = min(
            5,
            max(
                0,
                int(get_config("preview_result_limit", plugin_name=plugin_name, default=5) or 5),
            ),
        )
        self.preview_images_per_result = min(
            8,
            max(
                1,
                int(
                    get_config(
                        "preview_images_per_result",
                        plugin_name=plugin_name,
                        default=8,
                    )
                    or 8
                ),
            ),
        )
        self.preview_blur_strength = min(
            1.0,
            max(
                0.1,
                float(
                    get_config(
                        "blur_strength",
                        plugin_name=plugin_name,
                        default=0.8,
                    )
                    or 0.8
                ),
            ),
        )
        self.preview_timeout = max(
            3.0,
            float(get_config("timeout", plugin_name="magnet_check", default=30.0) or 30.0),
        )
        self.preview_detection_threshold = float(
            min(
                0.5,
                max(
                    0.05,
                    float(
                        get_config(
                            "detection_threshold",
                            plugin_name=plugin_name,
                            default=NUDENET_THRESHOLD,
                        )
                        or NUDENET_THRESHOLD
                    ),
                ),
            )
        )
        configured_preview_resolution = int(
            get_config(
                "inference_resolution",
                plugin_name=plugin_name,
                default=NUDENET_INFERENCE_RESOLUTION,
            )
            or NUDENET_INFERENCE_RESOLUTION
        )
        self.preview_inference_resolution = max(
            320,
            min(1280, round(configured_preview_resolution / 32) * 32),
        )
        self.preview_use_system_proxy = bool(
            get_config(
                "use_system_proxy",
                plugin_name="magnet_check",
                default=False,
            )
        )
        self._preview_detector: Any | None = None
        self._preview_detector_lock = threading.Lock()
        self._preview_inference_lock = threading.Lock()

        self.logger.info(
            "🔧 磁力搜索配置: %d个入口, 重试%d次, 超时%d秒, 补图前%d条×最多%d张, 检测阈值%.2f, 推理分辨率%d, 模糊强度%.0f%%",
            len(self.entry_points),
            self.max_retries,
            self.timeout,
            self.preview_result_limit,
            self.preview_images_per_result,
            self.preview_detection_threshold,
            self.preview_inference_resolution,
            self.preview_blur_strength * 100,
        )

    def _get_preview_detector(self) -> Any:
        if self._preview_detector is not None:
            return self._preview_detector
        with self._preview_detector_lock:
            if self._preview_detector is None:
                self._preview_detector = create_nudenet_detector(
                    inference_resolution=self.preview_inference_resolution,
                )
        return self._preview_detector

    def _build_preview_source(self, image_bytes: bytes, *, blurred: bool) -> str:
        payload = (
            blur_image_bytes(image_bytes, strength=self.preview_blur_strength)
            if blurred
            else image_bytes
        )
        with Image.open(io.BytesIO(payload)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source).convert("RGB")
            cropped = ImageOps.fit(
                image,
                (480, 270),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            output = io.BytesIO()
            cropped.save(output, format="JPEG", quality=82, optimize=True)
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    def _enrich_results_with_previews(self, results: list[dict[str, Any]]) -> None:
        """Attach up to eight checked/cropped previews to each of the first five results."""

        if self.preview_result_limit <= 0:
            return

        for result_index, item in enumerate(results[: self.preview_result_limit], start=1):
            magnet_value = str(item.get("magnet") or "")
            try:
                normalized_magnet = validate_magnet(magnet_value)
                _, check_data, _ = query_whatslink(
                    normalized_magnet,
                    timeout=self.preview_timeout,
                    use_system_proxy=self.preview_use_system_proxy,
                )
                if not isinstance(check_data, dict) or check_data.get("error"):
                    self.logger.warning(
                        "⚠️ 第%d条磁链未取得有效 check 数据: %s",
                        result_index,
                        check_data.get("error") if isinstance(check_data, dict) else "响应格式异常",
                    )
                    continue

                urls = screenshot_urls(check_data)[: self.preview_images_per_result]
                if not urls:
                    self.logger.info("ℹ️ 第%d条磁链没有可用资源截图", result_index)
                    continue

                detector = self._get_preview_detector()
                with self._preview_inference_lock:
                    decisions, image_cache = detect_screenshot_urls(
                        urls,
                        timeout=self.preview_timeout,
                        detector=detector,
                        threshold=self.preview_detection_threshold,
                        use_system_proxy=self.preview_use_system_proxy,
                    )

                previews = []
                for url in urls:
                    decision = decisions.get(
                        url,
                        NudityDecision(True, "missing_decision", failed_closed=True),
                    )
                    image_bytes = image_cache.get(url)
                    if decision.failed_closed or image_bytes is None:
                        self.logger.warning(
                            "⚠️ 第%d条磁链的一张预览检测失败，已跳过: %s",
                            result_index,
                            decision.reason,
                        )
                        continue
                    try:
                        previews.append(
                            {
                                "src": self._build_preview_source(
                                    image_bytes,
                                    blurred=decision.blur,
                                ),
                                "blurred": decision.blur,
                            }
                        )
                    except Exception as exc:
                        self.logger.warning(
                            "⚠️ 第%d条磁链的一张预览裁剪失败，已跳过: %s",
                            result_index,
                            exc,
                        )

                if previews:
                    item["preview_images"] = previews[: self.preview_images_per_result]
                    self.logger.info(
                        "✅ 第%d条磁链已补充%d张预览图",
                        result_index,
                        len(item["preview_images"]),
                    )
            except Exception as exc:
                self.logger.warning(
                    "⚠️ 第%d条磁链补图失败，保留文字结果: %s",
                    result_index,
                    exc,
                )

    def _should_trigger(self, text: str) -> Optional[str]:
        """检测是否包含配置的触发词，返回搜索关键词。"""
        if not text:
            return None

        if not self.trigger_keyword or self.trigger_keyword not in text:
            return None

        keyword = text.replace(self.trigger_keyword, "").strip()

        # 移除@mention (例如: @刘局, @bot等)
        # 匹配模式: @后跟任意非空白字符
        keyword = re.sub(r'@\S+', '', keyword).strip()

        if not keyword:
            return None

        return keyword

    def search_and_generate_pdf(
        self,
        keyword: str,
        sender: str = "",
    ) -> SearchGenerationResult:
        """搜索磁力链接并返回可区分的执行结果。"""
        try:
            self.logger.info(f"🔍 开始搜索磁力链接: {keyword}")

            # 调用搜索功能，传入配置参数
            results = fetch_magnet_links(
                keyword=keyword,
                entry_points=self.entry_points,
                max_retries=self.max_retries,
                retry_delay=self.retry_delay,
                timeout=self.timeout
            )

            if not results:
                self.logger.warning(f"⚠️ 未找到关键词 '{keyword}' 的搜索结果")
                return SearchGenerationResult(SearchStatus.NO_RESULTS)
        except Exception as exc:
            self.logger.error(f"❌ 磁力搜索请求失败: {exc}", exc_info=True)
            return SearchGenerationResult(SearchStatus.SEARCH_FAILED)

        try:
            self.logger.info(f"✅ 找到 {len(results)} 条结果,准备生成PDF")

            # 仅为前 5 条结果查询/检测预览；补图失败不影响原始搜索结果。
            self._enrich_results_with_previews(results)

            # 生成HTML内容
            self.logger.info(f"📝 开始生成HTML内容,结果数量: {len(results)}")
            html_content = generate_html_content(keyword, results, sender=sender)
            self.logger.info(f"✅ HTML内容生成完成,长度: {len(html_content)} 字符")

            # 生成PDF文件路径（sender_keyword 格式）
            safe_keyword = re.sub(r'[\\/:*?"<>|]', '_', keyword)
            safe_sender = re.sub(r'[\\/:*?"<>|]', '_', sender) if sender else "unknown"
            pdf_filename = f"{safe_sender}_{safe_keyword}.pdf"
            pdf_path = os.path.join(self.pdf_dir, pdf_filename)

            self.logger.info(f"📂 PDF保存目录: {self.pdf_dir}")
            self.logger.info(f"📄 PDF文件名: {pdf_filename}")
            self.logger.info(f"📍 完整PDF路径: {pdf_path}")

            # 渲染PDF
            self.logger.info(f"🖨️ 开始渲染HTML为PDF...")
            render_success = render_html_to_pdf(html_content, pdf_path)
            if render_success:
                self.logger.info(f"✅ PDF渲染完成")
            else:
                self.logger.error("❌ PDF渲染失败")

            # 检查文件是否存在
            if render_success and os.path.exists(pdf_path):
                file_size = os.path.getsize(pdf_path)
                self.logger.info(f"✅ PDF文件已生成: {pdf_path} (大小: {file_size} 字节)")
                return SearchGenerationResult(SearchStatus.SUCCESS, pdf_path)

            self.logger.error(
                "❌ 报告生成失败: render_success=%r, file_exists=%s, path=%s",
                render_success,
                os.path.exists(pdf_path),
                pdf_path,
            )
            return SearchGenerationResult(SearchStatus.REPORT_FAILED)

        except Exception as exc:
            self.logger.error(f"❌ 磁链报告生成失败: {exc}", exc_info=True)
            return SearchGenerationResult(SearchStatus.REPORT_FAILED)

    def handle_text(self, event: Event):
        """处理文本消息事件"""
        try:
            message = event.data.get("message", "")
            chat_name = event.data.get("chat_name", "")
            sender = event.data.get("sender", "")
            wx = event.context.get("wx")

            # 检测触发并提取搜索关键词
            keyword = self._should_trigger(message)
            if not keyword:
                return False

            self.logger.info(f"🧲 检测到磁力搜索请求: {keyword}")

            # 发送处理中提示
            # if wx:
            #     wx.send_message(chat_name, f"🔍 正在搜索 '{keyword}' 的磁力链接,请稍候...")

            # 搜索并生成PDF
            result = self.search_and_generate_pdf(keyword, sender=sender)

            if result.status is SearchStatus.NO_RESULTS:
                if wx:
                    wx.send_message(chat_name, f"❌ 未能找到 '{keyword}' 的磁力链接")
                return True

            if result.status is not SearchStatus.SUCCESS or not result.pdf_path:
                # 技术错误只记录日志，不向微信发送误导性的“未找到”。
                self.logger.error(
                    "❌ magnet_search 未发送报告，执行状态: %s",
                    result.status.value,
                )
                return True

            # 发送PDF文件
            if wx:
                try:
                    wx.send_files(chat_name, result.pdf_path)
                    self.logger.info(f"✅ 已发送PDF文件: {result.pdf_path}")
                except Exception as e:
                    self.logger.error(f"❌ 发送PDF文件失败: {e}")

            return True

        except Exception as e:
            self.logger.error(f"❌ magnet_search 处理失败: {e}", exc_info=True)
            return False


# 全局实例
plugin: Optional[MagnetSearchPlugin] = None


def handle_text(event: Event):
    """文本消息处理器"""
    if plugin:
        return plugin.handle_text(event)
    return False


def register(event_bus, subscribe, context):
    """注册插件"""
    global plugin
    logger.info("🧲 注册 magnet_search 插件...")

    try:
        plugin = MagnetSearchPlugin(context=context)
        context.health.register(lambda: {
            "status": "healthy" if plugin is not None else "unhealthy",
            "message": "磁力搜索服务已就绪" if plugin is not None else "磁力搜索服务未初始化",
        })
        context.register_cleanup(unregister)
        subscribe(
            event_type=EventType.TEXT_MESSAGE_RECEIVED,
            handler=handle_text
        )
        logger.info("✅ magnet_search 插件注册成功")
    except Exception as e:
        logger.error(f"❌ magnet_search 插件注册失败: {e}", exc_info=True)


def unregister():
    """取消注册插件"""
    global plugin
    logger.info("🧲 卸载 magnet_search 插件...")
    plugin = None
    logger.info("✅ magnet_search 插件卸载完成")
