"""Unified local agent runtime backed by the globally installed Codex CLI."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.services.codex_app_server import (
    CodexAppServerError,
    CodexAppServerManager,
    CodexThreadStateStore,
)
from app.services.codex_proxy.client import CodexCliClient, CodexProxyError, _as_bool, _as_runtime_path
from app.services.codex_access_service import codex_access_service
from app.services.file_tools_runtime import (
    build_codex_runtime_command,
    get_codex_bin_selection,
    get_file_tools_runtime,
)


logger = logging.getLogger(__name__)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


@dataclass(frozen=True)
class AgentProfile:
    name: str
    pool: str
    persistent: bool
    priority: int
    sandbox: str = "workspace-write"
    config_policy: str = "inherit"
    allow_exec_fallback: bool = True


DEFAULT_PROFILES: Dict[str, AgentProfile] = {
    "chat": AgentProfile("chat", "interactive", True, 100),
    "proxy": AgentProfile("proxy", "batch", False, 70),
    "memory": AgentProfile("memory", "batch", False, 60, sandbox="read-only"),
    "weekly": AgentProfile("weekly", "batch", False, 40),
    "batch": AgentProfile("batch", "batch", False, 50),
}


@dataclass(frozen=True)
class CodexBinaryIdentity:
    executable: str
    realpath: str
    version: str
    schema_hash: str
    compatible: bool
    capabilities: Dict[str, bool]
    checked_at: str
    errors: Tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.realpath}|{self.version}|{self.schema_hash}"

    def public(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["errors"] = list(self.errors)
        payload["key"] = self.key
        return payload


class CodexCompatibilityProbe:
    """Resolve the global CLI and validate the exact protocol it exposes."""

    REQUIRED_PROTOCOL_MARKERS = (
        '"thread/start"',
        '"thread/resume"',
        '"turn/start"',
        '"turn/interrupt"',
        '"item/completed"',
        '"turn/completed"',
        '"thread/tokenUsage/updated"',
        '"account/rateLimits/read"',
        '"ephemeral"',
        '"outputSchema"',
    )

    def __init__(self, *, codex_bin: Optional[str] = None, workdir: Optional[str] = None) -> None:
        self.workdir = str(Path(workdir or os.getenv("CODEX_PROXY_WORKDIR") or Path.cwd()).resolve())
        self.use_wsl = os.name == "nt" and _as_bool(os.getenv("CODEX_PROXY_USE_WSL"), True)
        binary_policy = str(os.getenv("CODEX_BINARY_POLICY") or "global").strip().lower()
        configured = codex_bin or os.getenv("CODEX_PROXY_BIN")
        if self.use_wsl:
            self.codex_bin = codex_bin or get_codex_bin_selection(use_wsl=True)["configured"]
        elif codex_bin:
            self.codex_bin = codex_bin
        elif binary_policy == "configured" and configured:
            self.codex_bin = configured
        else:
            self.codex_bin = shutil.which("codex") or "codex"

    def _command(self, args: List[str]) -> List[str]:
        inner = [self.codex_bin, *args]
        snapshot = (
            get_file_tools_runtime(use_wsl=True, codex_bin=self.codex_bin)
            if self.use_wsl
            else None
        )
        return build_codex_runtime_command(
            inner,
            use_wsl=self.use_wsl,
            snapshot=snapshot,
        )

    def quick_signature(self) -> Tuple[str, str, int]:
        realpath = self.codex_bin
        mtime_ns = 0
        if self.use_wsl:
            runtime = get_file_tools_runtime(use_wsl=True, codex_bin=self.codex_bin)
            result = subprocess.run(
                self._command(["--version"]),
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            version_lines = (result.stdout or result.stderr or "").strip().splitlines()
            if result.returncode != 0 or not version_lines:
                detail = (result.stderr or result.stdout or "Codex CLI unavailable").strip()
                raise RuntimeError(detail[-1500:])
            version = version_lines[-1]
            identity = runtime.get("codex") if isinstance(runtime.get("codex"), dict) else {}
            realpath = str(identity.get("realpath") or identity.get("path") or self.codex_bin)
            return realpath, version, mtime_ns
        else:
            resolved = shutil.which(self.codex_bin) or self.codex_bin
            realpath = str(Path(resolved).resolve())
            try:
                mtime_ns = Path(realpath).stat().st_mtime_ns
            except OSError:
                pass
        result = subprocess.run(
            self._command(["--version"]),
            cwd=self.workdir,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        version = (result.stdout or result.stderr or "").strip().splitlines()[-1:]
        return realpath, (version[0] if version else ""), mtime_ns

    def probe(self) -> CodexBinaryIdentity:
        errors: List[str] = []
        try:
            realpath, version, _ = self.quick_signature()
        except Exception as exc:
            return CodexBinaryIdentity(
                self.codex_bin,
                self.codex_bin,
                "",
                "",
                False,
                {},
                _iso_now(),
                (f"Codex CLI unavailable: {exc}",),
            )
        if not version:
            errors.append("Codex CLI did not report a version")

        capabilities: Dict[str, bool] = {}
        schema_hash = ""
        try:
            with tempfile.TemporaryDirectory(prefix="wxautox_codex_schema_") as directory:
                output_dir = Path(directory)
                runtime_dir = _as_runtime_path(output_dir, self.use_wsl)
                result = subprocess.run(
                    self._command(["app-server", "generate-json-schema", "--out", runtime_dir]),
                    cwd=self.workdir,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "schema generation failed").strip()
                    raise RuntimeError(detail[-1500:])
                schema_files = sorted(output_dir.rglob("*.json"))
                if not schema_files:
                    raise RuntimeError("Codex CLI generated no JSON Schema files")
                digest = hashlib.sha256()
                combined: List[str] = []
                for path in schema_files:
                    relative = path.relative_to(output_dir).as_posix()
                    data = path.read_bytes()
                    digest.update(relative.encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(data)
                    combined.append(data.decode("utf-8", errors="replace"))
                schema_hash = digest.hexdigest()
                schema_text = "\n".join(combined)
                for marker in self.REQUIRED_PROTOCOL_MARKERS:
                    capabilities[marker.strip('"')] = marker in schema_text
                capabilities["message_phase"] = '"final_answer"' in schema_text
                missing = [name for name, present in capabilities.items() if not present and name != "message_phase"]
                if missing:
                    errors.append("Missing protocol capabilities: " + ", ".join(sorted(missing)))
        except Exception as exc:
            errors.append(f"Protocol schema check failed: {exc}")

        return CodexBinaryIdentity(
            executable=self.codex_bin,
            realpath=realpath,
            version=version,
            schema_hash=schema_hash,
            compatible=not errors,
            capabilities=capabilities,
            checked_at=_iso_now(),
            errors=tuple(errors),
        )


def _iso_now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


class _PoolLease(AbstractContextManager[CodexAppServerManager]):
    def __init__(self, pool: "CodexProcessPool", index: int, manager: CodexAppServerManager) -> None:
        self.pool = pool
        self.index = index
        self.manager = manager

    def __enter__(self) -> CodexAppServerManager:
        return self.manager

    def __exit__(self, exc_type, exc, tb) -> None:
        self.pool.release(self.index, failed=exc is not None)


class CodexProcessPool:
    """Bounded process pool with draining and queue telemetry."""

    def __init__(
        self,
        name: str,
        managers: List[CodexAppServerManager],
        *,
        concurrency_per_process: int,
    ) -> None:
        self.name = name
        self.managers = managers
        self.capacity = max(1, int(concurrency_per_process))
        self._condition = threading.Condition(threading.RLock())
        self._active = [0 for _ in managers]
        self._accepting = True
        self._waiters: List[Tuple[int, int]] = []
        self._ticket_counter = 0
        self._completed = 0
        self._failed = 0
        self._queue_wait_total = 0.0

    def start(self) -> None:
        started: List[CodexAppServerManager] = []
        try:
            for manager in self.managers:
                manager.start()
                started.append(manager)
        except Exception:
            for manager in started:
                manager.stop()
            raise

    def acquire(self, timeout: int, *, priority: int = 0) -> _PoolLease:
        started_at = time.monotonic()
        deadline = started_at + max(1, int(timeout))
        with self._condition:
            self._ticket_counter += 1
            ticket = (int(priority), self._ticket_counter)
            self._waiters.append(ticket)
            try:
                while True:
                    if not self._accepting:
                        raise CodexAppServerError(f"Codex {self.name} pool is draining")
                    candidates = [
                        index
                        for index, count in enumerate(self._active)
                        if count < self.capacity and self.managers[index].is_running()
                    ]
                    next_ticket = max(self._waiters, key=lambda item: (item[0], -item[1]))
                    if candidates and ticket == next_ticket:
                        index = min(candidates, key=lambda item: self._active[item])
                        self._active[index] += 1
                        self._waiters.remove(ticket)
                        self._queue_wait_total += time.monotonic() - started_at
                        return _PoolLease(self, index, self.managers[index])
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CodexAppServerError(f"Codex {self.name} pool queue timed out")
                    self._condition.wait(timeout=min(remaining, 1.0))
            finally:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)

    def release(self, index: int, *, failed: bool) -> None:
        with self._condition:
            self._active[index] = max(0, self._active[index] - 1)
            self._completed += 1
            if failed:
                self._failed += 1
            self._condition.notify_all()

    def drain_and_stop(self, timeout: int = 60) -> None:
        deadline = time.monotonic() + max(0, int(timeout))
        with self._condition:
            self._accepting = False
            self._condition.notify_all()
            while any(self._active) and time.monotonic() < deadline:
                self._condition.wait(timeout=min(1.0, max(0.0, deadline - time.monotonic())))
        for manager in self.managers:
            manager.stop()

    def status(self) -> Dict[str, Any]:
        with self._condition:
            active = list(self._active)
            completed = self._completed
            failed = self._failed
            waiting = len(self._waiters)
            waiting_by_priority: Dict[str, int] = {}
            for priority, _ in self._waiters:
                key = str(priority)
                waiting_by_priority[key] = waiting_by_priority.get(key, 0) + 1
            accepting = self._accepting
            queue_wait_total = self._queue_wait_total
        return {
            "name": self.name,
            "accepting": accepting,
            "size": len(self.managers),
            "concurrency_per_process": self.capacity,
            "capacity": len(self.managers) * self.capacity,
            "active": sum(active),
            "waiting": waiting,
            "waiting_by_priority": waiting_by_priority,
            "completed": completed,
            "failed": failed,
            "average_queue_wait_seconds": round(queue_wait_total / completed, 3) if completed else 0.0,
            "workers": [manager.status() for manager in self.managers],
        }


class CodexAgentRuntime:
    """Single control plane for interactive, batch and fallback Codex calls."""

    def __init__(self, *, workdir: Optional[str] = None, codex_bin: Optional[str] = None) -> None:
        self.workdir = str(Path(workdir or os.getenv("CODEX_PROXY_WORKDIR") or Path.cwd()).resolve())
        self.probe = CodexCompatibilityProbe(codex_bin=codex_bin, workdir=self.workdir)
        self.state_store = CodexThreadStateStore()
        self.profiles = dict(DEFAULT_PROFILES)
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._identity: Optional[CodexBinaryIdentity] = None
        self._discovered_identity: Optional[CodexBinaryIdentity] = None
        self._pools: Dict[str, CodexProcessPool] = {}
        self._monitor_stop = threading.Event()
        self._maintenance = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._last_refresh_error = ""
        self._version_transitions = 0
        self._circuit_failures = 0
        self._circuit_open_until = 0.0
        self._started_at = ""
        self._quick_signature: Optional[Tuple[str, str, int]] = None

    def _manager(self, identity: CodexBinaryIdentity, pool_name: str, index: int) -> CodexAppServerManager:
        return CodexAppServerManager(
            codex_bin=identity.executable,
            workdir=self.workdir,
            state_store=self.state_store,
            instance_name=f"{pool_name}-{index + 1}",
            codex_version=identity.version,
            schema_hash=identity.schema_hash,
            experimental_api=False,
        )

    def _build_pools(self, identity: CodexBinaryIdentity) -> Dict[str, CodexProcessPool]:
        interactive_size = _positive_int(os.getenv("CODEX_INTERACTIVE_POOL_SIZE"), 1)
        batch_size = _positive_int(os.getenv("CODEX_BATCH_POOL_SIZE"), 1)
        interactive_concurrency = _positive_int(os.getenv("CODEX_INTERACTIVE_PROCESS_CONCURRENCY"), 4)
        batch_concurrency = _positive_int(os.getenv("CODEX_BATCH_PROCESS_CONCURRENCY"), 1)
        pools = {
            "interactive": CodexProcessPool(
                "interactive",
                [self._manager(identity, "interactive", index) for index in range(interactive_size)],
                concurrency_per_process=interactive_concurrency,
            ),
            "batch": CodexProcessPool(
                "batch",
                [self._manager(identity, "batch", index) for index in range(batch_size)],
                concurrency_per_process=batch_concurrency,
            ),
        }
        started: List[CodexProcessPool] = []
        try:
            for pool in pools.values():
                pool.start()
                started.append(pool)
        except Exception:
            for pool in started:
                pool.drain_and_stop(timeout=0)
            raise
        return pools

    def start(self) -> bool:
        activated = self.refresh(force=True)
        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="codex-runtime-monitor",
                daemon=True,
            )
            self._monitor_thread.start()
        return activated or bool(self._pools)

    def _monitor_loop(self) -> None:
        interval = _positive_int(os.getenv("CODEX_VERSION_CHECK_INTERVAL_SECONDS"), 60)
        while not self._monitor_stop.wait(interval):
            if self._maintenance.is_set():
                continue
            try:
                signature = self.probe.quick_signature()
                if self._quick_signature != signature:
                    self.refresh(force=True)
            except Exception as exc:
                self._last_refresh_error = str(exc)
                logger.warning("Codex runtime discovery failed: %s", exc)

    def refresh(self, *, force: bool = False) -> bool:
        if not self._refresh_lock.acquire(blocking=False):
            return False
        try:
            identity = self.probe.probe()
            self._discovered_identity = identity
            current = self._identity
            if not force and current and current.key == identity.key:
                return False
            if current and current.key == identity.key and self._pools:
                self._last_refresh_error = ""
                self._quick_signature = self.probe.quick_signature()
                return False
            if not identity.compatible:
                self._last_refresh_error = "; ".join(identity.errors)
                if current is None:
                    self._identity = identity
                logger.error("Codex runtime compatibility check failed: %s", self._last_refresh_error)
                return False

            candidate = self._build_pools(identity)
            with self._lock:
                previous = self._pools
                previous_identity = self._identity
                self._pools = candidate
                self._identity = identity
                self._quick_signature = self.probe.quick_signature()
                self._last_refresh_error = ""
                self._started_at = self._started_at or _iso_now()
                if previous_identity and previous_identity.key != identity.key:
                    self._version_transitions += 1
            if previous:
                threading.Thread(
                    target=self._stop_pools,
                    args=(previous,),
                    name="codex-runtime-drain",
                    daemon=True,
                ).start()
            logger.info(
                "Codex runtime active: executable=%s version=%s schema=%s",
                identity.executable,
                identity.version,
                identity.schema_hash[:12],
            )
            return True
        except Exception as exc:
            self._last_refresh_error = str(exc)
            logger.exception("Codex runtime activation failed")
            return False
        finally:
            self._refresh_lock.release()

    def switch_codex_bin(self, codex_bin: str) -> bool:
        """Activate a compatible Codex path without interrupting active pools."""
        normalized = str(codex_bin or "").strip()
        if not normalized:
            raise ValueError("Codex path is required")
        previous = self.probe.codex_bin
        self.probe.codex_bin = normalized
        activated = self.refresh(force=True)
        discovered = self._discovered_identity
        with self._lock:
            active = self._identity
            has_pools = bool(self._pools)
        active_matches = bool(
            active
            and discovered
            and active.compatible
            and active.key == discovered.key
            and has_pools
        )
        selected = bool(
            discovered
            and discovered.compatible
            and discovered.executable == normalized
            and (activated or active_matches)
        )
        if not selected:
            self.probe.codex_bin = previous
            with self._lock:
                self._discovered_identity = self._identity
        return selected

    def _stop_pools(self, pools: Dict[str, CodexProcessPool]) -> None:
        drain_timeout = _positive_int(os.getenv("CODEX_POOL_DRAIN_TIMEOUT_SECONDS"), 60)
        for pool in pools.values():
            pool.drain_and_stop(timeout=drain_timeout)

    def stop(self) -> None:
        self._monitor_stop.set()
        monitor = self._monitor_thread
        if monitor and monitor.is_alive() and monitor is not threading.current_thread():
            monitor.join(timeout=2)
        with self._lock:
            pools = self._pools
            self._pools = {}
        self._stop_pools(pools)

    def set_maintenance(self, active: bool) -> None:
        if active:
            self._maintenance.set()
        else:
            self._maintenance.clear()

    def _profile(self, name: str) -> AgentProfile:
        return self.profiles.get(name) or self.profiles["batch"]

    def _pool(self, name: str) -> Optional[CodexProcessPool]:
        with self._lock:
            return self._pools.get(name)

    def _prepare_payload(self, payload: Dict[str, Any], profile: AgentProfile) -> Dict[str, Any]:
        result = dict(payload)
        result.setdefault("codex_sandbox", profile.sandbox)
        result.setdefault("codex_config_policy", profile.config_policy)
        return result

    def _circuit_open(self) -> bool:
        return time.monotonic() < self._circuit_open_until

    def _record_success(self) -> None:
        self._circuit_failures = 0
        self._circuit_open_until = 0.0

    def _record_failure(self) -> None:
        self._circuit_failures += 1
        threshold = _positive_int(os.getenv("CODEX_CIRCUIT_FAILURE_THRESHOLD"), 3)
        if self._circuit_failures >= threshold:
            self._circuit_open_until = time.monotonic() + _positive_int(
                os.getenv("CODEX_CIRCUIT_RESET_SECONDS"), 60
            )

    @staticmethod
    def _fallback_is_safe(exc: BaseException) -> bool:
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "is disabled",
                "is not running",
                "connection closed",
                "exited unexpectedly",
                "pool is draining",
                "queue timed out",
                "not found",
                "compatibility",
            )
        )

    def _exec(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        async def call() -> Dict[str, Any]:
            timeout = _positive_int(payload.get("timeout"), 600)
            return await CodexCliClient(
                codex_bin=self.probe.codex_bin,
                workdir=self.workdir,
                timeout_seconds=timeout,
            ).chat(payload)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(call())
        result: Dict[str, Any] = {}
        error: List[BaseException] = []

        def runner() -> None:
            try:
                result["value"] = asyncio.run(call())
            except BaseException as exc:
                error.append(exc)

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result["value"]

    def chat(
        self,
        payload: Dict[str, Any],
        *,
        chat_id: str,
        role_name: Optional[str] = None,
        retry: bool = False,
        max_turns: int = 0,
        allow_exec_fallback: Optional[bool] = None,
        persistent_session: bool = True,
    ) -> Dict[str, Any]:
        profile = self._profile("chat")
        request = self._prepare_payload(payload, profile)
        access = codex_access_service.for_chat(chat_id, ensure=True)
        request = access.apply(request)
        fallback = profile.allow_exec_fallback if allow_exec_fallback is None else allow_exec_fallback

        # Restricted chats deliberately use a fresh exec process. Unlike the
        # shared App Server, exec can ignore user config and execpolicy rules,
        # so a broad local allow rule cannot pierce the per-chat boundary.
        if not persistent_session or not access.persistent_thread:
            return self._exec(request)

        pool = self._pool(profile.pool)
        if pool is None or self._circuit_open():
            if fallback:
                return self._exec(request)
            raise CodexProxyError(self._last_refresh_error or "Codex runtime is unavailable")
        try:
            with pool.acquire(
                _positive_int(request.get("timeout"), 600),
                priority=profile.priority,
            ) as manager:
                response = manager.chat(
                    request,
                    chat_id=chat_id,
                    role_name=role_name,
                    retry=retry,
                    max_turns=max_turns,
                )
            self._record_success()
            return response
        except CodexAppServerError as exc:
            self._record_failure()
            if fallback and self._fallback_is_safe(exc):
                return self._exec(request)
            raise

    def run(
        self,
        payload: Dict[str, Any],
        *,
        profile_name: str = "batch",
        allow_exec_fallback: Optional[bool] = None,
    ) -> Dict[str, Any]:
        profile = self._profile(profile_name)
        request = self._prepare_payload(payload, profile)
        fallback = profile.allow_exec_fallback if allow_exec_fallback is None else allow_exec_fallback
        pool = self._pool(profile.pool)
        if pool is None or self._circuit_open():
            if fallback:
                return self._exec(request)
            raise CodexProxyError(self._last_refresh_error or "Codex runtime is unavailable")
        try:
            with pool.acquire(
                _positive_int(request.get("timeout"), 600),
                priority=profile.priority,
            ) as manager:
                response = manager.run(request, profile_name=profile.name)
            self._record_success()
            return response
        except CodexAppServerError as exc:
            self._record_failure()
            if fallback and self._fallback_is_safe(exc):
                return self._exec(request)
            raise

    def read_rate_limits(self, timeout: int = 30) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        pool = self._pool("interactive") or self._pool("batch")
        if pool is None:
            raise CodexProxyError(self._last_refresh_error or "Codex runtime is unavailable")
        with pool.acquire(timeout, priority=100) as manager:
            return manager.read_rate_limits(timeout), manager.status()

    def invalidate_chat(self, chat_id: str) -> None:
        self.state_store.delete(str(chat_id or ""))

    def delete_chat(self, chat_id: str) -> Dict[str, Any]:
        """Delete the managed session and its persisted Codex thread."""
        normalized = str(chat_id or "").strip()
        state = self.state_store.get(normalized)
        if not state:
            return {"chat_id": normalized, "deleted": False, "thread_deleted": False}

        thread_id = str(state.get("thread_id") or "").strip()
        if thread_id:
            pool = self._pool("interactive") or self._pool("batch")
            if pool is None:
                raise CodexAppServerError("Codex 运行时未就绪，无法删除持久线程")
            with pool.acquire(30, priority=1000) as manager:
                manager.delete_thread(thread_id, timeout=30)

        deleted = self.state_store.delete(normalized)
        if thread_id:
            with self._lock:
                managers = [manager for pool in self._pools.values() for manager in pool.managers]
            for manager in managers:
                manager.forget_loaded_thread(thread_id)
        return {
            "chat_id": normalized,
            "thread_id": thread_id or None,
            "deleted": deleted,
            "thread_deleted": bool(thread_id),
        }

    def session_snapshot(self, active_jobs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        pool = self._pool("interactive")
        if pool and pool.managers:
            return pool.managers[0].session_snapshot(active_jobs)
        return {"sessions": [], "stats": {"session_count": 0, "active_session_count": 0}}

    def status(self) -> Dict[str, Any]:
        with self._lock:
            pools = dict(self._pools)
            identity = self._identity
            discovered = self._discovered_identity
        pool_status = {name: pool.status() for name, pool in pools.items()}
        workers = [worker for pool in pool_status.values() for worker in pool.get("workers", [])]
        running_workers = [worker for worker in workers if worker.get("running")]
        return {
            "enabled": True,
            "running": bool(running_workers),
            "pid": running_workers[0].get("pid") if running_workers else None,
            "pids": [worker.get("pid") for worker in running_workers],
            "generation": self._version_transitions + (1 if identity else 0),
            "loaded_threads": sum(int(worker.get("loaded_threads") or 0) for worker in workers),
            "user_agent": running_workers[0].get("user_agent") if running_workers else None,
            "codex_home": running_workers[0].get("codex_home") if running_workers else None,
            "active_version": identity.version if identity and identity.compatible else "",
            "discovered_version": discovered.version if discovered else "",
            "schema_hash": identity.schema_hash if identity else "",
            "compatibility": discovered.public() if discovered else None,
            "pools": pool_status,
            "profiles": {name: asdict(profile) for name, profile in self.profiles.items()},
            "version_transitions": self._version_transitions,
            "circuit": {
                "failures": self._circuit_failures,
                "open": self._circuit_open(),
                "reset_in_seconds": round(max(0.0, self._circuit_open_until - time.monotonic()), 3),
            },
            "started_at": self._started_at,
            "maintenance": self._maintenance.is_set(),
            "last_error": self._last_refresh_error or None,
        }


_runtime_lock = threading.Lock()
_runtime: Optional[CodexAgentRuntime] = None


def get_agent_runtime() -> CodexAgentRuntime:
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = CodexAgentRuntime()
    return _runtime
