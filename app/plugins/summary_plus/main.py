"""
summary_plus 摘要插件
"""


import base64
import gzip
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import warnings
from urllib.parse import parse_qs, unquote, urlparse
from typing import Optional, Any, cast, List, Tuple, Dict, Set

import requests
from PIL import Image
from selenium import webdriver
from selenium.webdriver.remote.remote_connection import RemoteConnection

from app.core.event_bus import Event, EventType
from app.services.llm_manager import get_llm_manager
from app.services.email_service import get_email_service
from app.utils.plugin_config import get_config
from .asr_service import bili_transcribe_local
from .browser_service import browser_summarize, open_blank_worker_tab
from .mindmap_service import (
    MINDMAP_SYSTEM_PROMPT_DEFAULT,
    get_mindmap_skip_reason,
    is_mindmap_skip_response,
    render_mindmap_to_image,
    resolve_mindmap_layout,
    summarize_to_mindmap_json,
)
from .platform_service import handle_link_message as route_link_message
from .subtitle_service import bili_get_subtitles
from .yt_transcript import get_best_transcript_text
from .ytdlp_cookie_service import ytdlp_browser_cookie_args

logger = logging.getLogger(__name__)
# start.py 将 app.plugins 默认压到 WARNING；summary_plus 是长链路插件，
# 需要保留 INFO 级链路日志，便于定位“URL 已获取但没有下一步”的问题。
logger.setLevel(logging.INFO)


TIKHUB_ENDPOINT_FETCH_ONE = "https://api.tikhub.io/api/v1/douyin/app/v3/fetch_one_video_by_share_url"
TIKHUB_ENDPOINT_FETCH_ONE_WEB = "https://api.tikhub.io/api/v1/douyin/web/fetch_one_video_by_share_url"
TIKHUB_ENDPOINT_TIKTOK_FETCH_ONE = "https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_one_video_by_share_url"
TIKHUB_ENDPOINT_XHS_IMAGE_NOTE = "https://api.tikhub.io/api/v1/xiaohongshu/app_v2/get_image_note_detail"
TIKHUB_ENDPOINT_XHS_VIDEO_NOTE = "https://api.tikhub.io/api/v1/xiaohongshu/app_v2/get_video_note_detail"


