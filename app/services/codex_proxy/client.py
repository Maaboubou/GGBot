from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.services.codex_job_manager import codex_job_manager

try:
    import tiktoken
except ImportError:  # pragma: no cover - optional dependency in non-app contexts
    tiktoken = None


class CodexProxyError(RuntimeError):
    """Raised when the Codex CLI backend cannot produce a usable response."""


logger = logging.getLogger(__name__)

_RUNNING_REQUESTS_LOCK = threading.Lock()
_RUNNING_REQUESTS: Dict[str, Dict[str, Any]] = {}


def get_running_codex_requests() -> List[Dict[str, Any]]:
    """Return a snapshot of currently running Codex CLI requests."""
    return codex_job_manager.list_active()


def get_recent_codex_jobs(limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent completed/cancelled/failed Codex CLI jobs."""
    return codex_job_manager.list_recent(limit=limit)


async def cancel_codex_request(request_id: str) -> Dict[str, Any]:
    """Cancel a currently running Codex CLI request."""
    return await codex_job_manager.cancel(request_id)


def _register_running_request(request_id: str, info: Dict[str, Any]) -> None:
    codex_job_manager.register(request_id, info)


def _update_running_request(request_id: str, **updates: Any) -> None:
    codex_job_manager.update(request_id, **updates)


def _unregister_running_request(request_id: str) -> None:
    codex_job_manager.finish(request_id)


def _tail_text(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in {"text", "input_text", "output_text"}:
                    parts.append(str(item.get("text") or ""))
                elif "text" in item:
                    parts.append(str(item.get("text") or ""))
                elif "content" in item:
                    parts.append(str(item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content)


def _extract_image_urls(content: Any) -> List[str]:
    if not isinstance(content, list):
        return []

    image_urls: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        if item_type == "image_url":
            image_url = item.get("image_url")
            if isinstance(image_url, dict):
                url = image_url.get("url")
            else:
                url = image_url
        elif item_type == "input_image":
            url = item.get("image_url") or item.get("url")
        else:
            url = None

        if isinstance(url, str) and url.strip():
            image_urls.append(url.strip())
    return image_urls


def extract_image_urls(messages: Iterable[Dict[str, Any]], allow_image_input: bool = False) -> List[str]:
    if not allow_image_input:
        return []
    latest_content: Any = None
    for message in messages:
        if str(message.get("role") or "user") == "user":
            latest_content = message.get("content")
    return _extract_image_urls(latest_content)


def _suffix_for_mime(mime_type: str) -> str:
    normalized = (mime_type or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(normalized, ".jpg")


_IMAGE_ATTACHMENT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
_DOCUMENT_ATTACHMENT_SUFFIXES = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".ppt",
    ".pptx",
    ".rtf",
    ".odt",
    ".ods",
    ".odp",
    ".epub",
}
_TEXT_DATA_ATTACHMENT_SUFFIXES = {
    ".txt",
    ".md",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
    ".log",
    ".sql",
}
_ARCHIVE_ATTACHMENT_SUFFIXES = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz"}
_MEDIA_ATTACHMENT_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".mp4", ".mov", ".webm", ".avi", ".mkv"}
_SAFE_ATTACHMENT_SUFFIXES = (
    _IMAGE_ATTACHMENT_SUFFIXES
    | _DOCUMENT_ATTACHMENT_SUFFIXES
    | _TEXT_DATA_ATTACHMENT_SUFFIXES
    | _ARCHIVE_ATTACHMENT_SUFFIXES
    | _MEDIA_ATTACHMENT_SUFFIXES
)
_ATTACHMENT_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".epub": "application/epub+zip",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".log": "text/plain",
    ".sql": "application/sql",
    ".zip": "application/zip",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
}


def _is_within_path(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _path_for_wsl(path: Path) -> str:
    resolved = str(path.resolve())
    drive, tail = os.path.splitdrive(resolved)
    if not drive:
        return resolved.replace("\\", "/")
    drive_letter = drive.rstrip(":").lower()
    normalized_tail = tail.replace("\\", "/").lstrip("/")
    return f"/mnt/{drive_letter}/{normalized_tail}"


def _as_runtime_path(path: Path, use_wsl: bool) -> str:
    if use_wsl and os.name == "nt":
        return _path_for_wsl(path)
    return str(path.resolve())


def _write_image_url_to_file(image_url: str) -> Path:
    if image_url.startswith("data:"):
        header, separator, payload = image_url.partition(",")
        if not separator or ";base64" not in header:
            raise CodexProxyError("Only base64 data image URLs are supported")

        mime_type = header[5:].split(";", 1)[0]
        try:
            image_bytes = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CodexProxyError("Invalid base64 image payload") from exc
        if not image_bytes:
            raise CodexProxyError("Empty image payload")

        path = Path(tempfile.gettempdir()) / f"codex_proxy_input_{uuid.uuid4().hex}{_suffix_for_mime(mime_type)}"
        path.write_bytes(image_bytes)
        return path

    local_path = Path(image_url)
    if local_path.exists() and local_path.is_file():
        return local_path

    raise CodexProxyError("Image input must be a base64 data URL or an existing local file path")


def _attachment_type_for_suffix(suffix: str) -> str:
    if suffix in _IMAGE_ATTACHMENT_SUFFIXES:
        return "image"
    return "file"


def _default_text_for_attachments(attachments: List[Dict[str, Any]]) -> str:
    if attachments and all(item.get("type") == "image" for item in attachments):
        return "已生成图片"
    return "已生成文件"


def _collect_artifact_attachments(output_dir: Path) -> List[Dict[str, Any]]:
    attachments: List[Dict[str, Any]] = []
    if not output_dir.exists():
        return attachments

    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in _SAFE_ATTACHMENT_SUFFIXES:
            continue
        if not _is_within_path(path, output_dir):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size <= 0:
            continue

        mime_type = _ATTACHMENT_MIME_TYPES.get(suffix, "application/octet-stream")
        attachments.append(
            {
                "type": _attachment_type_for_suffix(suffix),
                "mime_type": mime_type,
                "path": str(path.resolve()),
                "name": path.name,
                "size": stat.st_size,
            }
        )
    return attachments


def render_chat_prompt(
    messages: Iterable[Dict[str, Any]],
    artifact_output_dir: Optional[str] = None,
    native_web_search_enabled: bool = False,
    input_image_count: int = 0,
) -> str:
    """Render OpenAI chat messages into a single Codex exec prompt."""
    rendered: List[str] = [
        "You are being used as a chat-completions model provider.",
        "Answer the latest user request directly.",
        "Do not inspect local project files or modify repository files unless explicitly asked.",
        "Do not mention Codex unless asked.",
        "Preserve the behavior requested by system and developer messages below.",
    ]
    rendered.extend(["", "Conversation:"])
    for message in messages:
        role = str(message.get("role") or "user")
        name = message.get("name")
        label = f"{role} ({name})" if name else role
        text = _content_to_text(message.get("content"))
        if text:
            rendered.append(f"[{label}]\n{text}")
    if artifact_output_dir is not None:
        search_instructions = [
            "- Treat any conversation sections labeled like 网络搜索结果, web_search, or search_results as framework-provided context, not as the only allowed source of truth.",
        ]
        if native_web_search_enabled:
            search_instructions.extend(
                [
                    "- Native web search is enabled for this run.",
                    "- Use native web search proactively when the latest user request depends on current, recent, time-sensitive, niche, unfamiliar, or externally verifiable facts.",
                    "- Search is especially expected for latest/news/current events, prices, stocks, schedules, sports, laws/policies, product availability/specs, software/API behavior, obscure named entities, or when the chat asks what something is, when something happens, or whether something is true.",
                    "- If framework-provided search results are present and clearly fresh/sufficient, use them; if they are absent, empty, stale, contradictory, truncated, or too narrow, supplement with native web search before answering.",
                    "- Do not search for purely casual banter, roleplay-only replies, personal preference, or questions answerable entirely from the provided chat context.",
                ]
            )
        else:
            search_instructions.extend(
                [
                    "- Native web search is not available for this run.",
                    "- Use framework-provided search results when present. If they are absent or insufficient for time-sensitive facts, do not pretend you searched; answer with the appropriate uncertainty.",
                ]
            )
        rendered.extend(
            [
                "",
                "Final provider adapter instructions:",
                "- These final adapter instructions override any conversation instruction that asks for text-only or JSON-only replies.",
                "- Keep the role persona and conversation response format for the text portion, but handle files as provider-side attachments.",
                *search_instructions,
                f"- If your answer naturally needs generated or edited files, save final user-facing files under: {artifact_output_dir}",
                "- When the latest user asks to draw, generate, create, edit, modify, retouch, or transform an image, produce an actual image file there instead of only describing the image.",
                "- When the latest user asks to create or edit a document, spreadsheet, presentation, text/data file, archive, audio, or video, produce the actual file there instead of only describing it.",
                "- If the conversation response protocol requires JSON, create the required file first, then return the JSON text reply.",
                "- Do not say you will use imagegen, image generation, a skill, a tool, or another system later; complete the requested file output in this call.",
                "- Do not return a success message such as done/finished/created unless the final requested file exists in the output directory.",
                "- For image requests, your answer is incomplete unless at least one final image file exists in the output directory.",
                "- For image generation or image editing, only use the available real image generation/editing capability, including the imagegen skill or image_gen tool.",
                "- Apply the imagegen skill workflow for raster image generation/editing: use the built-in image_gen tool by default; treat user-supplied images as edit/reference inputs; preserve requested subject identity/style constraints; inspect/select the generated result; then copy or move the final raster output into the output directory.",
                "- If such a tool saves the image outside the output directory, copy or move the final user-facing raster image into the output directory before your final assistant message.",
                "- Final image attachments must be raster image files: .png, .jpg, .jpeg, .webp, .gif, .bmp, .tif, or .tiff.",
                "- Final non-image attachments may be common office, text/data, archive, audio, or video files such as .pdf, .docx, .xlsx, .pptx, .txt, .md, .csv, .json, .zip, .mp3, or .mp4.",
                "- Do not create images programmatically with Python, SVG, HTML, canvas, drawing libraries, or shell scripts.",
                "- If the real image generation/editing capability is unavailable or fails, do not fake success and do not create a placeholder image.",
                "- Use that directory only for final user-facing file outputs.",
                "- Do not write generated attachments anywhere else.",
                "- If no image/file output is needed, do not create files.",
                "- Include any normal text reply in your final assistant message.",
                "- Do not mention local file paths in the final assistant message; attachments are delivered separately.",
            ]
        )
    if input_image_count > 0:
        rendered.extend(
            [
                "",
                "Attached image instructions:",
                f"- The latest request includes {input_image_count} attached image input(s) supplied out-of-band by the chat-completions adapter.",
                "- These image(s) are part of the latest user message, not old conversation context.",
                "- When the latest user says 'this', '这个', '真的吗', '我问你这个', or similar, resolve it to the latest attached image(s).",
                "- Inspect the attached image(s) before answering; do not answer from earlier images or earlier topics unless the latest user explicitly asks for that history.",
            ]
        )
    rendered.append("")
    rendered.append("Return only the assistant reply text.")
    return "\n\n".join(rendered)


def _extract_output_text(event: Dict[str, Any]) -> str:
    """Best-effort extraction from Codex JSON events across CLI versions."""
    for key in ("message", "text", "content", "delta"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value

    item = event.get("item")
    if isinstance(item, dict):
        text = _extract_output_text(item)
        if text:
            return text

    msg = event.get("msg")
    if isinstance(msg, dict):
        text = _extract_output_text(msg)
        if text:
            return text

    return ""


def _is_final_event(event: Dict[str, Any]) -> bool:
    event_type = str(event.get("type") or event.get("event") or event.get("msg", {}).get("type") or "")
    return event_type in {
        "agent_message",
        "agent_message_delta",
        "assistant_message",
        "message",
        "turn_complete",
        "response.completed",
    }


def parse_codex_json_stdout(stdout: str) -> str:
    """Extract the final assistant text from `codex exec --json` output."""
    final_text = ""
    accumulated: List[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        text = _extract_output_text(event)
        if not text:
            continue
        if _is_final_event(event):
            final_text = text
        else:
            accumulated.append(text)

    return (final_text or "".join(accumulated)).strip()


def _extract_reasoning_text(event: Dict[str, Any]) -> str:
    """Extract public reasoning summaries/details from Codex JSON events, if present."""
    for key in ("reasoning_content", "reasoning", "thinking", "summary"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(_extract_reasoning_text(item) or _extract_output_text(item))
            text = "\n".join(part.strip() for part in parts if part and part.strip()).strip()
            if text:
                return text
        if isinstance(value, dict):
            text = _extract_reasoning_text(value) or _extract_output_text(value)
            if text:
                return text.strip()

    event_type = str(event.get("type") or event.get("event") or event.get("msg", {}).get("type") or "").lower()
    if any(marker in event_type for marker in ("reasoning", "thinking")):
        text = _extract_output_text(event)
        if text:
            return text.strip()

    for key in ("item", "msg", "delta"):
        value = event.get(key)
        if isinstance(value, dict):
            text = _extract_reasoning_text(value)
            if text:
                return text

    return ""


def parse_codex_json_reasoning(stdout: str) -> str:
    """Extract public reasoning text emitted by `codex exec --json`, when available."""
    parts: List[str] = []
    seen = set()
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        text = _extract_reasoning_text(event)
        if text and text not in seen:
            seen.add(text)
            parts.append(text)

    return "\n\n".join(parts).strip()


def parse_codex_json_usage(stdout: str) -> Dict[str, Any]:
    """Extract and normalize usage from the final Codex turn event.

    ``codex exec --json`` reports authoritative per-turn usage in a
    ``turn.completed`` event.  Keep the OpenAI-compatible fields used by the
    rest of the application while preserving cache/reasoning breakdowns.
    When multiple completed turns are present, the last one is the result of
    the current non-interactive invocation.
    """
    normalized: Dict[str, Any] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") != "turn.completed":
            continue
        raw_usage = event.get("usage")
        if not isinstance(raw_usage, dict):
            continue

        def _int_value(key: str) -> int:
            try:
                return max(0, int(raw_usage.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        input_tokens = _int_value("input_tokens")
        cached_input_tokens = _int_value("cached_input_tokens")
        output_tokens = _int_value("output_tokens")
        reasoning_output_tokens = _int_value("reasoning_output_tokens")
        if not any((input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens)):
            continue

        normalized = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            # Cached input and reasoning output are breakdowns of the totals,
            # not additional tokens to add again.
            "total_tokens": input_tokens + output_tokens,
            "cache_miss_input_tokens": max(
                input_tokens - min(cached_input_tokens, input_tokens),
                0,
            ),
            "prompt_tokens_details": {
                "cached_tokens": min(cached_input_tokens, input_tokens),
            },
            "completion_tokens_details": {
                "reasoning_tokens": min(reasoning_output_tokens, output_tokens),
            },
            "estimated": False,
            "source": "codex_cli_turn.completed",
        }

    return normalized


_O200K_ENCODING = None
_O200K_ENCODING_LOCK = threading.Lock()
_O200K_CACHE_KEY = "fb374d419588a4632f3f557e76b4b70aebbca790"


def _bundled_o200k_cache_dir() -> Optional[Path]:
    """Return LiteLLM's bundled tokenizer cache when it is available."""
    if tiktoken is None or not getattr(tiktoken, "__file__", None):
        return None
    cache_dir = (
        Path(tiktoken.__file__).resolve().parent.parent
        / "litellm"
        / "litellm_core_utils"
        / "tokenizers"
    )
    if (cache_dir / _O200K_CACHE_KEY).is_file():
        return cache_dir
    return None


def _get_o200k_encoding():
    global _O200K_ENCODING
    if _O200K_ENCODING is not None:
        return _O200K_ENCODING
    if tiktoken is None:
        return None

    with _O200K_ENCODING_LOCK:
        if _O200K_ENCODING is not None:
            return _O200K_ENCODING

        # LiteLLM ships the verified o200k_base cache asset. Point tiktoken at
        # it during initialization so prompt counting works even when the bot
        # host cannot reach OpenAI's public blob storage.
        bundled_cache = _bundled_o200k_cache_dir()
        previous_cache = os.environ.get("TIKTOKEN_CACHE_DIR")
        if bundled_cache is not None:
            os.environ["TIKTOKEN_CACHE_DIR"] = str(bundled_cache)
        try:
            _O200K_ENCODING = tiktoken.get_encoding("o200k_base")
        finally:
            if bundled_cache is not None:
                if previous_cache is None:
                    os.environ.pop("TIKTOKEN_CACHE_DIR", None)
                else:
                    os.environ["TIKTOKEN_CACHE_DIR"] = previous_cache
        return _O200K_ENCODING


def _count_o200k_tokens(text: str) -> int:
    if not text:
        return 0
    encoding = _get_o200k_encoding()
    if encoding is None:
        return 0
    return len(encoding.encode(text))


def estimate_codex_usage(prompt: str, output_text: str) -> Dict[str, Any]:
    """Best-effort local usage estimate for Codex CLI OAuth calls."""
    prompt_tokens = _count_o200k_tokens(prompt)
    completion_tokens = _count_o200k_tokens(output_text)
    if not prompt_tokens and not completion_tokens:
        return {}
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "estimated": True,
        "encoding": "o200k_base",
    }


