"""Browser summarization helpers for summary_plus."""

import random
import re
import time
from typing import Optional

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from .content_cleaner import build_clean_stats, clean_extracted_article_text, strip_markdown

__all__ = [
    "browser_summarize",
    "detect_access_gate_page",
    "is_access_gate_silent_response",
]

ACCESS_GATE_SILENT_SENTINEL = "__SUMMARY_PLUS_SILENT_SKIP_ACCESS_GATE__"


CHALLENGE_KEYWORDS = (
    "captcha",
    "人机验证",
    "验证码",
    "向右滑动以保护您的访问",
    "请完成安全验证",
    "verify you are human",
    "are you human",
    "security check",
    "bot detection",
    "cloudflare",
    "perimeterx",
    "cf-chl",
    "px-captcha",
    "access denied",
)

ACCESS_GATE_URL_KEYWORDS = (
    "captcha",
    "challenge",
    "verify",
    "verification",
    "signin",
    "sign-in",
    "login",
    "auth/login",
)

LOGIN_GATE_PHRASES = (
    "登录后继续",
    "请先登录",
    "请登录后",
    "账号登录",
    "帐号登录",
    "扫码登录",
    "手机号登录",
    "密码登录",
    "登录即可继续",
    "login required",
    "please sign in",
    "sign in to continue",
    "authentication required",
)


def safe_get_window_handles(driver):
    try:
        return driver.window_handles
    except Exception:
        return []


def safe_get_current_url(driver):
    try:
        return driver.current_url or ""
    except Exception:
        return ""


def open_blank_worker_tab(driver, logger) -> str:
    """Create a clean tab without running JavaScript in the current tab."""
    try:
        target = driver.execute_cdp_cmd("Target.createTarget", {"url": "about:blank"})
        handle = target.get("targetId")
        if handle:
            time.sleep(0.2)
            driver.switch_to.window(handle)
            logger.info(f"✅ 已通过 CDP 创建工作标签页 {handle}")
            return handle
    except Exception as e:
        logger.warning(f"⚠️ CDP 创建工作标签页失败，改用 Selenium 新标签页: {e}")

    try:
        driver.switch_to.new_window("tab")
        handle = driver.current_window_handle
        logger.info(f"✅ 已通过 Selenium 创建工作标签页 {handle}")
        return handle
    except Exception:
        driver.execute_script("window.open('about:blank', '_blank');")
        time.sleep(0.5)
        handle = driver.window_handles[-1]
        driver.switch_to.window(handle)
        logger.info(f"✅ 已通过 window.open 创建工作标签页 {handle}")
        return handle


def is_valid_content_url(url: str) -> bool:
    if not url:
        return False
    return not url.startswith(("about:blank", "chrome://", "devtools://"))


def looks_like_challenge_page(text: str) -> bool:
    low = (text or "").lower()
    if not low:
        return False
    return any(keyword.lower() in low for keyword in CHALLENGE_KEYWORDS)


def detect_access_gate_page(text: str, url: str = "", title: str = "") -> Optional[str]:
    """识别只有登录/人机验证内容、没有可摘要正文的访问门槛页面。"""
    normalized_text = re.sub(r"\s+", " ", text or "").strip()
    combined = " ".join((title or "", normalized_text)).lower()
    normalized_url = (url or "").lower()

    if looks_like_challenge_page(combined):
        return "human_verification"

    # 验证/登录专用 URL 配合短页面文本，视为访问门槛。长度限制避免正文链接
    # 或导航栏中偶然出现 login/auth 字样时误判。
    if len(normalized_text) <= 2000 and any(
        keyword in normalized_url for keyword in ACCESS_GATE_URL_KEYWORDS
    ):
        return "access_gate_url"

    if len(normalized_text) <= 2000 and any(
        phrase in combined for phrase in LOGIN_GATE_PHRASES
    ):
        return "login_required"

    # 极短的通用登录框通常只有“登录 / 账号 / 密码（或扫码）”等控件文案。
    if len(normalized_text) <= 500:
        has_login = "登录" in combined or "log in" in combined or "sign in" in combined
        has_credential = any(
            marker in combined
            for marker in ("账号", "帐号", "密码", "扫码", "手机号", "username", "password", "qr code")
        )
        if has_login and has_credential:
            return "login_form"

    return None


def is_access_gate_silent_response(text: str) -> bool:
    """模型仅返回约定哨兵时，视为语义判定的访问门槛页面。"""
    normalized = (text or "").strip().strip("`").strip()
    return normalized == ACCESS_GATE_SILENT_SENTINEL


