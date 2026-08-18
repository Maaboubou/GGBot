"""
菜单翻译插件 - HTML生成与PDF渲染模块
"""

import os
import base64
import time
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


def _get_category_icon(section_name: str) -> str:
    """根据分区名称返回对应图标"""
    name = section_name.lower()
    if any(k in name for k in ["酒", "drink", "beverage", "wine", "alcohol", "cocktail"]):
        return "🍷"
    if any(k in name for k in ["甜", "dessert", "cake", "ice cream", "sweet"]):
        return "🍰"
    if any(k in name for k in ["主菜", "main", "course", "entrée", "steak", "pasta", "pizza"]):
        return "🥘"
    if any(k in name for k in ["前菜", "start", "appetizer", "soup", "salad", "cold"]):
        return "🥗"
    if any(k in name for k in ["海鲜", "sea", "fish"]):
        return "🦞"
    if any(k in name for k in ["肉", "meat", "beef", "pork", "lamb", "chicken"]):
        return "🥩"
    if any(k in name for k in ["素", "veg", "plant"]):
        return "🥦"
    if any(k in name for k in ["早餐", "breakfast", "brunch", "egg"]):
        return "🍳"
    if any(k in name for k in ["咖啡", "coffee", "tea"]):
        return "☕"
    return "🍽️"


def generate_html(menu_data: Dict[str, Any]) -> str:
    """
    将菜单JSON数据渲染为精美的双语HTML (Modern Elegant Style)。
    - 瀑布流/双栏布局
    - 智能图标
    - 杂志级排版
    """
    restaurant_name = menu_data.get("restaurant_name", "")
    sections = menu_data.get("sections", [])

    # 构建各分区 HTML
    sections_html = ""
    for section in sections:
        section_name_zh = section.get("section_name_zh", "")
        section_name_orig = section.get("section_name_orig", "")
        items = section.get("items", [])
        
        icon = _get_category_icon(section_name_zh + section_name_orig)

        items_html = ""
        for item in items:
            name_zh = item.get("name_zh", "")
            name_orig = item.get("name_orig", "")
            desc_zh = item.get("desc_zh", "")
            desc_orig = item.get("desc_orig", "")
            price = item.get("price", "")
            
            desc_block = ""
            if desc_zh or desc_orig:
                desc_block = f"""
                <div class="item-desc">
                    <div class="desc-line desc-zh">{desc_zh}</div>
                    {f'<div class="desc-line desc-orig">{desc_orig}</div>' if desc_orig else ''}
                </div>"""

            price_block = f'<div class="item-price">{price}</div>' if price else ""

            items_html += f"""
            <div class="menu-item">
                <div class="item-text-block">
                    <div class="name-zh">{name_zh}</div>
                    {f'<div class="name-orig">{name_orig}</div>' if name_orig else ''}
                    {desc_block}
                </div>
                {price_block}
            </div>"""

        section_orig_display = f"<span class='section-orig'>{section_name_orig}</span>" if section_name_orig else ""
        
        sections_html += f"""
        <div class="section-card">
            <div class="section-header">
                <div class="section-icon">{icon}</div>
                <div class="section-title">
                    <div class="section-zh">{section_name_zh}</div>
                    {section_orig_display}
                </div>
            </div>
            <div class="section-items">
                {items_html}
            </div>
        </div>"""

    restaurant_block = ""
    if restaurant_name:
        restaurant_block = f'<h1 class="restaurant-name">{restaurant_name}</h1>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@500;600;700&family=Noto+Sans+SC:wght@400;500;600;700&family=Playfair+Display:wght@600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #FAF9F6; /* 微黄羊皮纸色，防反光光晕 */
            --card-bg: #FFFFFF;
            --primary: #3E2723; /* 深巧克力色 */
            --accent: #B08D55;
            --text-main: #333333; /* 深炭灰 */
            --text-sub: #4A4A4A;
            --text-meta: #5A5A5A;
            --text-dark: #3E2723; 
            --divider: #F0F0F0; /* 极淡的实线 */
        }}

        * {{ box-sizing: border-box; margin: 0; padding: 0; }}

        body {{
            font-family: 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            width: 1080px; /* 适合手机观看 */
            margin: 0 auto;
            padding: 60px;
        }}

        /* 顶部区域 */
        .page-header {{
            text-align: center;
            margin-bottom: 60px;
            padding-bottom: 30px;
            border-bottom: 2px solid var(--accent);
        }}
        
        .logo-mark {{
            font-size: 36px;
            color: var(--accent);
            margin-bottom: 15px;
        }}

        .restaurant-name {{
            font-family: 'Playfair Display', 'Noto Sans SC', serif;
            font-size: 52px;
            font-weight: 700;
            color: var(--primary);
            letter-spacing: 2px;
            margin-bottom: 16px;
            line-height: 1.2;
        }}

        .meta-info {{
            font-size: 18px;
            color: var(--text-meta);
            text-transform: uppercase;
            letter-spacing: 2px;
            font-weight: 600;
            font-family: 'Inter', sans-serif;
        }}

        /* 布局 */
        .menu-container {{
            max-width: 960px;
            margin: 0 auto;
        }}
        
        /* 分区 */
        .section-card {{
            margin-bottom: 60px;
            break-inside: avoid;
        }}

        .section-header {{
            text-align: center;
            margin-bottom: 45px;
            position: relative;
        }}

        .section-icon {{
            font-size: 40px;
            margin-bottom: 12px;
            display: block;
        }}

        .section-title {{
            display: flex;
            flex-direction: column;
            align-items: center;
        }}

        .section-zh {{
            font-family: 'Noto Sans SC', sans-serif;
            font-size: 38px;
            font-weight: 700;
            color: var(--primary);
            margin-bottom: 6px;
        }}

        .section-orig {{
            font-family: 'Inter', sans-serif;
            font-size: 22px;
            font-weight: 600;
            color: var(--accent);
            text-transform: uppercase;
            letter-spacing: 1px;
        }}

        /* 菜品列表 - 强化抗干扰与长行能力 */
        .menu-item {{
            margin-bottom: 35px;
            padding-bottom: 35px;
            border-bottom: 2px solid var(--divider);
            break-inside: avoid;
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
        }}
        
        /* 去除最后一个元素的底边框 */
        .menu-item:last-child {{
            border-bottom: none;
            margin-bottom: 10px;
        }}

        /* 文本区域占据左侧 */
        .item-text-block {{
            flex: 1;
            padding-right: 30px;
        }}

        .name-zh {{
            font-family: 'Noto Sans SC', 'PingFang SC', sans-serif;
            font-size: 32px;
            font-weight: 600; /* Semi-Bold 避免细边框断裂 */
            color: var(--primary);
            line-height: 1.4;
            display: block;
            margin-bottom: 8px;
        }}

        .name-orig {{
            font-family: 'Inter', 'Roboto', 'Montserrat', sans-serif;
            font-size: 26px; /* 中文字号32px的约81% */
            font-weight: 500; /* Medium 权重，弃用斜体 */
            color: var(--text-dark);
            line-height: 1.3;
            margin-bottom: 12px;
            display: block;
        }}

        .item-desc {{
            margin-top: 10px;
            color: var(--text-sub);
        }}

        .desc-line {{
            font-size: 20px;
            line-height: 1.6;
            color: var(--text-sub);
            font-weight: 400;
        }}
        
        .desc-orig {{
            color: var(--text-sub);
            font-family: 'Inter', sans-serif;
            font-size: 18px;
            margin-top: 6px;
        }}

        /* 价格强制推向右上角 */
        .item-price {{
            font-family: 'Courier New', Courier, 'Inter', monospace;
            font-size: 32px;
            font-weight: 700;
            color: var(--text-dark);
            white-space: nowrap;
            text-align: right;
            padding-top: 2px;
        }}

        /* 页脚 */
        .page-footer {{
            text-align: center;
            margin-top: 60px;
            font-size: 16px;
            color: var(--text-meta);
            font-family: 'Inter', sans-serif;
            opacity: 0.8;
            margin-bottom: 40px; 
            font-weight: 500;
        }}
    </style>
