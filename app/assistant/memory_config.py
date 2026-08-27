"""Core configuration helpers for Assistant conversational memory."""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping

from app.utils.plugin_config import get_plugin_config


# Kept only at the input boundary so existing installations and per-chat
# overrides upgrade without losing user choices.  Runtime code uses new keys.
LEGACY_MEMORY_CONFIG_ALIASES = {
    "memory_person_v3_enabled": "memory_person_enabled",
    "memory_person_v3_auto_activate_live": "memory_person_auto_activate_live",
    "memory_person_v3_person_centric_enabled": "memory_person_centric_enabled",
    "memory_person_v3_min_pending_messages": "memory_person_min_pending_messages",
    "memory_person_v3_batch_related_messages": "memory_person_batch_related_messages",
    "memory_person_v3_max_batch_people": "memory_person_max_batch_people",
    "memory_person_v3_max_observations_per_batch": "memory_person_max_observations_per_batch",
    "memory_person_v3_input_token_budget": "memory_person_input_token_budget",
    "memory_person_v3_candidate_memory_value": "memory_person_candidate_memory_value",
    "memory_person_v3_refresh_threshold": "memory_person_refresh_threshold",
    "memory_person_v3_max_refresh_people": "memory_person_max_refresh_people",
    "memory_person_v3_retrieval_max_people": "memory_person_retrieval_max_people",
    "memory_person_v3_retrieval_max_items": "memory_person_retrieval_max_items",
    "memory_person_v3_include_high_sensitivity": "memory_person_include_high_sensitivity",
}


def memory_schema() -> Dict[str, Dict[str, Any]]:
    manifest = get_plugin_config("assistant")
    schema = manifest.get("config_schema") or {}
    return {
        str(key): dict(definition)
        for key, definition in schema.items()
        if str(key).startswith("memory_") and isinstance(definition, Mapping)
    }


def memory_config_defaults() -> Dict[str, Any]:
    manifest = get_plugin_config("assistant")
    schema = memory_schema()
    configured = manifest.get("config") or {}
    return {
        key: configured.get(key, definition.get("default"))
        for key, definition in schema.items()
    }


def upgrade_memory_config_keys(values: Mapping[str, Any]) -> Dict[str, Any]:
    upgraded = dict(values or {})
    for old_key, new_key in LEGACY_MEMORY_CONFIG_ALIASES.items():
        if new_key not in upgraded and old_key in upgraded:
            upgraded[new_key] = upgraded[old_key]
        upgraded.pop(old_key, None)
    # The person-centric evidence pipeline is now the only implementation.
    upgraded.pop("memory_person_centric_enabled", None)
    upgraded.pop("memory_person_auto_activate_live", None)
    return upgraded


def load_memory_config(
    getter: Callable[[str, Any], Any],
) -> Dict[str, Any]:
    """Load manifest-declared memory values with one-release key migration."""
    defaults = memory_config_defaults()
    reverse_aliases = {
        new_key: old_key for old_key, new_key in LEGACY_MEMORY_CONFIG_ALIASES.items()
    }
    values: Dict[str, Any] = {}
    for key, default in defaults.items():
        value = getter(key, None)
        if value is None and key in reverse_aliases:
            value = getter(reverse_aliases[key], None)
        values[key] = default if value is None else value
    return sanitize_memory_config(values, defaults=defaults)


def sanitize_memory_config(
    values: Mapping[str, Any],
    *,
    defaults: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    schema = memory_schema()
    base = dict(defaults or memory_config_defaults())
    incoming = upgrade_memory_config_keys(values)
    result: Dict[str, Any] = {}

    for key, definition in schema.items():
        value = incoming.get(key, base.get(key, definition.get("default")))
        value_type = definition.get("type")
        try:
            if value_type == "boolean":
                value = bool(value)
            elif value_type == "integer":
                value = int(value)
            elif value_type == "number":
                value = float(value)
            elif value_type == "string":
                value = str(value or "").strip()
        except (TypeError, ValueError, OverflowError):
            value = base.get(key, definition.get("default"))

        minimum = definition.get("minimum")
        maximum = definition.get("maximum")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if minimum is not None:
                value = max(value, minimum)
            if maximum is not None:
                value = min(value, maximum)
        result[key] = value

    result["memory_event_target_messages"] = max(
        int(result["memory_event_min_messages"]),
        int(result["memory_event_target_messages"]),
    )
    result["memory_event_max_messages"] = max(
        int(result["memory_event_target_messages"]),
        int(result["memory_event_max_messages"]),
    )
    result["memory_stage_input_event_limit"] = max(
        int(result["memory_stage_event_threshold"]),
        int(result["memory_stage_input_event_limit"]),
    )
    result["memory_duplicate_similarity_threshold"] = max(
        float(result["memory_dedup_candidate_threshold"]),
        float(result["memory_duplicate_similarity_threshold"]),
    )
    return result
