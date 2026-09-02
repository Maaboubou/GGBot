"""Managed updates for the yt-dlp module used by Summary Plus."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Any, Dict, Optional

import requests
from packaging.version import InvalidVersion, Version

from app.utils.subprocess_utils import hidden_process_kwargs


logger = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalized_version(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        return str(Version(raw))
    except InvalidVersion as exc:
        raise YtDlpUpdateError(f"无法识别 yt-dlp 版本：{raw or '空值'}") from exc


def _is_newer(candidate: str, current: str) -> bool:
    try:
        return Version(candidate) > Version(current)
    except InvalidVersion:
        return False


class YtDlpUpdateError(RuntimeError):
    """A user-safe failure raised by the managed yt-dlp updater."""


class YtDlpUpdateService:
    """Update only yt-dlp in the Python environment that launches Mabobot."""

    def __init__(self, *, python_executable: Optional[str] = None) -> None:
        self.python_executable = str(python_executable or sys.executable)
        self._lock = threading.RLock()
        self._worker: Optional[threading.Thread] = None
        self._operation: Optional[Dict[str, Any]] = None
        self._available_version = ""
        self._checked_at = ""

    @staticmethod
    def _installed_version() -> str:
        try:
            return package_version("yt-dlp")
        except PackageNotFoundError as exc:
            raise YtDlpUpdateError("当前 Python 环境未安装 yt-dlp") from exc

    @staticmethod
    def _timeout_seconds() -> int:
        try:
            configured = int(os.getenv("YTDLP_UPDATE_TIMEOUT_SECONDS", "900"))
        except (TypeError, ValueError):
            configured = 900
        return max(120, min(configured, 3600))

    def command(self) -> list[str]:
        """Return a relocation-safe yt-dlp invocation for this Python environment."""
        return [self.python_executable, "-m", "yt_dlp"]

    def executable_path(self) -> str:
        """Return the display form kept for the existing system-tools API."""
        return subprocess.list2cmdline(self.command())

    @staticmethod
    def _run(args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
            **hidden_process_kwargs(),
        )

    def _install_exact(self, target_version: str, *, force: bool = False) -> None:
        target = _normalized_version(target_version)
        install_args = [
            self.python_executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--upgrade",
            "--upgrade-strategy",
            "only-if-needed",
        ]
        if force:
            install_args.extend(["--force-reinstall", "--no-deps"])
        install_args.append(f"yt-dlp=={target}")
        result = self._run(
            install_args,
            timeout=self._timeout_seconds(),
        )
        if result.returncode != 0:
            logger.error("yt-dlp pip install failed (exit %s)", result.returncode)
            raise YtDlpUpdateError(
                f"yt-dlp 安装失败（pip 退出码 {result.returncode}），请查看服务日志"
            )

    def _verify_fresh_process(self, target_version: str) -> None:
        target = _normalized_version(target_version)
        metadata_script = (
            "from importlib.metadata import version; "
            f"assert version('yt-dlp') == {target!r}, version('yt-dlp')"
        )
        metadata = self._run(
            [self.python_executable, "-c", metadata_script],
            timeout=min(self._timeout_seconds(), 180),
        )
        if metadata.returncode != 0:
            raise YtDlpUpdateError("新版 yt-dlp 未通过独立进程包版本校验")

        version_result = self._run(
            [*self.command(), "--version"],
            timeout=min(self._timeout_seconds(), 60),
        )
        reported = str(version_result.stdout or "").strip().splitlines()
        reported_version = reported[-1].strip() if reported else ""
        if version_result.returncode != 0 or _normalized_version(reported_version) != target:
            raise YtDlpUpdateError(
                f"yt-dlp 模块版本校验失败：期望 {target}，实际 {reported_version or '未知'}"
            )

    def check_latest(self, *, allow_during_update: bool = False) -> Dict[str, Any]:
        with self._lock:
            if not allow_during_update and self._worker and self._worker.is_alive():
                raise YtDlpUpdateError("yt-dlp 更新正在进行，暂时不能重复检查")
        try:
            response = requests.get(PYPI_URL, timeout=(5, 20))
            response.raise_for_status()
            latest = _normalized_version((response.json().get("info") or {}).get("version"))
        except YtDlpUpdateError:
            raise
        except Exception as exc:
            logger.warning("Unable to check the latest yt-dlp release: %s", exc)
            raise YtDlpUpdateError("无法连接 PyPI 检查 yt-dlp 版本，请检查网络或代理设置") from exc

        installed = self._installed_version()
        with self._lock:
            self._available_version = latest
            self._checked_at = _now_iso()
        return {
            "installed_version": installed,
            "available_version": latest,
            "update_available": _is_newer(latest, installed),
            "checked_at": self._checked_at,
        }

    def _set_stage(self, stage: str, progress: int, message: str) -> None:
        with self._lock:
            if self._operation:
                self._operation.update(
                    status="running",
                    stage=stage,
                    progress=max(0, min(int(progress), 100)),
                    message=message,
                    updated_at=_now_iso(),
                )

    def _complete(self, status: str, message: str, installed_version: str) -> None:
        with self._lock:
            if self._operation:
                self._operation.update(
                    status=status,
                    stage="complete" if status == "succeeded" else "failed",
                    progress=100,
                    message=message,
                    installed_version=installed_version,
                    finished_at=_now_iso(),
                    updated_at=_now_iso(),
                )

    def _record_audit(self, *, status: str, before: str, after: str, summary: str) -> None:
        try:
            from app.services.runtime_operations import get_runtime_operation_service

            get_runtime_operation_service().record_audit(
                category="tool_update",
                action="update_ytdlp",
                target="yt-dlp",
                status=status,
                summary=summary,
                before={"version": before},
                after={"version": after},
            )
        except Exception:
            logger.debug("Unable to record yt-dlp update audit", exc_info=True)

    def _run_update(self) -> None:
        previous_version = ""
        install_attempted = False
        try:
            self._set_stage("check", 10, "检查 PyPI 最新稳定版本")
            latest = self.check_latest(allow_during_update=True)
            previous_version = str(latest["installed_version"])
            target_version = str(latest["available_version"])
            with self._lock:
                if self._operation:
                    self._operation.update(
                        previous_version=previous_version,
                        target_version=target_version,
                    )

            if not latest["update_available"]:
                try:
                    self._verify_fresh_process(previous_version)
                    self._complete("succeeded", "当前已是最新版本且模块可用", previous_version)
                    return
                except Exception:
                    self._set_stage("install", 35, f"重新安装 yt-dlp {previous_version}")
                    install_attempted = True
                    self._install_exact(previous_version, force=True)
                    self._set_stage("verify", 82, "验证修复后的 yt-dlp 模块")
                    self._verify_fresh_process(previous_version)
                    self._complete(
                        "succeeded",
                        f"yt-dlp {previous_version} 模块调用已修复并立即生效",
                        previous_version,
                    )
                    self._record_audit(
                        status="success",
                        before=previous_version,
                        after=previous_version,
                        summary=f"yt-dlp {previous_version} 模块调用已修复",
                    )
                    return

            self._set_stage("install", 35, f"安装 yt-dlp {target_version}")
            install_attempted = True
            self._install_exact(target_version)

            self._set_stage("verify", 82, "验证 yt-dlp 模块与包版本")
            installed = self._installed_version()
            if _normalized_version(installed) != _normalized_version(target_version):
                raise YtDlpUpdateError(
                    f"安装后版本不一致：期望 {target_version}，实际 {installed}"
                )
            self._verify_fresh_process(target_version)
            self._complete("succeeded", f"yt-dlp {target_version} 已安装并立即生效", installed)
            self._record_audit(
                status="success",
                before=previous_version,
                after=installed,
                summary=f"yt-dlp 已升级到 {installed}",
            )
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            installed = ""
            if install_attempted and previous_version:
                try:
                    self._set_stage("rollback", 90, f"验证失败，恢复 yt-dlp {previous_version}")
                    self._install_exact(previous_version)
                    self._verify_fresh_process(previous_version)
                    installed = self._installed_version()
                    error += f"；已自动恢复 {previous_version}"
                except Exception as rollback_exc:
                    logger.exception("yt-dlp automatic rollback failed")
                    error += f"；自动恢复失败：{rollback_exc}"
            if not installed:
                try:
                    installed = self._installed_version()
                except YtDlpUpdateError:
                    installed = "unknown"
            self._complete("failed", error, installed)
            self._record_audit(
                status="failed",
                before=previous_version,
                after=installed,
                summary=error,
            )
            logger.exception("yt-dlp update operation failed")

    def start_update(self) -> Dict[str, Any]:
        with self._lock:
            if self._worker and self._worker.is_alive():
                raise YtDlpUpdateError("已有 yt-dlp 更新任务正在运行")
            installed = self._installed_version()
            now = _now_iso()
            self._operation = {
                "operation_id": uuid.uuid4().hex,
                "status": "queued",
                "stage": "queued",
                "progress": 0,
                "message": "等待执行",
                "started_at": now,
                "updated_at": now,
                "previous_version": installed,
                "target_version": self._available_version or None,
            }
            self._worker = threading.Thread(
                target=self._run_update,
                name="ytdlp-update",
                daemon=True,
            )
            self._worker.start()
            return dict(self._operation)

    def status(self) -> Dict[str, Any]:
        installed = self._installed_version()
        executable = self.executable_path()
        with self._lock:
            operation = dict(self._operation) if self._operation else None
            running = bool(self._worker and self._worker.is_alive())
            available = self._available_version
        return {
            "installed_version": installed,
            "available_version": available or None,
            "update_available": bool(available and _is_newer(available, installed)),
            "checked_at": self._checked_at or None,
            "operation_running": running,
            "operation": operation,
            "restart_required": False,
            "executable": executable or None,
            "environment_label": (
                "当前虚拟环境"
                if sys.prefix != getattr(sys, "base_prefix", sys.prefix)
                else "当前 Python 环境"
            ),
        }


_service_lock = threading.Lock()
_service: Optional[YtDlpUpdateService] = None


def get_ytdlp_update_service() -> YtDlpUpdateService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = YtDlpUpdateService()
    return _service
