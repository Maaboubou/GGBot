"""
网络工具 - 仅供 boc_rate 插件使用
"""

import time
import logging
from typing import Optional
import requests


def request_with_retry(
    method: str,
    url: str,
    *,
    logger: Optional[logging.Logger] = None,
    max_retries: int = 3,
    backoff_factor: float = 1.5,
    timeout: int = 10,
    **kwargs,
):
    attempt = 0
    last_exc = None
    method = method.lower().strip()
    while attempt < max_retries:
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
            if 500 <= resp.status_code < 600:
                raise requests.HTTPError(f"Server error: {resp.status_code}")
            return resp
        except Exception as e:
            last_exc = e
            attempt += 1
            if logger:
                logger.warning(f"request_with_retry failed ({attempt}/{max_retries}) for {url}: {e}")
            if attempt >= max_retries:
                break
            sleep_sec = backoff_factor * attempt
            time.sleep(sleep_sec)
    if logger:
        logger.error(f"request_with_retry exhausted retries for {url}: {last_exc}")
    raise last_exc