</head>
<body>
    <div class="page-header">
        <div class="logo-mark">✦</div>
        {restaurant_block}
        <div class="meta-info">
            Menu Translation · {time.strftime('%Y.%m.%d')}
        </div>
    </div>

    <div class="menu-container">
        {sections_html}
    </div>

    <div class="page-footer">
        AI-Powered Translation • For Reference Only • Generated by Menu Translator
    </div>
</body>
</html>"""
    return html


def render_image(html_content: str, output_image_path: str) -> bool:
    """
    使用 Selenium CDP 将 HTML 渲染为全页长图 (PNG)。
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    img_dir = os.path.dirname(os.path.abspath(output_image_path))
    os.makedirs(img_dir, exist_ok=True)
    temp_html_path = os.path.join(img_dir, "temp_menu_result.html")

    try:
        with open(temp_html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        logger.info(f"📄 临时 HTML 已写入: {temp_html_path}")

        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--window-size=1080,1920")

        driver = webdriver.Chrome(options=chrome_options)
        try:
            driver.get(f"file:///{temp_html_path}")
            time.sleep(2)  # 等待加载字体与渲染

            # 获取页面实际宽高
            metrics = driver.execute_cdp_cmd("Page.getLayoutMetrics", {})
            width = metrics["contentSize"]["width"]
            height = metrics["contentSize"]["height"]

            # 设置设备视口，以便截图能包含全部高度
            driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "mobile": False,
                "width": int(width),
                "height": int(height),
                "deviceScaleFactor": 1,
            })

            # 使用 CDP 的 CaptureScreenshot 支持长图截取
            screenshot = driver.execute_cdp_cmd("Page.captureScreenshot", {
                "format": "png",
                "captureBeyondViewport": True
            })

            with open(output_image_path, "wb") as f:
                f.write(base64.b64decode(screenshot["data"]))

            logger.info(f"✅ 图片已生成: {output_image_path}")
            return True

        finally:
            driver.quit()

    except Exception as e:
        logger.error(f"❌ 图片生成失败: {e}", exc_info=True)
        return False
    finally:
        try:
            if os.path.exists(temp_html_path):
                os.remove(temp_html_path)
        except Exception:
            pass