def count_codex_prompt_tokens(
    messages: Iterable[Dict[str, Any]],
    *,
    artifact_output_dir: Optional[str] = "<codex-output-dir>",
    native_web_search_enabled: bool = False,
    input_image_count: int = 0,
) -> int:
    """Count the exact rendered Codex text prompt with ``o200k_base``.

    Image payload tokens are reported by Codex after the call and are not part
    of this text-only preflight count.  ``input_image_count`` still includes
    the adapter instructions that are rendered into the prompt.
    """
    prompt = render_chat_prompt(
        messages,
        artifact_output_dir=artifact_output_dir,
        native_web_search_enabled=native_web_search_enabled,
        input_image_count=max(0, int(input_image_count or 0)),
    )
    return _count_o200k_tokens(prompt)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


_CODEX_WEB_SEARCH_MODES = {"disabled", "cached", "indexed", "live"}


def normalize_codex_web_search_mode(value: Any, default: str = "disabled") -> str:
    """Normalize legacy booleans and current Codex web-search modes."""
    default_mode = str(default or "disabled").strip().lower()
    if default_mode not in _CODEX_WEB_SEARCH_MODES:
        default_mode = "disabled"
    if value is None:
        return default_mode
    if isinstance(value, bool):
        return "live" if value else "disabled"
    if isinstance(value, (int, float)):
        return "live" if value else "disabled"
    normalized = str(value).strip().lower()
    if normalized in _CODEX_WEB_SEARCH_MODES:
        return normalized
    if normalized in {"1", "true", "yes", "on"}:
        return "live"
    if normalized in {"0", "false", "no", "off"}:
        return "disabled"
    return default_mode


