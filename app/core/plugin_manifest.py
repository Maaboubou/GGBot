"""Validation and presentation helpers for WxAutoX plugin manifests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from .event_bus import EventType


PLUGIN_MANIFEST_VERSION = 2
TRIGGER_KINDS = {
    "always",
    "keyword",
    "link",
    "mention_or_judge",
    "schedule",
    "session",
    "internal",
    "dynamic",
}
PROPAGATION_MODES = {"observe", "continue", "stop_on_consumed"}
CHAT_TYPES = {"group", "user"}


class PluginManifestError(ValueError):
    """Raised when a plugin does not satisfy the manifest v2 contract."""


def _require_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PluginManifestError(f"{label} 不能为空")
    return text


def _validate_trigger(
    trigger: Any,
    *,
    label: str,
    config_schema: Mapping[str, Any],
) -> Dict[str, Any]:
    if not isinstance(trigger, dict):
        raise PluginManifestError(f"{label}.trigger 必须是对象")
    kind = _require_text(trigger.get("kind"), f"{label}.trigger.kind")
    if kind not in TRIGGER_KINDS:
        raise PluginManifestError(f"{label}.trigger.kind 不受支持: {kind}")
    summary = _require_text(trigger.get("summary"), f"{label}.trigger.summary")
    config_keys = trigger.get("config_keys", [])
    if not isinstance(config_keys, list) or any(not isinstance(key, str) for key in config_keys):
        raise PluginManifestError(f"{label}.trigger.config_keys 必须是字符串数组")
    missing_keys = [key for key in config_keys if key not in config_schema]
    if missing_keys:
        raise PluginManifestError(
            f"{label}.trigger.config_keys 未在 config_schema 声明: {', '.join(missing_keys)}"
        )
    conditions = trigger.get("conditions", [])
    if not isinstance(conditions, list) or any(not isinstance(item, str) for item in conditions):
        raise PluginManifestError(f"{label}.trigger.conditions 必须是字符串数组")
    return {
        "kind": kind,
        "summary": summary,
        "config_keys": list(dict.fromkeys(config_keys)),
        "conditions": [item.strip() for item in conditions if item.strip()],
    }


def _validate_scope(scope: Any, *, label: str, config_schema: Mapping[str, Any]) -> Dict[str, Any]:
    if scope is None:
        scope = {"level": "global", "chat_types": ["group", "user"]}
    if not isinstance(scope, dict):
        raise PluginManifestError(f"{label}.scope 必须是对象")
    if scope.get("level", "global") != "global":
        raise PluginManifestError(f"{label}.scope.level 目前只支持 global")
    chat_types = scope.get("chat_types", ["group", "user"])
    if (
        not isinstance(chat_types, list)
        or not chat_types
        or any(item not in CHAT_TYPES for item in chat_types)
    ):
        raise PluginManifestError(f"{label}.scope.chat_types 只能包含 group/user")
    chat_name_key = scope.get("chat_name_config_key")
    if chat_name_key is not None and chat_name_key not in config_schema:
        raise PluginManifestError(
            f"{label}.scope.chat_name_config_key 未在 config_schema 声明: {chat_name_key}"
        )
    return {
        "level": "global",
        "chat_types": list(dict.fromkeys(chat_types)),
        "chat_name_config_key": chat_name_key,
    }


def load_plugin_manifest(plugin_path: Path, config: Mapping[str, Any]) -> Dict[str, Any]:
    """Load and strictly validate a plugin's required manifest.json."""
    path = plugin_path / "manifest.json"
    if not path.exists():
        raise PluginManifestError("缺少 manifest.json；所有插件必须升级到 Manifest v2")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginManifestError(f"manifest.json 无法读取: {exc}") from exc
    if not isinstance(payload, dict):
        raise PluginManifestError("manifest.json 顶层必须是对象")
    if payload.get("schema_version") != PLUGIN_MANIFEST_VERSION:
        raise PluginManifestError(f"schema_version 必须为 {PLUGIN_MANIFEST_VERSION}")

    config_schema = config.get("config_schema") or {}
    if not isinstance(config_schema, dict):
        raise PluginManifestError("config_schema 必须是对象")

    raw_listeners = payload.get("listeners", [])
    if not isinstance(raw_listeners, list):
        raise PluginManifestError("listeners 必须是数组")
    listeners = []
    identities = set()
    for index, raw in enumerate(raw_listeners):
        label = f"listeners[{index}]"
        if not isinstance(raw, dict):
            raise PluginManifestError(f"{label} 必须是对象")
        event = _require_text(raw.get("event"), f"{label}.event")
        try:
            EventType(event)
        except ValueError as exc:
            raise PluginManifestError(f"{label}.event 未知: {event}") from exc
        handler = _require_text(raw.get("handler"), f"{label}.handler")
        identity = (event, handler)
        if identity in identities:
            raise PluginManifestError(f"重复的监听器声明: {event}:{handler}")
        identities.add(identity)
        propagation = raw.get("propagation", "continue")
        if propagation not in PROPAGATION_MODES:
            raise PluginManifestError(f"{label}.propagation 不受支持: {propagation}")
        listeners.append(
            {
                "event": event,
                "handler": handler,
                "title": _require_text(raw.get("title"), f"{label}.title"),
                "trigger": _validate_trigger(
                    raw.get("trigger"), label=label, config_schema=config_schema
                ),
                "scope": _validate_scope(
                    raw.get("scope"), label=label, config_schema=config_schema
                ),
                "propagation": propagation,
            }
        )

    raw_jobs = payload.get("jobs", [])
    if not isinstance(raw_jobs, list):
        raise PluginManifestError("jobs 必须是数组")
    jobs = []
    job_ids = set()
    for index, raw in enumerate(raw_jobs):
        label = f"jobs[{index}]"
        if not isinstance(raw, dict):
            raise PluginManifestError(f"{label} 必须是对象")
        job_id = _require_text(raw.get("id"), f"{label}.id")
        if job_id in job_ids:
            raise PluginManifestError(f"重复的任务 id: {job_id}")
        job_ids.add(job_id)
        jobs.append(
            {
                "id": job_id,
                "title": _require_text(raw.get("title"), f"{label}.title"),
                "trigger": _validate_trigger(
                    raw.get("trigger"), label=label, config_schema=config_schema
                ),
                "scope": _validate_scope(
                    raw.get("scope"), label=label, config_schema=config_schema
                ),
            }
        )
    return {"schema_version": PLUGIN_MANIFEST_VERSION, "listeners": listeners, "jobs": jobs}


