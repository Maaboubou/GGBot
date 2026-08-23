"""Single registry for conversational-memory model routes and task contracts.

Users choose a small number of route profiles.  Internal operations keep their
own identifiers for prompts, schemas, telemetry, timeouts, and output limits.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Mapping, Optional


MEMORY_ROUTE_GENERATE = "memory_generate"
MEMORY_ROUTE_REVIEW = "memory_review"
MEMORY_ROUTE_SYNTHESIZE = "memory_synthesize"

MEMORY_ROUTE_PROFILES = (
    MEMORY_ROUTE_GENERATE,
    MEMORY_ROUTE_REVIEW,
    MEMORY_ROUTE_SYNTHESIZE,
)

MEMORY_TASKS: Dict[str, Dict[str, Any]] = {
    "memory_event_extract": {
        "profile": MEMORY_ROUTE_GENERATE,
        "temperature": 0.2,
        "max_tokens": 4000,
        "timeout": 120,
    },
    "memory_event_review": {
        "profile": MEMORY_ROUTE_REVIEW,
        "temperature": 0.0,
        "max_tokens": 2000,
        "timeout": 60,
    },
    "memory_event_relation": {
        "profile": MEMORY_ROUTE_REVIEW,
        "temperature": 0.0,
        "max_tokens": 1000,
        "timeout": 45,
    },
    "memory_stage_summarize": {
        "profile": MEMORY_ROUTE_SYNTHESIZE,
        "temperature": 0.2,
        "max_tokens": 4000,
        "timeout": 180,
    },
    "memory_person_extract": {
        "profile": MEMORY_ROUTE_GENERATE,
        "temperature": 0.0,
        "max_tokens": 8000,
        "timeout": 180,
    },
    "memory_person_review": {
        "profile": MEMORY_ROUTE_REVIEW,
        "temperature": 0.0,
        "max_tokens": 8000,
        "timeout": 180,
    },
    "memory_person_projection_review": {
        "profile": MEMORY_ROUTE_REVIEW,
        "temperature": 0.0,
        "max_tokens": 8000,
        "timeout": 180,
    },
    "memory_person_period_summarize": {
        "profile": MEMORY_ROUTE_SYNTHESIZE,
        "temperature": 0.1,
        "max_tokens": 5000,
        "timeout": 180,
    },
    "memory_person_consolidate": {
        "profile": MEMORY_ROUTE_SYNTHESIZE,
        "temperature": 0.1,
        "max_tokens": 7000,
        "timeout": 240,
    },
    "memory_person_alias_discover": {
        "profile": MEMORY_ROUTE_GENERATE,
        "temperature": 0.0,
        "max_tokens": 5000,
        "timeout": 180,
    },
    "memory_person_alias_review": {
        "profile": MEMORY_ROUTE_REVIEW,
        "temperature": 0.0,
        "max_tokens": 1200,
        "timeout": 180,
    },
}

# Upgrade-only aliases. They are removed from persisted mappings after the
# profile migration has completed.
UPGRADE_MEMORY_TASKS = {
    "memory_event": ("memory_event_extract",),
    "memory_verify": ("memory_event_review",),
    "memory_dedup": ("memory_event_relation",),
    "memory_stage": ("memory_stage_summarize",),
    "memory_person_observe": (
        "memory_person_extract",
        "memory_person_review",
        "memory_person_projection_review",
    ),
    "memory_person_period": ("memory_person_period_summarize",),
    "memory_person_consolidate": ("memory_person_consolidate",),
}

PROFILE_UPGRADE_SOURCES = {
    MEMORY_ROUTE_GENERATE: ("memory_event", "memory_person_observe"),
    MEMORY_ROUTE_REVIEW: (
        "memory_verify",
        "memory_dedup",
        "memory_person_observe",
    ),
    MEMORY_ROUTE_SYNTHESIZE: (
        "memory_person_consolidate",
        "memory_stage",
        "memory_person_period",
    ),
}

_CODE_OWNED_OVERRIDE_KEYS = {
    "temperature",
    "max_tokens",
    "timeout",
    "response_format",
}


def memory_task(call_type: str) -> Optional[Dict[str, Any]]:
    task = MEMORY_TASKS.get(str(call_type or "").strip())
    return copy.deepcopy(task) if task is not None else None


def memory_profile_for(call_type: str) -> Optional[str]:
    task = MEMORY_TASKS.get(str(call_type or "").strip())
    return str(task["profile"]) if task is not None else None


def apply_memory_task_contract(
    mapping: Mapping[str, Any],
    call_type: str,
) -> Dict[str, Any]:
    """Resolve a route mapping while keeping task execution limits code-owned."""
    resolved = copy.deepcopy(dict(mapping))
    task = MEMORY_TASKS.get(str(call_type or "").strip())
    if task is None:
        return resolved

    overrides = dict(resolved.get("override_params") or {})
    overrides.update(
        {
            "temperature": task["temperature"],
            "max_tokens": task["max_tokens"],
            "timeout": task["timeout"],
            "response_format": {"type": "json_object"},
        }
    )
    resolved["override_params"] = overrides
    return resolved


def default_memory_route_mappings(
    *,
    generate_primary: str,
    review_primary: str,
    synthesize_primary: str,
    generate_fallback: list[str],
    review_fallback: list[str],
    synthesize_fallback: list[str],
) -> Dict[str, Dict[str, Any]]:
    return {
        MEMORY_ROUTE_GENERATE: {
            "primary": generate_primary,
            "fallback": list(generate_fallback),
            "override_params": {},
        },
        MEMORY_ROUTE_REVIEW: {
            "primary": review_primary,
            "fallback": list(review_fallback),
            "override_params": {},
        },
        MEMORY_ROUTE_SYNTHESIZE: {
            "primary": synthesize_primary,
            "fallback": list(synthesize_fallback),
            "override_params": {},
        },
    }


def migrate_memory_route_mappings(
    mappings: Dict[str, Any],
    defaults: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Collapse historical task mappings into three profile mappings.

    Each profile adopts one deterministic historical route, then all former
    mapping points are removed. Execution limits remain code-owned.
    """
    changed = False
    original = copy.deepcopy(mappings)
    for profile, default in defaults.items():
        if profile not in mappings:
            selected = next(
                (
                    original[source]
                    for source in PROFILE_UPGRADE_SOURCES[profile]
                    if isinstance(original.get(source), Mapping)
                    and str(original[source].get("primary") or "").strip()
                ),
                None,
            )
            if selected is None:
                mappings[profile] = copy.deepcopy(dict(default))
            else:
                mappings[profile] = {
                    "primary": str(selected.get("primary") or "").strip(),
                    "fallback": list(selected.get("fallback") or []),
                    "override_params": {
                        key: copy.deepcopy(value)
                        for key, value in dict(
                            selected.get("override_params") or {}
                        ).items()
                        if key not in _CODE_OWNED_OVERRIDE_KEYS
                    },
                }
            changed = True

    for old_call_type in UPGRADE_MEMORY_TASKS:
        if old_call_type in mappings:
            del mappings[old_call_type]
            changed = True

    return changed


def resolve_memory_mapping(
    mappings: Mapping[str, Any],
    call_type: str,
) -> Optional[Dict[str, Any]]:
    profile = memory_profile_for(call_type)
    if profile is None:
        return None
    profile_mapping = mappings.get(profile)
    if not isinstance(profile_mapping, Mapping):
        return None
    resolved = copy.deepcopy(dict(profile_mapping))
    return apply_memory_task_contract(resolved, call_type)
