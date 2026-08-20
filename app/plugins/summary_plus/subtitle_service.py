"""Subtitle extraction helpers for summary_plus."""

import os
import re
import uuid
from typing import Optional

import requests
import yt_dlp

__all__ = [
    "srt_to_txt",
    "bili_get_subtitles",
]


def _log(logger, level: str, message: str):
    if logger is None:
        return
    fn = getattr(logger, level, None)
    if callable(fn):
        fn(message)


def srt_to_txt(srt_path: str, logger=None) -> Optional[str]:
    if not os.path.exists(srt_path):
        return None

    _log(
        logger,
        "info",
        f"[*] 正在从字幕提取纯文本: {os.path.basename(srt_path)}",
    )
    try:
        with open(srt_path, "r", encoding="utf-8") as f:
            content = f.read()

        clean_content = re.sub(r"\d+\n\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}", "", content)
        clean_content = re.sub(r"<[^>]*>", "", clean_content)
        lines = [line.strip() for line in clean_content.split("\n") if line.strip()]

        unique_lines = []
        for line in lines:
            if not unique_lines or line != unique_lines[-1]:
                unique_lines.append(line)

        text = "\n".join(unique_lines)
        _log(logger, "info", f"[+] 字幕纯文本提取完成 ({len(text)} 字)")
        return text
    except Exception as e:
        _log(logger, "error", f"[!] 提取文本失败: {e}")
        return None
    finally:
        if os.path.exists(srt_path):
            os.remove(srt_path)


def bili_get_subtitles(
    url: str,
    cookies_path: str,
    logger=None,
    temp_dir: Optional[str] = None,
) -> Optional[str]:
    """尝试获取 B 站字幕 (优先使用 yt-dlp 获取手动/AI 字幕，失败则回退到 API)"""
    match = re.search(r"BV[a-zA-Z0-9]+", url)
    if not match:
        return None
    bvid = match.group()

    _log(logger, "info", f"[*] 正在通过 yt-dlp 探测字幕: {url}")
    if os.path.exists(cookies_path):
        _log(logger, "info", f"[*] 使用 Cookie 文件: {os.path.basename(cookies_path)}")
    else:
        _log(logger, "warning", "[*] 未找到 Cookie 文件，获取 AI 字幕可能失败。")

    check_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    if os.path.exists(cookies_path):
        check_opts["cookiefile"] = cookies_path

    try:
        with yt_dlp.YoutubeDL(check_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            manual_subs = {k: v for k, v in info.get("subtitles", {}).items() if k != "danmaku"}

            if manual_subs:
                _log(logger, "info", f"[+] 发现手动上传字幕: {list(manual_subs.keys())}，优先下载。")
                langs_to_download = list(manual_subs.keys())
                write_auto = False
            else:
                _log(logger, "info", "[-] 未发现手动上传字幕，尝试获取 AI 生成字幕 (ai-zh / zh-Hans)。")
                langs_to_download = ["ai-zh", "zh-Hans", "zh-CN", "zh"]
                write_auto = True

        tmp_sub_dir = temp_dir or os.path.join(os.getcwd(), "tmp", "subtitles")
        os.makedirs(tmp_sub_dir, exist_ok=True)
        uid = uuid.uuid4().hex[:6]
        outtmpl = os.path.join(tmp_sub_dir, f"sub_{uid}_%(title)s.%(ext)s")

        ydl_opts = {
            "writesubtitles": True,
            "writeautomaticsub": write_auto,
            "subtitleslangs": langs_to_download,
            "skip_download": True,
            "outtmpl": outtmpl,
            "postprocessors": [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}],
            "quiet": True,
        }
        if os.path.exists(cookies_path):
            ydl_opts["cookiefile"] = cookies_path

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

            found_text = ""
            for file in os.listdir(tmp_sub_dir):
                if file.startswith(f"sub_{uid}") and file.endswith(".srt"):
                    srt_path = os.path.join(tmp_sub_dir, file)
                    text = srt_to_txt(srt_path, logger=logger)
                    if text:
                        found_text += text + "\n"

            if found_text.strip():
                _log(logger, "info", f"[+] yt-dlp 成功获取字幕 (约 {len(found_text)} 字)")
                return found_text.strip()

    except Exception as e:
        _log(logger, "warning", f"⚠️ yt-dlp 获取字幕失败: {e}")
        if "Cookies" in str(e):
            _log(logger, "warning", "建议检查 B 站登录状态。")

    _log(logger, "info", f"[*] 尝试通过 API 探测字幕: {bvid}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.bilibili.com/",
    }
    try:
        view_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        resp = requests.get(view_url, headers=headers, timeout=5)
        res_json = resp.json()
        if res_json.get("code") != 0:
            _log(logger, "warning", f"[!] API 报错: {res_json.get('message')}")
            return None

        data = res_json.get("data", {})
        subtitle_list = data.get("subtitle", {}).get("subtitles", [])
        if not subtitle_list:
            _log(logger, "info", "[*] API 未发现 CC 字幕。")
            return None

        target_sub = None
        priority = ["zh-Hans", "zh-CN", "zh", "zh-Hant", "en"]
        for p in priority:
            for sub in subtitle_list:
                if sub.get("lan") == p:
                    target_sub = sub
                    break
            if target_sub:
                break
        if not target_sub:
            target_sub = subtitle_list[0]

        sub_url = target_sub.get("subtitle_url")
        if not sub_url:
            return None
        if not sub_url.startswith("http"):
            sub_url = "https:" + sub_url

        _log(logger, "info", f"[+] API 发现字幕: {target_sub.get('lan_doc')}")
        sub_resp = requests.get(sub_url, headers=headers, timeout=5)
        sub_json = sub_resp.json()

        lines = []
        for item in sub_json.get("body", []):
            content = item.get("content", "").strip()
            if content:
                lines.append(content)
        if not lines:
            return None

        full_text = " ".join(lines)
        _log(logger, "info", f"[+] 成功通过 API 获取字幕 (约 {len(full_text)} 字)")
        return full_text
    except Exception as e:
        _log(logger, "warning", f"[!] API 字幕探测异常: {e}")
        return None
