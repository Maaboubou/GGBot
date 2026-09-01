"""Dynamic model discovery layered on top of the local LiteLLM catalog.

LiteLLM remains the source of capability metadata and the request adapter.  This
service only asks OpenAI-compatible providers which model IDs are currently
visible, then caches the last successful answer for fast startup and graceful
fallback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx


logger = logging.getLogger(__name__)


DEFAULT_OPENAI_COMPATIBLE_BASES = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "mistral": "https://api.mistral.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "xai": "https://api.x.ai/v1",
    "perplexity": "https://api.perplexity.ai",
    "together_ai": "https://api.together.xyz/v1",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "fireworks_ai": "https://api.fireworks.ai/inference/v1",
}

AUTH_REQUIRED_PROVIDERS = frozenset(DEFAULT_OPENAI_COMPATIBLE_BASES)
CHAT_MODES = frozenset({"", "chat", "completion", "responses"})
NON_CHAT_NAME_MARKERS = (
    "embedding",
    "embed-",
    "rerank",
    "moderation",
    "transcribe",
    "text-to-speech",
    "tts-",
)


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _nested_values(item: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Yield a small allowlist of metadata containers without retaining payloads."""
    yield item
    for key in ("metadata", "limits", "capabilities", "top_provider", "architecture", "model_info"):
        value = item.get(key)
        if isinstance(value, dict):
            yield value


def _first_int(item: Dict[str, Any], keys: Iterable[str]) -> Optional[int]:
    for container in _nested_values(item):
        for key in keys:
            parsed = _positive_int(container.get(key))
            if parsed is not None:
                return parsed
    return None


def _first_bool(item: Dict[str, Any], keys: Iterable[str]) -> Optional[bool]:
    for container in _nested_values(item):
        for key in keys:
            value = container.get(key)
            if isinstance(value, bool):
                return value
    return None


def _safe_endpoint(endpoint: str) -> str:
    """Remove credentials, query parameters, and fragments from display/log URLs."""
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        return urlunsplit((parts.scheme, f"{host}{port}", parts.path, "", ""))
    except (TypeError, ValueError):
        return ""


