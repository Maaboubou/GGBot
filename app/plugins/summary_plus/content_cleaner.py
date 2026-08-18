"""Text cleaning helpers for web/news extraction in summary_plus."""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
ONLY_BULLET_RE = re.compile(r"^[-*•]+$")
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


START_REGEXES = [
    re.compile(r"^#\s+.+"),
]


END_REGEXES = [
    re.compile(r"^##\s+Read Next$", re.IGNORECASE),
    re.compile(r"^###\s+Site Index$", re.IGNORECASE),
    re.compile(r"^###\s+About Reuters$", re.IGNORECASE),
    re.compile(r"^###\s+Follow Us$", re.IGNORECASE),
    re.compile(r"^Our Standards:", re.IGNORECASE),
    re.compile(r"^\*+\s*Suggested Topics:", re.IGNORECASE),
    re.compile(r"^Purchase Licensing Rights$", re.IGNORECASE),
    re.compile(r"^All quotes delayed a minimum of 15 minutes\.", re.IGNORECASE),
    re.compile(r"^HomeBTV\+", re.IGNORECASE),
    re.compile(r"^Terms of Service", re.IGNORECASE),
    re.compile(r"^NewslettersExplainers", re.IGNORECASE),
]


DROP_EXACT = {
    "Text",
    "Small Text",
    "Medium Text",
    "Large Text",
    "X",
    "Facebook",
    "Linkedin",
    "Email",
    "Link",
    "FacebookXLinkedIn",
    "EmailLink",
    "Gift",
    "Expand",
    "BookmarkSave",
    "GiftGift this article",
    "Provide news feedback or report an error",
    "Send a tip to our reporters",
    "Take our SurveyNew Window",
}


DROP_CONTAINS = (
    "Purchase Licensing Rights",
    "Sign up here.",
    "Contact us:",
    "Confidential tip?",
    "Site feedback:",
    "Do Not Sell or Share My Personal Information",
    "Terms of Service",
    "Privacy Policy",
    "CareersAdvertise",
    "NewslettersExplainers",
    "HomeBTV+",
    "Ad Ch",
    "Photographer:",
)


@dataclass
class CleanStats:
    raw_chars: int
    cleaned_chars: int
    raw_lines: int
    cleaned_lines: int

    def as_dict(self) -> Dict[str, float]:
        ratio = 0.0
        if self.raw_chars > 0:
            ratio = self.cleaned_chars / self.raw_chars
        return {
            "raw_chars": self.raw_chars,
            "cleaned_chars": self.cleaned_chars,
            "raw_lines": self.raw_lines,
            "cleaned_lines": self.cleaned_lines,
            "retain_ratio": round(ratio, 4),
            "reduction_pct": round((1 - ratio) * 100, 2) if self.raw_chars > 0 else 0.0,
        }


def _normalize_line(line: str) -> str:
    line = ZERO_WIDTH_RE.sub("", line).replace("\xa0", " ")
    line = MARKDOWN_IMAGE_RE.sub("", line)
    line = MARKDOWN_LINK_RE.sub(r"\1", line)
    line = MULTI_SPACE_RE.sub(" ", line).strip()
    return line


def _looks_like_html(text: str) -> bool:
    low = (text or "").lower()
    return "<html" in low or "<!doctype html" in low


def _format_iso_datetime(iso_text: str) -> str:
    if not iso_text:
        return ""
    try:
        dt = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    except Exception:
        return iso_text


def _format_epoch_datetime(value) -> str:
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            dt = datetime.utcfromtimestamp(ts)
            return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            return str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def _collect_value_text(node, out: List[str]) -> None:
    if isinstance(node, dict):
        val = node.get("value")
        if isinstance(val, str) and val.strip():
            out.append(val.strip())
        for v in node.values():
            _collect_value_text(v, out)
    elif isinstance(node, list):
        for item in node:
            _collect_value_text(item, out)