def wait_for_page_load(driver, page_load_timeout: int, page_content_stabilize_delay: float, logger) -> None:
    max_retries = 2
    last_error = None

    for attempt in range(max_retries):
        try:
            logger.info(f"🔄 等待页面加载完成 (第 {attempt + 1}/{max_retries} 次尝试)")

            def page_fully_loaded(d):
                try:
                    ready_state = d.execute_script("return document.readyState")
                    if ready_state == "complete":
                        return True
                    loading_indicators = d.find_elements(By.CSS_SELECTOR, ".loading, .spinner, [aria-busy='true']")
                    return len(loading_indicators) == 0
                except Exception:
                    return False

            WebDriverWait(driver, page_load_timeout).until(page_fully_loaded)
            logger.info(
                f"✅ 页面加载完成 (第 {attempt + 1} 次尝试)，额外等待 {page_content_stabilize_delay}s 以稳定内容"
            )
            time.sleep(page_content_stabilize_delay + random.uniform(0.2, 0.8))
            return
        except TimeoutException as e:
            last_error = e
            if attempt < max_retries - 1:
                logger.info("🔄 刷新页面并重试...")
                try:
                    driver.refresh()
                    time.sleep(2)
                except Exception:
                    pass
            else:
                raise

    if last_error:
        raise last_error


def strip_summary_tag_section(text: str) -> str:
    """普通摘要模式不输出“标签”段；兼容模型偶尔仍生成标签的情况。"""
    if not text:
        return text

    lines = text.splitlines()
    kept = []
    skipping_tags = False

    tag_line_pattern = re.compile(r"^\s*(?:🏷\s*)?(?:标签|tag|tags)\s*[:：]", re.IGNORECASE)
    section_header_pattern = re.compile(r"^\s*[📖🔑📰🎯📋💡🔧]\s*[^\n]*[:：]?\s*$")

    for line in lines:
        if tag_line_pattern.match(line):
            skipping_tags = True
            continue
        if skipping_tags:
            stripped = line.strip()
            if not stripped:
                continue
            if section_header_pattern.match(line):
                skipping_tags = False
                kept.append(line)
            continue
        kept.append(line)

    return "\n".join(kept).strip()


def optimized_scroll(driver, logger) -> None:
    try:
        time.sleep(random.uniform(1.0, 2.0))
        last_height = driver.execute_script("return document.body.scrollHeight")
        max_scrolls = 8

        for i in range(max_scrolls):
            current_pos = driver.execute_script("return window.pageYOffset")
            scroll_step = random.randint(400, 900)
            driver.execute_script(f"window.scrollTo(0, {current_pos + scroll_step});")
            time.sleep(random.uniform(0.2, 0.5))

            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height and i > 1:
                break
            last_height = new_height

        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(random.uniform(0.3, 0.8))
    except Exception as e:
        logger.warning(f"⚠️ 滚动加载过程中出现异常: {e}")


