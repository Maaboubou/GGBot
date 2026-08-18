import logging
import re
import time
import random
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

import certifi
import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import RequestBlocked, IpBlocked


logger = logging.getLogger(__name__)


MANUAL_PRIORITY = ["zh-CN", "zh-Hans", "zh", "zh-TW", "zh-HK", "en", "ja"]
GENERATED_PRIORITY = ["zh-CN", "zh-Hans", "zh", "zh-TW", "zh-HK", "en", "ja"]


@dataclass
class TranscriptResult:
    ok: bool
    video_id: str
    source: Optional[str] = None          # manual / generated / fallback_any
    language_code: Optional[str] = None
    language: Optional[str] = None
    is_generated: Optional[bool] = None
    text: str = ""
    segments: Optional[List[Dict[str, Any]]] = None
    available_tracks: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None
    used_proxy: Optional[Dict[str, str]] = None


def extract_video_id(url_or_id: str) -> str:
    s = url_or_id.strip()

    if re.fullmatch(r"[-_a-zA-Z0-9]{11}", s):
        return s

    patterns = [
        r"(?:v=)([-_a-zA-Z0-9]{11})",
        r"youtu\.be/([-_a-zA-Z0-9]{11})",
        r"/shorts/([-_a-zA-Z0-9]{11})",
        r"/embed/([-_a-zA-Z0-9]{11})",
    ]
    for p in patterns:
        m = re.search(p, s)
        if m:
            return m.group(1)

    raise ValueError(f"无法识别 YouTube video_id: {url_or_id}")


def build_api(
    user_agent: Optional[str] = None,
    proxy_urls: Optional[Dict[str, str]] = None,
) -> YouTubeTranscriptApi:
    session = requests.Session()
    session.trust_env = False
    session.verify = certifi.where()

    session.headers.update({
        "User-Agent": user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
    })

    if proxy_urls:
        session.proxies.update(proxy_urls)

    return YouTubeTranscriptApi(http_client=session)


def normalize_track_meta(transcript: Any) -> Dict[str, Any]:
    translation_languages = getattr(transcript, "translation_languages", None)
    if translation_languages:
        translation_languages = [
            {
                "language": getattr(item, "language", None),
                "language_code": getattr(item, "language_code", None),
            }
            for item in translation_languages
        ]

    return {
        "language": getattr(transcript, "language", None),
        "language_code": getattr(transcript, "language_code", None),
        "is_generated": getattr(transcript, "is_generated", None),
        "is_translatable": getattr(transcript, "is_translatable", None),
        "translation_languages": translation_languages,
    }


def fetch_track(track: Any, preserve_formatting: bool = False) -> Tuple[List[Dict[str, Any]], str]:
    fetched = track.fetch(preserve_formatting=preserve_formatting)
    raw = fetched.to_raw_data()

    text = "\n".join(
        (item.get("text") or "").replace("\xa0", " ").strip()
        for item in raw
        if (item.get("text") or "").strip()
    ).strip()

    return raw, text


def list_available_tracks(
    api: YouTubeTranscriptApi,
    video_id: str,
) -> Tuple[Any, List[Dict[str, Any]]]:
    transcript_list = api.list(video_id)
    tracks = [normalize_track_meta(track) for track in transcript_list]
    return transcript_list, tracks


def debug_print_tracks(tracks: List[Dict[str, Any]]) -> None:
    logger.debug("\n===== AVAILABLE TRACKS =====")
    if not tracks:
        logger.debug("(无可用轨道)")
    for i, track in enumerate(tracks, 1):
        logger.debug(
            f"{i}. language={track.get('language')} | "
            f"code={track.get('language_code')} | "
            f"is_generated={track.get('is_generated')} | "
            f"is_translatable={track.get('is_translatable')}"
        )
    logger.debug("============================\n")


def make_result_from_track(
    track: Any,
    source: str,
    used_proxy: Optional[Dict[str, str]],
    preserve_formatting: bool = False,
) -> TranscriptResult:
    segments, text = fetch_track(track, preserve_formatting=preserve_formatting)
    return TranscriptResult(
        ok=True,
        video_id=track.video_id,
        source=source,
        language_code=getattr(track, "language_code", None),
        language=getattr(track, "language", None),
        is_generated=getattr(track, "is_generated", None),
        text=text,
        segments=segments,
        used_proxy=used_proxy,
    )


def try_exact_priority(
    transcript_list: Any,
    language_priority: List[str],
    is_generated_target: bool,
    used_proxy: Optional[Dict[str, str]],
    preserve_formatting: bool = False,
) -> Optional[TranscriptResult]:
    candidates = list(transcript_list)

    for lang in language_priority:
        for track in candidates:
            if getattr(track, "is_generated", None) != is_generated_target:
                continue
            if getattr(track, "language_code", None) != lang:
                continue

            return make_result_from_track(
                track=track,
                source="generated" if is_generated_target else "manual",
                used_proxy=used_proxy,
                preserve_formatting=preserve_formatting,
            )

    return None


