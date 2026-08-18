from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

_INTERNAL_KEYS = {"proc"}


def _public_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Return a JSON-safe/public snapshot of a Codex job."""
    snapshot = {k: v for k, v in job.items() if k not in _INTERNAL_KEYS and not str(k).startswith("_")}
    now = time.time()
    started_at = snapshot.get("started_at")
    ended_at = snapshot.get("ended_at")
    if isinstance(started_at, (int, float)):
        snapshot.setdefault("elapsed_seconds", round((ended_at or now) - started_at, 3))
    return snapshot


class CodexJobManager:
    """In-memory Codex CLI job registry with cancel support.

    This manager is intentionally process-local. It tracks active Codex CLI
    subprocesses launched by this API process and keeps a short recent history
    for the web management panel.
    """

    def __init__(self, max_recent: int = 100) -> None:
        self._lock = threading.RLock()
        self._active: Dict[str, Dict[str, Any]] = {}
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=max_recent)

    def register(self, request_id: str, info: Dict[str, Any]) -> None:
        with self._lock:
            job = dict(info)
            job.setdefault("request_id", request_id)
            job.setdefault("status", "starting")
            job.setdefault("started_at", time.time())
            self._active[request_id] = job

    def update(self, request_id: str, **updates: Any) -> None:
        with self._lock:
            if request_id in self._active:
                self._active[request_id].update(updates)

    def attach_process(self, request_id: str, proc: asyncio.subprocess.Process, **updates: Any) -> None:
        payload = dict(updates)
        payload.update({"proc": proc, "pid": getattr(proc, "pid", None), "status": updates.get("status", "running")})
        self.update(request_id, **payload)

    def finish(self, request_id: str, status: str = "completed", **updates: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._active.pop(request_id, None)
            if not job:
                return None
            ended_at = time.time()
            job.update(updates)
            job["status"] = status
            job.setdefault("ended_at", ended_at)
            started_at = job.get("started_at")
            if isinstance(started_at, (int, float)):
                job["duration_seconds"] = round(job["ended_at"] - started_at, 3)
            public = _public_job(job)
            self._recent.appendleft(public)
            return public

    def list_active(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [_public_job(job) for job in self._active.values()]

    def list_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._recent)[: max(0, min(limit, len(self._recent)))]

    def get_active(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._active.get(request_id)
            return _public_job(job) if job else None

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            active = list(self._active.values())
            recent = list(self._recent)
        now = time.time()
        return {
            "active_count": len(active),
            "recent_count": len(recent),
            "oldest_active_age_seconds": round(max((now - j.get("started_at", now) for j in active), default=0), 3),
            "active_by_status": self._count_by(active, "status"),
            "recent_by_status": self._count_by(recent, "status"),
        }

    @staticmethod
    def _count_by(items: List[Dict[str, Any]], key: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in items:
            value = str(item.get(key) or "unknown")
            counts[value] = counts.get(value, 0) + 1
        return counts

    async def cancel(self, request_id: str) -> Dict[str, Any]:
        """Cancel an active Codex job and best-effort kill its process tree."""
        with self._lock:
            job = self._active.get(request_id)
            if not job:
                return {"success": False, "message": "job not found or already finished", "request_id": request_id}
            job["status"] = "cancelling"
            job["cancel_requested_at"] = time.time()
            proc = job.get("proc")
            pid = job.get("pid")
            runtime_output_path = str(job.get("runtime_output_path") or "")
            use_wsl = bool(job.get("use_wsl"))
            cancel_callback = job.get("_cancel_callback")

        logger.warning("Cancelling Codex job %s pid=%s marker=%s", request_id, pid, runtime_output_path)
        if callable(cancel_callback):
            # App Server turns share one long-lived process, so cancelling the
            # subprocess would also abort unrelated chats. Interrupt only the
            # selected turn through its registered protocol callback.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, cancel_callback)
        else:
            await self._terminate_process_tree(proc, pid, runtime_output_path, use_wsl, request_id)
        self.finish(request_id, status="cancelled", cancelled_at=time.time())
        return {"success": True, "message": "cancel requested", "request_id": request_id}

    async def _terminate_process_tree(
        self,
        proc: Any,
        pid: Any,
        runtime_output_path: str,
        use_wsl: bool,
        request_id: str,
    ) -> None:
        if os.name == "nt":
            if pid:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                    )
                except Exception:
                    logger.exception("Codex job %s: taskkill failed", request_id)
            if use_wsl and runtime_output_path:
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
                    logger.exception("Codex job %s: WSL child cleanup failed", request_id)
        else:
            if pid:
                try:
                    os.killpg(int(pid), 15)
                except Exception:
                    try:
                        if proc:
                            proc.terminate()
                    except ProcessLookupError:
                        pass
            try:
                if proc:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                    return
            except Exception:
                pass
            if pid:
                try:
                    os.killpg(int(pid), 9)
                except Exception:
                    try:
                        if proc:
                            proc.kill()
                    except ProcessLookupError:
                        pass

        try:
            if proc:
                await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass


codex_job_manager = CodexJobManager()