def browser_summarize(
    ctx,
    url: Optional[str],
    is_link_message: bool = False,
    chat_name: str = "",
    sender: str = "",
) -> Optional[str]:
    """使用浏览器 + GPT 进行摘要"""
    try:
        ctx.logger.info(
            "🧭 开始浏览器摘要: "
            f"is_link_message={is_link_message}, "
            f"url={(url or '')[:180]}"
        )
        driver = ctx._ensure_driver_available()
        ctx.logger.info("✅ WebDriver 已就绪，准备打开/复用页面")

        original_handle = None
        safe_handles = safe_get_window_handles(driver)
        if safe_handles:
            original_handle = safe_handles[0]

        worker_handle = None

        try:
            if is_link_message:
                ctx.logger.info("🔗 Link消息：复用已打开的页面")
                initial_handles = list(driver.window_handles)
                initial_count = len(initial_handles)
                start_ts = time.time()
                while time.time() - start_ts < ctx.page_load_timeout:
                    current_handles = safe_get_window_handles(driver)
                    if not current_handles:
                        time.sleep(ctx.RETRY_DELAY)
                        continue

                    if len(current_handles) > initial_count:
                        candidate = current_handles[-1]
                        driver.switch_to.window(candidate)
                        current_url = safe_get_current_url(driver)
                        if is_valid_content_url(current_url):
                            worker_handle = candidate
                            break
                        time.sleep(ctx.WINDOW_HANDLE_STABILIZE_DELAY)
                    else:
                        if driver.window_handles:
                            active = driver.current_window_handle
                            current_url = safe_get_current_url(driver)
                            if is_valid_content_url(current_url):
                                worker_handle = active
                                break
                    time.sleep(ctx.RETRY_DELAY)
                if not worker_handle and driver.window_handles:
                    worker_handle = driver.window_handles[-1]
                    driver.switch_to.window(worker_handle)
            else:
                if not url:
                    return "❌ 缺少URL"
                worker_handle = open_blank_worker_tab(driver, ctx.logger)
                time.sleep(ctx.RETRY_DELAY)
                driver.get(url)

            wait_for_page_load(
                driver=driver,
                page_load_timeout=ctx.page_load_timeout,
                page_content_stabilize_delay=ctx.PAGE_CONTENT_STABILIZE_DELAY,
                logger=ctx.logger,
            )
            optimized_scroll(driver=driver, logger=ctx.logger)

            try:
                current_url = safe_get_current_url(driver)
                fallback_url = current_url if current_url.startswith(("http://", "https://")) else (url or "")
                main_text = driver.execute_script("return document.body && document.body.innerText || ''") or ""
                try:
                    page_title = driver.title or ""
                except Exception:
                    page_title = ""
                access_gate_reason = detect_access_gate_page(
                    main_text,
                    url=fallback_url,
                    title=page_title,
                )
                if access_gate_reason:
                    ctx.logger.info(
                        "🔇 页面仅包含登录/人机验证，已静默跳过摘要: "
                        f"reason={access_gate_reason}, chars={len(main_text)}, "
                        f"url={fallback_url[:240]}"
                    )
                    return None
                if not main_text or not main_text.strip():
                    return "❌ 抓取正文为空"
                else:
                    cleaned_text = clean_extracted_article_text(main_text)
                    local_content = ""
                    if cleaned_text and len(cleaned_text.strip()) >= 300:
                        stats = build_clean_stats(main_text, cleaned_text).as_dict()
                        ctx.logger.info(
                            "🧼 正文清洗完成: "
                            f"{stats['raw_chars']} -> {stats['cleaned_chars']} chars "
                            f"(reduction {stats['reduction_pct']}%)"
                        )
                        local_content = cleaned_text
                    else:
                        ctx.logger.warning("⚠️ 正文清洗结果过短，回退使用原始文本")
                        local_content = main_text

                    user_content = local_content[: ctx.MAX_CONTENT_LENGTH]
            except Exception as e:
                ctx.logger.error(f"❌ 使用 innerText 提取内容时出错: {e}", exc_info=True)
                return "❌ 抓取正文失败"

            system_prompt = ctx.prompt_summary
            messages_list = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            messages_list.insert(
                len(messages_list) - 1,
                {
                    "role": "system",
                    "content": (
                        "先判断用户内容是否只有登录界面、人机验证、验证码、安全检查或访问受限提示，"
                        "而没有可总结的正文。若是，只输出以下哨兵字符串，不得输出其他任何内容："
                        f"{ACCESS_GATE_SILENT_SENTINEL}。"
                        "若存在可总结正文，则忽略本条并正常完成摘要。"
                    ),
                },
            )

            ctx.logger.info(
                "🤖 准备调用摘要 LLM: "
                f"call_type=summary, content_len={len(user_content)}, "
                f"chat={chat_name}, sender={sender}"
            )
            try:
                call_kwargs = {}
                if chat_name:
                    call_kwargs["_mabobot_chat_name"] = chat_name
                response = ctx.llm_manager.call(
                    plugin_name="summary_plus",
                    call_type="summary",
                    messages=messages_list,
                    **call_kwargs,
                )
            except Exception as e:
                ctx.logger.error(f"❌ 摘要 LLM 请求失败: {e}", exc_info=True)
                return "❌ 摘要模型调用失败"
            if is_access_gate_silent_response(response):
                ctx.logger.info(
                    "🔇 摘要模型判定页面仅包含登录/人机验证，已静默跳过回复: "
                    f"url={(fallback_url or url or '')[:240]}"
                )
                return None
            summary_text = strip_markdown(response.strip())
            return strip_summary_tag_section(summary_text)
        finally:
            try:
                handles_before_cleanup = len(safe_get_window_handles(driver)) if driver else 0

                safe_handles = safe_get_window_handles(driver)
                if worker_handle and worker_handle in safe_handles:
                    try:
                        driver.switch_to.window(worker_handle)
                        if safe_handles and worker_handle == safe_handles[0]:
                            driver.get("about:blank")
                            ctx.logger.info(f"✅ Root 标签页已重置为空白 {worker_handle}")
                        else:
                            driver.close()
                            ctx.logger.info(f"✅ 已关闭工作标签页 {worker_handle}")
                    except WebDriverException as e:
                        ctx.logger.warning(f"⚠️ 处理工作标签页时出错: {e}")

                remaining_handles = safe_get_window_handles(driver)
                if original_handle and original_handle in remaining_handles:
                    try:
                        driver.switch_to.window(original_handle)
                    except WebDriverException:
                        pass
                elif remaining_handles:
                    try:
                        driver.switch_to.window(remaining_handles[0])
                        ctx.logger.warning("⚠️ 原始窗口已失效，已切换到第一个可用窗口")
                    except WebDriverException:
                        pass

                handles_after_cleanup = len(safe_get_window_handles(driver)) if driver else 0
                ctx.logger.info(f"📊 窗口清理: {handles_before_cleanup} → {handles_after_cleanup}")
            except Exception as e:
                ctx.logger.warning(f"⚠️ 最终清理时发生不可预期的错误: {e}")
    except Exception as e:
        error_type = type(e).__name__
        ctx.logger.error(f"❌ 浏览器摘要失败: {error_type} - {e}", exc_info=True)
        return None