def try_any_fallback(
    transcript_list: Any,
    used_proxy: Optional[Dict[str, str]],
    preserve_formatting: bool = False,
) -> Optional[TranscriptResult]:
    """
    如果优先级列表里都没命中，就拿任意一个可用原文轨道。
    偏好：手动 > 自动
    """
    candidates = list(transcript_list)

    candidates.sort(key=lambda t: getattr(t, "is_generated", True))

    for track in candidates:
        try:
            return make_result_from_track(
                track=track,
                source="fallback_any",
                used_proxy=used_proxy,
                preserve_formatting=preserve_formatting,
            )
        except Exception:
            continue

    return None


def get_best_transcript(
    url_or_id: str,
    retries: int = 2,
    retry_base_sleep: float = 1.2,
    preserve_formatting: bool = False,
    proxy_urls: Optional[Dict[str, str]] = None,
    fallback_to_local_proxy: bool = True,
    local_proxy_port: int = 7897,
    debug: bool = True,
) -> Dict[str, Any]:
    """
    获取最佳原文字幕：
    1. 手动：中文 > 英文 > 日语
    2. 自动：中文 > 英文 > 日语
    3. 任意可用原文轨道（手动优先）
    4. 直连若触发 RequestBlocked / IpBlocked，则立即切本地代理
    """
    video_id = extract_video_id(url_or_id)
    last_error: Optional[Exception] = None

    local_proxy = {
        "http": f"http://127.0.0.1:{local_proxy_port}",
        "https": f"http://127.0.0.1:{local_proxy_port}",
    }

    proxy_candidates: List[Optional[Dict[str, str]]] = [proxy_urls]

    if fallback_to_local_proxy and proxy_urls is None:
        proxy_candidates.append(local_proxy)

    for current_proxy in proxy_candidates:
        for attempt in range(1, retries + 1):
            try:
                api = build_api(proxy_urls=current_proxy)
                transcript_list, tracks = list_available_tracks(api, video_id)

                if debug:
                    logger.info(f"当前代理: {current_proxy}")
                    debug_print_tracks(tracks)

                # 1) 手动优先
                result = try_exact_priority(
                    transcript_list=transcript_list,
                    language_priority=MANUAL_PRIORITY,
                    is_generated_target=False,
                    used_proxy=current_proxy,
                    preserve_formatting=preserve_formatting,
                )
                if result:
                    result.available_tracks = tracks
                    return asdict(result)

                # 2) 自动优先
                result = try_exact_priority(
                    transcript_list=transcript_list,
                    language_priority=GENERATED_PRIORITY,
                    is_generated_target=True,
                    used_proxy=current_proxy,
                    preserve_formatting=preserve_formatting,
                )
                if result:
                    result.available_tracks = tracks
                    return asdict(result)

                # 3) 任意原文轨道兜底
                result = try_any_fallback(
                    transcript_list=transcript_list,
                    used_proxy=current_proxy,
                    preserve_formatting=preserve_formatting,
                )
                if result:
                    result.available_tracks = tracks
                    return asdict(result)

                return asdict(TranscriptResult(
                    ok=False,
                    video_id=video_id,
                    available_tracks=tracks,
                    error="没有找到任何可用字幕轨道",
                    used_proxy=current_proxy,
                ))

            except (RequestBlocked, IpBlocked) as e:
                last_error = e
                logger.warning(
                    f"[attempt={attempt}] [proxy={current_proxy}] "
                    f"BLOCKED: {type(e).__name__}: {e}"
                )

                # 直连被封：立即切换到下一个代理候选，不在当前直连上浪费重试
                if current_proxy is None:
                    break

                # 代理模式下被封，才继续重试
                if attempt < retries:
                    sleep_s = retry_base_sleep * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
                    time.sleep(sleep_s)

            except Exception as e:
                last_error = e
                logger.warning(
                    f"[attempt={attempt}] [proxy={current_proxy}] "
                    f"ERROR: {type(e).__name__}: {e}"
                )

                if attempt < retries:
                    sleep_s = retry_base_sleep * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
                    time.sleep(sleep_s)

    return asdict(TranscriptResult(
        ok=False,
        video_id=video_id,
        error=f"{type(last_error).__name__}: {last_error}" if last_error else "未知错误",
    ))


def get_best_transcript_text(url_or_id: str, **kwargs: Any) -> str:
    result = get_best_transcript(url_or_id, **kwargs)
    if not result["ok"]:
        raise RuntimeError(result.get("error") or "获取字幕失败")
    return result["text"]
