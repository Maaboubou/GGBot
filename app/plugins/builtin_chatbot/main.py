"""
ChatBot 插件主模块
实现智能聊天机器人功能，包括角色扮演、上下文记忆和网络搜索
"""

import json
import re
import base64
import os
import time
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime
from app.core.event_bus import Event, EventType
from app.services.config_service import get_setting
from app.services.llm_manager import get_llm_manager
from app.plugins.builtin_chatbot.web_search import WebSearchService
from app.plugins.builtin_chatbot.chat_log import ChatLogManager
from app.plugins.builtin_chatbot.context_manager import ChatContextManager
from app.plugins.builtin_chatbot.memory_service import ChatMemoryService
from app.plugins.builtin_chatbot.role_manager import RoleManager
from app.plugins.builtin_chatbot.judge_manager import JudgeManager
from app.utils.dashboard_events import append_dashboard_event
from app.utils.bot_mentions import bot_names_for_user, find_bot_mention, strip_bot_mentions
from app.utils.plugin_config import get_config
from app.plugins.builtin_chatbot.ocr_utils import get_ocr_result


logger = logging.getLogger(__name__)


class ChatBotPlugin:
    """ChatBot 插件主类"""

    def __init__(self):
        # 从插件配置中读取参数
        self.bot_name = get_setting("WECHAT_BOT_NAME", "微信助手")  # 保留全局配置
        self.chat_log_manager = ChatLogManager()
        self.context_manager = ChatContextManager()
        self.memory_service = ChatMemoryService(
            self.chat_log_manager,
            self.context_manager,
        )
        self.web_search_service = WebSearchService()
        self.role_manager = RoleManager()
        self.judge_manager = JudgeManager()

        # 从插件自己的 config.json 读取配置
        plugin_name = "builtin_chatbot"
        self.codex_persistent_session_enabled = bool(
            get_config("codex_persistent_session_enabled", True, plugin_name=plugin_name)
        )
        codex_effort = str(
            get_config("codex_reasoning_effort", "inherit", plugin_name=plugin_name) or "inherit"
        ).strip().lower()
        self.codex_reasoning_effort = (
            codex_effort
            if codex_effort in {"inherit", "minimal", "low", "medium", "high", "xhigh"}
            else "inherit"
        )
        codex_summary = str(
            get_config("codex_reasoning_summary", "inherit", plugin_name=plugin_name) or "inherit"
        ).strip().lower()
        self.codex_reasoning_summary = (
            codex_summary
            if codex_summary in {"inherit", "none", "auto", "concise", "detailed"}
            else "inherit"
        )
        codex_search_mode = str(
            get_config("codex_web_search_mode", "inherit", plugin_name=plugin_name) or "inherit"
        ).strip().lower()
        self.codex_web_search_mode = (
            codex_search_mode
            if codex_search_mode in {"inherit", "disabled", "cached", "indexed", "live"}
            else "inherit"
        )
        self.codex_turn_timeout_seconds = max(
            0,
            min(3600, int(get_config("codex_turn_timeout_seconds", 0, plugin_name=plugin_name) or 0)),
        )
        self.codex_max_turns_per_thread = max(
            0,
            min(10000, int(get_config("codex_max_turns_per_thread", 0, plugin_name=plugin_name) or 0)),
        )
        self.codex_exec_fallback_enabled = bool(
            get_config("codex_exec_fallback_enabled", True, plugin_name=plugin_name)
        )
        self.context_limit = int(get_config("context_limit", 30, plugin_name=plugin_name))
        self.max_context_tokens = int(get_config("max_context_tokens", 220000, plugin_name=plugin_name))
        self.context_window_auto_detect = bool(
            get_config("context_window_auto_detect", True, plugin_name=plugin_name)
        )
        self.context_safety_margin_tokens = int(
            get_config("context_safety_margin_tokens", 24576, plugin_name=plugin_name)
        )
        self.reserved_output_tokens = int(get_config("reserved_output_tokens", 8192, plugin_name=plugin_name))
        self.context_message_fetch_limit = int(get_config("context_message_fetch_limit", 300, plugin_name=plugin_name))
        self.context_window_strategy = str(get_config("context_window_strategy", "anchored_append", plugin_name=plugin_name) or "anchored_append")
        self.anchor_message_count = int(get_config("anchor_message_count", 300, plugin_name=plugin_name))
        self.anchor_rollover_prompt_tokens = int(get_config("anchor_rollover_prompt_tokens", 205000, plugin_name=plugin_name))
        self.memory_context_ratio = float(get_config("memory_context_ratio", 0.10, plugin_name=plugin_name))
        self.recent_context_ratio = float(get_config("recent_context_ratio", 0.35, plugin_name=plugin_name))
        self.ephemeral_context_ratio = float(get_config("ephemeral_context_ratio", 0.10, plugin_name=plugin_name))
        self.ephemeral_context_max_tokens = int(
            get_config("ephemeral_context_max_tokens", 16000, plugin_name=plugin_name)
        )
        self.memory_enabled = bool(get_config("memory_enabled", True, plugin_name=plugin_name))
        self.memory_background_enabled = bool(
            get_config("memory_background_enabled", True, plugin_name=plugin_name)
        )
        self.memory_event_min_messages = int(
            get_config("memory_event_min_messages", 20, plugin_name=plugin_name)
        )
        self.memory_event_target_messages = int(
            get_config("memory_event_target_messages", 40, plugin_name=plugin_name)
        )
        self.memory_event_max_messages = int(
            get_config("memory_event_max_messages", 60, plugin_name=plugin_name)
        )
        self.memory_event_context_before_messages = int(
            get_config(
                "memory_event_context_before_messages",
                12,
                plugin_name=plugin_name,
            )
        )
        self.memory_event_context_after_messages = int(
            get_config(
                "memory_event_context_after_messages",
                12,
                plugin_name=plugin_name,
            )
        )
        self.memory_event_max_cards = int(
            get_config("memory_event_max_cards", 6, plugin_name=plugin_name)
        )
        self.memory_event_input_token_budget = int(
            get_config("memory_event_input_token_budget", 16000, plugin_name=plugin_name)
        )
        self.memory_initial_backfill_messages = int(
            get_config("memory_initial_backfill_messages", 2000, plugin_name=plugin_name)
        )
        self.memory_max_chunks_per_run = int(
            get_config("memory_max_chunks_per_run", 3, plugin_name=plugin_name)
        )
        self.memory_stage_event_threshold = int(
            get_config("memory_stage_event_threshold", 40, plugin_name=plugin_name)
        )
        self.memory_stage_input_event_limit = int(
            get_config("memory_stage_input_event_limit", 80, plugin_name=plugin_name)
        )
        self.memory_stage_input_token_budget = int(
            get_config("memory_stage_input_token_budget", 24000, plugin_name=plugin_name)
        )
        self.memory_stage_char_limit = int(
            get_config("memory_stage_char_limit", 6000, plugin_name=plugin_name)
        )
        self.memory_retrieval_top_k = int(
            get_config("memory_retrieval_top_k", 6, plugin_name=plugin_name)
        )
        self.memory_query_recent_messages = int(
            get_config("memory_query_recent_messages", 12, plugin_name=plugin_name)
        )
        self.memory_context_max_tokens = int(
            get_config("memory_context_max_tokens", 6000, plugin_name=plugin_name)
        )
        self.memory_person_v3_enabled = bool(
            get_config("memory_person_v3_enabled", True, plugin_name=plugin_name)
        )
        self.memory_person_v3_auto_activate_live = bool(
            get_config(
                "memory_person_v3_auto_activate_live",
                True,
                plugin_name=plugin_name,
            )
        )
        self.memory_person_v3_person_centric_enabled = bool(
            get_config(
                "memory_person_v3_person_centric_enabled",
                True,
                plugin_name=plugin_name,
            )
        )
        self.memory_person_v3_min_pending_messages = int(
            get_config(
                "memory_person_v3_min_pending_messages",
                30,
                plugin_name=plugin_name,
            )
        )
        self.memory_person_v3_batch_related_messages = int(
            get_config(
                "memory_person_v3_batch_related_messages",
                80,
                plugin_name=plugin_name,
            )
        )
        self.memory_person_v3_max_batch_people = int(
            get_config(
                "memory_person_v3_max_batch_people",
                4,
                plugin_name=plugin_name,
            )
        )
        self.memory_person_v3_max_observations_per_batch = int(
            get_config(
                "memory_person_v3_max_observations_per_batch",
                16,
                plugin_name=plugin_name,
            )
        )
        self.memory_person_v3_input_token_budget = int(
            get_config(
                "memory_person_v3_input_token_budget",
                24000,
                plugin_name=plugin_name,
            )
        )
        self.memory_person_v3_candidate_memory_value = float(
            get_config(
                "memory_person_v3_candidate_memory_value",
                0.58,
                plugin_name=plugin_name,
            )
        )
        self.memory_person_v3_refresh_threshold = int(
            get_config(
                "memory_person_v3_refresh_threshold",
                10,
                plugin_name=plugin_name,
            )
        )
        self.memory_person_v3_max_refresh_people = int(
            get_config(
                "memory_person_v3_max_refresh_people",
                4,
                plugin_name=plugin_name,
            )
        )
        self.memory_person_v3_retrieval_max_people = int(
            get_config(
                "memory_person_v3_retrieval_max_people",
                3,
                plugin_name=plugin_name,
            )
        )
        self.memory_person_v3_retrieval_max_items = int(
            get_config(
                "memory_person_v3_retrieval_max_items",
                12,
                plugin_name=plugin_name,
            )
        )
        self.memory_person_v3_include_high_sensitivity = bool(
            get_config(
                "memory_person_v3_include_high_sensitivity",
                False,
                plugin_name=plugin_name,
            )
        )
        self.memory_embedding_enabled = bool(
            get_config("memory_embedding_enabled", True, plugin_name=plugin_name)
        )
        self.memory_embedding_model = str(
            get_config(
                "memory_embedding_model",
                "BAAI/bge-small-zh-v1.5",
                plugin_name=plugin_name,
            )
            or "BAAI/bge-small-zh-v1.5"
        )
        self.memory_embedding_threads = int(
            get_config("memory_embedding_threads", 4, plugin_name=plugin_name)
        )
        self.memory_embedding_batch_size = int(
            get_config("memory_embedding_batch_size", 8, plugin_name=plugin_name)
        )
        self.memory_dedup_enabled = bool(
            get_config("memory_dedup_enabled", True, plugin_name=plugin_name)
        )
        self.memory_verification_enabled = bool(
            get_config(
                "memory_verification_enabled",
                True,
                plugin_name=plugin_name,
            )
        )
        self.memory_dedup_lookback_days = int(
            get_config("memory_dedup_lookback_days", 30, plugin_name=plugin_name)
        )
        self.memory_dedup_candidate_threshold = float(
            get_config(
                "memory_dedup_candidate_threshold",
                0.78,
                plugin_name=plugin_name,
            )
        )
        self.memory_duplicate_similarity_threshold = float(
            get_config(
                "memory_duplicate_similarity_threshold",
                0.90,
                plugin_name=plugin_name,
            )
        )
        self.memory_retrieval_mmr_lambda = float(
            get_config(
                "memory_retrieval_mmr_lambda",
                0.72,
                plugin_name=plugin_name,
            )
        )
        self.memory_retrieval_diversity_threshold = float(
            get_config(
                "memory_retrieval_diversity_threshold",
                0.92,
                plugin_name=plugin_name,
            )
        )
        self.memory_checkpoint_max_tokens = int(
            get_config("memory_checkpoint_max_tokens", 4000, plugin_name=plugin_name)
        )
        self.memory_retention_days = int(
            get_config("memory_retention_days", 0, plugin_name=plugin_name)
        )
        self.memory_candidate_retention_days = int(
            get_config(
                "memory_candidate_retention_days",
                90,
                plugin_name=plugin_name,
            )
        )
        self.memory_maintenance_interval_hours = int(
            get_config(
                "memory_maintenance_interval_hours",
                24,
                plugin_name=plugin_name,
            )
        )
        self.default_role = get_config("default_role", "default", plugin_name=plugin_name)
        self.search_enabled = get_config("search_enabled", True, plugin_name=plugin_name)
        self.ocr_enabled = get_config("ocr_enabled", False, plugin_name=plugin_name)

        # 群聊触发配置。主动回复是否启用以及触发/冷却参数分别由聊天权限和 Judge 管理。
        self.allow_mention_trigger = get_config("allow_mention_trigger", True, plugin_name=plugin_name)

        logger.info(
            f"🔧 ChatBot group trigger config: allow_mention_trigger={self.allow_mention_trigger}; "
            "proactive state is managed per chat and timing is managed by Judge"
        )

        # Processing locks to prevent concurrent proactive replies
        self._processing_locks = {}  # {chat_name: timestamp}
        self._lock_timeout = 30  # seconds

        # Judge cooldown tracking - prevents excessive API calls after rejections
        self._judge_cooldowns = {}  # {chat::judge: {'time': timestamp, 'msg_count': int, 'total_count': int}}

        # Anchored append context cache: keep the exact dynamic message prefix
        # sent to the LLM so later calls can append to it and reuse prefix caches.
        self._anchored_contexts: Dict[str, Dict[str, Any]] = {}
        self._anchored_context_dir = Path("data/chatbot_anchor_contexts")
        self._anchored_context_dir.mkdir(parents=True, exist_ok=True)

        # 注意：enabled_chats 权限检查已移至 EventBus 统一管理

        # 消息去重缓存，防止由于上游系统重复发布事件导致的多重回复
        self._message_dedup_cache = {}  # {(chat_name, sender, content_hash): timestamp}
        self._dedup_window = 3600.0  # 1小时去重窗口，防止重读历史消息导致重复回复

        # Per-chat follow-up sessions. Judge calls run outside the EventBus
        # worker; approved replies are published back into the same chat queue.
        self.event_bus = None
        self._followup_lock = threading.RLock()
        self._followup_sessions: Dict[str, Dict[str, Any]] = {}
        self._followup_executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="ChatBot-Followup",
        )
        self._followup_closed = False

        logger.info(
            "ChatBot插件初始化完成 - bot_name=%s codex_persistent=%s effort=%s search=%s",
            self.bot_name,
            self.codex_persistent_session_enabled,
            self.codex_reasoning_effort,
            self.codex_web_search_mode,
        )

    def _is_search_enabled(self) -> bool:
        """动态读取网络搜索开关，使 Web 端配置修改无需重启插件即可生效。"""
        value = get_config("search_enabled", self.search_enabled, plugin_name="builtin_chatbot")
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _bot_names_for_chat(self, chat_name: str) -> List[str]:
        """返回群级昵称别名；事件总线正常会预先完成匹配。"""
        try:
            from app.models.base import SessionLocal
            from app.models.user_permission import WeChatUser

            with SessionLocal() as db:
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                if user:
                    return bot_names_for_user(user, self.bot_name)
        except Exception as exc:
            logger.debug("🤖 读取群内机器人别名失败: %s", exc)
        return [self.bot_name]

    def _bot_display_name_for_chat(self, chat_name: str) -> str:
        names = self._bot_names_for_chat(chat_name)
        return names[0] if names else self.bot_name

    def _event_mentions_bot(self, event: Event) -> bool:
        if "bot_mentioned" in event.data:
            return bool(event.data.get("bot_mentioned"))
        return find_bot_mention(
            event.data.get("message", ""),
            self._bot_names_for_chat(str(event.data.get("chat_name") or "")),
        ) is not None

    def _ingress_mentions_bot(self, chat_name: str, ingress: Dict[str, Any]) -> bool:
        if "bot_mentioned" in ingress:
            return bool(ingress.get("bot_mentioned"))
        return find_bot_mention(
            ingress.get("message", ""),
            self._bot_names_for_chat(chat_name),
        ) is not None

    def _should_run_framework_search(self, chat_name: str = "") -> bool:
        """Only pre-search when enabled and the reply model is not local Codex."""
        if not self._is_search_enabled():
            logger.debug("🔍 Framework pre-search disabled, skipping for %s", chat_name)
            return False

        try:
            uses_codex = get_llm_manager().is_local_codex_call(
                "builtin_chatbot",
                "chat",
            )
        except Exception as exc:
            logger.warning(
                "⚠️ Failed to detect chatbot reply provider; retaining framework pre-search: %s",
                exc,
            )
            return True

        if uses_codex:
            logger.info(
                "🔍 Chatbot reply model is local Codex; skipping framework pre-search for %s",
                chat_name,
            )
            return False
        return True

    def reload_roles(self):
        """重新加载角色配置"""
        logger.info("🔄 ChatBot插件重新加载角色配置...")
        self.role_manager.reload_roles()
        logger.info("✅ ChatBot插件角色配置重新加载完成")

    def reload_judges(self):
        """重新加载 Judge 配置"""
        logger.info("🔄 ChatBot插件重新加载 Judge 配置...")
        self.judge_manager.reload_judges()
        logger.info("✅ ChatBot插件 Judge 配置重新加载完成")

    def handle_text_message(self, event: Event):
        """处理文本消息事件"""
        try:
            logger.info(f"🤖 ChatBot plugin received text message event")

            # 获取事件数据，保持与其他插件一致的数据结构
            content = event.data.get("message", "")
            chat_name = event.data.get("chat_name", "")
            sender = event.data.get("sender", "")
            chat_type = event.data.get("chat_type", "private")
            quote_content = event.data.get("quote_content", "") or ""
            followup_approved = bool(event.data.get("_followup_approved"))
            llm_content = self._build_quote_augmented_content(content, quote_content)
            if llm_content != content:
                logger.info(
                    "🤖 Quote text context injected for %s: content='%s', quote='%s'",
                    chat_name,
                    str(content)[:50],
                    str(quote_content)[:120],
                )

            if self._is_sender_ignored(chat_name, sender):
                logger.info(f"🤖 Ignored blacklisted sender: chat={chat_name}, sender={sender}")
                return False

            if not followup_approved:
                # ---- 消息去重逻辑 ----
                import hashlib
                message_id = str(event.data.get("message_id") or "").strip()
                fingerprint_source = message_id or f"{content}|{quote_content}"
                content_hash = hashlib.md5(fingerprint_source.encode('utf-8')).hexdigest()
                dedup_key = (chat_name, sender, content_hash)
                now = time.time()
                if dedup_key in self._message_dedup_cache:
                    if now - self._message_dedup_cache[dedup_key] < self._dedup_window:
                        logger.warning(f"⚠️ 检测到重复消息事件，跳过处理: chat={chat_name}, sender={sender}, content='{content[:30]}...'")
                        return False
                self._message_dedup_cache[dedup_key] = now
                # 清理过期缓存
                expired_keys = [k for k, t in self._message_dedup_cache.items() if now - t > self._dedup_window * 2]
                for k in expired_keys:
                    del self._message_dedup_cache[k]
                # --------------------

            # Every accepted message may advance the asynchronous event-memory
            # cursor, even when the proactive judge later decides not to reply.
            memory_config = self._get_chat_memory_config(chat_name)
            if not followup_approved:
                self.memory_service.schedule(chat_name, memory_config)

            # 初始化变量，防止在finally块中访问未定义变量
            is_mention = False
            proactive_processing_acquired = False

            # ✨ 检测是否为误识别的引用图片消息
            quote_detection = self._detect_misidentified_quote_image(content)
            if quote_detection:
                # 重构event.data，模拟引用图片消息
                logger.info(f"🔄 将误识别的text消息转发为quote_image事件处理")

                # 使用前缀作为实际消息内容
                event.data["message"] = quote_detection["prefix"] if quote_detection["prefix"] else content
                event.data["quote_content"] = "[图片]"
                event.data["has_quote_image"] = True

                # 直接调用引用图片处理方法
                return self.handle_quote_image_message(event)

            logger.info(f"🤖 Message from {sender} in {chat_name}: {content}")

            # 检查是否为@消息
            is_mention = self._event_mentions_bot(event)

            if followup_approved:
                if not self._followup_approval_is_current(event):
                    logger.info(
                        "🔗 Follow-up approval became stale before reply generation: %s",
                        chat_name,
                    )
                    return False
            elif is_mention:
                # Explicit triggers always own the turn. Cancelling here plus
                # ingress-sequence checks prevents an in-flight Judge from
                # producing a second reply.
                self._cancel_followup_pending(chat_name, reason="explicit_mention")
            elif chat_type == "group" and self._schedule_followup_candidate(
                event,
                llm_content,
            ):
                return False

            # 群聊触发策略：
            # 1) @开启 + 命中@：被动直通回复（不走Judge）
            # 2) 其余群聊场景：走主动Judge链路（包含 @关闭 时的@消息）
            should_use_group_judge = (
                chat_type == "group"
                and not followup_approved
                and (not is_mention or not self.allow_mention_trigger)
            )
            if should_use_group_judge:
                # 主动回复仅由聊天级权限控制。
                if not self._check_proactive_permission(chat_name):
                    logger.debug(f"Proactive disabled for user {chat_name}")
                    return False

                # 2. 检查消息时效性 (避免回复历史消息/过久的消息)
                # Event timestamp is float seconds
                event_time = event.timestamp
                if time.time() - event_time > 3 * 60:  # 3分钟超时
                    logger.debug(f"⏳ Skipping proactive check for stale message (lag: {int(time.time() - event_time)}s)")
                    return False

                # 3. 获取用户角色与 Judge 配置。Judge 自带触发/冷却参数。
                role_name = self._get_user_role(chat_name)
                judge_name = self._get_user_judge(chat_name)
                if not judge_name:
                    logger.debug(f"⚖️ No judge binding for {chat_name}, proactive judge disabled")
                    return False

                judge_timing = self.judge_manager.get_judge_timing(judge_name)

                # 4. 分析聊天状态 (沉默时长 & 用于判断的上下文)
                scan_threshold = max(
                    judge_timing["trigger_msg_threshold"],
                    judge_timing["cooldown_msg_threshold"],
                )
                state = self._analyze_chat_state(chat_name, scan_threshold=scan_threshold)
                msg_count = state['msg_count']
                last_time = state['last_reply_time']

                # 5. 阈值检查
                trigger_msg_threshold = judge_timing["trigger_msg_threshold"]
                trigger_interval_minutes = judge_timing["trigger_interval_minutes"]
                if msg_count < trigger_msg_threshold:
                    logger.debug(f"🤫 Not enough messages for proactive Judge[{judge_name}]: {msg_count}/{trigger_msg_threshold}")
                    return False

                if last_time:
                    minutes_since = (datetime.now() - last_time).total_seconds() / 60
                    if minutes_since < trigger_interval_minutes:
                        logger.debug(f"🤫 Too soon for proactive Judge[{judge_name}]: {int(minutes_since)}/{trigger_interval_minutes} min")
                        return False

                # 6. 检查裁判冷却（防止频繁调用API）
                # 传入 last_time 以检查是否有其他插件（如Summary）在冷却期间插话
                if not self._check_judge_cooldown(chat_name, judge_name, msg_count, judge_timing, last_time):
                    return False

                # 7. 检查是否正在处理中（防止并发）
                if self._is_processing(chat_name):
                    logger.debug(f"🔒 Already processing proactive reply for {chat_name}, skipping")
                    return False

                # 8. 获取上下文消息 (用于提交给裁判)
                # 注意：这里需要足够的上下文给裁判看
                judge_context = self._get_judge_context_messages(chat_name, 20)

                # 9. 裁判机制 (DeepSeek)
                logger.info(f"⚖️ Consulting Judge '{judge_name}' for {chat_name} (msgs: {msg_count})")

                if not self._consult_judge(judge_context, role_name, judge_name):
                    # 裁判拒绝 -> 设置冷却
                    self._set_judge_cooldown(chat_name, judge_name, msg_count, judge_timing)
                    return False

                # 10. 设置处理锁
                self._set_processing(chat_name)
                proactive_processing_acquired = True

                # 裁判通过 -> 继续执行后续回复逻辑
                logger.info(f"📢 Proactive reply triggered for {chat_name}")



            # 记录开始时间
            start_time = time.time()

            # 基础内容检查 (如果是@消息或者已经通过了主动检查，都会走到这里)
            if not self._should_respond(content, chat_type):
                logger.info(f"🤖 Should not respond to message from {sender}")
                return False

            # 注意：权限检查已移至 EventBus 统一管理，此处不再检查 enabled_chats

            # 获取用户角色配置 (如果前面主动逻辑已获取，这里会重复但无害，或者优化下)
            role_name = self._get_user_role(chat_name)

            # 获取较长上下文，实际入模内容由 token 预算动态裁剪
            context_msgs = self.chat_log_manager.get_context_messages(
                chat_name,
                self._memory_source_fetch_limit(memory_config),
            )
            memory_context, memory_stats = self.memory_service.build_retrieval_context(
                chat_name,
                sender=sender,
                content=llm_content,
                recent_messages=context_msgs,
                config=memory_config,
            )

            # 进行网络搜索（统一方法）
            search_results = ""
            if self._should_run_framework_search(chat_name):
                # 确保搜索上下文包含当前消息；引用文字消息使用增强后的本轮内容，避免只搜“核实一下”
                search_context = context_msgs[-self.context_limit:] + [{"sender": sender, "content": llm_content}]
                search_results = self.web_search_service.search(
                    search_context,
                    bot_names=self._bot_names_for_chat(chat_name),
                )

            # 构建消息数组（包含 system prompt 和变量替换）
            role_name = self._get_user_role(chat_name)
            messages = self._build_messages_array(
                chat_name,
                context_msgs,
                search_results,
                sender,
                llm_content,
                role_name,
                memory_config,
                memory_context=memory_context,
            )
            logger.info(
                "🧠 Retrieval context for %s: events=%s people=%s tokens≈%s vector=%s",
                chat_name,
                memory_stats.get("event_count"),
                memory_stats.get("people_count"),
                memory_stats.get("tokens", 0),
                memory_stats.get("vector_ready"),
            )

            verified_memory_trace = self._reconcile_memory_trace(
                memory_stats.get("trace"),
                messages,
            )

            if followup_approved and not self._followup_approval_is_current(event):
                logger.info(
                    "🔗 Follow-up approval became stale before main model call: %s",
                    chat_name,
                )
                self._discard_stale_followup_model_result(
                    chat_name,
                    invalidate_provider=False,
                )
                return False

            # 调用 LLM Manager
            response_attachments: List[Dict[str, Any]] = []
            response = self._call_chat_llm(
                chat_name,
                role_name,
                messages,
                _wxautox_attachment_capture=response_attachments,
                _wxautox_memory_trace=verified_memory_trace,
            )
            if response_attachments:
                response = self._strip_internal_action_markers(response)

            # 检查搜索是否失败，按需在回复后追加 emoji
            search_failed = bool(search_results and search_results.startswith("（⚠️ 网络搜索"))
            display_response = response
            if search_failed:
                display_response += "⚠️⛓️‍💥"

            # 发送回复
            wx_manager = event.context.get("wx")
            if wx_manager:
                # 方案优化: 为了不让 emoji (⚠️⛓️‍💥) 和 JSON 包装污染上下文
                has_response_text = bool(self._format_response_parts(display_response, self.role_manager.get_output_settings(role_name)))
                if followup_approved and not self._followup_approval_is_current(event):
                    logger.info(
                        "🔗 Discarding stale follow-up response before send: %s",
                        chat_name,
                    )
                    self._discard_stale_followup_model_result(chat_name)
                    return False
                if has_response_text:
                    # 发送实际带 emoji 的内容，但只在每一段确认发送成功后，
                    # 才把对应的干净内容写入上下文日志。
                    result = self._send_response_parts(
                        wx_manager,
                        chat_name,
                        display_response,
                        role_name,
                        silent=True,
                        log_response=response,
                    )
                else:
                    result = bool(response_attachments)

                if result:
                    if response_attachments:
                        result = self._send_response_attachments(wx_manager, chat_name, response_attachments)
                    if not result:
                        logger.error(f"🤖 Failed to send chatbot attachment(s) to {chat_name}")
                        return False
                    self._finalize_anchored_context(chat_name, response)
                    self._open_followup_window(
                        chat_name=chat_name,
                        chat_type=chat_type,
                        event=event,
                        context_messages=context_msgs,
                        response=response,
                        role_name=role_name,
                        automatic=followup_approved,
                    )
                    logger.info(f"🤖 Sent chatbot response to {chat_name}")

                    # 记录 E2E 响应时间
                    duration = time.time() - start_time
                    try:
                        # 使用 "reply_latency" 作为 call_type, "system" 作为 model
                        get_llm_manager()._record_stats("builtin_chatbot", "reply_latency", "system", None, duration)
                        logger.info(f"⏱️ E2E Reply Latency: {duration:.2f}s")
                    except Exception as e:
                        logger.warning(f"Failed to record latency: {e}")

                    # 清除裁判冷却（成功回复后重置）
                    self._clear_judge_cooldowns(chat_name)

                    return True
                else:
                    logger.error(f"🤖 Failed to send chatbot response to {chat_name}")
            else:
                logger.error("🤖 WeChat manager not available")
            return False
        except Exception as e:
            logger.error(f"🤖 ChatBot处理文本消息失败: {e}")
            # 主动模式下出错不发送错误提示给用户，避免打扰
            if not self._event_mentions_bot(event):
                 return False

            wx_manager = event.context.get("wx")
            if wx_manager:
                wx_manager.send_message(chat_name, "⚠️ AI自动回复失败，请稍后再试～")
            return False
        finally:
            # 清除处理锁（仅本次事件曾拿到锁）
            if proactive_processing_acquired:
                self._clear_processing(chat_name)


    def _check_proactive_permission(self, chat_name: str) -> bool:
        """检查用户是否开启了主动回复权限"""
        try:
            from app.models.base import SessionLocal
            from app.models.user_permission import UserPermission, WeChatUser

            with SessionLocal() as db:
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                if not user:
                    return False

                # 查找 plugin='builtin_chatbot' 的权限
                perm = db.query(UserPermission).filter(
                    UserPermission.user_id == user.id,
                    UserPermission.plugin_name == 'builtin_chatbot'
                ).first()

                if perm and perm.proactive_enabled:
                    return True
            return False
        except Exception as e:
            logger.error(f"❌ Error checking proactive permission: {e}")
            return False

    def _get_followup_config(self, chat_name: str) -> Dict[str, Any]:
        permission = self._get_user_permission_config(chat_name)
        return {
            "enabled": bool(permission.get("followup_enabled", False)),
            "window_seconds": max(
                10,
                min(600, int(permission.get("followup_window_seconds") or 60)),
            ),
            "merge_seconds": max(
                1,
                min(30, int(permission.get("followup_merge_seconds") or 3)),
            ),
            "max_turns": max(
                1,
                min(10, int(permission.get("followup_max_turns") or 3)),
            ),
        }

    def _get_chat_ingress_state(self, chat_name: str) -> Dict[str, Any]:
        getter = getattr(self.event_bus, "get_chat_ingress_state", None)
        if not callable(getter):
            return {"sequence": 0}
        try:
            return getter(chat_name)
        except Exception as exc:
            logger.warning("🔗 Failed to read ingress state for %s: %s", chat_name, exc)
            return {"sequence": 0}

    def _cancel_followup_pending(self, chat_name: str, *, reason: str) -> None:
        with self._followup_lock:
            session = self._followup_sessions.pop(chat_name, None)
            if not session:
                return
            timer = session.get("timer")
            if timer:
                timer.cancel()
            session["generation"] = int(session.get("generation") or 0) + 1
        logger.debug("🔗 Follow-up window cancelled for %s: %s", chat_name, reason)

    def _open_followup_window(
        self,
        *,
        chat_name: str,
        chat_type: str,
        event: Event,
        context_messages: List[Dict[str, Any]],
        response: str,
        role_name: str,
        automatic: bool,
    ) -> None:
        if chat_type != "group":
            self._cancel_followup_pending(chat_name, reason="not_group")
            return

        config = self._get_followup_config(chat_name)
        if not config["enabled"]:
            self._cancel_followup_pending(chat_name, reason="disabled")
            return

        response_parts = self._format_response_parts(
            response,
            self.role_manager.get_output_settings(role_name),
        )
        reply_text = "\n".join(response_parts).strip()
        if not reply_text:
            self._cancel_followup_pending(chat_name, reason="empty_reply")
            return

        now = time.time()
        anchor_id = uuid.uuid4().hex
        automatic_turns = (
            int(event.data.get("_followup_auto_turns") or 0) + 1
            if automatic
            else 0
        )
        context_before = []
        for message in (context_messages or [])[-2:]:
            context_before.append(
                {
                    "sender": str(message.get("sender") or ""),
                    "content": str(message.get("content") or ""),
                    "time": str(message.get("time") or ""),
                }
            )

        with self._followup_lock:
            previous = self._followup_sessions.get(chat_name)
            if previous and previous.get("timer"):
                previous["timer"].cancel()
            self._followup_sessions[chat_name] = {
                "anchor_id": anchor_id,
                "reply_sent_at": now,
                "expires_at": now + config["window_seconds"],
                "anchor_sequence": int(event.data.get("_followup_snapshot_seq") or event.data.get("_chat_seq") or 0),
                "automatic_turns": automatic_turns,
                "role_name": role_name,
                "reply_text": reply_text,
                "context_before": context_before,
                "window_seconds": config["window_seconds"],
                "merge_seconds": config["merge_seconds"],
                "max_turns": config["max_turns"],
                "pending_messages": [],
                "pending_first_at": 0.0,
                "generation": 0,
                "timer": None,
                "judge_inflight": False,
                "rerun_due": False,
                "judge_calls": 0,
                "last_judge_at": 0.0,
            }

        logger.info(
            "🔗 Follow-up window opened for %s: window=%ss merge=%ss turns=%s/%s",
            chat_name,
            config["window_seconds"],
            config["merge_seconds"],
            automatic_turns,
            config["max_turns"],
        )

    def _schedule_followup_candidate(self, event: Event, content: str) -> bool:
        """Own an unmentioned group message when a reply-follow-up window is active."""
        chat_name = str(event.data.get("chat_name") or "")
        sender = str(event.data.get("sender") or "")
        if not chat_name or not sender or sender == self.bot_name or not str(content or "").strip():
            return False

        config = self._get_followup_config(chat_name)
        if not config["enabled"]:
            return False

        event_time = float(event.timestamp or time.time())
        event_sequence = int(event.data.get("_chat_seq") or 0)
        now = time.time()
        wx_manager = event.context.get("wx")
        wx_manager = getattr(wx_manager, "_wx", wx_manager)

        with self._followup_lock:
            session = self._followup_sessions.get(chat_name)
            if not session:
                return False
            if event_time > float(session.get("expires_at") or 0):
                self._followup_sessions.pop(chat_name, None)
                return False
            if event_time <= float(session.get("reply_sent_at") or 0):
                logger.debug(
                    "🔗 Ignoring pre-reply queued message for follow-up: %s seq=%s",
                    chat_name,
                    event_sequence,
                )
                return True
            if int(session.get("automatic_turns") or 0) >= config["max_turns"]:
                logger.info("🔗 Follow-up turn limit reached for %s", chat_name)
                return True

            session["window_seconds"] = config["window_seconds"]
            session["merge_seconds"] = config["merge_seconds"]
            session["max_turns"] = config["max_turns"]
            session["expires_at"] = min(
                float(session.get("expires_at") or now),
                float(session.get("reply_sent_at") or now) + config["window_seconds"],
            )
            session["generation"] = int(session.get("generation") or 0) + 1
            if not session.get("pending_messages"):
                session["pending_first_at"] = now
            session["pending_messages"].append(
                {
                    "sequence": event_sequence,
                    "event_timestamp": event_time,
                    "sender": sender,
                    "content": str(content),
                    "event_data": {
                        key: value
                        for key, value in event.data.items()
                        if key not in {"_consumed", "_followup_approved"}
                    },
                    "wx": wx_manager,
                }
            )
            session["pending_messages"] = session["pending_messages"][-8:]

            if int(session.get("judge_calls") or 0) >= 2:
                logger.debug("🔗 Follow-up Judge call cap reached for %s", chat_name)
                return True

            self._arm_followup_timer_locked(chat_name, session)

        logger.debug(
            "🔗 Follow-up candidate queued for %s: seq=%s merge=%ss",
            chat_name,
            event_sequence,
            config["merge_seconds"],
        )
        return True

    def _arm_followup_timer_locked(
        self,
        chat_name: str,
        session: Dict[str, Any],
    ) -> None:
        timer = session.get("timer")
        if timer:
            timer.cancel()
        if session.get("judge_inflight"):
            session["rerun_due"] = True
            session["timer"] = None
            return
        if not session.get("pending_messages"):
            session["timer"] = None
            return

        now = time.time()
        merge_seconds = float(session.get("merge_seconds") or 3)
        first_at = float(session.get("pending_first_at") or now)
        due_at = min(
            now + merge_seconds,
            first_at + merge_seconds * 2,
            float(session.get("expires_at") or (now + merge_seconds)),
        )
        last_judge_at = float(session.get("last_judge_at") or 0)
        if last_judge_at:
            due_at = min(
                max(due_at, last_judge_at + 8),
                float(session.get("expires_at") or due_at),
            )
        delay = max(0.05, due_at - now)
        generation = int(session.get("generation") or 0)
        anchor_id = str(session.get("anchor_id") or "")
        timer = threading.Timer(
            delay,
            self._submit_followup_judge,
            args=(chat_name, anchor_id, generation),
        )
        timer.daemon = True
        session["timer"] = timer
        timer.start()

    def _submit_followup_judge(
        self,
        chat_name: str,
        anchor_id: str,
        generation: int,
    ) -> None:
        with self._followup_lock:
            if self._followup_closed:
                return
            session = self._followup_sessions.get(chat_name)
            if (
                not session
                or session.get("anchor_id") != anchor_id
                or int(session.get("generation") or 0) != generation
            ):
                return
            session["timer"] = None
            if session.get("judge_inflight"):
                session["rerun_due"] = True
                return
            if int(session.get("judge_calls") or 0) >= 2:
                return

            pending = list(session.get("pending_messages") or [])
            if not pending:
                return
            if float(pending[-1].get("event_timestamp") or 0) > float(
                session.get("expires_at") or 0
            ):
                session["pending_messages"] = []
                session["pending_first_at"] = 0.0
                return
            snapshot_sequence = int(pending[-1].get("sequence") or 0)
            ingress = self._get_chat_ingress_state(chat_name)
            if int(ingress.get("sequence") or 0) != snapshot_sequence:
                session["pending_messages"] = []
                session["pending_first_at"] = 0.0
                return
            if self._ingress_mentions_bot(chat_name, ingress):
                session["pending_messages"] = []
                session["pending_first_at"] = 0.0
                return

            session["pending_messages"] = []
            session["pending_first_at"] = 0.0
            session["judge_inflight"] = True
            session["rerun_due"] = False
            session["judge_calls"] = int(session.get("judge_calls") or 0) + 1
            session["last_judge_at"] = time.time()
            snapshot = {
                "anchor_id": anchor_id,
                "snapshot_sequence": snapshot_sequence,
                "role_name": str(session.get("role_name") or self.default_role),
                "context_before": list(session.get("context_before") or []),
                "reply_text": str(session.get("reply_text") or ""),
                "pending": pending,
                "automatic_turns": int(session.get("automatic_turns") or 0),
                "expires_at": float(session.get("expires_at") or 0),
            }

        try:
            self._followup_executor.submit(
                self._run_followup_judge,
                chat_name,
                snapshot,
            )
        except RuntimeError:
            with self._followup_lock:
                session = self._followup_sessions.get(chat_name)
                if session and session.get("anchor_id") == anchor_id:
                    session["judge_inflight"] = False

    def _run_followup_judge(
        self,
        chat_name: str,
        snapshot: Dict[str, Any],
    ) -> None:
        should_reply = False
        reason = ""
        try:
            judge_text = self._build_followup_judge_text(snapshot)
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是群聊连续对话分类器，只判断最新一小段消息是否明确在继续和机器人对话。"
                        "判断必须保守。只输出 JSON："
                        '{"should_reply":true/false,"target_message_index":整数,"reason":"简短原因"}。'
                    ),
                },
                {"role": "user", "content": judge_text},
            ]
            raw = self._call_llm(
                "followup_judge",
                messages,
                response_format={"type": "json_object"},
                _wxautox_chat_name=chat_name,
                _wxautox_role_name=str(snapshot.get("role_name") or ""),
            )
            parsed = self._extract_first_json_object(raw)
            should_reply = bool(
                isinstance(parsed, dict)
                and self._normalize_judge_result(parsed)["should_reply"]
            )
            reason = (
                str(parsed.get("reason") or "")
                if isinstance(parsed, dict)
                else "invalid_json"
            )
        except Exception as exc:
            reason = f"judge_error: {exc}"
            logger.warning("🔗 Follow-up Judge failed for %s: %s", chat_name, exc)

        append_dashboard_event(
            "judge_decision",
            {
                "should_reply": should_reply,
                "reason": reason,
                "atmosphere": "续聊判定",
                "role_name": str(snapshot.get("role_name") or ""),
                "judge_name": "followup_judge",
                "chat_name": chat_name,
                "mode": "followup",
            },
        )

        approved_event = None
        try:
            with self._followup_lock:
                session = self._followup_sessions.get(chat_name)
                if (
                    session
                    and session.get("anchor_id") == snapshot.get("anchor_id")
                ):
                    session["judge_inflight"] = False
                    ingress = self._get_chat_ingress_state(chat_name)
                    is_current = (
                        int(ingress.get("sequence") or 0)
                        == int(snapshot.get("snapshot_sequence") or 0)
                        and float(snapshot["pending"][-1].get("event_timestamp") or 0)
                        <= float(session.get("expires_at") or 0)
                        and not self._ingress_mentions_bot(chat_name, ingress)
                    )
                    if should_reply and is_current and snapshot.get("pending"):
                        target = snapshot["pending"][-1]
                        event_data = dict(target.get("event_data") or {})
                        event_data.update(
                            {
                                "_followup_approved": True,
                                "_followup_anchor_id": snapshot["anchor_id"],
                                "_followup_snapshot_seq": snapshot["snapshot_sequence"],
                                "_followup_auto_turns": snapshot["automatic_turns"],
                            }
                        )
                        approved_event = Event(
                            type=EventType.CHATBOT_FOLLOWUP_APPROVED,
                            source="builtin_chatbot_followup",
                            data=event_data,
                            context={"wx": target.get("wx")},
                            timestamp=float(target.get("event_timestamp") or time.time()),
                        )
                    if session.get("pending_messages") and session.get("rerun_due"):
                        self._arm_followup_timer_locked(chat_name, session)
        finally:
            if approved_event is not None and self.event_bus is not None:
                self.event_bus.publish(approved_event)

    def _build_followup_judge_text(self, snapshot: Dict[str, Any]) -> str:
        lines = [
            f"机器人名称：{self.bot_name}",
            f"角色：{snapshot.get('role_name') or self.default_role}",
            "",
            "机器人回复前的少量上下文：",
        ]
        for message in snapshot.get("context_before") or []:
            lines.append(
                f"[{message.get('sender') or '未知'}] {message.get('content') or ''}"
            )
        lines.extend(
            [
                "",
                "机器人刚刚的回复：",
                f"[{self.bot_name}] {snapshot.get('reply_text') or ''}",
                "",
                "回复后收到的新消息：",
            ]
        )
        for index, message in enumerate(snapshot.get("pending") or [], start=1):
            lines.append(
                f"{index}. [{message.get('sender') or '未知'}] {message.get('content') or ''}"
            )
        lines.extend(
            [
                "",
                "仅当最后一条消息或最后一组紧密相关消息明显是在追问、回答、纠正或继续回应机器人时才回复。",
                "群友之间聊天、新话题、礼貌致谢、简单附和、表情、信息不明确，或已经有人充分回答时不回复。",
            ]
        )
        return self.context_manager.truncate_text_to_budget(
            "\n".join(lines),
            1800,
            notice="续聊判定上下文达到预算上限",
        )

    def _followup_approval_is_current(self, event: Event) -> bool:
        chat_name = str(event.data.get("chat_name") or "")
        anchor_id = str(event.data.get("_followup_anchor_id") or "")
        snapshot_sequence = int(event.data.get("_followup_snapshot_seq") or 0)
        if not chat_name or not anchor_id or snapshot_sequence <= 0:
            return False
        config = self._get_followup_config(chat_name)
        if not config["enabled"]:
            return False
        ingress = self._get_chat_ingress_state(chat_name)
        if int(ingress.get("sequence") or 0) != snapshot_sequence:
            return False
        if self._ingress_mentions_bot(chat_name, ingress):
            return False
        with self._followup_lock:
            session = self._followup_sessions.get(chat_name)
            return bool(
                session
                and session.get("anchor_id") == anchor_id
                and int(session.get("automatic_turns") or 0) < config["max_turns"]
            )

    def _discard_stale_followup_model_result(
        self,
        chat_name: str,
        *,
        invalidate_provider: bool = True,
    ) -> None:
        """Drop conversation state after a generated follow-up is not sent."""
        self._anchored_contexts.pop(chat_name, None)
        try:
            self._anchored_context_path(chat_name).unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("🔗 Failed to remove stale anchor for %s: %s", chat_name, exc)
        if invalidate_provider and self.codex_persistent_session_enabled:
            try:
                from app.services.codex_app_server import get_codex_app_server_manager

                get_codex_app_server_manager().invalidate_chat(chat_name)
            except Exception as exc:
                logger.warning(
                    "🔗 Failed to invalidate stale Codex thread for %s: %s",
                    chat_name,
                    exc,
                )

    def close(self) -> None:
        with self._followup_lock:
            self._followup_closed = True
            sessions = list(self._followup_sessions.values())
            self._followup_sessions.clear()
        for session in sessions:
            timer = session.get("timer")
            if timer:
                timer.cancel()
        self._followup_executor.shutdown(wait=False, cancel_futures=True)
        self.memory_service.close()

    def _is_processing(self, chat_name: str) -> bool:
        """检查是否正在处理该聊天的主动回复"""
        if chat_name not in self._processing_locks:
            return False

        # 检查锁是否超时
        lock_time = self._processing_locks[chat_name]
        if time.time() - lock_time > self._lock_timeout:
            # 锁超时，清理
            del self._processing_locks[chat_name]
            logger.warning(f"⏰ Processing lock for {chat_name} expired (timeout: {self._lock_timeout}s)")
            return False

        return True

    def _set_processing(self, chat_name: str):
        """设置处理锁"""
        self._processing_locks[chat_name] = time.time()
        logger.debug(f"🔒 Set processing lock for {chat_name}")

    def _clear_processing(self, chat_name: str):
        """清除处理锁"""
        if chat_name in self._processing_locks:
            del self._processing_locks[chat_name]
            logger.debug(f"🔓 Cleared processing lock for {chat_name}")

    def _judge_cooldown_key(self, chat_name: str, judge_name: str) -> str:
        return f"{chat_name}::{judge_name}"

    def _clear_judge_cooldowns(self, chat_name: str):
        removed = False
        for key in list(self._judge_cooldowns.keys()):
            if key == chat_name or key.startswith(f"{chat_name}::"):
                del self._judge_cooldowns[key]
                removed = True
        if removed:
            logger.debug(f"❄️ Judge cooldown cleared for {chat_name} (successful reply)")

    def _check_judge_cooldown(
        self,
        chat_name: str,
        judge_name: str,
        current_msg_count: int,
        timing: Dict[str, int],
        last_reply_time: Optional[datetime] = None,
    ) -> bool:
        """检查裁判冷却是否已过期

        Args:
            chat_name: 聊天名称
            judge_name: Judge 名称
            current_msg_count: 当前消息数（距离上一条机器人消息）
            timing: Judge 触发/冷却参数
            last_reply_time: 上一次机器人回复的时间

        Returns:
            True if cooldown expired (can consult judge)
            False if still in cooldown
        """
        cooldown_key = self._judge_cooldown_key(chat_name, judge_name)
        if cooldown_key not in self._judge_cooldowns:
            return True  # No cooldown, can consult

        cooldown = self._judge_cooldowns[cooldown_key]
        cooldown_time = cooldown['time']
        cooldown_msg_count = int(cooldown.get('msg_count') or 0)
        cooldown_total_count = int(cooldown.get('total_count') or 0)

        # 1. 检查中间是否有机器人插话
        # 如果 last_reply_time 晚于 cooldown_time，说明在冷却期间机器人（或Summary插件）已经说过话了
        # 这会导致 current_msg_count 被重置，之前的 cooldown_msg_count 变得无意义
        # 此时应视为冷却失效（或者任务已完成），可以重新开始
        if last_reply_time:
             # datetime.fromtimestamp(cooldown_time) might be needed if they are diff types,
             # but cooldown_time is float (time.time())
             cooldown_dt = datetime.fromtimestamp(cooldown_time)
             if last_reply_time > cooldown_dt:
                 del self._judge_cooldowns[cooldown_key]
                 logger.debug(f"❄️ Judge[{judge_name}] cooldown cleared for {chat_name} (interleaved bot reply detected)")
                 return True

        # Check time condition
        cooldown_minutes = int(timing.get("cooldown_minutes", 1) or 0)
        cooldown_msg_threshold = int(timing.get("cooldown_msg_threshold", 0) or 0)
        time_elapsed = time.time() - cooldown_time
        time_ok = time_elapsed >= (cooldown_minutes * 60)

        # Use the cumulative log counter for cooldown deltas. current_msg_count comes
        # from a bounded scan window; once the bot's last reply falls outside that
        # window it can stop increasing and permanently pin the cooldown.
        current_total_count = self.chat_log_manager.count_messages(chat_name)
        if cooldown_total_count > 0 and current_total_count >= cooldown_total_count:
            new_msg_count = current_total_count - cooldown_total_count
            msg_count_ok = new_msg_count >= cooldown_msg_threshold
        else:
            # Backward-compatible fallback for in-memory cooldowns created before
            # total_count existed, or if cumulative counting is unavailable.
            new_msg_count = current_msg_count - cooldown_msg_count
            msg_count_ok = current_msg_count >= (cooldown_msg_count + cooldown_msg_threshold)

        if time_ok and msg_count_ok:
            # Cooldown expired, remove it
            del self._judge_cooldowns[cooldown_key]
            logger.debug(f"❄️ Judge[{judge_name}] cooldown expired for {chat_name}")
            return True

        logger.debug(
            f"❄️ Judge[{judge_name}] in cooldown for {chat_name}: "
            f"time={int(time_elapsed/60)}/{cooldown_minutes}min, "
            f"msgs={new_msg_count}/{cooldown_msg_threshold}"
        )
        return False

    def _set_judge_cooldown(self, chat_name: str, judge_name: str, current_msg_count: int, timing: Dict[str, int]):
        """设置裁判冷却

        Args:
            chat_name: 聊天名称
            judge_name: Judge 名称
            current_msg_count: 当前消息数
            timing: Judge 触发/冷却参数
        """
        cooldown_key = self._judge_cooldown_key(chat_name, judge_name)
        self._judge_cooldowns[cooldown_key] = {
            'time': time.time(),
            'msg_count': current_msg_count,
            'total_count': self.chat_log_manager.count_messages(chat_name),
        }
        logger.info(
            f"❄️ Judge[{judge_name}] cooldown set for {chat_name} "
            f"(will retry after {timing.get('cooldown_msg_threshold', 0)} msgs "
            f"AND {timing.get('cooldown_minutes', 1)} min)"
        )


    def handle_quote_image_message(self, event: Event):
        """处理引用图片消息事件 (Unified Flow)"""
        try:
            logger.info(f"🤖 ChatBot plugin received quote image message event")
            start_time = time.time()
            logger.info(f"🔍 开始处理quote_image消息")

            content = event.data.get("message", "")
            chat_name = event.data.get("chat_name", "")
            sender = event.data.get("sender", "")
            chat_type = event.data.get("chat_type", "private")
            quote_content = event.data.get("quote_content", "")

            if self._is_sender_ignored(chat_name, sender):
                logger.info(f"🤖 Ignored blacklisted quote sender: chat={chat_name}, sender={sender}")
                return False

            # ---- 消息去重逻辑 (引用消息也需要) ----
            import hashlib
            # 引用消息的去重 key 增加 quote_content
            content_hash = hashlib.md5(f"{content}|{quote_content}".encode('utf-8')).hexdigest()
            dedup_key = (chat_name, sender, content_hash)
            now = time.time()
            if dedup_key in self._message_dedup_cache:
                if now - self._message_dedup_cache[dedup_key] < self._dedup_window:
                    logger.warning(f"⚠️ 检测到重复引用消息事件，跳过处理: chat={chat_name}, sender={sender}")
                    return False
            self._message_dedup_cache[dedup_key] = now
            # --------------------
            logger.info(f"🔍 Quote message details: sender={sender}, chat={chat_name}, content='{content[:50]}', quote_content='{quote_content}'")


            # 初始化变量，防止在finally块中访问未定义变量
            is_mention = False
            proactive_processing_acquired = False
            logger.info(f"✅ is_mention initialized to False")

            # 1. 验证是否为图片引用
            # 如果是通过兜底检测转发来的，跳过验证（我们已经通过正则确认了）
            logger.info(f"🔍 Quote validation: quote_content='{quote_content}', has_quote_image={event.data.get('has_quote_image', False)}")
            if "[图片]" not in quote_content:
                # 检查是否为误识别消息转发（has_quote_image会被我们设置为True）
                if not event.data.get("has_quote_image", False):
                    logger.warning(f"🤖 Quote content verification failed. Content: {quote_content[:100]}...")
                    return False
                else:
                    logger.info(f"🔧 跳过quote验证：这是误识别的引用图片消息（已通过正则确认)")
            else:
                logger.info(f"✅ Quote validation passed")

            # 2. 基础响应检查
            logger.info(f"🔍 Content check: should_respond={self._should_respond(content, chat_type)}")
            if not self._should_respond(content, chat_type):
                logger.info(f"🤖 Should not respond to quote message from {sender} (Empty or filtered)")
                return False
            logger.info(f"✅ Content check passed")

            # 3. 触发判断 (Active vs Passive)
            is_mention = self._event_mentions_bot(event)
            should_reply = False
            logger.info(f"🔍 Trigger check: is_mention={is_mention}, chat_type={chat_type}")

            wx_manager = event.context.get("wx")

            # A. 被动触发 (@ 或 私聊)
            # 注意：wxautox4中私聊的chat_type可能是 "private", "friend", 或 "user"
            if chat_type in ["private", "friend", "user"] or (chat_type == "group" and is_mention and self.allow_mention_trigger):
                should_reply = True
                logger.info(f"🤖 Quote Trigger: Passive (Mention={is_mention}, Type={chat_type})")

            # B. 主动触发（群聊其余场景，包含 @关闭 时的@消息）
            elif chat_type == "group":
                if not self._check_proactive_permission(chat_name):
                    logger.debug(f"🤖 Quote Trigger: Proactive permission denied for {chat_name}")
                    return False

                if self._is_processing(chat_name):
                    logger.debug(f"🤖 Quote Trigger: Already processing")
                    return False

                role_name = self._get_user_role(chat_name)
                judge_name = self._get_user_judge(chat_name)
                if not judge_name:
                    logger.debug(f"⚖️ Quote Trigger: no judge binding for {chat_name}, proactive disabled")
                    return False
                judge_timing = self.judge_manager.get_judge_timing(judge_name)
                state = self._analyze_chat_state(
                    chat_name,
                    scan_threshold=max(judge_timing["trigger_msg_threshold"], judge_timing["cooldown_msg_threshold"]),
                )
                if not self._check_judge_cooldown(chat_name, judge_name, state['msg_count'], judge_timing, state['last_reply_time']):
                    logger.debug(f"🤖 Quote Trigger: Judge cooldown active")
                    return False

                judge_context = self._get_judge_context_messages(chat_name, 20)
                temp_last_msg = {"sender": sender, "content": f"{content} [引用了一张图片]"}

                if not self._consult_judge(judge_context + [temp_last_msg], role_name, judge_name):
                    self._set_judge_cooldown(chat_name, judge_name, state['msg_count'], judge_timing)
                    logger.info(f"🤖 Quote Trigger: Judge rejected")
                    return False

                should_reply = True
                self._set_processing(chat_name)
                proactive_processing_acquired = True
                logger.info(f"📢 Proactive reply triggered for Quote Image in {chat_name}")
            else:
                logger.debug(f"🤖 Quote Trigger: conditions not met (Type={chat_type})")

            if not should_reply:
                return False

            # 4. 执行处理
            try:
                # 4.1 下载/处理图片
                image_base64 = self._process_quoted_image(event.data, wx_manager)
                if not image_base64:
                    if is_mention and wx_manager:
                        wx_manager.send_message(chat_name, "⚠️ 无法获取引用图片，请稍后再试")
                    return False

                # 4.2 获取上下文，实际入模内容由 token 预算动态裁剪
                memory_config = self._get_chat_memory_config(chat_name)
                context_msgs = self.chat_log_manager.get_context_messages(
                    chat_name,
                    self._memory_source_fetch_limit(memory_config),
                )
                self.memory_service.schedule(chat_name, memory_config)

                # 4.3 调用 Web Search (多模态)
                # 引用图片问答必须把“当前问题指向随本条消息附带的图片”写进文本，
                # 否则像“真的吗 / 我问你这个”这类短问句很容易被长历史上下文带偏。
                quote_image_content = self._build_quote_image_augmented_content(content)
                memory_context, memory_stats = self.memory_service.build_retrieval_context(
                    chat_name,
                    sender=sender,
                    content=quote_image_content,
                    recent_messages=context_msgs,
                    config=memory_config,
                )
                search_context = context_msgs[-self.context_limit:] + [{"sender": sender, "content": quote_image_content}]
                search_results = ""
                if self._should_run_framework_search(chat_name):
                    search_results = self.web_search_service.search(
                        search_context,
                        image_base64=image_base64,
                        bot_names=self._bot_names_for_chat(chat_name),
                    )

                # 4.4 构建消息（包含 system prompt 和变量替换）
                role_name = self._get_user_role(chat_name)
                messages = self._build_messages_array(
                    chat_name,
                    context_msgs,
                    search_results,
                    sender,
                    quote_image_content,
                    role_name,
                    memory_config,
                    input_image_count=1,
                    memory_context=memory_context,
                )
                logger.info(
                    "🧠 Image retrieval context for %s: events=%s people=%s tokens≈%s",
                    chat_name,
                    memory_stats.get("event_count"),
                    memory_stats.get("people_count"),
                    memory_stats.get("tokens", 0),
                )
                self._attach_image_to_latest_user_message(messages, image_base64)

                # 4.6 将最终 Prompt 核验后的记忆审计随调用写入 LLM Records
                verified_memory_trace = self._reconcile_memory_trace(
                    memory_stats.get("trace"),
                    messages,
                )
                response_attachments: List[Dict[str, Any]] = []
                response = self._call_chat_llm(
                    chat_name,
                    role_name,
                    messages,
                    _wxautox_attachment_capture=response_attachments,
                    _wxautox_allow_image_input=True,
                    _wxautox_memory_trace=verified_memory_trace,
                )
                if response_attachments:
                    response = self._strip_internal_action_markers(response)

                # 4.7 发送回复
                if wx_manager:
                    has_response_text = bool(self._format_response_parts(response, self.role_manager.get_output_settings(role_name)))
                    sent_response = (
                        self._send_response_parts(wx_manager, chat_name, response, role_name)
                        if has_response_text
                        else bool(response_attachments)
                    )
                    if sent_response:
                        if response_attachments:
                            sent_response = self._send_response_attachments(wx_manager, chat_name, response_attachments)
                        if not sent_response:
                            logger.error(f"🤖 Failed to send quote image attachment(s) to {chat_name}")
                            return False
                        self._finalize_anchored_context(chat_name, response)
                        logger.info(f"🤖 Sent quote image response to {chat_name}")

                        # 记录 E2E 响应时间
                        duration = time.time() - start_time
                        try:
                            get_llm_manager()._record_stats("builtin_chatbot", "reply_latency", "system", None, duration)
                            logger.info(f"⏱️ E2E Reply Latency (Quote): {duration:.2f}s")
                        except Exception as e:
                            logger.warning(f"Failed to record latency: {e}")

                        # 清除裁判冷却
                        self._clear_judge_cooldowns(chat_name)
                        return True
            finally:
                if proactive_processing_acquired:
                    self._clear_processing(chat_name)

            return False

        except Exception as e:
            logger.error(f"🤖 ChatBot处理图片消息失败: {e}")
            return False

    @staticmethod
    def _attach_image_to_latest_user_message(messages: List[Dict], image_base64: str) -> None:
        """把引用图片附加到最后一条真实用户消息，交由 LLMManager 按模型能力保留或剥离。"""
        if not messages or not image_base64:
            return

        image_url = image_base64.strip()
        if not image_url.startswith("data:image/"):
            image_url = f"data:image/jpeg;base64,{image_url}"

        for msg in reversed(messages):
            if msg.get("role") != "user" or msg.get("name") == "search_context":
                continue

            content = msg.get("content")
            image_part = {"type": "image_url", "image_url": {"url": image_url}}
            if isinstance(content, list):
                content.append(image_part)
            elif content:
                msg["content"] = [
                    {"type": "text", "text": str(content)},
                    image_part,
                ]
            else:
                msg["content"] = [image_part]
            return

    @staticmethod
    def _build_quote_image_augmented_content(content: str) -> str:
        """给引用图片问答增加强绑定说明，但不替代真实图片输入。

        目标：让最终模型明确知道“这个/真的吗/我问你这个”指的是当前消息附带的引用图片，
        历史聊天只能作为语气背景，不能拿旧图或旧话题来补主语。
        """
        content = str(content or "").strip()
        binding = (
            "【重要】当前用户正在询问本条消息引用的图片。"
            "请直接查看随本条消息附带的图片来回答；"
            "历史聊天只作为语气和背景参考，不要根据历史中的其他图片、其他‘这个/真的吗’或其他话题猜测。"
        )
        if not content:
            return binding
        if "当前用户正在询问本条消息引用的图片" in content:
            return content
        return f"{content}\n\n{binding}"

    def _should_respond(self, content: str, chat_type: str) -> bool:
        """判断是否应该响应消息
        注意：@触发逻辑已统一移动到EventBus权限检查中处理
        此方法仅做基础内容检查
        """
        # 基础内容检查
        return bool(content.strip())

    def _get_user_role(self, chat_name: str) -> str:
        """获取用户的角色配置"""
        try:
            from app.models.base import SessionLocal
            from app.models.user_permission import WeChatUser
            from app.models.chatbot_role import UserChatBotRole, ChatBotRole

            with SessionLocal() as db:
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                if user:
                    # 查询用户角色关联
                    user_role = db.query(UserChatBotRole).filter(UserChatBotRole.user_id == user.id).first()
                    if user_role:
                        role = db.query(ChatBotRole).filter(ChatBotRole.id == user_role.role_id).first()
                        if role:
                            role_name = role.name
                            logger.debug(f"🎭 User '{chat_name}' using role: {role_name}")
                            return role_name

                logger.debug(f"🎭 User '{chat_name}' using default role")
                return self.default_role
        except Exception as e:
            logger.error(f"🎭 Error getting user role: {e}")
            return self.default_role

    def _get_user_judge(self, chat_name: str) -> Optional[str]:
        """获取用户的 Judge 绑定。未绑定时返回 None（主动 Judge 禁用）。"""
        try:
            from app.models.base import SessionLocal
            from app.models.user_permission import WeChatUser
            from app.models.chatbot_judge import UserChatBotJudge, ChatBotJudge

            with SessionLocal() as db:
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                if not user:
                    return None

                user_judge = db.query(UserChatBotJudge).filter(UserChatBotJudge.user_id == user.id).first()
                if not user_judge:
                    return None

                judge = db.query(ChatBotJudge).filter(ChatBotJudge.id == user_judge.judge_id).first()
                if not judge:
                    return None

                logger.debug(f"⚖️ User '{chat_name}' using judge: {judge.name}")
                return judge.name
        except Exception as e:
            logger.error(f"⚖️ Error getting user judge: {e}")
            return None

    def _clean_query(self, content: str, chat_name: str = "") -> str:
        """清理查询内容，移除@机器人名称"""
        return strip_bot_mentions(content, self._bot_names_for_chat(chat_name))

    def _build_quote_augmented_content(self, content: str, quote_content: str) -> str:
        """把文字引用内容显式放进本轮用户消息，避免 LLM 只看到“核实一下”。

        wxauto 能拿到的 quote_content 有时是微信 UI 的预览片段（可能以 ... 结尾），
        但即使只是片段，也比完全丢失引用上下文更能让模型判断用户在问哪条消息。
        图片/视频等引用由专门流程处理，这里只增强文字引用。
        """
        content = str(content or "")
        quote = str(quote_content or "").strip()
        if not quote:
            return content

        non_text_markers = {"[图片]", "图片", "视频", "[视频]", "动画表情", "[动画表情]"}
        if quote in non_text_markers:
            return content

        # 避免上游已经把引用拼进 content 时重复塞一遍。
        if quote and quote in content:
            return content

        return (
            "【当前消息引用的原文片段】\n"
            f"{quote}\n\n"
            "【当前消息】\n"
            f"{content}"
        )



    def _format_chat_text(self, context_messages: List[Dict]) -> str:
        """统一的聊天记录格式化方法

        用于judge和chat回复,确保两者看到相同格式的上下文
        """
        if not context_messages:
            return "（暂无历史消息）"

        chat_lines = []
        for msg in context_messages:
            sender = msg.get('sender', 'User')
            content = msg.get('content', '')
            time_str = msg.get('time', '')
            if time_str:
                chat_lines.append(f"[{time_str}] [{sender}]: {content}")
            else:
                chat_lines.append(f"[{sender}]: {content}")

        return "\n".join(chat_lines)

    def _get_active_model_context_window(self, chat_name: str) -> int:
        """Prefer authoritative App Server telemetry, then model metadata."""
        if self.context_window_auto_detect:
            try:
                from app.services.codex_app_server import get_codex_app_server_manager

                state = get_codex_app_server_manager().state_store.get(chat_name) or {}
                context_window = int(state.get("model_context_window") or 0)
                if context_window > 0:
                    return context_window
            except Exception as e:
                logger.debug("Codex context telemetry unavailable for %s: %s", chat_name, e)

            try:
                manager = get_llm_manager()
                mapping = manager._get_mapping("builtin_chatbot", "chat") or {}
                model = (manager.config.get("models") or {}).get(mapping.get("primary")) or {}
                context_window = int(
                    model.get("context_window_tokens")
                    or model.get("max_input_tokens")
                    or 0
                )
                if context_window > 0:
                    return context_window
            except Exception as e:
                logger.debug("Configured model context metadata unavailable: %s", e)
        return 0

    def _effective_context_limits(
        self,
        chat_name: str,
        memory_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, int]:
        memory_config = memory_config or self._get_default_memory_config()
        configured_cap = max(4096, int(self.max_context_tokens or 220000))
        reserved = max(1024, int(self.reserved_output_tokens or 8192))
        safety = max(0, int(self.context_safety_margin_tokens or 0))
        model_window = self._get_active_model_context_window(chat_name)
        model_input_cap = (
            max(4096, model_window - reserved - safety)
            if model_window > 0
            else configured_cap
        )
        input_cap = min(configured_cap, model_input_cap)
        configured_rollover = max(
            4096,
            int(memory_config.get("anchor_rollover_prompt_tokens") or 205000),
        )
        rollover = min(configured_rollover, max(4096, input_cap - 4096))
        return {
            "model_context_window": model_window,
            "configured_cap": configured_cap,
            "input_cap": input_cap,
            "rollover": rollover,
            "reserved_output": reserved,
            "safety_margin": safety,
        }

    def _calculate_context_budgets(
        self,
        chat_name: str,
        search_results: str,
        content: str,
        memory_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, int]:
        """Calculate adaptive token budgets for long-context prompting."""
        memory_config = memory_config or self._get_default_memory_config()
        limits = self._effective_context_limits(chat_name, memory_config)
        # Keep room for the static role prompt, output contract and chat
        # serialization so the finished sliding prompt respects input_cap.
        available = max(1024, limits["input_cap"] - 4096)

        current_tokens = self.context_manager.estimate_tokens(content) + 128
        search_tokens = self.context_manager.estimate_tokens(search_results)
        ephemeral_cap = min(
            max(512, int(self.ephemeral_context_max_tokens or 16000)),
            max(512, int(available * max(0.01, self.ephemeral_context_ratio))),
        )
        ephemeral_used = min(ephemeral_cap, search_tokens + current_tokens)

        durable_budget = max(1024, available - ephemeral_used)
        configured_memory_cap = max(
            0,
            int(memory_config.get("memory_context_max_tokens") or 0),
        )
        memory_budget = 0
        if memory_config.get("memory_enabled", True):
            memory_ratio = max(0.0, self.memory_context_ratio)
            recent_ratio = max(0.0, self.recent_context_ratio)
            ratio_total = memory_ratio + recent_ratio or 1.0
            memory_budget = min(
                configured_memory_cap,
                max(512, int(durable_budget * (memory_ratio / ratio_total))),
            )
        recent_budget = max(1024, durable_budget - memory_budget)

        return {
            "available": available,
            "memory": max(0, memory_budget),
            "recent": max(1024, recent_budget),
            "ephemeral_cap": ephemeral_cap,
            **limits,
        }

    def _build_messages_array(
        self,
        chat_name: str,
        context_messages: List[Dict],
        search_results: str,
        sender: str,
        content: str,
        role_name: str,
        memory_config: Optional[Dict[str, Any]] = None,
        input_image_count: int = 0,
        memory_context: str = "",
    ) -> List[Dict]:
        """构建发送给 LLM 的消息数组（预算驱动的分层上下文）

        Args:
            chat_name: 聊天名称
            context_messages: 上下文消息列表
            search_results: 网络搜索结果（纯文本）
            sender: 发送者
            content: 消息内容
            role_name: 角色名称

        Returns:
            OpenAI 格式的消息数组
        """
        memory_config = memory_config or self._get_default_memory_config()

        if self._use_anchored_append_context(memory_config, role_name):
            return self._build_anchored_append_messages(
                chat_name=chat_name,
                context_messages=context_messages,
                search_results=search_results,
                sender=sender,
                content=content,
                role_name=role_name,
                memory_config=memory_config,
                input_image_count=input_image_count,
                memory_context=memory_context,
            )

        # 去重逻辑：如果最后一条历史消息与当前消息一致，则移除
        if context_messages and len(context_messages) > 0:
            last_msg = context_messages[-1]
            if last_msg.get('sender') == sender and last_msg.get('content', '').strip() == content.strip():
                logger.debug(f"🤖 Removed duplicated last message from context: {content[:20]}...")
                context_messages = context_messages[:-1]

        budgets = self._calculate_context_budgets(
            chat_name,
            search_results,
            content,
            memory_config,
        )
        bounded_memory = self.context_manager.truncate_text_to_budget(
            memory_context,
            budgets["memory"],
            notice="相关记忆达到滑动窗口预算上限",
        )
        recent_messages, recent_tokens = self.context_manager.select_recent_messages(
            context_messages,
            budgets["recent"],
        )
        recent_text = self.context_manager.format_messages(recent_messages)
        sections = []
        if bounded_memory:
            sections.append(bounded_memory)
        sections.append("## 最近原始聊天记录\n" + recent_text)
        context_text = "\n\n".join(sections)
        context_stats = {
            "memory_tokens": self.context_manager.estimate_tokens(bounded_memory),
            "recent_tokens": recent_tokens,
            "recent_messages": len(recent_messages),
        }

        search_text = (search_results or "").strip()
        if search_text and search_text != "无结果":
            search_budget = max(
                256,
                budgets["ephemeral_cap"] - self.context_manager.estimate_tokens(content) - 128,
            )
            search_text = self.context_manager.truncate_text_to_budget(
                search_text,
                search_budget,
                notice="搜索结果因上下文预算限制已截断",
            )
        else:
            search_text = ""

        context_text = (
            "【上下文使用规则】\n"
            "1. 当前用户消息和最近原始聊天优先级最高。\n"
            "2. 检索记忆只在与当前话题、称呼、关系或固定梗直接相关时使用。\n"
            "3. 不要为了显得记得很多而主动扯旧事、成员画像、历史事件或群内黑话。\n"
            "4. 如果当前问题是普通事实问答、搜索问答或新话题，检索记忆通常不需要出现在回复里。\n\n"
            f"{context_text}"
        )

        # 1. 准备变量字典。保持角色 Prompt 自己定义的 {chat_text}/{search_results} 结构。
        variables = {
            'chat_text': context_text,
            'search_results': search_text,
            'sender': sender,
            'content': content
        }

        # 2. 获取角色 prompt（已完成变量替换）。
        # 如果角色 Prompt 本身不使用动态占位符，则把动态资料追加在所有静态规则之后。
        # 这样静态人设、长期规则、输出规范可以尽量保持 100% 前缀一致，利于 LLM 前缀缓存。
        role_template = self.role_manager.roles.get(role_name, "")
        role_uses_dynamic_slots = self._role_prompt_uses_dynamic_slots(role_template)
        role_prompt = self.role_manager.get_role_prompt(role_name, variables=variables)
        if not role_uses_dynamic_slots:
            role_prompt = role_prompt + self._build_dynamic_input_block(context_text, search_text)

        # 3. 构建消息数组
        # System message: 角色 prompt（已包含所有替换后的内容）
        # User message: 当前用户消息（带时间戳）
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        messages = [
            {"role": "system", "content": role_prompt},
        ]

        output_contract = self._build_human_like_output_contract(role_name)
        if output_contract:
            messages.append({"role": "system", "content": output_contract})

        messages.append(
            {"role": "user", "content": f"[{now_str}] [{sender}]: {content}"}
        )

        logger.info(
            "🤖 构建消息数组完成: msgs=%s, memory_tokens≈%s, "
            "recent_tokens≈%s, recent_msgs=%s, max_context=%s",
            len(messages),
            context_stats.get("memory_tokens"),
            context_stats.get("recent_tokens"),
            context_stats.get("recent_messages"),
            self.max_context_tokens,
        )
        return messages

    def _use_anchored_append_context(self, memory_config: Dict[str, Any], role_name: str) -> bool:
        if str(memory_config.get("context_window_strategy") or "").strip().lower() != "anchored_append":
            return False

        role_template = self.role_manager.roles.get(role_name, "")
        if self._role_prompt_uses_dynamic_slots(role_template):
            logger.warning(
                "🤖 Anchored append context disabled for role '%s': role prompt uses dynamic slots",
                role_name,
            )
            return False

        return True

    def _build_anchored_append_messages(
        self,
        chat_name: str,
        context_messages: List[Dict],
        search_results: str,
        sender: str,
        content: str,
        role_name: str,
        memory_config: Dict[str, Any],
        input_image_count: int = 0,
        memory_context: str = "",
    ) -> List[Dict]:
        role_prompt = self.role_manager.get_role_prompt(role_name)
        messages = [{"role": "system", "content": role_prompt}]

        output_contract = self._build_human_like_output_contract(role_name)
        if output_contract:
            messages.append({"role": "system", "content": output_contract})

        dynamic_messages = self._get_anchored_dynamic_messages(
            chat_name=chat_name,
            context_messages=context_messages,
            sender=sender,
            content=content,
            memory_config=memory_config,
        )
        state = self._anchored_contexts.get(chat_name) or {}
        checkpoint_text = str(state.get("memory_checkpoint") or "").strip()
        if checkpoint_text:
            messages.append(
                {
                    "role": "system",
                    "name": "memory_checkpoint",
                    "content": (
                        "【冻结群聊记忆检查点】\n"
                        "此检查点在当前线程内保持不变。最近原始消息与当前用户消息优先；"
                        "仅在相关时使用记忆，不要提及检查点或系统结构。\n\n"
                        f"{checkpoint_text}"
                    ),
                }
            )

        search_text = self._prepare_search_tail(
            chat_name,
            search_results,
            content,
            memory_config,
        )
        search_message = None
        if search_text:
            search_message = {
                "role": "user",
                "name": "search_context",
                "content": (
                    "【本轮网络搜索资料】\n"
                    "以下资料只服务于后面紧接着的当前聊天消息。不要提搜索、资料来源或系统结构，"
                    "消化后直接按角色口吻接话。\n\n"
                    f"{search_text}"
                ),
            }

        memory_message = None
        bounded_memory = self.context_manager.truncate_text_to_budget(
            memory_context,
            max(0, int(memory_config.get("memory_context_max_tokens") or 0)),
            notice="相关记忆达到本轮预算上限",
        )
        if bounded_memory:
            memory_message = {
                "role": "user",
                "name": "memory_context",
                "content": (
                    "【本轮相关群聊记忆】\n"
                    "以下内容只服务于后面紧接着的当前聊天消息。最近原始聊天优先；"
                    "仅在直接相关时自然使用，不要提及检索、事件卡或记忆系统。\n\n"
                    f"{bounded_memory}"
                ),
            }

        def _build_full_prompt(dynamic_base: List[Dict]) -> tuple[List[Dict], List[Dict]]:
            dynamic_prompt = list(dynamic_base)
            if memory_message:
                dynamic_prompt = self._insert_context_before_current_user_message(
                    dynamic_prompt,
                    memory_message,
                )
            if search_message:
                dynamic_prompt = self._insert_context_before_current_user_message(
                    dynamic_prompt,
                    search_message,
                )
            return messages + dynamic_prompt, dynamic_prompt

        full_messages, dynamic_prompt_messages = _build_full_prompt(dynamic_messages)
        prompt_tokens, token_source = self._count_chat_prompt_tokens(
            full_messages,
            input_image_count=input_image_count,
        )

        limits = self._effective_context_limits(chat_name, memory_config)
        rollover_tokens = limits["rollover"]
        input_cap = limits["input_cap"]
        anchor_count = int(memory_config.get("anchor_message_count") or 300)
        if rollover_tokens > 0 and prompt_tokens >= rollover_tokens:
            logger.info(
                "🤖 Anchored context rollover for %s: prompt_tokens=%s source=%s >= %s; "
                "resetting to last %s messages",
                chat_name,
                prompt_tokens,
                token_source,
                rollover_tokens,
                anchor_count,
            )
            state = self._reset_anchored_context(
                chat_name=chat_name,
                context_messages=context_messages,
                sender=sender,
                content=content,
                anchor_count=anchor_count,
                total_count=self.chat_log_manager.count_messages(chat_name),
                memory_config=memory_config,
            )
            dynamic_messages = list(state.get("messages") or [])
            messages = messages[: 2 if output_contract else 1]
            checkpoint_text = str(state.get("memory_checkpoint") or "").strip()
            if checkpoint_text:
                messages.append(
                    {
                        "role": "system",
                        "name": "memory_checkpoint",
                        "content": (
                            "【冻结群聊记忆检查点】\n"
                            "此检查点在当前线程内保持不变。最近原始消息与当前用户消息优先；"
                            "仅在相关时使用记忆，不要提及检查点或系统结构。\n\n"
                            f"{checkpoint_text}"
                        ),
                    }
                )
            full_messages, dynamic_prompt_messages = _build_full_prompt(dynamic_messages)
            prompt_tokens, token_source = self._count_chat_prompt_tokens(
                full_messages,
                input_image_count=input_image_count,
            )

        if prompt_tokens > input_cap:
            dynamic_messages, prompt_tokens = self._trim_anchored_messages_to_cap(
                base_messages=messages,
                dynamic_messages=dynamic_messages,
                search_message=search_message,
                memory_message=memory_message,
                input_cap=input_cap,
                input_image_count=input_image_count,
            )
            state = self._anchored_contexts.get(chat_name)
            if state is not None:
                state["messages"] = list(dynamic_messages)
            full_messages, dynamic_prompt_messages = _build_full_prompt(dynamic_messages)
            prompt_tokens, token_source = self._count_chat_prompt_tokens(
                full_messages,
                input_image_count=input_image_count,
            )

        self._mark_anchored_pending(chat_name, dynamic_prompt_messages)

        logger.info(
            "🤖 构建锚定追加消息完成: msgs=%s, dynamic_msgs=%s, prompt_tokens=%s, "
            "token_source=%s, anchor_messages=%s, rollover=%s, input_cap=%s, model_window=%s",
            len(full_messages),
            len(dynamic_prompt_messages),
            prompt_tokens,
            token_source,
            memory_config.get("anchor_message_count"),
            rollover_tokens,
            input_cap,
            limits["model_context_window"],
        )
        return full_messages

    def _trim_anchored_messages_to_cap(
        self,
        *,
        base_messages: List[Dict],
        dynamic_messages: List[Dict],
        search_message: Optional[Dict],
        memory_message: Optional[Dict],
        input_cap: int,
        input_image_count: int,
    ) -> tuple[List[Dict], int]:
        """Drop oldest raw messages while preserving the current user turn."""
        trimmed = list(dynamic_messages)

        def build() -> List[Dict]:
            candidate = list(trimmed)
            if memory_message:
                candidate = self._insert_context_before_current_user_message(
                    candidate,
                    memory_message,
                )
            if search_message:
                candidate = self._insert_context_before_current_user_message(
                    candidate,
                    search_message,
                )
            return list(base_messages) + candidate

        tokens, _ = self._count_chat_prompt_tokens(
            build(),
            input_image_count=input_image_count,
        )
        while tokens > input_cap and len(trimmed) > 1:
            drop_count = max(1, min(len(trimmed) - 1, len(trimmed) // 8))
            trimmed = trimmed[drop_count:]
            tokens, _ = self._count_chat_prompt_tokens(
                build(),
                input_image_count=input_image_count,
            )

        if tokens > input_cap and search_message:
            # Search is optional; the current user message and role contract are not.
            search_message.clear()
            tokens, _ = self._count_chat_prompt_tokens(
                build(),
                input_image_count=input_image_count,
            )

        if tokens > input_cap and memory_message:
            # Retrieved memory is optional; never drop the triggering message.
            memory_message.clear()
            tokens, _ = self._count_chat_prompt_tokens(
                build(),
                input_image_count=input_image_count,
            )

        if tokens > input_cap:
            logger.error(
                "Anchored prompt remains above hard cap after trimming: tokens=%s cap=%s",
                tokens,
                input_cap,
            )
        else:
            logger.warning(
                "Anchored prompt trimmed to hard cap: remaining_messages=%s tokens=%s cap=%s",
                len(trimmed),
                tokens,
                input_cap,
            )
        return trimmed, tokens

    def _insert_context_before_current_user_message(
        self,
        dynamic_messages: List[Dict],
        context_message: Dict,
    ) -> List[Dict]:
        """Keep the real triggering chat message as the final user message."""
        if not dynamic_messages:
            return [context_message]

        last_msg = dynamic_messages[-1]
        if (
            last_msg.get("role") == "user"
            and last_msg.get("name") not in {"search_context", "memory_context"}
        ):
            return dynamic_messages[:-1] + [context_message, last_msg]

        return dynamic_messages + [context_message]

    def _prepare_search_tail(
        self,
        chat_name: str,
        search_results: str,
        content: str,
        memory_config: Dict[str, Any],
    ) -> str:
        search_text = (search_results or "").strip()
        if not search_text or search_text == "无结果":
            return ""

        available = self._effective_context_limits(chat_name, memory_config)["input_cap"]
        ephemeral_cap = min(
            max(512, int(self.ephemeral_context_max_tokens or 16000)),
            max(512, int(available * max(0.01, self.ephemeral_context_ratio))),
        )
        search_budget = max(
            256,
            ephemeral_cap - self.context_manager.estimate_tokens(content) - 128,
        )
        return self.context_manager.truncate_text_to_budget(
            search_text,
            search_budget,
            notice="搜索结果因上下文预算限制已截断",
        )

    def _get_anchored_dynamic_messages(
        self,
        chat_name: str,
        context_messages: List[Dict],
        sender: str,
        content: str,
        memory_config: Dict[str, Any],
    ) -> List[Dict]:
        total_count = self.chat_log_manager.count_messages(chat_name)
        state = self._anchored_contexts.get(chat_name) or self._load_anchored_context(chat_name)
        anchor_count = int(memory_config.get("anchor_message_count") or 300)

        if not state:
            state = self._reset_anchored_context(
                chat_name=chat_name,
                context_messages=context_messages,
                sender=sender,
                content=content,
                anchor_count=anchor_count,
                total_count=total_count,
                memory_config=memory_config,
            )
        else:
            if "memory_checkpoint" not in state:
                checkpoint_text, checkpoint_tokens = (
                    self.memory_service.get_checkpoint_text(
                        chat_name,
                        token_budget=int(memory_config["memory_checkpoint_max_tokens"]),
                    )
                )
                state["memory_checkpoint"] = checkpoint_text
                state["memory_checkpoint_tokens"] = checkpoint_tokens
                state["memory_checkpoint_created_at"] = datetime.now().isoformat(
                    timespec="seconds"
                )
            state = self._append_new_log_messages_to_anchor(
                chat_name=chat_name,
                state=state,
                sender=sender,
                content=content,
                total_count=total_count,
            )

        return list(state.get("messages") or [])

    def _reset_anchored_context(
        self,
        chat_name: str,
        context_messages: List[Dict],
        sender: str,
        content: str,
        anchor_count: int,
        total_count: int,
        memory_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        memory_config = memory_config or self._get_default_memory_config()
        source_messages = list(context_messages or [])[-anchor_count:]
        if not source_messages:
            source_messages = self.chat_log_manager.get_context_messages(chat_name, anchor_count)

        source_messages = self._ensure_current_message_present(source_messages, sender, content)
        formatted = self.chat_log_manager.format_messages_array(source_messages, bot_name=self.bot_name)
        checkpoint_text, checkpoint_tokens = self.memory_service.get_checkpoint_text(
            chat_name,
            token_budget=int(memory_config["memory_checkpoint_max_tokens"]),
        )
        state = {
            "messages": formatted,
            "log_count": total_count,
            "anchor_message_count": anchor_count,
            "memory_checkpoint": checkpoint_text,
            "memory_checkpoint_tokens": checkpoint_tokens,
            "memory_checkpoint_created_at": datetime.now().isoformat(timespec="seconds"),
            "pending_messages": None,
        }
        self._anchored_contexts[chat_name] = state
        return state

    def _append_new_log_messages_to_anchor(
        self,
        chat_name: str,
        state: Dict[str, Any],
        sender: str,
        content: str,
        total_count: int,
    ) -> Dict[str, Any]:
        last_count = int(state.get("log_count") or 0)
        if total_count < last_count:
            logger.warning(
                "🤖 Ignoring regressed chat count for %s: observed=%s < anchored=%s; "
                "repairing the cumulative floor and preserving the Codex prefix",
                chat_name,
                total_count,
                last_count,
            )
            total_count = self.chat_log_manager.ensure_minimum_count(
                chat_name,
                last_count,
            )

        delta = total_count - last_count
        if delta > 0:
            new_messages = self.chat_log_manager.get_context_messages(chat_name, delta)
            formatted = self.chat_log_manager.format_messages_array(new_messages, bot_name=self.bot_name)
            state["messages"] = list(state.get("messages") or []) + formatted
            state["log_count"] = total_count

        state["messages"] = self._ensure_current_formatted_present(
            list(state.get("messages") or []),
            sender,
            content,
        )
        return state

    def _ensure_current_message_present(self, messages: List[Dict], sender: str, content: str) -> List[Dict]:
        if not content.strip():
            return messages
        if messages:
            last_msg = messages[-1]
            if last_msg.get("sender") == sender and str(last_msg.get("content", "")).strip() == content.strip():
                return messages
        return messages + [{
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sender": sender,
            "content": content,
        }]

    def _ensure_current_formatted_present(self, messages: List[Dict], sender: str, content: str) -> List[Dict]:
        if not content.strip():
            return messages
        expected_content = f"[{sender}]: {content}"
        if messages:
            last_msg = messages[-1]
            if last_msg.get("role") == "user" and str(last_msg.get("content", "")).strip() == expected_content.strip():
                return messages
        sender_name = self.chat_log_manager._sanitize_name(sender)
        return messages + [{"role": "user", "name": sender_name, "content": expected_content}]

    def _mark_anchored_pending(self, chat_name: str, dynamic_messages: List[Dict]) -> None:
        state = self._anchored_contexts.get(chat_name)
        if state is not None:
            state["pending_messages"] = list(dynamic_messages)

    def _finalize_anchored_context(self, chat_name: str, response: str) -> None:
        state = self._anchored_contexts.get(chat_name)
        if not state:
            return
        pending = state.get("pending_messages")
        if not pending:
            return
        state["messages"] = self._strip_ephemeral_context_messages(list(pending)) + [
            {"role": "assistant", "content": response or ""}
        ]
        state["log_count"] = max(
            int(state.get("log_count") or 0),
            self.chat_log_manager.count_messages(chat_name),
        )
        state["pending_messages"] = None
        self._save_anchored_context(chat_name, state)

    def _strip_ephemeral_context_messages(self, messages: List[Dict]) -> List[Dict]:
        """Drop per-turn tool context before persisting anchored chat state."""
        return [
            msg for msg in messages
            if msg.get("name") not in {"search_context", "memory_context"}
        ]

    def _count_chat_prompt_tokens(
        self,
        messages: List[Dict],
        *,
        input_image_count: int = 0,
    ) -> tuple[int, str]:
        """Count the current chat prompt with the active provider's renderer."""
        try:
            token_count = get_llm_manager().count_rendered_prompt_tokens(
                plugin_name="builtin_chatbot",
                call_type="chat",
                messages=messages,
                input_image_count=input_image_count,
            )
            if token_count is not None:
                return int(token_count), "codex_o200k_base"
        except Exception as e:
            logger.warning(f"⚠️ Accurate Codex prompt token count unavailable, using heuristic: {e}")

        return self._estimate_messages_tokens(messages), "heuristic_fallback"

    def _estimate_messages_tokens(self, messages: List[Dict]) -> int:
        total = 0
        for msg in messages or []:
            total += 4
            total += self.context_manager.estimate_tokens(msg.get("role", ""))
            total += self.context_manager.estimate_tokens(msg.get("name", ""))
            total += self.context_manager.estimate_tokens(msg.get("content", ""))
        return total

    def _anchored_context_path(self, chat_name: str) -> Path:
        safe_name = re.sub(r'[\\/:*?"<>|\s]+', "_", chat_name).strip("_")
        if not safe_name:
            safe_name = "unknown_chat"
        return self._anchored_context_dir / f"{safe_name}.json"

    def invalidate_memory_context(self, chat_name: str) -> None:
        """Apply an explicit memory/settings edit on the next model turn."""
        self.memory_service.invalidate(chat_name)
        self._anchored_contexts.pop(chat_name, None)
        try:
            self._anchored_context_path(chat_name).unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Failed to invalidate anchored memory for %s: %s", chat_name, e)

    def _load_anchored_context(self, chat_name: str) -> Optional[Dict[str, Any]]:
        path = self._anchored_context_path(chat_name)
        if not path.exists():
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)

            state = payload.get("state") if isinstance(payload, dict) else None
            if not isinstance(state, dict) or not isinstance(state.get("messages"), list):
                return None

            state["pending_messages"] = None
            self._anchored_contexts[chat_name] = state
            logger.info(
                "🤖 Loaded persisted anchored context for %s: messages=%s, log_count=%s, tokens≈%s",
                chat_name,
                len(state.get("messages") or []),
                state.get("log_count"),
                self._estimate_messages_tokens(state.get("messages") or []),
            )
            return state
        except Exception as e:
            logger.warning(f"⚠️ Failed to load anchored context for {chat_name}: {e}")
            return None

    def _save_anchored_context(self, chat_name: str, state: Dict[str, Any]) -> None:
        try:
            path = self._anchored_context_path(chat_name)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "chat_name": chat_name,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "state": {
                    "messages": self._strip_ephemeral_context_messages(list(state.get("messages") or [])),
                    "log_count": int(state.get("log_count") or 0),
                    "anchor_message_count": int(state.get("anchor_message_count") or 0),
                    "memory_checkpoint": str(state.get("memory_checkpoint") or ""),
                    "memory_checkpoint_tokens": int(
                        state.get("memory_checkpoint_tokens") or 0
                    ),
                    "memory_checkpoint_created_at": state.get(
                        "memory_checkpoint_created_at"
                    ),
                    "pending_messages": None,
                },
            }
            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.warning(f"⚠️ Failed to save anchored context for {chat_name}: {e}")

    def _role_prompt_uses_dynamic_slots(self, role_prompt: str) -> bool:
        """Return whether a role prompt embeds per-turn dynamic variables itself."""
        if not role_prompt:
            return False
        dynamic_slots = ("chat_text", "search_results", "sender", "content")
        return any(
            re.search(r"\{\{\s*" + re.escape(slot) + r"\s*\}\}", role_prompt)
            or re.search(r"\{\s*" + re.escape(slot) + r"\s*\}", role_prompt)
            for slot in dynamic_slots
        )

    def _build_dynamic_input_block(self, context_text: str, search_text: str) -> str:
        """Append per-turn inputs after the static prompt prefix for better cache locality."""
        search_section = (search_text or "").strip() or "（本轮无可用搜索结果）"
        return f"""

【动态输入资料】
以下资料每轮可能变化。当前用户消息以最后一条 user message 为准；最近原始聊天优先于摘要，搜索结果优先于长期记忆。
不要复述资料结构，不要提“长期记忆/搜索结果/上下文”这些来源名，直接像群友一样接话。

## 群聊上下文
{context_text}

## 网络搜索结果
{search_section}
"""

    def _build_human_like_output_contract(self, role_name: str) -> str:
        settings = self.role_manager.get_output_settings(role_name)
        if not settings.get("enabled"):
            return ""

        max_chars = settings.get("max_chars", 120)
        max_count = settings.get("max_count", 3)
        strip_period = settings.get("strip_trailing_period", True)
        period_rule = "每条消息结尾不要用句号或中文句号。" if strip_period else ""
        return f"""【微信回复输出协议】
你必须只返回合法 json，不要返回 Markdown、代码块、解释文本或额外字段。

JSON 格式示例：
{{
  "messages": ["自然的一条微信回复"]
}}

messages 是你要发送到微信的消息数组。请像真实微信用户一样回复：短、自然、即时，不要写成文章。
能一句话说清就只返回 1 条。
只有在信息确实较多、或自然聊天节奏需要停顿时，才拆成 2 到 {max_count} 条。
通常不要超过 {max_count} 条。每条尽量短，目标约 {max_chars} 字以内，但不要机械截断句子。
不要为了凑条数而拆分，不要使用标题、列表、总结腔或客服腔。
{period_rule}
"""

    def _get_human_like_response_format(self, role_name: str) -> Optional[Dict[str, str]]:
        settings = self.role_manager.get_output_settings(role_name)
        if not settings.get("enabled"):
            return None
        return {"type": "json_object"}

    def _call_chat_llm(
        self,
        chat_name: str,
        role_name: str,
        messages: List[Dict],
        **kwargs,
    ) -> str:
        response_format = self._get_human_like_response_format(role_name)
        call_kwargs = dict(kwargs)
        call_kwargs["_wxautox_chat_name"] = chat_name
        call_kwargs["_wxautox_role_name"] = role_name
        # These hidden kwargs are consumed only by the local Codex adapter;
        # ordinary LLM providers and their fallback parameters stay untouched.
        if self.codex_persistent_session_enabled:
            call_kwargs["_wxautox_chat_id"] = chat_name
        call_kwargs["_wxautox_codex_reasoning_effort"] = self.codex_reasoning_effort
        call_kwargs["_wxautox_codex_reasoning_summary"] = self.codex_reasoning_summary
        call_kwargs["_wxautox_codex_web_search_mode"] = self.codex_web_search_mode
        call_kwargs["_wxautox_codex_timeout_seconds"] = self.codex_turn_timeout_seconds
        call_kwargs["_wxautox_codex_max_turns"] = self.codex_max_turns_per_thread
        call_kwargs["_wxautox_codex_exec_fallback"] = self.codex_exec_fallback_enabled
        if response_format:
            call_kwargs["response_format"] = response_format

        response = self._call_llm("chat", messages, **call_kwargs)
        if not response_format:
            return response
        attachment_capture = call_kwargs.get("_wxautox_attachment_capture")
        if isinstance(attachment_capture, list) and attachment_capture:
            logger.info("🤖 Human-like JSON retry skipped because model returned attachment(s)")
            return response

        if self._is_valid_human_like_response(response):
            return response

        retry_messages = messages + [
            {"role": "assistant", "content": response or ""},
            {
                "role": "user",
                "content": (
                    "上一次回复不是合法 json，或没有包含非空 messages 数组。"
                    "请重新只返回合法 json，格式为 {\"messages\": [\"一条自然微信回复\"]}。"
                ),
            },
        ]
        logger.warning("🤖 Human-like JSON response invalid or empty; retrying once")
        retry_kwargs = dict(call_kwargs)
        retry_kwargs["_wxautox_codex_retry"] = True
        return self._call_llm("chat", retry_messages, **retry_kwargs)

    def _is_valid_human_like_response(self, response: str) -> bool:
        return self._extract_human_like_messages(response or "") is not None

    def _strip_internal_action_markers(self, response: str) -> str:
        """移除模型误输出的内部动作占位文本，例如 [发送文件]。"""
        lines = []
        for line in str(response or "").splitlines():
            if line.strip() in ChatLogManager.INTERNAL_ACTION_MARKERS:
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    def _send_response_parts(
        self,
        wx_manager,
        chat_name: str,
        response: str,
        role_name: str,
        silent: bool = False,
        log_response: Optional[str] = None,
    ) -> bool:
        settings = self.role_manager.get_output_settings(role_name)
        parts = self._format_response_parts(response, settings)
        if not parts:
            return False

        log_parts = (
            self._format_response_parts(log_response, settings)
            if log_response is not None
            else []
        )

        interval = float(settings.get("interval_seconds", 0.0) or 0.0)
        send_session = getattr(wx_manager, "outbound_send_session", None)
        session_context = send_session() if callable(send_session) and len(parts) > 1 else nullcontext()
        with session_context:
            for index, part in enumerate(parts):
                if index > 0 and interval > 0:
                    time.sleep(interval)
                if not wx_manager.send_message(chat_name, part, silent=silent):
                    return False
                if index < len(log_parts):
                    self._save_response_part_to_log(chat_name, log_parts[index])
        return True

    def _send_response_attachments(self, wx_manager, chat_name: str, attachments: List[Dict[str, Any]]) -> bool:
        """发送模型 provider 返回的文件附件。"""
        if not attachments:
            return True

        artifact_root = Path(os.getenv("CODEX_PROXY_ARTIFACT_ROOT") or "tmp/images/codex")
        if not artifact_root.is_absolute():
            artifact_root = Path.cwd() / artifact_root
        allowed_root = artifact_root.resolve()
        allowed_suffixes = {
            ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx",
            ".rtf", ".odt", ".ods", ".odp", ".epub",
            ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
            ".xml", ".html", ".htm", ".log", ".sql",
            ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz",
            ".mp3", ".wav", ".m4a", ".flac", ".mp4", ".mov", ".webm", ".avi", ".mkv",
        }
        file_paths: List[str] = []
        seen = set()

        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            if attachment.get("type") not in (None, "image", "file"):
                continue
            raw_path = attachment.get("path")
            if not raw_path:
                continue

            path = Path(str(raw_path)).expanduser()
            try:
                resolved = path.resolve()
                resolved.relative_to(allowed_root)
            except Exception:
                logger.warning(f"🤖 跳过不在允许目录内的附件: {raw_path}")
                continue

            if resolved.suffix.lower() not in allowed_suffixes:
                logger.warning(f"🤖 跳过不支持的附件类型: {resolved}")
                continue
            if not resolved.exists() or not resolved.is_file():
                logger.warning(f"🤖 跳过不存在的附件: {resolved}")
                continue
            if str(resolved) in seen:
                continue

            seen.add(str(resolved))
            file_paths.append(str(resolved))

        if not file_paths:
            return True

        logger.info(f"🤖 Sending {len(file_paths)} model attachment(s) to {chat_name}")
        return bool(wx_manager.send_files(chat_name, file_paths))

    def _save_response_parts_to_log(self, chat_name: str, response: str, role_name: str) -> None:
        settings = self.role_manager.get_output_settings(role_name)
        for part in self._format_response_parts(response, settings):
            self._save_response_part_to_log(chat_name, part)

    def _save_response_part_to_log(self, chat_name: str, part: str) -> None:
        self.chat_log_manager.save_message(
            chat_name,
            self._bot_display_name_for_chat(chat_name),
            part,
            is_bot=True,
        )

    def _format_response_parts(self, response: str, settings: Dict[str, Any]) -> List[str]:
        text = self._strip_internal_action_markers(response)
        if not text:
            return []

        if not settings.get("enabled"):
            return [text]

        strip_period = bool(settings.get("strip_trailing_period", True))

        normalized = []
        for part in self._parse_model_response_parts(text):
            clean = self._clean_response_part(part)
            if clean in ChatLogManager.INTERNAL_ACTION_MARKERS:
                continue
            if strip_period:
                clean = re.sub(r"[。．.]+$", "", clean).strip()
            if clean:
                normalized.append(clean)
        if normalized:
            return normalized
        if text.lstrip().startswith(("{", "[")):
            return []
        return [text]



    def _parse_model_response_parts(self, text: str) -> List[str]:
        json_messages = self._extract_human_like_messages(text)
        if json_messages is not None:
            return json_messages

        # 1. 优先检查显式分隔符
        if "||SPLIT||" in text:
            return [part.strip() for part in text.split("||SPLIT||") if part.strip()]

        # 2. 检查 JSON 格式 (List 或包含 messages 的 Dict)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
            if isinstance(parsed, dict) and isinstance(parsed.get("messages"), list):
                return [str(item).strip() for item in parsed["messages"] if str(item).strip()]
        except Exception:
            pass

        # 3. 检查 JSONL 格式 (每行一个 JSON 对象)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 1:
            jsonl_parts = []
            is_jsonl = True
            for line in lines:
                try:
                    item = json.loads(line)
                    if isinstance(item, str):
                        jsonl_parts.append(item.strip())
                    elif isinstance(item, dict):
                        value = item.get("text") or item.get("content") or item.get("message")
                        if value:
                            jsonl_parts.append(str(value).strip())
                        else:
                            is_jsonl = False; break
                    else:
                        is_jsonl = False; break
                except Exception:
                    is_jsonl = False; break
            
            if is_jsonl and jsonl_parts:
                return jsonl_parts

        return [text]

    def _extract_human_like_messages(self, raw_text: str) -> Optional[List[str]]:
        """解析 Human-like Output 的 JSON 包装协议。"""
        if not raw_text:
            return None

        text = self._sanitize_judge_response_text(raw_text)
        candidates = [text]
        snippet = self._extract_balanced_json_snippet(text)
        if snippet and snippet not in candidates:
            candidates.append(snippet)

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue

            if isinstance(parsed, dict):
                messages = parsed.get("messages")
            elif isinstance(parsed, list):
                messages = parsed
            else:
                continue

            if not isinstance(messages, list):
                continue

            parts = []
            for item in messages:
                if isinstance(item, dict):
                    value = item.get("text") or item.get("content") or item.get("message")
                else:
                    value = item
                clean = str(value or "").strip()
                if clean:
                    parts.append(clean)

            if parts:
                return parts

        return None

    def _clean_response_part(self, text: str) -> str:
        clean = (text or "").strip()
        # 移除开头的无意义标点和空白
        clean = re.sub(r"^[，,。．.；;、\s]+", "", clean).strip()
        # 将连续的两个或更多换行符（空行）替换为单个换行符
        clean = re.sub(r"\n{2,}", "\n", clean)
        return clean



    def _process_quoted_image(self, message: Dict[str, Any], wx_manager) -> Optional[str]:
        """处理引用的图片，返回 base64 编码
        使用新的按需下载机制。
        """
        try:
            chat_name = message.get("chat_name", "")
            message_id = message.get("message_id")
            quote_image_path = message.get("quote_image_path")
            has_quote_image = message.get("has_quote_image", False)

            image_path = None

            # 1) 如果已经有图片路径，直接使用
            if quote_image_path and Path(quote_image_path).exists():
                image_path = quote_image_path
                logger.debug(f"🤖 使用已有引用图片路径: {image_path}")
            # 2) 如果有引用图片标记但没有路径，进行按需下载
            elif has_quote_image and wx_manager and message_id:
                try:
                    logger.info(f"🤖 开始按需下载引用图片: {chat_name}:{message_id}")
                    image_path = wx_manager.download_quote_image(chat_name, message_id=message_id)

                except Exception as e:
                    logger.error(f"🤖 按需下载引用图片失败: {e}")
                    image_path = None


            if not image_path or not Path(image_path).exists():
                # 引用图片必须精确来自当前引用消息。绝不能用“最近一张图”
                # 猜测，否则异步 OCR 下载的前一张/后一张图都可能串入。
                logger.error(
                    "🤖 引用图片精确路径不存在或下载失败，已拒绝使用最近图片: "
                    "chat=%s message_id=%s requested_path=%s downloaded_path=%s",
                    chat_name,
                    message_id,
                    quote_image_path,
                    image_path,
                )
                return None

            # 读取并编码为base64
            with open(image_path, "rb") as f:
                image_base64 = base64.b64encode(f.read()).decode("utf-8")



            return image_base64
        except Exception as e:
            logger.error(f"🤖 处理引用图片失败: {e}")
            return None

    def _detect_misidentified_quote_image(self, content: str) -> Optional[Dict[str, str]]:
        """检测被误识别为text的引用图片消息

        Args:
            content: 消息内容

        Returns:
            如果检测到引用图片特征，返回 {"prefix": "实际内容", "quoted_sender": "被引用者"}
            否则返回 None
        """
        # 正则匹配: "前缀内容引用 xxx 的消息 : 图片"
        pattern = r'^(.*)引用\s+(.+?)\s+的消息\s*[:：]\s*(图片|\[图片\])$'
        match = re.match(pattern, content.strip(), re.DOTALL)

        if match:
            prefix = match.group(1).strip()
            quoted_sender = match.group(2).strip()

            logger.info(f"🔧 检测到误识别的引用图片消息: prefix='{prefix}', quoted='{quoted_sender}'")
            return {
                "prefix": prefix,
                "quoted_sender": quoted_sender
            }

        return None

    def _get_user_permission_config(self, chat_name: str) -> Dict[str, Any]:
        """获取用户针对本插件的权限配置"""
        try:
            from app.models.base import SessionLocal
            from app.models.user_permission import UserPermission, WeChatUser

            with SessionLocal() as db:
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                if not user:
                    return {}

                perm = db.query(UserPermission).filter(
                    UserPermission.user_id == user.id,
                    UserPermission.plugin_name == 'builtin_chatbot'
                ).first()

                if perm:
                    return {
                        "proactive_enabled": perm.proactive_enabled,
                        "followup_enabled": bool(getattr(perm, "followup_enabled", False)),
                        "followup_window_seconds": int(getattr(perm, "followup_window_seconds", 60) or 60),
                        "followup_merge_seconds": int(getattr(perm, "followup_merge_seconds", 3) or 3),
                        "followup_max_turns": int(getattr(perm, "followup_max_turns", 3) or 3),
                        "memory_profile": getattr(perm, "memory_profile", None),
                        "ignored_senders": getattr(perm, "ignored_senders", None),
                    }
            return {}
        except Exception as e:
            logger.error(f"❌ Error getting user permission config: {e}")
            return {}

    def _get_ignored_senders(self, chat_name: str) -> List[str]:
        """Return normalized sender names ignored for this chat's builtin_chatbot permission."""
        raw_value = self._get_user_permission_config(chat_name).get("ignored_senders")
        if not raw_value:
            return []

        try:
            parsed = json.loads(raw_value)
        except Exception:
            parsed = raw_value.splitlines()

        if not isinstance(parsed, list):
            return []

        ignored = []
        for item in parsed:
            sender = str(item or "").strip()
            if sender:
                ignored.append(sender)
        return ignored

    def _is_sender_ignored(self, chat_name: str, sender: str) -> bool:
        if not chat_name or not sender:
            return False
        return sender.strip() in self._get_ignored_senders(chat_name)

    def _get_default_memory_config(self) -> Dict[str, Any]:
        return {
            "context_message_fetch_limit": self.context_message_fetch_limit,
            "context_window_strategy": self.context_window_strategy,
            "anchor_message_count": self.anchor_message_count,
            "anchor_rollover_prompt_tokens": self.anchor_rollover_prompt_tokens,
            "memory_enabled": self.memory_enabled,
            "memory_background_enabled": self.memory_background_enabled,
            "memory_event_min_messages": self.memory_event_min_messages,
            "memory_event_target_messages": self.memory_event_target_messages,
            "memory_event_max_messages": self.memory_event_max_messages,
            "memory_event_context_before_messages": self.memory_event_context_before_messages,
            "memory_event_context_after_messages": self.memory_event_context_after_messages,
            "memory_event_max_cards": self.memory_event_max_cards,
            "memory_event_input_token_budget": self.memory_event_input_token_budget,
            "memory_initial_backfill_messages": self.memory_initial_backfill_messages,
            "memory_max_chunks_per_run": self.memory_max_chunks_per_run,
            "memory_stage_event_threshold": self.memory_stage_event_threshold,
            "memory_stage_input_event_limit": self.memory_stage_input_event_limit,
            "memory_stage_input_token_budget": self.memory_stage_input_token_budget,
            "memory_stage_char_limit": self.memory_stage_char_limit,
            "memory_retrieval_top_k": self.memory_retrieval_top_k,
            "memory_query_recent_messages": self.memory_query_recent_messages,
            "memory_context_max_tokens": self.memory_context_max_tokens,
            "memory_person_v3_enabled": self.memory_person_v3_enabled,
            "memory_person_v3_auto_activate_live": self.memory_person_v3_auto_activate_live,
            "memory_person_v3_person_centric_enabled": self.memory_person_v3_person_centric_enabled,
            "memory_person_v3_min_pending_messages": self.memory_person_v3_min_pending_messages,
            "memory_person_v3_batch_related_messages": self.memory_person_v3_batch_related_messages,
            "memory_person_v3_max_batch_people": self.memory_person_v3_max_batch_people,
            "memory_person_v3_max_observations_per_batch": self.memory_person_v3_max_observations_per_batch,
            "memory_person_v3_input_token_budget": self.memory_person_v3_input_token_budget,
            "memory_person_v3_candidate_memory_value": self.memory_person_v3_candidate_memory_value,
            "memory_person_v3_refresh_threshold": self.memory_person_v3_refresh_threshold,
            "memory_person_v3_max_refresh_people": self.memory_person_v3_max_refresh_people,
            "memory_person_v3_retrieval_max_people": self.memory_person_v3_retrieval_max_people,
            "memory_person_v3_retrieval_max_items": self.memory_person_v3_retrieval_max_items,
            "memory_person_v3_include_high_sensitivity": self.memory_person_v3_include_high_sensitivity,
            "memory_embedding_enabled": self.memory_embedding_enabled,
            "memory_embedding_model": self.memory_embedding_model,
            "memory_embedding_threads": self.memory_embedding_threads,
            "memory_embedding_batch_size": self.memory_embedding_batch_size,
            "memory_dedup_enabled": self.memory_dedup_enabled,
            "memory_verification_enabled": self.memory_verification_enabled,
            "memory_dedup_lookback_days": self.memory_dedup_lookback_days,
            "memory_dedup_candidate_threshold": self.memory_dedup_candidate_threshold,
            "memory_duplicate_similarity_threshold": self.memory_duplicate_similarity_threshold,
            "memory_retrieval_mmr_lambda": self.memory_retrieval_mmr_lambda,
            "memory_retrieval_diversity_threshold": self.memory_retrieval_diversity_threshold,
            "memory_checkpoint_max_tokens": self.memory_checkpoint_max_tokens,
            "memory_retention_days": self.memory_retention_days,
            "memory_candidate_retention_days": self.memory_candidate_retention_days,
            "memory_maintenance_interval_hours": self.memory_maintenance_interval_hours,
        }

    def _sanitize_memory_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        defaults = self._get_default_memory_config()
        int_ranges = {
            "context_message_fetch_limit": (20, 20000),
            "anchor_message_count": (20, 5000),
            "anchor_rollover_prompt_tokens": (4096, 1000000),
            "memory_event_min_messages": (5, 200),
            "memory_event_target_messages": (5, 500),
            "memory_event_max_messages": (5, 1000),
            "memory_event_context_before_messages": (0, 100),
            "memory_event_context_after_messages": (0, 100),
            "memory_event_max_cards": (1, 12),
            "memory_event_input_token_budget": (1024, 100000),
            "memory_initial_backfill_messages": (0, 100000),
            "memory_max_chunks_per_run": (1, 20),
            "memory_stage_event_threshold": (5, 500),
            "memory_stage_input_event_limit": (5, 500),
            "memory_stage_input_token_budget": (2048, 200000),
            "memory_stage_char_limit": (1000, 20000),
            "memory_retrieval_top_k": (1, 20),
            "memory_query_recent_messages": (1, 100),
            "memory_context_max_tokens": (512, 20000),
            "memory_person_v3_refresh_threshold": (1, 100),
            "memory_person_v3_max_refresh_people": (1, 20),
            "memory_person_v3_min_pending_messages": (5, 500),
            "memory_person_v3_batch_related_messages": (8, 500),
            "memory_person_v3_max_batch_people": (1, 20),
            "memory_person_v3_max_observations_per_batch": (4, 30),
            "memory_person_v3_input_token_budget": (4000, 100000),
            "memory_person_v3_retrieval_max_people": (1, 6),
            "memory_person_v3_retrieval_max_items": (3, 20),
            "memory_embedding_threads": (1, 8),
            "memory_embedding_batch_size": (1, 64),
            "memory_dedup_lookback_days": (1, 3650),
            "memory_checkpoint_max_tokens": (512, 100000),
            "memory_retention_days": (0, 3650),
            "memory_candidate_retention_days": (7, 3650),
            "memory_maintenance_interval_hours": (1, 720),
        }
        for key, (lower, upper) in int_ranges.items():
            try:
                value = int(config.get(key, defaults[key]))
            except Exception:
                value = defaults[key]
            config[key] = max(lower, min(upper, value))

        for key in (
            "memory_enabled",
            "memory_background_enabled",
            "memory_embedding_enabled",
            "memory_dedup_enabled",
            "memory_verification_enabled",
            "memory_person_v3_enabled",
            "memory_person_v3_auto_activate_live",
            "memory_person_v3_person_centric_enabled",
            "memory_person_v3_include_high_sensitivity",
        ):
            config[key] = bool(config.get(key, defaults[key]))

        float_ranges = {
            "memory_dedup_candidate_threshold": (0.5, 0.99),
            "memory_duplicate_similarity_threshold": (0.6, 0.999),
            "memory_retrieval_mmr_lambda": (0.1, 1.0),
            "memory_retrieval_diversity_threshold": (0.6, 0.999),
            "memory_person_v3_candidate_memory_value": (0.35, 0.95),
        }
        for key, (lower, upper) in float_ranges.items():
            try:
                value = float(config.get(key, defaults[key]))
            except (TypeError, ValueError):
                value = float(defaults[key])
            config[key] = max(lower, min(upper, value))
        config["memory_duplicate_similarity_threshold"] = max(
            config["memory_dedup_candidate_threshold"],
            config["memory_duplicate_similarity_threshold"],
        )

        config["memory_event_target_messages"] = max(
            config["memory_event_min_messages"],
            config["memory_event_target_messages"],
        )
        config["memory_event_max_messages"] = max(
            config["memory_event_target_messages"],
            config["memory_event_max_messages"],
        )
        config["memory_stage_input_event_limit"] = max(
            config["memory_stage_event_threshold"],
            config["memory_stage_input_event_limit"],
        )
        config["memory_embedding_model"] = str(
            config.get("memory_embedding_model")
            or defaults["memory_embedding_model"]
        ).strip()

        strategy = str(
            config.get("context_window_strategy")
            or defaults.get("context_window_strategy")
            or "anchored_append"
        ).strip().lower()
        config["context_window_strategy"] = (
            strategy
            if strategy in {"sliding", "anchored_append"}
            else "anchored_append"
        )

        return config

    def _memory_source_fetch_limit(self, memory_config: Dict[str, Any]) -> int:
        """Reply construction only needs the configured recent raw window."""
        limit = int(memory_config.get("context_message_fetch_limit") or 300)
        return max(20, min(20000, limit))

    def _get_chat_memory_config(self, chat_name: str) -> Dict[str, Any]:
        """合并全局默认与该群/用户的 Memory Profile 覆盖项。"""
        config = self._get_default_memory_config()
        try:
            perm_config = self._get_user_permission_config(chat_name)
            raw_profile = perm_config.get("memory_profile")
            if raw_profile:
                profile = json.loads(raw_profile)
                if isinstance(profile, dict) and profile.get("enabled"):
                    overrides = profile.get("overrides") if isinstance(profile.get("overrides"), dict) else profile
                    for key in config.keys():
                        if key in overrides and overrides[key] is not None:
                            config[key] = overrides[key]
                    logger.debug(f"🧠 Memory profile applied for {chat_name}: {config}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load memory profile for {chat_name}: {e}")

        sanitized = self._sanitize_memory_config(config)
        # Bot replies are present in the same raw chat log as human messages.
        # They must never be learned back as a group member profile.
        sanitized["memory_person_v3_excluded_sender_names"] = self._bot_names_for_chat(chat_name)
        sanitized["memory_person_v3_excluded_sender_ids"] = []
        return sanitized

    def _analyze_chat_state(self, chat_name: str, scan_threshold: Optional[int] = None) -> Dict[str, Any]:
        """分析聊天状态：计算自上次机器人回复后的消息数和时间"""
        try:
            # 读取足够多的历史消息以找到机器人上次的回复
            # 扫描范围：阈值 * 2 或 至少100条
            threshold = 0 if scan_threshold is None else scan_threshold
            scan_limit = max(100, threshold * 3)
            messages = self.chat_log_manager.get_context_messages(chat_name, limit=scan_limit)

            if not messages:
                return {"msg_count": 0, "last_reply_time": None}

            # 倒序查找机器人的最后一条消息
            last_bot_index = -1
            for i in range(len(messages) - 1, -1, -1):
                # 简单匹配发送者名称 (可能需要更严谨的判断，但目前足够)
                if self._is_chatbot_reply_record(messages[i]):
                    last_bot_index = i
                    break

            if last_bot_index == -1:
                # 范围内没找到机器人回复 -> 视为无限久
                # 返回当前消息总数作为计数
                msg_count = len(messages)
                last_time = None
            else:
                # 计数：最后一条机器人消息之后的消息数
                msg_count = len(messages) - 1 - last_bot_index

                # 解析时间
                last_time_str = messages[last_bot_index].get("time")
                try:
                    # 尝试解析标准格式
                    last_time = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S")
                except:
                    last_time = None

            return {"msg_count": msg_count, "last_reply_time": last_time}
        except Exception as e:
            logger.error(f"❌ Error analyzing chat state: {e}")
            return {"msg_count": 0, "last_reply_time": None}

    def _is_chatbot_reply_record(self, message: Dict[str, Any]) -> bool:
        """判断聊天记录中的机器人消息是否应视为 chatbot 角色发言。"""
        if not message.get("is_bot") and message.get("sender") != self.bot_name:
            return False

        content = str(message.get("content") or "").strip()
        if not content:
            return False

        # summary_plus 和其他工具型插件也会通过同一个微信账号发言。
        # 这些工具输出不应重置角色主动 Judge 的消息计数，否则大群里频繁摘要会压住角色插话。
        if self.chat_log_manager._is_internal_action_message(message):
            return False
        summary_markers = ("📖 一句话总结", "🔑 关键要点", "🏷 标签")
        if any(marker in content for marker in summary_markers):
            return False

        return True

    def _is_tool_output_record(self, message: Dict[str, Any]) -> bool:
        """判断聊天记录中的机器人消息是否为工具型输出。"""
        if not message.get("is_bot") and message.get("sender") != self.bot_name:
            return False
        return not self._is_chatbot_reply_record(message)

    def _get_judge_context_messages(self, chat_name: str, limit: int = 20) -> List[Dict[str, Any]]:
        """获取 Judge 上下文，保留工具摘要但避免误判为角色刚发言。"""
        raw_messages = self.chat_log_manager.get_context_messages(chat_name, limit * 3)
        normalized = [self._normalize_judge_context_message(msg) for msg in raw_messages]
        return normalized[-limit:]

    def _normalize_judge_context_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """把工具型机器人输出改写为背景消息，供 Judge 正确理解。"""
        if not self._is_tool_output_record(message):
            return message

        normalized = dict(message)
        content = str(normalized.get("content") or "").strip()
        normalized["sender"] = "工具摘要"
        normalized["content"] = f"【工具输出，不代表{self.bot_name}角色发言】{content}"
        return normalized

    def _build_judge_output_guard(self) -> str:
        """统一的 Judge 输出约束（无需手写在 Prompt 里）。"""
        return (
            "你必须只输出一个 JSON 对象，不要输出 Markdown、代码块、解释文本。"
            "JSON 必须包含以下字段："
            '{"should_reply": true/false, "reason": "string", "atmosphere": "string"}。'
            "其中 should_reply 必须是布尔值。"
        )

    def _sanitize_judge_response_text(self, raw_text: str) -> str:
        """清洗 Judge 原始输出，移除常见的不可见字符和 markdown 包裹。"""
        if not isinstance(raw_text, str):
            raw_text = str(raw_text or "")

        text = raw_text.strip().lstrip("\ufeff")
        text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\u2060", "")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```\s*$", "", text)
        return text.strip()

    def _extract_balanced_json_snippet(self, text: str) -> Optional[str]:
        """提取第一个花括号平衡的 JSON 对象片段，忽略字符串内的大括号。"""
        start_idx = text.find("{")
        if start_idx == -1:
            return None

        depth = 0
        in_string = False
        escape = False

        for idx in range(start_idx, len(text)):
            char = text[idx]

            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start_idx : idx + 1]

        return None

    def _extract_first_json_object(self, raw_text: str) -> Optional[Dict[str, Any]]:
        """从模型回复中提取第一个可解析 JSON 对象。"""
        if not raw_text:
            return None

        text = self._sanitize_judge_response_text(raw_text)
        candidates: List[str] = [text]
        decoder = json.JSONDecoder()

        # 1) 优先提取 markdown json 代码块
        code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if code_block_match:
            candidates.insert(0, code_block_match.group(1).strip())

        # 2) 回退：提取第一个括号平衡的 JSON 对象
        balanced_json = self._extract_balanced_json_snippet(text)
        if balanced_json:
            candidates.append(balanced_json.strip())

        for candidate in candidates:
            try:
                return json.loads(candidate)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

            try:
                parsed_obj, _ = decoder.raw_decode(candidate.lstrip())
                if isinstance(parsed_obj, dict):
                    return parsed_obj
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return None

    def _normalize_judge_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """标准化 Judge 结果，容忍轻微字段偏差。"""
        raw_decision = result.get(
            "should_reply",
            result.get("reply", result.get("need_reply", result.get("decision", False)))
        )

        if isinstance(raw_decision, bool):
            should_reply = raw_decision
        elif isinstance(raw_decision, (int, float)):
            should_reply = raw_decision != 0
        elif isinstance(raw_decision, str):
            value = raw_decision.strip().lower()
            if value in {"true", "1", "yes", "y", "reply", "需要", "是"}:
                should_reply = True
            elif value in {"false", "0", "no", "n", "silent", "stay silent", "不需要", "否"}:
                should_reply = False
            else:
                should_reply = False
        else:
            should_reply = False

        reason = (
            result.get("reason")
            or result.get("decision_reason")
            or result.get("why")
            or "No reason provided"
        )
        atmosphere = result.get("atmosphere") or result.get("mood") or ""

        return {
            "should_reply": bool(should_reply),
            "reason": str(reason),
            "atmosphere": str(atmosphere),
        }

    def _consult_judge(self, context_messages: List[Dict], role_name: str, judge_name: str) -> bool:
        """咨询裁判是否应该插话（使用 LLM Manager）"""
        try:
            # 使用统一的格式化方法
            chat_text = self._format_chat_text(context_messages)

            # 根据用户绑定 Judge 渲染判断提示词（simple/template 双模式）
            prompt = self.judge_manager.get_judge_prompt(
                judge_name,
                variables={"chat_text": chat_text},
            )
            if not prompt:
                logger.warning(f"⚖️ Judge prompt empty for judge '{judge_name}'")
                return False

            # 使用 LLM Manager 调用 judge
            messages = [
                {"role": "system", "content": self._build_judge_output_guard()},
                {"role": "user", "content": prompt},
            ]

            # 尝试使用原生 JSON 模式
            # 注意：LiteLLM/DeepSeek 支持 response_format={"type": "json_object"}
            response_text = self._call_llm("judge", messages, response_format={"type": "json_object"})

            logger.debug(f"⚖️ Judge raw response: {response_text}")

            # 解析 JSON 响应
            try:
                parsed_json = self._extract_first_json_object(response_text)
                if not parsed_json:
                    logger.error(
                        "❌ Failed to extract JSON from judge response. Raw repr: %r, sanitized repr: %r",
                        response_text,
                        self._sanitize_judge_response_text(response_text),
                    )
                    return False

                normalized = self._normalize_judge_result(parsed_json)
                should_reply = normalized["should_reply"]
                reason = normalized["reason"]
                atmosphere = normalized["atmosphere"]

                append_dashboard_event(
                    "judge_decision",
                    {
                        "should_reply": should_reply,
                        "reason": reason,
                        "atmosphere": atmosphere,
                        "role_name": role_name,
                        "judge_name": judge_name,
                    }
                )

                if should_reply:
                    logger.info(f"⚖️ Judge[{judge_name}] decided to REPLY: {reason}")
                else:
                    logger.info(f"⚖️ Judge[{judge_name}] decided to STAY SILENT: {reason}")

                return should_reply
            except (json.JSONDecodeError, AttributeError, ValueError) as e:
                logger.error(
                    "❌ Failed to parse judge response as JSON. Raw repr: %r, sanitized repr: %r, error: %s",
                    response_text[:500] if isinstance(response_text, str) else response_text,
                    self._sanitize_judge_response_text(response_text)[:500],
                    e,
                )
                return False

        except Exception as e:
            logger.error(f"❌ Judge consultation failed: {e}")
            return False

    def _call_llm(self, call_type: str, messages: List[Dict], **kwargs) -> str:
        """
        统一调用 LLM（使用 LLM Manager）

        Args:
            call_type: 调用类型 ("chat", "vision", "judge")
            messages: OpenAI 格式的消息数组
            **kwargs: 额外参数（如 response_format）

        Returns:
            模型返回的文本内容
        """
        try:
            llm_manager = get_llm_manager()
            response = llm_manager.call(
                plugin_name="builtin_chatbot",
                call_type=call_type,
                messages=messages,
                **kwargs
            )
            return self._strip_markdown(response)
        except Exception as e:
            logger.error(f"🤖 LLM调用失败 ({call_type}): {e}")
            raise

    def _reconcile_memory_trace(
        self,
        memory_trace: Optional[Dict[str, Any]],
        messages: List[Dict],
    ) -> Optional[Dict[str, Any]]:
        """Verify trace entries against the final messages sent to the model."""
        if not memory_trace:
            return None
        trace = json.loads(json.dumps(memory_trace, ensure_ascii=False))
        memory_text = self._extract_injected_memory_text(messages)
        trace["final_prompt_verified"] = True

        if trace.get("enabled") is False:
            trace["tokens"] = 0
            return trace

        injected_events = []
        dropped_events = list(trace.get("dropped_events") or [])
        dropped_event_ids = {int(item.get("id") or 0) for item in dropped_events}
        for event in trace.get("events") or []:
            prompt_text = str(event.get("prompt_text") or "")
            if prompt_text and prompt_text in memory_text:
                injected_events.append(event)
                continue
            value = dict(event)
            value["prompt_text"] = ""
            value["drop_reason"] = "final_prompt_budget"
            event_id = int(value.get("id") or 0)
            if event_id not in dropped_event_ids:
                dropped_events.append(value)
                dropped_event_ids.add(event_id)
        trace["events"] = injected_events
        trace["dropped_events"] = dropped_events

        injected_people = []
        dropped_people = list(trace.get("dropped_people") or [])
        dropped_people_keys = {
            (str(item.get("name") or ""), int(item.get("source_event_id") or 0))
            for item in dropped_people
        }
        for person in trace.get("people") or []:
            prompt_text = str(person.get("prompt_text") or "")
            if prompt_text and prompt_text in memory_text:
                injected_people.append(person)
                continue
            value = dict(person)
            value["prompt_text"] = ""
            value["drop_reason"] = "final_prompt_budget"
            key = (
                str(value.get("name") or ""),
                int(value.get("source_event_id") or 0),
            )
            if key not in dropped_people_keys:
                dropped_people.append(value)
                dropped_people_keys.add(key)
        trace["people"] = injected_people
        trace["dropped_people"] = dropped_people

        stage = dict(trace.get("stage") or {})
        stage_prompt = str(stage.get("prompt_text") or "")
        if stage_prompt and stage_prompt in memory_text:
            stage["included"] = True
        else:
            actual_stage = self._extract_memory_section(
                memory_text,
                "## 当前阶段记忆",
                ("## 本轮相关人物资料", "## 检索到的相关历史事件"),
            )
            if actual_stage:
                stage["included"] = True
                stage["prompt_text"] = actual_stage
                stage["text"] = actual_stage.replace(
                    "## 当前阶段记忆",
                    "",
                    1,
                ).lstrip()
                stage["truncated"] = True
            else:
                stage["included"] = False
                stage["prompt_text"] = ""
                stage["text"] = ""
        trace["stage"] = stage
        trace["tokens"] = self.context_manager.estimate_tokens(memory_text)
        return trace

    @classmethod
    def _extract_injected_memory_text(cls, messages: List[Dict]) -> str:
        marker = "以下是系统按当前话题检索出的群聊记忆。"
        for message in messages:
            content = cls._llm_record_content_text(message.get("content"))
            if message.get("name") == "memory_context" and marker in content:
                return content[content.find(marker) :].strip()
        for message in messages:
            content = cls._llm_record_content_text(message.get("content"))
            start = content.find(marker)
            if start < 0:
                continue
            value = content[start:]
            boundaries = (
                "\n\n## 最近原始聊天记录",
                "\n\n【本轮网络搜索资料】",
                "\n\n【当前用户消息】",
            )
            end_positions = [
                value.find(boundary)
                for boundary in boundaries
                if value.find(boundary) >= 0
            ]
            if end_positions:
                value = value[: min(end_positions)]
            return value.strip()
        return ""

    @staticmethod
    def _extract_memory_section(
        memory_text: str,
        heading: str,
        next_headings: tuple[str, ...],
    ) -> str:
        start = memory_text.find(heading)
        if start < 0:
            return ""
        value = memory_text[start:]
        end_positions = [
            value.find(next_heading)
            for next_heading in next_headings
            if value.find(next_heading) >= 0
        ]
        if end_positions:
            value = value[: min(end_positions)]
        return value.strip()

    @classmethod
    def _llm_record_content_text(cls, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                value
                for value in (
                    cls._llm_record_content_text(item)
                    for item in content
                )
                if value
            )
        if isinstance(content, dict):
            if content.get("type") in {"image_url", "input_image"}:
                return ""
            for key in ("text", "input_text", "output_text", "content"):
                if key in content:
                    return cls._llm_record_content_text(content.get(key))
            return ""
        return str(content)

    def _strip_markdown(self, text: str) -> str:
        """移除 Markdown 和 HTML 格式"""
        fenced = re.fullmatch(r"\s*```(?:\w+)?\s*(.*?)\s*```\s*", text or "", flags=re.S)
        if fenced:
            text = fenced.group(1)
        # 1. 移除 Markdown 代码块
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        # 2. 移除行内代码
        text = re.sub(r"`([^`]*)`", r"\1", text)
        # 3. 移除加粗/斜体
        text = re.sub(r"(\*\*|\*|_|~~)(.*?)\1", r"\2", text)
        # 4. 移除标题标记
        text = re.sub(r"^#+\s*", "", text, flags=re.M)

        # 5. 处理 HTML 标签
        # 将 <br> 和 <br/> 转换为换行符
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        # 移除其他常见的 HTML 标签 (保留内容)
        text = re.sub(r"</?(?:p|div|span|strong|em|b|i|u)>", "", text, flags=re.I)
        # 移除任何剩余的 HTML 标签
        text = re.sub(r"<[^>]+>", "", text)

        return text.strip()

    def handle_image_message(self, event: Event):
        """处理图片消息事件（OCR）。

        OCR 只用于补充聊天记录上下文，不需要向微信发送消息；因此不要在
        EventBus 的单群串行 worker 中同步等待下载/模型识别，避免慢 OCR
        阻塞后续 @/引用图片/Codex 等真正需要及时响应的消息。
        """
        try:
            logger.info(f"🤖 ChatBot handle_image_message triggered. ocr_enabled={self.ocr_enabled}")

            if not self.ocr_enabled:
                logger.debug("🤖 OCR disabled, skipping")
                return False

            data = dict(event.data or {})
            chat_name = data.get("chat_name", "")
            sender = data.get("sender", "")
            file_path = data.get("file_path", "")

            if self._is_sender_ignored(chat_name, sender):
                logger.info(f"🤖 Ignored blacklisted image sender: chat={chat_name}, sender={sender}")
                return False

            wx_manager = event.context.get("wx")
            message_id = data.get("message_id")
            logger.info(f"🤖 OCR queued async: chat={chat_name}, sender={sender}, msg={message_id}, file={file_path or 'missing'}")
            threading.Thread(
                target=self._process_image_ocr_async,
                args=(data, wx_manager),
                name=f"ChatBot-OCR-{chat_name}-{message_id or 'noid'}",
                daemon=True,
            ).start()

            # 只是后台记录上下文，不消费/阻断该图片消息事件。
            return False

        except Exception as e:
            logger.error(f"❌ Error queueing image OCR: {e}")
            return False

    def _process_image_ocr_async(self, data: Dict[str, Any], wx_manager) -> None:
        """后台执行图片下载和 OCR，并把结果写入聊天记录。"""
        try:
            chat_name = data.get("chat_name", "")
            sender = data.get("sender", "")
            file_path = data.get("file_path", "")
            message_id = data.get("message_id")

            logger.debug(f"🤖 OCR async processing: chat={chat_name}, file={file_path}")

            if not file_path or not os.path.exists(file_path):
                if wx_manager and message_id:
                    logger.info(f"🤖 OCR async: file_path missing, attempting download for msg {message_id}")
                    try:
                        file_path = wx_manager.download_image_message(chat_name, message_id)
                        if file_path:
                            logger.info(f"🤖 OCR async download success: {file_path}")
                        else:
                            logger.warning("🤖 OCR async download returned None")
                    except Exception as e:
                        logger.error(f"🤖 OCR async download failed: {e}")

            if not file_path:
                logger.warning("🤖 OCR async skipped: No file_path in event data and download failed")
                return

            if not os.path.exists(file_path):
                logger.warning(f"🤖 OCR async skipped: File not found at {file_path}")
                return

            try:
                with open(file_path, "rb") as f:
                    image_base64 = base64.b64encode(f.read()).decode("utf-8")
            except Exception as e:
                logger.error(f"❌ Failed to read image file for OCR: {e}")
                return

            logger.info("🤖 Calling OCR in background...")
            ocr_text = get_ocr_result(image_base64)
            if ocr_text:
                logger.info(f"✅ OCR Success for {chat_name}: {ocr_text[:20]}...")
                formatted_msg = f"[{sender}] 图片内容：“{ocr_text}”"
            else:
                logger.info("🤖 OCR returned no text")
                formatted_msg = f"[{sender}] [发送了一张图片]"

            self.chat_log_manager.save_message(chat_name, "OCR", formatted_msg)
        except Exception as e:
            logger.error(f"❌ Error handling image OCR in background: {e}")


