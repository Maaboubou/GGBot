import cloudscraper
from bs4 import BeautifulSoup
from urllib.parse import quote, urljoin, urlparse
import time
import os
import json
import base64
import html as html_module
import tempfile
import threading
import re
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By


_PDF_RENDER_LOCK = threading.Lock()

DEFAULT_ENTRY_POINTS = [
    "https://磁力搜索.com/",
    "https://www.1024btso.com/",
]
_SEARCH_FIELD_NAMES = ("search", "keyword", "q", "wd")
_MAX_ENTRY_DISCOVERY_PAGES = 12
_URL_IN_REFRESH_RE = re.compile(r"(?:^|;)\s*url\s*=\s*(['\"]?)(.+?)\1\s*$", re.IGNORECASE)


def _http_url(base_url, value):
    """Return an absolute HTTP(S) URL, or ``None`` for unsafe/invalid values."""

    candidate = urljoin(base_url, str(value or "").strip())
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate


def _refresh_target(response, soup):
    """Read non-standard refresh redirects used by expired 1024BT domains."""

    refresh_values = []
    headers = getattr(response, "headers", {}) or {}
    if headers.get("Refresh"):
        refresh_values.append(headers["Refresh"])
    for tag in soup.find_all("meta", attrs={"http-equiv": re.compile(r"^refresh$", re.I)}):
        refresh_values.append(tag.get("content", ""))

    for value in refresh_values:
        match = _URL_IN_REFRESH_RE.search(str(value))
        if not match:
            continue
        target = _http_url(response.url, match.group(2))
        if target:
            return target
    return None


def _fnv1a_32(value):
    """Match the small JavaScript FNV-1a helper on the address publication page."""

    result = 2166136261
    for character in value:
        result ^= ord(character)
        result = (result * 16777619) & 0xFFFFFFFF
    return result


def _seeded_code(seed_text, length):
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    state = _fnv1a_32(seed_text) or 1
    output = []
    for _ in range(length):
        state ^= (state << 13) & 0xFFFFFFFF
        state &= 0xFFFFFFFF
        state ^= state >> 17
        state &= 0xFFFFFFFF
        state ^= (state << 5) & 0xFFFFFFFF
        state &= 0xFFFFFFFF
        output.append(alphabet[state % len(alphabet)])
    return "".join(output)