def _reasoning_summary_config_args(reasoning_summary: str) -> List[str]:
    if reasoning_summary == "inherit":
        return []
    return [
        "-c",
        f'model_reasoning_summary="{reasoning_summary}"',
        "-c",
        "model_supports_reasoning_summaries="
        + ("true" if reasoning_summary != "none" else "false"),
    ]


class CodexCliClient:
    """Thin backend that calls the authenticated Codex CLI."""

    def __init__(
        self,
        *,
        codex_bin: Optional[str] = None,
        workdir: Optional[str] = None,
        timeout_seconds: int = 600,
    ) -> None:
        self.codex_bin = self._resolve_codex_bin(codex_bin or os.getenv("CODEX_PROXY_BIN"))
        self.workdir = str(Path(workdir or os.getenv("CODEX_PROXY_WORKDIR") or Path.cwd()).resolve())
        self.timeout_seconds = timeout_seconds
        self.use_wsl = os.name == "nt" and _as_bool(os.getenv("CODEX_PROXY_USE_WSL"), True)

    @staticmethod
    def _resolve_codex_bin(configured: Optional[str]) -> str:
        if configured:
            return configured
        resolved = shutil.which("codex")
        if resolved:
            return resolved
        return "codex"

    async def _terminate_process_tree(self, proc: asyncio.subprocess.Process, runtime_output_path: str, request_id: str) -> None:
        """Best-effort termination for Codex CLI and children, including WSL children."""
        _update_running_request(request_id, status="terminating", terminating_at=time.time())
        logger.warning(
            "Codex request %s timed out; terminating process tree pid=%s marker=%s",
            request_id,
            getattr(proc, "pid", None),
            runtime_output_path,
        )

        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except Exception:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

            if self.use_wsl and runtime_output_path:
                # wsl.exe can exit while the Linux-side codex child remains. Match the unique
                # per-request output path embedded in the codex command line and kill both
                # wrapper and native binary processes.
                marker = shlex.quote(runtime_output_path)
                kill_cmd = (
                    f"pkill -TERM -f {marker} 2>/dev/null || true; "
                    f"sleep 1; "
                    f"pkill -KILL -f {marker} 2>/dev/null || true"
                )
                try:
                    subprocess.run(
                        ["wsl.exe", "bash", "-lc", kill_cmd],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                    )
                except Exception:
                    logger.exception("Codex request %s: failed to clean WSL codex children", request_id)
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
                return
            except Exception:
                pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass

    async def chat(self, request: Dict[str, Any]) -> Dict[str, Any]:
        model = str(request.get("model") or os.getenv("CODEX_PROXY_MODEL") or "gpt-5.6-sol")
        extra_body = request.get("extra_body") if isinstance(request.get("extra_body"), dict) else {}
        reasoning_effort = (
            request.get("reasoning_effort")
            or extra_body.get("reasoning_effort")
            or os.getenv("CODEX_PROXY_REASONING_EFFORT")
            or "high"
        )
        web_search_mode = normalize_codex_web_search_mode(
            request.get(
                "codex_web_search",
                request.get(
                    "web_search",
                    extra_body.get("codex_web_search", extra_body.get("web_search")),
                ),
            ),
            default=normalize_codex_web_search_mode(
                os.getenv("CODEX_PROXY_WEB_SEARCH"),
                "disabled",
            ),
        )
        web_search_enabled = web_search_mode != "disabled"
        reasoning_summary = str(
            request.get("codex_reasoning_summary")
            or extra_body.get("codex_reasoning_summary")
            or "inherit"
        ).strip().lower()
        if reasoning_summary not in {"inherit", "none", "auto", "concise", "detailed"}:
            reasoning_summary = "inherit"
        allow_image_input = _as_bool(
            request.get("wxautox_allow_image_input", extra_body.get("wxautox_allow_image_input")),
            default=False,
        )
        messages = request.get("messages") or []
        if not isinstance(messages, list):
            raise CodexProxyError("messages must be a list")
        sandbox_mode = str(
            request.get("codex_sandbox")
            or extra_body.get("codex_sandbox")
            or "workspace-write"
        ).strip().lower()
        if sandbox_mode not in {
            "read-only",
            "workspace-write",
            "danger-full-access",
        }:
            sandbox_mode = "workspace-write"
        isolated_workdir = _as_bool(
            request.get(
                "codex_isolated_workdir",
                extra_body.get("codex_isolated_workdir"),
            ),
            default=False,
        )
        output_schema = request.get("output_schema")
        if not isinstance(output_schema, dict):
            output_schema = extra_body.get("output_schema")
        if not isinstance(output_schema, dict):
            output_schema = None

        image_urls = extract_image_urls(messages, allow_image_input=allow_image_input)

        request_id = uuid.uuid4().hex
        artifact_root = Path(os.getenv("CODEX_PROXY_ARTIFACT_ROOT") or "tmp/images/codex")
        if not artifact_root.is_absolute():
            artifact_root = Path(self.workdir) / artifact_root
        request_dir = artifact_root / request_id
        output_dir = request_dir / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        runtime_output_dir = _as_runtime_path(output_dir, self.use_wsl)
        runtime_output_path = _as_runtime_path(output_path := request_dir / "last_message.txt", self.use_wsl)
        isolated_workdir_path: Optional[Path] = None
        if isolated_workdir:
            isolated_workdir_path = Path(
                tempfile.mkdtemp(prefix="wxautox_codex_memory_")
            ).resolve()
            workdir_path = isolated_workdir_path
        else:
            workdir_path = Path(self.workdir)
        runtime_workdir = _as_runtime_path(workdir_path, self.use_wsl)
        output_schema_path: Optional[Path] = None
        runtime_output_schema_path = ""
        if output_schema:
            output_schema_path = request_dir / "output_schema.json"
            output_schema_path.write_text(
                json.dumps(output_schema, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_output_schema_path = _as_runtime_path(
                output_schema_path,
                self.use_wsl,
            )

        prompt = render_chat_prompt(
            messages,
            runtime_output_dir,
            native_web_search_enabled=web_search_enabled,
            input_image_count=len(image_urls),
        )
        image_paths: List[Path] = []
        temporary_image_paths: List[Path] = []
        runtime_image_paths: List[str] = []
        if image_urls:
            logger.info(
                "Codex proxy image input enabled: allow=%s count=%s",
                allow_image_input,
                len(image_urls),
            )
        elif allow_image_input:
            logger.debug("Codex proxy image input enabled but no image_url parts found in latest user message")

        for image_url in image_urls:
            image_path = _write_image_url_to_file(image_url)
            image_paths.append(image_path)
            if image_url.startswith("data:"):
                temporary_image_paths.append(image_path)
            runtime_image_paths.append(_as_runtime_path(image_path, self.use_wsl))

        timeout = int(request.get("timeout") or self.timeout_seconds)
        args = [os.getenv("CODEX_PROXY_WSL_BIN", "codex") if self.use_wsl else self.codex_bin]
        if web_search_mode == "live":
            args.append("--search")
        args.extend([
            "exec",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
            "-c",
            f'web_search="{web_search_mode}"',
        ])
        args.extend(_reasoning_summary_config_args(reasoning_summary))
        for image_path in runtime_image_paths:
            args.extend(["-i", image_path])
        if runtime_image_paths:
            logger.info(
                "Codex proxy exec image args: model=%s image_count=%s",
                model,
                len(runtime_image_paths),
            )
        if runtime_output_schema_path:
            args.extend(["--output-schema", runtime_output_schema_path])
        args.extend([
            "-s",
            sandbox_mode,
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "-C",
            runtime_workdir,
            "--json",
            "-o",
            runtime_output_path,
            "-",
        ])
        proc_args = args
        if self.use_wsl:
            proc_args = ["wsl.exe", "bash", "-lic", " ".join(shlex.quote(str(arg)) for arg in args)]

        started_at = time.time()
        _register_running_request(
            request_id,
            {
                "request_id": request_id,
                "status": "starting",
                "model": model,
                "reasoning_effort": reasoning_effort,
                "web_search": web_search_enabled,
                "web_search_mode": web_search_mode,
                "reasoning_summary": reasoning_summary,
                "timeout": timeout,
                "workdir": str(workdir_path),
                "sandbox": sandbox_mode,
                "isolated_workdir": isolated_workdir,
                "structured_output": bool(output_schema),
                "request_dir": str(request_dir),
                "output_dir": str(output_dir),
                "prompt_chars": len(prompt),
                "message_count": len(messages),
                "image_count": len(image_urls),
                "started_at": started_at,
            },
        )
        logger.info(
            "Codex request %s starting: model=%s timeout=%ss search=%s messages=%s prompt_chars=%s images=%s output_dir=%s",
            request_id,
            model,
            timeout,
            web_search_mode,
            len(messages),
            len(prompt),
            len(image_urls),
            output_dir,
        )

        try:
            try:
                subprocess_kwargs: Dict[str, Any] = {}
                if os.name == "nt":
                    subprocess_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                else:
                    subprocess_kwargs["start_new_session"] = True
                proc = await asyncio.create_subprocess_exec(
                    *proc_args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **subprocess_kwargs,
                )
                codex_job_manager.attach_process(
                    request_id,
                    proc,
                    status="running",
                    runtime_output_path=runtime_output_path,
                    runtime_output_dir=runtime_output_dir,
                    use_wsl=self.use_wsl,
                    proc_args_preview=" ".join(shlex.quote(str(arg)) for arg in proc_args[:8]),
                )
            except FileNotFoundError as exc:
                raise CodexProxyError(
                    "Codex CLI was not found. Install Codex or set CODEX_PROXY_BIN "
                    "to the full path of codex/codex.cmd."
                ) from exc
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")),
                    timeout=timeout,
                )
            except asyncio.TimeoutError as exc:
                await self._terminate_process_tree(proc, runtime_output_path, request_id)
                raise CodexProxyError(f"Codex CLI timed out after {timeout}s") from exc

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            _update_running_request(
                request_id,
                status="completed_process",
                returncode=proc.returncode,
                stdout_tail=_tail_text(stdout, 1200),
                stderr_tail=_tail_text(stderr, 1200),
            )
            text = ""
            try:
                if output_path.exists():
                    text = output_path.read_text(encoding="utf-8").strip()
            finally:
                try:
                    output_path.unlink(missing_ok=True)
                except Exception:
                    pass

            if not text:
                text = parse_codex_json_stdout(stdout)
            reasoning_text = parse_codex_json_reasoning(stdout)
            reported_usage = parse_codex_json_usage(stdout)

            if proc.returncode != 0:
                detail = (stderr or stdout).strip()
                raise CodexProxyError(f"Codex CLI exited with {proc.returncode}: {detail[:1000]}")
            attachments = _collect_artifact_attachments(output_dir)
            if attachments:
                logger.info("Codex proxy collected %s attachment(s) from %s", len(attachments), output_dir)
            if not text and attachments:
                text = _default_text_for_attachments(attachments)
            if not text:
                logger.warning(
                    "Codex CLI returned empty response: returncode=%s input_images=%s output_dir=%s stdout_tail=%r stderr_tail=%r",
                    proc.returncode,
                    len(image_paths),
                    output_dir,
                    _tail_text(stdout),
                    _tail_text(stderr),
                )
                raise CodexProxyError("Codex CLI returned an empty response")
            _update_running_request(
                request_id,
                status="completed",
                text_chars=len(text or ""),
                attachment_count=len(attachments or []),
            )
        finally:
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass
            if output_schema_path is not None:
                try:
                    output_schema_path.unlink(missing_ok=True)
                except Exception:
                    pass
            for image_path in temporary_image_paths:
                try:
                    image_path.unlink(missing_ok=True)
                except Exception:
                    pass
            if isolated_workdir_path is not None:
                try:
                    shutil.rmtree(isolated_workdir_path, ignore_errors=True)
                except Exception:
                    pass
            try:
                if output_dir.exists() and not any(output_dir.iterdir()):
                    output_dir.rmdir()
                if request_dir.exists() and not any(request_dir.iterdir()):
                    request_dir.rmdir()
            except Exception:
                pass
            active_job = codex_job_manager.get_active(request_id)
            if active_job:
                current_status = str(active_job.get("status") or "")
                if current_status == "completed":
                    final_status = "completed"
                    final_updates: Dict[str, Any] = {}
                elif current_status in {"cancelling", "cancelled"}:
                    final_status = "cancelled"
                    final_updates = {"error": "cancelled"}
                elif current_status in {"terminating"}:
                    final_status = "timeout"
                    final_updates = {"error": f"Codex CLI timed out after {timeout}s"}
                else:
                    final_status = "failed"
                    final_updates = {"error": f"Codex job ended before completion (status={current_status or 'unknown'})"}
                codex_job_manager.finish(request_id, status=final_status, **final_updates)

        now = int(time.time())
        usage = reported_usage or estimate_codex_usage(prompt, text)
        logger.info(
            "Codex request %s finished: model=%s elapsed=%.2fs text_chars=%s attachments=%s "
            "tokens=%s usage_source=%s",
            request_id,
            model,
            time.time() - started_at,
            len(text or ""),
            len(attachments or []),
            usage.get("total_tokens", 0) if isinstance(usage, dict) else 0,
            usage.get("source", usage.get("encoding", "unavailable")) if isinstance(usage, dict) else "unavailable",
        )
        return {
            "id": f"chatcmpl-codex-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": now,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": text,
                        **({"attachments": attachments} if attachments else {}),
                        **({"reasoning_content": reasoning_text, "reasoning": reasoning_text} if reasoning_text else {}),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
            "attachments": attachments,
        }
