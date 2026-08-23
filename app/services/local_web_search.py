"""Local web-search and page-reading primitives.

The service deliberately contains no model routing.  It turns explicit search
queries into normalized source records and can fetch a small number of public
pages for the caller.  The chatbot decides *when* to use the tool; DDGS only
does the network work.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Iterable, List, Optional
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)


class LocalWebSearchError(RuntimeError):
    """Raised when the local search backend cannot return usable results."""


class UnsafeFetchTarget(ValueError):
    """Raised when a page URL can reach a non-public network target."""


@dataclass
class SearchResult:
    query: str
    title: str
    url: str
    snippet: str
    source: str = ""
    excerpt: str = ""


class LocalWebSearchService:
    """DDGS-backed search with a small, SSRF-aware page reader."""

    def __init__(
        self,
        *,
        region: str = "cn-zh",
        safesearch: str = "moderate",
        timeout_seconds: int = 10,
        max_results: int = 6,
        fetch_max_pages: int = 2,
        fetch_max_bytes: int = 1_500_000,
        fetch_excerpt_chars: int = 5000,
        proxy: Optional[str] = None,
    ) -> None:
        self.region = str(region or "cn-zh").strip() or "cn-zh"
        self.safesearch = (
            str(safesearch or "moderate").strip().lower()
            if str(safesearch or "").strip().lower() in {"on", "moderate", "off"}
            else "moderate"
        )
        self.timeout_seconds = max(3, min(60, int(timeout_seconds or 10)))
        self.max_results = max(1, min(20, int(max_results or 6)))
        self.fetch_max_pages = max(0, min(5, int(fetch_max_pages or 0)))
        self.fetch_max_bytes = max(100_000, min(5_000_000, int(fetch_max_bytes or 1_500_000)))
        self.fetch_excerpt_chars = max(500, min(12_000, int(fetch_excerpt_chars or 5000)))
        self.proxy = str(proxy or "").strip() or None

    def search(
        self,
        queries: Iterable[str],
        *,
        time_limit: Optional[str] = None,
        fetch_pages: bool = True,
    ) -> List[SearchResult]:
        normalized_queries = []
        for raw_query in queries:
            query = " ".join(str(raw_query or "").split())[:400]
            if query and query not in normalized_queries:
                normalized_queries.append(query)
            if len(normalized_queries) >= 3:
                break
        if not normalized_queries:
            return []

        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise LocalWebSearchError("DDGS 未安装，请重新运行项目安装器") from exc

        supported_time_limit = time_limit if time_limit in {"d", "w", "m", "y"} else None
        results: List[SearchResult] = []
        seen_urls = set()
        errors = []
        per_query_limit = max(
            1,
            min(8, math.ceil(self.max_results / len(normalized_queries))),
        )

        for query in normalized_queries:
            try:
                client = DDGS(proxy=self.proxy, timeout=self.timeout_seconds)
                raw_results = client.text(
                    query,
                    region=self.region,
                    safesearch=self.safesearch,
                    timelimit=supported_time_limit,
                    max_results=per_query_limit,
                    backend="auto",
                )
            except Exception as exc:  # DDGS wraps several provider-specific failures.
                errors.append(f"{query}: {type(exc).__name__}: {exc}")
                logger.warning("DDGS search failed for %r: %s", query, exc)
                continue

            for raw in raw_results or []:
                if not isinstance(raw, dict):
                    continue
                url = str(raw.get("href") or raw.get("url") or "").strip()
                if not url or url in seen_urls:
                    continue
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    continue
                seen_urls.add(url)
                results.append(
                    SearchResult(
                        query=query,
                        title=" ".join(str(raw.get("title") or url).split())[:500],
                        url=url,
                        snippet=" ".join(str(raw.get("body") or raw.get("snippet") or "").split())[:2000],
                        source=str(raw.get("source") or parsed.hostname or "")[:200],
                    )
                )
                if len(results) >= self.max_results:
                    break
            if len(results) >= self.max_results:
                break

        if not results and errors:
            raise LocalWebSearchError("；".join(errors)[:1200])

        if fetch_pages and self.fetch_max_pages > 0 and results:
            self._fill_page_excerpts(results[: self.fetch_max_pages])
        return results

    def _fill_page_excerpts(self, results: List[SearchResult]) -> None:
        workers = min(3, len(results))
        if workers <= 0:
            return
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="WebFetch") as executor:
            future_map = {
                executor.submit(self.fetch_page, item.url): item
                for item in results
            }
            for future in as_completed(future_map):
                item = future_map[future]
                try:
                    item.excerpt = future.result()
                except Exception as exc:
                    logger.debug("Page fetch skipped for %s: %s", item.url, exc)

    def fetch_page(self, url: str) -> str:
        """Fetch a public HTTP(S) page and return a bounded plain-text excerpt."""
        current_url = str(url or "").strip()
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36 GGBot-WebFetch/1.0"
                ),
                "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.8,*/*;q=0.1",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            }
        )

        response = None
        for redirect_index in range(4):
            self._validate_public_url(current_url)
            response = session.get(
                current_url,
                timeout=(self.timeout_seconds, self.timeout_seconds),
                allow_redirects=False,
                stream=True,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = str(response.headers.get("location") or "").strip()
                response.close()
                if not location:
                    raise LocalWebSearchError("网页重定向缺少目标地址")
                if redirect_index >= 3:
                    raise LocalWebSearchError("网页重定向次数超过上限")
                current_url = urljoin(current_url, location)
                continue
            break
        if response is None:
            raise LocalWebSearchError("网页读取没有返回响应")
        try:
            response.raise_for_status()
            content_type = str(response.headers.get("content-type") or "").lower()
            allowed_types = ("text/", "application/xhtml+xml", "application/json")
            if content_type and not any(token in content_type for token in allowed_types):
                raise LocalWebSearchError(f"不支持的网页内容类型：{content_type[:120]}")

            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.fetch_max_bytes:
                    raise LocalWebSearchError("网页正文超过读取上限")
                chunks.append(chunk)
            encoding = response.encoding or response.apparent_encoding or "utf-8"
            html = b"".join(chunks).decode(encoding, errors="replace")
        finally:
            response.close()

        soup = BeautifulSoup(html, "html.parser")
        for node in soup(["script", "style", "noscript", "svg", "canvas", "form", "nav"]):
            node.decompose()
        root = soup.find("article") or soup.find("main") or soup.body or soup
        text = "\n".join(
            line.strip()
            for line in root.get_text("\n").splitlines()
            if line.strip()
        )
        return text[: self.fetch_excerpt_chars]

    @staticmethod
    def _validate_public_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise UnsafeFetchTarget("只允许读取公开 HTTP(S) 网页")
        if parsed.username or parsed.password:
            raise UnsafeFetchTarget("网页地址不能包含认证信息")
        try:
            port = parsed.port
        except ValueError as exc:
            raise UnsafeFetchTarget("网页地址端口无效") from exc
        if port not in {None, 80, 443}:
            raise UnsafeFetchTarget("网页地址使用了不允许的端口")

        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise UnsafeFetchTarget("不允许读取本机地址")
        try:
            default_port = 443 if parsed.scheme == "https" else 80
            addresses = {item[4][0] for item in socket.getaddrinfo(host, port or default_port)}
        except socket.gaierror as exc:
            raise LocalWebSearchError(f"无法解析网页域名：{host}") from exc
        if not addresses:
            raise LocalWebSearchError(f"网页域名没有可用地址：{host}")
        for raw_address in addresses:
            address = ipaddress.ip_address(raw_address.split("%", 1)[0])
            if not address.is_global:
                raise UnsafeFetchTarget("不允许读取内网、回环或保留地址")
