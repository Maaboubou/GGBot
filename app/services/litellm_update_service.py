"""Safe, in-process management for the locally installed LiteLLM package."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Any, Dict, Optional

import requests
from packaging.version import InvalidVersion, Version


logger = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/litellm/json"
_CREDENTIAL_URL_PATTERN = re.compile(r"(https?://)([^\s/@:]+):([^\s/@]+)@", re.IGNORECASE)
try:
    # This module is imported while the Web process starts, before an updater
    # can replace files on disk. Keep that value separate from later metadata.
    _PROCESS_LITELLM_VERSION = package_version("litellm")
except PackageNotFoundError:
    _PROCESS_LITELLM_VERSION = ""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalized_version(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        return str(Version(raw))
    except InvalidVersion as exc:
        raise LiteLLMUpdateError(f"无法识别 LiteLLM 版本：{raw or '空值'}") from exc


def _is_newer(candidate: str, current: str) -> bool:
    try:
        return Version(candidate) > Version(current)
    except InvalidVersion:
        return False


def _versions_equal(left: str, right: str) -> bool:
    try:
        return Version(left) == Version(right)
    except InvalidVersion:
        return str(left or "").strip() == str(right or "").strip()


def _safe_command_detail(result: subprocess.CompletedProcess) -> str:
    raw = str(result.stderr or result.stdout or "").strip()
    redacted = _CREDENTIAL_URL_PATTERN.sub(r"\1***:***@", raw)
    return redacted[-1500:]


class LiteLLMUpdateError(RuntimeError):
    """A user-safe failure raised by the managed updater."""


class LiteLLMUpdateService:
    """Check and update only ``litellm`` in the current Python environment."""

    def __init__(
        self,
        *,
        python_executable: Optional[str] = None,
        runtime_version: Optional[str] = None,
    ) -> None:
        self.python_executable = str(python_executable or sys.executable)
        self.runtime_version = str(
            runtime_version or _PROCESS_LITELLM_VERSION or self._installed_version()
        )
        self._lock = threading.RLock()
        self._worker: Optional[threading.Thread] = None
        self._operation: Optional[Dict[str, Any]] = None
        self._available_version = ""
        self._checked_at = ""

    @staticmethod
    def _installed_version() -> str:
        try:
            return package_version("litellm")
        except PackageNotFoundError as exc:
            raise LiteLLMUpdateError("当前 Python 环境未安装 LiteLLM") from exc

    @staticmethod
    def _timeout_seconds() -> int:
        try:
            configured = int(os.getenv("LITELLM_UPDATE_TIMEOUT_SECONDS", "900"))
        except (TypeError, ValueError):
            configured = 900
        return max(120, min(configured, 3600))

    def _run(self, args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )

    def _install_exact(self, target_version: str) -> None:
        target = _normalized_version(target_version)
        result = self._run(
            [
                self.python_executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--upgrade",
                "--upgrade-strategy",
                "only-if-needed",
                f"litellm=={target}",
            ],
            timeout=self._timeout_seconds(),
        )
        if result.returncode != 0:
            detail = _safe_command_detail(result)
            logger.error("LiteLLM pip install failed (exit %s): %s", result.returncode, detail)
            raise LiteLLMUpdateError(f"LiteLLM 安装失败（pip 退出码 {result.returncode}），请查看服务日志")

    def _verify_fresh_import(self, target_version: str) -> None:
        target = _normalized_version(target_version)
        script = (
            "import litellm; "
            "from importlib.metadata import version; "
            f"assert version('litellm') == {target!r}, version('litellm')"
        )
        result = self._run(
            [self.python_executable, "-c", script],
            timeout=min(self._timeout_seconds(), 180),
        )
        if result.returncode != 0:
            detail = _safe_command_detail(result)
            logger.error("LiteLLM fresh-import verification failed: %s", detail)
            raise LiteLLMUpdateError("新版 LiteLLM 无法在独立进程中通过导入与版本校验")

    def check_latest(self, *, allow_during_update: bool = False) -> Dict[str, Any]:
        with self._lock:
            if not allow_during_update and self._worker and self._worker.is_alive():
                raise LiteLLMUpdateError("LiteLLM 更新正在进行，暂时不能重复检查")
        try:
            response = requests.get(PYPI_URL, timeout=(5, 20))
            response.raise_for_status()
            payload = response.json()
            latest = _normalized_version((payload.get("info") or {}).get("version"))
        except LiteLLMUpdateError:
            raise
        except Exception as exc:
            logger.warning("Unable to check latest LiteLLM release from PyPI: %s", exc)
            raise LiteLLMUpdateError("无法连接 PyPI 检查 LiteLLM 版本，请检查网络或代理设置") from exc

        installed = self._installed_version()
        with self._lock:
            self._available_version = latest
            self._checked_at = _now_iso()
        return {
            "installed_version": installed,
            "runtime_version": self.runtime_version,
            "available_version": latest,
            "update_available": _is_newer(latest, installed),
            "checked_at": self._checked_at,
            "restart_required": not _versions_equal(installed, self.runtime_version),
        }

    def _set_stage(self, stage: str, progress: int, message: str) -> None:
        with self._lock:
            if not self._operation:
                return
            self._operation.update(
                {
                    "status": "running",
                    "stage": stage,
                    "progress": max(0, min(int(progress), 100)),
                    "message": message,
                    "updated_at": _now_iso(),
                }
            )

    def _complete(self, status: str, message: str, installed_version: str) -> None:
        with self._lock:
            if not self._operation:
                return
            self._operation.update(
                {
                    "status": status,
                    "stage": "complete" if status == "succeeded" else "failed",
                    "progress": 100,
                    "message": message,
                    "installed_version": installed_version,
                    "finished_at": _now_iso(),
                    "updated_at": _now_iso(),
                }
            )

    def _run_update(self) -> None:
        previous_version = ""
        target_version = ""
        install_attempted = False
        try:
            self._set_stage("check", 10, "检查 PyPI 最新稳定版本")
            latest = self.check_latest(allow_during_update=True)
            previous_version = str(latest["installed_version"])
            target_version = str(latest["available_version"])
            with self._lock:
                if self._operation:
                    self._operation["previous_version"] = previous_version
                    self._operation["target_version"] = target_version

            if not latest["update_available"]:
                self._complete("succeeded", "当前已是最新版本", previous_version)
                return

            self._set_stage("install", 35, f"安装 LiteLLM {target_version}")
            install_attempted = True
            self._install_exact(target_version)

            self._set_stage("verify", 82, "在独立进程中验证新版 LiteLLM")
            installed = self._installed_version()
            if _normalized_version(installed) != _normalized_version(target_version):
                raise LiteLLMUpdateError(
                    f"安装后版本不一致：期望 {target_version}，实际 {installed}"
                )
            self._verify_fresh_import(target_version)
            self._complete(
                "succeeded",
                f"LiteLLM {target_version} 已安装，重启全部服务后生效",
                installed,
            )
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            installed = ""
            if install_attempted and previous_version:
                try:
                    self._set_stage("rollback", 90, f"验证失败，恢复 LiteLLM {previous_version}")
                    self._install_exact(previous_version)
                    self._verify_fresh_import(previous_version)
                    installed = self._installed_version()
                    error += f"；已自动恢复 {previous_version}"
                except Exception as rollback_exc:
                    logger.exception("LiteLLM automatic rollback failed")
                    try:
                        installed = self._installed_version()
                    except LiteLLMUpdateError:
                        installed = "unknown"
                    error += f"；自动恢复失败：{rollback_exc}"
            else:
                try:
                    installed = self._installed_version()
                except LiteLLMUpdateError:
                    installed = "unknown"
            self._complete("failed", error, installed)
            logger.exception("LiteLLM update operation failed")

    def start_update(self) -> Dict[str, Any]:
        with self._lock:
            if self._worker and self._worker.is_alive():
                raise LiteLLMUpdateError("已有 LiteLLM 更新任务正在运行")
            installed = self._installed_version()
            if not _versions_equal(installed, self.runtime_version):
                raise LiteLLMUpdateError(
                    "磁盘中的 LiteLLM 版本尚未由当前进程加载，请先重启全部服务"
                )
            now = _now_iso()
            self._operation = {
                "operation_id": uuid.uuid4().hex,
                "status": "queued",
                "stage": "queued",
                "progress": 0,
                "message": "等待执行",
                "started_at": now,
                "updated_at": now,
                "target_version": self._available_version or None,
            }
            self._worker = threading.Thread(
                target=self._run_update,
                name="litellm-update",
                daemon=True,
            )
            self._worker.start()
            return dict(self._operation)

    def status(self) -> Dict[str, Any]:
        installed = self._installed_version()
        with self._lock:
            operation = dict(self._operation) if self._operation else None
            running = bool(self._worker and self._worker.is_alive())
            available = self._available_version
            checked_at = self._checked_at or None
        return {
            "runtime_version": self.runtime_version,
            "installed_version": installed,
            "available_version": available or None,
            "update_available": bool(available and _is_newer(available, installed)),
            "checked_at": checked_at,
            "operation_running": running,
            "operation": operation,
            "restart_required": not _versions_equal(installed, self.runtime_version),
            "environment_label": (
                "当前虚拟环境" if sys.prefix != getattr(sys, "base_prefix", sys.prefix) else "当前 Python 环境"
            ),
        }


_service_lock = threading.Lock()
_service: Optional[LiteLLMUpdateService] = None


def get_litellm_update_service() -> LiteLLMUpdateService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = LiteLLMUpdateService()
    return _service