class ModelRegistryService:
    """Discover and merge live provider models with LiteLLM metadata."""

    def __init__(
        self,
        *,
        cache_path: Optional[Path] = None,
        cache_ttl_seconds: int = 15 * 60,
        stale_ttl_seconds: int = 30 * 24 * 60 * 60,
    ) -> None:
        self.cache_path = cache_path or Path("data/cache/model_registry.json")
        self.cache_ttl_seconds = max(1, int(cache_ttl_seconds))
        self.stale_ttl_seconds = max(self.cache_ttl_seconds, int(stale_ttl_seconds))
        self._cache_loaded = False
        self._cache: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _cache_key(provider: str, endpoint: str, api_key: str) -> str:
        credential_fingerprint = hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()[:16]
        material = f"{provider.lower()}\n{endpoint.rstrip('/').lower()}\n{credential_fingerprint}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        self._cache_loaded = True
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            entries = payload.get("entries") if isinstance(payload, dict) else None
            if isinstance(entries, dict):
                self._cache = {
                    str(key): value
                    for key, value in entries.items()
                    if isinstance(value, dict) and isinstance(value.get("models"), list)
                }
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("忽略损坏的模型注册表缓存: %s", exc)

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps({"version": 1, "entries": self._cache}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.cache_path)
        except Exception as exc:
            logger.warning("保存模型注册表缓存失败: %s", exc)

    @staticmethod
    def _resolve_endpoint(provider: str, api_base: str) -> str:
        base = str(api_base or "").strip() or DEFAULT_OPENAI_COMPATIBLE_BASES.get(provider, "")
        if not base:
            return ""
        try:
            parts = urlsplit(base)
        except ValueError:
            return ""
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return ""
        path = parts.path.rstrip("/")
        if path.endswith("/models"):
            models_path = path
        elif path:
            models_path = f"{path}/models"
        else:
            models_path = "/v1/models"
        return urlunsplit((parts.scheme, parts.netloc, models_path, parts.query, ""))

    @staticmethod
    def _parse_models(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, dict):
            records = payload.get("data")
            if not isinstance(records, list):
                records = payload.get("models")
        elif isinstance(payload, list):
            records = payload
        else:
            records = None
        if not isinstance(records, list):
            raise ValueError("响应中没有 data/models 数组")

        parsed: List[Dict[str, Any]] = []
        seen = set()
        for record in records:
            if isinstance(record, str):
                record = {"id": record}
            if not isinstance(record, dict):
                continue
            raw_id = str(record.get("id") or record.get("name") or "").strip()
            if raw_id.startswith("models/"):
                raw_id = raw_id[7:]
            if not raw_id or len(raw_id) > 300 or raw_id in seen:
                continue
            # Several OpenAI-compatible APIs use ``type: model`` as an object
            # discriminator, not a capability mode. Only an explicit ``mode``
            # is safe to use for chat/embedding filtering.
            mode = str(record.get("mode") or "").strip().lower()
            if mode not in CHAT_MODES or any(marker in raw_id.lower() for marker in NON_CHAT_NAME_MARKERS):
                continue
            parsed.append({
                "server_id": raw_id,
                "mode": mode or "chat",
                "max_input_tokens": _first_int(
                    record,
                    ("max_input_tokens", "context_length", "context_window", "max_context_length", "input_token_limit"),
                ),
                "max_output_tokens": _first_int(
                    record,
                    ("max_output_tokens", "output_token_limit", "max_completion_tokens"),
                ),
                "supports_vision": _first_bool(record, ("supports_vision", "vision", "image_input")),
                "supports_reasoning": _first_bool(record, ("supports_reasoning", "reasoning")),
                "supports_web_search": _first_bool(record, ("supports_web_search", "web_search")),
                "supports_function_calling": _first_bool(
                    record,
                    ("supports_function_calling", "function_calling", "tools"),
                ),
            })
            seen.add(raw_id)
        return parsed

    async def _fetch_models(self, endpoint: str, api_key: str) -> List[Dict[str, Any]]:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = httpx.Timeout(12.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(endpoint, headers=headers)
            response.raise_for_status()
            if len(response.content) > 8 * 1024 * 1024:
                raise ValueError("模型目录响应超过 8 MiB")
            return self._parse_models(response.json())

    async def discover(
        self,
        *,
        provider: str,
        api_base: str = "",
        api_key: str = "",
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        provider_key = str(provider or "").strip().lower()
        endpoint = self._resolve_endpoint(provider_key, api_base)
        safe_endpoint = _safe_endpoint(endpoint)
        if provider_key in {"anthropic", "gemini", "azure", "bedrock", "vertex_ai", "local_codex"}:
            return {
                "status": "unsupported",
                "attempted": False,
                "models": [],
                "endpoint": safe_endpoint,
                "message": "该供应商暂不提供 OpenAI 兼容的模型发现接口",
            }
        if not endpoint:
            return {
                "status": "not_configured",
                "attempted": False,
                "models": [],
                "endpoint": "",
                "message": "填写 API 地址后可同步服务端模型",
            }
        if provider_key in AUTH_REQUIRED_PROVIDERS and not api_key:
            return {
                "status": "not_configured",
                "attempted": False,
                "models": [],
                "endpoint": safe_endpoint,
                "message": "配置 API Key 后可同步服务端模型",
            }

        self._load_cache()
        key = self._cache_key(provider_key, endpoint, api_key)
        cached = self._cache.get(key)
        now = time.time()
        cached_at = float(cached.get("fetched_timestamp") or 0) if cached else 0
        cache_age = now - cached_at if cached_at else float("inf")
        if cached and cache_age <= self.cache_ttl_seconds and not force_refresh:
            return {
                "status": "cache",
                "attempted": False,
                "models": cached["models"],
                "endpoint": safe_endpoint,
                "fetched_at": cached.get("fetched_at"),
                "stale": False,
                "message": "已使用最近同步的服务端模型",
            }

        try:
            models = await self._fetch_models(endpoint, api_key)
            fetched_at = datetime.now(timezone.utc).isoformat()
            self._cache[key] = {
                "provider": provider_key,
                "endpoint": safe_endpoint,
                "fetched_at": fetched_at,
                "fetched_timestamp": now,
                "models": models,
            }
            self._save_cache()
            return {
                "status": "live",
                "attempted": True,
                "models": models,
                "endpoint": safe_endpoint,
                "fetched_at": fetched_at,
                "stale": False,
                "message": f"已从服务端同步 {len(models)} 个模型",
            }
        except Exception as exc:
            # Exception strings from HTTP clients may contain the original URL
            # including a sensitive query string. Log only the sanitized target
            # and exception type.
            logger.info("模型目录实时同步失败 (%s): %s", safe_endpoint, type(exc).__name__)
            if cached and cache_age <= self.stale_ttl_seconds:
                return {
                    "status": "stale",
                    "attempted": True,
                    "models": cached["models"],
                    "endpoint": safe_endpoint,
                    "fetched_at": cached.get("fetched_at"),
                    "stale": True,
                    "message": "实时同步失败，已使用最近一次成功缓存",
                }
            return {
                "status": "error",
                "attempted": True,
                "models": [],
                "endpoint": safe_endpoint,
                "stale": False,
                "message": "服务端模型同步失败，已保留 LiteLLM 目录",
            }

    @staticmethod
    def _canonical_name(provider: str, server_id: str) -> str:
        if provider in {"openai", "compatible", "custom_openai"} or server_id.startswith(f"{provider}/"):
            return server_id
        return f"{provider}/{server_id}"

    def merge_catalog(
        self,
        *,
        provider: str,
        static_models: List[Dict[str, Any]],
        discovery: Dict[str, Any],
        query: str = "",
        limit: int = 300,
    ) -> List[Dict[str, Any]]:
        """Overlay live availability/metadata without hiding LiteLLM-only entries."""
        provider_key = str(provider or "").strip().lower()
        live_models = discovery.get("models") if isinstance(discovery, dict) else []
        live_models = live_models if isinstance(live_models, list) else []
        live_known = discovery.get("status") in {"live", "cache", "stale"}

        static_by_id: Dict[str, Dict[str, Any]] = {}
        for item in static_models:
            model_id = str(item.get("id") or "")
            if not model_id:
                continue
            static_by_id[model_id] = item
            prefix = f"{provider_key}/"
            if model_id.startswith(prefix):
                static_by_id.setdefault(model_id[len(prefix):], item)

        merged: List[Dict[str, Any]] = []
        seen = set()
        for live in live_models:
            server_id = str(live.get("server_id") or "").strip()
            if not server_id:
                continue
            canonical = self._canonical_name(provider_key, server_id)
            static = static_by_id.get(canonical) or static_by_id.get(server_id) or {}
            item = dict(static)
            item.update({key: value for key, value in live.items() if value is not None and key != "server_id"})
            item.update({
                "id": canonical,
                "server_id": server_id,
                "provider": provider_key,
                "availability": "available",
                "metadata_source": "provider+litellm" if static else "provider",
            })
            item.setdefault("recommended", True)
            item.setdefault("deprecated", False)
            merged.append(item)
            seen.add(canonical)

        for static in static_models:
            model_id = str(static.get("id") or "")
            if not model_id or model_id in seen:
                continue
            item = dict(static)
            item.update({
                "server_id": model_id[len(provider_key) + 1:] if model_id.startswith(f"{provider_key}/") else model_id,
                "availability": "catalog_only" if live_known else "unknown",
                "metadata_source": "litellm",
            })
            merged.append(item)

        needle = str(query or "").strip().lower()
        if needle:
            merged = [item for item in merged if needle in str(item.get("id") or "").lower()]
        merged.sort(key=lambda item: (
            item.get("availability") != "available",
            bool(item.get("deprecated")),
            not bool(item.get("recommended")),
            len(str(item.get("id") or "")),
            str(item.get("id") or "").lower(),
        ))
        return merged[: max(1, min(int(limit or 300), 300))]


_model_registry_service: Optional[ModelRegistryService] = None


def get_model_registry_service() -> ModelRegistryService:
    global _model_registry_service
    if _model_registry_service is None:
        _model_registry_service = ModelRegistryService()
    return _model_registry_service
