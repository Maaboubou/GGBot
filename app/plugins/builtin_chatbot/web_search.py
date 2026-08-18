"""
网络搜索服务 - 基于 Gemini + Google Search (Unified via LLMManager)
"""

import logging
import json
import re
from datetime import datetime
from typing import Optional, List, Dict

from app.utils.plugin_config import get_config

logger = logging.getLogger(__name__)


class WebSearchService:
    """网络搜索服务 (LLMManager 版)"""
    
    def __init__(self):
        from app.services.llm_manager import get_llm_manager

        self.llm_manager = get_llm_manager()
        self.search_prompt = get_config("search_prompt", None) or self._get_default_search_prompt()
    
    def search(
        self,
        context_messages: List[Dict],
        image_base64: Optional[str] = None,
        bot_names: Optional[List[str]] = None,
    ) -> str:
        """
        统一的搜索方法, 适用于主动和被动模式
        
        Args:
            context_messages: 对话历史消息列表
            image_base64: 可选的图片 Base64 编码 (不带前缀)
        
        Returns:
            搜索结果字符串
        """
        try:
            # 1. 构建 System Prompt
            system_prompt = self._get_unified_search_prompt()
            
            messages = [
                {"role": "system", "content": system_prompt}
            ]
            
            # 2. 构建 User Message
            # 关键：当前触发消息优先；若最后一句只是转交/召唤机器人，
            # 则回溯最近的实质问题作为搜索目标，避免旧话题污染。
            user_content = []
            
            # 清理历史记录中的 @bot 名称，避免泄露到搜索词中
            from app.services.config_service import get_setting
            bot_name = get_setting("WECHAT_BOT_NAME", "微信助手")
            names = list(bot_names or [bot_name])
            
            cleaned_context = []
            for msg in context_messages:
                m = dict(msg)
                if 'content' in m and isinstance(m['content'], str):
                    from app.utils.bot_mentions import strip_bot_mentions

                    m['content'] = strip_bot_mentions(m['content'], names)
                cleaned_context.append(m)
            cleaned_context = self._dedupe_adjacent_messages(cleaned_context)

            has_image = bool(image_base64)
            if cleaned_context:
                search_task = self._build_search_task(cleaned_context, has_image=has_image)
            else:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if has_image:
                    search_task = (
                        f"【当前时间】{now} GMT+8 / Asia/Shanghai\n\n"
                        "本轮请求包含图片。请先理解图片可见内容，再围绕图片中的文字、Logo、品牌、商品、"
                        "车型、游戏/IP、截图平台、地点或事件线索执行联网搜索。"
                    )
                else:
                    search_task = f"【当前时间】{now} GMT+8 / Asia/Shanghai\n\n请执行联网搜索。"
            
            user_content.append({"type": "text", "text": search_task})
            
            # 添加图片 (如果存在)
            if image_base64:
                image_url = f"data:image/jpeg;base64,{image_base64}"
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": image_url}
                })
            
            messages.append({"role": "user", "content": user_content})
            
            # 4. 调用 LLMManager (启用 Google Search 工具)
            # 注意: 这里假设底层模型支持 Google Search 工具 (如 gemini-3-flash)
            logger.info(f"🔍 开始网络搜索 (Context: {len(context_messages)} msgs, Image: {'Yes' if image_base64 else 'No'})")
            
            response = self.llm_manager.call(
                plugin_name="builtin_chatbot",
                call_type="web_search",
                messages=messages,
                tools=[{"google_search": {}}]  # 启用 Google Search
            )

            # 调用完成后读取实际使用的模型（fallback 时与主模型不同）
            model_name = (
                self.llm_manager.last_used_model
                or self.llm_manager.get_model_name("builtin_chatbot", "web_search")
            )
            
            if response:
                content = self._normalize_search_response(response)
                # 记录符合 Dashboard 解析格式的日志
                # Format: 🔍 Web Search Success | Query: "..." | Model: ... | Length: ... | Content: ...
                # 取用户最后一条消息作为查询词（便于 dashboard 展示）
                query_text = ""
                if cleaned_context:
                    raw = cleaned_context[-1].get('content', '')
                    # 只取前 80 个字符，避免日志过长
                    query_text = str(raw)[:80]
                if image_base64:
                    query_text += " (with Image)" if query_text else "(Image)"
                
                # 注意：为了让 dashboard.py 能读取多行内容，这里确保 content 中的换行符被写入日志
                # dashboard.py 会读取 Content: 之后的所有行
                logger.info(f'🔍 Web Search Success | Query: "{query_text}" | Model: {model_name} | Length: {len(content)} | Content: {content}')
                try:
                    from app.utils.dashboard_events import append_dashboard_event

                    append_dashboard_event(
                        "web_search",
                        {
                            "query": query_text,
                            "content": content,
                            "result_length": len(content),
                            "model_name": model_name,
                        }
                    )
                except Exception as event_error:
                    logger.debug(f"Dashboard web_search event skipped: {event_error}")
                return content
            else:
                logger.warning("⚠️ 搜索返回空结果")
                return "（⚠️ 网络搜索未获取到相关信息）"

        except Exception as e:
            logger.error(f"❌ 搜索服务出错: {e}")
            return "（⚠️ 网络搜索服务暂时不可用）"

    def _format_chat_text(self, context_messages: List[Dict]) -> str:
        """统一的聊天记录格式化方法"""
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

    def _build_search_task(self, cleaned_context: List[Dict], has_image: bool = False) -> str:
        """Build a focused prompt for prompt-triggered native web search."""
        chat_text = self._format_chat_text(cleaned_context)
        current_msg = cleaned_context[-1] if cleaned_context else {}
        current_line = self._format_chat_text([current_msg]) if current_msg else "（无）"
        target_lines = self._format_chat_text(self._select_search_target_messages(cleaned_context))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        image_task = ""
        if has_image:
            image_task = (
                "\n【图片搜索强制要求】\n"
                "本轮请求包含图片。你必须先理解图片中的可见内容，再围绕图片内容触发联网搜索；"
                "不要只根据用户文字判断为闲聊。\n"
                "如果用户文字只是“好看吗”“这是什么”“怎么看”“踢了吧”这类短句，"
                "仍以图片中的主体、文字、Logo、品牌、车型、游戏/IP、人物公开身份、地点、截图平台或事件线索作为搜索目标。\n"
                "必须提取 1-5 个图片相关搜索查询并调用搜索工具。只有在已经围绕图片内容搜索后，"
                "仍完全找不到可关联公开信息时，才说明未找到明确外部结果；不要返回 status=no_result。\n"
                "不要做人肉、隐私定位或普通素人身份识别；人物只在图片中有明确公开人物/作品/新闻语境时作为公开信息搜索。\n"
            )

        return (
            f"【当前时间】{now} GMT+8 / Asia/Shanghai\n\n"
            f"【当前触发消息】\n{current_line}\n\n"
            f"【本轮搜索目标】\n{target_lines}\n\n"
            f"【仅供消歧的历史背景】\n{chat_text}\n\n"
            f"{image_task}"
            "【联网搜索任务】\n"
            "请围绕【本轮搜索目标】判断是否需要联网搜索，并触发你的原生联网搜索能力。\n"
            "如果【当前触发消息】只是转交、召唤或追问机器人（例如“你来回答”“助手怎么看”“这个你说下”），"
            "则以【本轮搜索目标】中最近的实质问题或话题为准。\n"
            "历史背景只用于补全代词、省略主语、延续话题和地域语境；不要主动搜索历史里的无关旧话题。\n"
            "如果本轮搜索目标是成人向/擦边/福利姬/套图/视频资源类请求，搜索目标应转化为："
            "当前公开网络上相关热门关键词、热门账号/人物名称、平台讨论趋势、圈层语境、风险与合规提示；"
            "不要提供下载、购买、引流、破解、盗版或私密资源获取路径。\n"
            "如果本轮搜索目标仍然只是闲聊、玩笑、表情或没有实质信息需求，请返回 status=no_result。"
            "但只要本轮包含图片，就必须优先适用【图片搜索强制要求】，不能因为文字像闲聊而返回 no_result。"
        )

    def _select_search_target_messages(self, cleaned_context: List[Dict]) -> List[Dict]:
        """Select the current search target without letting old topics dominate."""
        if not cleaned_context:
            return []

        current = cleaned_context[-1]
        current_content = str(current.get("content", "") or "").strip()
        if not self._looks_like_handoff_message(current_content):
            return [current]

        target_messages: List[Dict] = []
        for msg in reversed(cleaned_context[:-1]):
            content = str(msg.get("content", "") or "").strip()
            if not content or self._looks_like_low_value_message(content):
                continue
            target_messages.append(msg)
            if len(target_messages) >= 3:
                break

        target_messages.reverse()
        return target_messages or [current]

    def _dedupe_adjacent_messages(self, messages: List[Dict]) -> List[Dict]:
        """Remove adjacent duplicate sender/content pairs introduced by trigger replay."""
        deduped: List[Dict] = []
        for msg in messages:
            sender = str(msg.get("sender", ""))
            content = str(msg.get("content", "")).strip()
            if deduped:
                prev = deduped[-1]
                if str(prev.get("sender", "")) == sender and str(prev.get("content", "")).strip() == content:
                    continue
            deduped.append(msg)
        return deduped

    def _looks_like_handoff_message(self, content: str) -> bool:
        if not content:
            return False
        text = re.sub(r"\s+", "", content)
        patterns = (
            r"(你|机器人|助手).{0,8}(来)?(回答|答下|讲下|说下|评价|点评|怎么看|点睇|点讲)",
            r"(回答|答下|讲下|说下|评价|点评|怎么看|点睇|点讲).{0,8}(你|机器人|助手)",
            r"(交给|轮到|该).{0,4}(你|机器人|助手)",
            r"^(这个|这事|这件事|上面|前面|刚才).{0,6}(你怎么看|怎么看|点睇|怎么说|点讲)$",
        )
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)

    def _looks_like_low_value_message(self, content: str) -> bool:
        text = re.sub(r"\s+", "", content)
        if len(text) <= 1:
            return True
        low_value_patterns = (
            r"^(哈哈+|呵呵+|笑死|牛逼|可以|好的|好|嗯+|啊+|哦+|对|不是|没错|确实)$",
            r"^[😂🤣😅😊🙂😉👍👌🙏]+$",
        )
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in low_value_patterns)

    def _normalize_search_response(self, response: str) -> str:
        """Strip common markdown wrappers and collapse no_result to a clean marker."""
        content = (response or "").strip()
        if not content:
            return ""

        cleaned = self._strip_response_wrappers(content)
        data = self._try_parse_json_object(cleaned)
        if not data:
            return cleaned

        status = str(data.get("status", "")).strip().lower()
        if status in {"no_result", "none", "无结果"}:
            return "无结果"
        return json.dumps(data, ensure_ascii=False)

    def _strip_response_wrappers(self, content: str) -> str:
        text = content.strip()
        if text.startswith("**") and text.endswith("**") and len(text) >= 4:
            text = text[2:-2].strip()
        fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S | re.I)
        if fence_match:
            text = fence_match.group(1).strip()
        return text

    def _try_parse_json_object(self, content: str) -> Optional[Dict]:
        text = content.strip()
        if not text:
            return None
        candidates = [text]
        if "{" in text and "}" in text:
            candidates.append(text[text.find("{"):text.rfind("}") + 1])
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except Exception:
                continue
            if isinstance(data, dict):
                return data
        return None
    
    def _get_unified_search_prompt(self) -> str:
        """获取统一的搜索系统提示词"""
        prompt = self.search_prompt or ""
        addendum = (
            "\n\n## 当前搜索目标优先级补充（高优先级）\n"
            "1. 以上规则中的“玩笑内容/闲聊无结果”不能机械套用。"
            "如果【本轮搜索目标】包含可搜索实体、网络黑话、成人向/擦边内容、福利姬、Cosplay套图、视频资源、热门账号、热门关键词等，"
            "仍应触发联网搜索，整理公开网络信息、热度、相关关键词、平台趋势和语境含义。\n"
            "2. 不要仅因为语气像调侃、成人向、擦边或群聊玩笑就返回 no_result。"
            "成人/擦边目标应输出 status=search_completed，并提供公开可搜索的热度、关键词、人物/账号名称、平台趋势、语境含义和风险提示。"
            "只有在本轮搜索目标完全没有可搜索对象、没有信息需求、也无法从上下文补全时，才返回 no_result。\n"
            "3. 如果用户请求带有图片，图片本身就是搜索目标。"
            "必须先识别图片中可见文字、Logo、品牌、商品、车型、游戏/IP、截图平台、地点、事件线索或公开人物语境，"
            "再把这些视觉线索转成搜索查询并调用搜索工具。"
            "不得因为用户只问“好看吗”“这是什么”“怎么看”或图片像闲聊素材就返回 no_result；"
            "带图请求应输出 status=search_completed，除非已经围绕图片内容搜索且明确没有可用公开结果。\n"
            "4. 如涉及违法、未成年人、非自愿泄露隐私或明显侵权资源，只整理风险提示、公开背景和安全替代信息，不要提供违法获取路径。"
        )
        return prompt + addendum
    
    def _get_default_search_prompt(self) -> str:
        """获取默认搜索提示词"""
        return """你是一个强力联网搜索助手，专门为聊天机器人提供实时搜索参考信息。

核心规则（必须遵守）：
1. 你【必须】调用搜索工具执行真实网络搜索，不得仅凭自身训练数据回答。
2. 搜索关键词要精准、多角度且具有上下文感知能力。如果用户消息简短或具有指代性，请结合对话历史补全搜索词（例如：前文在聊北京天气，用户说“上海呢？”，你应该搜索“上海天气”）。
3. 即使搜索结果不完整，也要把找到的内容如实整理后返回，切勿因"信源不足"就放弃。
4. **只有当搜索工具明确返回无任何相关结果时**，才可以写"暂无相关结果"。

输出格式要求：
- **绝对禁止**返回任何来源引用链接或 `[1]` `[2](https...)` 这类文献标注，只保留纯文本事实
- 内容要具体：包含关键事实、数据、时间线、主要观点
- 你提供的是供机器人参考的背景资料，内容越详实越好"""
