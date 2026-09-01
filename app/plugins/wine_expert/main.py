"""
红酒鉴定临时插件（发图版）
流程：
1) 用户发文本命中触发词 -> 提示用户发送需要鉴定的图片（120秒内）
2) 用户发送图片 -> 通过 wx_manager.download_image_message(...) 下载（wx_bot 内部使用 msg.download()）
3) 调用 Gemini（可选 Google Search）生成鉴定与市场比价结果并回发
"""

import base64
import logging
import mimetypes
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from app.core.event_bus import Event, EventType
from app.utils.plugin_config import get_config
from app.services.llm_manager import get_llm_manager

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """
# Role: 全球名酒分析师

## Profile
- **角色定位**: 资深全品类酒水鉴定师 + 二级市场价格分析师
- **服务对象**: 微信端的高净值酒类爱好者/投资者
- **核心能力**: 跨品类精准识别（葡萄酒/威士忌/白酒/清酒/白兰地等）、全球实时比价、冷峻客观的购买建议。
- **语言风格**: 极简、专业、客观（类似彭博社终端风格），拒绝任何营销废话。

## Instructions
当用户输入酒类图片或文字描述时，请严格遵循以下步骤：

1.  **分类与识别 (关键)**:
    - 首先判断酒类属性（葡萄酒、威士忌、白酒、清酒等）。
    - 识别核心要素：酒庄/品牌、具体年份/批次 (Vintage/Batch)、产区/香型、等级/陈年时间。
    - **[必须执行]** 根据酒类调用对应权威库：
        - *葡萄酒*: Wine-Searcher, Vivino
        - *威士忌/烈酒*: Whiskybase, Master of Malt
        - *清酒*: Sakenomy, Saketime
        - *白酒*: 国内主流电商及拍卖行情

2.  **数据清洗**:
    - 剔除明显的最高价和最低价，寻找中位区间。
    - 将外币（USD/EUR/JPY/GBP）按当前大致汇率换算为人民币 (CNY) 供参考。

3.  **动态字段适配**:
    - 根据酒类调整输出模板中的特定字段（详见 Output Format 说明）。

4.  **输出生成**:
    - 严格按照【Output Format】输出，不要添加任何开场白。

## Constraints (零容忍规则)
1.  **年份/版本敏感**: 价格必须对应具体年份、批次或版本（如威士忌的Bottled年份或白酒的生产日期）。
2.  **拒绝瞎猜**: 若搜索不到确切价格，必须输出："⚠️ 该特定版本暂无公开成交数据，无法估价"。
3.  **排版**: 专为手机屏优化，使用emoji作为列表符，段落之间空一行，不得使用markdown代码块。

## Output Format (严格执行)

📦 【酒款识别】
[酒名中文]
[酒名外文原文]
🏰 厂牌: [酒庄/蒸馏厂/酒厂名]
📅 年份: [年份/陈年时间/批次] (若无年份需标注NV或NAS)
📍 产区: [国家] > [具体产区]
⚙️ 规格: [葡萄品种] 或 [桶型/度数/香型/精米步合] (根据酒类自动适配)

🍷 【风味与评分】
🏷️ 风格: [关键词1] | [关键词2] | [关键词3]
💎 评级: [RP/WS/JS/WhiskyBase/Sakenomy 评分] (若无则不显示)
⏳ 周期: [适饮期 / 收藏潜力 / 陈年能力]

🥣 【侍酒/饮用指南】
🌡️ 温度: [建议饮用温度]°C
🏺 建议: [红酒:醒酒建议] 或 [烈酒:纯饮/加冰/加水] 或 [清酒:冷饮/温酌]
🥂 杯型: [波尔多杯 / 闻香杯 / 茅台杯 / 蛇目杯等]

💰 【市场参考】
📊 国际行情: [¥ 人民币区间] (或 原币价格)
✅ 理性入手: [¥ 价格] 以内 (国内行货合理价)
🔗 参考源: [Wine-Searcher / Whiskybase / 拍卖行等]

🛒 【鉴定结论】
💡 一句话点评: [极简犀利的购买/投资/饮用建议]
"""


@dataclass
class PendingSession:
    chat_name: str
    sender: str
    created_at: float
    expires_at: float