# 全局实例
chatbot_plugin = None


def get_chatbot_plugin():
    """获取chatbot_plugin实例（避免导入时的None问题）"""
    return chatbot_plugin


def handle_text_message(event: Event):
    """处理文本消息事件"""
    global chatbot_plugin
    if chatbot_plugin:
        return chatbot_plugin.handle_text_message(event)
    return False


def handle_quote_image_message(event: Event):
    """处理引用图片消息事件"""
    global chatbot_plugin
    if chatbot_plugin:
        return chatbot_plugin.handle_quote_image_message(event)
    return False


def handle_image_message(event: Event):
    """处理图片消息事件"""
    global chatbot_plugin
    if chatbot_plugin:
        return chatbot_plugin.handle_image_message(event)
    return False


def handle_followup_approved(event: Event):
    """Run an approved follow-up reply inside the chat's serialized EventBus queue."""
    global chatbot_plugin
    if chatbot_plugin:
        return chatbot_plugin.handle_text_message(event)
    return False


def register(event_bus, subscribe):
    """插件注册函数"""
    global chatbot_plugin

    logger.info("🤖 Registering ChatBot plugin...")

    # 初始化ChatBot插件
    chatbot_plugin = ChatBotPlugin()
    chatbot_plugin.event_bus = event_bus

    # 订阅文本消息事件
    subscribe(
        event_type=EventType.TEXT_MESSAGE_RECEIVED,
        handler=handle_text_message
        # 不指定优先级，使用配置文件中的值
    )

    # 订阅图片消息事件
    subscribe(
        event_type=EventType.IMAGE_MESSAGE_RECEIVED,
        handler=handle_image_message,
    )

    # 订阅引用图片消息事件 - 专门处理需要图片的场景
    subscribe(
        event_type=EventType.QUOTE_IMAGE_MESSAGE_RECEIVED,
        handler=handle_quote_image_message,
    )

    # 订阅引用文字消息事件 - 处理纯文字引用
    subscribe(
        event_type=EventType.QUOTE_TEXT_MESSAGE_RECEIVED,
        handler=handle_text_message,
    )

    subscribe(
        event_type=EventType.CHATBOT_FOLLOWUP_APPROVED,
        handler=handle_followup_approved,
    )

    logger.info("✅ ChatBot 插件注册成功")


def unregister():
    """取消注册插件"""
    global chatbot_plugin

    logger.info("🤖 Unregistering ChatBot plugin...")
    if chatbot_plugin:
        chatbot_plugin.close()
    chatbot_plugin = None
    logger.info("ChatBot plugin unregistered")
