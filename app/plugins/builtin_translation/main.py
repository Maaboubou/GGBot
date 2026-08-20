"""
智能翻译插件
从原系统的TranslationService迁移而来
"""

import re
import logging

from app.core.event_bus import Event, EventType
from app.services.llm_manager import get_llm_manager
from app.utils.plugin_config import get_config


logger = logging.getLogger(__name__)


class TranslationService:
    """翻译服务"""
    
    def __init__(self):
        self.llm_manager = get_llm_manager()
        self.logger = logger
        
        # 注意：enabled_chats 权限检查已移至 EventBus 统一管理
        plugin_name = "builtin_translation"
        self.prompt_translate = get_config("prompt_translate", plugin_name=plugin_name)
        
        # Unicode 表情符号范围的正则模式 - 完整版包含所有表情符号
        self._emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"  # 情感符号 (Emoticons)
            "\U0001F300-\U0001F5FF"  # 符号和象形文字 (Symbols & Pictographs)
            "\U0001F680-\U0001F6FF"  # 交通和地图符号 (Transport & Map Symbols)
            "\U0001F1E0-\U0001F1FF"  # 地区指示符号 (Flags - Regional Indicators)
            "\U00002500-\U00002BEF"  # 中日韩符号和标点、方框元素等
            "\U00002702-\U000027B0"  # 装饰符号 (Dingbats)
            "\U0000FE0F"  # 变体选择符-16 (VS16) 使字符显示为 Emoji
            "\U0000200D"  # 零宽度连接符 (ZWJ) 用于组合 Emoji，如家庭、彩虹旗等
            "\U0001F900-\U0001F9FF"  # 补充符号和象形文字 (Supplemental Symbols and Pictographs)
            "\U0001FA70-\U0001FAFF"  # 符号和象形文字扩展-A (Symbols and Pictographs Extended-A)
            "\U0001F3FB-\U0001F3FF"  # Emoji 肤色修饰符 (Emoji Modifier Fitzpartick Type)
            # 以下是一些较为零散但也被视为 Emoji 的字符
            "\U00002122"  # 商标 (Trade Mark)
            "\U00002300-\U000023FF" # 杂项技术符号
            "\U00002B50"  # 白色五角星
            "\U00002B55"  # 空心红色圆形
            "\U00002934-\U00002935" # 箭头
            "\U00002640-\U00002642" # 性别符号
            "\U00002600-\U000026FF" # 杂项符号
            "\U00003030"  # 波浪虚线
            "\U0000303D"  # Part Alternation Mark
            "\U00003297"  # Circled Ideograph "Congratulation"
            "\U00003299"  # Circled Ideograph "Secret"
            "\U0001F004"  # 麻将牌红中
            "\U0001F0CF"  # 扑克牌Joker
            # 键帽数字 (需要与变体选择符结合，但单独匹配数字也可)
            "\U00000030-\U00000039"
            "]+",
            re.UNICODE
        )
        
        # 微信表情标签白名单
        self._wechat_emoji_set = frozenset([
            "微笑", "撇嘴", "色", "发呆", "得意", "流泪", "害羞", "闭嘴", "睡", "大哭", "尴尬", "发怒", "调皮", "呲牙", 
            "惊讶", "难过", "囧", "抓狂", "吐", "偷笑", "愉快", "白眼", "傲慢", "困", "惊恐", "憨笑", "悠闲", "咒骂",
            "疑问", "嘘", "晕", "衰", "骷髅", "敲打", "再见", "擦汗", "抠鼻", "鼓掌", "坏笑", "右哼哼", "鄙视", "委屈",
            "快哭了", "阴险", "亲亲", "可怜", "笑脸", "生病", "脸红", "破涕为笑", "恐惧", "失望", "无语", "嘿哈",
            "捂脸", "奸笑", "机智", "皱眉", "耶", "吃瓜", "加油", "汗", "天啊", "Emm", "社会社会", "旺柴", "好的",
            "打脸", "哇", "翻白眼", "666", "让我看看", "叹气", "苦涩", "裂开", "嘴唇", "爱心", "心碎", "拥抱", "强",
            "弱", "握手", "胜利", "抱拳", "勾引", "拳头", "OK", "合十", "啤酒", "咖啡", "蛋糕", "玫瑰", "凋谢", 
            "菜刀", "炸弹", "便便", "月亮", "太阳", "庆祝", "礼物", "红包", "發", "福", "烟花", "爆竹", "猪头", 
            "跳跳", "发抖", "转圈"
        ])
        
        # 微信方括号表情的正则模式
        self._wechat_emoji_pattern = re.compile(r'\[([^\[\]\n]{1,8})\]')
    
    def _is_emoji_only(self, text: str) -> bool:
        """判断文本是否只包含 emoji 表情而没有实际文字内容"""
        if not text or not text.strip():
            return True
        
        working_text = text
        
        # 1. 移除所有 Unicode emoji 表情
        working_text = self._emoji_pattern.sub('', working_text)
        
        # 2. 移除微信方括号表情（仅白名单内的）
        def replace_wechat_emoji(match):
            inner_text = match.group(1)
            if inner_text in self._wechat_emoji_set:
                return ''  # 移除这个表情
            else:
                return match.group(0)  # 保留非表情的方括号内容
        
        working_text = self._wechat_emoji_pattern.sub(replace_wechat_emoji, working_text)
        
        # 3. 移除空白字符后检查是否还有内容
        cleaned_text = working_text.strip()
        
        # 如果移除所有表情后没有内容，说明原文只有表情
        return len(cleaned_text) == 0
    
    def _clean_quote_content(self, text: str) -> str:
        """临时处理：移除引用消息后缀 '引用 name 的消息 : content' """
        # 匹配 "引用 ... 的消息 : ..." 或 "引用 ... 的消息 ： ..." (兼顾中英文冒号)
        # non-greedy match for name, matches until end of string
        pattern = r"引用.*?的消息\s*[:：].*$"
        cleaned_text = re.sub(pattern, "", text, flags=re.DOTALL)
        return cleaned_text.strip()
    
    def translate_text(self, text: str) -> str:
        """三语互译：自动识别中文、英文和保加利亚语，翻译出另外两个语言"""
        try:
            system_prompt = self.prompt_translate
            
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ]
            
            response = self.llm_manager.call(
                plugin_name="builtin_translation",
                call_type="translate",
                messages=messages
            )
            return self._strip_markdown(response)
            
        except Exception as e:
            self.logger.error(f"三语互译失败: {e}")
            return "❌ 翻译失败"
    
    
    def _strip_markdown(self, text: str) -> str:
        """移除Markdown格式"""
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        text = re.sub(r"`([^`]*)`", r"\1", text)
        text = re.sub(r"(\*\*|\*|_|~~)(.*?)\1", r"\2", text)
        text = re.sub(r"^#+\s*", "", text, flags=re.M)
        return text.strip()