class WineExpertTempPlugin:
    def __init__(self, context) -> None:
        self.context = context
        # 配置
        plugin_name = "wine_expert"
        self.trigger_keywords: List[str] = get_config("trigger_keywords", plugin_name=plugin_name) or []
        self.collect_timeout: int = int(get_config("collect_timeout", plugin_name=plugin_name) or 120)
        self.search_enabled: bool = bool(get_config("search_enabled", plugin_name=plugin_name))

        # LLM Manager
        self.llm_manager = get_llm_manager()

        # 会话：按 (chat_name, sender) 隔离
        # 会话：按 (chat_name, sender) 隔离
        self._sessions: Dict[Tuple[str, str], PendingSession] = {}
        self._last_images = {} # {chat_name: {'path': str, 'time': float}}
        self._lock = threading.RLock()

        logger.info("🍷 wine_expert 插件加载完毕")
        logger.info(f"   触发词: {self.trigger_keywords}")
        logger.info(f"   等待超时: {self.collect_timeout}秒")
        logger.info(f"   搜索增强: {'✅ 开启' if self.search_enabled else '❌ 关闭'}")

    def _is_triggered(self, message: str) -> bool:
        if not message:
            return False
        return any(kw in message for kw in self.trigger_keywords)

    def _key(self, chat_name: str, sender: str) -> Tuple[str, str]:
        return (chat_name or "", sender or "")

    def _start_timeout_watch(self, key: Tuple[str, str], wx_manager: Any) -> None:
        """倒计时超时清理（每个会话一个线程，轻量）"""
        while True:
            time.sleep(1.0)
            with self._lock:
                session = self._sessions.get(key)
                if not session:
                    return
                if time.time() < session.expires_at:
                    continue

                # 超时：发消息并清理
                chat_name, sender = key
                del self._sessions[key]
                try:
                    if wx_manager:
                        wx_manager.send_message(
                            chat_name,
                            f"⏰ 已等待 {self.collect_timeout} 秒未收到图片，已取消本次鉴定（@{sender}）。请重新发送“鉴定”等指令后再发图。",
                        )
                except Exception:  # noqa: BLE001
                    pass
                return

    def handle_text_message(self, event: Event) -> bool:
        message = (event.data.get("message") or "").strip()
        chat_name = event.data.get("chat_name") or ""
        sender = event.data.get("sender") or ""
        wx_manager = event.context.get("wx")

        if not self._is_triggered(message):
            return False

        key = self._key(chat_name, sender)
        now = time.time()
        expires_at = now + self.collect_timeout

        with self._lock:
            # 覆盖/刷新会话
            self._sessions[key] = PendingSession(
                chat_name=chat_name,
                sender=sender,
                created_at=now,
                expires_at=expires_at,
            )

        # 启动超时线程（不持锁）
        self.context.workers.start(
            f"session-timeout-{chat_name}-{sender}-{time.time_ns()}",
            self._start_timeout_watch,
            args=(key, wx_manager),
        )

        if wx_manager:
            wx_manager.send_message(
                chat_name,
                f"🍷 开始使用鉴酒模式（@{sender}）。请在 {self.collect_timeout} 秒内发送需要鉴定的图片。",
            )
        return True

    def handle_image_message(self, event: Event) -> bool:

        chat_name = event.data.get("chat_name") or ""
        sender = event.data.get("sender") or ""
        message_id = event.data.get("message_id")
        wx_manager = event.context.get("wx")

        if not message_id:
            return False

        # ✅ 尝试获取并缓存图片路径 (即使无session也执行，以便供引用消息 fallback)
        file_path = event.data.get("file_path")

        # 如果事件中没带路径，尝试自己确保下载 (Cooperative with ChatBot)
        # 如果ChatBot跑在前面并下载了，理应更新event.data (但目前ChatBot并未更新event)。
        # 所以我们尝试自己下载 (wx_manager内部如果有缓存最好，没有就下载)
        # 为了避免阻塞，我们仅在 path 缺失时下载。
        if (not file_path or not Path(file_path).exists()) and wx_manager:
             # 注意：这里同步下载可能会阻塞事件循环，但在线程中处理?
             # handle_image_message 是在 worker thread 中调用的吗? 是的。
             try:
                 # 只有当 file_path 真的缺失时才下载
                 # 这是一个权衡：为了 robust fallback，我们可能重复下载。
                 # 但通常 ChatBot 已经下载了。如果我们能检测到就好了。
                 # 既然 ChatBot 日志显示已下载，说明文件在。但我们不知道文件名。
                 # 暂时不强制同步下载，除非我们处于 active session。
                 # 对于 passive cache，我们依赖 event.data 或者稍后的机制?
                 # 不，为了 fallback，必须知道路径。
                 # 让我们做一个非阻塞的尝试? 不行。
                 # 稍微激进一点：如果 ChatBot 是 120，我们将自己设为 130，
                 # 并且修改 ChatBot 让其回写 file_path 到 event.data。
                 pass
             except Exception:
                 pass

        # 暂时只记录已有的 (假设 ChatBot 回写了)
        if file_path and Path(file_path).exists():
            with self._lock:
                self._last_images[chat_name] = {
                    'path': str(file_path),
                    'time': time.time()
                }

        key = self._key(chat_name, sender)
        with self._lock:
            session = self._sessions.get(key)
            if not session:
                return False
            if time.time() >= session.expires_at:
                # 已超时，清理并忽略
                self._sessions.pop(key, None)
                return False

            # 收到图片：先清理会话，避免重复触发
            self._sessions.pop(key, None)

        if not wx_manager:
            return False

        # ✅ 缓存图片供 fallback 使用
        # 注意: 即使没有session，我们也应该尝试缓存图片(如果在事件中提供了路径)
        # 但目前逻辑是 'if not session: return'.
        # 为了支持 fallback，我们需要在这里拦截并缓存，即使 return False
        # 但为了最小化改动，我们只在 session 命中时缓存（或者 ChatBot 已经缓存了?）
        # 不，ChatBot 和 WineExpert 内存不共享。
        # 我们需要在 `if not session` 之前就尝试缓存。
        pass

        # 为了简单起见，我们只能在有 session 时缓存
        # 或者我们修改逻辑，允许 `handle_image_message` 在无 session 时也检查 path 并缓存
        # 但 `handle_image_message` 返回值决定是否传播? Unclear.
        # 我们这里暂时只在命中 session 时缓存 (这覆盖了大部分 "刚发图就引用" 的场景? 不，用户可能先发图(无session)，再引用)
        # 修正: 我们需要更靠前的缓存逻辑
        pass

        if not wx_manager:
            return False

        # 发“处理中”回执（Gemini + Search 可能较慢）
        try:
            wx_manager.send_message(chat_name, "🍷 已收到图片，正在检索全球行情，请稍候...")
        except Exception:  # noqa: BLE001
            pass

        self.context.workers.start(
            f"identify-{chat_name}-{message_id}",
            self._process_async,
            args=(wx_manager, chat_name, message_id),
        )
        return True

    def handle_quote_image_message(self, event: Event) -> bool:
        """处理引用图片消息"""
        message = (event.data.get("message") or "").strip()
        chat_name = event.data.get("chat_name") or ""
        sender = event.data.get("sender") or ""
        message_id = event.data.get("message_id")
        quote_content = event.data.get("quote_content", "")
        wx_manager = event.context.get("wx")

        # 1. 检查是否触发
        if not self._is_triggered(message):
            return False

        # 2. 检查是否为图片引用
        if "[图片]" not in quote_content:
            return False

        if not wx_manager or not message_id:
            return False

        # 发“处理中”回执
        try:
            wx_manager.send_message(chat_name, "🍷 已收到引用图片，正在检索全球行情，请稍候...")
        except Exception:
            pass

        # 3. 下载引用图片 (异步)
        def _download_and_process():
            image_path = None
            try:
                # 尝试按需下载
                logger.info(f"🍷 wine_expert: 下载引用图片 {chat_name}:{message_id}")
                image_path = wx_manager.download_quote_image(chat_name, message_id=message_id)

                if image_path and Path(image_path).exists():
                     self._process_async(wx_manager, chat_name, message_id, direct_image_path=image_path)
                else:
                    # Fallback: Check cached images if direct download failed
                    found_in_cache = False
                    if chat_name in self._last_images:
                        cached = self._last_images[chat_name]
                        if time.time() - cached['time'] < 300: # 5 mins
                             cached_path = cached['path']
                             if Path(cached_path).exists():
                                 image_path = cached_path
                                 logger.info(f"🍷 wine_expert: 使用缓存图片 fallback: {image_path}")
                                 self._process_async(wx_manager, chat_name, message_id, direct_image_path=image_path)
                                 found_in_cache = True

                    if not found_in_cache:
                        wx_manager.send_message(chat_name, "⚠️ 引用图片下载失败，请尝试直接发送图片。")
            except Exception as e:
                logger.error(f"❌ wine_expert 引用处理失败: {e}")
                wx_manager.send_message(chat_name, "⚠️ 处理失败，请稍后重试。")

        self.context.workers.start(
            f"quote-identify-{chat_name}-{message_id}",
            _download_and_process,
        )

        # 4. 阻止事件继续传播(防止chatbot回复)
        logger.info(f"🍷 wine_expert 拦截引用图片事件: {chat_name}")
        return True

    def _guess_mime(self, file_path: str) -> str:
        mime, _ = mimetypes.guess_type(file_path)
        if mime and mime.startswith("image/"):
            return mime
        # 兜底：mabowx 默认保存多为 jpg
        return "image/jpeg"

    def _process_async(self, wx_manager: Any, chat_name: str, message_id: str, direct_image_path: Optional[str] = None) -> None:
        temp_path: Optional[str] = direct_image_path
        try:
            if not temp_path:
                logger.info(f"🍷 开始下载图片消息: {chat_name}:{message_id}")
                temp_path = wx_manager.download_image_message(chat_name, message_id)

            if not temp_path or not Path(temp_path).exists():
                wx_manager.send_message(chat_name, "⚠️ 图片下载失败，请重新发送图片后再试。")
                return

            # Read image and convert to base64
            with open(temp_path, "rb") as f:
                image_bytes = f.read()

            import base64
            mime_type = self._guess_mime(temp_path)
            base64_image = base64.b64encode(image_bytes).decode('utf-8')
            image_url = f"data:{mime_type};base64,{base64_image}"

            # Prepare messages in OpenAI format
            messages = [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url}
                        },
                        {
                            "type": "text",
                            "text": "请严格按照系统设定完成红酒鉴定与市场比价。"
                        }
                    ]
                }
            ]

            # Call LLM Manager with Google Search tool if enabled
            kwargs = {}
            if self.search_enabled:
                # LiteLLM format for Google Search
                kwargs["tools"] = [{"googleSearch": {}}]

            logger.info(f"🍷 调用 LLM Manager (Search: {self.search_enabled})")

            response_text = self.llm_manager.call(
                plugin_name="wine_expert",
                call_type="identify",
                messages=messages,
                **kwargs
            )

            if response_text:
                wx_manager.send_message(chat_name, response_text)
                logger.info("✅ 鉴定结果已发送")
            else:
                wx_manager.send_message(
                    chat_name,
                    "⚠️ 鉴定失败：AI 未能生成有效内容，可能由于图片模糊或内容受限。",
                )
        except Exception as e:  # noqa: BLE001
            logger.error(f"❌ wine_expert 处理异常: {e}", exc_info=True)
            try:
                wx_manager.send_message(chat_name, "⚠️ 服务暂时不可用，请稍后重试。")
            except Exception:  # noqa: BLE001
                pass
        finally:
            # 临时插件不主动删除下载文件（由 wx_bot / 系统策略管理）
            pass


plugin: Optional[WineExpertTempPlugin] = None


def handle_text_message(event: Event) -> bool:
    if plugin:
        return plugin.handle_text_message(event)
    return False



def handle_image_message(event: Event) -> bool:
    if plugin:
        return plugin.handle_image_message(event)
    return False


def handle_quote_image_message(event: Event) -> bool:
    if plugin:
        return plugin.handle_quote_image_message(event)
    return False


def register(event_bus, subscribe, context) -> None:
    global plugin
    plugin = WineExpertTempPlugin(context)
    context.health.register(lambda: {
        "status": "healthy" if plugin is not None else "unhealthy",
        "message": "红酒识别服务已就绪" if plugin is not None else "红酒识别服务未初始化",
    })
    context.register_cleanup(unregister)
    subscribe(EventType.TEXT_MESSAGE_RECEIVED, handle_text_message)
    subscribe(EventType.IMAGE_MESSAGE_RECEIVED, handle_image_message)
    subscribe(EventType.QUOTE_IMAGE_MESSAGE_RECEIVED, handle_quote_image_message)
    logger.info("🍷 wine_expert 已注册")


def unregister() -> None:
    global plugin
    plugin = None
    logger.info("🍷 wine_expert 已注销")