class SummaryService:
    """摘要服务（复用 builtin_summary 的浏览器摘要能力，并增强抖音解析）"""

    # 时间常量 (秒)
    WINDOW_HANDLE_STABILIZE_DELAY = 0.5  # 窗口句柄稳定等待时间
    RETRY_DELAY = 0.25  # 重试间隔
    PAGE_CONTENT_STABILIZE_DELAY = 1.5  # 页面内容稳定等待时间

    # 内容限制
    MAX_CONTENT_LENGTH = 20000  # 最大内容长度(字符)

    def __init__(self):
        self.llm_manager = get_llm_manager()
        self.logger = logger

        # Load config
        plugin_name = "summary_plus"
        # Chrome settings
        self.chrome_debug_port = int(get_config("chrome_debug_port", plugin_name=plugin_name))
        self.chrome_path = get_config("chrome_path", plugin_name=plugin_name)
        self.chrome_user_data_dir = get_config("chrome_user_data_dir", plugin_name=plugin_name, default="tmp/chrome_data")
        self.chrome_profile_dir = get_config("chrome_profile_dir", plugin_name=plugin_name)
        self.page_load_timeout = int(get_config("page_load_timeout", plugin_name=plugin_name))
        self.webdriver_command_timeout_sec = int(
            get_config("webdriver_command_timeout_sec", plugin_name=plugin_name, default=8)
        )
        # Translation settings
        self.special_translation_groups = set(get_config("special_translation_groups", plugin_name=plugin_name) or [])
        self.special_translation_target_language = str(get_config("special_translation_target_language", plugin_name=plugin_name) or "English")
        self.domain_blacklist = [d.lower() for d in (get_config("domain_blacklist", plugin_name=plugin_name) or [])]
        self.sender_blacklist = set(
            s.lower().strip()
            for s in (get_config("sender_blacklist", plugin_name=plugin_name) or [])
            if isinstance(s, str) and s.strip()
        )

        # Prompts
        self.prompt_summary = get_config("prompt_summary", plugin_name=plugin_name)

        # WSL 探测
        self.is_wsl = self._check_is_wsl()
        if self.is_wsl:
            self.logger.info("🐧 检测到 WSL 环境，将启用 Windows 互操作模式")

        self.prompt_bilibili_mindmap = str(
            get_config(
                "prompt_bilibili_mindmap",
                plugin_name=plugin_name,
                default=MINDMAP_SYSTEM_PROMPT_DEFAULT,
            )
            or MINDMAP_SYSTEM_PROMPT_DEFAULT
        )
        self.prompt_youtube_mindmap = str(
            get_config(
                "prompt_youtube_mindmap",
                plugin_name=plugin_name,
                default=MINDMAP_SYSTEM_PROMPT_DEFAULT,
            )
            or MINDMAP_SYSTEM_PROMPT_DEFAULT
        )
        self.mindmap_layout = str(
            get_config("mindmap_layout", plugin_name=plugin_name, default="vertical") or "vertical"
        ).strip().lower()

        # Danmaku settings
        self.danmaku_font_size = int(get_config("danmaku_font_size", plugin_name=plugin_name, default=50))
        self.danmaku_line_spacing = float(get_config("danmaku_line_spacing", plugin_name=plugin_name, default=1.2))
        self.danmaku_display_region_ratio = float(get_config("danmaku_display_region_ratio", plugin_name=plugin_name, default=0.8))
        self.danmaku_limit_window_seconds = float(get_config("danmaku_limit_window_seconds", plugin_name=plugin_name, default=5))
        self.danmaku_max_per_window = int(get_config("danmaku_max_per_window", plugin_name=plugin_name, default=20))
        self.bilibili_danmaku_webmask_enabled = bool(get_config("bilibili_danmaku_webmask_enabled", plugin_name=plugin_name, default=True))
        self.bilibili_video_crf = int(get_config("bilibili_video_crf", plugin_name=plugin_name, default=20))
        self.bilibili_max_download_duration = int(get_config("bilibili_max_download_duration", plugin_name=plugin_name, default=120))
        self.ffmpeg_bin = self._resolve_media_tool(
            "ffmpeg",
            plugin_name=plugin_name,
            configured_path=str(get_config("ffmpeg_path", plugin_name=plugin_name, default="") or ""),
        )
        self.ffprobe_bin = self._resolve_media_tool(
            "ffprobe",
            plugin_name=plugin_name,
            configured_path=str(get_config("ffprobe_path", plugin_name=plugin_name, default="") or ""),
        )
        self.yt_dlp_bin = self._resolve_ytdlp_tool()
        self.logger.info(f"🎞️ FFmpeg: {self.ffmpeg_bin}")
        self.logger.info(f"🎞️ FFprobe: {self.ffprobe_bin}")
        self.logger.info(f"📥 yt-dlp: {self.yt_dlp_bin}")

        # Local ASR settings
        self.local_asr_enabled = bool(
            get_config("local_asr_enabled", plugin_name=plugin_name, default=True)
        )
        self.local_asr_max_duration_minutes = max(
            1,
            int(
                get_config(
                    "local_asr_max_duration_minutes",
                    plugin_name=plugin_name,
                    default=35,
                )
            ),
        )
        self.local_asr_timeout_seconds = max(
            30,
            int(
                get_config(
                    "local_asr_timeout_seconds",
                    plugin_name=plugin_name,
                    default=600,
                )
            ),
        )
        def resolve_local_asr_path(config_key: str, default_path: str) -> str:
            configured = str(
                get_config(config_key, plugin_name=plugin_name, default=default_path)
                or default_path
            ).strip()
            configured = os.path.expandvars(os.path.expanduser(configured))
            return configured if os.path.isabs(configured) else os.path.abspath(configured)

        self.local_asr_runtime_path = resolve_local_asr_path(
            "local_asr_runtime_path",
            os.path.join(
                "data", "models", "sensevoice", "llama-funasr-sensevoice.exe"
            ),
        )
        self.local_asr_model_path = resolve_local_asr_path(
            "local_asr_model_path",
            os.path.join(
                "data", "models", "sensevoice", "sensevoice-small-f32.gguf"
            ),
        )
        self.local_asr_vad_path = resolve_local_asr_path(
            "local_asr_vad_path",
            os.path.join("data", "models", "sensevoice", "fsmn-vad.gguf"),
        )
        if self.local_asr_enabled:
            missing_asr_resources = [
                path
                for path in (
                    self.local_asr_runtime_path,
                    self.local_asr_model_path,
                    self.local_asr_vad_path,
                )
                if not os.path.isfile(path)
            ]
            if missing_asr_resources:
                self.logger.error(
                    "❌ 本地 ASR 已启用，但资源不存在: %s",
                    ", ".join(missing_asr_resources),
                )
            else:
                self.logger.info(
                    "🎙️ 本地 ASR 已启用: SenseVoice F32 / CPU AVX2, 最大时长=%s 分钟",
                    self.local_asr_max_duration_minutes,
                )

        # XHS settings
        self.xhs_max_download_duration = int(
            get_config("xhs_max_download_duration", plugin_name=plugin_name, default=120)
        )
        self.xhs_max_images = int(get_config("xhs_max_images", plugin_name=plugin_name, default=9))

        # YouTube settings
        self.yt_transcript_proxy = get_config("yt_transcript_proxy", plugin_name=plugin_name, default="")
        self.yt_transcript_local_port = int(get_config("yt_transcript_local_port", plugin_name=plugin_name, default=7897))

        # Bilibili settings
        self.bilibili_burn_danmu = bool(get_config("bilibili_burn_danmu", plugin_name=plugin_name, default=True))
        self.bili_cookie_email_alert_enabled = bool(
            get_config("bili_cookie_email_alert_enabled", plugin_name=plugin_name, default=True)
        )
        self.bili_cookie_alert_cooldown_sec = int(
            get_config("bili_cookie_alert_cooldown_sec", plugin_name=plugin_name, default=21600)
        )
        self._bili_cookie_alert_last_ts = 0.0

        # 加载本地 JS 依赖
        self._load_local_assets()

        # WebDriver 单例管理
        self.driver: Optional[webdriver.Chrome] = None
        self.driver_lock = threading.RLock()
        with self.driver_lock:
            self._init_webdriver()

    # -------------------------
    # 资源管理
    # -------------------------
    def _load_local_assets(self):
        """预加载本地 JS 依赖以供 HTML 模板使用"""
        assets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
        self.js_d3 = ""
        self.js_markmap_lib = ""
        self.js_markmap_view = ""

        try:
            d3_path = os.path.join(assets_dir, "d3.min.js")
            lib_path = os.path.join(assets_dir, "markmap-lib.js")
            view_path = os.path.join(assets_dir, "markmap-view.js")

            if os.path.exists(d3_path):
                with open(d3_path, 'r', encoding='utf-8') as f: self.js_d3 = f.read()
            if os.path.exists(lib_path):
                with open(lib_path, 'r', encoding='utf-8') as f: self.js_markmap_lib = f.read()
            if os.path.exists(view_path):
                with open(view_path, 'r', encoding='utf-8') as f: self.js_markmap_view = f.read()

            self.logger.info(f"📦 已加载本地 JS 资源 (D3: {len(self.js_d3)}, Lib: {len(self.js_markmap_lib)}, View: {len(self.js_markmap_view)})")
        except Exception as e:
            self.logger.error(f"❌ 加载本地资源失败: {e}")

    # -------------------------
    # Chrome / WebDriver 管理
    # -------------------------
    def _set_webdriver_command_timeout(self, driver: Optional[webdriver.Chrome] = None) -> None:
        """降低 Selenium HTTP 命令超时，避免 driver.title 卡住 120 秒。"""
        timeout = max(3, int(self.webdriver_command_timeout_sec or 8))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                RemoteConnection.set_timeout(timeout)
        except Exception as e:
            self.logger.debug(f"设置 Selenium 全局命令超时失败: {e}")

        if not driver:
            return
        try:
            executor = getattr(driver, "command_executor", None)
            client_config = getattr(executor, "_client_config", None)
            if client_config is not None:
                client_config.timeout = timeout
        except Exception as e:
            self.logger.debug(f"设置当前 WebDriver 命令超时失败: {e}")

    def _get_driver_service_pid(self, driver: Optional[webdriver.Chrome]) -> Optional[int]:
        try:
            service = getattr(driver, "service", None)
            process = getattr(service, "process", None)
            pid = getattr(process, "pid", None)
            return int(pid) if pid else None
        except Exception:
            return None

    def _terminate_process_by_pid(self, pid: Optional[int], label: str = "process") -> None:
        if not pid:
            return
        try:
            if self.is_wsl:
                cmd = ["taskkill.exe", "/F", "/PID", str(pid)]
            elif os.name == "nt":
                cmd = ["taskkill", "/F", "/PID", str(pid)]
            else:
                cmd = ["kill", "-9", str(pid)]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            if result.returncode == 0:
                self.logger.info(f"✅ 已终止 {label}: PID={pid}")
            else:
                stderr = (result.stderr or result.stdout or "").strip()
                self.logger.warning(f"⚠️ 终止 {label} 失败: PID={pid} {stderr}")
        except Exception as e:
            self.logger.warning(f"⚠️ 终止 {label} 异常: PID={pid} {e}")

    def _quit_webdriver(self, driver: Optional[webdriver.Chrome], reason: str = "") -> None:
        if not driver:
            return
        service_pid = self._get_driver_service_pid(driver)
        try:
            self._set_webdriver_command_timeout(driver)
            driver.quit()
            self.logger.info(f"✅ WebDriver 已关闭{f' ({reason})' if reason else ''}")
        except Exception as e:
            self.logger.warning(f"⚠️ WebDriver quit 失败{f' ({reason})' if reason else ''}: {e}")
        finally:
            self._terminate_process_by_pid(service_pid, "chromedriver")

    def _validate_webdriver_connection(self, driver: webdriver.Chrome) -> None:
        """用浏览器级 CDP 命令验证连接，避免在当前标签页执行 JS 时卡死。"""
        version = driver.execute_cdp_cmd("Browser.getVersion", {})
        browser = version.get("product") or version.get("Browser") or "unknown"
        self.logger.info(f"✅ WebDriver CDP 连接验证成功: {browser}")

    def _init_webdriver(self):
        """初始化 WebDriver 实例 (假定锁已被外部获取)"""
        if self.driver:
            self.logger.info("ℹ️ WebDriver 实例已存在，跳过初始化")
            return
        try:
            self.driver = self._get_webdriver_instance()
            if self.driver:
                self.logger.info("✅ WebDriver 初始化成功")
            else:
                self.logger.error("❌ WebDriver 初始化失败，driver 为 None")
        except Exception as e:
            self.logger.error(f"❌ WebDriver 初始化异常: {e}", exc_info=True)
            self.driver = None

    def _check_is_wsl(self) -> bool:
        """检查是否在 WSL 环境中运行"""
        try:
            if os.name == "nt":
                return False
            with open("/proc/version", "r") as f:
                return "microsoft" in f.read().lower()
        except:
            return False

    def _get_webdriver_instance(self) -> Optional[webdriver.Chrome]:
        """获取 WebDriver 实例 - 封装创建和连接逻辑"""
        try:
            self.logger.info(f"🔍 检查调试端口 {self.chrome_debug_port} 是否可用")
            if not self._is_debug_port_ready():
                self.logger.info("🚀 调试端口不可用，将启动新的 Chrome 实例")
                self._terminate_chrome_process() # 确保端口彻底释放
                self._start_chrome_debug()
            else:
                self.logger.info("✅ 调试端口已可用，将尝试连接现有 Chrome 实例")

            opts = webdriver.ChromeOptions()
            opts.debugger_address = f"127.0.0.1:{self.chrome_debug_port}"
            self._set_webdriver_command_timeout()

            last_err = None
            max_attempts = 10
            chrome_restarted = False
            for attempt in range(max_attempts):
                try:
                    self.logger.info(f"🔄 尝试连接 WebDriver (第 {attempt + 1}/{max_attempts} 次)")
                    # 关键：设置较短的命令超时，避免在无效会话上卡死
                    driver = webdriver.Chrome(options=opts)
                    self._set_webdriver_command_timeout(driver)
                    try:
                        driver.set_page_load_timeout(max(5, int(self.page_load_timeout or 15)))
                    except Exception:
                        pass

                    # 验证 driver 是否真正可用。不要在当前标签页执行 JS：
                    # Chrome 可能停在新标签页、扩展页或旧的卡死页面。
                    try:
                        self._validate_webdriver_connection(driver)
                        self.logger.info(f"✅ WebDriver 连接成功 (尝试 {attempt + 1}/{max_attempts})")
                    except Exception as e:
                        self.logger.warning(f"⚠️ WebDriver 连接成功但验证失败: {e}")
                        self._quit_webdriver(driver, reason="验证失败")
                        raise e

                    # 注入反爬脚本
                    self._apply_stealth(driver)
                    # 等待 DevTools session 完全稳定
                    time.sleep(0.75)
                    return driver
                except Exception as e:
                    last_err = e
                    err_msg = str(getattr(e, "msg", str(e))).lower()
                    self.logger.warning(f"⚠️ 第 {attempt + 1} 次连接失败: {err_msg}")

                    # 如果是致命错误或多次失败，强制重启
                    should_restart_chrome = (
                        "target window already closed" in err_msg
                        or "invalid session id" in err_msg
                        or "read timed out" in err_msg
                        or "max retries exceeded" in err_msg
                        or attempt >= 3
                    )
                    if should_restart_chrome and not chrome_restarted:
                        chrome_restarted = True
                        self.logger.warning(f"⚠️ 触发强制重启模式 (错误类型: {'会话失效' if 'invalid' in err_msg else '窗口关闭' if 'window' in err_msg else '重试超限'})")
                        self._terminate_chrome_process()
                        self._start_chrome_debug()
                    elif should_restart_chrome:
                        self.logger.warning("⚠️ 本轮已重启过 Chrome，避免重复拉起新实例，继续重试连接")

                    time.sleep(self.WINDOW_HANDLE_STABILIZE_DELAY)

            error_msg = f"无法连接到 Chrome 调试端口 127.0.0.1:{self.chrome_debug_port}: {last_err}"
            self.logger.error(f"❌ {error_msg}")
            raise RuntimeError(error_msg)
        except Exception as e:
            self.logger.error(f"❌ 创建 WebDriver 实例失败: {e}", exc_info=True)
            return None

    def _is_debug_port_ready(self) -> bool:
        """检查远程调试端口是否可用"""
        try:
            resp = requests.get(f"http://127.0.0.1:{self.chrome_debug_port}/json/version", timeout=1.0)
            if resp.status_code != 200:
                return False
            data = resp.json()
            browser = str(data.get("Browser") or data.get("product") or "")
            websocket_url = str(data.get("webSocketDebuggerUrl") or "")
            if browser.startswith("Chrome/") and websocket_url.startswith("ws://"):
                return True
            self.logger.warning(
                f"⚠️ 端口 {self.chrome_debug_port} 响应了请求，但不是 Chrome DevTools: {browser or 'unknown'}"
            )
            return False
        except Exception:
            return False

    def _start_chrome_debug(self):
        """启动 Chrome 调试模式"""
        if self._is_debug_port_ready():
            self.logger.info(f"✅ Chrome 调试端口 {self.chrome_debug_port} 已可用，跳过重复启动")
            return

        user_data_dir = os.path.abspath(self.chrome_user_data_dir)
        try:
            os.makedirs(user_data_dir, exist_ok=True)
        except Exception as e:
            self.logger.warning(f"⚠️ 创建用户数据目录失败: {e}")

        # WSL 路径转换
        if self.is_wsl:
            try:
                # 尝试将 WSL 路径转换为 Windows 路径以供 chrome.exe 使用
                cmd = ["wslpath", "-w", user_data_dir]
                win_user_data_dir = subprocess.check_output(cmd, text=True).strip()
                self.logger.info(f"📂 WSL 路径转换: {user_data_dir} -> {win_user_data_dir}")
                user_data_dir = win_user_data_dir
            except Exception as e:
                self.logger.warning(f"⚠️ wslpath 转换失败: {e}")

        try:
            # 增强反爬标志
            args = [
                self.chrome_path,
                f"--remote-debugging-port={self.chrome_debug_port}",
                f"--user-data-dir={user_data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",  # 禁用自动化检测
                "--disable-infobars",  # 禁用 "正在受自动化测试软件控制"
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-browser-side-navigation",
            ]
            if isinstance(self.chrome_profile_dir, str) and self.chrome_profile_dir.strip():
                args.append(f"--profile-directory={self.chrome_profile_dir.strip()}")

            self.logger.info(f"🚀 正在启动 Chrome: {' '.join(args)}")
            # 在 WSL 中使用 subprocess.Popen 启动 Windows 程序是异步的且不会阻塞
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

            self.logger.info("⏳ 等待 Chrome 调试端口就绪...")
            for i in range(40):
                if self._is_debug_port_ready():
                    self.logger.info(f"✅ Chrome 调试端口在 {i * 0.25:.2f} 秒后就绪")
                    return
                time.sleep(0.25)

            raise RuntimeError("Chrome 调试端口启动超时")
        except Exception as e:
            self.logger.error(f"❌ 启动 Chrome 调试模式失败: {e}", exc_info=True)
            raise

    def _apply_stealth(self, driver: webdriver.Chrome):
        """注入 CDP 脚本以掩盖自动化特征"""
        try:
            # 这里的脚本是核心，用于绕过大多数基础检测（如 navigator.webdriver）
            stealth_js = """
            Object.defineProperty(navigator, 'webdriver', {
              get: () => undefined
            });

            // 掩盖 WebGL 指纹
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
              if (parameter === 37445) return 'Intel Inc.';
              if (parameter === 37446) return 'Intel(R) Iris(TM) Graphics 6100';
              return getParameter.apply(this, arguments);
            };

            // 补充其它常见特征
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """
            # 在每个新页面加载前执行
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": stealth_js
            })
            self.logger.info("🛡️ CDP Stealth 脚本已注入")
        except Exception as e:
            self.logger.warning(f"⚠️ 注入 Stealth 脚本失败: {e}")

    def _chrome_process_match_terms(self) -> List[str]:
        terms = [f"--remote-debugging-port={self.chrome_debug_port}"]
        user_data_dir = os.path.abspath(self.chrome_user_data_dir)

        if self.is_wsl:
            try:
                user_data_dir = subprocess.check_output(["wslpath", "-w", user_data_dir], text=True).strip()
            except Exception:
                pass

        if user_data_dir:
            terms.append(f"--user-data-dir={user_data_dir}")
            terms.append(user_data_dir)
        return [term for term in terms if term]

    def _terminate_automation_chrome_processes(self) -> None:
        """按命令行特征清理本插件拉起的 Chrome，避免残留自动化实例堆积。"""
        terms = self._chrome_process_match_terms()
        if not terms:
            return

        ps_terms = ", ".join("'" + term.replace("'", "''") + "'" for term in terms)
        ps_script = (
            f"$terms=@({ps_terms}); "
            "Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
            "Where-Object { "
            "$cmd=$_.CommandLine; "
            "$cmd -and ($terms | Where-Object { $cmd -like \"*$_*\" }) "
            "} | ForEach-Object { "
            "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue "
            "}"
        )

        cmd = "powershell.exe" if self.is_wsl else "powershell"
        try:
            result = subprocess.run(
                [cmd, "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                self.logger.info("✅ 已按自动化 Chrome 命令行特征清理残留进程")
            else:
                self.logger.warning(f"⚠️ 自动化 Chrome 命令行清理失败: {result.stderr.strip()}")
        except Exception as e:
            self.logger.warning(f"⚠️ 自动化 Chrome 命令行清理异常: {e}")

    def _is_windows_chrome_pid(self, pid: str) -> bool:
        cmd = "powershell.exe" if self.is_wsl else "powershell"
        try:
            result = subprocess.run(
                [
                    cmd,
                    "-NoProfile",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\").Name",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip().lower() == "chrome.exe"
        except Exception as e:
            self.logger.warning(f"⚠️ 检查 PID {pid} 进程类型失败: {e}")
            return False

    def _terminate_chrome_process(self):
        """强制终止监听指定调试端口的 Chrome 进程"""
        try:
            if self.is_wsl:
                # 在 WSL 中，我们需要找到监听 9222 端口的 Windows 进程并杀掉它
                self.logger.info(f"🔍 (WSL) 正在查找监听端口 {self.chrome_debug_port} 的 Windows 进程...")
                try:
                    # 使用 netstat.exe 查找 PID
                    cmd_netstat = f"netstat.exe -ano | grep LISTENING | grep :{self.chrome_debug_port}"
                    result = subprocess.run(cmd_netstat, capture_output=True, text=True, shell=True)
                    if result.returncode == 0 and result.stdout.strip():
                        # netstat.exe 输出最后一位是 PID
                        lines = result.stdout.strip().split('\n')
                        pids = set()
                        for line in lines:
                            parts = line.split()
                            if len(parts) >= 5:
                                pids.add(parts[-1])

                        for pid in pids:
                            if not self._is_windows_chrome_pid(pid):
                                self.logger.warning(f"⚠️ PID {pid} 不是 chrome.exe，跳过端口清理")
                                continue
                            self.logger.info(f"🔫 (WSL-Win) 找到 PID: {pid}，准备使用 taskkill.exe 终止...")
                            result = subprocess.run(["taskkill.exe", "/F", "/PID", pid], capture_output=True, text=True)
                            if result.returncode == 0:
                                self.logger.info(f"✅ (WSL-Win) 进程 {pid} 已终止")
                            else:
                                stderr = (result.stderr or result.stdout or "").strip()
                                self.logger.warning(f"⚠️ (WSL-Win) 终止进程 {pid} 失败: {stderr}")
                        time.sleep(1)
                    else:
                        self.logger.info(f"🤷 (WSL) 未通过 netstat.exe 找到监听端口 {self.chrome_debug_port} 的进程")
                        # 兜底：直接按名字杀
                        self.logger.info("🛡️ (WSL) 尝试按映像名称清理 chrome.exe...")
                        subprocess.run(["taskkill.exe", "/F", "/IM", "chrome.exe", "/FI", f"WINDOWTITLE eq *{self.chrome_debug_port}*"], capture_output=True)
                except Exception as e:
                    self.logger.warning(f"⚠️ WSL 互操作关闭 Chrome 失败: {e}")

            elif os.name == "nt":
                command = f'netstat -aon | findstr LISTENING | findstr ":{self.chrome_debug_port}"'
                result = subprocess.run(command, capture_output=True, text=True, shell=True)
                if result.returncode == 0 and result.stdout.strip():
                    pids = set()
                    for line in result.stdout.strip().splitlines():
                        parts = line.split()
                        if len(parts) >= 5:
                            pids.add(parts[-1])
                    for pid in pids:
                        if not self._is_windows_chrome_pid(pid):
                            self.logger.warning(f"⚠️ PID {pid} 不是 chrome.exe，跳过端口清理")
                            continue
                        self.logger.info(f"🔫 (Windows) 找到监听端口 {self.chrome_debug_port} 的 PID: {pid}，准备终止...")
                        kill_result = subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, text=True)
                        if kill_result.returncode == 0:
                            self.logger.info(f"✅ 进程 {pid} 已终止")
                        else:
                            stderr = (kill_result.stderr or kill_result.stdout or "").strip()
                            self.logger.warning(f"⚠️ 终止进程 {pid} 失败: {stderr}")
                    time.sleep(1)
                else:
                    self.logger.info(f"🤷 (Windows) 未找到监听端口 {self.chrome_debug_port} 的进程")
            else:
                command = f"lsof -ti :{self.chrome_debug_port}"
                result = subprocess.run(command, capture_output=True, text=True, shell=True)
                if result.stdout.strip():
                    pids = result.stdout.strip().split("\n")
                    for pid in pids:
                        if pid.strip():
                            self.logger.info(f"🔫 (Unix) 找到 PID: {pid}，准备终止...")
                            subprocess.run(f"kill -9 {pid}", check=True, shell=True, capture_output=True)
                            self.logger.info(f"✅ 进程 {pid} 已终止")
                    time.sleep(1)
                else:
                    self.logger.info(f"🤷 (Unix) 未找到监听端口 {self.chrome_debug_port} 的进程")
        except Exception as e:
            self.logger.error(f"❌ 终止 Chrome 进程时出错: {e}", exc_info=True)
        finally:
            self._terminate_automation_chrome_processes()

    def _is_driver_healthy(self) -> bool:
        """检查 WebDriver 健康状态"""
        with self.driver_lock:
            if not self.driver:
                return False
            try:
                # 不在健康检查里执行 Selenium/CDP 命令。
                # 实测 Chrome/ChromeDriver 偶发卡死时，execute_cdp_cmd("Browser.getVersion")
                # 可能无视较短的 RemoteConnection timeout，把用户事件 worker 卡在
                # “已解析链接卡片 URL”之后，导致后续摘要流程没有任何日志。这里改用
                # DevTools HTTP 探活（requests timeout=1s），失败再重建 driver。
                resp = requests.get(
                    f"http://127.0.0.1:{self.chrome_debug_port}/json/version",
                    timeout=1.0,
                )
                resp.raise_for_status()
                data = resp.json()
                browser = str(data.get("Browser") or data.get("product") or "unknown")
                if not browser.startswith("Chrome/"):
                    raise RuntimeError(f"调试端口响应异常: {browser}")
                self.logger.info(f"✅ WebDriver HTTP 健康检查通过: {browser}")
                return True
            except Exception as e:
                self.logger.warning(f"⚠️ WebDriver 健康检查失败，将重建: {e}")
                return False

    def _is_blacklisted(self, url: str) -> bool:
        """检查 URL 是否在域名黑名单中。
        支持精确域名（如 example.com）和通配符后缀（如 *.xyz 或直接填 .xyz）。
        """
        if not url or not self.domain_blacklist:
            return False
        try:
            netloc = urlparse(url).netloc.lower()
            # 去掉端口号
            netloc = netloc.split(":")[0]
            if not netloc:
                return False

            for entry in self.domain_blacklist:
                entry = entry.lstrip("*")  # 兼容 *.xyz 写法，统一为 .xyz
                if entry.startswith("."):
                    # 后缀匹配：命中 foo.xyz / bar.baz.xyz 等，但不命中 xyz 本身
                    if netloc.endswith(entry):
                        return True
                else:
                    # 精确匹配或子域名匹配
                    if netloc == entry or netloc.endswith("." + entry):
                        return True
        except Exception as e:
            self.logger.warning(f"⚠️ 检查黑名单时解析 URL 出错: {e}")
        return False

    def _is_sender_blacklisted(self, sender: str) -> bool:
        """检查消息发送者是否在 sender 黑名单中（大小写不敏感）"""
        if not sender or not self.sender_blacklist:
            return False
        return sender.lower().strip() in self.sender_blacklist

    def _ensure_driver_available(self) -> webdriver.Chrome:
        """确保 WebDriver 可用，如果不可用则尝试重新初始化"""
        if not self._is_driver_healthy():
            with self.driver_lock:
                if self.driver:
                    self._quit_webdriver(self.driver, reason="健康检查失败")
                    self.driver = None
                self._init_webdriver()
            if not self._is_driver_healthy():
                raise RuntimeError("无法获取健康的 WebDriver 实例")
        return cast(webdriver.Chrome, self.driver)

    def _close_driver(self):
        """关闭 WebDriver 实例 (假定锁已被外部获取)"""
        if self.driver:
            self._quit_webdriver(self.driver, reason="服务关闭")
            self.driver = None

    # -------------------------
    # 抖音解析（TikHub）
    # -------------------------
    def _extract_douyin_share_url(self, text: str) -> Optional[str]:
        """提取抖音分享链接（支持 v.douyin.com 短链和 www.douyin.com）"""
        if not text:
            return None

        # 匹配 v.douyin.com 短链（支持字母、数字、连字符和下划线）
        # 边界：空格或字符串结尾
        m = re.search(r"(https?://v\.douyin\.com/[0-9A-Za-z\-_]+/?)(?:\s|$)", text)
        if m:
            return m.group(1)

        # 兼容 www.douyin.com 长链接
        m = re.search(r"(https?://(?:www\.)?douyin\.com/[^\s<>\"]+)", text)
        return m.group(1) if m else None

    def _extract_tiktok_share_url(self, text: str) -> Optional[str]:
        """提取 TikTok 分享链接（支持 vt/vm.tiktok.com 短链和 www.tiktok.com 长链）"""
        if not text:
            return None

        # 匹配 vt.tiktok.com / vm.tiktok.com 短链
        m = re.search(r"(https?://(?:vt|vm)\.tiktok\.com/[0-9A-Za-z\-_]+/?)", text)
        if m:
            return m.group(1)

        # 兼容 www.tiktok.com 长链接
        m = re.search(r"(https?://(?:www\.)?tiktok\.com/[^\s<>\"]+)", text)
        return m.group(1) if m else None

    def _extract_bilibili_share_url(self, text: str) -> Optional[str]:
        """提取 Bilibili 分享链接并去除多余参数"""
        if not text:
            return None

        m = re.search(r"(https?://(?:www\.)?bilibili\.com/video/[a-zA-Z0-9]+)/?", text)
        return m.group(1) if m else None

    def _extract_xhs_share_url(self, text: str) -> Optional[str]:
        """提取小红书分享链接"""
        if not text:
            return None
        # TikHub App V2 同时支持 xhslink.com、xhslink.cn 和 xiaohongshu.com。
        m = re.search(
            r"(https?://(?:www\.)?(?:xiaohongshu\.com|xhslink\.(?:com|cn))/[^\s<>\"]+)",
            text,
        )
        return m.group(1) if m else None

    def _extract_xhs_note_id(self, text: str) -> Optional[str]:
        """从小红书长链接中提取 note_id，作为 TikHub share_text 解析失败时的稳定参数。"""
        if not text:
            return None
        normalized = self._normalize_xhs_share_url(text)
        candidates = [text]
        if normalized != text:
            candidates.append(normalized)
        candidates.append(unquote(text))
        for candidate in candidates:
            m = re.search(r"xiaohongshu\.com/(?:(?:discovery/)?item|explore)/([0-9a-fA-F]+)", candidate)
            if m:
                return m.group(1)
        return None

    def _normalize_xhs_share_url(self, url: str) -> str:
        """把小红书 login redirectPath 链接还原成真实笔记链接。"""
        if not url:
            return url
        try:
            parsed = urlparse(url)
            if parsed.netloc.endswith("xiaohongshu.com") and parsed.path.rstrip("/") == "/login":
                redirect_path = (parse_qs(parsed.query).get("redirectPath") or [""])[0]
                if redirect_path:
                    return unquote(redirect_path)
        except Exception as e:
            self.logger.warning(f"⚠️ 解析小红书跳转链接失败: {e}")
        m = re.search(r"redirectPath=([^&\s]+)", url)
        return unquote(m.group(1)) if m else url

    def _extract_youtube_url(self, text: str) -> Optional[str]:
        """提取 YouTube 链接"""
        if not text:
            return None
        # 支持 youtube.com, youtu.be, youtube.com/shorts/ 等
        patterns = [
            r"(https?://(?:www\.)?youtube\.com/watch\?v=[-_a-zA-Z0-9]{11})",
            r"(https?://youtu\.be/[-_a-zA-Z0-9]{11})",
            r"(https?://(?:www\.)?youtube\.com/shorts/[-_a-zA-Z0-9]{11})",
            r"(https?://(?:www\.)?youtube\.com/embed/[-_a-zA-Z0-9]{11})",
        ]
        for p in patterns:
            m = re.search(p, text)
            if m:
                return m.group(1)
        return None

    def _extract_weibo_url(self, text: str) -> Optional[str]:
        """提取微博链接（支持 weibo.com、weibo.cn 及其子域名）"""
        if not text:
            return None
        m = re.search(
            r"(https?://(?:[a-zA-Z0-9-]+\.)*weibo\.(?:com|cn)/"
            r"[^\s<>\"'，。！？；：、）】》]+)",
            text,
            re.IGNORECASE,
        )
        return m.group(1) if m else None

    def _extract_hupu_url(self, text: str) -> Optional[str]:
        """提取虎扑链接（支持 hupu.com 主域及子域）"""
        if not text:
            return None
        m = re.search(r"(https?://(?:[a-zA-Z0-9-]+\.)*hupu\.com/[^\s<>\"]*)", text)
        return m.group(1) if m else None

    def _get_tikhub_token(self) -> str:
        return (os.getenv("TIKHUB_API_TOKEN") or "").strip()

    def _json_loads_if_needed(self, v: Any) -> Any:
        if isinstance(v, str):
            s = v.strip()
            if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
                try:
                    return json.loads(s)
                except Exception:
                    return v
        return v

    def _get_tikhub_json(
        self,
        endpoint: str,
        *,
        headers: dict,
        params: dict,
        label: str,
    ) -> Tuple[Optional[dict], bool]:
        """Call a paid TikHub endpoint without retrying deterministic failures."""
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            error: Optional[Exception] = None
            try:
                response = requests.get(
                    endpoint,
                    headers=headers,
                    params=params,
                    timeout=20,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    self.logger.warning("⚠️ TikHub %s 返回非对象 JSON", label)
                    return None, True
                return payload, False
            except (requests.Timeout, requests.ConnectionError) as exc:
                error = exc
                transient = True
            except requests.HTTPError as exc:
                error = exc
                status_code = getattr(exc.response, "status_code", 0) or 0
                transient = status_code >= 500
            except (requests.RequestException, ValueError) as exc:
                error = exc
                transient = False
            except Exception as exc:
                error = exc
                transient = False

            self.logger.warning(
                "⚠️ TikHub %s 请求失败 (尝试 %s/%s): %s",
                label,
                attempt,
                max_attempts,
                error,
            )
            if not transient or attempt >= max_attempts:
                return None, not transient
            time.sleep(2)
        return None, False

    def _extract_first_play_url_from_aweme_detail(self, aweme_detail: Any) -> Optional[List[str]]:
        """
        从 TikHub 的 aweme_detail 中提取视频直链列表。
        规则：
        1) 强制优先：aweme_detail.video.download_addr.url_list (返回整个列表)
        """

        def _pick_url_list(obj: Any, path: List[str]) -> Optional[List[str]]:
            cur: Any = obj
            for key in path:
                if not isinstance(cur, dict):
                    return None
                cur = cur.get(key)
            if not isinstance(cur, list) or not cur:
                return None
            # 过滤出有效的 URL
            valid_urls = []
            for item in cur:
                if isinstance(item, str):
                    s = item.strip()
                    if s.startswith(("http://", "https://")):
                        valid_urls.append(s)
            return valid_urls if valid_urls else None

        try:
            url_list = _pick_url_list(aweme_detail, ["video", "download_addr", "url_list"])
            if url_list:
                return url_list
        except Exception:
            return None

    def _extract_play_url_from_tiktok_aweme(self, aweme_detail: Any) -> Optional[List[str]]:
        """
        从 TikHub 的 TikTok aweme_detail 中提取视频直链列表。
        TikTok App V3 接口使用 video.play_addr.url_list 路径。
        """

        def _pick_url_list(obj: Any, path: List[str]) -> Optional[List[str]]:
            cur: Any = obj
            for key in path:
                if not isinstance(cur, dict):
                    return None
                cur = cur.get(key)
            if not isinstance(cur, list) or not cur:
                return None
            valid_urls = [url for url in cur if isinstance(url, str) and url.strip().startswith(("http://", "https://"))]
            return valid_urls if valid_urls else None

        try:
            url_list = _pick_url_list(aweme_detail, ["video", "play_addr", "url_list"])
            if url_list:
                return url_list
        except Exception:
            return None

    def parse_douyin_video(self, text: str) -> Optional[List[str]]:
        """使用 TikHub 解析抖音分享链接，返回直链列表（url_list）"""
        share_url = self._extract_douyin_share_url(text)
        if not share_url:
            return None

        token = self._get_tikhub_token()
        if not token:
            self.logger.warning("⚠️ 缺少 TikHub Token：请在 .env 设置 TIKHUB_API_TOKEN")
            return None

        headers = {"Authorization": f"Bearer {token}"}
        params = {"share_url": share_url}
        payload, app_permanent_failure = self._get_tikhub_json(
            TIKHUB_ENDPOINT_FETCH_ONE,
            headers=headers,
            params=params,
            label="抖音 App V3",
        )
        data = self._json_loads_if_needed(payload.get("data")) if payload else None
        if isinstance(data, dict):
            aweme_detail = self._json_loads_if_needed(data.get("aweme_detail"))
            if isinstance(aweme_detail, dict):
                url_list = self._extract_first_play_url_from_aweme_detail(aweme_detail)
                if url_list:
                    return url_list

        if app_permanent_failure:
            self.logger.error("❌ TikHub 抖音 App V3 请求不可重试，停止解析")
            return None

        # App V3 成功但无视频直链时，官方建议尝试 Web 接口。
        self.logger.info("🔄 抖音 App V3 未返回视频直链，尝试 Web 接口")
        payload_web, _ = self._get_tikhub_json(
            TIKHUB_ENDPOINT_FETCH_ONE_WEB,
            headers=headers,
            params=params,
            label="抖音 Web",
        )
        data_web = self._json_loads_if_needed(payload_web.get("data")) if payload_web else None
        if not isinstance(data_web, dict):
            self.logger.error("❌ TikHub 抖音解析失败")
            return None

        aweme_detail_web = self._json_loads_if_needed(data_web.get("aweme_detail"))
        if not isinstance(aweme_detail_web, dict):
            self.logger.error("❌ TikHub 抖音 Web 接口未返回作品数据")
            return None

        url_list = self._extract_first_play_url_from_aweme_detail(aweme_detail_web)
        if url_list:
            return url_list

        # 图文视频使用 images[0].video.play_addr.url_list。
        images = aweme_detail_web.get("images")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            video = images[0].get("video")
            play_addr = video.get("play_addr") if isinstance(video, dict) else None
            raw_urls = play_addr.get("url_list") if isinstance(play_addr, dict) else None
            if isinstance(raw_urls, list):
                valid_urls = [
                    url
                    for url in raw_urls
                    if isinstance(url, str)
                    and url.strip().startswith(("http://", "https://"))
                ]
                if valid_urls:
                    self.logger.info(
                        "✅ 从抖音 Web images 路径提取到 %s 个视频直链",
                        len(valid_urls),
                    )
                    return valid_urls
        self.logger.error("❌ TikHub 抖音 Web 接口未返回视频直链")
        return None

    def parse_tiktok_video(self, text: str) -> Optional[List[str]]:
        """使用 TikHub 解析 TikTok 分享链接，返回直链列表（url_list）"""
        share_url = self._extract_tiktok_share_url(text)
        if not share_url:
            return None

        token = self._get_tikhub_token()
        if not token:
            self.logger.warning("⚠️ 缺少 TikHub Token：请在 .env 设置 TIKHUB_API_TOKEN")
            return None

        headers = {"Authorization": f"Bearer {token}"}
        params = {"share_url": share_url}
        payload, _ = self._get_tikhub_json(
            TIKHUB_ENDPOINT_TIKTOK_FETCH_ONE,
            headers=headers,
            params=params,
            label="TikTok App V3",
        )
        data = self._json_loads_if_needed(payload.get("data")) if payload else None
        if not isinstance(data, dict):
            return None

        aweme_detail = self._json_loads_if_needed(data.get("aweme_detail"))
        if not isinstance(aweme_detail, dict):
            return None
        return self._extract_play_url_from_tiktok_aweme(aweme_detail)

    def _rank_video_download_url(self, url: str) -> int:
        """给视频直链排序：优先稳定 CDN，降低 experiment 节点优先级。"""
        u = (url or "").lower()
        score = 0
        if "experiment" in u:
            score += 20
        if "ov-experiment" in u:
            score += 20
        if "v5-hl" in u:
            score -= 5
        return score

    def _download_douyin_with_ytdlp(
        self,
        share_url: str,
        timeout_sec: int = 180,
    ) -> Optional[str]:
        """Download Douyin with yt-dlp before the paid TikHub fallback."""
        share_url = self._extract_douyin_share_url(share_url or "") or ""
        if not share_url:
            return None

        tmp_dir = os.path.join(os.getcwd(), "tmp", "videos")
        os.makedirs(tmp_dir, exist_ok=True)
        output_template = os.path.join(
            tmp_dir,
            f"douyin_ytdlp_{int(time.time())}_{uuid.uuid4().hex[:8]}.%(ext)s",
        )
        self.logger.info("📥 抖音优先使用 yt-dlp 下载: %s", share_url)
        try:
            result = self._run_platform_ytdlp(
                "douyin",
                [
                    "--ffmpeg-location",
                    self.ffmpeg_bin,
                    "--format",
                    "b[format_id^=h264_]/b[vcodec=h264]/b[vcodec^=avc]/b[ext=mp4]/b",
                    "--merge-output-format",
                    "mp4",
                    "--remux-video",
                    "mp4",
                    "--no-write-thumbnail",
                    "--no-progress",
                    "-o",
                    output_template,
                    share_url,
                ],
                timeout_sec=timeout_sec,
            )
            if result.returncode != 0:
                self._log_ytdlp_failure("抖音", result)
                return None
            video_path = self._find_ytdlp_output(output_template)
            if not video_path:
                self.logger.warning("⚠️ 抖音 yt-dlp 未生成视频文件")
                return None

            codec = self._probe_video_codec(video_path)
            if codec and codec not in {"h264", "avc1"}:
                self.logger.info("🔄 抖音 yt-dlp 输出编码为 %s，转换为微信兼容 H.264", codec)
                video_path = self._convert_to_wechat_compatible(video_path) or video_path
            self.logger.info("✅ 抖音 yt-dlp 下载成功: %s", video_path)
            return video_path
        except subprocess.TimeoutExpired:
            self.logger.warning("⚠️ 抖音 yt-dlp 下载超时（>%ss）", timeout_sec)
            return None
        except FileNotFoundError:
            self.logger.warning("⚠️ 未找到 yt-dlp，抖音将回退 TikHub")
            return None
        except Exception as exc:
            self.logger.warning("⚠️ 抖音 yt-dlp 下载异常，将回退 TikHub: %s", exc)
            return None

    def _download_video(self, url_list: List[str]) -> Optional[str]:
        """下载视频到临时文件，支持 fallback 机制遍历 url_list"""
        if not url_list:
            self.logger.error("❌ URL 列表为空")
            return None

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.douyin.com/',
        }
        connect_timeout = 5
        read_timeout = 12
        chunk_size = 256 * 1024
        url_list = sorted(url_list, key=self._rank_video_download_url)

        # 创建临时目录
        tmp_dir = os.path.join(os.getcwd(), "tmp", "videos")
        os.makedirs(tmp_dir, exist_ok=True)

        # 遍历 url_list，尝试每个 URL 直到成功
        for idx, url in enumerate(url_list, 1):
            try:
                self.logger.info(
                    f"⬇️ 尝试下载视频 ({idx}/{len(url_list)}): {url[:200]}... "
                    f"timeout=({connect_timeout}s connect, {read_timeout}s read)"
                )
                resp = requests.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(connect_timeout, read_timeout),
                )
                resp.raise_for_status()

                # 检查内容类型
                content_type = resp.headers.get('Content-Type', '')
                if 'video' not in content_type and 'octet-stream' not in content_type:
                    self.logger.warning(f"⚠️ 警告：返回的内容类型可能不是视频: {content_type}")

                filename = f"douyin_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"
                filepath = os.path.join(tmp_dir, filename)

                with open(filepath, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)

                self.logger.info(f"✅ 下载成功！文件已保存至: {filepath}")

                # 简单校验文件头 (MP4 常见的 ftyp 标记)
                try:
                    with open(filepath, 'rb') as f:
                        header = f.read(12)
                        if b'ftyp' in header:
                            self.logger.info("✅ 验证通过：文件头部包含有效的 MP4 标识。")
                        else:
                            self.logger.warning("⚠️ 警告：文件头部未检测到标准 MP4 标识，请确认文件是否可播放。")
                except Exception as e:
                    self.logger.warning(f"⚠️ 校验视频文件头失败: {e}")

                return filepath
            except Exception as e:
                self.logger.warning(f"⚠️ URL {idx}/{len(url_list)} 下载失败: {e}")
                if idx == len(url_list):
                    self.logger.error(f"❌ 所有 URL 均下载失败，共尝试 {len(url_list)} 个")
                continue

        return None

    def _download_hupu_video(self, url: str, timeout_sec: int = 60) -> Optional[str]:
        """使用 yt-dlp 下载虎扑视频，成功后转为微信兼容格式"""
        if not url:
            return None

        tmp_dir = os.path.join(os.getcwd(), "tmp", "videos")
        os.makedirs(tmp_dir, exist_ok=True)

        output_tpl = os.path.join(
            tmp_dir, f"hupu_{int(time.time())}_{uuid.uuid4().hex[:8]}.%(ext)s"
        )
        self.logger.info(f"🏀 命中虎扑链接，开始尝试 yt-dlp 下载: {url}")

        try:
            cmd = ["yt-dlp", "--no-playlist", "-o", output_tpl, url]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                self.logger.warning(
                    f"⚠️ 虎扑 yt-dlp 下载失败，返回码 {result.returncode}: {url}"
                )
                err_lines = (result.stderr or "").strip().split("\n")
                if err_lines:
                    self.logger.warning(f"⚠️ yt-dlp 错误输出(尾部): {chr(10).join(err_lines[-5:])}")
                return None

            matched_files = []
            prefix = output_tpl.replace("%(ext)s", "")
            for fn in os.listdir(tmp_dir):
                full_path = os.path.join(tmp_dir, fn)
                if os.path.isfile(full_path) and full_path.startswith(prefix):
                    matched_files.append(full_path)

            if not matched_files:
                self.logger.warning(f"⚠️ 虎扑下载未找到输出文件: {url}")
                return None

            matched_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            downloaded_path = matched_files[0]
            self.logger.info(f"✅ 虎扑视频下载成功: {downloaded_path}")

            converted_path = self._convert_to_wechat_compatible(downloaded_path)
            if converted_path and os.path.exists(converted_path):
                self.logger.info(f"✅ 虎扑视频已转换为微信兼容格式: {converted_path}")
                return converted_path

            self.logger.warning("⚠️ 虎扑视频转码失败，回退使用原始下载文件")
            return downloaded_path if os.path.exists(downloaded_path) else None
        except subprocess.TimeoutExpired:
            self.logger.warning(f"⚠️ 虎扑 yt-dlp 下载超时（>{timeout_sec}s）: {url}")
            return None
        except FileNotFoundError:
            self.logger.error("❌ 未找到 yt-dlp 命令，虎扑视频下载已跳过")
            return None
        except Exception as e:
            self.logger.error(f"❌ 虎扑视频下载异常: {e}", exc_info=True)
            return None

    def _download_weibo_video(self, url: str, timeout_sec: int = 180) -> Optional[str]:
        """直接使用 yt-dlp 下载微博视频并返回本地文件路径。"""
        if not url:
            return None

        tmp_dir = os.path.join(os.getcwd(), "tmp", "videos")
        os.makedirs(tmp_dir, exist_ok=True)

        output_tpl = os.path.join(
            tmp_dir, f"weibo_{int(time.time())}_{uuid.uuid4().hex[:8]}.%(ext)s"
        )
        self.logger.info(f"📺 命中微博链接，开始使用 yt-dlp 下载: {url}")

        try:
            cmd = [
                self.yt_dlp_bin,
                "--no-playlist",
                "--ffmpeg-location",
                self.ffmpeg_bin,
                "--merge-output-format",
                "mp4",
                "--remux-video",
                "mp4",
                "-o",
                output_tpl,
                url,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                self.logger.warning(
                    f"⚠️ 微博 yt-dlp 下载失败，返回码 {result.returncode}: {url}"
                )
                err_lines = (result.stderr or "").strip().splitlines()
                if err_lines:
                    self.logger.warning(
                        f"⚠️ yt-dlp 错误输出(尾部): {chr(10).join(err_lines[-5:])}"
                    )
                return None

            prefix = output_tpl.replace("%(ext)s", "")
            matched_files = [
                os.path.join(tmp_dir, filename)
                for filename in os.listdir(tmp_dir)
                if os.path.isfile(os.path.join(tmp_dir, filename))
                and os.path.join(tmp_dir, filename).startswith(prefix)
                and not filename.endswith((".part", ".temp", ".ytdl"))
            ]
            if not matched_files:
                self.logger.warning(f"⚠️ 微博下载未找到输出文件: {url}")
                return None

            matched_files.sort(key=os.path.getmtime, reverse=True)
            downloaded_path = matched_files[0]
            self.logger.info(f"✅ 微博视频下载成功: {downloaded_path}")
            return downloaded_path
        except subprocess.TimeoutExpired:
            self.logger.warning(f"⚠️ 微博 yt-dlp 下载超时（>{timeout_sec}s）: {url}")
            return None
        except FileNotFoundError:
            self.logger.error("❌ 未找到 yt-dlp 命令，微博视频下载已跳过")
            return None
        except Exception as e:
            self.logger.error(f"❌ 微博视频下载异常: {e}", exc_info=True)
            return None

    def _check_bilibili_duration(self, url: str) -> Optional[int]:
        """检查B站视频时长，返回秒数。若失败则返回 None"""
        yt_dlp_timeout_sec = 20
        try:
            self.logger.info(f"⏱️ 正在检查视频时长: {url}")
            bvid_match = re.search(r"BV[0-9A-Za-z]+", url)
            if bvid_match:
                bvid = bvid_match.group(0)
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://www.bilibili.com/",
                }
                try:
                    session = requests.Session()
                    session.trust_env = False
                    api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
                    resp = session.get(api_url, headers=headers, timeout=8)
                    payload = resp.json() if resp.ok else {}
                    data = payload.get("data", {}) if isinstance(payload, dict) else {}
                    duration = data.get("duration")
                    if isinstance(duration, (int, float)) and duration > 0:
                        total_seconds = int(duration)
                        self.logger.info(f"✅ B站 API 视频时长检测: {total_seconds}秒")
                        return total_seconds
                    if isinstance(duration, str) and duration.isdigit():
                        total_seconds = int(duration)
                        self.logger.info(f"✅ B站 API 视频时长检测: {total_seconds}秒")
                        return total_seconds
                    pages = data.get("pages", [])
                    if isinstance(pages, list) and pages:
                        page_duration = pages[0].get("duration") if isinstance(pages[0], dict) else None
                        if isinstance(page_duration, (int, float)) and page_duration > 0:
                            total_seconds = int(page_duration)
                            self.logger.info(f"✅ B站 API 分P时长检测: {total_seconds}秒")
                            return total_seconds
                    self.logger.warning(f"⚠️ B站 API 未返回有效时长: {payload.get('message') if isinstance(payload, dict) else 'unknown'}")
                except Exception as e:
                    self.logger.warning(f"⚠️ B站 API 获取视频时长失败，回退 yt-dlp: {e}")

            # 使用 yt-dlp --get-duration 获取视频时长
            cookies_path = self._get_bili_cookies_path()
            cmd = ['yt-dlp', '--get-duration', '--no-playlist', '--proxy', '']

            # 添加反爬和身份标识
            cmd.extend(['--add-headers', 'User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'])
            cmd.extend(['--add-headers', 'Referer:https://www.bilibili.com/'])

            if os.path.exists(cookies_path):
                cmd.extend(['--cookies', cookies_path])

            cmd.append(url)
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=yt_dlp_timeout_sec,
                encoding='utf-8', errors='replace'
            )

            if result.returncode != 0:
                self.logger.warning(f"⚠️ 获取视频时长失败: yt-dlp 返回码 {result.returncode}, 错误信息: {result.stderr.strip()}")
                return None

            duration_str = result.stdout.strip()
            if not duration_str:
                self.logger.warning("⚠️ 无法解析视频时长，yt-dlp 返回为空")
                return None

            # 解析时间字符串
            parts = duration_str.split(':')
            total_seconds = 0
            if len(parts) == 3:  # HH:MM:SS
                total_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif len(parts) == 2:  # MM:SS
                total_seconds = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 1:  # SS
                total_seconds = int(parts[0])
            else:
                self.logger.warning(f"⚠️ 无法识别的时间格式: {duration_str}")
                return None

            self.logger.info(f"✅ 视频时长检测: {duration_str} ({total_seconds}秒)")
            return total_seconds

        except subprocess.TimeoutExpired:
            self.logger.warning(f"⚠️ 获取视频时长超时 (超过{yt_dlp_timeout_sec}秒): {url}")
            return None
        except FileNotFoundError:
            self.logger.error("❌ 未找到 yt-dlp 命令，请确认已安装并加入系统 PATH")
            return None
        except Exception as e:
            self.logger.error(f"❌ 解析视频时长发生异常: {e}", exc_info=True)
            return None

    def _download_bilibili_video(self, url: str, max_720p: bool = False) -> Optional[str]:
        """使用 yt-dlp 下载 Bilibili 视频"""
        # 创建临时目录
        tmp_dir = os.path.join(os.getcwd(), "tmp", "videos")
        os.makedirs(tmp_dir, exist_ok=True)

        filename = f"bilibili_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"
        filepath = os.path.join(tmp_dir, filename)

        try:
            self.logger.info(f"⬇️ 开始使用 yt-dlp 下载视频 (Max 720p: {max_720p}): {url}")

            # 使用 yt-dlp 下载视频并合并为 mp4。
            format_str = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best' if max_720p else 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best'

            cookies_path = self._get_bili_cookies_path()
            cmd = [
                'yt-dlp',
                '-f', format_str,
                '--ffmpeg-location', self.ffmpeg_bin,
                '--merge-output-format', 'mp4',
                '--no-playlist',
                '--proxy', '',
                '-o', filepath
            ]

            if self.bilibili_burn_danmu:
                cmd.extend([
                    '--write-subs', '--sub-langs', 'danmaku',
                    '--use-postprocessor', f"danmaku:font_size={self.danmaku_font_size};line_spacing={self.danmaku_line_spacing};display_region_ratio={self.danmaku_display_region_ratio}"
                ])

            cmd.extend([
                '--add-headers', 'User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                '--add-headers', 'Referer:https://www.bilibili.com/'
            ])

            if os.path.exists(cookies_path):
                cmd.extend(['--cookies', cookies_path])

            cmd.append(url)

            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=600, # 增加超时到10分钟
                encoding='utf-8', errors='replace'
            )

            if result.returncode == 0 and os.path.exists(filepath):
                self.logger.info(f"✅ Bilibili 视频下载成功！文件已保存至: {filepath}")
                return filepath
            else:
                self.logger.error(f"❌ yt-dlp 下载失败，返回码: {result.returncode}")
                # 记录最后几行错误输出以供调试
                err_lines = result.stderr.strip().split('\n')
                if err_lines:
                    self.logger.error(f"yt-dlp 倒数错误: {chr(10).join(err_lines[-5:])}")
                return None

        except subprocess.TimeoutExpired:
            self.logger.error(f"❌ 下载 Bilibili 视频超时 (超过300秒): {url}")
            return None

    def _parse_ass_time_to_seconds(self, value: str) -> Optional[float]:
        """Parse ASS timestamp like H:MM:SS.cc into seconds."""
        parts = value.strip().split(":")
        if len(parts) != 3:
            return None
        try:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
        except ValueError:
            return None
        return hours * 3600 + minutes * 60 + seconds

    def _limit_danmaku_ass_file(self, ass_path: str) -> Tuple[int, int]:
        """Limit danmaku Dialogue lines per time window for mobile-friendly viewing."""
        window_seconds = self.danmaku_limit_window_seconds
        max_per_window = self.danmaku_max_per_window
        if window_seconds <= 0 or max_per_window <= 0:
            return 0, 0

        try:
            with open(ass_path, "r", encoding="utf-8-sig", errors="replace") as f:
                lines = f.readlines()
        except OSError as exc:
            self.logger.warning(f"⚠️ 读取弹幕文件失败，跳过弹幕限流: {exc}")
            return 0, 0

        buckets: Dict[int, List[Tuple[int, float]]] = {}
        always_keep_indices: Set[int] = set()
        dialogue_count = 0
        for idx, line in enumerate(lines):
            if not line.startswith("Dialogue:"):
                continue
            dialogue_count += 1
            payload = line[len("Dialogue:"):].strip()
            fields = payload.split(",", 9)
            if len(fields) < 10:
                always_keep_indices.add(idx)
                continue
            start_seconds = self._parse_ass_time_to_seconds(fields[1])
            if start_seconds is None:
                always_keep_indices.add(idx)
                continue
            bucket_key = int(start_seconds // window_seconds)
            buckets.setdefault(bucket_key, []).append((idx, start_seconds))

        keep_indices: Set[int] = set(always_keep_indices)
        for entries in buckets.values():
            entries.sort(key=lambda item: item[1])
            if len(entries) <= max_per_window:
                keep_indices.update(idx for idx, _ in entries)
                continue

            if max_per_window == 1:
                selected_positions = [len(entries) // 2]
            else:
                selected_positions = [
                    round(i * (len(entries) - 1) / (max_per_window - 1))
                    for i in range(max_per_window)
                ]
            keep_indices.update(entries[pos][0] for pos in selected_positions)

        if dialogue_count == 0:
            return 0, 0

        kept_dialogues = 0
        filtered_lines: List[str] = []
        for idx, line in enumerate(lines):
            if line.startswith("Dialogue:"):
                if idx not in keep_indices:
                    continue
                kept_dialogues += 1
            filtered_lines.append(line)

        if kept_dialogues == dialogue_count:
            return dialogue_count, dialogue_count

        try:
            with open(ass_path, "w", encoding="utf-8", newline="") as f:
                f.writelines(filtered_lines)
        except OSError as exc:
            self.logger.warning(f"⚠️ 写入弹幕限流结果失败，使用原始弹幕: {exc}")
            return dialogue_count, dialogue_count

        return dialogue_count, kept_dialogues

    def _get_bilibili_webmask_info(self, source_url: str) -> Optional[Tuple[str, int]]:
        """Fetch official Bilibili webmask URL and FPS. No local segmentation is used."""
        if not source_url:
            return None

        bvid_match = re.search(r"BV[0-9A-Za-z]+", source_url)
        if not bvid_match:
            return None

        bvid = bvid_match.group(0)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        }

        try:
            view_resp = requests.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                headers=headers,
                timeout=12,
            )
            view_resp.raise_for_status()
            view_data = view_resp.json()
            pages = ((view_data.get("data") or {}).get("pages") or [])
            if not pages:
                return None
            cid = pages[0].get("cid")
            if not cid:
                return None

            for endpoint in (
                "https://api.bilibili.com/x/player/wbi/v2",
                "https://api.bilibili.com/x/player/v2",
            ):
                try:
                    player_resp = requests.get(
                        endpoint,
                        params={"bvid": bvid, "cid": cid},
                        headers=headers,
                        timeout=12,
                    )
                    player_resp.raise_for_status()
                    player_data = player_resp.json()
                except Exception as exc:
                    self.logger.debug(f"B站 webmask 接口请求失败，继续尝试下一个端点: {endpoint}, {exc}")
                    continue
                dm_mask = ((player_data.get("data") or {}).get("dm_mask") or {})
                mask_url = dm_mask.get("mask_url")
                if not mask_url:
                    continue
                if mask_url.startswith("//"):
                    mask_url = "https:" + mask_url
                elif mask_url.startswith("/"):
                    mask_url = "https://www.bilibili.com" + mask_url
                fps = int(dm_mask.get("fps") or 30)
                self.logger.info(f"✅ 获取到 B站 webmask: cid={cid}, fps={fps}")
                return mask_url, fps
        except Exception as exc:
            self.logger.warning(f"⚠️ 获取 B站 webmask 信息失败，将回退普通弹幕压制: {exc}")

        return None

    def _download_bilibili_webmask(self, mask_url: str, target_path: str) -> bool:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        }
        try:
            resp = requests.get(mask_url, headers=headers, timeout=30)
            resp.raise_for_status()
            content = resp.content
            if len(content) < 16 or content[:4] != b"MASK":
                self.logger.warning("⚠️ webmask 文件格式无效，将回退普通弹幕压制")
                return False
            with open(target_path, "wb") as f:
                f.write(content)
            self.logger.info(f"✅ webmask 下载完成: {len(content)} bytes")
            return True
        except Exception as exc:
            self.logger.warning(f"⚠️ 下载 webmask 失败，将回退普通弹幕压制: {exc}")
            return False

    def _extract_webmask_svg_frames(self, webmask_path: str, output_dir: str) -> int:
        """Extract official webmask SVG frames from .webmask file."""
        try:
            with open(webmask_path, "rb") as f:
                buf = f.read()

            if len(buf) < 16 or buf[:4] != b"MASK":
                return 0

            segment_count = struct.unpack(">i", buf[12:16])[0]
            if segment_count <= 0:
                return 0

            offsets: List[int] = []
            for idx in range(segment_count):
                pos = 16 + idx * 16
                if pos + 16 > len(buf):
                    return 0
                offsets.append(struct.unpack(">q", buf[pos + 8:pos + 16])[0])

            os.makedirs(output_dir, exist_ok=True)
            frame_count = 0
            for idx, start in enumerate(offsets):
                end = offsets[idx + 1] if idx + 1 < len(offsets) else len(buf)
                if start < 0 or end <= start or end > len(buf):
                    continue
                try:
                    block = gzip.decompress(buf[start:end])
                except OSError:
                    continue

                for raw_frame in block.split(b"data:image/svg+xml;base64,")[1:]:
                    encoded_svg = raw_frame.split(b"\x00", 1)[0].strip()
                    if not encoded_svg:
                        continue
                    try:
                        svg = base64.b64decode(encoded_svg).decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    frame_count += 1
                    frame_path = os.path.join(output_dir, f"mask_{frame_count - 1:06d}.svg")
                    with open(frame_path, "w", encoding="utf-8") as f:
                        f.write(svg)

            return frame_count
        except Exception as exc:
            self.logger.warning(f"⚠️ 解析 webmask 失败，将回退普通弹幕压制: {exc}")
            return 0

    def _probe_video_dimensions(self, video_path: str) -> Optional[Tuple[int, int]]:
        try:
            result = subprocess.run(
                [
                    self.ffprobe_bin, "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "json",
                    video_path,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout or "{}")
            stream = (data.get("streams") or [{}])[0]
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            if width > 0 and height > 0:
                return width, height
        except Exception:
            return None
        return None

    def _probe_video_codec(self, video_path: str) -> Optional[str]:
        try:
            result = subprocess.run(
                [
                    self.ffprobe_bin, "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    video_path,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                codec = (result.stdout or "").strip().casefold()
                return codec or None
        except Exception:
            return None
        return None

    def _probe_video_duration_seconds(self, video_path: str) -> Optional[float]:
        try:
            result = subprocess.run(
                [
                    self.ffprobe_bin, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    video_path,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                duration = float((result.stdout or "").strip())
                if duration > 0:
                    return duration
        except Exception:
            return None
        return None

    def _parse_ffprobe_rate(self, value: Any) -> Optional[float]:
        if not value:
            return None
        try:
            text = str(value).strip()
            if "/" in text:
                numerator, denominator = text.split("/", 1)
                denominator_value = float(denominator)
                if denominator_value == 0:
                    return None
                rate = float(numerator) / denominator_value
            else:
                rate = float(text)
            if 1 <= rate <= 240:
                return rate
        except Exception:
            return None
        return None

    def _probe_video_frame_rate(self, video_path: str) -> Optional[float]:
        try:
            result = subprocess.run(
                [
                    self.ffprobe_bin, "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                    "-of", "json",
                    video_path,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout or "{}")
            stream = (data.get("streams") or [{}])[0]
            avg_rate = self._parse_ffprobe_rate(stream.get("avg_frame_rate"))
            real_rate = self._parse_ffprobe_rate(stream.get("r_frame_rate"))
            return avg_rate or real_rate
        except Exception:
            return None

    def _choose_video_output_fps(self, video_path: str, fallback_fps: float) -> int:
        video_fps = self._probe_video_frame_rate(video_path)
        chosen_fps = video_fps or fallback_fps or 30
        if chosen_fps < 24:
            chosen_fps = 30
        return max(24, min(60, int(round(chosen_fps))))

    def _resolve_media_tool(self, tool_name: str, plugin_name: str, configured_path: str = "") -> str:
        if configured_path and os.path.exists(configured_path):
            return configured_path
        if configured_path:
            self.logger.warning(f"⚠️ 配置的 {tool_name} 路径不存在，将尝试自动查找: {configured_path}")

        env_name = f"{tool_name.upper()}_PATH"
        env_path = (os.environ.get(env_name) or "").strip()
        if env_path and os.path.exists(env_path):
            return env_path
        if env_path:
            self.logger.warning(f"⚠️ 环境变量 {env_name} 指向的路径不存在，将尝试自动查找: {env_path}")

        ffmpeg_dir = str(get_config("ffmpeg_dir", plugin_name=plugin_name, default="") or "").strip()
        if ffmpeg_dir:
            candidate = os.path.join(ffmpeg_dir, f"{tool_name}.exe" if os.name == "nt" else tool_name)
            if os.path.exists(candidate):
                return candidate
            self.logger.warning(f"⚠️ 配置的 ffmpeg_dir 未找到 {tool_name}，将尝试自动查找: {candidate}")

        project_candidate = os.path.join(
            os.getcwd(),
            "tools",
            "ffmpeg",
            "bin",
            f"{tool_name}.exe" if os.name == "nt" else tool_name,
        )
        if os.path.exists(project_candidate):
            return project_candidate

        try:
            from static_ffmpeg import run as static_ffmpeg_run

            ffmpeg_path, ffprobe_path = (
                static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
            )
            bundled_path = ffmpeg_path if tool_name == "ffmpeg" else ffprobe_path
            if os.path.exists(bundled_path):
                return bundled_path
        except Exception as exc:
            self.logger.warning(f"⚠️ 自动准备 {tool_name} 失败，将尝试直接调用系统命令: {exc}")

        if os.name == "nt":
            for tool_dir in (
                "C:\\msys64\\ucrt64\\bin",
                "C:\\msys64\\mingw64\\bin",
            ):
                candidate = os.path.join(tool_dir, f"{tool_name}.exe")
                if os.path.exists(candidate):
                    return candidate

        resolved_path = shutil.which(tool_name)
        if resolved_path:
            return resolved_path

        return tool_name

    def _resolve_ytdlp_tool(self) -> str:
        """优先使用当前 Python/虚拟环境中由 pip 安装的 yt-dlp 命令。"""
        executable_name = "yt-dlp.exe" if os.name == "nt" else "yt-dlp"
        environment_candidate = os.path.join(
            os.path.dirname(os.path.abspath(sys.executable)),
            executable_name,
        )
        if os.path.isfile(environment_candidate):
            return environment_candidate

        resolved_path = shutil.which("yt-dlp")
        return resolved_path or "yt-dlp"

    def _run_platform_ytdlp(
        self,
        platform: str,
        arguments: List[str],
        *,
        timeout_sec: int,
        cookie_args: Optional[List[str]] = None,
    ) -> subprocess.CompletedProcess:
        """Run yt-dlp with cookies from the project's dedicated Chrome profile."""
        if cookie_args is not None:
            return subprocess.run(
                [
                    self.yt_dlp_bin,
                    "--ignore-config",
                    "--no-playlist",
                    *cookie_args,
                    *arguments,
                ],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                encoding="utf-8",
                errors="replace",
            )
        with ytdlp_browser_cookie_args(
            platform=platform,
            debug_port=self.chrome_debug_port,
            user_data_dir=self.chrome_user_data_dir,
            profile_dir=self.chrome_profile_dir,
            logger=self.logger,
        ) as cookie_args:
            return subprocess.run(
                [
                    self.yt_dlp_bin,
                    "--ignore-config",
                    "--no-playlist",
                    *cookie_args,
                    *arguments,
                ],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                encoding="utf-8",
                errors="replace",
            )

    def _find_ytdlp_output(self, output_template: str) -> Optional[str]:
        prefix = output_template.replace("%(ext)s", "")
        directory = os.path.dirname(output_template)
        candidates = [
            os.path.join(directory, filename)
            for filename in os.listdir(directory)
            if os.path.isfile(os.path.join(directory, filename))
            and os.path.join(directory, filename).startswith(prefix)
            and not filename.endswith((".part", ".temp", ".ytdl", ".json"))
        ]
        if not candidates:
            return None
        candidates.sort(key=os.path.getmtime, reverse=True)
        return candidates[0]

    def _log_ytdlp_failure(self, platform: str, result: subprocess.CompletedProcess) -> None:
        self.logger.warning(
            "⚠️ %s yt-dlp 失败，返回码 %s",
            platform,
            result.returncode,
        )
        err_lines = (result.stderr or "").strip().splitlines()
        if err_lines:
            self.logger.warning(
                "⚠️ %s yt-dlp 错误输出(尾部): %s",
                platform,
                chr(10).join(err_lines[-5:]),
            )

    def _escape_ffmpeg_filter_path(self, path: str) -> str:
        return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")

    def _remove_path_quietly(self, path: str) -> None:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _burn_bilibili_danmaku_with_webmask(
        self,
        video_path: str,
        danmaku_path: str,
        source_url: str,
        output_path: str,
    ) -> bool:
        """Burn danmaku while respecting Bilibili official webmask."""
        if not self.bilibili_danmaku_webmask_enabled or not source_url:
            return False

        webmask_info = self._get_bilibili_webmask_info(source_url)
        if not webmask_info:
            return False

        dimensions = self._probe_video_dimensions(video_path)
        if not dimensions:
            self.logger.warning("⚠️ 无法探测视频尺寸，将回退普通弹幕压制")
            return False
        width, height = dimensions

        base_path = video_path.rsplit(".", 1)[0]
        webmask_path = base_path + ".webmask"
        mask_svg_dir = base_path + "_webmask_svg"
        mask_url, mask_fps = webmask_info
        if mask_fps <= 0:
            mask_fps = 30

        if not self._download_bilibili_webmask(mask_url, webmask_path):
            return False

        frame_count = self._extract_webmask_svg_frames(webmask_path, mask_svg_dir)
        if frame_count <= 0:
            return False

        video_duration = self._probe_video_duration_seconds(video_path)
        if video_duration and frame_count / mask_fps < video_duration * 0.8:
            self.logger.warning(
                "⚠️ webmask 帧数明显短于视频，将回退普通弹幕压制: "
                f"frames={frame_count}, fps={mask_fps}, video={video_duration:.1f}s"
            )
            return False

        output_fps = self._choose_video_output_fps(video_path, mask_fps)
        self.logger.info(
            "🛡️ 启用 B站 webmask 防遮挡弹幕压制: "
            f"{frame_count} 帧, mask={mask_fps}fps, output={output_fps}fps, {width}x{height}"
        )

        safe_ass_path = self._escape_ffmpeg_filter_path(danmaku_path)
        mask_pattern = os.path.join(mask_svg_dir, "mask_%06d.svg")
        filter_complex = (
            f"[0:v]setpts=PTS-STARTPTS,fps={output_fps},format=gbrp,split=2[base][subbase];"
            f"[subbase]subtitles='{safe_ass_path}',format=gbrp[withsub];"
            f"[1:v]setpts=PTS-STARTPTS,fps={output_fps},format=rgba,"
            f"scale={width}:{height}:flags=bilinear,format=rgba,alphaextract,format=gbrp[mask];"
            f"[base][withsub][mask]maskedmerge=planes=7,format=yuv420p[v]"
        )
        ffmpeg_cmd = [
            self.ffmpeg_bin, "-y",
            "-i", video_path,
            "-thread_queue_size", "512",
            "-framerate", str(mask_fps),
            "-i", mask_pattern,
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-crf", str(self.bilibili_video_crf),
            "-preset", "slow",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            output_path,
        ]

        try:
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                timeout=900,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            self.logger.warning("⚠️ webmask 弹幕压制超时，将回退普通弹幕压制")
            return False
        except Exception as exc:
            self.logger.warning(f"⚠️ webmask 弹幕压制异常，将回退普通弹幕压制: {exc}")
            return False

        if result.returncode != 0 or not os.path.exists(output_path):
            self.logger.warning(f"⚠️ webmask 弹幕压制失败，返回码: {result.returncode}")
            err_lines = result.stderr.strip().split("\n")
            if err_lines:
                self.logger.warning(f"webmask FFmpeg 倒数错误: {chr(10).join(err_lines[-5:])}")
            return False

        input_duration = video_duration
        output_duration = self._probe_video_duration_seconds(output_path)
        if input_duration and output_duration and output_duration < input_duration * 0.95:
            self.logger.warning(
                "⚠️ webmask 输出视频疑似被截断，将回退普通弹幕压制: "
                f"input={input_duration:.1f}s, output={output_duration:.1f}s"
            )
            return False

        self.logger.info(f"✅ webmask 防遮挡弹幕压制成功: {output_path}")
        return True

    def _process_bilibili_video(self, video_path: str, source_url: str = "") -> Optional[str]:
        """处理 Bilibili 视频：合并弹幕并确保微信兼容。"""
        if not video_path or not os.path.exists(video_path):
            return None

        if not self.bilibili_burn_danmu:
            self.logger.info("ℹ️ 弹幕压制开关已关闭，直接转换为微信兼容格式")
            return self._convert_to_wechat_compatible(video_path)

        # 弹幕文件探测：尝试多种可能的后缀
        base_path = video_path.rsplit('.', 1)[0]
        possible_danmaku_paths = [
            base_path + ".danmaku.ass",
            base_path + ".ass",
            base_path + ".zh-Hans.ass",
            base_path + ".zh-CN.ass"
        ]

        danmaku_path = None
        for p in possible_danmaku_paths:
            if os.path.exists(p):
                danmaku_path = p
                break

        if not danmaku_path:
            self.logger.info("ℹ️ 未发现压制所需的弹幕 (.ass) 文件，仅执行微信格式转换")
            return self._convert_to_wechat_compatible(video_path)

        original_count, kept_count = self._limit_danmaku_ass_file(danmaku_path)
        if original_count > 0:
            if kept_count < original_count:
                self.logger.info(
                    "📉 弹幕限流完成: "
                    f"{original_count} -> {kept_count} "
                    f"(窗口 {self.danmaku_limit_window_seconds:g}s 最多 {self.danmaku_max_per_window} 条)"
                )
            else:
                self.logger.info(
                    "ℹ️ 弹幕数量未超过限流阈值: "
                    f"{kept_count} 条 (窗口 {self.danmaku_limit_window_seconds:g}s 最多 {self.danmaku_max_per_window} 条)"
                )

        output_path = video_path.replace(".mp4", "_danmaku_wechat.mp4")
        try:
            base_path = video_path.rsplit(".", 1)[0]
            webmask_path = base_path + ".webmask"
            mask_svg_dir = base_path + "_webmask_svg"
            if self._burn_bilibili_danmaku_with_webmask(video_path, danmaku_path, source_url, output_path):
                self._remove_path_quietly(video_path)
                self._remove_path_quietly(danmaku_path)
                self._remove_path_quietly(webmask_path)
                self._remove_path_quietly(mask_svg_dir)
                return output_path
            self._remove_path_quietly(webmask_path)
            self._remove_path_quietly(mask_svg_dir)

            self.logger.info(f"🔥 正在压制弹幕并转换格式: {video_path}")

            # FFmpeg 的 subtitles 滤镜路径处理 (Windows 下)
            safe_ass_path = self._escape_ffmpeg_filter_path(danmaku_path)

            # 重新编码以烧入弹幕，同时应用微信兼容参数。
            ffmpeg_cmd = [
                self.ffmpeg_bin, '-y', '-i', video_path,
                '-vf', f"subtitles='{safe_ass_path}'",
                '-c:v', 'libx264',
                '-crf', str(self.bilibili_video_crf),
                '-preset', 'slow',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'copy',
                '-movflags', '+faststart',
                output_path
            ]

            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=600, encoding='utf-8', errors='replace')

            if result.returncode == 0 and os.path.exists(output_path):
                self.logger.info(f"✅ 弹幕压制与转换成功: {output_path}")
                # 清理临时文件
                try: os.remove(video_path)
                except: pass
                try: os.remove(danmaku_path)
                except: pass
                return output_path
            else:
                self.logger.error(f"❌ 弹幕压制失败，返回码: {result.returncode}")
                # 记录最后几行错误输出以供调试
                err_lines = result.stderr.strip().split('\n')
                if err_lines:
                    self.logger.error(f"FFmpeg 倒数错误: {chr(10).join(err_lines[-5:])}")
                return self._convert_to_wechat_compatible(video_path) # 失败则回退
        except Exception as e:
            self.logger.error(f"❌ 弹幕处理异常: {e}")
            return self._convert_to_wechat_compatible(video_path)

    def _convert_to_wechat_compatible(self, input_path: str) -> Optional[str]:
        """将视频转换为微信兼容格式"""
        if not input_path or not os.path.exists(input_path):
            return None

        output_path = input_path.replace(".mp4", "_wechat.mp4")
        try:
            self.logger.info(f"🔄 正在转换视频为微信兼容格式: {input_path}")
            # ffmpeg -i 输入文件名.mp4 -c:v libx264 -pix_fmt yuv420p -c:a copy -movflags +faststart 输出文件名.mp4
            result = subprocess.run([
                self.ffmpeg_bin, '-y', '-i', input_path,
                '-c:v', 'libx264',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'copy',
                '-movflags', '+faststart',
                output_path
            ], capture_output=True, text=True, timeout=600, encoding='utf-8', errors='replace')

            if result.returncode == 0 and os.path.exists(output_path):
                self.logger.info(f"✅ 微信兼容格式转换成功: {output_path}")
                # 尝试删除原文件以节省空间
                try: os.remove(input_path)
                except: pass
                return output_path
            else:
                self.logger.error(f"❌ 微信格式转换失败，返回码: {result.returncode}")
                return input_path # 失败则回退使用原文件
        except Exception as e:
            self.logger.error(f"❌ 微信格式转换异常: {e}")
            return input_path

    # -------------------------
    # Bilibili: Brainmap / ASR
    # -------------------------
    def _get_bili_cookies_path(self) -> str:
        """获取唯一受支持的 B 站 Cookie 文件路径。"""
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(plugin_dir, "cookies.txt")

    def _verify_bili_login_by_cookie_map(self, cookie_map: dict) -> bool:
        """通过 cookie 映射调用 B 站 nav 接口验证登录态"""
        try:
            required = ("SESSDATA", "DedeUserID")
            if not all(cookie_map.get(k) for k in required):
                self.logger.warning("⚠️ Cookie 缺少关键登录字段 (SESSDATA / DedeUserID)")
                return False

            cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_map.items() if v)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://www.bilibili.com/",
                "Cookie": cookie_header,
            }
            resp = requests.get("https://api.bilibili.com/x/web-interface/nav", headers=headers, timeout=8)
            payload = resp.json() if resp.ok else {}
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            is_login = bool(data.get("isLogin"))
            if is_login:
                uname = data.get("uname", "")
                if uname:
                    self.logger.info(f"✅ B 站登录态验证通过，账号: {uname}")
                else:
                    self.logger.info("✅ B 站登录态验证通过")
                return True

            self.logger.warning("⚠️ B 站 nav 接口返回未登录状态")
            return False
        except Exception as e:
            self.logger.warning(f"⚠️ 验证 B 站登录态失败: {e}")
            return False

    def _verify_bili_login_by_cookies(self, cookies: List[dict]) -> bool:
        """通过 cookies 调用 B 站 nav 接口验证登录态"""
        cookie_map = {}
        for c in cookies:
            name = str(c.get("name", "")).strip()
            value = str(c.get("value", "")).strip()
            if name and value:
                cookie_map[name] = value
        return self._verify_bili_login_by_cookie_map(cookie_map)

    def _load_netscape_cookie_map(self, file_path: str) -> dict:
        """读取 Netscape cookies.txt 并转换为 cookie 映射"""
        cookie_map = {}
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 7:
                        continue
                    name = parts[5].strip()
                    value = parts[6].strip()
                    if name and value:
                        cookie_map[name] = value
        except Exception as e:
            self.logger.warning(f"⚠️ 读取 Cookie 文件失败: {file_path}, {e}")
        return cookie_map

    def _verify_bili_login_by_cookie_file(self, file_path: str) -> bool:
        """验证本地 cookie 文件是否仍为有效登录态"""
        if not os.path.exists(file_path):
            return False
        cookie_map = self._load_netscape_cookie_map(file_path)
        if not cookie_map:
            self.logger.warning(f"⚠️ Cookie 文件为空或格式不兼容: {file_path}")
            return False
        return self._verify_bili_login_by_cookie_map(cookie_map)

    def _send_bili_cookie_failure_email(self, chat_name: str) -> None:
        """发送 B 站 cookie 失效告警邮件（带冷却）"""
        if not self.bili_cookie_email_alert_enabled:
            return

        now = time.time()
        cooldown = max(60, int(self.bili_cookie_alert_cooldown_sec or 21600))
        if now - self._bili_cookie_alert_last_ts < cooldown:
            self.logger.info("ℹ️ B站 Cookie 告警邮件处于冷却期，跳过发送")
            return

        self._bili_cookie_alert_last_ts = now
        cookies_path = self._get_bili_cookies_path()
        body = (
            "summary_plus 检测到 B 站 Cookie 失效。\n\n"
            "判定条件：\n"
            "1) 当前本地 Cookie nav 校验失败\n"
            "2) 自动抓取后 nav 再次校验失败\n\n"
            f"会话: {chat_name or '(unknown)'}\n"
            f"Cookie 文件: {cookies_path}\n"
            f"时间戳: {int(now)}\n\n"
            "建议：在调试 Chrome 中重新登录 B 站，再重试。"
        )
        try:
            ok = get_email_service().send_email(body, "🚨 summary_plus B站Cookie失效告警")
            if ok:
                self.logger.info("✅ 已发送 B站 Cookie 失效告警邮件")
            else:
                self.logger.error("❌ B站 Cookie 失效告警邮件发送失败")
        except Exception as e:
            self.logger.error(f"❌ 发送 B站 Cookie 失效告警邮件异常: {e}")

    def _ensure_bili_cookie_login_ready(self, wx: Any = None, chat_name: str = "") -> bool:
        """
        确保 B 站 cookie 可用：
        1) 先校验现有 cookie 文件(nav)
        2) 失败则自动抓取并再次校验(nav)
        3) 两次都失败则触发告警
        """
        cookies_path = self._get_bili_cookies_path()

        if self._verify_bili_login_by_cookie_file(cookies_path):
            return True

        self.logger.warning(f"⚠️ 本地 Cookie nav 校验失败，尝试自动抓取: {cookies_path}")
        login_ok = self._update_bili_cookies_from_browser()
        if login_ok and self._verify_bili_login_by_cookie_file(cookies_path):
            return True

        self.notify_login_failure(wx, chat_name)
        self._send_bili_cookie_failure_email(chat_name=chat_name)
        return False

    def _update_bili_cookies_from_browser(self):
        """用 Selenium 驱动获取 B 站 Cookie 并保存为 Netscape 格式"""
        cookies_path = self._get_bili_cookies_path()
        try:
            driver = self._ensure_driver_available()
            self.logger.info("🔄 正在通过 Selenium 提取 B 站 Cookie...")

            # 记录原始窗口和 URL
            original_handle = driver.current_window_handle
            original_url = driver.current_url

            # 通过 CDP/Selenium 创建工作标签页。直接依赖 window.open 可能被
            # Chrome 拦截，并导致下方从空句柄列表取 [0] 时越界。
            worker_handle = open_blank_worker_tab(driver, self.logger)
            driver.get("https://www.bilibili.com")

            # 等待 B 站加载一点点
            time.sleep(1.5)

            cookies = driver.get_cookies()

            # 关闭临时标签页并切换回原始窗口
            try:
                driver.close()
            except:
                pass
            try:
                driver.switch_to.window(original_handle)
            except:
                pass

            if not cookies:
                self.logger.warning("⚠️ Selenium 未能获取到任何 Cookie")
                return False

            # 关键：先验证登录，再写入文件，避免覆盖已有有效 cookie
            if not self._verify_bili_login_by_cookies(cookies):
                self.logger.warning(
                    f"⚠️ 本次提取的 B 站 Cookie 未通过登录校验，已放弃覆盖: {cookies_path}"
                )
                return False

            self._save_cookies_as_netscape(cookies, cookies_path)
            self.logger.info(f"✅ B 站 Cookie 提取并保存成功: {cookies_path}")
            return True

        except Exception as e:
            self.logger.error(f"❌ 通过 Selenium 更新 B 站 Cookie 发生异常: {e}")
            return False

    def _save_cookies_as_netscape(self, cookies: List[dict], file_path: str):
        """将 Selenium 格式的 cookies 转换为 Netscape (yt-dlp 支持) 格式"""
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# http://curl.haxx.se/rfc/cookie_spec.html\n")
            f.write("# This is a generated file!  Do not edit.\n\n")

            for cookie in cookies:
                # Netscape 格式: domain, is_domain_flag, path, is_secure, expires, name, value
                # domain: .bilibili.com
                # is_domain_flag: TRUE/FALSE (如果以 . 开头通常为 TRUE)
                # is_secure: TRUE/FALSE
                # expires: timestamp (0 means session)

                domain = cookie.get('domain', '')
                # Netscape 格式要求：如果是子域名包含模式，domain 必须以 . 开头，且 flag 为 TRUE
                # 如果 Selenium 返回的 domain 不带 .，我们根据情况补上
                is_domain_flag = 'TRUE'
                if not domain.startswith('.'):
                    if domain.count('.') >= 1: # 比如 bilibili.com -> .bilibili.com
                        domain = '.' + domain
                    else:
                        is_domain_flag = 'FALSE'

                path = cookie.get('path', '/')
                is_secure = 'TRUE' if cookie.get('secure', False) else 'FALSE'

                # expiry 可能为 None (session cookie)
                expiry = cookie.get('expiry')
                expires = int(expiry) if expiry is not None else 0

                name = cookie.get('name', '')
                value = cookie.get('value', '')

                line = f"{domain}\t{is_domain_flag}\t{path}\t{is_secure}\t{expires}\t{name}\t{value}\n"
                f.write(line)

    def notify_login_failure(self, wx: Any, chat_name: str):
        """通知用户 B 站登录失效 (已改为仅记录日志)"""
        msg = "⚠️ 【B站 AI 字幕】检测到 B 站登录已失效或无法从浏览器获取 Cookie。 请确保已经在调试用的 Chrome 中登录了 B 站，且该浏览器没有被完全锁定。"
        self.logger.warning(msg)
        # if wx:
        #     try:
        #         wx.send_message(chat_name, msg)
        #     except Exception as e:
        #         self.logger.error(f"❌ 发送登录失败通知失败: {e}")


    # -------------------------
    # Delegated Services (subtitle/local ASR/mindmap/browser)
    # -------------------------
    async def _generate_bilibili_mindmap_async(self, b_url: str, wx: Any, chat_name: str, article_text: Optional[str] = None):
        try:
            # 1. get subtitles ( if not provided )
            if not article_text:
                article_text = self._bili_get_subtitles(b_url)
            if not article_text:
                if not self.local_asr_enabled:
                    self.logger.info("[*] 未找到字幕且本地 ASR 已关闭，跳过脑图生成。")
                    return
                duration = self._check_bilibili_duration(b_url)
                max_seconds = self.local_asr_max_duration_minutes * 60
                if duration is None:
                    self.logger.warning("[!] 无法确认视频时长，为避免超限，跳过本地 ASR。")
                    return
                if duration > max_seconds:
                    self.logger.info(
                        "[*] 未找到字幕且视频时长 %s 秒超过本地 ASR 上限 %s 秒，跳过。",
                        duration,
                        max_seconds,
                    )
                    return
                self.logger.info("[*] 未找到字幕，切换到本地 SenseVoice ASR...")
                article_text = self._bili_transcribe_local(b_url)

            if not article_text:
                self.logger.warning("[!] 未提取到任何有效文字内容，无法生成思维导图。")
                #if wx: wx.send_message(chat_name, "❌ 未提取到任何有效文字内容，无法生成思维导图。")
                return

            self.logger.info(f"[*] 提取到文字内容长度：{len(article_text)}。正在通过 LLM 构建思维导图结构...")
            # if wx: wx.send_message(chat_name, "⏳ 内容提取成功，正在生成思维导图...")

            # 2. summarize to mindmap
            my_json = self._bili_summarize_to_mindmap(article_text)
            if is_mindmap_skip_response(my_json):
                reason = get_mindmap_skip_reason(my_json) or "内容信息密度不足"
                self.logger.info("[*] 跳过脑图生成：%s", reason)
                return
            if not my_json:
                self.logger.warning("[!] LLM 生成导图失败。")
                return

            base_dir = os.path.join(os.getcwd(), "tmp", "mindmaps")
            os.makedirs(base_dir, exist_ok=True)
            uid = uuid.uuid4().hex[:8]
            layout = self._resolve_mindmap_layout()
            png_file = os.path.join(base_dir, f"bili_map_{int(time.time())}_{uid}_{layout}.png")

            self.logger.info(f"[*] 正在渲染并截取高清脑图（模式: {layout}）...")
            success = await self._render_mindmap_to_image(my_json, png_file)

            if success:
                self.logger.info(f"✨ 流程全部完成！导图图片: {png_file}")
            else:
                self.logger.error("❌ 渲染脑图失败")

            # send pic
            if wx and os.path.exists(png_file):
                if hasattr(wx, "send_files"):
                    wx.send_files(chat_name, [png_file])
                elif hasattr(wx, "SendFiles"):
                    wx.SendFiles(png_file, chat_name)
                else:
                    self.logger.error("❌ 无法发送脑图图片: chat=%s file=%s (wx 实例缺少 send_files/SendFiles)", chat_name, png_file)

        except Exception as e:
            self.logger.error(f"[❌] 脑图生成流程执行出错: {e}", exc_info=True)

    async def _generate_youtube_mindmap_async(self, yt_url: str, wx: Any, chat_name: str):
        """异步处理 YouTube 脑图逻辑：获取字幕 -> LLM 总结 -> 生成脑图 -> 发送"""
        try:
            self.logger.info(f"🎬 开始处理 YouTube 脑图: {yt_url}")

            # 1. 获取字幕
            try:
                proxy_dict = None
                if self.yt_transcript_proxy:
                    proxy_dict = {
                        "http": self.yt_transcript_proxy,
                        "https": self.yt_transcript_proxy,
                    }

                article_text = get_best_transcript_text(
                    yt_url,
                    proxy_urls=proxy_dict,
                    local_proxy_port=self.yt_transcript_local_port,
                    debug=True,
                )
                if not article_text:
                    self.logger.warning(f"⚠️ YouTube 视频未获取到有效字幕内容: {yt_url}")
                    return
            except Exception as e:
                self.logger.error(f"❌ 获取 YouTube 字幕失败: {e}")
                return

            # 2. 总结生成脑图 JSON
            mindmap_json = self._yt_summarize_to_mindmap(article_text)
            if is_mindmap_skip_response(mindmap_json):
                reason = get_mindmap_skip_reason(mindmap_json) or "内容信息密度不足"
                self.logger.info("⚠️ YouTube 内容跳过脑图: %s", reason)
                return
            if mindmap_json:
                # 3. 生成预览图
                base_dir = os.path.join(os.getcwd(), "tmp", "mindmaps")
                os.makedirs(base_dir, exist_ok=True)

                uid = uuid.uuid4().hex[:8]
                layout = self._resolve_mindmap_layout()
                png_file = os.path.join(base_dir, f"yt_map_{int(time.time())}_{uid}_{layout}.png")

                success = await self._render_mindmap_to_image(mindmap_json, png_file)

                if success and os.path.exists(png_file):
                    # 4. 发送图片
                    if wx:
                        if hasattr(wx, "send_files"):
                            wx.send_files(chat_name, [png_file])
                        elif hasattr(wx, "SendFiles"):
                            wx.SendFiles(png_file, chat_name)
                    self.logger.info(f"✅ YouTube 脑图发送成功: {png_file}")
                else:
                    self.logger.error("❌ YouTube 脑图渲染失败")
            else:
                self.logger.warning("⚠️ YouTube 脑图总结内容为空")

        except Exception as e:
            self.logger.error(f"❌ 生成 YouTube 脑图流程异常: {e}", exc_info=True)

    def _yt_summarize_to_mindmap(self, text: str):
        """针对 YouTube 内容生成脑图 JSON"""
        try:
            return self._summarize_to_mindmap_json(
                text=text,
                system_prompt=self.prompt_youtube_mindmap or MINDMAP_SYSTEM_PROMPT_DEFAULT,
                call_type="youtube_mindmap",
            )
        except Exception as e:
            self.logger.error(f"❌ YouTube 脑图 LLM 总结失败: {e}")
            return None


    def _bili_transcribe_local(self, url: str) -> str:
        return bili_transcribe_local(
            url=url,
            cookies_path=self._get_bili_cookies_path(),
            yt_dlp_bin=self.yt_dlp_bin,
            ffmpeg_bin=self.ffmpeg_bin,
            runtime_path=self.local_asr_runtime_path,
            model_path=self.local_asr_model_path,
            vad_path=self.local_asr_vad_path,
            timeout_sec=self.local_asr_timeout_seconds,
            cache_enabled=True,
            logger=self.logger,
        )

    def _bili_get_subtitles(self, url: str) -> Optional[str]:
        return bili_get_subtitles(
            url=url,
            cookies_path=self._get_bili_cookies_path(),
            logger=self.logger,
        )

    def _bili_summarize_to_mindmap(self, text: str):
        return self._summarize_to_mindmap_json(
            text=text,
            system_prompt=self.prompt_bilibili_mindmap or MINDMAP_SYSTEM_PROMPT_DEFAULT,
            call_type="bilibili_mindmap",
        )

    def _summarize_to_mindmap_json(self, text: str, system_prompt: str, call_type: str):
        return summarize_to_mindmap_json(
            llm_manager=self.llm_manager,
            text=text,
            system_prompt=system_prompt,
            call_type=call_type,
            plugin_name="summary_plus",
        )

    def _resolve_mindmap_layout(self) -> str:
        return resolve_mindmap_layout(self.mindmap_layout)

    async def _render_mindmap_to_image(self, mindmap_json: dict, png_path: str) -> bool:
        return await render_mindmap_to_image(
            mindmap_json=mindmap_json,
            png_path=png_path,
            layout=self.mindmap_layout,
            js_d3=self.js_d3,
            js_markmap_lib=self.js_markmap_lib,
            js_markmap_view=self.js_markmap_view,
            logger=self.logger,
        )


    # -------------------------
    # 浏览器摘要（Selenium + OpenAI）
    # -------------------------
    def summarize_url(
        self,
        url: str,
        is_link_message: bool = False,
        chat_name: str = "",
        sender: str = "",
    ) -> Optional[str]:
        actual_url = None if url == "LINK_MESSAGE_CLICKED" else url
        return self._browser_summarize(
            actual_url,
            is_link_message,
            chat_name=chat_name,
            sender=sender,
        )

    def translate_text_for_special_group(self, chinese_text: str) -> Optional[str]:
        try:
            target_lang = self.special_translation_target_language
            prompt = f"你是专业翻译，请将下面这段中文翻译成{target_lang}，保持原文段落和格式，保持语言地道、自然，不要附加任何说明，只输出翻译结果。"

            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": chinese_text},
            ]

            response = self.llm_manager.call(
                plugin_name="summary_plus",
                call_type="translate",
                messages=messages
            )
            return response.strip()
        except Exception as e:
            self.logger.error(f"❌ 特殊群翻译失败: {e}")
            return None

    def _browser_summarize(
        self,
        url: Optional[str],
        is_link_message: bool = False,
        chat_name: str = "",
        sender: str = "",
    ) -> Optional[str]:
        return browser_summarize(
            self,
            url,
            is_link_message,
            chat_name=chat_name,
            sender=sender,
        )

    def _xhs_ytdlp_info(
        self,
        share_url: str,
        timeout_sec: int = 60,
        cookie_args: Optional[List[str]] = None,
    ) -> Optional[dict]:
        try:
            result = self._run_platform_ytdlp(
                "xiaohongshu",
                [
                    "--ignore-no-formats-error",
                    "--dump-single-json",
                    share_url,
                ],
                timeout_sec=timeout_sec,
                cookie_args=cookie_args,
            )
            if result.returncode != 0:
                self._log_ytdlp_failure("小红书元数据", result)
                return None
            payload = json.loads((result.stdout or "").strip())
            return payload if isinstance(payload, dict) else None
        except (json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            self.logger.warning("⚠️ 小红书 yt-dlp 元数据解析失败: %s", exc)
            return None
        except Exception as exc:
            self.logger.warning("⚠️ 小红书 yt-dlp 元数据获取异常: %s", exc)
            return None

    def _xhs_ytdlp_image_urls(self, info: Any) -> List[str]:
        """Deduplicate urlDefault/urlPre thumbnail pairs while preserving note order."""
        if not isinstance(info, dict):
            return []
        selected: Dict[str, Tuple[int, str]] = {}
        for thumbnail in info.get("thumbnails") or []:
            if not isinstance(thumbnail, dict):
                continue
            url = str(thumbnail.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            parsed = urlparse(url)
            variant = re.search(r"^(.*)!nd_(dft|prv)_", parsed.path, re.IGNORECASE)
            if variant:
                key = f"{parsed.netloc.casefold()}{variant.group(1)}"
                score = 2 if variant.group(2).casefold() == "dft" else 1
            else:
                key = url
                score = 0
            current = selected.get(key)
            if current is None or score > current[0]:
                selected[key] = (score, url)
        return [url for _score, url in selected.values()]

    def _process_xhs_image_urls(self, image_urls: List[str], uid: str) -> Optional[str]:
        image_urls = image_urls[:self.xhs_max_images]
        if not image_urls:
            return None

        tmp_dir = os.path.join(os.getcwd(), "tmp", "images")
        os.makedirs(tmp_dir, exist_ok=True)
        if len(image_urls) == 1:
            raw_path = os.path.join(tmp_dir, f"temp_{uid}_ytdlp_raw")
            output_path = os.path.join(tmp_dir, f"xhs_img_{uid}.jpg")
            try:
                self._xhs_download_file(image_urls[0], raw_path)
                self._xhs_convert_to_jpg(raw_path, output_path)
                return output_path
            finally:
                self._remove_path_quietly(raw_path)

        self.logger.info(
            "检测到 yt-dlp 小红书多图（%s 张），准备合并为长图",
            len(image_urls),
        )
        temp_files: List[str] = []
        converted_images: List[str] = []
        try:
            for index, image_url in enumerate(image_urls):
                raw_path = os.path.join(tmp_dir, f"temp_{uid}_ytdlp_{index}_raw")
                jpg_path = os.path.join(tmp_dir, f"temp_{uid}_ytdlp_{index}.jpg")
                self._xhs_download_file(image_url, raw_path)
                temp_files.append(raw_path)
                self._xhs_convert_to_jpg(raw_path, jpg_path)
                temp_files.append(jpg_path)
                converted_images.append(jpg_path)
            if not converted_images:
                return None
            output_path = os.path.join(tmp_dir, f"xhs_long_img_{uid}.jpg")
            self._merge_images_vertically(converted_images, output_path)
            return output_path
        except Exception as exc:
            self.logger.warning("⚠️ yt-dlp 小红书图片处理失败，将回退 TikHub: %s", exc)
            return None
        finally:
            for temp_path in temp_files:
                self._remove_path_quietly(temp_path)

    def _download_xhs_video_with_ytdlp(
        self,
        share_url: str,
        uid: str,
        info: Optional[dict] = None,
        timeout_sec: int = 240,
        cookie_args: Optional[List[str]] = None,
    ) -> Optional[str]:
        tmp_dir = os.path.join(os.getcwd(), "tmp", "videos")
        os.makedirs(tmp_dir, exist_ok=True)
        output_template = os.path.join(tmp_dir, f"xhs_ytdlp_{uid}_{uuid.uuid4().hex[:8]}.%(ext)s")
        info_path = ""
        try:
            input_args: List[str]
            if info:
                handle, info_path = tempfile.mkstemp(
                    prefix="summary_plus_xhs_",
                    suffix=".info.json",
                )
                os.close(handle)
                with open(info_path, "w", encoding="utf-8") as info_file:
                    json.dump(info, info_file, ensure_ascii=False)
                try:
                    os.chmod(info_path, 0o600)
                except OSError:
                    pass
                input_args = ["--load-info-json", info_path]
            else:
                input_args = [share_url]
            result = self._run_platform_ytdlp(
                "xiaohongshu",
                [
                    "--ffmpeg-location",
                    self.ffmpeg_bin,
                    "--format",
                    "b[vcodec=EF4]/b[vcodec=h264]/b[vcodec^=avc]/b[ext=mp4]/b",
                    "--merge-output-format",
                    "mp4",
                    "--remux-video",
                    "mp4",
                    "--no-write-thumbnail",
                    "--no-progress",
                    "-o",
                    output_template,
                    *input_args,
                ],
                timeout_sec=timeout_sec,
                cookie_args=cookie_args,
            )
            if result.returncode != 0:
                self._log_ytdlp_failure("小红书视频", result)
                return None
            video_path = self._find_ytdlp_output(output_template)
            if not video_path:
                return None
            codec = self._probe_video_codec(video_path)
            if codec and codec not in {"h264", "avc1"}:
                self.logger.info("🔄 小红书 yt-dlp 输出编码为 %s，转换为微信兼容 H.264", codec)
                video_path = self._convert_to_wechat_compatible(video_path) or video_path
            return video_path
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            self.logger.warning("⚠️ 小红书 yt-dlp 视频下载失败: %s", exc)
            return None
        except Exception as exc:
            self.logger.warning("⚠️ 小红书 yt-dlp 视频下载异常: %s", exc)
            return None
        finally:
            if info_path:
                self._remove_path_quietly(info_path)

    def _process_xhs_note_with_ytdlp(self, share_url: str) -> Tuple[bool, Optional[str]]:
        self.logger.info("📥 小红书优先使用 yt-dlp 处理: %s", share_url)
        try:
            with ytdlp_browser_cookie_args(
                platform="xiaohongshu",
                debug_port=self.chrome_debug_port,
                user_data_dir=self.chrome_user_data_dir,
                profile_dir=self.chrome_profile_dir,
                logger=self.logger,
            ) as cookie_args:
                info = self._xhs_ytdlp_info(share_url, cookie_args=cookie_args)
                if not info:
                    return False, None

                uid = str(
                    info.get("id")
                    or self._extract_xhs_note_id(share_url)
                    or uuid.uuid4().hex[:8]
                )
                formats = info.get("formats") or []
                if isinstance(formats, list) and formats:
                    duration = self._xhs_video_duration_seconds(info)
                    if not duration:
                        duration = max(
                            (self._xhs_video_duration_seconds(item) for item in formats),
                            default=0,
                        )
                    if duration > self.xhs_max_download_duration:
                        self.logger.info(
                            "跳过处理(yt-dlp): 视频时长为 %ss，超过 %ss",
                            duration,
                            self.xhs_max_download_duration,
                        )
                        return True, None
                    video_path = self._download_xhs_video_with_ytdlp(
                        share_url,
                        uid,
                        info=info,
                        cookie_args=cookie_args,
                    )
                    if video_path:
                        self.logger.info("✅ 小红书 yt-dlp 视频下载成功: %s", video_path)
                        return True, video_path
                    return False, None

                image_urls = self._xhs_ytdlp_image_urls(info)
                if image_urls:
                    self.logger.info(
                        "🖼️ 小红书 yt-dlp 识别到 %s 张去重后的正文图片",
                        len(image_urls),
                    )
                    image_path = self._process_xhs_image_urls(image_urls, uid)
                    if image_path:
                        self.logger.info("✅ 小红书 yt-dlp 图片处理成功: %s", image_path)
                        return True, image_path
                return False, None
        except Exception as exc:
            self.logger.warning("⚠️ 小红书 yt-dlp 处理异常: %s", exc)
            return False, None

    def _xhs_download_file(self, url: str, save_path: str):
        """下载文件并保存 (带 Headers 以防止 405)"""
        self.logger.info(f"正在下载: {url}")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.xiaohongshu.com/"
        }
        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                response = requests.get(url, headers=headers, stream=True, timeout=(15, 90))
                response.raise_for_status()
                with open(save_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                self.logger.info(f"保存成功: {save_path}")
                return
            except Exception as e:
                last_err = e
                self.logger.warning(f"⚠️ 下载失败 (requests 第 {attempt + 1}/2 次): {e}")
                time.sleep(1 + attempt)

        curl_bin = "curl.exe" if self.is_wsl else "curl"
        try:
            subprocess.run(
                [
                    curl_bin,
                    "--max-time",
                    "180",
                    "-L",
                    "-A",
                    headers["User-Agent"],
                    "-e",
                    headers["Referer"],
                    url,
                    "-o",
                    save_path,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=210,
            )
            if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                self.logger.info(f"保存成功(curl fallback): {save_path}")
                return
            raise RuntimeError("curl fallback produced empty file")
        except Exception as e:
            self.logger.error(f"❌ 下载失败(curl fallback): {e}")
            if last_err:
                raise last_err
            raise

    def _merge_images_vertically(self, image_paths: list, output_path: str, target_width: int = 1080, margin: int = 40):
        """将多张图片缩放到相同宽度（算上边框）后垂直拼接，不裁剪，添加等宽边框"""
        self.logger.info(f"正在合并 {len(image_paths)} 张图片并添加边框...")
        images = []

        # 有效图片宽度 = 总宽度 - 左右边框
        inner_width = target_width - 2 * margin

        try:
            total_height = margin # 初始顶部边距
            for p in image_paths:
                img = Image.open(p)
                w, h = img.size
                ratio = inner_width / w
                new_h = int(h * ratio)

                img_resized = img.resize((inner_width, new_h), Image.Resampling.LANCZOS)
                images.append(img_resized)

                # 图片高度 + 图间/底部边距
                total_height += new_h + margin

            # 创建空白画布
            result = Image.new('RGB', (target_width, total_height), (255, 255, 255))

            current_y = margin
            for img in images:
                result.paste(img, (margin, current_y))
                current_y += img.height + margin

            result.save(output_path, "JPEG", quality=95)
            self.logger.info(f"等距长图已保存: {output_path}")
        finally:
            for img in images:
                img.close()

    def _xhs_convert_to_jpg(self, input_path: str, output_path: str):
        """使用 ffmpeg 将图片转换为 jpg"""
        self.logger.info(f"正在进行格式转换: {input_path} -> {output_path}")
        try:
            # -y 表示覆盖输出文件
            subprocess.run([self.ffmpeg_bin, "-y", "-i", input_path, output_path], check=True, capture_output=True)
            self.logger.info("转换完成")
        except subprocess.CalledProcessError as e:
            self.logger.error(f"转换失败: {e.stderr.decode()}")
            raise

    def _xhs_pick_url(self, value: Any) -> Optional[str]:
        """从 TikHub 新旧结构里提取可下载 URL。"""
        if isinstance(value, str):
            value = value.strip()
            return value if value.startswith(("http://", "https://")) else None
        if isinstance(value, list):
            for item in value:
                url = self._xhs_pick_url(item)
                if url:
                    return url
            return None
        if isinstance(value, dict):
            url_keys = (
                "url",
                "url_default",
                "url_pre",
                "url_size_large",
                "original",
                "origin",
                "master_url",
                "masterUrl",
                "backup_url",
                "backupUrl",
                "backup_urls",
                "backupUrls",
                "main_url",
                "mainUrl",
                "h265_url",
                "h265Url",
                "h264_url",
                "h264Url",
            )
            for key in url_keys:
                url = self._xhs_pick_url(value.get(key))
                if url:
                    return url
            nested_keys = (
                "url_list",
                "url_info_list",
                "info_list",
                "play_addr",
                "download_addr",
                "stream",
                "media",
                "video",
            )
            for key in nested_keys:
                url = self._xhs_pick_url(value.get(key))
                if url:
                    return url
        return None

    def _xhs_extract_notes_from_response(self, res_json: Any) -> list:
        """兼容 TikHub V1 新结构、旧结构和 App V2 结构。"""
        notes = []
        seen = set()

        def looks_like_note(item: dict) -> bool:
            note_keys = {
                "note_id",
                "id",
                "type",
                "image_list",
                "images_list",
                "video_info",
                "video_info_v2",
                "video",
            }
            return bool(note_keys.intersection(item.keys()))

        def visit(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return

            if looks_like_note(value):
                marker = value.get("note_id") or value.get("id") or id(value)
                if marker not in seen:
                    seen.add(marker)
                    notes.append(value)

            for key in (
                "data",
                "note",
                "note_detail",
                "note_info",
                "note_list",
                "items",
                "list",
                "result",
            ):
                if key in value:
                    visit(value.get(key))

        visit(res_json.get("data") if isinstance(res_json, dict) else res_json)
        return notes

    def _xhs_has_usable_media(self, note: dict) -> bool:
        images = note.get("image_list") or note.get("images_list") or note.get("images") or []
        if isinstance(images, dict):
            images = [images]
        video_info = self._xhs_get_video_info(note)
        has_image_url = any(self._xhs_pick_url(img) for img in images)
        return bool(has_image_url or self._xhs_pick_url(video_info))

    def _xhs_get_video_info(self, note: dict) -> dict:
        """兼容 TikHub App V2 的 video_info_v2 和旧结构。"""
        if not isinstance(note, dict):
            return {}
        video_info = note.get("video_info_v2") or note.get("video_info") or note.get("video") or {}
        return video_info if isinstance(video_info, dict) else {}

    def _xhs_target_note_id(self, share_url: str) -> Optional[str]:
        return self._extract_xhs_note_id(self._normalize_xhs_share_url(share_url))

    def _xhs_select_target_notes(self, notes: list, target_note_id: Optional[str]) -> list:
        """有目标 ID 时严格只保留目标笔记，绝不使用接口返回的关联笔记代替。"""
        valid_notes = [note for note in notes if isinstance(note, dict)]
        if not target_note_id:
            return valid_notes

        target_notes = [
            note
            for note in valid_notes
            if str(note.get("note_id") or note.get("id") or "") == target_note_id
        ]
        ignored_ids = [
            str(note.get("note_id") or note.get("id") or "<unknown>")
            for note in valid_notes
            if str(note.get("note_id") or note.get("id") or "") != target_note_id
        ]
        if ignored_ids:
            self.logger.info(
                "小红书严格目标匹配: target=%s, ignored_related=%s",
                target_note_id,
                ignored_ids,
            )
        if not target_notes:
            self.logger.warning(
                "⚠️ TikHub 响应未包含目标小红书笔记: target=%s, candidates=%s",
                target_note_id,
                ignored_ids,
            )
        return target_notes

    def _xhs_static_image_url(self, image_info: Any) -> Optional[str]:
        """只提取静态图 URL，避免多图 live_photo 场景误取动态视频。"""
        if isinstance(image_info, str):
            image_info = image_info.strip()
            return image_info if image_info.startswith(("http://", "https://")) else None
        if not isinstance(image_info, dict):
            return None
        for key in ("url_size_large", "original", "url", "url_default", "url_pre", "origin"):
            url = self._xhs_pick_url(image_info.get(key))
            if url:
                return url
        image_node = image_info.get("image")
        if isinstance(image_node, dict):
            for key in ("url_size_large", "original", "url", "url_default", "url_pre", "origin"):
                url = self._xhs_pick_url(image_node.get(key))
                if url:
                    return url
        return None

    def _xhs_extract_live_photo_video_url(self, image_info: Any) -> Optional[str]:
        """单张 live 图优先返回动态视频；多图场景不调用。"""
        if not isinstance(image_info, dict):
            return None
        live_photo = image_info.get("live_photo")
        if not isinstance(live_photo, dict):
            return None
        return self._xhs_extract_video_url(live_photo)

    def _xhs_request_note_endpoint(
        self,
        token: str,
        endpoint_name: str,
        api_url: str,
        params: dict,
        target_note_id: Optional[str],
    ) -> Optional[dict]:
        """请求一个 TikHub App V2 笔记端点，并过滤不可用/非目标响应。"""
        headers = {"Authorization": f"Bearer {token}"}
        retry_count = 3
        retry_delay = 2

        for attempt in range(retry_count):
            try:
                self.logger.info(
                    f"正在请求 TikHub 小红书{endpoint_name}接口 "
                    f"(尝试 {attempt + 1}/{retry_count}): params={params}"
                )
                response = requests.get(api_url, headers=headers, params=params, timeout=30)
                if response.status_code == 200:
                    res_json = response.json()
                    notes = self._xhs_extract_notes_from_response(res_json)
                    notes = self._xhs_select_target_notes(notes, target_note_id)
                    if notes:
                        return res_json
                    self.logger.warning(
                        f"⚠️ TikHub 小红书{endpoint_name}接口返回成功但未找到目标笔记"
                    )
                    # TikHub 会对这种 HTTP 200 的上游空结果正常计费；相同参数
                    # 立即重试只会重复扣费，不会改变结果。
                    return None
                else:
                    self.logger.warning(
                        f"⚠️ TikHub 小红书{endpoint_name}接口请求失败: "
                        f"{response.status_code} - {response.text}"
                    )
                    # 参数或路由错误重试不会改善结果，也会拖长摘要链路。
                    if response.status_code in (400, 401, 403, 404, 422):
                        break
            except Exception as e:
                self.logger.warning(f"⚠️ TikHub 小红书{endpoint_name}接口请求异常: {e}")

            if attempt < retry_count - 1:
                time.sleep(retry_delay)

        return None

    def _xhs_fetch_note_response(self, token: str, share_url: str) -> Optional[dict]:
        """按 TikHub App V2 文档流程获取笔记；视频用专用详情接口补全播放地址。"""
        normalized_share_url = self._normalize_xhs_share_url(share_url)
        note_id = self._extract_xhs_note_id(normalized_share_url)

        parsed = urlparse(normalized_share_url)
        is_xhs_long_url = parsed.netloc.lower().endswith("xiaohongshu.com")
        if is_xhs_long_url and not note_id:
            self.logger.warning(
                "⚠️ 小红书长链接缺少笔记 ID，跳过 TikHub 请求以避免无效调用: %s",
                normalized_share_url,
            )
            return None

        if note_id:
            # 文档要求二选一并优先 note_id；不要同时携带过期的分享参数。
            params = {"note_id": note_id}
        else:
            params = {"share_text": normalized_share_url}

        # 2026-08-14 文档推荐：先用图文详情识别类型；视频再用同一 note_id
        # 请求视频详情。已下线并持续返回 404 的 App V1 get_note_info 不再兜底。
        image_response = self._xhs_request_note_endpoint(
            token,
            "App V2 图文",
            TIKHUB_ENDPOINT_XHS_IMAGE_NOTE,
            params,
            note_id,
        )
        if not image_response:
            return None

        image_notes = self._xhs_extract_notes_from_response(image_response)
        image_notes = self._xhs_select_target_notes(image_notes, note_id)
        primary_note = image_notes[0] if image_notes else {}
        note_type = str(primary_note.get("type") or primary_note.get("note_type") or "").lower()

        if note_type != "video":
            if any(self._xhs_has_usable_media(note) for note in image_notes):
                return image_response
            self.logger.warning("⚠️ TikHub 小红书App V2 图文接口未返回可用媒体字段")
            return None

        video_note_id = str(primary_note.get("note_id") or primary_note.get("id") or note_id or "").strip()
        video_params = {"note_id": video_note_id} if video_note_id else params
        video_response = self._xhs_request_note_endpoint(
            token,
            "App V2 视频",
            TIKHUB_ENDPOINT_XHS_VIDEO_NOTE,
            video_params,
            video_note_id or note_id,
        )
        if not video_response:
            return None

        video_notes = self._xhs_extract_notes_from_response(video_response)
        video_notes = self._xhs_select_target_notes(video_notes, video_note_id or note_id)
        if any(self._xhs_extract_video_url(self._xhs_get_video_info(note)) for note in video_notes):
            return video_response

        self.logger.warning("⚠️ TikHub 小红书App V2 视频接口未返回可用视频播放地址")

        return None

    def _xhs_video_duration_seconds(self, video_info: Any) -> float:
        """兼容 TikHub 和小红书 H5 __INITIAL_STATE__ 的视频时长字段。"""
        if not isinstance(video_info, dict):
            return 0

        def normalize_duration(value: Any) -> Optional[float]:
            if value is None:
                return None
            try:
                duration = float(value)
                return duration / 1000 if duration > 1000 else duration
            except (TypeError, ValueError):
                return None

        for key in ("duration", "duration_sec", "duration_seconds"):
            duration = normalize_duration(video_info.get(key))
            if duration:
                return duration

        for path in (
            ("capa", "duration"),
            ("media", "video", "duration"),
        ):
            node: Any = video_info
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            duration = normalize_duration(node)
            if duration:
                return duration
        return 0

    def _xhs_extract_video_url(self, video_info: Any) -> Optional[str]:
        """提取视频 URL；优先 H.264 MP4，避免 H.265 在微信/部分播放器兼容性差。"""
        if not isinstance(video_info, dict):
            return None

        candidates = []

        def collect(value: Any, meta: str = "") -> None:
            if isinstance(value, list):
                for item in value:
                    collect(item, meta)
                return
            url = self._xhs_pick_url(value)
            if url:
                candidates.append((url, meta or str(value)))

        streams = [video_info.get("stream")]
        media = video_info.get("media")
        if isinstance(media, dict):
            streams.append(media.get("stream"))
            # TikHub 2026-08 新结构：video_info_v2.media.video.stream.h264。
            media_video = media.get("video")
            if isinstance(media_video, dict):
                streams.append(media_video.get("stream"))

        for stream in streams:
            if not isinstance(stream, dict):
                continue
            for codec_key in ("h264", "h265", "h266", "av1"):
                collect(stream.get(codec_key), codec_key)

        for key in (
            "url_info_list",
            "url_list",
            "video_list",
            "stream_list",
            "media",
            "play_addr",
            "download_addr",
            "video",
            "url",
        ):
            collect(video_info.get(key), key)

        def is_mp4(url: str) -> bool:
            return ".mp4" in urlparse(url).path.lower()

        for url, meta in candidates:
            if is_mp4(url) and ("h264" in meta.lower() or "_259.mp4" in url or "streamtype': 259" in meta.lower()):
                return url
        for url, _meta in candidates:
            if is_mp4(url):
                return url
        return candidates[0][0] if candidates else None

    def _xhs_extract_initial_state_json(self, page_html: str) -> Optional[dict]:
        """从小红书 H5 页面提取 window.__INITIAL_STATE__（自动修复 JS 残留如 undefined）。"""
        marker = "window.__INITIAL_STATE__="
        start = page_html.find(marker)
        if start < 0:
            return None
        start += len(marker)
        end = page_html.find("</script>", start)
        if end < 0:
            return None
        raw = page_html[start:end].strip().rstrip(";")
        # JavaScript literal → valid JSON: replace undefined with null
        raw = re.sub(r"\bundefined\b", "null", raw)
        try:
            return json.loads(raw)
        except Exception as e:
            self.logger.warning(f"⚠️ 解析小红书 H5 INITIAL_STATE 失败: {e}")
            return None

    def _xhs_find_video_info_in_state(self, state: Any) -> Optional[dict]:
        """递归扫描 INITIAL_STATE，找到包含真实 stream/masterUrl 的 video 对象。"""
        seen = set()

        def has_video_url(value: Any) -> bool:
            if isinstance(value, dict):
                url = self._xhs_extract_video_url(value)
                if url and (".mp4" in urlparse(url).path.lower() or "sns-video" in url):
                    return True
                return any(has_video_url(v) for v in value.values())
            if isinstance(value, list):
                return any(has_video_url(v) for v in value)
            return False

        def visit(value: Any) -> Optional[dict]:
            obj_id = id(value)
            if obj_id in seen:
                return None
            seen.add(obj_id)

            if isinstance(value, dict):
                video = value.get("video") or value.get("video_info")
                if isinstance(video, dict) and has_video_url(video):
                    return video
                if has_video_url(value) and ("stream" in value or "media" in value):
                    return value
                for child in value.values():
                    found = visit(child)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = visit(child)
                    if found:
                        return found
            return None

        return visit(state)

    def _xhs_fetch_video_note_from_h5(self, share_url: str) -> Optional[dict]:
        """直连小红书 H5 页面，绕过第三方 API 提取视频流；仅用于视频兜底。"""
        normalized_share_url = self._normalize_xhs_share_url(share_url)
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36",
            "Referer": "https://www.xiaohongshu.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            self.logger.info(f"正在直连小红书 H5 页面提取视频: {normalized_share_url}")
            response = requests.get(normalized_share_url, headers=headers, timeout=30, allow_redirects=True)
            response.raise_for_status()
            state = self._xhs_extract_initial_state_json(response.text)
            video_info = self._xhs_find_video_info_in_state(state) if state else None
            if not video_info or not self._xhs_extract_video_url(video_info):
                self.logger.warning("⚠️ 小红书 H5 页面中未找到可用视频流")
                return None
            uid = self._extract_xhs_note_id(response.url) or self._extract_xhs_note_id(normalized_share_url) or uuid.uuid4().hex[:8]
            return {"type": "video", "note_id": uid, "video_info": video_info}
        except Exception as e:
            self.logger.warning(f"⚠️ 直连小红书 H5 视频提取失败: {e}")
            return None

    def _xhs_url_looks_video(self, share_url: str) -> bool:
        try:
            parsed = urlparse(self._normalize_xhs_share_url(share_url))
            qs = parse_qs(parsed.query)
            return (qs.get("type") or [""])[0].lower() == "video"
        except Exception:
            return "type=video" in (share_url or "").lower()

    def process_xhs_note(self, share_url: str) -> Optional[str]:
        """获取小红书笔记并根据规则处理，返回文件路径。

        优先使用 yt-dlp；失败后回退 TikHub，并保留原有 H5 视频兜底。
        """
        handled, ytdlp_path = self._process_xhs_note_with_ytdlp(share_url)
        if handled:
            return ytdlp_path
        self.logger.info("🔄 小红书 yt-dlp 处理失败，回退 TikHub/H5")

        token = (os.getenv("TIKHUB_API_TOKEN") or "").strip()
        target_note_id = self._xhs_target_note_id(share_url)

        # ---- 视频 H5 直解析兜底（无论有无 TikHub token 都可尝试） ----
        h5_video_note: Optional[dict] = None

        def _try_h5_video_fallback() -> Optional[dict]:
            nonlocal h5_video_note
            if h5_video_note is not None:
                return h5_video_note
            if not self._xhs_url_looks_video(share_url):
                h5_video_note = False  # type: ignore
                return None
            note_data = self._xhs_fetch_video_note_from_h5(share_url)
            h5_video_note = note_data or False  # type: ignore
            return note_data

        # ---- 先走 TikHub API ----
        notes: list = []
        video_from_tikhub_failed = False
        if token:
            res_json = self._xhs_fetch_note_response(token, share_url)
            notes = self._xhs_extract_notes_from_response(res_json) if res_json else []
            notes = self._xhs_select_target_notes(notes, target_note_id)
            if notes:
                # 检查 TikHub 视频笔记是否能提取到有效 URL
                for note in notes:
                    note_type = str(note.get("type") or note.get("note_type") or "").lower()
                    video_info = self._xhs_get_video_info(note)
                    if note_type == "video" or video_info:
                        selected_url = self._xhs_extract_video_url(video_info)
                        if not selected_url:
                            video_from_tikhub_failed = True
                            self.logger.info("TikHub 返回视频笔记但无法提取有效 MP4 URL，将尝试 H5 兜底")
                        break

        # ---- 视频兜底：TikHub 无结果或无有效视频 URL 时走 H5 ----
        h5_note = (
            _try_h5_video_fallback()
            if video_from_tikhub_failed or not notes
            else None
        )
        if h5_note:
            video_info = h5_note.get("video_info", {})
            duration = self._xhs_video_duration_seconds(video_info)
            if duration > self.xhs_max_download_duration:
                self.logger.info(
                    f"跳过处理(H5): 视频时长为 {duration}s，"
                    f"超过 {self.xhs_max_download_duration}s"
                )
                return None
            selected_url = self._xhs_extract_video_url(video_info)
            if selected_url:
                uid = h5_note.get("note_id", uuid.uuid4().hex[:8])
                tmp_dir = os.path.join(os.getcwd(), "tmp", "videos")
                os.makedirs(tmp_dir, exist_ok=True)
                filepath = os.path.join(tmp_dir, f"xhs_video_{uid}.mp4")
                self._xhs_download_file(selected_url, filepath)
                self.logger.info(f"✅ H5 兜底下载小红书视频成功: {filepath}")
                return filepath
            self.logger.warning("H5 兜底也未找到有效视频 URL")
            return None

        # ---- 图片处理：仅走 TikHub API ----
        if not notes:
            if not token:
                self.logger.warning("⚠️ 缺少 TikHub Token：请在 .env 设置 TIKHUB_API_TOKEN（图片提取需要）")
            else:
                self.logger.warning("未获取到有效小红书数据")
            return None

        try:
            for note in notes:
                note_type = str(note.get("type") or note.get("note_type") or "").lower()
                images_list = note.get("image_list") or note.get("images_list") or note.get("images") or []
                if isinstance(images_list, dict):
                    images_list = [images_list]
                video_info = self._xhs_get_video_info(note)
                uid = note.get("note_id") or note.get("id") or uuid.uuid4().hex[:8]

                # 规则 1: 视频处理（TikHub 成功路径）
                if video_info or note_type == "video":
                    duration = self._xhs_video_duration_seconds(video_info)
                    if duration > self.xhs_max_download_duration:
                        self.logger.info(
                            f"跳过处理: 目标视频 {uid} 时长为 {duration}s，"
                            f"超过 {self.xhs_max_download_duration}s"
                        )
                        return None

                    selected_url = self._xhs_extract_video_url(video_info)
                    if selected_url:
                        tmp_dir = os.path.join(os.getcwd(), "tmp", "videos")
                        os.makedirs(tmp_dir, exist_ok=True)
                        filename = f"xhs_video_{uid}.mp4"
                        filepath = os.path.join(tmp_dir, filename)
                        self._xhs_download_file(selected_url, filepath)
                        return filepath
                    else:
                        self.logger.warning("未找到有效视频 URL")

                # 规则 2: 单图或多图处理
                elif images_list or note_type in ("normal", "image", "images"):
                    img_count = len(images_list)
                    if img_count == 1:
                        live_video_url = self._xhs_extract_live_photo_video_url(images_list[0])
                        if live_video_url:
                            tmp_dir = os.path.join(os.getcwd(), "tmp", "videos")
                            os.makedirs(tmp_dir, exist_ok=True)
                            filepath = os.path.join(tmp_dir, f"xhs_live_{uid}.mp4")
                            self._xhs_download_file(live_video_url, filepath)
                            return filepath

                        # 单张静态图逻辑
                        img_url = self._xhs_static_image_url(images_list[0])
                        if img_url:
                            tmp_dir = os.path.join(os.getcwd(), "tmp", "images")
                            os.makedirs(tmp_dir, exist_ok=True)
                            temp_img = os.path.join(tmp_dir, f"temp_{uid}_raw")
                            output_jpg = os.path.join(tmp_dir, f"xhs_img_{uid}.jpg")

                            self._xhs_download_file(img_url, temp_img)
                            try:
                                self._xhs_convert_to_jpg(temp_img, output_jpg)
                                if os.path.exists(temp_img):
                                    os.remove(temp_img)
                                return output_jpg
                            except Exception as e:
                                self.logger.error(f"图片处理异常: {e}")
                                if os.path.exists(temp_img):
                                    os.remove(temp_img)
                        else:
                            self.logger.warning("未找到图片原图 URL")

                    elif img_count >= 2:
                        # 多图合并逻辑
                        process_count = min(img_count, self.xhs_max_images)
                        self.logger.info(f"检测到多图 ({img_count} 张)，准备合并前 {process_count} 张为长图...")
                        tmp_dir = os.path.join(os.getcwd(), "tmp", "images")
                        os.makedirs(tmp_dir, exist_ok=True)

                        temp_files = []
                        converted_images = []

                        try:
                            for i, img_info in enumerate(images_list[:self.xhs_max_images]):
                                img_url = self._xhs_static_image_url(img_info)
                                if not img_url: continue

                                raw_file = os.path.join(tmp_dir, f"temp_{uid}_{i}_raw")
                                jpg_file = os.path.join(tmp_dir, f"temp_{uid}_{i}.jpg")

                                self._xhs_download_file(img_url, raw_file)
                                temp_files.append(raw_file)

                                # 全部先转成 jpg 以便 Pillow 处理
                                self._xhs_convert_to_jpg(raw_file, jpg_file)
                                temp_files.append(jpg_file)
                                converted_images.append(jpg_file)

                            if converted_images:
                                final_long_img = os.path.join(tmp_dir, f"xhs_long_img_{uid}.jpg")
                                self._merge_images_vertically(converted_images, final_long_img)
                                return final_long_img
                        except Exception as e:
                            self.logger.error(f"多图处理失败: {e}")
                        finally:
                            # 清理所有临时文件
                            for f in temp_files:
                                if os.path.exists(f):
                                    try: os.remove(f)
                                    except: pass
                    else:
                        self.logger.info(f"跳过处理: 图片数量为 {img_count}")

                # 规则 3: 其他情况
                else:
                    self.logger.info(f"跳过处理: 类型为 {note_type}, 图片数量为 {len(images_list)}")

            return None
        except Exception as e:
            self.logger.error(f"处理小红书笔记时出错: {e}")
            return None


# 全局实例
summary_service: Optional[SummaryService] = None


def handle_link_message(event: Event):
    """处理链接消息事件（委托到 platform_service）"""
    global summary_service
    svc = summary_service
    if svc is None:
        return False
    return route_link_message(event=event, svc=svc, logger=logger)


def register(event_bus, subscribe):
    global summary_service
    logger.info("📰 Registering summary_plus plugin...")
    summary_service = SummaryService()
    # 默认阻断由 config.json 控制；此处不强制覆盖
    subscribe(event_type=EventType.LINK_MESSAGE_RECEIVED, handler=handle_link_message)
    logger.info("✅ summary_plus 插件注册成功")


def unregister():
    """取消注册插件"""
    global summary_service
    logger.info("📰 Unregistering summary_plus plugin...")
    if summary_service:
        with summary_service.driver_lock:
            summary_service._close_driver()
    summary_service = None
    logger.info("✅ summary_plus 插件卸载完成")
