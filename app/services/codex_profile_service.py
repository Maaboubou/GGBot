"""Manage non-secret Codex Profile metadata and isolated launchers."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

from app.services.file_tools_runtime import _as_wsl_path, _default_use_wsl, get_file_tools_runtime


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_SCRIPT = PROJECT_ROOT / "scripts" / "file_tools" / "manage_codex_profiles.py"
DEFAULT_ASSISTANT_PROFILE_KEY = "ASSISTANT_CODEX_PROFILE_ID"


class CodexProfileError(RuntimeError):
    pass


def public_profile(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    allowed = {
        "name",
        "auth_type",
        "model",
        "provider_name",
        "base_url",
        "reasoning_effort",
        "model_verbosity",
        "context_window",
        "wire_api",
        "codex_bin",
        "wrapper_path",
        "config_path",
        "codex_home",
        "created_at",
        "account_email",
        "plan_type",
        "key_configured",
        "auth_configured",
        "available",
    }
    profile = {key: deepcopy(value[key]) for key in allowed if key in value}
    required = ("name", "model", "wrapper_path")
    return profile if all(str(profile.get(key) or "").strip() for key in required) else None


class CodexProfileService:
    """Bridge to a stdlib-only helper that runs in the target Linux home."""

    def __init__(self, *, use_wsl: Optional[bool] = None) -> None:
        self.use_wsl = _default_use_wsl() if use_wsl is None else bool(use_wsl)
        self._cache_lock = threading.RLock()
        self._cache_until = 0.0
        self._cached_listing: Optional[dict[str, Any]] = None

    def _command(self, action: str) -> list[str]:
        script = _as_wsl_path(PROFILE_SCRIPT) if self.use_wsl else str(PROFILE_SCRIPT)
        command = ["wsl.exe", "python3", script] if self.use_wsl else [sys.executable, script]
        return [*command, "--action", action]

    def _run(self, action: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        try:
            result = subprocess.run(
                self._command(action),
                input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CodexProfileError(f"无法访问本地 Codex Profile：{exc}") from exc
        lines = (result.stdout or "").strip().splitlines()
        if not lines:
            detail = (result.stderr or "Profile 管理脚本没有返回数据").strip()
            raise CodexProfileError(detail[-1500:])
        try:
            response = json.loads(lines[-1])
        except (ValueError, TypeError) as exc:
            raise CodexProfileError("Profile 管理脚本返回了无效数据") from exc
        if result.returncode != 0 or response.get("status") != "success":
            raise CodexProfileError(str(response.get("error") or "Codex Profile 操作失败"))
        data = response.get("data")
        if not isinstance(data, dict):
            raise CodexProfileError("Profile 管理脚本返回内容不完整")
        return data

    def list_profiles(self) -> dict[str, Any]:
        with self._cache_lock:
            cached = (
                deepcopy(self._cached_listing)
                if self._cached_listing is not None and time.monotonic() < self._cache_until
                else None
            )
        data = cached or self._run("list")
        if cached is None:
            with self._cache_lock:
                self._cached_listing = deepcopy(data)
                self._cache_until = time.monotonic() + 3.0
        profiles = [profile for item in data.get("profiles") or [] if (profile := public_profile(item))]
        default_id = self.default_profile_id()
        for profile in profiles:
            profile["is_default"] = profile.get("name") == default_id
        return {
            "codex_home": str(data.get("codex_home") or ""),
            "default_profile_id": default_id,
            "profiles": profiles,
        }

    def create_profile(self, payload: dict[str, Any], *, codex_bin: str) -> dict[str, Any]:
        request = dict(payload)
        request["codex_bin"] = str(codex_bin or "").strip()
        profile = public_profile(self._run("create", request))
        if not profile:
            raise CodexProfileError("新建 Profile 返回内容不完整")
        self.invalidate_cache()
        get_file_tools_runtime(use_wsl=self.use_wsl, force=True)
        if bool(payload.get("make_default", True)):
            self.set_default_profile(str(profile["name"]))
        return profile

    def update_profile(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        profile = public_profile(self._run("update", {**dict(payload), "name": name}))
        if not profile:
            raise CodexProfileError("更新 Profile 返回内容不完整")
        self.invalidate_cache()
        get_codex_runtime_registry().invalidate(str(profile["name"]))
        return profile

    def get_profile(self, name: str) -> Optional[dict[str, Any]]:
        normalized = str(name or "").strip()
        if not normalized:
            return None
        return next(
            (item for item in self.list_profiles().get("profiles") or [] if item.get("name") == normalized),
            None,
        )

    @staticmethod
    def default_profile_id() -> str:
        try:
            from app.services.config_service import get_setting

            return str(get_setting(DEFAULT_ASSISTANT_PROFILE_KEY, "") or "").strip()
        except Exception:
            return ""

    def set_default_profile(self, name: str) -> None:
        normalized = str(name or "").strip()
        if normalized and self.get_profile(normalized) is None:
            raise CodexProfileError("Codex Profile 不存在")
        from app.services.config_service import update_setting

        if not update_setting(DEFAULT_ASSISTANT_PROFILE_KEY, normalized):
            raise CodexProfileError("保存默认 Codex Profile 失败")

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cached_listing = None
            self._cache_until = 0.0


class CodexProfileRuntimeRegistry:
    """Keep isolated Codex process pools per Profile; never switch global state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runtimes: dict[str, tuple[str, Any]] = {}

    def resolve(self, profile_id: str = "") -> tuple[Any, Optional[dict[str, Any]]]:
        from app.services.agent_runtime import CodexAgentRuntime, get_agent_runtime

        normalized = str(profile_id or "").strip()
        if not normalized:
            return get_agent_runtime(), None
        profile = get_codex_profile_service().get_profile(normalized)
        if not profile:
            raise CodexProfileError(f"Codex Profile 不存在：{normalized}")
        if not profile.get("available"):
            raise CodexProfileError(f"Codex Profile 尚未完成登录或密钥配置：{normalized}")
        signature = "|".join(
            str(profile.get(key) or "")
            for key in ("wrapper_path", "model", "reasoning_effort", "created_at")
        )
        with self._lock:
            cached = self._runtimes.get(normalized)
            if cached and cached[0] == signature:
                return cached[1], profile
            previous = cached[1] if cached else None
            runtime = CodexAgentRuntime(codex_bin=str(profile["wrapper_path"]))
            try:
                runtime.start()
            except Exception:
                runtime.stop()
                raise
            self._runtimes[normalized] = (signature, runtime)
        if previous is not None:
            previous.stop()
        return runtime, profile

    def invalidate(self, profile_id: str) -> None:
        with self._lock:
            cached = self._runtimes.pop(str(profile_id or "").strip(), None)
        if cached:
            cached[1].stop()

    def invalidate_chat(self, chat_id: str) -> None:
        with self._lock:
            runtimes = [item[1] for item in self._runtimes.values()]
        for runtime in runtimes:
            runtime.invalidate_chat(chat_id)

    def stop_all(self) -> None:
        with self._lock:
            values = list(self._runtimes.values())
            self._runtimes.clear()
        for _signature, runtime in values:
            runtime.stop()


_profile_service: Optional[CodexProfileService] = None
_runtime_registry: Optional[CodexProfileRuntimeRegistry] = None


def get_codex_profile_service() -> CodexProfileService:
    global _profile_service
    if _profile_service is None:
        _profile_service = CodexProfileService()
    return _profile_service


def get_codex_runtime_registry() -> CodexProfileRuntimeRegistry:
    global _runtime_registry
    if _runtime_registry is None:
        _runtime_registry = CodexProfileRuntimeRegistry()
    return _runtime_registry