def _extract_bloomberg_story_from_html(html: str) -> str:
    if not html or "bloomberg.com" not in html.lower():
        return ""
    m = NEXT_DATA_RE.search(html)
    if not m:
        return ""
    try:
        data = json.loads(m.group(1))
    except Exception:
        return ""

    story = (
        data.get("props", {})
        .get("pageProps", {})
        .get("story", {})
    )
    if not isinstance(story, dict):
        return ""

    headline = (story.get("headline") or "").strip()
    if not headline:
        return ""

    lines: List[str] = [f"# {headline}"]

    byline = (story.get("byline") or "").strip()
    if byline:
        lines.append("")
        lines.append(byline)

    published = _format_iso_datetime((story.get("publishedAt") or "").strip())
    if published:
        lines.append("")
        lines.append(published)

    body = story.get("body", {})
    content = body.get("content", []) if isinstance(body, dict) else []
    if not isinstance(content, list):
        return "\n".join(lines).strip()

    lines.append("")
    for node in content:
        if not isinstance(node, dict):
            continue
        node_type = (node.get("type") or "").strip().lower()
        if node_type in {"ad", "inline-newsletter", "video", "image"}:
            continue

        parts: List[str] = []
        _collect_value_text(node, parts)
        text = " ".join(" ".join(parts).split()).strip()
        if not text:
            continue
        if text in DROP_EXACT:
            continue
        if any(marker in text for marker in DROP_CONTAINS):
            continue
        if node_type in {"heading", "subheading"}:
            lines.append(f"## {text}")
        else:
            lines.append(text)
        lines.append("")

    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _html_fragment_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    text = fragment
    # Preserve paragraph/heading structure before stripping tags.
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</h[1-6]\s*>", "\n\n", text)
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = ZERO_WIDTH_RE.sub("", text).replace("\xa0", " ")
    text = MULTI_SPACE_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_nikkei_story_from_html(html: str) -> str:
    if not html or "asia.nikkei.com" not in html.lower():
        return ""
    m = NEXT_DATA_RE.search(html)
    if not m:
        return ""
    try:
        data = json.loads(m.group(1))
    except Exception:
        return ""

    article = (
        data.get("props", {})
        .get("pageProps", {})
        .get("data", {})
    )
    if not isinstance(article, dict):
        return ""

    headline = (article.get("headline") or "").strip()
    body_html = (article.get("body") or "").strip()
    if not headline or not body_html:
        return ""

    lines: List[str] = [f"# {headline}"]

    byline = (article.get("byline") or "").strip()
    if byline:
        lines.append("")
        lines.append(byline)

    display_date = _format_epoch_datetime(article.get("displayDate"))
    if display_date:
        lines.append("")
        lines.append(display_date)

    body_text = _html_fragment_to_text(body_html)
    if body_text:
        lines.append("")
        lines.append(body_text)

    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _find_start(lines: List[str]) -> int:
    for idx, line in enumerate(lines):
        for regex in START_REGEXES:
            if regex.match(line):
                return idx
    return 0


def _find_end(lines: List[str], start_idx: int) -> int:
    for idx in range(start_idx, len(lines)):
        line = lines[idx]
        for regex in END_REGEXES:
            if regex.search(line):
                return idx
    return len(lines)


def _is_noise_line(line: str) -> bool:
    if not line:
        return True
    canonical = line.lstrip("*- ").strip()
    if canonical.startswith("[]("):
        return True
    if canonical.startswith("]("):
        return True
    if canonical.startswith("http://") or canonical.startswith("https://"):
        return True
    if canonical in DROP_EXACT:
        return True
    for marker in DROP_CONTAINS:
        if marker in line:
            return True
    if ONLY_BULLET_RE.match(line):
        return True
    if line.lower().startswith("skip to main content"):
        return True
    if line.lower().startswith("browse ") and len(line) < 40:
        return True
    if line.lower().endswith("category"):
        return True
    if line.startswith("ago[") and line.endswith("category"):
        return True
    return False


def clean_extracted_article_text(raw_text: str) -> str:
    """Clean noisy extraction output into model-ready article text."""
    if not raw_text:
        return ""

    if _looks_like_html(raw_text):
        bloomberg_text = _extract_bloomberg_story_from_html(raw_text)
        if bloomberg_text and len(bloomberg_text) >= 300:
            return bloomberg_text
        nikkei_text = _extract_nikkei_story_from_html(raw_text)
        if nikkei_text and len(nikkei_text) >= 300:
            return nikkei_text

    normalized_lines = [_normalize_line(x) for x in raw_text.splitlines()]
    start_idx = _find_start(normalized_lines)
    end_idx = _find_end(normalized_lines, start_idx)
    candidate_lines = normalized_lines[start_idx:end_idx]

    cleaned_lines: List[str] = []
    prev_blank = False
    for line in candidate_lines:
        if _is_noise_line(line):
            if not prev_blank:
                cleaned_lines.append("")
                prev_blank = True
            continue
        cleaned_lines.append(line)
        prev_blank = False

    text = "\n".join(cleaned_lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_clean_stats(raw_text: str, cleaned_text: str) -> CleanStats:
    return CleanStats(
        raw_chars=len(raw_text or ""),
        cleaned_chars=len(cleaned_text or ""),
        raw_lines=len((raw_text or "").splitlines()),
        cleaned_lines=len((cleaned_text or "").splitlines()),
    )


def strip_markdown(text: str) -> str:
    """移除 Markdown 和 HTML 格式"""
    if not text:
        return ""
    # 1. 移除 Markdown 代码块
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    # 2. 移除行内代码
    text = re.sub(r"`([^`]*)`", r"\1", text)
    # 3. 移除加粗/斜体
    text = re.sub(r"(\*\*|\*|_|~~)(.*?)\1", r"\2", text)
    # 4. 移除标题标记
    text = re.sub(r"^#+\s*", "", text, flags=re.M)

    # 5. 移除图片和链接 (保持链接文字)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # 6. 处理 HTML 标签
    # 将 <br> 和 <br/> 转换为换行符
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    # 移除其他常见的 HTML 标签 (保留内容)
    text = re.sub(r"</?(?:p|div|span|strong|em|b|i|u)>", "", text, flags=re.I)
    # 移除任何剩余的 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)

    return text.strip()