def _generated_entry_urls(page_text, now=None):
    """Reproduce rotating entry URLs published by 磁力搜索.com.

    The publication page renders its links in JavaScript, so BeautifulSoup cannot
    see them.  Keep this deliberately narrow to the documented ``CONFIG`` block.
    """

    config_match = re.search(r"\bconst\s+CONFIG\s*=\s*\{(?P<body>.*?)\}\s*;", page_text, re.S)
    if not config_match:
        return []

    body = config_match.group("body")
    domains_match = re.search(r"\bdomains\s*:\s*\[(?P<domains>.*?)\]", body, re.S)
    interval_match = re.search(r"\bintervalMinutes\s*:\s*(\d+)", body)
    length_match = re.search(r"\bcodeLength\s*:\s*(\d+)", body)
    salt_match = re.search(r"\bsalt\s*:\s*(['\"])(.*?)\1", body, re.S)
    if not all((domains_match, interval_match, length_match, salt_match)):
        return []

    domains = re.findall(r"['\"]([^'\"]+)['\"]", domains_match.group("domains"))
    interval_minutes = max(1, int(interval_match.group(1)))
    code_length = max(1, min(63, int(length_match.group(1))))
    salt = salt_match.group(2)
    current_slot = int((time.time() if now is None else now) // (interval_minutes * 60))

    entries = []
    # Include the preceding slot to tolerate a cached publication page or small
    # client/server clock skew around the half-hour rotation boundary.
    for slot in (current_slot, current_slot - 1):
        for index, raw_domain in enumerate(domains):
            parsed = urlparse(
                raw_domain if "://" in raw_domain else f"https://{raw_domain}"
            )
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            path = "" if parsed.path == "/" else parsed.path.rstrip("/")
            code = _seeded_code(
                f"{salt}|{parsed.hostname.lower()}|{slot}|{index}",
                code_length,
            )
            entries.append(f"https://{code}.{parsed.hostname.lower()}{path}")
    return entries


def _discovered_entry_urls(response, soup):
    """Extract official redirect/link variants from an address publication page."""

    candidates = []
    refresh_target = _refresh_target(response, soup)
    if refresh_target:
        candidates.append(refresh_target)

    for tag in soup.select("a.address-link[href], a.open-button[href], [data-copy]"):
        value = tag.get("href") or tag.get("data-copy")
        target = _http_url(response.url, value)
        if target:
            candidates.append(target)

    candidates.extend(_generated_entry_urls(response.text))
    return list(dict.fromkeys(candidates))


def _search_form(soup):
    """Find the site's search form and its keyword field."""

    for form in soup.find_all("form"):
        named_inputs = {
            str(tag.get("name")): tag
            for tag in form.find_all(("input", "textarea"), attrs={"name": True})
            if not tag.has_attr("disabled")
        }
        for field_name in _SEARCH_FIELD_NAMES:
            field = named_inputs.get(field_name)
            if field is not None and str(field.get("type", "text")).lower() != "hidden":
                return form, field_name
    return None, None


def _submit_search_form(scraper, landing_response, soup, keyword, timeout):
    form, field_name = _search_form(soup)
    if form is None:
        return None

    payload = {}
    for field in form.find_all(("input", "textarea"), attrs={"name": True}):
        if field.has_attr("disabled"):
            continue
        field_type = str(field.get("type", "text")).lower()
        if field_type in {"submit", "button", "image", "file", "reset"}:
            continue
        payload[str(field["name"])] = str(field.get("value", ""))
    payload[field_name] = keyword

    target_url = _http_url(landing_response.url, form.get("action") or landing_response.url)
    if not target_url:
        return None

    request_options = {
        "timeout": timeout,
        "allow_redirects": True,
        "headers": {"Referer": landing_response.url},
    }
    method = str(form.get("method", "GET")).upper()
    if method == "POST":
        return scraper.post(target_url, data=payload, **request_options)
    return scraper.get(target_url, params=payload, **request_options)


def _is_search_page(response):
    """Reject address-publication/error pages that happen to return HTTP 200."""

    soup = BeautifulSoup(response.text, "html.parser")
    form, _ = _search_form(soup)
    return form is not None or soup.select_one("div.ssbox") is not None


def _fetch_from_entry(scraper, entry_url, keyword, timeout):
    """Resolve a stable/publication URL and execute its advertised search form."""

    pending = [entry_url]
    visited = set()
    legacy_origins = []

    while pending and len(visited) < _MAX_ENTRY_DISCOVERY_PAGES:
        candidate = pending.pop(0)
        normalized = _http_url(candidate, candidate)
        if not normalized or normalized in visited:
            continue
        visited.add(normalized)

        try:
            response = scraper.get(normalized, timeout=timeout, allow_redirects=True)
            soup = BeautifulSoup(response.text, "html.parser")
            submitted = _submit_search_form(scraper, response, soup, keyword, timeout)
        except Exception as exc:
            print(f"[!] 候选入口访问失败 {normalized}: {str(exc)[:100]}")
            continue
        if submitted is not None and submitted.status_code == 200:
            return submitted, normalized

        discovered = _discovered_entry_urls(response, soup)
        pending.extend(url for url in discovered if url not in visited and url not in pending)

        if response.status_code == 200:
            parsed = urlparse(response.url)
            legacy_origins.append(f"{parsed.scheme}://{parsed.netloc}")

    # Backwards compatibility for mirrors that still serve the former path but
    # no longer render a usable form on their root page.
    for origin in dict.fromkeys(legacy_origins):
        target_url = f"{origin}/search-{quote(keyword, safe='')}-0-3-1.html"
        response = scraper.get(target_url, timeout=timeout, allow_redirects=True)
        if response.status_code == 200 and _is_search_page(response):
            return response, origin
    return None, None


def fetch_magnet_links(keyword, entry_points=None, max_retries=3, retry_delay=2, timeout=15):
    """
    Searching for magnet links and returning a list of dictionaries.
    Enhanced with retry mechanism for better reliability.

    Args:
        keyword: Search keyword
        entry_points: List of entry URLs to try (default: predefined list)
        max_retries: Maximum retry attempts per entry (default: 3)
        retry_delay: Delay in seconds between retries (default: 2)
        timeout: Request timeout in seconds (default: 15)
    """
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
    )

    # 使用默认入口（如果未提供）
    if entry_points is None:
        entry_points = DEFAULT_ENTRY_POINTS

    final_response = None
    print(f"[*] 开始搜索关键词: {keyword}")

    successful_entry = None

    # --- 入口追踪逻辑（带重试机制） ---
    for entry_url in entry_points:
        print(f"[*] 尝试入口: {entry_url}")

        # 对每个入口进行重试
        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    print(f"[*] 第 {attempt} 次重试 {entry_url}...")
                    time.sleep(retry_delay)

                response, resolved_entry = _fetch_from_entry(
                    scraper,
                    entry_url,
                    keyword,
                    timeout,
                )
                if response is not None:
                    final_response = response
                    successful_entry = resolved_entry or entry_url
                    print(
                        f"[+] 成功通过入口: {entry_url} -> {successful_entry} "
                        f"(尝试 {attempt}/{max_retries})"
                    )
                    break
                print(f"[!] 入口未发现可用搜索表单或搜索页: {entry_url}")

            except Exception as e:
                error_msg = str(e)
                # 简化错误信息显示
                if "Max retries exceeded" in error_msg:
                    print(f"[!] 入口 {entry_url} 连接超时 (尝试 {attempt}/{max_retries})")
                elif "NameResolutionError" in error_msg or "getaddrinfo failed" in error_msg:
                    print(f"[!] 入口 {entry_url} DNS解析失败 (尝试 {attempt}/{max_retries})")
                else:
                    print(f"[!] 入口 {entry_url} 访问失败 (尝试 {attempt}/{max_retries}): {error_msg[:100]}")

                # 如果是最后一次尝试，继续下一个入口
                if attempt == max_retries:
                    print(f"[!] 入口 {entry_url} 在 {max_retries} 次尝试后仍然失败，尝试下一个入口")
                    break

        # 如果成功获取到结果，跳出外层循环
        if final_response is not None and final_response.status_code == 200:
            break

    if final_response is None:
        print(f"[!] 所有入口均失败，已尝试 {len(entry_points)} 个入口，每个入口重试 {max_retries} 次")
        print("[!] 无法访问搜索结果，请检查网络连接")
        return []

    # --- 核心解析逻辑 ---
    soup = BeautifulSoup(final_response.text, 'html.parser')
    all_boxes = soup.find_all('div', class_='ssbox', limit=20) # 稍微增加一点限制

    results = []

    if not all_boxes:
        print("[-] 未找到相关搜索结果。")
        return results

    print(f"\n[+] 成功提取到 {len(all_boxes)} 条结果，正在解析...\n")

    # 加载敏感词库
    ban_words = []
    ban_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banwords.txt")
    if os.path.exists(ban_file):
        try:
            with open(ban_file, "r", encoding="utf-8") as f:
                ban_words = [line.strip() for line in f if line.strip()]
            print(f"[*] 已加载 {len(ban_words)} 个敏感词")
        except Exception as e:
            print(f"[!] 加载敏感词失败: {e}")

    for box in all_boxes:
        # 1. 提取标题
        title_tag = box.find('div', class_='title')
        title = title_tag.get_text(strip=True) if title_tag else "未知标题"

        # 敏感词过滤
        if ban_words:
            for word in ban_words:
                if word in title:
                    title = title.replace(word, "*" * len(word))

        # 2. 提取 sbar 中的信息
        sbar = box.find('div', class_='sbar')
        spans = sbar.find_all('span') if sbar else []

        info = {
            'title': title,
            'add_time': "未知",
            'size': "未知",
            'hot': "未知",
            'magnet': "未找到磁力链接"
        }

        for span in spans:
            text = span.get_text()
            if "添加时间:" in text:
                info['add_time'] = span.find('b').get_text(strip=True) if span.find('b') else "未知"
            elif "大小:" in text:
                info['size'] = span.find('b').get_text(strip=True) if span.find('b') else "未知"
            elif "热度:" in text:
                info['hot'] = span.find('b').get_text(strip=True) if span.find('b') else "未知"

            # 寻找磁力链接
            a_tag = span.find('a', href=True)
            if a_tag and "magnet:?" in a_tag['href']:
                info['magnet'] = a_tag['href']

        results.append(info)

    return results

