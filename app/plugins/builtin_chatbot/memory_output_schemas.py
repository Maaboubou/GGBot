from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional


JsonSchema = Dict[str, Any]


def _string(*values: str) -> JsonSchema:
    schema: JsonSchema = {"type": "string"}
    if values:
        schema["enum"] = list(values)
    return schema


def _number() -> JsonSchema:
    return {"type": "number"}


def _integer() -> JsonSchema:
    return {"type": "integer"}


def _array(items: JsonSchema) -> JsonSchema:
    return {"type": "array", "items": items}


def _object(properties: Mapping[str, JsonSchema]) -> JsonSchema:
    copied = dict(properties)
    return {
        "type": "object",
        "properties": copied,
        "required": list(copied),
        "additionalProperties": False,
    }


def _string_array() -> JsonSchema:
    return _array(_string())


def _integer_array() -> JsonSchema:
    return _array(_integer())


def _event_schema() -> JsonSchema:
    claim = _object(
        {
            "text": _string(),
            "subject": _string(),
            "speaker": _string(),
            "claim_evidence_cursors": _integer_array(),
            "subject_evidence_cursors": _integer_array(),
        }
    )
    opinion = _object({"person": _string(), "view": _string()})
    event = _object(
        {
            "title": _string(),
            "summary": _string(),
            "anchor_cursor": _integer(),
            "source_start_cursor": _integer(),
            "source_end_cursor": _integer(),
            "participants": _string_array(),
            "keywords": _string_array(),
            "opinions": _array(opinion),
            "claims": _array(claim),
            "decisions": _string_array(),
            "open_items": _string_array(),
            "event_type": _string(
                "personal_update",
                "group_decision",
                "question",
                "debate",
                "shared_info",
                "joke",
                "other",
            ),
            "certainty": _string(
                "confirmed_in_chat",
                "self_report",
                "attributed_claim",
                "unverified_external",
                "rumor_or_joke",
            ),
            "source_note": _string(),
            "importance": _number(),
        }
    )
    return _object({"events": _array(event)})


def _event_verify_schema() -> JsonSchema:
    decision = _object(
        {
            "candidate_index": _integer(),
            "action": _string("accept", "quarantine"),
            "reason": _string(),
        }
    )
    return _object({"decisions": _array(decision)})


def _event_dedup_schema() -> JsonSchema:
    decision = _object(
        {
            "candidate_index": _integer(),
            "action": _string(
                "keep",
                "skip_duplicate",
                "keep_update",
                "quarantine_conflict",
            ),
            "related_event_id": _integer(),
            "related_candidate_index": _integer(),
            "consolidated_title": _string(),
            "consolidated_summary": _string(),
            "reason": _string(),
        }
    )
    return _object({"decisions": _array(decision)})


def _people_updates_schema() -> JsonSchema:
    update = _object(
        {
            "name": _string(),
            "profile": _string(),
            "confidence": _number(),
        }
    )
    return _object({"people_updates": _array(update)})


def _stage_schema(*, include_people: bool) -> JsonSchema:
    properties: Dict[str, JsonSchema] = {
        "summary": _string(),
        "stable_facts": _string_array(),
        "shared_claims": _string_array(),
        "active_topics": _string_array(),
        "group_dynamics": _string_array(),
        "open_items": _string_array(),
        "stale_or_uncertain": _string_array(),
    }
    if include_people:
        properties.update(_people_updates_schema()["properties"])
    return _object(properties)


def _observation_schema() -> JsonSchema:
    observation = _object(
        {
            "subject_name": _string(),
            "subject_sender_id": _string(),
            "observation_type": _string(
                "objective_fact",
                "experience",
                "preference",
                "interest",
                "skill",
                "habit",
                "group_role",
                "relationship",
                "status",
                "plan",
            ),
            "field": _string(
                "identity",
                "group_role",
                "occupation",
                "employer",
                "education",
                "location",
                "family",
                "relationship",
                "health",
                "preference",
                "interest",
                "skill",
                "asset",
                "experience",
                "habit",
                "plan",
                "current_status",
                "other",
            ),
            "statement": _string(),
            "source_relation": _string(
                "self_report",
                "direct_action",
                "attributed_statement",
                "group_interaction",
            ),
            "epistemic_status": _string(
                "asserted",
                "uncertain",
                "joke",
                "sarcasm",
                "roleplay",
                "hypothetical",
                "denied",
            ),
            "confidence": _number(),
            "valid_from": _string(),
            "valid_to": _string(),
            "observed_at": _string(),
            "evidence_cursors": _integer_array(),
            "subject_evidence_cursors": _integer_array(),
            "sensitivity": _string("low", "medium", "high"),
            "durability": _string("stable", "lifecycle", "ephemeral"),
            "evidence_strength": _string(
                "explicit",
                "repeated_behavior",
                "weak_inference",
            ),
            "memory_value": _number(),
            "future_value_reason": _string(),
        }
    )
    return _object({"observations": _array(observation)})


