#!/usr/bin/env python3
"""Create and inspect isolated Codex homes without exposing their secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}
VERBOSITY_VALUES = {"inherit", "low", "medium", "high"}
AUTH_TYPES = {"api_key", "chatgpt"}
SECRET_ENV_NAME = "WXAUTOX_CODEX_PROFILE_API_KEY"


class ProfileError(ValueError):
    pass


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _safe_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            os.chmod(path, 0o700)
    except OSError as exc:
        raise ProfileError(f"无法保护目录 {path}: {exc}") from exc


def _required(payload: dict[str, Any], key: str, label: str, limit: int) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ProfileError(f"{label}不能为空")
    if len(value) > limit or "\x00" in value:
        raise ProfileError(f"{label}格式不正确")
    return value


def _validate(payload: dict[str, Any]) -> dict[str, Any]:
    name = _required(payload, "name", "Profile 名称", 48)
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ProfileError("Profile 名称只能包含字母、数字、连字符和下划线，且必须以字母或数字开头")
    model = _required(payload, "model", "模型 ID", 128)
    if not MODEL_ID_RE.fullmatch(model):
        raise ProfileError("模型 ID 格式不正确")
    auth_type = str(payload.get("auth_type") or "api_key").strip().lower()
    if auth_type not in AUTH_TYPES:
        raise ProfileError("登录方式必须是 api_key 或 chatgpt")

    base_url = str(payload.get("base_url") or "").strip()
    api_key = str(payload.get("api_key") or "")
    if auth_type == "api_key":
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or any(character.isspace() for character in base_url)
        ):
            raise ProfileError("API Base URL 必须是有效的 http(s) 地址，且不能包含账号密码")
        if not api_key or len(api_key) > 4096 or any(char in api_key for char in "\r\n\x00"):
            raise ProfileError("API Key 不能为空，且不能包含换行符")
    else:
        base_url, api_key = "", ""

    default_provider = "ChatGPT 官方登录" if auth_type == "chatgpt" else "OpenAI Responses compatible"
    provider_name = str(payload.get("provider_name") or default_provider).strip()
    if not provider_name or len(provider_name) > 100 or "\x00" in provider_name:
        raise ProfileError("供应商名称格式不正确")
    effort = str(payload.get("reasoning_effort") or "high").strip().lower()
    if effort not in REASONING_EFFORTS:
        raise ProfileError("推理强度格式不正确")
    verbosity = str(payload.get("model_verbosity") or "inherit").strip().lower()
    if verbosity not in VERBOSITY_VALUES:
        raise ProfileError("输出详细度格式不正确")
    try:
        context_window = int(payload.get("context_window") or 128000)
    except (TypeError, ValueError) as exc:
        raise ProfileError("上下文窗口必须是整数") from exc
    if not 4096 <= context_window <= 10_000_000:
        raise ProfileError("上下文窗口必须在 4096 到 10000000 之间")
    codex_path = Path(_required(payload, "codex_bin", "Codex 路径", 1000)).expanduser()
    if not codex_path.is_absolute() or not codex_path.is_file() or not os.access(codex_path, os.X_OK):
        raise ProfileError("Codex 路径必须是当前 Linux/WSL 用户可执行的绝对路径")
    return {
        "name": name,
        "auth_type": auth_type,
        "model": model,
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "provider_name": provider_name,
        "reasoning_effort": effort,
        "model_verbosity": verbosity,
        "context_window": context_window,
        "codex_bin": str(codex_path),
    }


def _paths(home: Path, name: str) -> dict[str, Path]:
    metadata = home / ".codex" / "wxautox-profiles"
    codex_home = metadata / name
    return {
        "metadata": metadata,
        "codex_home": codex_home,
        "config": codex_home / "config.toml",
        "catalog": codex_home / "model-catalog.json",
        "secret": codex_home / "api_key",
        "auth": codex_home / "auth.json",
        "manifest": metadata / f"{name}.json",
        "wrapper": home / ".local" / "bin" / f"codex-profile-{name}",
    }


def _model_catalog(profile: dict[str, Any]) -> dict[str, Any]:
    model = profile["model"]
    window = int(profile["context_window"])
    verbosity = profile["model_verbosity"]
    return {
        "models": [
            {
                "slug": model,
                "display_name": model,
                "description": f"{model} through {profile['provider_name']}",
                "base_instructions": "You are Codex, a coding agent. Inspect relevant files, preserve unrelated changes, and verify completed work.",
                "default_reasoning_level": profile["reasoning_effort"],
                "supported_reasoning_levels": [
                    {"effort": item, "description": f"{item} reasoning"}
                    for item in ("minimal", "low", "medium", "high", "xhigh")
                ],
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 1,
                "additional_speed_tiers": [],
                "service_tiers": [],
                "availability_nux": None,
                "upgrade": None,
                "include_skills_usage_instructions": False,
                "include_plugin_usage_instructions": True,
                "include_apps_usage_instructions": True,
                "default_reasoning_summary": "none",
                "support_verbosity": verbosity != "inherit",
                "default_verbosity": None if verbosity == "inherit" else verbosity,
                "web_search_tool_type": "text",
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "supports_image_detail_original": False,
                "context_window": window,
                "max_context_window": window,
                "effective_context_window_percent": 95,
                "experimental_supported_tools": [],
                "input_modalities": ["text"],
                "supports_search_tool": False,
                "use_responses_lite": False,
                "node_repl_auto_review_required": False,
                "node_repl_disabled": False,
            }
        ]
    }


def _render_config(profile: dict[str, Any], paths: dict[str, Path]) -> str:
    toml = lambda value: json.dumps(str(value), ensure_ascii=False)
    lines = [
        "# Managed by wxautox4 in an isolated CODEX_HOME.",
        f"model = {toml(profile['model'])}",
        f"model_reasoning_effort = {toml(profile['reasoning_effort'])}",
        'model_reasoning_summary = "none"',
    ]
    if profile["model_verbosity"] != "inherit":
        lines.append(f"model_verbosity = {toml(profile['model_verbosity'])}")
    if profile["auth_type"] == "chatgpt":
        lines.extend(['forced_login_method = "chatgpt"', 'cli_auth_credentials_store = "file"', ""])
        return "\n".join(lines)
    provider_id = "wxautox_" + re.sub(r"[^A-Za-z0-9_-]", "_", profile["name"])
    lines[0] += " The API key is stored in a separate 0600 file."
    lines[2:2] = [
        f"model_provider = {toml(provider_id)}",
        f"model_catalog_json = {toml(str(paths['catalog']))}",
        f"model_context_window = {profile['context_window']}",
    ]
    lines.extend(
        [
            "",
            f"[model_providers.{provider_id}]",
            f"name = {toml(profile['provider_name'])}",
            f"base_url = {toml(profile['base_url'])}",
            f"env_key = {toml(SECRET_ENV_NAME)}",
            'wire_api = "responses"',
            "requires_openai_auth = false",
            "",
        ]
    )
    return "\n".join(lines)


def _render_wrapper(profile: dict[str, Any], paths: dict[str, Path]) -> str:
    common = f'''#!/usr/bin/env bash
set -euo pipefail
readonly WXAUTOX_PROFILE_CODEX_HOME={shlex.quote(str(paths["codex_home"]))}
readonly WXAUTOX_PROFILE_CODEX_BIN={shlex.quote(profile["codex_bin"])}
export CODEX_HOME="${{WXAUTOX_PROFILE_CODEX_HOME}}"
export CODEX_SQLITE_HOME="${{WXAUTOX_PROFILE_CODEX_HOME}}"
'''
    if profile["auth_type"] == "chatgpt":
        return common + '''unset CODEX_ACCESS_TOKEN CODEX_API_KEY OPENAI_API_KEY WXAUTOX_CODEX_PROFILE_API_KEY
exec "${WXAUTOX_PROFILE_CODEX_BIN}" "$@"
'''
    return common + f'''readonly WXAUTOX_PROFILE_KEY_FILE={shlex.quote(str(paths["secret"]))}
profile_api_key=""
if [[ -s "${{WXAUTOX_PROFILE_KEY_FILE}}" ]]; then
    IFS= read -r profile_api_key < "${{WXAUTOX_PROFILE_KEY_FILE}}" || true
    profile_api_key="${{profile_api_key%$'\\r'}}"
fi
if [[ -z "${{profile_api_key}}" ]]; then
    printf 'Codex Profile API Key 不存在或为空：%s\\n' "${{WXAUTOX_PROFILE_KEY_FILE}}" >&2
    exit 1
fi
export {SECRET_ENV_NAME}="${{profile_api_key}}"
unset profile_api_key CODEX_ACCESS_TOKEN CODEX_API_KEY OPENAI_API_KEY
exec "${{WXAUTOX_PROFILE_CODEX_BIN}}" "$@"
'''


def create_profile(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    profile = _validate(payload)
    paths = _paths(home.resolve(), profile["name"])
    if paths["codex_home"].exists() or paths["manifest"].exists() or paths["wrapper"].exists():
        raise ProfileError(f"Profile 已存在或文件名冲突：{profile['name']}")
    for key in ("codex_home", "metadata"):
        _safe_directory(paths[key])
    _safe_directory(paths["wrapper"].parent)
    public = {
        "name": profile["name"],
        "auth_type": profile["auth_type"],
        "model": profile["model"],
        "provider_name": profile["provider_name"],
        "base_url": profile["base_url"],
        "reasoning_effort": profile["reasoning_effort"],
        "model_verbosity": profile["model_verbosity"],
        "context_window": profile["context_window"],
        "wire_api": "chatgpt" if profile["auth_type"] == "chatgpt" else "responses",
        "codex_bin": profile["codex_bin"],
        "wrapper_path": str(paths["wrapper"]),
        "config_path": str(paths["config"]),
        "codex_home": str(paths["codex_home"]),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    written: list[Path] = []
    try:
        if profile["auth_type"] == "api_key":
            _atomic_write(paths["secret"], profile["api_key"] + "\n", 0o600)
            written.append(paths["secret"])
            _atomic_write(paths["catalog"], json.dumps(_model_catalog(profile), ensure_ascii=False, indent=2) + "\n", 0o600)
            written.append(paths["catalog"])
        _atomic_write(paths["config"], _render_config(profile, paths), 0o600)
        written.append(paths["config"])
        _atomic_write(paths["wrapper"], _render_wrapper(profile, paths), 0o700)
        written.append(paths["wrapper"])
        _atomic_write(paths["manifest"], json.dumps(public, ensure_ascii=False, indent=2) + "\n", 0o600)
        written.append(paths["manifest"])
    except Exception:
        for path in reversed(written):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return {
        **public,
        "key_configured": profile["auth_type"] == "api_key",
        "auth_configured": False,
        "available": profile["auth_type"] == "api_key",
    }


def update_profile(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    name = _required(payload, "name", "Profile 名称", 48)
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ProfileError("Profile 名称格式不正确")
    paths = _paths(home.resolve(), name)
    try:
        profile = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ProfileError("Codex Profile 不存在或清单已损坏") from exc
    if "model" in payload:
        model = _required(payload, "model", "模型 ID", 128)
        if not MODEL_ID_RE.fullmatch(model):
            raise ProfileError("模型 ID 格式不正确")
        profile["model"] = model
    if "reasoning_effort" in payload:
        effort = str(payload.get("reasoning_effort") or "").strip().lower()
        if effort not in REASONING_EFFORTS:
            raise ProfileError("推理强度格式不正确")
        profile["reasoning_effort"] = effort
    if "context_window" in payload:
        try:
            value = int(payload["context_window"])
        except (TypeError, ValueError) as exc:
            raise ProfileError("上下文窗口必须是整数") from exc
        if not 4096 <= value <= 10_000_000:
            raise ProfileError("上下文窗口必须在 4096 到 10000000 之间")
        profile["context_window"] = value
    _atomic_write(paths["config"], _render_config(profile, paths), 0o600)
    if profile.get("auth_type") == "api_key":
        _atomic_write(paths["catalog"], json.dumps(_model_catalog(profile), ensure_ascii=False, indent=2) + "\n", 0o600)
    _atomic_write(paths["manifest"], json.dumps(profile, ensure_ascii=False, indent=2) + "\n", 0o600)
    return profile


def list_profiles(*, home: Path) -> dict[str, Any]:
    metadata = home.resolve() / ".codex" / "wxautox-profiles"
    profiles: list[dict[str, Any]] = []
    try:
        manifests = sorted(metadata.glob("*.json"))
    except OSError:
        manifests = []
    for manifest in manifests:
        if manifest.is_symlink():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        name = str(data.get("name") or "") if isinstance(data, dict) else ""
        if not PROFILE_NAME_RE.fullmatch(name):
            continue
        expected = _paths(home.resolve(), name)
        if manifest != expected["manifest"]:
            continue
        auth_type = str(data.get("auth_type") or "api_key")
        key_ready = expected["secret"].is_file() and expected["secret"].stat().st_size > 0
        auth_ready = expected["auth"].is_file() and expected["auth"].stat().st_size > 0
        credentials_ready = auth_ready if auth_type == "chatgpt" else key_ready
        profiles.append(
            {
                **{key: value for key, value in data.items() if key != "api_key"},
                "auth_type": auth_type,
                "key_configured": key_ready,
                "auth_configured": auth_ready,
                "available": expected["wrapper"].is_file()
                and os.access(expected["wrapper"], os.X_OK)
                and expected["config"].is_file()
                and credentials_ready,
            }
        )
    return {"codex_home": str(metadata), "profiles": profiles}


def _request() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (ValueError, TypeError) as exc:
        raise ProfileError("请求体必须是 JSON 对象") from exc
    if not isinstance(value, dict):
        raise ProfileError("请求体必须是 JSON 对象")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("list", "create", "update"), required=True)
    parser.add_argument("--home", help=argparse.SUPPRESS)
    options = parser.parse_args()
    home = Path(options.home).expanduser() if options.home else Path.home()
    try:
        if options.action == "list":
            result = list_profiles(home=home)
        elif options.action == "create":
            result = create_profile(_request(), home=home)
        else:
            result = update_profile(_request(), home=home)
        print(json.dumps({"status": "success", "data": result}, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (ProfileError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