def listener_manifest_map(manifest: Mapping[str, Any]) -> Dict[tuple[str, str], Dict[str, Any]]:
    return {
        (str(item["event"]), str(item["handler"])): dict(item)
        for item in manifest.get("listeners", [])
    }


def config_value(config: Mapping[str, Any], key: str) -> Any:
    if key in config:
        return config[key]
    values = config.get("config")
    if isinstance(values, dict) and key in values:
        return values[key]
    schema = config.get("config_schema")
    if isinstance(schema, dict) and isinstance(schema.get(key), dict):
        return schema[key].get("default")
    return None


def describe_listener_trigger(spec: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
    """Resolve editable trigger values without duplicating matching logic in the UI."""
    trigger = spec.get("trigger") or {}
    schema = config.get("config_schema") or {}
    editable = []
    for key in trigger.get("config_keys", []):
        field = schema.get(key) or {}
        editable.append(
            {
                "key": key,
                "title": str(field.get("title") or field.get("description") or key),
                "type": str(field.get("type") or "string"),
                "value": config_value(config, key),
            }
        )
    return {
        "kind": trigger.get("kind", "dynamic"),
        "summary": str(trigger.get("summary") or "由插件运行时判断"),
        "conditions": list(trigger.get("conditions") or []),
        "editable": editable,
        "editable_count": len(editable),
    }


def validate_registered_listeners(
    manifest: Mapping[str, Any], registered: Iterable[tuple[str, str]]
) -> None:
    declared = set(listener_manifest_map(manifest))
    actual = set(registered)
    missing = sorted(declared - actual)
    extra = sorted(actual - declared)
    if missing or extra:
        details = []
        if missing:
            details.append("未注册=" + ", ".join(f"{e}:{h}" for e, h in missing))
        if extra:
            details.append("未声明=" + ", ".join(f"{e}:{h}" for e, h in extra))
        raise PluginManifestError("监听器与 manifest.json 不一致（" + "；".join(details) + "）")
