#!/usr/bin/env python3
"""Fail-closed JACS freshness, authority, and automation preflight.

The Fleet host cannot talk directly to ChatGPT connectors. This module therefore
consumes a connector-produced JSON snapshot containing the relevant JACS rows and
fresh live-source observations. It never upgrades UNKNOWN/CONFLICTED state by
inference and never mutates an external scheduler/provider. Expiration transitions
are persisted only as append-only local Evidence/Audit journal records; canonical
JACS writeback remains a separate connected-source reconciliation step.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


class PreflightError(RuntimeError):
    """Raised for malformed or unavailable preflight inputs."""


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    canonical_key: str | None = None
    state_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreflightReport:
    workflow_id: str
    allowed: bool
    findings: list[Finding] = field(default_factory=list)
    resolved_facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    effective_state_status: dict[str, str] = field(default_factory=dict)
    automation_diff: dict[str, Any] = field(default_factory=dict)
    primary_wip_count: int = 0
    stale_events_persisted: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["findings"] = [asdict(item) for item in self.findings]
        return payload

    def summary(self) -> str:
        blockers = [f for f in self.findings if f.severity == "BLOCK"]
        if not blockers:
            return "JACS preflight passed"
        return "; ".join(f"{item.code}: {item.message}" for item in blockers)


AUTHORITY_RANKS: dict[str, dict[str, int]] = {
    "contractual": {
        "user_explicit": 120,
        "policy": 115,
        "provider": 100,
        "counterparty": 100,
        "scheduler_runtime": 30,
        "financial_institution": 30,
        "registry_projection": 20,
        "cached_projection": 20,
        "recurrence_prediction": 10,
        "model_prediction": 10,
        "inference": 5,
    },
    "financial_balance": {
        "user_explicit": 120,
        "financial_institution": 100,
        "provider": 80,
        "registry_projection": 20,
        "cached_projection": 20,
        "recurrence_prediction": 10,
        "model_prediction": 10,
        "inference": 5,
    },
    "financial_settlement": {
        "user_explicit": 120,
        "financial_institution": 100,
        "provider": 85,
        "registry_projection": 20,
        "cached_projection": 20,
        "recurrence_prediction": 10,
        "model_prediction": 10,
        "inference": 5,
    },
    "automation_runtime": {
        "user_explicit": 120,
        "scheduler_runtime": 100,
        "registry_projection": 20,
        "cached_projection": 20,
        "inference": 5,
    },
    "github": {
        "user_explicit": 120,
        "github": 100,
        "registry_projection": 20,
        "cached_projection": 20,
        "model_prediction": 10,
        "inference": 5,
    },
    "calendar": {
        "user_explicit": 120,
        "calendar": 100,
        "registry_projection": 20,
        "cached_projection": 20,
        "model_prediction": 10,
        "inference": 5,
    },
}
DEFAULT_RANKS = {
    "user_explicit": 120,
    "policy": 115,
    "provider": 90,
    "counterparty": 90,
    "financial_institution": 90,
    "scheduler_runtime": 90,
    "github": 90,
    "calendar": 90,
    "registry_projection": 20,
    "cached_projection": 20,
    "recurrence_prediction": 10,
    "model_prediction": 10,
    "inference": 5,
}
PREDICTIVE_SOURCES = {"recurrence_prediction", "model_prediction", "inference"}
EXPIRABLE_STORED_STATUSES = {"VERIFIED", "PROBABLE", "UNKNOWN"}
BLOCKING_EFFECTIVE_STATUSES = {"STALE", "UNKNOWN", "CONFLICTED"}


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/Chicago")
    except Exception as exc:  # pragma: no cover - platform zoneinfo failure
        raise PreflightError(f"invalid timezone {name!r}: {exc}") from exc


def parse_timestamp(value: Any, *, default_timezone: str = "America/Chicago") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise PreflightError("empty timestamp")
        if len(text) == 10:
            try:
                d = date.fromisoformat(text)
            except ValueError as exc:
                raise PreflightError(f"invalid date timestamp {value!r}") from exc
            parsed = datetime.combine(d, time(23, 59, 59, 999999), tzinfo=_tz(default_timezone))
        else:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError as exc:
                raise PreflightError(f"invalid timestamp {value!r}") from exc
    else:
        raise PreflightError(f"timestamp must be ISO-8601 text, got {type(value).__name__}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_tz(default_timezone))
    return parsed.astimezone(timezone.utc)


def normalize_value(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def source_rank(data_type: str, source_kind: str) -> int:
    return AUTHORITY_RANKS.get(data_type, DEFAULT_RANKS).get(source_kind, 0)


def effective_state_status(
    state: Mapping[str, Any],
    *,
    now: datetime,
    default_timezone: str,
) -> str:
    stored = str(state.get("status", "UNKNOWN")).upper()
    if stored not in EXPIRABLE_STORED_STATUSES:
        return stored
    stale_after = state.get("stale_after")
    if not stale_after:
        return stored
    expiry = parse_timestamp(stale_after, default_timezone=default_timezone)
    return "STALE" if expiry <= now.astimezone(timezone.utc) else stored


def _journal_ids(state: Mapping[str, Any]) -> tuple[str, str, str]:
    material = f"{state.get('state_id','')}|{state.get('stale_after','')}".encode("utf-8")
    suffix = hashlib.sha256(material).hexdigest()[:16]
    return (
        f"EV.RUNTIME.STALE.{suffix}",
        f"AUD.RUNTIME.STALE.{suffix}",
        f"runtime:stale:{suffix}",
    )


def append_stale_transition_journal(
    path: Path,
    states: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    workflow_id: str,
) -> int:
    """Append idempotent Evidence/Audit records for effective stale transitions."""

    if not states:
        return 0
    existing_keys: set[str] = set()
    if path.exists():
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                if not raw_line.strip():
                    continue
                item = json.loads(raw_line)
                key = item.get("idempotency_key")
                if key:
                    existing_keys.add(str(key))
        except (OSError, json.JSONDecodeError) as exc:
            raise PreflightError(f"cannot read append-only stale journal {path}: {exc}") from exc

    records: list[dict[str, Any]] = []
    stamp = now.astimezone(timezone.utc).isoformat()
    for state in states:
        evidence_id, audit_id, idem = _journal_ids(state)
        if idem in existing_keys:
            continue
        state_id = str(state.get("state_id", ""))
        canonical_key = str(state.get("canonical_key", ""))
        records.extend(
            [
                {
                    "record_type": "Evidence",
                    "evidence_id": evidence_id,
                    "timestamp": stamp,
                    "workflow_id": workflow_id,
                    "state_id": state_id,
                    "canonical_key": canonical_key,
                    "observation": "effective stale_after expiration detected by runtime preflight",
                    "stored_status": str(state.get("status", "UNKNOWN")).upper(),
                    "effective_status": "STALE",
                    "stale_after": state.get("stale_after"),
                    "idempotency_key": idem,
                },
                {
                    "record_type": "Audit",
                    "event_id": audit_id,
                    "timestamp": stamp,
                    "workflow_id": workflow_id,
                    "event_type": "EFFECTIVE_STALE_TRANSITION",
                    "state_id": state_id,
                    "canonical_key": canonical_key,
                    "prior_state": str(state.get("status", "UNKNOWN")).upper(),
                    "new_state": "STALE",
                    "evidence_refs": [evidence_id],
                    "result": "LOCAL_APPEND_ONLY_PROVENANCE_PERSISTED_CANONICAL_WRITEBACK_PENDING",
                    "idempotency_key": idem,
                },
            ]
        )
        existing_keys.add(idem)

    if not records:
        return 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
    except OSError as exc:
        raise PreflightError(f"cannot append stale transition journal {path}: {exc}") from exc
    return len(records) // 2


def _registry_state_candidates(states: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for state in states:
        key = state.get("canonical_key")
        if not key:
            continue
        facts.append(
            {
                "canonical_key": key,
                "data_type": state.get("authority_data_type", state.get("data_type", "generic")),
                "source_kind": "registry_projection",
                "value": state.get("value"),
                "observed_at": state.get("observed_at"),
                "state_id": state.get("state_id"),
                "terminal": bool(state.get("terminal", False)),
                "status": str(state.get("status", "UNKNOWN")).upper(),
            }
        )
    return facts


def resolve_authoritative_fact(
    canonical_key: str,
    candidates: Sequence[Mapping[str, Any]],
    *,
    default_timezone: str,
) -> tuple[dict[str, Any] | None, list[Finding]]:
    relevant = [dict(item) for item in candidates if item.get("canonical_key") == canonical_key]
    if not relevant:
        return None, [
            Finding(
                "MISSING_AUTHORITY_EVIDENCE",
                "BLOCK",
                f"no evidence candidates exist for required fact {canonical_key}",
                canonical_key=canonical_key,
            )
        ]

    data_types = {str(item.get("data_type", "generic")) for item in relevant}
    if len(data_types) > 1:
        return None, [
            Finding(
                "AUTHORITY_DATA_TYPE_CONFLICT",
                "BLOCK",
                f"candidates for {canonical_key} disagree on data_type: {sorted(data_types)}",
                canonical_key=canonical_key,
            )
        ]
    data_type = next(iter(data_types))
    ranked = [(source_rank(data_type, str(item.get("source_kind", ""))), item) for item in relevant]
    top_rank = max(rank for rank, _ in ranked)
    top = [item for rank, item in ranked if rank == top_rank]
    top_values = {normalize_value(item.get("value")) for item in top}
    if len(top_values) > 1:
        return None, [
            Finding(
                "AUTHORITATIVE_CONFLICT",
                "BLOCK",
                f"equally authoritative sources disagree for {canonical_key}",
                canonical_key=canonical_key,
                details={"sources": [item.get("source_kind") for item in top]},
            )
        ]

    def observed_sort(item: Mapping[str, Any]) -> datetime:
        raw = item.get("observed_at")
        if not raw:
            return datetime.min.replace(tzinfo=timezone.utc)
        return parse_timestamp(raw, default_timezone=default_timezone)

    chosen = max(top, key=observed_sort)
    findings: list[Finding] = []
    chosen_value = normalize_value(chosen.get("value"))
    for item in relevant:
        if item is chosen:
            continue
        if normalize_value(item.get("value")) == chosen_value:
            continue
        source = str(item.get("source_kind", ""))
        if source in PREDICTIVE_SOURCES:
            findings.append(
                Finding(
                    "PREDICTION_DEMOTED",
                    "INFO",
                    f"{source} contradicted stronger {chosen.get('source_kind')} evidence for {canonical_key}",
                    canonical_key=canonical_key,
                )
            )
        elif source in {"registry_projection", "cached_projection"}:
            findings.append(
                Finding(
                    "REGISTRY_AUTHORITY_DRIFT",
                    "BLOCK",
                    f"cached/registry value for {canonical_key} differs from authoritative {chosen.get('source_kind')} value",
                    canonical_key=canonical_key,
                    state_id=item.get("state_id"),
                    details={"authoritative_value": chosen.get("value"), "cached_value": item.get("value")},
                )
            )

    registry_items = [item for item in relevant if item.get("source_kind") == "registry_projection"]
    for registry in registry_items:
        if not registry.get("terminal"):
            continue
        if normalize_value(registry.get("value")) == chosen_value:
            continue
        chosen_at = observed_sort(chosen)
        registry_at = observed_sort(registry)
        if chosen_at > registry_at and source_rank(data_type, str(chosen.get("source_kind", ""))) > source_rank(
            data_type, "registry_projection"
        ):
            findings.append(
                Finding(
                    "LATE_HIGHER_AUTHORITY_REOPEN",
                    "BLOCK",
                    f"newer higher-authority evidence reopens terminal result for {canonical_key}",
                    canonical_key=canonical_key,
                    state_id=registry.get("state_id"),
                )
            )

    return chosen, findings


def diff_automations(
    registry_rows: Sequence[Mapping[str, Any]],
    live_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    registry = {str(row.get("automation_id")): dict(row) for row in registry_rows if row.get("automation_id")}
    live = {str(row.get("id") or row.get("automation_id")): dict(row) for row in live_rows if row.get("id") or row.get("automation_id")}

    missing_in_registry = sorted(set(live) - set(registry))
    enabled_orphans = sorted(
        automation_id
        for automation_id in set(registry) - set(live)
        if bool(registry[automation_id].get("enabled"))
    )
    field_drift: list[dict[str, Any]] = []
    fields = (
        ("enabled", "is_enabled"),
        ("timing", "schedule"),
        ("notifications_enabled", "notifications_enabled"),
        ("email_enabled", "email_enabled"),
    )
    for automation_id in sorted(set(registry) & set(live)):
        reg = registry[automation_id]
        current = live[automation_id]
        for registry_field, live_field in fields:
            if registry_field not in reg and live_field not in current:
                continue
            if reg.get(registry_field) != current.get(live_field):
                field_drift.append(
                    {
                        "automation_id": automation_id,
                        "field": registry_field,
                        "registry": reg.get(registry_field),
                        "live": current.get(live_field),
                    }
                )

    delivery_claims = []
    for row in registry_rows:
        if row.get("delivery_verified_from_last_run"):
            delivery_claims.append(str(row.get("automation_id", "")))

    return {
        "missing_in_registry": missing_in_registry,
        "enabled_orphans": enabled_orphans,
        "field_drift": field_drift,
        "invalid_delivery_claims": sorted(item for item in delivery_claims if item),
        "has_drift": bool(missing_in_registry or enabled_orphans or field_drift or delivery_claims),
    }


def primary_wip_count(bundle: Mapping[str, Any]) -> int:
    portfolio = bundle.get("registry", {}).get("portfolio", {})
    primary = portfolio.get("primary_workflow_id")
    if primary:
        return 1
    states = bundle.get("registry", {}).get("states", [])
    return sum(1 for state in states if state.get("canonical_key") == "closure:primary")


def run_preflight(
    bundle: Mapping[str, Any],
    *,
    now: datetime | None = None,
    stale_journal_path: Path | None = None,
) -> PreflightReport:
    request = dict(bundle.get("request") or {})
    workflow_id = str(request.get("workflow_id") or "UNKNOWN_WORKFLOW")
    consequential = bool(request.get("consequential", True))
    timezone_name = str(bundle.get("default_timezone") or "America/Chicago")
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_tz(timezone_name))
    now = now.astimezone(timezone.utc)

    registry = dict(bundle.get("registry") or {})
    states = list(registry.get("states") or [])
    report = PreflightReport(workflow_id=workflow_id, allowed=True)
    report.primary_wip_count = primary_wip_count(bundle)

    expired_transition_states: list[Mapping[str, Any]] = []
    state_by_key: dict[str, Mapping[str, Any]] = {}
    for state in states:
        state_id = str(state.get("state_id") or state.get("canonical_key") or "UNKNOWN_STATE")
        key = str(state.get("canonical_key") or "")
        if key:
            state_by_key[key] = state
        try:
            effective = effective_state_status(state, now=now, default_timezone=timezone_name)
        except PreflightError as exc:
            effective = "UNKNOWN"
            report.findings.append(
                Finding(
                    "INVALID_STALE_AFTER",
                    "BLOCK" if consequential else "WARN",
                    str(exc),
                    canonical_key=key or None,
                    state_id=state_id,
                )
            )
        report.effective_state_status[state_id] = effective
        if effective == "STALE" and str(state.get("status", "UNKNOWN")).upper() != "STALE":
            expired_transition_states.append(state)

    if expired_transition_states:
        if stale_journal_path is None:
            report.findings.append(
                Finding(
                    "STALE_PERSISTENCE_UNAVAILABLE",
                    "BLOCK" if consequential else "WARN",
                    "effective stale transitions detected but no append-only Evidence/Audit journal is configured",
                    details={"count": len(expired_transition_states)},
                )
            )
        else:
            try:
                report.stale_events_persisted = append_stale_transition_journal(
                    stale_journal_path,
                    expired_transition_states,
                    now=now,
                    workflow_id=workflow_id,
                )
            except PreflightError as exc:
                report.findings.append(
                    Finding(
                        "STALE_PERSISTENCE_FAILED",
                        "BLOCK",
                        str(exc),
                        details={"count": len(expired_transition_states)},
                    )
                )

    required_state_keys = [str(item) for item in request.get("required_state_keys", [])]
    for key in required_state_keys:
        state = state_by_key.get(key)
        if state is None:
            report.findings.append(
                Finding(
                    "MISSING_REQUIRED_STATE",
                    "BLOCK",
                    f"required JACS State row is missing: {key}",
                    canonical_key=key,
                )
            )
            continue
        state_id = str(state.get("state_id") or key)
        effective = report.effective_state_status.get(state_id, "UNKNOWN")
        if effective in BLOCKING_EFFECTIVE_STATUSES:
            report.findings.append(
                Finding(
                    f"REQUIRED_STATE_{effective}",
                    "BLOCK",
                    f"required state {key} has effective status {effective}",
                    canonical_key=key,
                    state_id=state_id,
                )
            )

    candidates = _registry_state_candidates(states) + [dict(item) for item in bundle.get("facts", [])]
    required_fact_keys = [str(item) for item in request.get("required_fact_keys", [])]
    for key in required_fact_keys:
        try:
            chosen, findings = resolve_authoritative_fact(
                key,
                candidates,
                default_timezone=timezone_name,
            )
        except PreflightError as exc:
            chosen = None
            findings = [Finding("INVALID_AUTHORITY_EVIDENCE", "BLOCK", str(exc), canonical_key=key)]
        report.findings.extend(findings)
        if chosen is not None:
            report.resolved_facts[key] = chosen

    live_automations = list(bundle.get("live", {}).get("automations") or [])
    registry_automations = list(registry.get("automations") or [])
    report.automation_diff = diff_automations(registry_automations, live_automations)
    if report.automation_diff.get("invalid_delivery_claims"):
        report.findings.append(
            Finding(
                "NOTIFICATION_RECEIPT_UNPROVEN",
                "BLOCK" if request.get("require_notification_receipt") else "WARN",
                "scheduler last_run/configuration cannot prove user receipt",
                details={"automation_ids": report.automation_diff["invalid_delivery_claims"]},
            )
        )
    if request.get("require_automation_sync") and report.automation_diff.get("has_drift"):
        report.findings.append(
            Finding(
                "AUTOMATION_RUNTIME_DRIFT",
                "BLOCK",
                "live scheduler differs from JACS automation projection",
                details=report.automation_diff,
            )
        )

    if report.primary_wip_count > 1:
        report.findings.append(
            Finding(
                "PRIMARY_WIP_VIOLATION",
                "BLOCK",
                f"primary execution WIP is {report.primary_wip_count}, expected at most 1",
            )
        )

    if consequential:
        report.allowed = not any(item.severity == "BLOCK" for item in report.findings)
    return report


def load_bundle(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PreflightError(f"cannot read JACS preflight snapshot {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PreflightError(f"invalid JACS preflight snapshot {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PreflightError("JACS preflight snapshot root must be an object")
    return payload


def run_preflight_from_file(
    path: Path,
    *,
    stale_journal_path: Path | None = None,
    now: datetime | None = None,
) -> PreflightReport:
    return run_preflight(load_bundle(path), now=now, stale_journal_path=stale_journal_path)
