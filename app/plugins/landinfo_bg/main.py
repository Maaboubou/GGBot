"""
保加利亚地块信息查询插件
- 检测用户输入中的地块关键词(地块/land/имот)
- 提取地块编号(格式: 43462.164.31)
- 复用已启动的浏览器进行查询
- 调用 LLM 翻译保加利亚语信息为中文
- 发送截图
"""

import re
import logging
import time
import os
import subprocess
import threading
from typing import Optional, Dict

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException

from app.core.event_bus import Event, EventType
from app.services.llm_manager import get_llm_manager
from app.utils.plugin_config import get_config
from app.utils.subprocess_utils import hidden_process_kwargs

logger = logging.getLogger(__name__)


class LandInfoPlugin:
    """保加利亚地块信息查询插件"""

    WORKER_TAB_TITLE = "LAND_INFO_WORKER"

    def __init__(self, context=None):
        self.llm_manager = get_llm_manager()
        self.logger = logger

        # 加载配置
        plugin_name = "landinfo_bg"
        self.trigger_keywords = get_config(
            "trigger_keywords", ["地块", "land", "імот", "имот"], plugin_name=plugin_name
        ) or []
        self.chrome_debug_port = int(get_config("chrome_debug_port", plugin_name=plugin_name))
        self.chrome_path = get_config("chrome_path", plugin_name=plugin_name)
        self.chrome_user_data_dir = get_config("chrome_user_data_dir", plugin_name=plugin_name)
        self.chrome_profile_dir = get_config("chrome_profile_dir", plugin_name=plugin_name)
        self.page_load_timeout = int(get_config("page_load_timeout", plugin_name=plugin_name))
        self.prompt_translate = get_config("prompt_translate", plugin_name=plugin_name)
        if context is not None:
            migration_notes = context.storage.migrate_legacy_directory(
                os.path.join(os.path.dirname(__file__), "screenshots"),
                storage_class="generated",
                relative="screenshots",
            )
            self.screenshot_dir = str(context.storage.generated_root / "screenshots")
            if migration_notes:
                context.audit.record(
                    "storage_migration",
                    summary="地块截图已迁移到插件标准存储目录",
                    details={"moved_files": len(migration_notes)},
                )
        else:
            self.screenshot_dir = os.path.join(os.path.dirname(__file__), "screenshots")

        # WebDriver 管理
        self.driver: Optional[webdriver.Chrome] = None
        self.driver_lock = threading.Lock()

        # 初始化 WebDriver
        with self.driver_lock:
            self._init_driver()

    def _is_debug_port_ready(self) -> bool:
        """检查远程调试端口是否可用"""
        try:
            resp = requests.get(f"http://127.0.0.1:{self.chrome_debug_port}/json/version", timeout=1.0)
            if resp.status_code != 200:
                return False
            data = resp.json()
            browser = str(data.get("Browser") or data.get("product") or "")
            websocket_url = str(data.get("webSocketDebuggerUrl") or "")
            return browser.startswith("Chrome/") and websocket_url.startswith("ws://")
        except Exception:
            return False

    def _start_chrome_debug(self):
        """启动 Chrome 调试模式"""
        try:
            os.makedirs(self.chrome_user_data_dir, exist_ok=True)
        except Exception as e:
            self.logger.warning(f"⚠️ 创建用户数据目录失败: {e}")

        try:
            args = [
                self.chrome_path,
                f"--remote-debugging-port={self.chrome_debug_port}",
                f"--user-data-dir={os.path.abspath(self.chrome_user_data_dir)}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if isinstance(self.chrome_profile_dir, str) and self.chrome_profile_dir.strip():
                args.append(f"--profile-directory={self.chrome_profile_dir.strip()}")

            self.logger.info(f"🚀 启动 Chrome: {' '.join(args)}")
            subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                **hidden_process_kwargs(),
            )

            self.logger.info("⏳ 等待 Chrome 调试端口就绪...")
            for i in range(40):
                if self._is_debug_port_ready():
                    self.logger.info(f"✅ Chrome 调试端口在 {i * 0.25:.2f} 秒后就绪")
                    return
                time.sleep(0.25)

            raise RuntimeError("Chrome 调试端口启动超时")
        except Exception as e:
            self.logger.error(f"❌ 启动 Chrome 调试模式失败: {e}")
            raise

    def _init_driver(self):
        """初始化 WebDriver 实例"""
        try:
            self.logger.info(f"🔍 检查调试端口 {self.chrome_debug_port} 是否可用")
            if not self._is_debug_port_ready():
                self.logger.info("🚀 调试端口不可用,将启动新的 Chrome 实例")
                self._start_chrome_debug()
            else:
                self.logger.info("✅ 调试端口已可用,将尝试连接现有 Chrome 实例")

            opts = webdriver.ChromeOptions()
            opts.debugger_address = f"127.0.0.1:{self.chrome_debug_port}"

            for attempt in range(10):
                try:
                    self.logger.info(f"🔄 尝试连接 WebDriver (第 {attempt + 1}/10 次)")
                    self.driver = webdriver.Chrome(options=opts)
                    self.logger.info(f"✅ WebDriver 连接成功")
                    return
                except WebDriverException as e:
                    self.logger.warning(f"⚠️ 第 {attempt + 1} 次连接失败: {e}")
                    time.sleep(0.5)

            raise RuntimeError(f"无法连接到 Chrome 调试端口 127.0.0.1:{self.chrome_debug_port}")
        except Exception as e:
            self.logger.error(f"❌ 初始化 WebDriver 失败: {e}")
            self.driver = None

    def _get_or_create_worker_tab(self) -> str:
        """获取或创建工作标签页"""
        if not self.driver:
            raise RuntimeError("WebDriver 未初始化")

        # 查找已存在的工作标签页
        for handle in self.driver.window_handles:
            try:
                self.driver.switch_to.window(handle)
                title = self.driver.execute_script("return document.title || ''") or ""
                if title == self.WORKER_TAB_TITLE:
                    self.logger.info(f"♻️ 复用已存在的工作标签页")
                    return handle
            except Exception:
                continue

        # 创建新的工作标签页
        self.logger.info("🆕 创建新的工作标签页")
        self.driver.execute_script("window.open('about:blank','_blank');")
        handle = self.driver.window_handles[-1]
        self.driver.switch_to.window(handle)
        try:
            self.driver.execute_script(f"document.title='{self.WORKER_TAB_TITLE}';")
        except Exception:
            pass
        return handle

    def _should_trigger(self, text: str) -> Optional[str]:
        """检测是否包含地块关键词,返回地块编号"""
        if not text:
            return None

        text_lower = text.lower()

        # 检测关键词
        keywords = [str(item).lower() for item in self.trigger_keywords if str(item).strip()]
        if not any(keyword in text_lower for keyword in keywords):
            return None

        # 提取地块编号: 格式如 43462.164.31
        pattern = r'\b\d+\.\d+\.\d+\b'
        match = re.search(pattern, text)
        return match.group(0) if match else None

    def translate_land_info(self, bg_text: str) -> str:
        """翻译保加利亚语地块信息为中文"""
        try:
            messages = [
                {"role": "system", "content": self.prompt_translate},
                {"role": "user", "content": bg_text}
            ]

            response = self.llm_manager.call(
                plugin_name="landinfo_bg",
                call_type="translate",
                messages=messages
            )
            return response.strip()
        except Exception as e:
            self.logger.error(f"❌ LLM 翻译失败: {e}")
            return "❌ 翻译失败"

    def query_land_info(self, target_number: str) -> Optional[Dict]:
        """查询地块信息"""
        if not self.driver:
            self.logger.error("❌ WebDriver 未初始化")
            return None

        self.logger.info(f"🔍 查询地块: {target_number}")

        original_handle = None
        worker_handle = None

        try:
            # 保存原始窗口句柄
            try:
                if self.driver.window_handles:
                    original_handle = self.driver.current_window_handle
            except WebDriverException:
                if self.driver.window_handles:
                    original_handle = self.driver.window_handles[0]

            # 获取或创建工作标签页
            worker_handle = self._get_or_create_worker_tab()
            self.driver.switch_to.window(worker_handle)

            # 导航到地图页面
            self.logger.info("1. [Selenium] 导航到地图...")
            self.driver.get("https://kais.cadastre.bg/bg/Map")

            wait = WebDriverWait(self.driver, 20)

            # 点击"快速搜索"标签
            try:
                tab = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#map-search-tabs li.k-item:first-child")))
                tab.click()
                time.sleep(1)
            except Exception as e:
                self.logger.warning(f"⚠️ 无法点击标签: {e}")

            # 等待搜索输入框
            self.logger.info("2. [Selenium] 等待搜索输入框...")
            search_input = wait.until(EC.presence_of_element_located((By.NAME, "KeyWords")))

            # 输入查询内容
            self.logger.info(f"3. [Selenium] 输入查询: {target_number}...")
            self.driver.execute_script("arguments[0].value = arguments[1];", search_input, target_number)
            self.driver.execute_script("$(arguments[0]).trigger('change');", search_input)

            # 点击搜索
            self.logger.info("4. [Selenium] 点击搜索...")
            search_btn = self.driver.find_element(By.ID, "submit-search")
            self.driver.execute_script("arguments[0].click();", search_btn)

            # 智能等待结果加载
            self.logger.info("5. [Selenium] 等待结果...")
            try:
                # 等待结果列表出现
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".resultsList .object")))
                time.sleep(2)  # 额外等待 Kendo 数据绑定
            except Exception:
                # 如果智能等待失败,使用固定延迟
                time.sleep(5)

            # 提取数据
            self.logger.info("6. [Selenium] 提取数据...")
            data_item = self.driver.execute_script("""
                var listView = $(".resultsList").data("kendoListView");
                if (listView && listView.dataSource.view().length > 0) {
                    return listView.dataSource.view()[0];
                }
                return null;
            """)

            if not data_item:
                self.logger.error("❌ 未找到搜索结果")
                return None

            self.logger.info(f"✅ 找到项目: {data_item.get('Title')} (Id: {data_item.get('Id')})")

            # 7. 缩放到地块并准备截图
            self.logger.info("7. [Selenium] 缩放到地块...")
            try:
                # 点击缩放按钮
                zoom_btn = self.driver.find_element(By.CSS_SELECTOR, ".resultsList .object .object-option.zoom-js")
                self.driver.execute_script("arguments[0].click();", zoom_btn)

                # 等待地图缩放动画完成(减少等待时间)
                time.sleep(3)
                self.logger.info("✅ 已缩放到地块")
            except Exception as e:
                self.logger.warning(f"⚠️ 缩放失败: {e}")

            # 8. 设置地图比例为2000米
            self.logger.info("8. [Selenium] 设置地图比例为2000米...")
            try:
                from selenium.webdriver.common.keys import Keys
                from selenium.webdriver.common.action_chains import ActionChains

                # 等待页面完全初始化
                time.sleep(2)

                # 尝试多个选择器查找比例输入框
                selectors = [
                    ".mc-mapstat input.k-input-inner[role='spinbutton']",
                    ".mc-mapstat input[role='spinbutton']",
                    "input.k-input-inner[role='spinbutton']",
                    ".k-numerictextbox input"
                ]

                scale_input = None
                for selector in selectors:
                    try:
                        scale_input = self.driver.find_element(By.CSS_SELECTOR, selector)
                        self.logger.info(f"✅ 找到比例输入框: {selector}")
                        break
                    except:
                        continue

                if scale_input:
                    # 使用 ActionChains 模拟真实键盘输入
                    actions = ActionChains(self.driver)

                    # 点击输入框获得焦点
                    actions.click(scale_input).perform()
                    time.sleep(0.5)

                    # 全选现有文本 (Ctrl+A)
                    actions.key_down(Keys.CONTROL).send_keys('a').key_up(Keys.CONTROL).perform()
                    time.sleep(0.3)

                    # 输入 "2000"
                    actions.send_keys("2000").perform()
                    time.sleep(0.5)

                    # 按回车确认
                    actions.send_keys(Keys.RETURN).perform()

                    # 等待地图调整到新比例
                    time.sleep(5)
                    self.logger.info("✅ 地图比例已设置为2000米")
                else:
                    self.logger.warning("⚠️ 未找到比例输入框,跳过比例设置")

            except Exception as e:
                self.logger.warning(f"⚠️ 设置地图比例失败: {e}")

            # 9. 提取地块信息
            self.logger.info("9. [Selenium] 提取地块信息...")
            plot_info_text = None
            try:
                # 点击信息图标
                self.driver.execute_script("""
                    var infoBtn = $(".resultsList .object .object-option.info-js").first();
                    if (infoBtn.length > 0) {
                        infoBtn.click();
                    }
                """)

                # 智能等待信息窗口出现
                try:
                    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.k-window-content")))
                    time.sleep(1)  # 额外等待内容渲染
                except Exception:
                    time.sleep(3)  # 如果智能等待失败,使用固定延迟

                # 提取信息窗口内容
                plot_info_text = self.driver.execute_script("""
                    var content = $("div.k-window-content").first();
                    if (content.length > 0) {
                        return content.text();
                    }
                    return null;
                """)

                if plot_info_text:
                    self.logger.info(f"✅ 提取地块信息 ({len(plot_info_text)} 字符)")

                # 关闭信息窗口
                try:
                    self.driver.execute_script("""
                        var closeBtn = $("span.k-icon.k-font-icon.k-i-x.k-button-icon").first();
                        if (closeBtn.length > 0) {
                            closeBtn.click();
                        }
                    """)
                    time.sleep(0.5)
                    self.logger.info("✅ 已关闭信息窗口")
                except Exception:
                    pass

            except Exception as e:
                self.logger.warning(f"⚠️ 提取地块信息失败: {e}")

            # 截图功能 - 在关闭信息窗口后进行
            screenshot_path = None
            try:
                self.logger.info("10. [Selenium] 准备截图...")

                # 最大化窗口以获得完整画面
                self.driver.maximize_window()
                time.sleep(1)

                # 切换到卫星视图
                self.logger.info("11. [Selenium] 切换到卫星视图...")
                try:
                    # 点击 Ортофото 2022 卫星图层
                    satellite_option = self.driver.find_element(By.CSS_SELECTOR, "a.baseMap-js[data-layer='orthophoto_2022']")
                    self.driver.execute_script("arguments[0].click();", satellite_option)

                    # 等待瓦片加载
                    time.sleep(3)
                    self.logger.info("✅ 已切换到卫星视图")
                except Exception as e:
                    self.logger.warning(f"⚠️ 切换卫星视图失败: {e}")

                # 使用地块编号作为文件名,保存到插件目录下
                screenshot_filename = f"{target_number.replace('.', '_')}.png"
                os.makedirs(self.screenshot_dir, exist_ok=True)
                screenshot_path = os.path.join(self.screenshot_dir, screenshot_filename)

                self.driver.save_screenshot(screenshot_path)
                self.logger.info(f"✅ 截图已保存: {screenshot_path}")
            except Exception as e:
                self.logger.warning(f"⚠️ 截图失败: {e}")

            # 获取几何数据
            self.logger.info("12. [Requests] 获取几何数据...")
            cookies = self.driver.get_cookies()
            cookies_dict = {c['name']: c['value'] for c in cookies}
            user_agent = self.driver.execute_script("return navigator.userAgent;")

            csrf_token = self.driver.execute_script("return $('[name=\"__RequestVerificationToken\"]').val();")
            if not csrf_token:
                csrf_token = cookies_dict.get('csrf', '')

            session = requests.Session()
            session.cookies.update(cookies_dict)

            headers = {
                'User-Agent': user_agent,
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-CSRF-TOKEN': csrf_token,
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': 'https://kais.cadastre.bg/bg/Map',
                'Origin': 'https://kais.cadastre.bg'
            }

            geom_url = "https://kais.cadastre.bg/bg/Map/GetGeometry/"
            geom_payload = {
                'IsChecked': 'false',
                'Id': data_item.get('Id'),
                'Type': data_item.get('Type'),
                'Number': data_item.get('Number'),
                'Title': data_item.get('Title'),
                'ShortDescription': data_item.get('ShortDescription'),
                'Hash': data_item.get('Hash'),
                '__RequestVerificationToken': csrf_token
            }

            try:
                res = session.post(geom_url, headers=headers, data=geom_payload, timeout=30)

                if res.status_code != 200:
                    self.logger.error(f"❌ 几何数据请求失败: {res.status_code}")
                    return None

                geometry_data = res.json()

                # 提取属性
                if geometry_data and len(geometry_data) > 0:
                    attributes = geometry_data[0].get('Attributes', {})

                    cadnum = attributes.get('cadnum', 'N/A')
                    shortinfo = attributes.get('shortinfo', 'N/A')
                    area = attributes.get('st_area(shape)', 0)
                    area_int = int(area)

                    # 提取详细信息
                    detailed_info = ""
                    if plot_info_text:
                        start_marker = "Добави в списък с обекти"
                        end_marker = "Основна заповед"

                        start_idx = plot_info_text.find(start_marker)
                        end_idx = plot_info_text.find(end_marker)

                        if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
                            detailed_text = plot_info_text[start_idx + len(start_marker):end_idx]
                            detailed_info = ' '.join(detailed_text.split())

                    return {
                        "cadnum": cadnum,
                        "shortinfo": shortinfo,
                        "area_int": area_int,
                        "detailed_info": detailed_info,
                        "screenshot_path": screenshot_path
                    }
                else:
                    self.logger.error("❌ 几何数据为空")
                    return None

            except Exception as e:
                self.logger.error(f"❌ 获取几何数据失败: {e}")
                return None

        except Exception as e:
            self.logger.error(f"❌ 查询地块失败: {e}", exc_info=True)
            return None

        finally:
            # 清理工作标签页
            try:
                if worker_handle and worker_handle in self.driver.window_handles:
                    self.driver.switch_to.window(worker_handle)
                    self.driver.get("about:blank")
                    self.driver.execute_script(f"document.title='{self.WORKER_TAB_TITLE}';")
                    self.logger.info("✅ 工作标签页已复位")

                # 切换回原始窗口
                if original_handle and original_handle in self.driver.window_handles:
                    self.driver.switch_to.window(original_handle)
                elif self.driver.window_handles:
                    self.driver.switch_to.window(self.driver.window_handles[0])

            except Exception as e:
                self.logger.warning(f"⚠️ 清理工作标签页时出错: {e}")

    def handle_text(self, event: Event):
        """处理文本消息事件"""
        try:
            message = event.data.get("message", "")
            chat_name = event.data.get("chat_name", "")
            wx = event.context.get("wx")

            # 检测触发并提取地块编号
            land_number = self._should_trigger(message)
            if not land_number:
                return False

            self.logger.info(f"🏞️ 检测到地块查询请求: {land_number}")

            # 查询地块信息
            result = self.query_land_info(land_number)

            if not result:
                if wx:
                    wx.send_message(chat_name, f"❌ 未能查询到地块 {land_number} 的信息")
                return True

            # 提取信息
            cadnum = result.get("cadnum", "N/A")
            shortinfo = result.get("shortinfo", "N/A")
            area_int = result.get("area_int", 0)
            detailed_info = result.get("detailed_info", "")
            screenshot_path = result.get("screenshot_path")

            # 构建保加利亚语原文
            bg_text = f"""地块编号: {cadnum}
简要信息: {shortinfo}
面积: {area_int} ㎡

详细信息: {detailed_info}"""

            # LLM 翻译
            self.logger.info("🌐 开始翻译地块信息...")
            cn_text = self.translate_land_info(bg_text)

            # 构建最终返回内容
            final_message = f"""保加利亚语原文:
{cadnum}
{shortinfo}
{area_int} ㎡

{detailed_info}


中文翻译:
{cn_text}"""

            if wx:
                # 先发送文字信息
                wx.send_message(chat_name, final_message)

                # 如果有截图,发送截图
                if screenshot_path and os.path.exists(screenshot_path):
                    try:
                        wx.send_files(chat_name, screenshot_path)
                        self.logger.info(f"✅ 已发送地块 {land_number} 的截图")
                    except Exception as e:
                        self.logger.warning(f"⚠️ 发送截图失败: {e}")

                self.logger.info(f"✅ 已发送地块 {land_number} 的信息")

            return True

        except Exception as e:
            self.logger.error(f"❌ landinfo_bg 处理失败: {e}", exc_info=True)
            try:
                wx = event.context.get("wx")
                if wx:
                    wx.send_message(event.data.get("chat_name", ""), "⚠️ 地块查询失败,请稍后重试")
            except Exception:
                pass
            return False

    def cleanup(self):
        """清理资源"""
        if self.driver:
            try:
                # 不关闭浏览器,只结束会话
                self.driver.quit()
                self.logger.info("✅ WebDriver 会话已结束")
            except Exception as e:
                self.logger.warning(f"⚠️ 关闭 WebDriver 时出错: {e}")
            finally:
                self.driver = None


