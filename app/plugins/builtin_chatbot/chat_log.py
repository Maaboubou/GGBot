"""
聊天记录管理器
"""

import os
import json
import time
import logging
import threading
import uuid
from itertools import islice
from typing import List, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)

# ChatLogManager is instantiated by more than one plugin.  Keep the counter
# read-modify-write transaction shared across all instances in this process.
_COUNTS_LOCK = threading.RLock()
_COUNT_HIGH_WATER: Dict[str, int] = {}


class ChatLogManager:
    """聊天记录管理器"""

    INTERNAL_ACTION_MARKERS = {"[发送文件]", "[文件回复]"}

    def __init__(self):
        self.log_dir = Path("data/chat_logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.counts_path = Path("data/chat_log_counts.json")
        logger.info(f"📝 ChatLogManager初始化，日志目录: {self.log_dir}")

    def _load_counts(self) -> Dict[str, int]:
        with _COUNTS_LOCK:
            if not self.counts_path.exists():
                return dict(_COUNT_HIGH_WATER)
            try:
                with open(self.counts_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("聊天累计计数文件不是 JSON object")

                counts = {str(k): max(0, int(v)) for k, v in data.items()}
                # A transiently stale file must never move an in-process
                # cumulative count backwards.
                for chat_name, count in _COUNT_HIGH_WATER.items():
                    counts[chat_name] = max(counts.get(chat_name, 0), count)
                _COUNT_HIGH_WATER.update(counts)
                return counts
            except Exception as e:
                logger.warning(
                    f"⚠️ 读取聊天累计计数失败，将使用进程内高水位和日志行数兜底: {e}"
                )
                return dict(_COUNT_HIGH_WATER)

    def _save_counts(self, counts: Dict[str, int]) -> None:
        with _COUNTS_LOCK:
            temp_path = None
            try:
                normalized = {
                    str(chat_name): max(0, int(count))
                    for chat_name, count in counts.items()
                }
                self.counts_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = self.counts_path.with_name(
                    f"{self.counts_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                )
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(normalized, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, self.counts_path)
                _COUNT_HIGH_WATER.update(normalized)
            except Exception as e:
                logger.warning(f"⚠️ 保存聊天累计计数失败: {e}")
            finally:
                if temp_path is not None:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except Exception:
                        pass

    def _count_log_lines(self, chat_name: str) -> int:
        log_path = self.log_dir / f"{chat_name}.jsonl"
        if not log_path.exists():
            return 0
        try:
            with open(log_path, "rb") as f:
                return sum(1 for line in f if line.strip())
        except Exception as e:
            logger.error(f"❌ 统计聊天记录行数失败: {e}")
            return 0

    def _message_count_floor(self, chat_name: str) -> int:
        with _COUNTS_LOCK:
            counts = self._load_counts()
            floor = max(
                int(counts.get(chat_name, 0) or 0),
                int(_COUNT_HIGH_WATER.get(chat_name, 0) or 0),
                self._count_log_lines(chat_name),
            )
            _COUNT_HIGH_WATER[chat_name] = floor
            return floor

    def _increment_message_count(self, chat_name: str) -> int:
        with _COUNTS_LOCK:
            counts = self._load_counts()
            # save_message appends the new JSONL row before incrementing the
            # durable counter, so the physical-line fallback already includes
            # this message.
            previous_log_lines = max(0, self._count_log_lines(chat_name) - 1)
            current_count = max(
                int(counts.get(chat_name, 0) or 0),
                int(_COUNT_HIGH_WATER.get(chat_name, 0) or 0),
                previous_log_lines,
            )
            next_count = current_count + 1
            counts[chat_name] = next_count
            _COUNT_HIGH_WATER[chat_name] = next_count
            self._save_counts(counts)
            return next_count

    def ensure_minimum_count(self, chat_name: str, minimum: int) -> int:
        """Persist a known cumulative-count floor without decrementing it."""
        with _COUNTS_LOCK:
            counts = self._load_counts()
            current_count = max(
                int(counts.get(chat_name, 0) or 0),
                int(_COUNT_HIGH_WATER.get(chat_name, 0) or 0),
                self._count_log_lines(chat_name),
            )
            repaired_count = max(current_count, max(0, int(minimum or 0)))
            _COUNT_HIGH_WATER[chat_name] = repaired_count
            if int(counts.get(chat_name, 0) or 0) < repaired_count:
                counts[chat_name] = repaired_count
                self._save_counts(counts)
            return repaired_count

    def save_message(
        self,
        chat_name: str,
        sender: str,
        content: str,
        *,
        sender_id: str = "",
        sender_remark: str = "",
        is_bot: bool = False,
    ) -> None:
        """保存聊天消息到 jsonl 文件"""
        log_path = self.log_dir / f"{chat_name}.jsonl"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        with _COUNTS_LOCK:
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    row = {
                        "time": timestamp,
                        "sender": sender,
                        "content": content
                    }
                    if str(sender_id or "").strip():
                        row["sender_id"] = str(sender_id).strip()
                    if str(sender_remark or "").strip():
                        row["sender_remark"] = str(sender_remark).strip()
                    if is_bot:
                        row["is_bot"] = True
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

                self._increment_message_count(chat_name)
                logger.debug(f"📝 保存聊天记录成功: {chat_name} - {sender}")
            except Exception as e:
                logger.error(f"❌ 保存聊天记录失败: {e}")

    def _is_internal_action_message(self, message: Dict[str, Any]) -> bool:
        """Return whether a legacy log row is an internal action, not chat text."""
        content = str(message.get("content") or "").strip()
        return content in self.INTERNAL_ACTION_MARKERS

    def remove_emoji_from_last_message(self, chat_name: str, emoji: str = " ⛓️💥") -> None:
        """剥离最新一条记录中附加的特定表情符号（用于清理搜索失败等显示用的临时标记）"""
        log_path = self.log_dir / f"{chat_name}.jsonl"
        if not log_path.exists():
            return

        try:
            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            if not lines:
                return

            last_line = lines[-1].strip()
            if not last_line:
                return

            msg_data = json.loads(last_line)
            content = msg_data.get("content", "")

            if content.endswith(emoji):
                msg_data["content"] = content[:-len(emoji)]
                lines[-1] = json.dumps(msg_data, ensure_ascii=False) + "\n"

                with open(log_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                logger.debug(f"📝 成功清理聊天记录中的表情标记: {chat_name}")

        except Exception as e:
            logger.error(f"❌ 清理聊天记录表情失败: {e}")

    def get_context_messages(self, chat_name: str, limit: int = 100) -> List[Dict]:
        """获取上下文消息"""
        log_path = self.log_dir / f"{chat_name}.jsonl"

        if not log_path.exists():
            logger.debug(f"📝 聊天记录文件不存在: {chat_name}")
            return []

        try:
            # 尝试多种编码方式读取文件
            encodings = ['utf-8', 'gbk', 'gb2312', 'cp936']
            content = None

            for encoding in encodings:
                try:
                    with open(log_path, encoding=encoding) as f:
                        content = f.readlines()[-limit:]
                        break
                except (UnicodeDecodeError, LookupError):
                    continue

            if content is None:
                # 如果所有编码都失败，使用错误替换模式
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    content = f.readlines()[-limit:]
                    logger.warning(f"⚠️ 使用错误替换模式读取聊天记录: {chat_name}")

            # 解析 JSON，跳过损坏的行
            messages = []
            for line in content:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                    if self._is_internal_action_message(message):
                        continue
                    messages.append(message)
                except json.JSONDecodeError as e:
                    logger.warning(f"⚠️ 跳过损坏的聊天记录行: {e}")
                    continue

            logger.debug(f"📝 获取聊天上下文成功: {chat_name}, 消息数: {len(messages)}")
            return messages

        except Exception as e:
            logger.error(f"❌ 读取聊天记录失败: {e}")
            return []

    def count_log_messages(self, chat_name: str) -> int:
        """Return the physical JSONL cursor, independent of cumulative counters."""
        return self._count_log_lines(chat_name)

    def get_messages_range(
        self,
        chat_name: str,
        *,
        start_cursor: int,
        end_cursor: int,
        limit: int,
    ) -> List[Dict]:
        """Read a contiguous physical-log range and annotate each row cursor."""
        log_path = self.log_dir / f"{chat_name}.jsonl"
        if not log_path.exists() or end_cursor <= start_cursor or limit <= 0:
            return []

        start = max(0, int(start_cursor or 0))
        end = max(start, int(end_cursor or 0))
        messages: List[Dict[str, Any]] = []
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for zero_index, line in enumerate(islice(f, start, end), start=start):
                    value = line.strip()
                    if not value:
                        continue
                    try:
                        message = json.loads(value)
                    except json.JSONDecodeError as e:
                        logger.warning(f"⚠️ 跳过损坏的聊天记录行: {e}")
                        continue
                    if self._is_internal_action_message(message):
                        continue
                    message["_log_cursor"] = zero_index + 1
                    messages.append(message)
                    if len(messages) >= limit:
                        break
            return messages
        except Exception as e:
            logger.error(f"❌ 按游标读取聊天记录失败: {e}")
            return []

    def count_messages(self, chat_name: str) -> int:
        """统计指定聊天的累计消息数，不受日志清理影响。"""
        return self._message_count_floor(chat_name)

    def format_context(self, messages: List[Dict]) -> str:
        """格式化上下文消息"""
        if not messages:
            return "（无历史消息）"

        formatted = []
        for msg in messages:
            if self._is_internal_action_message(msg):
                continue
            formatted.append(f"[{msg['sender']}]: {msg['content']}")

        context = "\n".join(formatted)
        logger.debug(f"📝 格式化上下文完成，长度: {len(context)}")
        return context

    def _sanitize_name(self, name: str) -> str:
        """将发送者名字转换为符合OpenAI规范的格式

        OpenAI API 对 name 字段的要求:
        - 只能包含字母、数字、下划线和连字符
        - 长度不超过64字符

        策略:
        1. 如果是纯英文/数字,直接使用
        2. 如果包含中文,转换为拼音
        3. 其他字符替换为下划线
        """
        import re

        # 检查是否包含中文字符
        has_chinese = bool(re.search(r'[\u4e00-\u9fff]', name))

        if has_chinese:
            try:
                # 尝试导入pypinyin进行拼音转换
                from pypinyin import lazy_pinyin, Style
                # 转换为拼音,首字母大写
                pinyin_parts = lazy_pinyin(name, style=Style.NORMAL)
                # 将拼音首字母大写并连接
                sanitized = ''.join([p.capitalize() for p in pinyin_parts])
            except ImportError:
                # 如果pypinyin未安装,使用hash作为后备方案
                import hashlib
                # 使用名字的hash值前8位作为唯一标识
                name_hash = hashlib.md5(name.encode()).hexdigest()[:8]
                sanitized = f"user_{name_hash}"
        else:
            # 移除非法字符,只保留字母数字下划线连字符
            sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', name)

        # 如果清理后为空,使用默认值
        if not sanitized or sanitized == '_' * len(sanitized):
            return "user"

        # 限制长度
        return sanitized[:64]

    def format_messages_array(self, messages: List[Dict], bot_name: str = None) -> List[Dict]:
        """将历史消息转换为OpenAI消息格式的数组

        Args:
            messages: 原始消息列表
            bot_name: 机器人名称，用于识别是否为助手消息
        """
        if not messages:
            return []

        formatted_messages = []
        for msg in messages:
            if self._is_internal_action_message(msg):
                continue
            sender = msg['sender']
            content = msg['content']

            if msg.get("is_bot") or (bot_name and sender == bot_name):
                # 机器人自己的消息 -> assistant
                formatted_messages.append({
                    "role": "assistant",
                    "content": content
                })
            else:
                # 用户的消息 -> user，并在内容前加名字以便区分
                formatted_messages.append({
                    "role": "user",
                    "name": self._sanitize_name(sender),  # 使用清理后的真实发送者名字
                    "content": f"[{sender}]: {content}"
                })

        logger.debug(f"📝 格式化消息数组完成，消息数: {len(formatted_messages)}")
        return formatted_messages

    def clear_chat_log(self, chat_name: str) -> bool:
        """清除指定聊天的记录"""
        log_path = self.log_dir / f"{chat_name}.jsonl"
        with _COUNTS_LOCK:
            try:
                if log_path.exists():
                    log_path.unlink()
                    counts = self._load_counts()
                    counts.pop(chat_name, None)
                    _COUNT_HIGH_WATER.pop(chat_name, None)
                    self._save_counts(counts)
                    logger.info(f"📝 清除聊天记录成功: {chat_name}")
                    return True
                else:
                    logger.warning(f"📝 聊天记录文件不存在: {chat_name}")
                    return False
            except Exception as e:
                logger.error(f"❌ 清除聊天记录失败: {e}")
                return False

    def get_chat_list(self) -> List[str]:
        """获取所有有聊天记录的聊天名称"""
        try:
            chat_files = list(self.log_dir.glob("*.jsonl"))
            chat_names = [f.stem for f in chat_files]
            logger.debug(f"📝 获取聊天列表: {len(chat_names)} 个聊天")
            return chat_names
        except Exception as e:
            logger.error(f"❌ 获取聊天列表失败: {e}")
            return []

    def cleanup_logs(self, max_days: int = None, max_size_mb: int = None) -> None:
        """清理过期的聊天记录

        Args:
            max_days: 保留的天数，None表示不限制
            max_size_mb: 单个文件最大大小(MB)，None表示不限制
        """
        import datetime

        now = datetime.datetime.now()
        logger.info(f"📝 开始清理聊天记录: max_days={max_days}, max_size_mb={max_size_mb}")

        count = 0
        for log_path in self.log_dir.glob("*.jsonl"):
            try:
                changed = False
                # 注意：对于极大的文件，readlines 可能会占用较多内存
                # 但考虑到 max_size_mb 默认 100MB，通常在可接受范围内
                if not log_path.exists():
                    continue

                with open(log_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                if not lines:
                    continue

                new_lines = []
                # 1. 按天数过滤
                if max_days is not None:
                    cutoff = now - datetime.timedelta(days=max_days)
                    for line in lines:
                        try:
                            msg = json.loads(line)
                            # 假设时间格式为 "YYYY-MM-DD HH:MM:SS"
                            msg_time = datetime.datetime.strptime(msg["time"], "%Y-%m-%d %H:%M:%S")
                            if msg_time >= cutoff:
                                new_lines.append(line)
                            else:
                                changed = True
                        except (json.JSONDecodeError, KeyError, ValueError):
                            # 格式不正确的行也清掉
                            changed = True
                else:
                    new_lines = lines

                # 2. 按大小过滤 (如果仍超过限制)
                if max_size_mb is not None and new_lines:
                    max_size_bytes = max_size_mb * 1024 * 1024
                    # 粗略估算字节数（包括换行符）
                    current_size = sum(len(line.encode('utf-8')) for line in new_lines)

                    if current_size > max_size_bytes:
                        # 从后往前保留消息，直到达到大小限制
                        truncated_lines = []
                        size_acc = 0
                        for line in reversed(new_lines):
                            line_size = len(line.encode('utf-8'))
                            if size_acc + line_size <= max_size_bytes:
                                truncated_lines.insert(0, line)
                                size_acc += line_size
                            else:
                                break
                        new_lines = truncated_lines
                        changed = True

                if changed:
                    with open(log_path, "w", encoding="utf-8") as f:
                        f.writelines(new_lines)
                    logger.info(f"    - {log_path.name}: 清理完成, 剩余 {len(new_lines)} 条记录")
                    count += 1

            except Exception as e:
                logger.error(f"❌ 清理日志文件出错 {log_path.name}: {e}")

        logger.info(f"✅ 聊天记录清理任务结束，共清理了 {count} 个文件")