def generate_html_content(keyword, results, sender=""):
    """
    Generating clean HTML content for PDF export.
    Obfuscation removed as per user request.
    """

    items_html = ""
    for i, item in enumerate(results, 1):
        preview_items = []
        for preview in item.get("preview_images", []):
            if not isinstance(preview, dict) or not preview.get("src"):
                continue
            source = html_module.escape(str(preview["src"]), quote=True)
            blurred = bool(preview.get("blurred"))
            badge = '<span class="preview-badge">已模糊</span>' if blurred else ""
            preview_items.append(
                f'<div class="preview-item"><img src="{source}" alt="资源预览">{badge}</div>'
            )
        preview_html = ""
        if preview_items:
            preview_html = f"""
            <div class="preview-section">
                <div class="preview-title">资源预览</div>
                <div class="preview-grid">{''.join(preview_items[:8])}</div>
            </div>
            """

        title = html_module.escape(str(item.get("title", "未知标题")))
        size = html_module.escape(str(item.get("size", "未知")))
        hot = html_module.escape(str(item.get("hot", "未知")))
        add_time = html_module.escape(str(item.get("add_time", "未知")))
        magnet = html_module.escape(str(item.get("magnet", "未找到磁力链接")))
        items_html += f"""
        <div class="card">
            <div class="header">
                <span class="index">{i}</span>
                <div class="meta">
                    <span class="badge size">{size}</span>
                    <span class="badge hot">🔥 {hot}</span>
                    <span class="date">{add_time}</span>
                </div>
            </div>

            <!-- 标题区域：正常显示，无干扰 -->
            <div class="title-container">
                <div class="title-text">{title}</div>
            </div>

            {preview_html}

            <div class="magnet-box">
                <div class="magnet-link">{magnet}</div>
            </div>
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <style>
            :root {{
                --bg-color: #ffffff; /* PDF 背景通常为白 */
                --card-bg: #ffffff;
                --primary: #1890ff;
                --text-main: #333333;
                --text-sub: #666666;
                --border: #e8e8e8;
            }}
            body {{
                font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
                background-color: var(--bg-color);
                margin: 0;
                padding: 40px; /* A4 纸边距 */
                color: var(--text-main);
            }}
            .search-header {{
                text-align: center;
                margin-bottom: 30px;
                color: #555;
                font-size: 12px;
                border-bottom: 1px solid #eee;
                padding-bottom: 10px;
            }}
            .card {{
                background: var(--card-bg);
                border-radius: 8px;
                padding: 15px;
                margin-bottom: 15px;
                border: 1px solid var(--border);
                page-break-inside: avoid; /* 防止卡片在分页时被切断 */
            }}

            .header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 10px;
            }}
            .index {{
                font-weight: bold;
                font-size: 16px;
                color: #999;
            }}
            .meta {{
                display: flex;
                gap: 10px;
                font-size: 12px;
            }}
            .badge {{
                padding: 2px 8px;
                border-radius: 4px;
                font-weight: bold;
            }}
            .badge.size {{ background: #e6f7ff; color: #1890ff; }}
            .badge.hot {{ background: #fff1f0; color: #ff4d4f; }}
            .date {{ color: #999; }}

            .title-container {{
                margin: 10px 0;
            }}
            .title-text {{
                font-size: 16px;
                font-weight: bold;
                line-height: 1.4;
                color: #222;
                word-wrap: break-word;
            }}

            .preview-section {{
                margin: 12px 0 10px;
            }}
            .preview-title {{
                margin-bottom: 6px;
                color: #666;
                font-size: 11px;
                font-weight: bold;
            }}
            .preview-grid {{
                display: grid;
                grid-template-columns: repeat(4, minmax(0, 1fr));
                gap: 6px;
            }}
            .preview-item {{
                position: relative;
                overflow: hidden;
                aspect-ratio: 16 / 9;
                border: 1px solid #e3e7eb;
                border-radius: 5px;
                background: #202a30;
            }}
            .preview-item img {{
                display: block;
                width: 100%;
                height: 100%;
                object-fit: cover;
            }}
            .preview-badge {{
                position: absolute;
                left: 5px;
                bottom: 5px;
                padding: 2px 5px;
                border-radius: 3px;
                background: rgba(20, 27, 32, .78);
                color: #fff;
                font-size: 8px;
                font-weight: bold;
            }}

            .magnet-box {{
                margin-top: 10px;
                background: #f9f9f9;
                padding: 8px;
                border-radius: 4px;
                border: 1px solid #eee;
            }}
            .magnet-link {{
                font-family: 'Courier New', Courier, monospace;
                font-size: 11px;
                color: #555;
                word-break: break-all;
            }}
        </style>
    </head>
    <body>
        <div class="search-header">
            搜索关键词: <strong>{html_module.escape(str(keyword))}</strong> | 请求人: <strong>{html_module.escape(str(sender if sender else '未知'))}</strong> | 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}
        </div>
        <div class="container">
            {items_html}
        </div>
    </body>
    </html>
    """
    return html

