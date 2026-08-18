"""
微信管理器 - 封装与wxautox的交互
基于wxautox官方文档实现
"""

import logging
import os
import time
import threading
import uuid
from contextlib import contextmanager
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
import requests

from .event_bus import EventBus, get_event_bus, Event, EventType
from ..utils.health_state import ConsecutiveHealthGate

# --- wx_bot.py 的地址 ---
WX_BOT_PORT = os.getenv("WX_BOT_PORT", "5555").strip() or "5555"
WX_BOT_URL = os.getenv("WX_BOT_URL", "").strip() or f"http://127.0.0.1:{WX_BOT_PORT}"
LISTENER_API_TIMEOUT_SEC = 75
CONNECTION_MONITOR_INTERVAL_SEC = 5
CONNECTION_FAILURE_THRESHOLD = 3
TEXT_SEND_API_TIMEOUT_SEC = 30

@dataclass
class MessageInfo:
    """消息信息"""
    content: str
    sender: str
    chat_name: str
    chat_type: str  # user, group
    message_type: str  # text, image, link, quote, video, file, voice
    timestamp: float
    raw_message: Any = None
    sender_remark: Optional[str] = None


class WeChatManager:
    """
    微信管理器 - API客户端模式
    - 通过HTTP API与独立的wx_bot.py进程通信
    """
    _outbound_send_lock = threading.RLock()
    
    def __init__(self, event_bus: Optional[EventBus] = None):
        self.event_bus = event_bus or get_event_bus()
        self.logger = logging.getLogger(__name__)
        self._running = False
        self._listened_chats = {}
        self._stats = {
            'messages_received': 0,
            'messages_processed': 0,
            'events_published': 0,
            'last_message_time': None,
            'listened_chats_count': 0
        }
        # 连接监控
        self._connection_monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop_flag = False
        self._last_health: Dict[str, Any] = {
            'wechat_connected': False,
            'wechat_online': False,
            'timestamp': 0
        }
        self._last_reconnected_ts: float = 0.0

    def _post_outbound(self, endpoint: str, payload: Dict[str, Any], timeout: int) -> requests.Response:
        """
        wxautox 依赖单一微信窗口/焦点，所有真实发送动作必须串行化。
        这里不锁下载、监听等只读操作，只锁会操作微信窗口的出站发送 API。
        """
        with self._outbound_send_lock:
            self.logger.debug(
                "Acquired WeChat outbound send lock: endpoint=%s chat=%s",
                endpoint,
                payload.get("who") or payload.get("chat_name")
            )
            return requests.post(
                f"{WX_BOT_URL}{endpoint}",
                json=payload,
                timeout=timeout
            )

    @contextmanager
    def outbound_send_session(self):
        """把多段回复视为一次连续出站发送，避免中途被其他群切走窗口。"""
        with self._outbound_send_lock:
            yield
        
    def start(self) -> bool:
        """启动微信管理器并检查与wx_bot的连接"""
        self.logger.info("Starting WeChat manager (API client mode)...")
        self._last_health = self._get_health()
        if self._last_health.get('wechat_connected'):
            self.logger.info("✅ Successfully connected to wx_bot service.")
            self._running = True
            # 启动连接状态监控线程（无论是否连接都开启，便于后续自动恢复）
            self._start_connection_monitor()
            return True
        else:
            self.logger.error("❌ Failed to connect to wx_bot service. Please ensure wx_bot.py is running.")
            self._running = False
            # 即使当前未连接，也启动监控线程以便后续自动恢复
            self._start_connection_monitor()
            return False

    def stop(self) -> None:
        """停止微信管理器"""
        self.logger.info("Stopping WeChat manager (API client mode)...")
        self._running = False
        self._monitor_stop_flag = True
        if self._connection_monitor_thread and self._connection_monitor_thread.is_alive():
            self._connection_monitor_thread.join(timeout=5)
        self.logger.info("WeChat manager stopped.")

    def _check_wechat_connection(self) -> bool:
        """通过API检查与wx_bot的连接状态"""
        try:
            response = requests.get(f"{WX_BOT_URL}/health", timeout=3)
            response.raise_for_status()
            data = response.json()
            return data.get("status") == "ok" and data.get("wechat_connected", False)
        except requests.RequestException as e:
            self.logger.error(f"Connection check to wx_bot failed: {e}")
            return False

    def _get_health(self) -> Dict[str, Any]:
        """获取 wx_bot 健康状态详情（包含 online/connected）"""
        try:
            response = requests.get(f"{WX_BOT_URL}/health", timeout=3)
            response.raise_for_status()
            data = response.json()
            listeners = data.get('listeners') or {}
            self._sync_listened_chats_from_listener_status(listeners)
            return {
                'wechat_connected': bool(data.get('wechat_connected')),
                'wechat_online': bool(data.get('wechat_online')),
                'health_status': data.get('health_status', 'ok'),
                'listeners': listeners,
                'timestamp': data.get('timestamp', time.time())
            }
        except requests.RequestException:
            return {
                'wechat_connected': False,
                'wechat_online': False,
                'health_status': 'unavailable',
                'listeners': {},
                'timestamp': time.time()
            }

    def _sync_listened_chats_from_listener_status(self, listeners: Dict[str, Any]) -> None:
        """用 wx_bot 的 desired/actual 快照刷新本进程里的活跃监听视图。"""
        if not isinstance(listeners, dict):
            return
        if listeners.get('probe_skipped'):
            return

        desired = listeners.get('desired') or []
        actual = listeners.get('actual') or []
        if not isinstance(desired, list) or not isinstance(actual, list):
            return

        desired_set = set(str(name) for name in desired if name)
        actual_set = set(str(name) for name in actual if name)
        active_set = desired_set & actual_set

        now_ts = time.time()
        for chat_name in active_set:
            self._listened_chats.setdefault(chat_name, {
                'added_time': now_ts,
                'message_count': 0
            })

        for chat_name in list(self._listened_chats.keys()):
            if chat_name not in active_set:
                self._listened_chats.pop(chat_name, None)

        self._stats['listened_chats_count'] = len(self._listened_chats)

    def _start_connection_monitor(self) -> None:
        """启动后台连接监控；确认连续失败后才发布一次重连事件。"""
        if self._connection_monitor_thread and self._connection_monitor_thread.is_alive():
            return

        self._monitor_stop_flag = False

        def _loop():
            self.logger.info("Starting WeChat connection monitor thread...")
            self._last_health = self._get_health()
            health_gate = ConsecutiveHealthGate(
                self._last_health,
                failure_threshold=CONNECTION_FAILURE_THRESHOLD,
            )
            while not self._monitor_stop_flag:
                try:
                    current = self._get_health()
                    transition = health_gate.observe(current)
                    if not transition.accepted:
                        self.logger.debug(
                            "Ignoring transient wx_bot health failure %s/%s",
                            transition.consecutive_failures,
                            CONNECTION_FAILURE_THRESHOLD,
                        )
                        time.sleep(CONNECTION_MONITOR_INTERVAL_SEC)
                        continue

                    self._last_health = transition.confirmed
                    self._running = transition.healthy
                    if not transition.healthy:
                        if transition.became_unhealthy:
                            self.logger.warning(
                                "wx_bot health failed %s consecutive times; marking connection unavailable",
                                transition.consecutive_failures,
                            )
                        time.sleep(CONNECTION_MONITOR_INTERVAL_SEC)
                        continue

                    if transition.reconnected:
                        now_ts = time.time()
                        # 1分钟内避免重复恢复
                        if now_ts - self._last_reconnected_ts > 60:
                            self._last_reconnected_ts = now_ts
                            self.logger.info("🔄 检测到微信已确认恢复，发布一次监听器同步事件...")
                            try:
                                if self.event_bus:
                                    self.event_bus.publish(
                                        Event(
                                            type=EventType.WECHAT_RECONNECTED,
                                            source="wechat_manager",
                                            data={"timestamp": now_ts}
                                        )
                                    )
                            except Exception as e:
                                self.logger.error(f"发布微信恢复事件失败: {e}")

                    time.sleep(CONNECTION_MONITOR_INTERVAL_SEC)
                except Exception:
                    # 避免监控线程崩溃
                    time.sleep(CONNECTION_MONITOR_INTERVAL_SEC)

        self._connection_monitor_thread = threading.Thread(target=_loop, name="wechat_connection_monitor", daemon=True)
        self._connection_monitor_thread.start()

    def add_listen_chat(self, chat_name: str, exact: bool = False) -> bool:
        """添加监听聊天 - 通过API"""
        try:
            response = requests.post(
                f"{WX_BOT_URL}/api/add_listener",
                json={"who": chat_name},
                timeout=LISTENER_API_TIMEOUT_SEC
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self._listened_chats[chat_name] = {
                    'added_time': time.time(),
                    'message_count': 0
                }
                self._stats['listened_chats_count'] = len(self._listened_chats)
                self.logger.debug(f"✅ Successfully added listen chat via API: {chat_name}")
                return True
            else:
                self.logger.error(f"❌ Failed to add listen chat via API: {data.get('message')}")
                return False
        except requests.RequestException as e:
            self.logger.error(f"❌ Failed to call add_listener API: {e}")
            return False

    def remove_listen_chat(self, chat_name: str) -> bool:
        """移除监听聊天 - 通过API"""
        try:
            response = requests.post(
                f"{WX_BOT_URL}/api/remove_listener",
                json={"who": chat_name},
                timeout=5
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                if chat_name in self._listened_chats:
                    del self._listened_chats[chat_name]
                    self._stats['listened_chats_count'] = len(self._listened_chats)
                self.logger.debug(f"✅ Successfully removed listen chat via API: {chat_name}")
                return True
            else:
                self.logger.error(f"❌ Failed to remove listen chat via API: {data.get('message')}")
                return False
        except requests.RequestException as e:
            self.logger.error(f"❌ Failed to call remove_listener API: {e}")
            return False
    
    def send_message(self, chat_name: str, message: str, at_users: List[str] = None) -> bool:
        """发送消息 - 通过API"""
        if at_users:
            at_str = " ".join([f"@{user}" for user in at_users])
            message = f"{at_str} {message}"

        request_id = uuid.uuid4().hex
        try:
            response = self._post_outbound(
                "/api/send_message",
                {
                    "who": chat_name,
                    "message": message,
                    "request_id": request_id,
                },
                timeout=TEXT_SEND_API_TIMEOUT_SEC,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.logger.debug(
                    "Sent message to %s via API: request_id=%s route=%s attempts=%s message=%s...",
                    chat_name,
                    request_id,
                    data.get("route"),
                    data.get("attempt_count"),
                    message[:50],
                )
                # 增加处理消息计数：只有成功发送回复时才计数
                self._stats['messages_processed'] += 1
                return True
            else:
                self.logger.error(f"Failed to send message via API: {data.get('message')}")
                return False
        except requests.HTTPError as e:
            response_body = ""
            if e.response is not None:
                response_body = (e.response.text or "").strip()
                if len(response_body) > 1000:
                    response_body = response_body[:1000] + "...<truncated>"
            self.logger.error(
                "Failed to call send_message API: request_id=%s error=%s | response_body=%s",
                request_id,
                e,
                response_body or "<empty>"
            )
            return False
        except requests.ReadTimeout as e:
            # The Flask worker may still be running after the HTTP client stops
            # waiting. Do not retry here; request_id lets the server deduplicate
            # a deliberate status/retry workflow in the future.
            self.logger.error(
                "send_message API read timed out; delivery status unknown and client retry suppressed: "
                "request_id=%s chat=%s timeout=%ss error=%s",
                request_id,
                chat_name,
                TEXT_SEND_API_TIMEOUT_SEC,
                e,
            )
            return False
        except requests.RequestException as e:
            self.logger.error(
                "Failed to call send_message API: request_id=%s error=%s",
                request_id,
                e,
            )
            return False

    def quote_message(self, chat_name: str, message_id: str, message: str) -> bool:
        """引用指定的已缓存微信消息并回复。"""
        if not message_id:
            return False

        try:
            response = self._post_outbound(
                "/api/quote_message",
                {
                    "chat_name": chat_name,
                    "message_id": message_id,
                    "message": message,
                },
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.logger.debug(
                    "Quoted message in %s via API: message_id=%s",
                    chat_name,
                    message_id,
                )
                self._stats['messages_processed'] += 1
                return True

            self.logger.warning(
                "Failed to quote message via API; caller may fall back to plain send: %s",
                data.get("message"),
            )
            return False
        except requests.RequestException as e:
            response_body = ""
            if getattr(e, "response", None) is not None:
                response_body = (e.response.text or "").strip()
                if len(response_body) > 1000:
                    response_body = response_body[:1000] + "...<truncated>"
            self.logger.warning(
                "Failed to call quote_message API; caller may fall back to plain send: %s | response_body=%s",
                e,
                response_body or "<empty>",
            )
            return False

    # === 新增：原WXAUTOTEST.py中使用的方法 ===
    
    def send_files(self, chat_name: str, file_paths: List[str]) -> bool:
        """发送文件 - 通过API"""
        try:
            # 支持单个文件路径的兼容性
            if isinstance(file_paths, str):
                file_paths = [file_paths]
                
            response = self._post_outbound(
                "/api/send_files",
                {"who": chat_name, "file_paths": file_paths},
                timeout=600  # 周报/PDF 文件发送可能触发微信 UI 操作，保留长超时
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.logger.info(f"✅ Successfully sent {len(file_paths)} files to {chat_name}")
                return True
            else:
                self.logger.error(f"❌ Failed to send files via API: {data.get('message')}")
                return False
        except requests.RequestException as e:
            self.logger.error(f"❌ Failed to call send_files API: {e}")
            return False

    def send_url_card(self, chat_name: str, url: str, timeout: int = 30) -> Dict[str, Any]:
        """发送URL卡片 - 通过API"""
        try:
            response = self._post_outbound(
                "/api/send_url_card",
                {"who": chat_name, "url": url},
                timeout=timeout
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.logger.info(f"✅ Successfully sent URL card to {chat_name}")
                return {"success": True, "message": "URL card sent successfully"}
            else:
                error_msg = data.get('message', 'Unknown error')
                self.logger.error(f"❌ Failed to send URL card via API: {error_msg}")
                return {"success": False, "message": error_msg}
        except requests.RequestException as e:
            error_msg = f"API request failed: {e}"
            self.logger.error(f"❌ Failed to call send_url_card API: {e}")
            return {"success": False, "message": error_msg}



    def resolve_link_url(self, chat_name: str, message_id: str, timeout: int = 60) -> Optional[str]:
        """按需解析微信链接卡片真实URL。"""
        if not message_id:
            return None

        try:
            response = requests.post(
                f"{WX_BOT_URL}/api/resolve_link_url",
                json={"chat_name": chat_name, "message_id": message_id, "timeout": timeout},
                timeout=max(timeout + 15, 20)
            )
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "success":
                url = data.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    self.logger.info(f"✅ 成功解析链接卡片URL: {url}")
                    return url

            self.logger.error(f"❌ 链接卡片URL解析失败: {data.get('message')}")
            return None
        except requests.RequestException as e:
            self.logger.error(f"❌ Failed to call resolve_link_url API: {e}")
            return None

    def download_quote_image(self, chat_name: str, message_id: str = None) -> Optional[str]:
        """按需下载引用图片（带重试机制）"""
        # wx_bot 会合并相同 message_id 的并发请求。首次超时留足正常 UI
        # 下载时间，避免 5~10 秒的短超时制造仍在后台执行的重复请求。
        retry_configs = [
            {"attempt": 1, "timeout": 30, "description": "首次尝试"},
            {"attempt": 2, "timeout": 90, "description": "第二次重试"},
        ]

        last_exception = None

        for config in retry_configs:
            attempt = config["attempt"]
            timeout = config["timeout"]
            description = config["description"]

            try:
                self.logger.info(f"🖼️ {description} - 下载引用图片 (消息ID: {message_id}, 超时: {timeout}秒)")

                response = requests.post(
                    f"{WX_BOT_URL}/api/download_quote_image_on_demand",
                    json={"chat_name": chat_name, "message_id": message_id},
                    timeout=timeout
                )
                response.raise_for_status()
                data = response.json()

                if data.get("status") == "success":
                    file_path = data.get("file_path")
                    if file_path:
                        self.logger.info(f"✅ 成功下载引用图片 (第{attempt}次尝试): {file_path}")
                        return file_path
                    else:
                        self.logger.error(f"❌ 第{attempt}次尝试失败: API未返回文件路径")
                        last_exception = Exception("API未返回文件路径")
                        continue
                else:
                    error_msg = data.get('message', '未知错误')
                    self.logger.error(f"❌ 第{attempt}次尝试失败: {error_msg}")
                    last_exception = Exception(f"API返回错误: {error_msg}")

                    # 如果是服务器错误，等待后重试
                    if response.status_code >= 500:
                        if attempt < len(retry_configs):
                            time.sleep(1)
                    else:
                        # 客户端错误（如400、404）直接返回失败
                        break

            except requests.exceptions.Timeout as e:
                self.logger.warning(f"⏰ 第{attempt}次尝试超时 ({timeout}秒): {e}")
                last_exception = e
                # 超时后立即重试，因为可能是网络波动
                
            except requests.exceptions.ConnectionError as e:
                self.logger.warning(f"🔗 第{attempt}次尝试连接失败: {e}")
                last_exception = e
                if attempt < len(retry_configs):
                    time.sleep(1)

            except requests.RequestException as e:
                self.logger.error(f"❌ 第{attempt}次尝试网络请求失败: {e}")
                last_exception = e
                if attempt < len(retry_configs):
                    time.sleep(1)

        # 所有重试都失败了
        self.logger.error(f"❌ 引用图片下载失败 - 消息ID: {message_id}，已尝试{len(retry_configs)}次")
        if last_exception:
            self.logger.error(f"最后一次错误: {last_exception}")
        return None

    def download_image_message(self, chat_name: str, message_id: str) -> Optional[str]:
        """下载指定消息ID的图片消息（带重试机制）"""
        # 服务端会合并相同 message_id 的并发请求；这里只保留一次宽松重试。
        retry_configs = [
            {"attempt": 1, "timeout": 30, "description": "首次尝试"},
            {"attempt": 2, "timeout": 90, "description": "第二次重试"},
        ]

        last_exception = None

        for config in retry_configs:
            attempt = config["attempt"]
            timeout = config["timeout"]
            description = config["description"]

            try:
                self.logger.info(f"🖼️ {description} - 下载图片 (消息ID: {message_id}, 超时: {timeout}秒)")

                response = requests.post(
                    f"{WX_BOT_URL}/api/download_image_message",
                    json={"chat_name": chat_name, "message_id": message_id},
                    timeout=timeout
                )
                response.raise_for_status()
                data = response.json()

                if data.get("status") == "success":
                    file_path = data.get("file_path")
                    if file_path:
                        self.logger.info(f"✅ 成功下载图片消息 (第{attempt}次尝试): {file_path}")
                        return file_path
                    else:
                        self.logger.error(f"❌ 第{attempt}次尝试失败: API未返回文件路径")
                        last_exception = Exception("API未返回文件路径")
                        continue
                else:
                    error_msg = data.get('message', '未知错误')
                    self.logger.error(f"❌ 第{attempt}次尝试失败: {error_msg}")
                    last_exception = Exception(f"API返回错误: {error_msg}")

                    # 如果是服务器错误，等待后重试
                    if response.status_code >= 500:
                        self.logger.info(f"🔄 检测到服务器错误 (状态码: {response.status_code})，准备重试...")
                        if attempt < len(retry_configs):
                            time.sleep(1)  # 重试前等待1秒
                    else:
                        # 客户端错误（如400、404）直接返回失败
                        break

            except requests.exceptions.Timeout as e:
                self.logger.warning(f"⏰ 第{attempt}次尝试超时 ({timeout}秒): {e}")
                last_exception = e
                if attempt < len(retry_configs):
                    time.sleep(0.5)  # 超时后短暂等待再重试

            except requests.exceptions.ConnectionError as e:
                self.logger.warning(f"🔗 第{attempt}次尝试连接失败: {e}")
                last_exception = e
                if attempt < len(retry_configs):
                    time.sleep(1)  # 连接失败后等待1秒再重试

            except requests.RequestException as e:
                self.logger.error(f"❌ 第{attempt}次尝试网络请求失败: {e}")
                last_exception = e
                if attempt < len(retry_configs):
                    time.sleep(1)  # 网络错误后等待1秒再重试

        # 所有重试都失败了
        self.logger.error(f"❌ 图片下载失败 - 消息ID: {message_id}，已尝试{len(retry_configs)}次")
        if last_exception:
            self.logger.error(f"最后一次错误: {last_exception}")
        return None





    def get_chat_info(self, chat_name: str) -> Dict[str, Any]:
        """获取聊天信息 - 通过API（模拟chat.ChatInfo()）"""
        try:
            response = requests.post(
                f"{WX_BOT_URL}/api/get_chat_info",
                json={"who": chat_name},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                chat_info = data.get("chat_info", {})
                self.logger.debug(f"Successfully got chat info for {chat_name}")
                return chat_info
            else:
                self.logger.error(f"Failed to get chat info via API: {data.get('message')}")
                return {}
        except requests.RequestException as e:
            self.logger.error(f"Failed to call get_chat_info API: {e}")
            return {}

    def keep_running(self) -> None:
        """保持运行 - 通过API（模拟wx.KeepRunning()）"""
        try:
            response = requests.post(
                f"{WX_BOT_URL}/api/keep_running",
                json={},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.logger.info("✅ Successfully started KeepRunning mode")
            else:
                self.logger.error(f"Failed to start KeepRunning via API: {data.get('message')}")
        except requests.RequestException as e:
            self.logger.error(f"Failed to call keep_running API: {e}")

    def restart_wechat(self) -> bool:
        """重启微信连接 - 通过API"""
        try:
            self.logger.info("🔄 Requesting WeChat restart...")
            response = requests.post(
                f"{WX_BOT_URL}/api/restart_wechat",
                json={},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.logger.info("✅ WeChat restart request submitted successfully")
                return True
            else:
                self.logger.error(f"Failed to restart WeChat via API: {data.get('message')}")
                return False
        except requests.RequestException as e:
            self.logger.error(f"Failed to call restart_wechat API: {e}")
            return False

    # === 原有方法保持不变 ===
    
    def get_all_friends(self, keywords: str = None) -> List[Dict[str, Any]]:
        """获取所有好友 - 通过API"""
        try:
            params = {"keywords": keywords} if keywords else {}
            response = requests.get(f"{WX_BOT_URL}/api/get_friends", params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                return data.get("friends", [])
            else:
                self.logger.error(f"Failed to get friends list via API: {data.get('message')}")
                return []
        except requests.RequestException as e:
            self.logger.error(f"Failed to call get_friends API: {e}")
            return []
    
    def get_recent_groups(self) -> List[Dict[str, Any]]:
        """获取最近群聊列表 - 通过API"""
        try:
            response = requests.get(f"{WX_BOT_URL}/api/get_groups", timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                return data.get("groups", [])
            else:
                self.logger.error(f"Failed to get groups list via API: {data.get('message')}")
                return []
        except requests.RequestException as e:
            self.logger.error(f"Failed to call get_groups API: {e}")
            return []
    
    def get_current_chat_info(self) -> Dict[str, Any]:
        """获取当前聊天信息 - 通过API"""
        try:
            response = requests.get(f"{WX_BOT_URL}/api/get_current_chat", timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                return data.get("chat_info", {})
            else:
                self.logger.error(f"Failed to get current chat info via API: {data.get('message')}")
                return {}
        except requests.RequestException as e:
            self.logger.error(f"Failed to call get_current_chat API: {e}")
            return {}

    def get_my_info(self) -> Dict[str, Any]:
        """获取当前用户信息 - 通过API"""
        try:
            response = requests.get(f"{WX_BOT_URL}/api/get_my_info", timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                return data.get("info", {})
            else:
                self.logger.error(f"Failed to get my info via API: {data.get('message')}")
                return {}
        except requests.RequestException as e:
            self.logger.error(f"Failed to call get_my_info API: {e}")
            return {}
    
    def get_listened_chats(self) -> Dict[str, Dict[str, Any]]:
        """获取正在监听的聊天列表"""
        self.get_listener_status()
        return self._listened_chats.copy()

    def get_listener_status(self) -> Dict[str, Any]:
        """获取 wx_bot 侧期望监听、实际监听窗口和缺失项。"""
        try:
            response = requests.get(f"{WX_BOT_URL}/api/listeners/status", timeout=5)
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "success":
                self._sync_listened_chats_from_listener_status(data)
            return data
        except requests.RequestException as e:
            self.logger.warning(f"Failed to call listeners status API: {e}")
            return {
                "status": "error",
                "message": str(e),
                "desired": [],
                "actual": [],
                "missing": []
            }
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息；状态页使用监控线程缓存，不额外请求 wx_bot。"""
        cached_health = self.get_cached_health()
        listener_status = cached_health.get('listeners') or {}
        return {
            **self._stats,
            'connected': bool(cached_health.get('wechat_connected')),
            'running': self._running,
            'listened_chats': list(self._listened_chats.keys()),
            'listener_status': listener_status
        }
    
    def is_connected(self) -> bool:
        """检查是否连接"""
        return self._check_wechat_connection()

    def get_cached_health(self) -> Dict[str, Any]:
        """返回监控线程最近确认的健康状态，不发起新的桥接请求。"""
        return dict(self._last_health)

    def is_connected_cached(self) -> bool:
        """供健康检查等只读路径使用，避免探针反过来阻塞服务。"""
        return bool(self._last_health.get('wechat_connected'))
    
    def is_online(self) -> bool:
        """检查微信是否在线 - 通过API（模拟wx.IsOnline()）"""
        try:
            response = requests.get(
                f"{WX_BOT_URL}/api/is_online",
                timeout=5
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                online = data.get("online", False)
                self.logger.debug(f"WeChat online status: {online}")
                return online
            else:
                self.logger.warning(f"Failed to check online status via API: {data.get('message')}")
                return False
        except requests.RequestException as e:
            self.logger.warning(f"Failed to call is_online API: {e}")
            return False

    # === 兼容性方法 - 提供与原WXAUTOTEST.py相同的接口 ===
    
    def SendFiles(self, filepath: str, who: str) -> bool:
        """兼容性方法 - 发送文件（保持原有接口风格）"""
        return self.send_files(who, [filepath])

    def SendUrlCard(self, url: str, friends: str, timeout: int = 30) -> Dict[str, Any]:
        """兼容性方法 - 发送URL卡片（保持原有接口风格）"""
        return self.send_url_card(friends, url, timeout)

    def AddListenChat(self, user_name: str, callback=None) -> bool:
        """兼容性方法 - 添加监听聊天（保持原有接口风格）"""
        # 注意：callback参数在API模式下不适用，消息通过event_bus处理
        if callback:
            self.logger.warning("callback parameter is ignored in API mode. Messages are handled via event_bus.")
        return self.add_listen_chat(user_name)

    def KeepRunning(self) -> None:
        """兼容性方法 - 保持运行（保持原有接口风格）"""
        self.keep_running()