# 全局实例
plugin: Optional[LandInfoPlugin] = None


def handle_text(event: Event):
    """文本消息处理器"""
    if plugin:
        return plugin.handle_text(event)
    return False


def register(event_bus, subscribe, context):
    """注册插件"""
    global plugin
    logger.info("🏞️ 注册 landinfo_bg 插件...")

    try:
        plugin = LandInfoPlugin(context)
        context.health.register(lambda: {
            "status": "healthy" if plugin is not None else "unhealthy",
            "message": "地块查询服务已就绪" if plugin is not None else "地块查询服务未初始化",
            "browser_session": bool(plugin and plugin.driver),
        })
        context.register_cleanup(unregister)
        subscribe(
            event_type=EventType.TEXT_MESSAGE_RECEIVED,
            handler=handle_text
        )
        logger.info("✅ landinfo_bg 插件注册成功")
    except Exception as e:
        logger.error(f"❌ landinfo_bg 插件注册失败: {e}", exc_info=True)


def unregister():
    """取消注册插件"""
    global plugin
    logger.info("🏞️ 卸载 landinfo_bg 插件...")

    if plugin:
        try:
            plugin.cleanup()
        except Exception as e:
            logger.warning(f"⚠️ 清理资源时出错: {e}")

    plugin = None
    logger.info("✅ landinfo_bg 插件卸载完成")