def _observation_verification_schema(*, projection: bool) -> JsonSchema:
    if projection:
        verification = _object(
            {
                "item_id": _string(),
                "verdict": _string("supported", "reject"),
                "field_alignment": _string("correct", "wrong"),
                "temporal_alignment": _string("correct", "wrong"),
                "atomicity": _string("atomic", "compound", "not_applicable"),
                "confidence": _number(),
                "reason": _string(),
            }
        )
    else:
        verification = _object(
            {
                "candidate_id": _string(),
                "verdict": _string("supported", "uncertain", "reject"),
                "subject_binding": _string(
                    "explicit",
                    "contextual",
                    "unsupported",
                ),
                "literalness": _string(
                    "literal",
                    "direct_behavior",
                    "joke",
                    "sarcasm",
                    "boast",
                    "roleplay",
                    "hypothetical",
                    "third_party",
                ),
                "evidence_completeness": _string(
                    "complete",
                    "partial",
                    "missing",
                ),
                "atomicity": _string("atomic", "compound"),
                "confidence": _number(),
                "reason": _string(),
            }
        )
    return _object({"verifications": _array(verification)})


def _period_schema() -> JsonSchema:
    candidate_fact = _object(
        {
            "field": _string(),
            "value": _string(),
            "status": _string(
                "current",
                "historical",
                "planned",
                "uncertain",
                "disputed",
            ),
            "valid_from": _string(),
            "valid_to": _string(),
            "evidence_observation_ids": _integer_array(),
            "sensitivity": _string("low", "medium", "high"),
        }
    )
    timeline = _object(
        {
            "text": _string(),
            "evidence_observation_ids": _integer_array(),
        }
    )
    pattern = _object(
        {
            "type": _string(
                "interest",
                "preference",
                "habit",
                "skill",
                "group_role",
                "communication_style",
                "trait",
            ),
            "description": _string(),
            "evidence_observation_ids": _integer_array(),
        }
    )
    relationship = _object(
        {
            "target_name": _string(),
            "description": _string(),
            "evidence_observation_ids": _integer_array(),
        }
    )
    return _object(
        {
            "summary": _string(),
            "candidate_facts": _array(candidate_fact),
            "timeline": _array(timeline),
            "pattern_signals": _array(pattern),
            "relationship_signals": _array(relationship),
        }
    )


def _consolidation_schema() -> JsonSchema:
    fact = _object(
        {
            "slot_key": _string(),
            "field": _string(),
            "value": _string(),
            "status": _string(
                "current",
                "historical",
                "planned",
                "uncertain",
                "disputed",
            ),
            "confidence": _number(),
            "valid_from": _string(),
            "valid_to": _string(),
            "observed_at": _string(),
            "evidence_observation_ids": _integer_array(),
            "priority": _number(),
            "sensitivity": _string("low", "medium", "high"),
        }
    )
    pattern = _object(
        {
            "type": _string(
                "trait",
                "interest",
                "preference",
                "habit",
                "skill",
                "group_role",
                "communication_style",
            ),
            "label": _string(),
            "description": _string(),
            "state": _string(
                "candidate",
                "confirmed",
                "declining",
                "disputed",
            ),
            "confidence": _number(),
            "evidence_observation_ids": _integer_array(),
            "sensitivity": _string("low", "medium", "high"),
        }
    )
    relationship = _object(
        {
            "target_name": _string(),
            "type": _string(
                "family",
                "friend",
                "colleague",
                "group_affinity",
                "group_friction",
                "mentor",
                "collaboration",
                "other",
            ),
            "description": _string(),
            "status": _string(
                "current",
                "historical",
                "uncertain",
                "disputed",
            ),
            "confidence": _number(),
            "evidence_observation_ids": _integer_array(),
            "sensitivity": _string("low", "medium", "high"),
        }
    )
    snapshot_item = _object(
        {
            "text": _string(),
            "evidence_observation_ids": _integer_array(),
            "valid_from": _string(),
            "valid_to": _string(),
            "confidence": _number(),
            "sensitivity": _string("low", "medium", "high"),
        }
    )
    snapshot = _object(
        {
            "current_snapshot": _array(snapshot_item),
            "timeline": _array(snapshot_item),
            "stable_traits": _array(snapshot_item),
            "group_relationships": _array(snapshot_item),
            "uncertain": _array(snapshot_item),
        }
    )
    return _object(
        {
            "facts": _array(fact),
            "patterns": _array(pattern),
            "relationships": _array(relationship),
            "snapshot": snapshot,
        }
    )


def codex_memory_output_schema(
    call_type: str,
    schema_hint: str,
    messages: Iterable[Mapping[str, Any]],
) -> Optional[JsonSchema]:
    """Return a strict Codex output schema for production memory call types."""
    normalized = str(call_type or "").strip()
    hint = str(schema_hint or "")
    prompt_text = "\n".join(
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, Mapping)
    )

    if normalized == "memory_event":
        return _event_schema()
    if normalized == "memory_verify":
        return _event_verify_schema()
    if normalized == "memory_dedup":
        return _event_dedup_schema()
    if normalized == "memory_stage":
        if "keep_fact_ids" in hint:
            # status_updates has arbitrary fact IDs as object keys, which the
            # strict Structured Outputs subset cannot represent safely.
            return None
        if "people_updates" in hint and "阶段" not in hint:
            return _people_updates_schema()
        return _stage_schema(include_people="people_updates" in hint)
    if normalized == "memory_person_observe":
        if "observations" in hint:
            return _observation_schema()
        if "verifications" in hint:
            return _observation_verification_schema(
                projection='"item_id"' in prompt_text
            )
        return None
    if normalized == "memory_person_period":
        return _period_schema()
    if normalized == "memory_person_consolidate":
        return _consolidation_schema()
    return None