def _render_html_to_pdf_locked(html_content, output_pdf_path):
    """
    Use Selenium CDP (Chrome DevTools Protocol) to print to PDF.
    This creates a searchable, text-based PDF.
    """
    # 1. Save a unique temp HTML to the same directory as the PDF.
    pdf_dir = os.path.dirname(os.path.abspath(output_pdf_path))
    os.makedirs(pdf_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".html",
        prefix="magnet_result_",
        dir=pdf_dir,
        delete=False,
    ) as temp_html:
        temp_html.write(html_content)
        temp_html_path = temp_html.name


    print(f"[*] 临时 HTML 已生成: {temp_html_path}")

    # 2. Configure Selenium
    chrome_options = Options()
    chrome_options.add_argument('--headless') # Must be headless for printToPDF
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--no-proxy-server')

    # urllib3 otherwise proxies the local ChromeDriver connection through the
    # machine's HTTP proxy. Only augment localhost bypasses for this render.
    previous_no_proxy = os.environ.get("NO_PROXY")
    previous_no_proxy_lower = os.environ.get("no_proxy")
    local_bypass = "localhost,127.0.0.1"
    existing_bypass = previous_no_proxy or previous_no_proxy_lower or ""
    combined_bypass = ",".join(
        part for part in (existing_bypass, local_bypass) if part
    )
    os.environ["NO_PROXY"] = combined_bypass
    os.environ["no_proxy"] = combined_bypass
    previous_se_avoid_stats = os.environ.get("SE_AVOID_STATS")
    previous_se_offline = os.environ.get("SE_OFFLINE")
    os.environ["SE_AVOID_STATS"] = "true"
    # The project already has a matching driver in Selenium's local cache.
    # Offline mode avoids a needless network version probe on every search.
    os.environ["SE_OFFLINE"] = "true"

    driver = None
    try:
        driver = webdriver.Chrome(options=chrome_options)
        # 3. Load Page
        driver.get(Path(temp_html_path).resolve().as_uri())
        time.sleep(1) # Wait for render

        # 4. Execute CDP Command for PDF
        # https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-printToPDF
        print_options = {
            'landscape': False,
            'displayHeaderFooter': False,
            'printBackground': True,
            'preferCSSPageSize': True,
        }

        result = driver.execute_cdp_cmd("Page.printToPDF", print_options)

        # 5. Save PDF
        with open(output_pdf_path, 'wb') as f:
            f.write(base64.b64decode(result['data']))

        print(f"[+] PDF 已保存至: {output_pdf_path}")
        return True

    except Exception as e:
        print(f"[!] PDF 生成失败: {e}")
        return False
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        if previous_no_proxy is None:
            os.environ.pop("NO_PROXY", None)
        else:
            os.environ["NO_PROXY"] = previous_no_proxy
        if previous_no_proxy_lower is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = previous_no_proxy_lower
        if previous_se_avoid_stats is None:
            os.environ.pop("SE_AVOID_STATS", None)
        else:
            os.environ["SE_AVOID_STATS"] = previous_se_avoid_stats
        if previous_se_offline is None:
            os.environ.pop("SE_OFFLINE", None)
        else:
            os.environ["SE_OFFLINE"] = previous_se_offline
        # 清理临时HTML文件
        try:
            if os.path.exists(temp_html_path):
                os.remove(temp_html_path)
        except Exception:
            pass


def render_html_to_pdf(html_content, output_pdf_path):
    """Serialize browser PDF rendering because it temporarily adjusts process proxies."""
    with _PDF_RENDER_LOCK:
        return _render_html_to_pdf_locked(html_content, output_pdf_path)

def main():
    keyword = "哪吒" # 默认关键词

    results = fetch_magnet_links(keyword)

    if results:
        html = generate_html_content(keyword, results)
        output_path = f"magnet_result_{int(time.time())}.pdf" # Change extension to .pdf
        render_html_to_pdf(html, output_path)

        # Open the PDF
        try:
            os.startfile(output_path)
        except:
            pass
    else:
        print("[-] 无结果，未生成文件。")

if __name__ == "__main__":
    main()
