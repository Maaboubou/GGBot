"""Persistent Codex App Server client used by the chatbot.

The App Server process lives with the FastAPI application.  Each WeChat chat
is mapped to a persisted Codex thread and receives only messages that were not
already sent to that thread.  Other LLM call types continue to use the
stateless Codex CLI adapter.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.services.codex_job_manager import codex_job_manager
from app.services.codex_proxy.client import (
    CodexProxyError,
    _as_bool,
    _as_runtime_path,
    _collect_artifact_attachments,
    _content_to_text,
    _default_text_for_attachments,
    _write_image_url_to_file,
    estimate_codex_usage,
    extract_image_urls,
    normalize_codex_web_search_mode,
    render_chat_prompt,
)


logger = logging.getLogger(__name__)


class CodexAppServerError(CodexProxyError):
    """Raised when the persistent Codex App Server cannot serve a turn."""


class _ResumeThreadError(CodexAppServerError):
    """Raised when a saved Codex thread can no longer be resumed."""


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[str] = None


@dataclass
class _TurnTracker:
    completed: threading.Event = field(default_factory=threading.Event)
    usage_ready: threading.Event = field(default_factory=threading.Event)
    agent_messages: List[str] = field(default_factory=list)
    reasoning: List[str] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    error: Optional[str] = None


@dataclass
class _MessageDelta:
    messages: List[Dict[str, Any]]
    stable_fingerprints: List[str]
    ephemeral_fingerprints: List[str]
    resume: bool
    rotation_reason: Optional[str] = None


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonnegative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _message_fingerprint(message: Dict[str, Any]) -> str:
    """Fingerprint semantic text while intentionally excluding image bytes."""
    normalized = {
        "role": str(message.get("role") or "user"),
        "name": str(message.get("name") or ""),
        "content": _content_to_text(message.get("content")),
    }
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_ephemeral_message(message: Dict[str, Any]) -> bool:
    return str(message.get("name") or "") in {"search_context", "memory_context"}


def _split_message_fingerprints(
    messages: Iterable[Dict[str, Any]],
) -> Tuple[List[str], List[str]]:
    stable: List[str] = []
    ephemeral: List[str] = []
    for message in messages:
        fingerprint = _message_fingerprint(message)
        if _is_ephemeral_message(message):
            ephemeral.append(fingerprint)
        else:
            stable.append(fingerprint)
    return stable, ephemeral


def _plan_message_delta(
    messages: List[Dict[str, Any]],
    state: Optional[Dict[str, Any]],
    *,
    model: str,
    reasoning_effort: str,
    retry: bool = False,
    rotate_tokens: int = 0,
    max_turns: int = 0,
) -> _MessageDelta:
    """Return the unsent message suffix or explain why a new thread is needed."""
    stable, ephemeral = _split_message_fingerprints(messages)
    if not state or not state.get("thread_id"):
        return _MessageDelta(list(messages), stable, ephemeral, False, "no_saved_thread")
    if str(state.get("model") or "") != model:
        return _MessageDelta(list(messages), stable, ephemeral, False, "model_changed")
    if str(state.get("reasoning_effort") or "") != reasoning_effort:
        return _MessageDelta(list(messages), stable, ephemeral, False, "reasoning_effort_changed")
    if max_turns > 0 and int(state.get("turn_count") or 0) >= max_turns:
        return _MessageDelta(list(messages), stable, ephemeral, False, "max_turns_reached")
    if rotate_tokens > 0 and int(state.get("last_input_tokens") or 0) >= rotate_tokens:
        return _MessageDelta(list(messages), stable, ephemeral, False, "soft_token_limit")

    sent_stable = list(state.get("stable_fingerprints") or [])
    if len(stable) < len(sent_stable) or stable[: len(sent_stable)] != sent_stable:
        return _MessageDelta(list(messages), stable, ephemeral, False, "message_prefix_changed")

    # Preserve original order: ephemeral search/memory context normally sits
    # directly before the newly appended user message.
    suffix: List[Dict[str, Any]] = []
    stable_index = 0
    duplicate_retry_ephemeral = retry and ephemeral == list(
        state.get("last_ephemeral_fingerprints") or []
    )
    for message in messages:
        if _is_ephemeral_message(message):
            if not duplicate_retry_ephemeral:
                suffix.append(message)
            continue
        if stable_index >= len(sent_stable):
            suffix.append(message)
        stable_index += 1

    return _MessageDelta(suffix, stable, ephemeral, True)


def _normalize_app_server_usage(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize authoritative App Server per-turn usage for LLMManager."""
    if not isinstance(raw, dict):
        return {}
    last = raw.get("last") if isinstance(raw.get("last"), dict) else {}
    total = raw.get("total") if isinstance(raw.get("total"), dict) else {}

    def number(source: Dict[str, Any], key: str) -> int:
        try:
            return max(0, int(source.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    input_tokens = number(last, "inputTokens")
    cached_tokens = min(number(last, "cachedInputTokens"), input_tokens)
    output_tokens = number(last, "outputTokens")
    reasoning_tokens = min(number(last, "reasoningOutputTokens"), output_tokens)
    total_tokens = number(last, "totalTokens") or input_tokens + output_tokens
    if not any((input_tokens, output_tokens, total_tokens)):
        return {}

    usage: Dict[str, Any] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_miss_input_tokens": max(input_tokens - cached_tokens, 0),
        "prompt_tokens_details": {"cached_tokens": cached_tokens},
        "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "estimated": False,
        "source": "codex_app_server.thread/tokenUsage/updated",
    }
    context_window = raw.get("modelContextWindow")
    if isinstance(context_window, int) and context_window > 0:
        usage["model_context_window"] = context_window
    if total:
        usage["session_total"] = {
            "input_tokens": number(total, "inputTokens"),
            "cached_input_tokens": number(total, "cachedInputTokens"),
            "output_tokens": number(total, "outputTokens"),
            "reasoning_output_tokens": number(total, "reasoningOutputTokens"),
            "total_tokens": number(total, "totalTokens"),
        }
    return usage


class CodexThreadStateStore:
    """Small atomic JSON store for WeChat chat -> Codex thread mappings."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(
            path
            or os.getenv("CODEX_APP_SERVER_THREAD_STATE")
            or "data/codex_app_server_threads.json"
        )
        self._lock = threading.RLock()
        self._states: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._states = {}
                return
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                states = payload.get("threads") if isinstance(payload, dict) else None
                self._states = states if isinstance(states, dict) else {}
            except Exception as exc:
                logger.warning("Failed to load Codex App Server thread state %s: %s", self.path, exc)
                self._states = {}

    def get(self, chat_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._states.get(chat_id)
            return dict(state) if isinstance(state, dict) else None

    def list_all(self) -> Dict[str, Dict[str, Any]]:
        """Return a detached snapshot for read-only monitoring APIs."""
        with self._lock:
            return {
                str(chat_id): dict(state)
                for chat_id, state in self._states.items()
                if isinstance(state, dict)
            }

    def put(self, chat_id: str, state: Dict[str, Any]) -> None:
        with self._lock:
            self._states[chat_id] = dict(state)
            self._save_locked()

    def delete(self, chat_id: str) -> None:
        with self._lock:
            if chat_id not in self._states:
                return
            self._states.pop(chat_id, None)
            self._save_locked()

    def _save_locked(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
            payload = {
                "version": 1,
                "updated_at": datetime.now().isoformat(),
                "threads": self._states,
            }
            try:
                temp_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(str(temp_path), str(self.path))
            finally:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass


class CodexAppServerManager:
    """Own a Codex App Server process and multiplex persistent chat threads."""

    def __init__(
        self,
        *,
        codex_bin: Optional[str] = None,
        workdir: Optional[str] = None,
        state_path: Optional[Path] = None,
    ) -> None:
        configured_bin = codex_bin or os.getenv("CODEX_PROXY_BIN")
        self.codex_bin = configured_bin or shutil.which("codex") or "codex"
        self.workdir = str(Path(workdir or os.getenv("CODEX_PROXY_WORKDIR") or Path.cwd()).resolve())
        self.use_wsl = os.name == "nt" and _as_bool(os.getenv("CODEX_PROXY_USE_WSL"), True)
        self.enabled = _as_bool(os.getenv("CODEX_APP_SERVER_ENABLED"), True)
        self.startup_timeout = _positive_int(os.getenv("CODEX_APP_SERVER_STARTUP_TIMEOUT"), 30)
        self.rotate_tokens = _nonnegative_int(
            os.getenv("CODEX_APP_SERVER_ROTATE_TOKENS", "220000"),
            220000,
        )
        self.state_store = CodexThreadStateStore(state_path)

        self._lifecycle_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.RLock()
        self._turn_lock = threading.RLock()
        self._chat_locks_lock = threading.Lock()
        self._chat_locks: Dict[str, threading.Lock] = {}
        self._pending: Dict[int, _PendingRequest] = {}
        self._turns: Dict[str, _TurnTracker] = {}
        self._loaded_threads: Dict[str, str] = {}
        self._request_id = 0
        self._generation = 0
        self._proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stopping = False
        self._initialize_result: Dict[str, Any] = {}

    def _command(self) -> List[str]:
        executable = os.getenv("CODEX_PROXY_WSL_BIN", "codex") if self.use_wsl else self.codex_bin
        args = [executable, "app-server", "--listen", "stdio://"]
        if self.use_wsl:
            return ["wsl.exe", "bash", "-lic", " ".join(shlex.quote(str(arg)) for arg in args)]
        return args

    def is_running(self) -> bool:
        proc = self._proc
        return bool(proc is not None and proc.poll() is None)

    def status(self) -> Dict[str, Any]:
        proc = self._proc
        return {
            "enabled": self.enabled,
            "running": self.is_running(),
            "pid": getattr(proc, "pid", None),
            "generation": self._generation,
            "loaded_threads": len(self._loaded_threads),
            "user_agent": self._initialize_result.get("userAgent"),
            "codex_home": self._initialize_result.get("codexHome"),
        }

    def invalidate_chat(self, chat_id: str) -> None:
        """Forget a chat mapping so the next call starts a clean thread."""
        self.state_store.delete(str(chat_id or ""))

    def session_snapshot(
        self,
        active_jobs: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Build a public view of persisted chatbot threads and active turns."""
        jobs = active_jobs if active_jobs is not None else codex_job_manager.list_active()
        active_by_chat: Dict[str, Dict[str, Any]] = {}
        for job in jobs:
            if not isinstance(job, dict):
                continue
            chat_id = str(job.get("chat_id") or "").strip()
            if chat_id:
                active_by_chat[chat_id] = job

        states = self.state_store.list_all()
        chat_ids = set(states) | set(active_by_chat)
        sessions: List[Dict[str, Any]] = []
        for chat_id in chat_ids:
            state = states.get(chat_id, {})
            active = active_by_chat.get(chat_id, {})
            session_total = (
                state.get("session_total")
                if isinstance(state.get("session_total"), dict)
                else {}
            )
            last_input = int(state.get("last_input_tokens") or 0)
            last_cached = min(int(state.get("last_cached_input_tokens") or 0), last_input)
            last_completion = int(
                state.get("last_completion_tokens")
                or max(int(state.get("last_total_tokens") or 0) - last_input, 0)
            )
            context_window = int(state.get("model_context_window") or 0)
            context_usage_percent = (
                round(last_input / context_window * 100, 2)
                if context_window > 0
                else None
            )
            cache_hit_rate = (
                round(last_cached / last_input, 6)
                if last_input > 0
                else None
            )
            sessions.append(
                {
                    "chat_id": chat_id,
                    "role_name": state.get("role_name"),
                    "thread_id": active.get("thread_id") or state.get("thread_id"),
                    "turn_id": active.get("turn_id") or state.get("last_turn_id"),
                    "request_id": active.get("request_id"),
                    "status": str(active.get("status") or "idle"),
                    "model": active.get("model") or state.get("model"),
                    "reasoning_effort": active.get("reasoning_effort") or state.get("reasoning_effort"),
                    "reasoning_summary": active.get("reasoning_summary") or state.get("reasoning_summary"),
                    "web_search_mode": active.get("web_search_mode") or state.get("web_search_mode"),
                    "turn_count": int(state.get("turn_count") or 0),
                    "last_input_tokens": last_input,
                    "last_cached_input_tokens": last_cached,
                    "last_cache_miss_input_tokens": max(last_input - last_cached, 0),
                    "last_completion_tokens": last_completion,
                    "last_reasoning_tokens": int(state.get("last_reasoning_tokens") or 0),
                    "last_total_tokens": int(state.get("last_total_tokens") or 0),
                    "cache_hit_rate": cache_hit_rate,
                    "model_context_window": context_window or None,
                    "context_usage_percent": context_usage_percent,
                    "session_total": session_total,
                    "usage_source": state.get("usage_source"),
                    "usage_estimated": bool(state.get("usage_estimated", False)),
                    "active_prompt_chars": int(active.get("prompt_chars") or 0),
                    "updated_at": state.get("updated_at") or active.get("started_at"),
                }
            )

        def updated_timestamp(item: Dict[str, Any]) -> float:
            value = item.get("updated_at")
            if isinstance(value, (int, float)):
                return float(value)
            try:
                return datetime.fromisoformat(str(value or "")).timestamp()
            except (TypeError, ValueError):
                return 0.0

        sessions.sort(key=updated_timestamp, reverse=True)
        current_thread_total = sum(
            int((item.get("session_total") or {}).get("total_tokens") or 0)
            for item in sessions
        )
        current_thread_cached = sum(
            int((item.get("session_total") or {}).get("cached_input_tokens") or 0)
            for item in sessions
        )
        return {
            "sessions": sessions,
            "stats": {
                "session_count": len(sessions),
                "active_session_count": sum(1 for item in sessions if item["status"] != "idle"),
                "total_turn_count": sum(int(item.get("turn_count") or 0) for item in sessions),
                "current_thread_total_tokens": current_thread_total,
                "current_thread_cached_tokens": current_thread_cached,
            },
        }

    def start(self, timeout: Optional[int] = None) -> bool:
        if not self.enabled:
            logger.info("Codex App Server is disabled; stateless codex exec remains available")
            return False
        with self._lifecycle_lock:
            if self.is_running():
                return True
            self._stopping = False
            self._fail_all("Codex App Server restarting")
            command = self._command()
            popen_kwargs: Dict[str, Any] = {
                "cwd": self.workdir,
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                popen_kwargs["start_new_session"] = True
            try:
                proc = subprocess.Popen(command, **popen_kwargs)
            except FileNotFoundError as exc:
                raise CodexAppServerError(
                    "Codex CLI/App Server was not found; check CODEX_PROXY_BIN or WSL Codex installation"
                ) from exc

            self._proc = proc
            self._loaded_threads.clear()
            self._stdout_thread = threading.Thread(
                target=self._stdout_loop,
                args=(proc,),
                name="codex-app-server-stdout",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._stderr_loop,
                args=(proc,),
                name="codex-app-server-stderr",
                daemon=True,
            )
            self._stdout_thread.start()
            self._stderr_thread.start()
            try:
                self._initialize_result = self._request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "wxautox4-chatbot",
                            "title": "wxautox4 ChatBot",
                            "version": "1.0.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                    timeout=timeout or self.startup_timeout,
                    ensure_started=False,
                )
                self._notify("initialized", {})
            except Exception:
                self._stop_process(proc)
                if self._proc is proc:
                    self._proc = None
                raise
            self._generation += 1
            logger.info(
                "Codex App Server started: pid=%s generation=%s user_agent=%s",
                proc.pid,
                self._generation,
                self._initialize_result.get("userAgent", "unknown"),
            )
            return True

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stopping = True
            proc = self._proc
            self._proc = None
            if proc is not None:
                self._stop_process(proc)
            self._loaded_threads.clear()
            self._fail_all("Codex App Server stopped")
            logger.info("Codex App Server stopped")

    def _stop_process(self, proc: subprocess.Popen) -> None:
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is not None:
            return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=3)
            return
        except Exception:
            pass
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass

    def _stdout_loop(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw_line in stream:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Ignoring non-JSON Codex App Server stdout: %s", line[:500])
                    continue
                self._handle_message(message)
        except Exception:
            if not self._stopping:
                logger.exception("Codex App Server stdout reader failed")
        finally:
            if self._proc is proc:
                self._proc = None
                if not self._stopping:
                    logger.warning("Codex App Server exited unexpectedly with code %s", proc.poll())
                    self._fail_all("Codex App Server exited unexpectedly")

    def _stderr_loop(self, proc: subprocess.Popen) -> None:
        stream = proc.stderr
        if stream is None:
            return
        try:
            for raw_line in stream:
                line = raw_line.strip()
                if line:
                    logger.debug("Codex App Server stderr: %s", line[:2000])
        except Exception:
            if not self._stopping:
                logger.debug("Codex App Server stderr reader ended", exc_info=True)

    def _handle_message(self, message: Dict[str, Any]) -> None:
        if "method" in message and "id" in message:
            self._handle_server_request(message)
            return
        if "id" in message:
            request_id = message.get("id")
            with self._pending_lock:
                pending = self._pending.get(request_id)
            if pending is None:
                return
            if isinstance(message.get("error"), dict):
                error = message["error"]
                pending.error = str(error.get("message") or error)
            else:
                pending.result = message.get("result")
            pending.event.set()
            return
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        self._handle_notification(method, params)

    def _handle_server_request(self, message: Dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if method.endswith("/requestApproval") or "approval" in method.lower():
            response = {"id": request_id, "result": {"decision": "decline"}}
        elif "elicitation" in method.lower():
            response = {"id": request_id, "result": {"action": "decline"}}
        else:
            response = {
                "id": request_id,
                "error": {"code": -32601, "message": f"Unsupported client method: {method}"},
            }
        try:
            self._send(response)
        except Exception:
            logger.debug("Failed to reject Codex App Server request %s", method, exc_info=True)

    def _tracker(self, turn_id: str) -> _TurnTracker:
        with self._turn_lock:
            return self._turns.setdefault(turn_id, _TurnTracker())

    @staticmethod
    def _append_unique(values: List[str], value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)

    def _read_completed_item(self, tracker: _TurnTracker, item: Dict[str, Any]) -> None:
        item_type = str(item.get("type") or "")
        if item_type == "agentMessage":
            self._append_unique(tracker.agent_messages, item.get("text"))
        elif item_type == "reasoning":
            summary = item.get("summary")
            content = item.get("content")
            if isinstance(summary, list):
                for part in summary:
                    self._append_unique(tracker.reasoning, part)
            else:
                self._append_unique(tracker.reasoning, summary)
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        self._append_unique(tracker.reasoning, part.get("text"))
                    else:
                        self._append_unique(tracker.reasoning, part)

    def _handle_notification(self, method: str, params: Dict[str, Any]) -> None:
        turn_id = str(params.get("turnId") or "")
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        if not turn_id:
            turn_id = str(turn.get("id") or "")
        if not turn_id:
            return
        tracker = self._tracker(turn_id)
        if method == "item/completed":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            self._read_completed_item(tracker, item)
        elif method == "thread/tokenUsage/updated":
            tracker.usage = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), dict) else {}
            tracker.usage_ready.set()
        elif method == "error":
            error = params.get("error") if isinstance(params.get("error"), dict) else {}
            if not params.get("willRetry"):
                tracker.error = str(error.get("message") or error or "Codex turn failed")
        elif method == "turn/completed":
            for item in turn.get("items") or []:
                if isinstance(item, dict):
                    self._read_completed_item(tracker, item)
            tracker.status = str(turn.get("status") or "completed")
            error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
            if error:
                tracker.error = str(error.get("message") or error)
            tracker.completed.set()

    def _send(self, message: Dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                raise CodexAppServerError("Codex App Server is not running")
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise CodexAppServerError("Codex App Server connection closed") from exc

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._send({"method": method, "params": params or {}})

    def _request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: int = 30,
        ensure_started: bool = True,
    ) -> Any:
        if ensure_started:
            self.start()
        with self._pending_lock:
            self._request_id += 1
            request_id = self._request_id
            pending = _PendingRequest()
            self._pending[request_id] = pending
        try:
            self._send({"id": request_id, "method": method, "params": params or {}})
            if not pending.event.wait(timeout=max(1, timeout)):
                raise CodexAppServerError(f"Codex App Server {method} timed out after {timeout}s")
            if pending.error:
                raise CodexAppServerError(f"Codex App Server {method} failed: {pending.error}")
            return pending.result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def _fail_all(self, error: str) -> None:
        with self._pending_lock:
            pending_values = list(self._pending.values())
        for pending in pending_values:
            pending.error = error
            pending.event.set()
        with self._turn_lock:
            trackers = list(self._turns.values())
        for tracker in trackers:
            if not tracker.completed.is_set():
                tracker.error = error
                tracker.status = "failed"
                tracker.completed.set()

    def _chat_lock(self, chat_id: str) -> threading.Lock:
        with self._chat_locks_lock:
            return self._chat_locks.setdefault(chat_id, threading.Lock())

    def _thread_config(
        self,
        reasoning_effort: str,
        web_search_mode: str,
        reasoning_summary: str,
    ) -> Dict[str, Any]:
        config: Dict[str, Any] = {
            "model_reasoning_effort": reasoning_effort,
            "web_search": web_search_mode,
            "features": {
                "multi_agent": False,
                "memories": False,
                "remote_plugin": False,
            },
            "apps": {"_default": {"enabled": False}},
        }
        if reasoning_summary != "inherit":
            config["model_reasoning_summary"] = reasoning_summary
            config["model_supports_reasoning_summaries"] = reasoning_summary != "none"
        return config

    @staticmethod
    def _thread_config_signature(config: Dict[str, Any]) -> str:
        return json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _start_thread(
        self,
        *,
        model: str,
        reasoning_effort: str,
        web_search_mode: str,
        reasoning_summary: str,
        timeout: int,
    ) -> str:
        runtime_workdir = _as_runtime_path(Path(self.workdir), self.use_wsl)
        thread_config = self._thread_config(
            reasoning_effort,
            web_search_mode,
            reasoning_summary,
        )
        result = self._request(
            "thread/start",
            {
                "model": model,
                "cwd": runtime_workdir,
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "ephemeral": False,
                "config": thread_config,
            },
            timeout=min(timeout, 120),
        )
        thread = result.get("thread") if isinstance(result, dict) else {}
        thread_id = str(thread.get("id") or "") if isinstance(thread, dict) else ""
        if not thread_id:
            raise CodexAppServerError("Codex App Server thread/start returned no thread id")
        self._loaded_threads[thread_id] = self._thread_config_signature(thread_config)
        return thread_id

    def _resume_thread(
        self,
        thread_id: str,
        *,
        model: str,
        reasoning_effort: str,
        web_search_mode: str,
        reasoning_summary: str,
        timeout: int,
    ) -> None:
        thread_config = self._thread_config(
            reasoning_effort,
            web_search_mode,
            reasoning_summary,
        )
        config_signature = self._thread_config_signature(thread_config)
        if self._loaded_threads.get(thread_id) == config_signature:
            return
        runtime_workdir = _as_runtime_path(Path(self.workdir), self.use_wsl)
        try:
            result = self._request(
                "thread/resume",
                {
                    "threadId": thread_id,
                    "model": model,
                    "cwd": runtime_workdir,
                    "approvalPolicy": "never",
                    "sandbox": "workspace-write",
                    "config": thread_config,
                },
                timeout=min(timeout, 120),
            )
        except CodexAppServerError as exc:
            raise _ResumeThreadError(str(exc)) from exc
        thread = result.get("thread") if isinstance(result, dict) else {}
        resumed_id = str(thread.get("id") or "") if isinstance(thread, dict) else ""
        if resumed_id != thread_id:
            raise _ResumeThreadError("Codex App Server resumed an unexpected thread")
        self._loaded_threads[thread_id] = config_signature

    def _interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        try:
            self._request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                timeout=10,
            )
        except Exception:
            logger.warning("Failed to interrupt Codex turn %s", turn_id, exc_info=True)

    def chat(
        self,
        request: Dict[str, Any],
        *,
        chat_id: str,
        role_name: Optional[str] = None,
        retry: bool = False,
        max_turns: int = 0,
    ) -> Dict[str, Any]:
        if not self.enabled:
            raise CodexAppServerError("Codex App Server is disabled")
        if not chat_id:
            raise CodexAppServerError("chat_id is required for persistent Codex threads")
        with self._chat_lock(chat_id):
            return self._chat_locked(
                request,
                chat_id=chat_id,
                role_name=role_name,
                retry=retry,
                max_turns=max(0, int(max_turns or 0)),
            )

    def _chat_locked(
        self,
        request: Dict[str, Any],
        *,
        chat_id: str,
        role_name: Optional[str],
        retry: bool,
        max_turns: int,
    ) -> Dict[str, Any]:
        self.start()
        model = str(request.get("model") or os.getenv("CODEX_PROXY_MODEL") or "gpt-5.6-sol")
        extra_body = request.get("extra_body") if isinstance(request.get("extra_body"), dict) else {}
        reasoning_effort = str(
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
            False,
        )
        timeout = _positive_int(request.get("timeout"), 600)
        messages = request.get("messages") or []
        if not isinstance(messages, list):
            raise CodexAppServerError("messages must be a list")

        state = self.state_store.get(chat_id)
        effective_rotate_tokens = self.rotate_tokens
        state_context_window = int((state or {}).get("model_context_window") or 0)
        if state_context_window > 0:
            # Keep a final safety band even when an old environment override
            # was tuned for a previous 1M-context model.
            runtime_soft_limit = max(4096, state_context_window - 32768)
            effective_rotate_tokens = (
                min(effective_rotate_tokens, runtime_soft_limit)
                if effective_rotate_tokens > 0
                else runtime_soft_limit
            )
        delta = _plan_message_delta(
            messages,
            state,
            model=model,
            reasoning_effort=reasoning_effort,
            retry=retry,
            rotate_tokens=effective_rotate_tokens,
            max_turns=max_turns,
        )
        thread_id = str(state.get("thread_id") or "") if delta.resume and state else ""
        if thread_id:
            try:
                self._resume_thread(
                    thread_id,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    web_search_mode=web_search_mode,
                    reasoning_summary=reasoning_summary,
                    timeout=timeout,
                )
            except _ResumeThreadError as exc:
                logger.warning(
                    "Codex thread %s for chat %s cannot be resumed (%s); rotating",
                    thread_id,
                    chat_id,
                    exc,
                )
                delta = _MessageDelta(
                    list(messages),
                    delta.stable_fingerprints,
                    delta.ephemeral_fingerprints,
                    False,
                    "resume_failed",
                )
                thread_id = ""

        if not thread_id:
            thread_id = self._start_thread(
                model=model,
                reasoning_effort=reasoning_effort,
                web_search_mode=web_search_mode,
                reasoning_summary=reasoning_summary,
                timeout=timeout,
            )
            logger.info(
                "Codex persistent thread started: chat=%s thread=%s reason=%s",
                chat_id,
                thread_id,
                delta.rotation_reason,
            )
        elif not delta.messages:
            raise CodexAppServerError("No new chatbot messages to send to the persistent Codex thread")

        request_id = uuid.uuid4().hex
        artifact_root = Path(os.getenv("CODEX_PROXY_ARTIFACT_ROOT") or "tmp/images/codex")
        if not artifact_root.is_absolute():
            artifact_root = Path(self.workdir) / artifact_root
        request_dir = artifact_root / request_id
        output_dir = request_dir / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        runtime_output_dir = _as_runtime_path(output_dir, self.use_wsl)

        image_urls = extract_image_urls(delta.messages, allow_image_input=allow_image_input)
        temporary_image_paths: List[Path] = []
        runtime_image_paths: List[str] = []
        for image_url in image_urls:
            image_path = _write_image_url_to_file(image_url)
            if image_url.startswith("data:"):
                temporary_image_paths.append(image_path)
            runtime_image_paths.append(_as_runtime_path(image_path, self.use_wsl))

        prompt = render_chat_prompt(
            delta.messages,
            artifact_output_dir=runtime_output_dir,
            native_web_search_enabled=web_search_enabled,
            input_image_count=len(runtime_image_paths),
        )
        if delta.resume:
            prompt = (
                "Continue the existing conversation. The conversation block below contains only "
                "new framework messages; all earlier messages remain in this Codex thread.\n\n"
                + prompt
            )
        turn_input: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        turn_input.extend(
            {"type": "localImage", "path": path, "detail": "auto"}
            for path in runtime_image_paths
        )

        started_at = time.time()
        codex_job_manager.register(
            request_id,
            {
                "request_id": request_id,
                "status": "starting",
                "backend": "codex_app_server",
                "model": model,
                "reasoning_effort": reasoning_effort,
                "web_search": web_search_enabled,
                "web_search_mode": web_search_mode,
                "reasoning_summary": reasoning_summary,
                "max_turns": max_turns,
                "timeout": timeout,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "message_count": len(delta.messages),
                "prompt_chars": len(prompt),
                "image_count": len(runtime_image_paths),
                "request_dir": str(request_dir),
                "output_dir": str(output_dir),
                "started_at": started_at,
            },
        )
        turn_id = ""
        tracker: Optional[_TurnTracker] = None
        try:
            result = self._request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": turn_input,
                    "model": model,
                    "effort": reasoning_effort,
                    "cwd": _as_runtime_path(Path(self.workdir), self.use_wsl),
                    "approvalPolicy": "never",
                    **(
                        {"summary": reasoning_summary}
                        if reasoning_summary != "inherit"
                        else {}
                    ),
                },
                timeout=min(timeout, 120),
            )
            turn = result.get("turn") if isinstance(result, dict) else {}
            turn_id = str(turn.get("id") or "") if isinstance(turn, dict) else ""
            if not turn_id:
                raise CodexAppServerError("Codex App Server turn/start returned no turn id")
            tracker = self._tracker(turn_id)
            codex_job_manager.update(
                request_id,
                status="running",
                turn_id=turn_id,
                _cancel_callback=lambda: self._interrupt_turn(thread_id, turn_id),
            )
            if not tracker.completed.wait(timeout=timeout):
                self._interrupt_turn(thread_id, turn_id)
                raise CodexAppServerError(f"Codex App Server turn timed out after {timeout}s")
            if not tracker.usage:
                tracker.usage_ready.wait(timeout=2)
            if tracker.error or tracker.status not in {"completed", "complete"}:
                raise CodexAppServerError(
                    tracker.error or f"Codex App Server turn ended with status {tracker.status}"
                )

            text = tracker.agent_messages[-1].strip() if tracker.agent_messages else ""
            attachments = _collect_artifact_attachments(output_dir)
            if not text and attachments:
                text = _default_text_for_attachments(attachments)
            if not text:
                raise CodexAppServerError("Codex App Server returned an empty response")
            usage = _normalize_app_server_usage(tracker.usage)
            if not usage:
                usage = estimate_codex_usage(prompt, text)
                usage["source"] = "codex_app_server_local_estimate_missing_notification"
            reasoning_text = "\n\n".join(tracker.reasoning).strip()

            assistant_fingerprint = _message_fingerprint(
                {"role": "assistant", "content": text}
            )
            previous_turn_count = int(state.get("turn_count") or 0) if state and delta.resume else 0
            state_payload = {
                "thread_id": thread_id,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "reasoning_summary": reasoning_summary,
                "web_search_mode": web_search_mode,
                "role_name": role_name,
                "stable_fingerprints": delta.stable_fingerprints + [assistant_fingerprint],
                "last_input_stable_fingerprints": delta.stable_fingerprints,
                "last_ephemeral_fingerprints": delta.ephemeral_fingerprints,
                "turn_count": previous_turn_count + 1,
                "last_input_tokens": int(usage.get("prompt_tokens") or 0),
                "last_cached_input_tokens": int(
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                ),
                "last_cache_miss_input_tokens": int(usage.get("cache_miss_input_tokens") or 0),
                "last_completion_tokens": int(usage.get("completion_tokens") or 0),
                "last_reasoning_tokens": int(
                    (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
                ),
                "last_total_tokens": int(usage.get("total_tokens") or 0),
                "model_context_window": usage.get("model_context_window"),
                "session_total": usage.get("session_total"),
                "usage_source": usage.get("source"),
                "usage_estimated": bool(usage.get("estimated", False)),
                "last_turn_id": turn_id,
                "updated_at": datetime.now().isoformat(),
            }
            self.state_store.put(chat_id, state_payload)
            codex_job_manager.update(
                request_id,
                status="completed",
                text_chars=len(text),
                attachment_count=len(attachments),
                prompt_tokens=usage.get("prompt_tokens", 0),
                cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            )
            logger.info(
                "Codex App Server turn finished: chat=%s thread=%s turn=%s resumed=%s "
                "elapsed=%.2fs input=%s cached=%s output=%s total=%s",
                chat_id,
                thread_id,
                turn_id,
                delta.resume,
                time.time() - started_at,
                usage.get("prompt_tokens", 0),
                (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
            )
            now = int(time.time())
            return {
                "id": f"chatcmpl-codex-thread-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": now,
                "model": model,
                "backend": "codex_app_server",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": text,
                            **({"attachments": attachments} if attachments else {}),
                            **(
                                {"reasoning_content": reasoning_text, "reasoning": reasoning_text}
                                if reasoning_text
                                else {}
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
                "attachments": attachments,
            }
        except Exception as exc:
            codex_job_manager.update(request_id, error=str(exc))
            raise
        finally:
            if turn_id:
                with self._turn_lock:
                    self._turns.pop(turn_id, None)
            for image_path in temporary_image_paths:
                try:
                    image_path.unlink(missing_ok=True)
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
                status = str(active_job.get("status") or "")
                if status == "completed":
                    codex_job_manager.finish(request_id, status="completed")
                elif status in {"cancelling", "cancelled"}:
                    codex_job_manager.finish(request_id, status="cancelled", error="cancelled")
                else:
                    codex_job_manager.finish(
                        request_id,
                        status="failed",
                        error=str(active_job.get("error") or "Codex App Server turn failed"),
                    )


_manager_lock = threading.Lock()
_manager: Optional[CodexAppServerManager] = None


def get_codex_app_server_manager() -> CodexAppServerManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = CodexAppServerManager()
    return _manager
