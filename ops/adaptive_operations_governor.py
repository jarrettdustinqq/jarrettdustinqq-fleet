#!/usr/bin/env python3
"""Fail-closed adaptive scheduling policy above JACS and existing executors.

The governor is intentionally not a scheduler, event store, or actuator. It consumes
one canonical JACS snapshot, emits a deterministic shadow plan, and may emit at most
one allowlisted canary mutation intent when every persistence, heartbeat, authority,
and readback gate is satisfied. Applying an intent remains the responsibility of the
existing scheduler adapter and JACS persistence path.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


SNAPSHOT_SCHEMA_VERSION = "AOG.SNAPSHOT.v1"
LOOP_REGISTRY_VERSION = "AOG.LOOP_REGISTRY.v1"
POLICY_VERSION = "AOG.POLICY.v1"

LIFECYCLE_STATES = {
    "CANDIDATE",
    "SHADOW",
    "ACTIVE",
    "DEGRADED",
    "PAUSED",
    "RETIRED",
    "QUARANTINED",
}
OUTCOME_CLASSES = {
    "VERIFIED_CONSEQUENCE",
    "VERIFIED_INFORMATION_GAIN",
    "VERIFIED_ABSTENTION",
    "NO_MATERIAL_CHANGE",
    "BLOCKED",
    "FAILED",
    "CONTRADICTED",
}
MUTATION_ACTIONS = {"SKIP_ONCE", "PAUSE", "RESUME", "SET_CADENCE"}
ADVISOR_ALLOWED_KEYS = {
    "proposal_id",
    "loop_id",
    "action",
    "reason",
    "confidence",
    "evidence_refs",
    "target_cadence_seconds",
}
DEPENDENCY_READY_STATES = {"READY", "GREEN", "VERIFIED", "AVAILABLE"}
AUTHORITY_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}

POLICY_MANIFEST: dict[str, Any] = {
    "policy_version": POLICY_VERSION,
    "role": "deterministic_authoritative_kernel",
    "persistence": {
        "authoritative_store": "JACS",
        "minimum_consecutive_clean_runs": 5,
        "required_checks": [
            "provider_atomic_audit_cohort",
            "exact_cardinality",
            "exact_idempotency",
            "neighbor_integrity",
            "state_after_audit",
            "final_audit_state_readback",
            "historical_t05_loss_preserved",
        ],
    },
    "heartbeat": {
        "fixed_primitive_required": True,
        "replacement_forbidden": True,
    },
    "canary": {
        "max_loops": 1,
        "max_mutations_per_snapshot": 1,
        "allowed_actions": sorted(MUTATION_ACTIONS),
        "requires_exact_scoped_authority": True,
        "requires_rollback": True,
    },
    "scoring": {
        "priority_class": {"P0": 4000.0, "P1": 3000.0, "P2": 2000.0, "P3": 1000.0, "P4": 0.0},
        "hard_obligation": 10000.0,
        "deadline_pressure": 800.0,
        "expected_realized_benefit": 300.0,
        "information_gain": 120.0,
        "external_branching_value": 100.0,
        "external_progress_probability": 140.0,
        "employment_compatibility": 50.0,
        "starvation": 80.0,
        "cost": -100.0,
        "attention_burden": -80.0,
        "risk": -180.0,
    },
    "outcomes_counted_as_value": [
        "VERIFIED_CONSEQUENCE",
        "VERIFIED_INFORMATION_GAIN",
        "VERIFIED_ABSTENTION",
    ],
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


POLICY_HASH = _sha256(POLICY_MANIFEST)


class ContractError(ValueError):
    """Raised when the canonical snapshot does not satisfy the input contract."""


@dataclasses.dataclass(frozen=True)
class GateResult:
    ready: bool
    reasons: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: tuple[str, ...]
    due: bool
    deadline_pressure: float


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} must be an object")
    return value


def _require_sequence(value: Any, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ContractError(f"{field} must be an array")
    return value


def _require_string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    if not allow_empty and not value.strip():
        raise ContractError(f"{field} must not be empty")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{field} must be a boolean")
    return value


def _require_int(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{field} must be >= {minimum}")
    return value


def _bounded_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ContractError(f"{field} must be between 0 and 1")
    return number


def _nonnegative_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ContractError(f"{field} must be nonnegative")
    return number


def _parse_time(value: Any, field: str) -> dt.datetime:
    raw = _require_string(value, field)
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field} must be ISO 8601 with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{field} must include a timezone")
    return parsed


def _optional_time(value: Any, field: str) -> dt.datetime | None:
    if value in (None, ""):
        return None
    return _parse_time(value, field)


def _seconds_between(later: dt.datetime, earlier: dt.datetime) -> float:
    return (later.astimezone(dt.timezone.utc) - earlier.astimezone(dt.timezone.utc)).total_seconds()


def _get_required(mapping: Mapping[str, Any], key: str, field: str) -> Any:
    if key not in mapping:
        raise ContractError(f"{field}.{key} is required")
    return mapping[key]


def validate_snapshot(snapshot: Any) -> dict[str, Any]:
    """Validate and deep-copy one canonical JACS governor snapshot."""

    raw = _require_mapping(snapshot, "snapshot")
    normalized = copy.deepcopy(dict(raw))

    if _require_string(_get_required(raw, "schema_version", "snapshot"), "snapshot.schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ContractError(f"snapshot.schema_version must equal {SNAPSHOT_SCHEMA_VERSION}")
    if _require_string(_get_required(raw, "registry_version", "snapshot"), "snapshot.registry_version") != LOOP_REGISTRY_VERSION:
        raise ContractError(f"snapshot.registry_version must equal {LOOP_REGISTRY_VERSION}")
    _parse_time(_get_required(raw, "snapshot_at", "snapshot"), "snapshot.snapshot_at")
    _require_string(_get_required(raw, "principal_id", "snapshot"), "snapshot.principal_id")

    requested_mode = _require_string(_get_required(raw, "requested_mode", "snapshot"), "snapshot.requested_mode").upper()
    if requested_mode not in {"SHADOW", "CANARY"}:
        raise ContractError("snapshot.requested_mode must be SHADOW or CANARY")
    normalized["requested_mode"] = requested_mode

    _require_string(_get_required(raw, "policy_hash", "snapshot"), "snapshot.policy_hash")

    journal = _require_mapping(_get_required(raw, "journal_gate", "snapshot"), "snapshot.journal_gate")
    _require_string(_get_required(journal, "degradation_state", "snapshot.journal_gate"), "snapshot.journal_gate.degradation_state")
    _require_int(_get_required(journal, "proof_streak", "snapshot.journal_gate"), "snapshot.journal_gate.proof_streak", minimum=0)
    _require_int(_get_required(journal, "required_streak", "snapshot.journal_gate"), "snapshot.journal_gate.required_streak", minimum=5)
    for key in (
        "provider_atomic_audit_cohort",
        "exact_cardinality_verified",
        "exact_idempotency_verified",
        "neighbor_integrity_verified",
        "state_after_audit_verified",
        "final_audit_state_readback_verified",
        "historical_t05_loss_preserved",
    ):
        _require_bool(_get_required(journal, key, "snapshot.journal_gate"), f"snapshot.journal_gate.{key}")
    _require_string(_get_required(journal, "last_qualified_run", "snapshot.journal_gate"), "snapshot.journal_gate.last_qualified_run", allow_empty=True)

    heartbeat = _require_mapping(_get_required(raw, "heartbeat", "snapshot"), "snapshot.heartbeat")
    _require_bool(_get_required(heartbeat, "fixed_primitive", "snapshot.heartbeat"), "snapshot.heartbeat.fixed_primitive")
    _require_bool(_get_required(heartbeat, "replacement_requested", "snapshot.heartbeat"), "snapshot.heartbeat.replacement_requested")
    _require_bool(_get_required(heartbeat, "reported_healthy", "snapshot.heartbeat"), "snapshot.heartbeat.reported_healthy")
    _parse_time(_get_required(heartbeat, "last_seen_at", "snapshot.heartbeat"), "snapshot.heartbeat.last_seen_at")
    _require_int(_get_required(heartbeat, "max_age_seconds", "snapshot.heartbeat"), "snapshot.heartbeat.max_age_seconds", minimum=1)

    registry = _require_mapping(_get_required(raw, "registry_health", "snapshot"), "snapshot.registry_health")
    _require_bool(_get_required(registry, "snapshot_readback_verified", "snapshot.registry_health"), "snapshot.registry_health.snapshot_readback_verified")
    _require_bool(_get_required(registry, "critical_state_fresh", "snapshot.registry_health"), "snapshot.registry_health.critical_state_fresh")
    contradictions = _require_sequence(_get_required(registry, "critical_contradictions", "snapshot.registry_health"), "snapshot.registry_health.critical_contradictions")
    for index, item in enumerate(contradictions):
        _require_string(item, f"snapshot.registry_health.critical_contradictions[{index}]")

    capacity = _require_mapping(_get_required(raw, "capacity", "snapshot"), "snapshot.capacity")
    _require_int(_get_required(capacity, "max_dispatches", "snapshot.capacity"), "snapshot.capacity.max_dispatches", minimum=1)
    _require_int(_get_required(capacity, "max_heavy", "snapshot.capacity"), "snapshot.capacity.max_heavy", minimum=0)
    _require_int(_get_required(capacity, "max_calls", "snapshot.capacity"), "snapshot.capacity.max_calls", minimum=1)

    loops = _require_sequence(_get_required(raw, "loops", "snapshot"), "snapshot.loops")
    if not loops:
        raise ContractError("snapshot.loops must not be empty")
    seen: set[str] = set()
    for index, item in enumerate(loops):
        loop = _require_mapping(item, f"snapshot.loops[{index}]")
        prefix = f"snapshot.loops[{index}]"
        loop_id = _require_string(_get_required(loop, "loop_id", prefix), f"{prefix}.loop_id")
        if loop_id in seen:
            raise ContractError(f"duplicate loop_id: {loop_id}")
        seen.add(loop_id)
        for key in (
            "objective_id",
            "layer",
            "trigger",
            "idempotency_rule",
            "pause_rule",
            "retirement_rule",
            "priority_class",
            "weight_class",
        ):
            _require_string(_get_required(loop, key, prefix), f"{prefix}.{key}")
        _require_string(loop.get("scheduler_id", ""), f"{prefix}.scheduler_id", allow_empty=True)
        _require_string(loop.get("parent_loop_id", ""), f"{prefix}.parent_loop_id", allow_empty=True)
        lifecycle = _require_string(_get_required(loop, "lifecycle_state", prefix), f"{prefix}.lifecycle_state").upper()
        if lifecycle not in LIFECYCLE_STATES:
            raise ContractError(f"{prefix}.lifecycle_state is unsupported: {lifecycle}")
        _require_bool(_get_required(loop, "enabled", prefix), f"{prefix}.enabled")
        _require_bool(_get_required(loop, "triggered", prefix), f"{prefix}.triggered")
        _require_bool(_get_required(loop, "hard_obligation", prefix), f"{prefix}.hard_obligation")
        _require_bool(_get_required(loop, "authority_valid", prefix), f"{prefix}.authority_valid")
        _require_bool(loop.get("resume_triggered", False), f"{prefix}.resume_triggered")

        cadence = _require_mapping(_get_required(loop, "cadence", prefix), f"{prefix}.cadence")
        minimum = _require_int(_get_required(cadence, "min_seconds", f"{prefix}.cadence"), f"{prefix}.cadence.min_seconds", minimum=1)
        maximum = _require_int(_get_required(cadence, "max_seconds", f"{prefix}.cadence"), f"{prefix}.cadence.max_seconds", minimum=minimum)
        current = _require_int(_get_required(cadence, "current_seconds", f"{prefix}.cadence"), f"{prefix}.cadence.current_seconds", minimum=minimum)
        if current > maximum:
            raise ContractError(f"{prefix}.cadence.current_seconds exceeds max_seconds")
        _require_string(_get_required(cadence, "phase", f"{prefix}.cadence"), f"{prefix}.cadence.phase")

        required_auth = _require_string(_get_required(loop, "required_authority", prefix), f"{prefix}.required_authority").upper()
        current_auth = _require_string(_get_required(loop, "current_authority", prefix), f"{prefix}.current_authority").upper()
        if required_auth not in AUTHORITY_ORDER or current_auth not in AUTHORITY_ORDER:
            raise ContractError(f"{prefix} has an unsupported authority level")

        dependencies = _require_sequence(_get_required(loop, "dependencies", prefix), f"{prefix}.dependencies")
        for dep_index, dep_item in enumerate(dependencies):
            dep = _require_mapping(dep_item, f"{prefix}.dependencies[{dep_index}]")
            _require_string(_get_required(dep, "dependency_id", f"{prefix}.dependencies[{dep_index}]"), f"{prefix}.dependencies[{dep_index}].dependency_id")
            _require_string(_get_required(dep, "status", f"{prefix}.dependencies[{dep_index}]"), f"{prefix}.dependencies[{dep_index}].status")
            _require_bool(_get_required(dep, "critical", f"{prefix}.dependencies[{dep_index}]"), f"{prefix}.dependencies[{dep_index}].critical")
            _optional_time(dep.get("fresh_until"), f"{prefix}.dependencies[{dep_index}].fresh_until")

        bounds = _require_mapping(_get_required(loop, "adaptation_bounds", prefix), f"{prefix}.adaptation_bounds")
        actions = _require_sequence(_get_required(bounds, "allowed_actions", f"{prefix}.adaptation_bounds"), f"{prefix}.adaptation_bounds.allowed_actions")
        for action_index, action in enumerate(actions):
            action_name = _require_string(action, f"{prefix}.adaptation_bounds.allowed_actions[{action_index}]").upper()
            if action_name not in MUTATION_ACTIONS:
                raise ContractError(f"unsupported adaptation action: {action_name}")
        _require_int(_get_required(bounds, "no_change_threshold", f"{prefix}.adaptation_bounds"), f"{prefix}.adaptation_bounds.no_change_threshold", minimum=1)
        _require_int(_get_required(bounds, "failure_pause_threshold", f"{prefix}.adaptation_bounds"), f"{prefix}.adaptation_bounds.failure_pause_threshold", minimum=1)

        budget = _require_mapping(_get_required(loop, "resource_budget", prefix), f"{prefix}.resource_budget")
        for key in ("max_calls_per_run", "estimated_calls"):
            _require_int(_get_required(budget, key, f"{prefix}.resource_budget"), f"{prefix}.resource_budget.{key}", minimum=0)
        for key in ("max_runtime_seconds", "estimated_runtime_seconds", "max_cost_usd", "estimated_cost_usd"):
            _nonnegative_number(_get_required(budget, key, f"{prefix}.resource_budget"), f"{prefix}.resource_budget.{key}")

        for key in ("success_evidence", "verified_abstention_criteria", "contradictions", "evidence_refs"):
            values = _require_sequence(_get_required(loop, key, prefix), f"{prefix}.{key}")
            for value_index, value in enumerate(values):
                _require_string(value, f"{prefix}.{key}[{value_index}]")

        for key in (
            "expected_benefit",
            "probability_success",
            "information_gain",
            "external_branching_value",
            "external_progress_probability",
            "employment_compatibility",
            "cost",
            "attention_burden",
            "risk",
        ):
            _bounded_number(_get_required(loop, key, prefix), f"{prefix}.{key}")

        for key in ("no_material_change_streak", "consecutive_failures", "deferred_cycles"):
            _require_int(_get_required(loop, key, prefix), f"{prefix}.{key}", minimum=0)

        for key in (
            "last_run_at",
            "next_due_at",
            "deadline_at",
            "source_fresh_until",
            "last_material_outcome_at",
        ):
            _optional_time(loop.get(key), f"{prefix}.{key}")
        _require_string(loop.get("event_key", ""), f"{prefix}.event_key", allow_empty=True)
        _require_string(loop.get("last_event_key", ""), f"{prefix}.last_event_key", allow_empty=True)

    proposals = _require_sequence(raw.get("advisor_proposals", []), "snapshot.advisor_proposals")
    proposal_ids: set[str] = set()
    for index, item in enumerate(proposals):
        proposal = _require_mapping(item, f"snapshot.advisor_proposals[{index}]")
        unknown = set(proposal) - ADVISOR_ALLOWED_KEYS
        if unknown:
            raise ContractError(
                f"snapshot.advisor_proposals[{index}] contains forbidden keys: {sorted(unknown)}"
            )
        proposal_id = _require_string(_get_required(proposal, "proposal_id", f"snapshot.advisor_proposals[{index}]"), f"snapshot.advisor_proposals[{index}].proposal_id")
        if proposal_id in proposal_ids:
            raise ContractError(f"duplicate advisor proposal_id: {proposal_id}")
        proposal_ids.add(proposal_id)
        _require_string(_get_required(proposal, "loop_id", f"snapshot.advisor_proposals[{index}]"), f"snapshot.advisor_proposals[{index}].loop_id")
        action = _require_string(_get_required(proposal, "action", f"snapshot.advisor_proposals[{index}]"), f"snapshot.advisor_proposals[{index}].action").upper()
        if action not in MUTATION_ACTIONS:
            raise ContractError(f"unsupported advisor action: {action}")
        _require_string(_get_required(proposal, "reason", f"snapshot.advisor_proposals[{index}]"), f"snapshot.advisor_proposals[{index}].reason")
        _bounded_number(_get_required(proposal, "confidence", f"snapshot.advisor_proposals[{index}]"), f"snapshot.advisor_proposals[{index}].confidence")
        evidence = _require_sequence(_get_required(proposal, "evidence_refs", f"snapshot.advisor_proposals[{index}]"), f"snapshot.advisor_proposals[{index}].evidence_refs")
        if not evidence:
            raise ContractError(f"snapshot.advisor_proposals[{index}].evidence_refs must not be empty")
        for evidence_index, ref in enumerate(evidence):
            _require_string(ref, f"snapshot.advisor_proposals[{index}].evidence_refs[{evidence_index}]")
        if action == "SET_CADENCE":
            _require_int(_get_required(proposal, "target_cadence_seconds", f"snapshot.advisor_proposals[{index}]"), f"snapshot.advisor_proposals[{index}].target_cadence_seconds", minimum=1)
        elif "target_cadence_seconds" in proposal and proposal["target_cadence_seconds"] is not None:
            raise ContractError(
                f"snapshot.advisor_proposals[{index}].target_cadence_seconds is only valid for SET_CADENCE"
            )

    outcomes = _require_sequence(raw.get("recent_outcomes", []), "snapshot.recent_outcomes")
    outcome_ids: set[str] = set()
    for index, item in enumerate(outcomes):
        outcome = _require_mapping(item, f"snapshot.recent_outcomes[{index}]")
        outcome_id = _require_string(_get_required(outcome, "outcome_id", f"snapshot.recent_outcomes[{index}]"), f"snapshot.recent_outcomes[{index}].outcome_id")
        if outcome_id in outcome_ids:
            raise ContractError(f"duplicate outcome_id: {outcome_id}")
        outcome_ids.add(outcome_id)
        _require_string(_get_required(outcome, "loop_id", f"snapshot.recent_outcomes[{index}]"), f"snapshot.recent_outcomes[{index}].loop_id")
        outcome_class = _require_string(_get_required(outcome, "outcome_class", f"snapshot.recent_outcomes[{index}]"), f"snapshot.recent_outcomes[{index}].outcome_class").upper()
        if outcome_class not in OUTCOME_CLASSES:
            raise ContractError(f"unsupported outcome_class: {outcome_class}")
        _parse_time(_get_required(outcome, "observed_at", f"snapshot.recent_outcomes[{index}]"), f"snapshot.recent_outcomes[{index}].observed_at")
        refs = _require_sequence(_get_required(outcome, "evidence_refs", f"snapshot.recent_outcomes[{index}]"), f"snapshot.recent_outcomes[{index}].evidence_refs")
        if outcome_class.startswith("VERIFIED_") and not refs:
            raise ContractError(f"verified outcome {outcome_id} requires evidence_refs")
        for ref_index, ref in enumerate(refs):
            _require_string(ref, f"snapshot.recent_outcomes[{index}].evidence_refs[{ref_index}]")

    if "canary_authority" in raw and raw["canary_authority"] is not None:
        _validate_canary_authority(raw["canary_authority"], raw)

    return normalized


def _validate_canary_authority(value: Any, snapshot: Mapping[str, Any]) -> None:
    grant = _require_mapping(value, "snapshot.canary_authority")
    for key in ("grant_id", "principal_id", "loop_id", "verification_method"):
        _require_string(_get_required(grant, key, "snapshot.canary_authority"), f"snapshot.canary_authority.{key}")
    if grant["principal_id"] != snapshot["principal_id"]:
        raise ContractError("snapshot.canary_authority.principal_id does not match snapshot.principal_id")
    actions = _require_sequence(_get_required(grant, "allowed_actions", "snapshot.canary_authority"), "snapshot.canary_authority.allowed_actions")
    if not actions:
        raise ContractError("snapshot.canary_authority.allowed_actions must not be empty")
    for index, action in enumerate(actions):
        name = _require_string(action, f"snapshot.canary_authority.allowed_actions[{index}]").upper()
        if name not in MUTATION_ACTIONS:
            raise ContractError(f"unsupported canary authority action: {name}")
    _parse_time(_get_required(grant, "issued_at", "snapshot.canary_authority"), "snapshot.canary_authority.issued_at")
    _parse_time(_get_required(grant, "expires_at", "snapshot.canary_authority"), "snapshot.canary_authority.expires_at")
    _require_int(_get_required(grant, "max_mutations", "snapshot.canary_authority"), "snapshot.canary_authority.max_mutations", minimum=1)
    _require_bool(_get_required(grant, "rollback_required", "snapshot.canary_authority"), "snapshot.canary_authority.rollback_required")


def journal_gate(snapshot: Mapping[str, Any]) -> GateResult:
    gate = _require_mapping(snapshot["journal_gate"], "snapshot.journal_gate")
    reasons: list[str] = []
    if str(gate["degradation_state"]).upper() != "CLEARED":
        reasons.append("JOURNAL_DEGRADATION_NOT_CLEARED")
    if int(gate["proof_streak"]) < int(gate["required_streak"]):
        reasons.append("INSUFFICIENT_CONSECUTIVE_CLEAN_RUNS")
    checks = {
        "provider_atomic_audit_cohort": "AUDIT_COHORT_NOT_PROVIDER_ATOMIC",
        "exact_cardinality_verified": "AUDIT_CARDINALITY_UNVERIFIED",
        "exact_idempotency_verified": "AUDIT_IDEMPOTENCY_UNVERIFIED",
        "neighbor_integrity_verified": "AUDIT_NEIGHBOR_INTEGRITY_UNVERIFIED",
        "state_after_audit_verified": "STATE_AFTER_AUDIT_UNVERIFIED",
        "final_audit_state_readback_verified": "FINAL_AUDIT_STATE_READBACK_UNVERIFIED",
        "historical_t05_loss_preserved": "HISTORICAL_T05_LOSS_NOT_PRESERVED",
    }
    for key, reason in checks.items():
        if not bool(gate[key]):
            reasons.append(reason)
    if int(gate["proof_streak"]) > 0 and not str(gate["last_qualified_run"]).strip():
        reasons.append("LAST_QUALIFIED_RUN_MISSING")
    return GateResult(not reasons, tuple(reasons))


def heartbeat_gate(snapshot: Mapping[str, Any]) -> GateResult:
    heartbeat = _require_mapping(snapshot["heartbeat"], "snapshot.heartbeat")
    snapshot_at = _parse_time(snapshot["snapshot_at"], "snapshot.snapshot_at")
    last_seen = _parse_time(heartbeat["last_seen_at"], "snapshot.heartbeat.last_seen_at")
    reasons: list[str] = []
    if not bool(heartbeat["fixed_primitive"]):
        reasons.append("FIXED_HEARTBEAT_MISSING")
    if bool(heartbeat["replacement_requested"]):
        reasons.append("HEARTBEAT_REPLACEMENT_REQUESTED")
    if not bool(heartbeat["reported_healthy"]):
        reasons.append("HEARTBEAT_REPORTED_UNHEALTHY")
    age = max(0.0, _seconds_between(snapshot_at, last_seen))
    if age > int(heartbeat["max_age_seconds"]):
        reasons.append("HEARTBEAT_STALE")
    return GateResult(not reasons, tuple(reasons))


def registry_gate(snapshot: Mapping[str, Any]) -> GateResult:
    registry = _require_mapping(snapshot["registry_health"], "snapshot.registry_health")
    reasons: list[str] = []
    if not bool(registry["snapshot_readback_verified"]):
        reasons.append("REGISTRY_SNAPSHOT_READBACK_UNVERIFIED")
    if not bool(registry["critical_state_fresh"]):
        reasons.append("CRITICAL_REGISTRY_STATE_STALE")
    if registry["critical_contradictions"]:
        reasons.append("CRITICAL_REGISTRY_CONTRADICTIONS_OPEN")
    if snapshot["policy_hash"] != POLICY_HASH:
        reasons.append("POLICY_HASH_DRIFT")
    return GateResult(not reasons, tuple(reasons))


def canary_authority_gate(snapshot: Mapping[str, Any]) -> GateResult:
    requested = str(snapshot["requested_mode"]).upper()
    if requested != "CANARY":
        return GateResult(True, ())
    grant = snapshot.get("canary_authority")
    if not grant:
        return GateResult(False, ("CANARY_AUTHORITY_MISSING",))
    _validate_canary_authority(grant, snapshot)
    snapshot_at = _parse_time(snapshot["snapshot_at"], "snapshot.snapshot_at")
    issued_at = _parse_time(grant["issued_at"], "snapshot.canary_authority.issued_at")
    expires_at = _parse_time(grant["expires_at"], "snapshot.canary_authority.expires_at")
    reasons: list[str] = []
    if issued_at > snapshot_at:
        reasons.append("CANARY_AUTHORITY_NOT_YET_VALID")
    if snapshot_at >= expires_at:
        reasons.append("CANARY_AUTHORITY_EXPIRED")
    if int(grant["max_mutations"]) != 1:
        reasons.append("CANARY_AUTHORITY_MUST_ALLOW_EXACTLY_ONE_MUTATION")
    if not bool(grant["rollback_required"]):
        reasons.append("CANARY_AUTHORITY_MUST_REQUIRE_ROLLBACK")
    loop_ids = {loop["loop_id"] for loop in snapshot["loops"]}
    if grant["loop_id"] not in loop_ids:
        reasons.append("CANARY_AUTHORITY_LOOP_NOT_IN_REGISTRY")
    return GateResult(not reasons, tuple(reasons))


def _deadline_pressure(loop: Mapping[str, Any], snapshot_at: dt.datetime) -> float:
    deadline = _optional_time(loop.get("deadline_at"), f"loop[{loop['loop_id']}].deadline_at")
    if deadline is None:
        return 0.0
    remaining = _seconds_between(deadline, snapshot_at)
    if remaining <= 0:
        return 1.0
    cadence = int(loop["cadence"]["current_seconds"])
    lead_window = max(cadence, int(loop.get("deadline_lead_seconds", cadence)))
    return max(0.0, min(1.0, 1.0 - (remaining / lead_window)))


def _dependencies_ready(loop: Mapping[str, Any], snapshot_at: dt.datetime) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for dep in loop["dependencies"]:
        if not dep["critical"]:
            continue
        status = str(dep["status"]).upper()
        if status not in DEPENDENCY_READY_STATES:
            reasons.append(f"DEPENDENCY_UNAVAILABLE:{dep['dependency_id']}:{status}")
        fresh_until = _optional_time(dep.get("fresh_until"), f"dependency[{dep['dependency_id']}].fresh_until")
        if fresh_until is not None and snapshot_at > fresh_until:
            reasons.append(f"DEPENDENCY_STALE:{dep['dependency_id']}")
    return not reasons, reasons


def loop_eligibility(loop: Mapping[str, Any], snapshot_at: dt.datetime) -> Eligibility:
    reasons: list[str] = []
    lifecycle = str(loop["lifecycle_state"]).upper()
    if not bool(loop["enabled"]):
        reasons.append("DISABLED")
    if lifecycle in {"PAUSED", "RETIRED", "QUARANTINED"}:
        reasons.append(f"LIFECYCLE_{lifecycle}")
    if loop["contradictions"]:
        reasons.append("LOOP_CONTRADICTIONS_OPEN")
    fresh_until = _optional_time(loop.get("source_fresh_until"), f"loop[{loop['loop_id']}].source_fresh_until")
    if fresh_until is not None and snapshot_at > fresh_until:
        reasons.append("SOURCE_STALE")

    dependencies_ready, dependency_reasons = _dependencies_ready(loop, snapshot_at)
    if not dependencies_ready:
        reasons.extend(dependency_reasons)

    required = AUTHORITY_ORDER[str(loop["required_authority"]).upper()]
    current = AUTHORITY_ORDER[str(loop["current_authority"]).upper()]
    if not bool(loop["authority_valid"]) or current < required:
        reasons.append("AUTHORITY_NOT_READY")

    budget = loop["resource_budget"]
    if int(budget["estimated_calls"]) > int(budget["max_calls_per_run"]):
        reasons.append("CALL_BUDGET_EXCEEDED")
    if float(budget["estimated_runtime_seconds"]) > float(budget["max_runtime_seconds"]):
        reasons.append("RUNTIME_BUDGET_EXCEEDED")
    if float(budget["estimated_cost_usd"]) > float(budget["max_cost_usd"]):
        reasons.append("COST_BUDGET_EXCEEDED")

    event_key = str(loop.get("event_key", ""))
    if event_key and event_key == str(loop.get("last_event_key", "")):
        reasons.append("DUPLICATE_SOURCE_EVENT")

    next_due = _optional_time(loop.get("next_due_at"), f"loop[{loop['loop_id']}].next_due_at")
    due = bool(loop["triggered"]) or next_due is None or snapshot_at >= next_due
    if not due:
        reasons.append("NOT_DUE")

    return Eligibility(not reasons, tuple(reasons), due, _deadline_pressure(loop, snapshot_at))


def _starvation_score(loop: Mapping[str, Any]) -> float:
    threshold = max(1, int(loop["adaptation_bounds"].get("starvation_threshold", 3)))
    cycles = int(loop["deferred_cycles"])
    return min(1.0, cycles / threshold)


def score_loop(loop: Mapping[str, Any], eligibility: Eligibility) -> tuple[float, dict[str, float]]:
    weights = POLICY_MANIFEST["scoring"]
    priority_weight = float(weights["priority_class"].get(str(loop["priority_class"]).upper(), 0.0))
    components = {
        "priority_class": priority_weight,
        "hard_obligation": float(weights["hard_obligation"]) if loop["hard_obligation"] else 0.0,
        "deadline_pressure": eligibility.deadline_pressure * float(weights["deadline_pressure"]),
        "expected_realized_benefit": float(loop["expected_benefit"])
        * float(loop["probability_success"])
        * float(weights["expected_realized_benefit"]),
        "information_gain": float(loop["information_gain"]) * float(weights["information_gain"]),
        "external_branching_value": float(loop["external_branching_value"])
        * float(weights["external_branching_value"]),
        "external_progress_probability": float(loop["external_progress_probability"])
        * float(weights["external_progress_probability"]),
        "employment_compatibility": float(loop["employment_compatibility"])
        * float(weights["employment_compatibility"]),
        "starvation": _starvation_score(loop) * float(weights["starvation"]),
        "cost": float(loop["cost"]) * float(weights["cost"]),
        "attention_burden": float(loop["attention_burden"]) * float(weights["attention_burden"]),
        "risk": float(loop["risk"]) * float(weights["risk"]),
    }
    return round(sum(components.values()), 6), {key: round(value, 6) for key, value in components.items()}


def build_dispatch_plan(snapshot: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    snapshot_at = _parse_time(snapshot["snapshot_at"], "snapshot.snapshot_at")
    evaluated: list[dict[str, Any]] = []
    for loop in snapshot["loops"]:
        eligibility = loop_eligibility(loop, snapshot_at)
        score, components = score_loop(loop, eligibility)
        evaluated.append(
            {
                "loop_id": loop["loop_id"],
                "eligible": eligibility.eligible,
                "due": eligibility.due,
                "score": score,
                "score_components": components,
                "reasons": list(eligibility.reasons),
                "weight_class": str(loop["weight_class"]).upper(),
                "estimated_calls": int(loop["resource_budget"]["estimated_calls"]),
                "priority_class": str(loop["priority_class"]).upper(),
                "hard_obligation": bool(loop["hard_obligation"]),
                "idempotency_rule": loop["idempotency_rule"],
            }
        )

    ranked = sorted(evaluated, key=lambda item: (-item["score"], item["loop_id"]))
    capacity = snapshot["capacity"]
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    heavy = 0
    calls = 0
    diagnostics: list[str] = []

    for item in ranked:
        decision = copy.deepcopy(item)
        if not item["eligible"]:
            decision["decision"] = "REJECT"
            rejected.append(decision)
            continue
        capacity_reasons: list[str] = []
        if len(selected) >= int(capacity["max_dispatches"]):
            capacity_reasons.append("DISPATCH_CAPACITY_EXHAUSTED")
        if item["weight_class"] == "HEAVY" and heavy >= int(capacity["max_heavy"]):
            capacity_reasons.append("HEAVY_CAPACITY_EXHAUSTED")
        if calls + int(item["estimated_calls"]) > int(capacity["max_calls"]):
            capacity_reasons.append("TOTAL_CALL_CAPACITY_EXHAUSTED")
        if capacity_reasons:
            decision["decision"] = "REJECT"
            decision["reasons"] = capacity_reasons
            rejected.append(decision)
            if item["hard_obligation"]:
                diagnostics.append(f"HARD_OBLIGATION_STARVED:{item['loop_id']}")
            continue
        decision["decision"] = "WOULD_DISPATCH"
        decision["rank"] = len(selected) + 1
        selected.append(decision)
        calls += int(item["estimated_calls"])
        if item["weight_class"] == "HEAVY":
            heavy += 1

    rejected.sort(key=lambda item: (item["loop_id"], item["reasons"]))
    return selected, rejected, diagnostics


def _proposal_id(source: str, loop_id: str, action: str, payload: Mapping[str, Any]) -> str:
    digest = _sha256({"source": source, "loop_id": loop_id, "action": action, "payload": payload})[:16]
    return f"AOG.PROPOSAL.{source}.{loop_id}.{action}.{digest}"


def heuristic_proposals(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    snapshot_at = _parse_time(snapshot["snapshot_at"], "snapshot.snapshot_at")
    proposals: list[dict[str, Any]] = []
    for loop in sorted(snapshot["loops"], key=lambda item: item["loop_id"]):
        actions = {str(action).upper() for action in loop["adaptation_bounds"]["allowed_actions"]}
        current = int(loop["cadence"]["current_seconds"])
        minimum = int(loop["cadence"]["min_seconds"])
        maximum = int(loop["cadence"]["max_seconds"])
        dependency_ready, dependency_reasons = _dependencies_ready(loop, snapshot_at)
        pressure = _deadline_pressure(loop, snapshot_at)

        if (
            int(loop["consecutive_failures"]) >= int(loop["adaptation_bounds"]["failure_pause_threshold"])
            and not dependency_ready
            and "PAUSE" in actions
            and str(loop["lifecycle_state"]).upper() not in {"PAUSED", "RETIRED", "QUARANTINED"}
            and not bool(loop["hard_obligation"])
        ):
            payload = {"dependency_reasons": dependency_reasons}
            proposals.append(
                {
                    "proposal_id": _proposal_id("HEURISTIC", loop["loop_id"], "PAUSE", payload),
                    "source": "HEURISTIC",
                    "loop_id": loop["loop_id"],
                    "action": "PAUSE",
                    "reason": "Repeated failures plus unavailable critical dependency",
                    "confidence": 1.0,
                    "evidence_refs": list(loop["evidence_refs"]),
                }
            )
            continue

        if (
            str(loop["lifecycle_state"]).upper() == "PAUSED"
            and bool(loop.get("resume_triggered", False))
            and dependency_ready
            and "RESUME" in actions
        ):
            proposals.append(
                {
                    "proposal_id": _proposal_id("HEURISTIC", loop["loop_id"], "RESUME", {}),
                    "source": "HEURISTIC",
                    "loop_id": loop["loop_id"],
                    "action": "RESUME",
                    "reason": "Explicit resume trigger and all critical dependencies ready",
                    "confidence": 1.0,
                    "evidence_refs": list(loop["evidence_refs"]),
                }
            )
            continue

        if (
            pressure >= 0.75
            and current > minimum
            and "SET_CADENCE" in actions
            and bool(loop["enabled"])
            and str(loop["lifecycle_state"]).upper() in {"ACTIVE", "DEGRADED", "SHADOW"}
        ):
            target = max(minimum, current // 2)
            payload = {"target_cadence_seconds": target, "pressure": pressure}
            proposals.append(
                {
                    "proposal_id": _proposal_id("HEURISTIC", loop["loop_id"], "SET_CADENCE", payload),
                    "source": "HEURISTIC",
                    "loop_id": loop["loop_id"],
                    "action": "SET_CADENCE",
                    "target_cadence_seconds": target,
                    "reason": "Deadline pressure exceeds the bounded acceleration threshold",
                    "confidence": 1.0,
                    "evidence_refs": list(loop["evidence_refs"]),
                }
            )
            continue

        if (
            int(loop["no_material_change_streak"]) >= int(loop["adaptation_bounds"]["no_change_threshold"])
            and not bool(loop["triggered"])
            and pressure == 0.0
            and current < maximum
            and "SET_CADENCE" in actions
            and not bool(loop["hard_obligation"])
        ):
            target = min(maximum, current * 2)
            payload = {"target_cadence_seconds": target, "streak": int(loop["no_material_change_streak"])}
            proposals.append(
                {
                    "proposal_id": _proposal_id("HEURISTIC", loop["loop_id"], "SET_CADENCE", payload),
                    "source": "HEURISTIC",
                    "loop_id": loop["loop_id"],
                    "action": "SET_CADENCE",
                    "target_cadence_seconds": target,
                    "reason": "Repeated verified no-material-change outcomes justify a bounded slower cadence",
                    "confidence": 1.0,
                    "evidence_refs": list(loop["evidence_refs"]),
                }
            )
    return proposals


def normalize_advisor_proposals(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    for raw in snapshot.get("advisor_proposals", []):
        item = dict(raw)
        item["source"] = "ADVISOR"
        item["action"] = str(item["action"]).upper()
        proposals.append(item)
    return proposals


def _validate_proposal_against_loop(
    proposal: Mapping[str, Any], loop: Mapping[str, Any], snapshot_at: dt.datetime
) -> list[str]:
    reasons: list[str] = []
    action = str(proposal["action"]).upper()
    allowed = {str(value).upper() for value in loop["adaptation_bounds"]["allowed_actions"]}
    if action not in allowed:
        reasons.append("ACTION_OUTSIDE_LOOP_ADAPTATION_BOUNDS")
    if not proposal.get("evidence_refs"):
        reasons.append("PROPOSAL_EVIDENCE_MISSING")
    if loop["contradictions"]:
        reasons.append("LOOP_CONTRADICTIONS_OPEN")

    dependencies_ready, _ = _dependencies_ready(loop, snapshot_at)
    pressure = _deadline_pressure(loop, snapshot_at)
    lifecycle = str(loop["lifecycle_state"]).upper()

    if action == "SET_CADENCE":
        target = proposal.get("target_cadence_seconds")
        if isinstance(target, bool) or not isinstance(target, int):
            reasons.append("TARGET_CADENCE_INVALID")
        else:
            minimum = int(loop["cadence"]["min_seconds"])
            maximum = int(loop["cadence"]["max_seconds"])
            if target < minimum or target > maximum:
                reasons.append("TARGET_CADENCE_OUTSIDE_BOUNDS")
            if target == int(loop["cadence"]["current_seconds"]):
                reasons.append("TARGET_CADENCE_UNCHANGED")
    elif action == "SKIP_ONCE":
        if loop["hard_obligation"]:
            reasons.append("HARD_OBLIGATION_CANNOT_BE_SKIPPED")
        if loop["triggered"]:
            reasons.append("TRIGGERED_LOOP_CANNOT_BE_SKIPPED")
        if pressure > 0.0:
            reasons.append("DEADLINE_RELEVANT_LOOP_CANNOT_BE_SKIPPED")
        if int(loop["consecutive_failures"]) > 0:
            reasons.append("RECOVERY_RUN_CANNOT_BE_SKIPPED")
    elif action == "PAUSE":
        if lifecycle == "PAUSED":
            reasons.append("LOOP_ALREADY_PAUSED")
        if lifecycle in {"RETIRED", "QUARANTINED"}:
            reasons.append(f"LIFECYCLE_{lifecycle}")
        if loop["hard_obligation"]:
            reasons.append("HARD_OBLIGATION_CANNOT_BE_PAUSED")
        if dependencies_ready and int(loop["consecutive_failures"]) == 0:
            reasons.append("PAUSE_CAUSAL_TRIGGER_NOT_ESTABLISHED")
    elif action == "RESUME":
        if lifecycle != "PAUSED":
            reasons.append("ONLY_PAUSED_LOOP_CAN_RESUME")
        if not dependencies_ready:
            reasons.append("DEPENDENCIES_NOT_READY_FOR_RESUME")
        if not bool(loop.get("resume_triggered", False)):
            reasons.append("RESUME_TRIGGER_MISSING")
    return reasons


def _mutation_state(loop: Mapping[str, Any], proposal: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    action = str(proposal["action"]).upper()
    before = {
        "enabled": bool(loop["enabled"]),
        "lifecycle_state": str(loop["lifecycle_state"]).upper(),
        "cadence_seconds": int(loop["cadence"]["current_seconds"]),
        "phase": loop["cadence"]["phase"],
    }
    after = copy.deepcopy(before)
    if action == "PAUSE":
        after["lifecycle_state"] = "PAUSED"
    elif action == "RESUME":
        after["lifecycle_state"] = "ACTIVE"
    elif action == "SET_CADENCE":
        after["cadence_seconds"] = int(proposal["target_cadence_seconds"])
    elif action == "SKIP_ONCE":
        after["skip_once"] = True
    return before, after


def evaluate_proposals(
    snapshot: Mapping[str, Any], effective_mode: str, all_gates_ready: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    loops = {loop["loop_id"]: loop for loop in snapshot["loops"]}
    snapshot_at = _parse_time(snapshot["snapshot_at"], "snapshot.snapshot_at")
    proposals = heuristic_proposals(snapshot) + normalize_advisor_proposals(snapshot)
    proposals.sort(key=lambda item: (item["loop_id"], item["source"], item["proposal_id"]))
    evaluated: list[dict[str, Any]] = []
    intents: list[dict[str, Any]] = []
    grant = snapshot.get("canary_authority") or {}

    for proposal in proposals:
        record = copy.deepcopy(dict(proposal))
        loop = loops.get(proposal["loop_id"])
        reasons: list[str] = []
        if loop is None:
            reasons.append("LOOP_NOT_IN_CANONICAL_REGISTRY")
        else:
            reasons.extend(_validate_proposal_against_loop(proposal, loop, snapshot_at))
        if effective_mode == "CANARY":
            if not all_gates_ready:
                reasons.append("GLOBAL_CANARY_GATES_NOT_READY")
            if grant.get("loop_id") != proposal["loop_id"]:
                reasons.append("PROPOSAL_OUTSIDE_CANARY_LOOP")
            grant_actions = {str(action).upper() for action in grant.get("allowed_actions", [])}
            if str(proposal["action"]).upper() not in grant_actions:
                reasons.append("PROPOSAL_OUTSIDE_CANARY_AUTHORITY")
            if intents:
                reasons.append("CANARY_MUTATION_LIMIT_REACHED")

        if reasons:
            record["status"] = "REJECTED"
            record["mutation_allowed"] = False
            record["reasons"] = sorted(set(reasons))
            evaluated.append(record)
            continue

        if effective_mode != "CANARY":
            record["status"] = "SHADOW_ACCEPTED"
            record["mutation_allowed"] = False
            record["reasons"] = ["SHADOW_MODE_NO_MUTATION"]
            evaluated.append(record)
            continue

        assert loop is not None
        before, after = _mutation_state(loop, proposal)
        precondition = {
            "loop_id": loop["loop_id"],
            "before": before,
            "registry_version": snapshot["registry_version"],
            "policy_hash": POLICY_HASH,
            "journal_last_qualified_run": snapshot["journal_gate"]["last_qualified_run"],
            "authority_grant_id": grant["grant_id"],
        }
        intent_id = f"AOG.INTENT.{_sha256({'proposal': proposal, 'precondition': precondition})[:24]}"
        intent = {
            "intent_id": intent_id,
            "idempotency_key": intent_id,
            "loop_id": loop["loop_id"],
            "scheduler_id": loop.get("scheduler_id", ""),
            "action": str(proposal["action"]).upper(),
            "before": before,
            "after": after,
            "precondition": precondition,
            "precondition_hash": _sha256(precondition),
            "verification_required": [
                "scheduler_runtime_exact_readback",
                "loop_registry_exact_readback",
                "audit_append_exact_readback",
                "unchanged_fixed_heartbeat",
            ],
            "rollback": {
                "action": "RESTORE_EXACT_PRIOR_CONFIGURATION",
                "restore": before,
                "trigger": "ANY_VERIFICATION_FAILURE_OR_ACCEPTANCE_THRESHOLD_BREACH",
            },
        }
        record["status"] = "CANARY_INTENT_EMITTED"
        record["mutation_allowed"] = True
        record["reasons"] = []
        record["intent_id"] = intent_id
        evaluated.append(record)
        intents.append(intent)

    return evaluated, intents


def outcome_metrics(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    counts = {name: 0 for name in sorted(OUTCOME_CLASSES)}
    unknown = 0
    for outcome in snapshot.get("recent_outcomes", []):
        name = str(outcome["outcome_class"]).upper()
        if name in counts:
            counts[name] += 1
        else:
            unknown += 1
    total = sum(counts.values())
    verified_value = sum(counts[name] for name in POLICY_MANIFEST["outcomes_counted_as_value"])
    no_change = counts["NO_MATERIAL_CHANGE"]
    failed_or_blocked = counts["FAILED"] + counts["BLOCKED"] + counts["CONTRADICTED"]
    return {
        "window_outcomes": total,
        "counts": counts,
        "verified_value_outcomes": verified_value,
        "verified_value_rate": round(verified_value / total, 6) if total else None,
        "no_material_change_rate": round(no_change / total, 6) if total else None,
        "failure_block_contradiction_rate": round(failed_or_blocked / total, 6) if total else None,
        "unknown_outcome_classes": unknown,
        "progress_rule": "Only verified consequence, information gain, or abstention counts as value",
    }


def evaluate(snapshot: Any) -> dict[str, Any]:
    canonical = validate_snapshot(snapshot)
    journal = journal_gate(canonical)
    heartbeat = heartbeat_gate(canonical)
    registry = registry_gate(canonical)
    authority = canary_authority_gate(canonical)
    all_gates_ready = journal.ready and heartbeat.ready and registry.ready and authority.ready

    requested = canonical["requested_mode"]
    effective_mode = "CANARY" if requested == "CANARY" and all_gates_ready else "SHADOW"
    gate_reasons = list(journal.reasons + heartbeat.reasons + registry.reasons + authority.reasons)

    selected, rejected, dispatch_diagnostics = build_dispatch_plan(canonical)
    proposals, intents = evaluate_proposals(canonical, effective_mode, all_gates_ready)

    decision_basis = {
        "schema_version": canonical["schema_version"],
        "registry_version": canonical["registry_version"],
        "snapshot_at": canonical["snapshot_at"],
        "policy_version": POLICY_VERSION,
        "policy_hash": POLICY_HASH,
        "snapshot_hash": _sha256(canonical),
        "requested_mode": requested,
        "effective_mode": effective_mode,
        "selected_loop_ids": [item["loop_id"] for item in selected],
        "proposal_ids": [item["proposal_id"] for item in proposals],
    }
    decision_id = f"AOG.DECISION.{_sha256(decision_basis)[:24]}"

    return {
        "decision_id": decision_id,
        "generated_from_snapshot_at": canonical["snapshot_at"],
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "registry_version": LOOP_REGISTRY_VERSION,
        "policy_version": POLICY_VERSION,
        "policy_hash": POLICY_HASH,
        "snapshot_hash": decision_basis["snapshot_hash"],
        "requested_mode": requested,
        "effective_mode": effective_mode,
        "production_mutation_allowed": bool(intents),
        "gates": {
            "journal": dataclasses.asdict(journal),
            "heartbeat": dataclasses.asdict(heartbeat),
            "registry": dataclasses.asdict(registry),
            "canary_authority": dataclasses.asdict(authority),
            "all_ready": all_gates_ready,
            "fail_closed_reasons": sorted(set(gate_reasons)),
        },
        "dispatch_plan": {
            "selected": selected,
            "rejected": rejected,
            "diagnostics": sorted(set(dispatch_diagnostics)),
            "semantics": "WOULD_DISPATCH is advisory until the existing executor consumes a verified intent",
        },
        "adaptation_proposals": proposals,
        "mutation_intents": intents,
        "metrics": outcome_metrics(canonical),
        "rollback_contract": {
            "global_stop_condition": (
                "Any Audit/State mismatch, duplicate external effect, missed hard deadline, "
                "policy drift, stale critical source, heartbeat loss, or unverifiable scheduler readback"
            ),
            "on_stop": "Emit no further intent; preserve evidence; restore exact prior loop configuration",
        },
    }


def _read_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(value: Any, path: str | None, pretty: bool) -> None:
    text = json.dumps(value, indent=2 if pretty else None, sort_keys=True)
    if pretty:
        text += "\n"
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate a canonical JACS loop snapshot without replacing its scheduler or persistence."
    )
    parser.add_argument("snapshot", nargs="?", help="JSON snapshot path, or - for stdin")
    parser.add_argument("--output", help="Write the decision envelope to this JSON path")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    parser.add_argument("--print-policy-hash", action="store_true", help="Print the compiled policy hash and exit")
    args = parser.parse_args(argv)

    if args.print_policy_hash:
        print(POLICY_HASH)
        return 0
    if not args.snapshot:
        parser.error("snapshot is required unless --print-policy-hash is used")

    try:
        result = evaluate(_read_json(args.snapshot))
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"adaptive-operations-governor: {exc}\n")
        return 2

    _write_json(result, args.output, args.pretty)
    if result["requested_mode"] == "CANARY" and result["effective_mode"] != "CANARY":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