# 全局实例
translation_service = None

def handle_text_message(event: Event):
    """处理文本消息事件"""
    global translation_service
    
    try:
        logger.info(f"🔄 Translation plugin received event: {event.type}")
        
        message = event.data.get("message", "")
        chat_name = event.data.get("chat_name", "")
        sender = event.data.get("sender", "")
        
        logger.info(f"🔄 Event data - message: '{message}', chat: '{chat_name}', sender: '{sender}'")
        
        # 临时修复：处理引用消息被当作普通文本的问题
        # 如果消息包含引用后缀，将其移除
        cleaned_message = translation_service._clean_quote_content(message)
        if cleaned_message != message:
            logger.debug(f"✂️ Removed quote content. Original: '{message}' -> Cleaned: '{cleaned_message}'")
            message = cleaned_message
        
        # 检查是否只包含表情
        # 检查是否只包含表情
        if translation_service._is_emoji_only(message):
            logger.info(f"⏭️ Skipping emoji-only message from {sender}")
            return False
        
        # 注意：权限检查已移至 EventBus 统一管理，此处不再检查 enabled_chats
        
        logger.info(f"✅ Processing translation request from {sender} in {chat_name}")
        
        # 执行翻译
        translation = translation_service.translate_text(message)
        
        if translation and translation != "❌ 翻译失败":
            # 发送翻译结果
            wx = event.context.get("wx")
            if wx:
                # WeChatManager使用send_message方法
                result = wx.send_message(chat_name, translation)
                if result:
                    logger.info(f"Sent translation to {chat_name}")
                    return True
                else:
                    logger.error(f"Failed to send translation to {chat_name}")
            else:
                logger.error("WeChat manager not available in event context")
        return False
    except Exception as e:
        logger.error(f"Error handling text message for translation: {e}")
        return False


def register(event_bus, subscribe, context):
    """注册插件"""
    global translation_service
    
    logger.info("🔄 Registering translation plugin...")
    
    # 初始化翻译服务
    translation_service = TranslationService()
    context.health.register(lambda: {
        "status": "healthy" if translation_service is not None else "unhealthy",
        "message": "翻译服务已就绪" if translation_service is not None else "翻译服务未初始化",
    })
    context.register_cleanup(unregister)
    
    # 订阅文本消息事件
    subscribe(
        event_type=EventType.TEXT_MESSAGE_RECEIVED,
        handler=handle_text_message
    )
    
    # 订阅引用消息事件 - 现在图片是按需下载，不会影响翻译性能
    subscribe(
        event_type=EventType.QUOTE_MESSAGE_RECEIVED,
        handler=handle_text_message
    )
    
    
    logger.info("✅ Translation plugin registered successfully")


def unregister():
    """取消注册插件"""
    global translation_service
    
    logger.info("Unregistering translation plugin...")
    translation_service = None
    logger.info("Translation plugin unregistered")
