#!/usr/bin/env python3
"""Fail-closed JACS freshness, authority, dependency, and automation preflight.

The Fleet host cannot call ChatGPT connectors directly. It consumes a versioned,
connector-produced JSON snapshot whose envelope binds freshness, declared reads,
source provenance, and a deterministic content digest. The runtime never upgrades
UNKNOWN/CONFLICTED by inference and never mutates external providers or scheduler
state. Effective stale transitions and accepted snapshot IDs are persisted only in
local append-only provenance journals; canonical JACS/Google-Sheets writeback is a
separate reconciliation step.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


SNAPSHOT_SCHEMA_VERSION = 2
MAX_SNAPSHOT_AGE_SECONDS = 24 * 60 * 60
MAX_FUTURE_SKEW_SECONDS = 60
SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


class PreflightError(RuntimeError):
    """Raised for malformed, undeclared, unavailable, or unsafe preflight inputs."""


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
    snapshot_id: str
    allowed: bool
    findings: list[Finding] = field(default_factory=list)
    resolved_facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    effective_state_status: dict[str, str] = field(default_factory=dict)
    automation_diff: dict[str, Any] = field(default_factory=dict)
    primary_wip_count: int = 0
    stale_events_persisted: int = 0
    snapshot_receipt_persisted: bool = False

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
REGISTERED_SOURCE_KINDS = frozenset(DEFAULT_RANKS)
PREDICTIVE_SOURCES = {"recurrence_prediction", "model_prediction", "inference"}
EXPIRABLE_STORED_STATUSES = {"VERIFIED", "PROBABLE", "UNKNOWN"}
BLOCKING_EFFECTIVE_STATUSES = {"STALE", "UNKNOWN", "CONFLICTED"}
ENVELOPE_KEYS = {
    "schema_version",
    "snapshot_id",
    "generated_at",
    "expires_at",
    "max_age_seconds",
    "authoritative_source_manifest",
    "required_state_keys",
    "required_fact_keys",
    "content_digest",
}
MANIFEST_ENTRY_KEYS = {"source_kind", "data_types", "observed_at", "source_ref"}


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/Chicago")
    except Exception as exc:
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
                parsed = datetime.combine(
                    date.fromisoformat(text),
                    time(23, 59, 59, 999999),
                    tzinfo=_tz(default_timezone),
                )
            except ValueError as exc:
                raise PreflightError(f"invalid date timestamp {value!r}") from exc
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


def _canonical_snapshot_payload(bundle: Mapping[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(bundle))
    envelope = payload.get("envelope")
    if isinstance(envelope, dict):
        envelope.pop("content_digest", None)
    return payload


def compute_content_digest(bundle: Mapping[str, Any]) -> str:
    material = normalize_value(_canonical_snapshot_payload(bundle)).encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def source_rank(data_type: str, source_kind: str) -> int:
    if source_kind not in REGISTERED_SOURCE_KINDS:
        raise PreflightError(f"unregistered source_kind {source_kind!r}")
    return AUTHORITY_RANKS.get(data_type, DEFAULT_RANKS).get(source_kind, 0)


def _require_unique_string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise PreflightError(f"{field_name} must be a list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise PreflightError(f"{field_name} must contain non-empty strings")
    normalized = [item.strip() for item in value]
    if len(set(normalized)) != len(normalized):
        raise PreflightError(f"{field_name} contains duplicates")
    return normalized


def validate_snapshot_envelope(
    bundle: Mapping[str, Any],
    *,
    now: datetime,
    default_timezone: str,
) -> dict[str, Any]:
    envelope = bundle.get("envelope")
    if not isinstance(envelope, dict):
        raise PreflightError("snapshot envelope is required")
    if set(envelope) != ENVELOPE_KEYS:
        missing = sorted(ENVELOPE_KEYS - set(envelope))
        extra = sorted(set(envelope) - ENVELOPE_KEYS)
        raise PreflightError(f"snapshot envelope keys mismatch: missing={missing} extra={extra}")
    if envelope.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise PreflightError(
            f"unsupported snapshot schema_version {envelope.get('schema_version')!r}; "
            f"expected {SNAPSHOT_SCHEMA_VERSION}"
        )
    snapshot_id = envelope.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise PreflightError("snapshot_id must be 8-128 safe identifier characters")
    generated_at = parse_timestamp(envelope.get("generated_at"), default_timezone=default_timezone)
    expires_at = parse_timestamp(envelope.get("expires_at"), default_timezone=default_timezone)
    max_age = envelope.get("max_age_seconds")
    if isinstance(max_age, bool) or not isinstance(max_age, int) or not 1 <= max_age <= MAX_SNAPSHOT_AGE_SECONDS:
        raise PreflightError(
            f"max_age_seconds must be an integer from 1 to {MAX_SNAPSHOT_AGE_SECONDS}"
        )
    if expires_at <= generated_at:
        raise PreflightError("expires_at must be later than generated_at")
    if expires_at > generated_at + timedelta(seconds=max_age):
        raise PreflightError("expires_at exceeds generated_at + max_age_seconds")
    if generated_at > now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        raise PreflightError("snapshot generated_at is future-dated beyond allowed clock skew")
    if now > expires_at:
        raise PreflightError("snapshot envelope has expired")
    if now - generated_at > timedelta(seconds=max_age):
        raise PreflightError("snapshot age exceeds max_age_seconds")

    required_state_keys = _require_unique_string_list(
        envelope.get("required_state_keys"), "envelope.required_state_keys"
    )
    required_fact_keys = _require_unique_string_list(
        envelope.get("required_fact_keys"), "envelope.required_fact_keys"
    )
    expected_digest = compute_content_digest(bundle)
    supplied_digest = envelope.get("content_digest")
    if not isinstance(supplied_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", supplied_digest):
        raise PreflightError("content_digest must be sha256:<64 lowercase hex characters>")
    if supplied_digest != expected_digest:
        raise PreflightError("snapshot content_digest does not match canonical snapshot content")

    manifest = envelope.get("authoritative_source_manifest")
    if not isinstance(manifest, list) or not manifest:
        raise PreflightError("authoritative_source_manifest must be a non-empty list")
    manifest_map: dict[str, dict[str, Any]] = {}
    for raw in manifest:
        if not isinstance(raw, dict) or set(raw) != MANIFEST_ENTRY_KEYS:
            raise PreflightError(
                "each authoritative_source_manifest entry must contain "
                "source_kind/data_types/observed_at/source_ref"
            )
        source_kind = raw.get("source_kind")
        if not isinstance(source_kind, str) or source_kind not in REGISTERED_SOURCE_KINDS:
            raise PreflightError(f"unregistered source_kind in manifest: {source_kind!r}")
        if source_kind in manifest_map:
            raise PreflightError(f"duplicate source_kind in manifest: {source_kind}")
        data_types = _require_unique_string_list(raw.get("data_types"), f"manifest[{source_kind}].data_types")
        observed_at = parse_timestamp(raw.get("observed_at"), default_timezone=default_timezone)
        if observed_at > generated_at + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
            raise PreflightError(f"manifest source {source_kind} observed_at is later than snapshot generation")
        source_ref = raw.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise PreflightError(f"manifest source {source_kind} requires non-empty source_ref")
        manifest_map[source_kind] = {
            "source_kind": source_kind,
            "data_types": data_types,
            "observed_at": observed_at,
            "source_ref": source_ref.strip(),
        }

    facts = bundle.get("facts") or []
    if not isinstance(facts, list):
        raise PreflightError("facts must be a list")
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise PreflightError(f"facts[{index}] must be an object")
        source_kind = fact.get("source_kind")
        data_type = str(fact.get("data_type", "generic"))
        if not isinstance(source_kind, str) or source_kind not in REGISTERED_SOURCE_KINDS:
            raise PreflightError(f"facts[{index}] has unregistered source_kind {source_kind!r}")
        entry = manifest_map.get(source_kind)
        if entry is None:
            raise PreflightError(f"facts[{index}] source_kind {source_kind} is absent from source manifest")
        if data_type not in entry["data_types"]:
            raise PreflightError(
                f"facts[{index}] data_type {data_type!r} is not declared for source {source_kind}"
            )
        fact_at = fact.get("observed_at")
        if fact_at:
            observed = parse_timestamp(fact_at, default_timezone=default_timezone)
            if observed > generated_at + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
                raise PreflightError(f"facts[{index}] observed_at is future-dated beyond snapshot generation")

    live_automations = (bundle.get("live") or {}).get("automations") or []
    if live_automations:
        scheduler = manifest_map.get("scheduler_runtime")
        if scheduler is None or "automation_runtime" not in scheduler["data_types"]:
            raise PreflightError(
                "live automations require scheduler_runtime/automation_runtime in source manifest"
            )

    request = bundle.get("request")
    if not isinstance(request, dict):
        raise PreflightError("request must be an object")
    declared_state_reads = _require_unique_string_list(
        request.get("declared_state_reads", []), "request.declared_state_reads"
    )
    declared_fact_reads = _require_unique_string_list(
        request.get("declared_fact_reads", []), "request.declared_fact_reads"
    )
    if set(declared_state_reads) != set(required_state_keys):
        raise PreflightError(
            "request.declared_state_reads must exactly match envelope.required_state_keys"
        )
    if set(declared_fact_reads) != set(required_fact_keys):
        raise PreflightError(
            "request.declared_fact_reads must exactly match envelope.required_fact_keys"
        )

    return {
        "snapshot_id": snapshot_id,
        "generated_at": generated_at,
        "expires_at": expires_at,
        "max_age_seconds": max_age,
        "required_state_keys": required_state_keys,
        "required_fact_keys": required_fact_keys,
        "manifest": manifest_map,
        "content_digest": supplied_digest,
    }


def assert_dependency_declared(bundle: Mapping[str, Any], kind: str, canonical_key: str) -> None:
    """Guard a runtime read so undeclared JACS dependencies fail closed."""
    envelope = bundle.get("envelope") or {}
    field_name = {"state": "required_state_keys", "fact": "required_fact_keys"}.get(kind)
    if field_name is None:
        raise PreflightError(f"unsupported dependency kind {kind!r}")
    declared = envelope.get(field_name)
    if not isinstance(declared, list) or canonical_key not in declared:
        raise PreflightError(f"undeclared {kind} dependency read refused: {canonical_key}")


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


def _read_jsonl(path: Path, description: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            item = json.loads(raw_line)
            if not isinstance(item, dict):
                raise PreflightError(f"{description} contains a non-object record")
            rows.append(item)
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"cannot read {description} {path}: {exc}") from exc
    return rows


def _append_jsonl(path: Path, records: Sequence[Mapping[str, Any]], description: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(dict(record), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
    except OSError as exc:
        raise PreflightError(f"cannot append {description} {path}: {exc}") from exc


def append_stale_transition_journal(
    path: Path,
    states: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    workflow_id: str,
    snapshot_id: str,
) -> int:
    if not states:
        return 0
    existing_keys = {
        str(item.get("idempotency_key"))
        for item in _read_jsonl(path, "append-only stale journal")
        if item.get("idempotency_key")
    }
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
                    "snapshot_id": snapshot_id,
                    "state_id": state_id,
                    "canonical_key": canonical_key,
                    "observation": "effective stale_after expiration detected by runtime preflight",
                    "stored_status": str(state.get("status", "UNKNOWN")).upper(),
                    "effective_status": "STALE",
                    "stale_after": state.get("stale_after"),
                    "canonical_writeback": "PENDING_EXTERNAL_RECONCILIATION",
                    "idempotency_key": idem,
                },
                {
                    "record_type": "Audit",
                    "event_id": audit_id,
                    "timestamp": stamp,
                    "workflow_id": workflow_id,
                    "snapshot_id": snapshot_id,
                    "event_type": "EFFECTIVE_STALE_TRANSITION",
                    "state_id": state_id,
                    "canonical_key": canonical_key,
                    "prior_state": str(state.get("status", "UNKNOWN")).upper(),
                    "new_state": "STALE",
                    "evidence_refs": [evidence_id],
                    "result": "LOCAL_APPEND_ONLY_PROVENANCE_ONLY_CANONICAL_WRITEBACK_PENDING",
                    "idempotency_key": idem,
                },
            ]
        )
        existing_keys.add(idem)
    if records:
        _append_jsonl(path, records, "append-only stale journal")
    return len(records) // 2


def check_snapshot_replay(path: Path, snapshot_id: str, content_digest: str) -> Finding | None:
    for item in _read_jsonl(path, "snapshot replay journal"):
        if item.get("snapshot_id") != snapshot_id:
            continue
        if item.get("content_digest") == content_digest:
            return Finding(
                "SNAPSHOT_REPLAY",
                "BLOCK",
                f"snapshot_id {snapshot_id} has already been accepted for consequential use",
            )
        return Finding(
            "SNAPSHOT_ID_COLLISION",
            "BLOCK",
            f"snapshot_id {snapshot_id} was previously recorded with different content",
        )
    return None


def append_snapshot_receipt(
    path: Path,
    *,
    snapshot_id: str,
    content_digest: str,
    workflow_id: str,
    now: datetime,
) -> None:
    _append_jsonl(
        path,
        [
            {
                "record_type": "SnapshotReceipt",
                "snapshot_id": snapshot_id,
                "content_digest": content_digest,
                "workflow_id": workflow_id,
                "accepted_at": now.astimezone(timezone.utc).isoformat(),
                "canonical_writeback": "NOT_APPLICABLE_LOCAL_REPLAY_PROVENANCE",
            }
        ],
        "snapshot replay journal",
    )


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
        return None, [Finding(
            "MISSING_AUTHORITY_EVIDENCE", "BLOCK",
            f"no evidence candidates exist for required fact {canonical_key}",
            canonical_key=canonical_key,
        )]
    data_types = {str(item.get("data_type", "generic")) for item in relevant}
    if len(data_types) > 1:
        return None, [Finding(
            "AUTHORITY_DATA_TYPE_CONFLICT", "BLOCK",
            f"candidates for {canonical_key} disagree on data_type: {sorted(data_types)}",
            canonical_key=canonical_key,
        )]
    data_type = next(iter(data_types))
    ranked = []
    for item in relevant:
        source_kind = str(item.get("source_kind", ""))
        ranked.append((source_rank(data_type, source_kind), item))
    top_rank = max(rank for rank, _ in ranked)
    top = [item for rank, item in ranked if rank == top_rank]
    top_values = {normalize_value(item.get("value")) for item in top}
    if len(top_values) > 1:
        return None, [Finding(
            "AUTHORITATIVE_CONFLICT", "BLOCK",
            f"equally authoritative sources disagree for {canonical_key}",
            canonical_key=canonical_key,
            details={"sources": [item.get("source_kind") for item in top]},
        )]

    def observed_sort(item: Mapping[str, Any]) -> datetime:
        raw = item.get("observed_at")
        if not raw:
            return datetime.min.replace(tzinfo=timezone.utc)
        return parse_timestamp(raw, default_timezone=default_timezone)

    chosen = max(top, key=observed_sort)
    findings: list[Finding] = []
    chosen_value = normalize_value(chosen.get("value"))
    for item in relevant:
        if item is chosen or normalize_value(item.get("value")) == chosen_value:
            continue
        source = str(item.get("source_kind", ""))
        if source in PREDICTIVE_SOURCES:
            findings.append(Finding(
                "PREDICTION_DEMOTED", "INFO",
                f"{source} contradicted stronger {chosen.get('source_kind')} evidence for {canonical_key}",
                canonical_key=canonical_key,
            ))
        elif source in {"registry_projection", "cached_projection"}:
            findings.append(Finding(
                "REGISTRY_AUTHORITY_DRIFT", "BLOCK",
                f"cached/registry value for {canonical_key} differs from authoritative {chosen.get('source_kind')} value",
                canonical_key=canonical_key,
                state_id=item.get("state_id"),
                details={"authoritative_value": chosen.get("value"), "cached_value": item.get("value")},
            ))

    for registry in [item for item in relevant if item.get("source_kind") == "registry_projection"]:
        if not registry.get("terminal") or normalize_value(registry.get("value")) == chosen_value:
            continue
        if (
            observed_sort(chosen) > observed_sort(registry)
            and source_rank(data_type, str(chosen.get("source_kind", "")))
            > source_rank(data_type, "registry_projection")
        ):
            findings.append(Finding(
                "LATE_HIGHER_AUTHORITY_REOPEN", "BLOCK",
                f"newer higher-authority evidence reopens terminal result for {canonical_key}",
                canonical_key=canonical_key,
                state_id=registry.get("state_id"),
            ))
    return chosen, findings


def _normalize_rrule(line: str) -> str:
    prefix, sep, body = line.partition(":")
    if not sep:
        return line.strip()
    components = [part.strip().upper() for part in body.split(";") if part.strip()]
    return prefix.strip().upper() + ":" + ";".join(sorted(components))


def normalize_schedule(value: Any) -> str:
    """Normalize common iCalendar/dict schedule representations for drift checks."""
    if value is None:
        return "null"
    if isinstance(value, str):
        lines = [line.strip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]
        normalized: list[str] = []
        for line in lines:
            upper = line.upper()
            if upper.startswith("RRULE:"):
                normalized.append(_normalize_rrule(line))
            elif upper.startswith(("BEGIN:", "END:", "DTSTART", "DTEND", "EXDATE", "RDATE")):
                name, sep, body = line.partition(":")
                normalized.append(name.strip().upper() + (":" + body.strip() if sep else ""))
            else:
                normalized.append(line.strip())
        return "\n".join(normalized)
    if isinstance(value, Mapping):
        return normalize_value({str(k): value[k] for k in sorted(value)})
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return normalize_value(list(value))
    return normalize_value(value)


def diff_automations(
    registry_rows: Sequence[Mapping[str, Any]],
    live_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    registry = {str(row.get("automation_id")): dict(row) for row in registry_rows if row.get("automation_id")}
    live = {str(row.get("id") or row.get("automation_id")): dict(row) for row in live_rows if row.get("id") or row.get("automation_id")}
    missing_in_registry = sorted(set(live) - set(registry))
    extra_in_registry = sorted(set(registry) - set(live))
    field_drift: list[dict[str, Any]] = []
    fields = (
        ("enabled", "is_enabled", False),
        ("timing", "schedule", True),
        ("notifications_enabled", "notifications_enabled", False),
        ("email_enabled", "email_enabled", False),
    )
    for automation_id in sorted(set(registry) & set(live)):
        reg = registry[automation_id]
        current = live[automation_id]
        for registry_field, live_field, is_schedule in fields:
            if registry_field not in reg and live_field not in current:
                continue
            left = reg.get(registry_field)
            right = current.get(live_field)
            if is_schedule:
                left = normalize_schedule(left)
                right = normalize_schedule(right)
            if left != right:
                field_drift.append({
                    "automation_id": automation_id,
                    "field": registry_field,
                    "registry": reg.get(registry_field),
                    "live": current.get(live_field),
                })
    delivery_claims = sorted(
        str(row.get("automation_id"))
        for row in registry_rows
        if row.get("automation_id") and row.get("delivery_verified_from_last_run")
    )
    return {
        "missing_in_registry": missing_in_registry,
        "extra_in_registry": extra_in_registry,
        "enabled_orphans": sorted(
            automation_id for automation_id in extra_in_registry
            if bool(registry[automation_id].get("enabled"))
        ),
        "field_drift": field_drift,
        "invalid_delivery_claims": delivery_claims,
        "has_drift": bool(missing_in_registry or extra_in_registry or field_drift or delivery_claims),
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
    replay_journal_path: Path | None = None,
) -> PreflightReport:
    timezone_name = str(bundle.get("default_timezone") or "America/Chicago")
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_tz(timezone_name))
    now = now.astimezone(timezone.utc)
    envelope_info = validate_snapshot_envelope(
        bundle, now=now, default_timezone=timezone_name
    )
    request = dict(bundle.get("request") or {})
    workflow_id = str(request.get("workflow_id") or "UNKNOWN_WORKFLOW")
    if workflow_id == "UNKNOWN_WORKFLOW":
        raise PreflightError("request.workflow_id is required")
    consequential = bool(request.get("consequential", True))
    report = PreflightReport(
        workflow_id=workflow_id,
        snapshot_id=envelope_info["snapshot_id"],
        allowed=True,
    )
    report.primary_wip_count = primary_wip_count(bundle)

    if replay_journal_path is None:
        if consequential:
            report.findings.append(Finding(
                "SNAPSHOT_REPLAY_STORE_UNAVAILABLE", "BLOCK",
                "consequential preflight requires an append-only snapshot replay journal",
            ))
    else:
        try:
            replay = check_snapshot_replay(
                replay_journal_path,
                envelope_info["snapshot_id"],
                envelope_info["content_digest"],
            )
            if replay:
                report.findings.append(replay)
        except PreflightError as exc:
            report.findings.append(Finding("SNAPSHOT_REPLAY_CHECK_FAILED", "BLOCK", str(exc)))

    registry = dict(bundle.get("registry") or {})
    states = list(registry.get("states") or [])
    expired_transition_states: list[Mapping[str, Any]] = []
    state_by_key: dict[str, Mapping[str, Any]] = {}
    for state in states:
        if not isinstance(state, Mapping):
            report.findings.append(Finding("INVALID_STATE_ROW", "BLOCK", "registry state row is not an object"))
            continue
        state_id = str(state.get("state_id") or state.get("canonical_key") or "UNKNOWN_STATE")
        key = str(state.get("canonical_key") or "")
        if key:
            if key in state_by_key:
                report.findings.append(Finding(
                    "DUPLICATE_STATE_KEY", "BLOCK",
                    f"multiple registry State rows share canonical_key {key}", canonical_key=key,
                ))
            state_by_key[key] = state
        try:
            effective = effective_state_status(state, now=now, default_timezone=timezone_name)
        except PreflightError as exc:
            effective = "UNKNOWN"
            report.findings.append(Finding(
                "INVALID_STALE_AFTER", "BLOCK" if consequential else "WARN", str(exc),
                canonical_key=key or None, state_id=state_id,
            ))
        report.effective_state_status[state_id] = effective
        if effective == "STALE" and str(state.get("status", "UNKNOWN")).upper() != "STALE":
            expired_transition_states.append(state)

    if expired_transition_states:
        if stale_journal_path is None:
            report.findings.append(Finding(
                "STALE_PERSISTENCE_UNAVAILABLE", "BLOCK" if consequential else "WARN",
                "effective stale transitions detected but no append-only Evidence/Audit journal is configured",
                details={"count": len(expired_transition_states)},
            ))
        else:
            try:
                report.stale_events_persisted = append_stale_transition_journal(
                    stale_journal_path, expired_transition_states,
                    now=now, workflow_id=workflow_id,
                    snapshot_id=envelope_info["snapshot_id"],
                )
            except PreflightError as exc:
                report.findings.append(Finding(
                    "STALE_PERSISTENCE_FAILED", "BLOCK", str(exc),
                    details={"count": len(expired_transition_states)},
                ))

    for key in envelope_info["required_state_keys"]:
        assert_dependency_declared(bundle, "state", key)
        state = state_by_key.get(key)
        if state is None:
            report.findings.append(Finding(
                "MISSING_REQUIRED_STATE", "BLOCK", f"required JACS State row is missing: {key}",
                canonical_key=key,
            ))
            continue
        state_id = str(state.get("state_id") or key)
        effective = report.effective_state_status.get(state_id, "UNKNOWN")
        if effective in BLOCKING_EFFECTIVE_STATUSES:
            report.findings.append(Finding(
                f"REQUIRED_STATE_{effective}", "BLOCK",
                f"required state {key} has effective status {effective}",
                canonical_key=key, state_id=state_id,
            ))

    candidates = _registry_state_candidates(states) + [dict(item) for item in bundle.get("facts", [])]
    for key in envelope_info["required_fact_keys"]:
        assert_dependency_declared(bundle, "fact", key)
        try:
            chosen, findings = resolve_authoritative_fact(
                key, candidates, default_timezone=timezone_name,
            )
        except PreflightError as exc:
            chosen = None
            findings = [Finding("INVALID_AUTHORITY_EVIDENCE", "BLOCK", str(exc), canonical_key=key)]
        report.findings.extend(findings)
        if chosen is not None:
            report.resolved_facts[key] = chosen

    live_automations = list((bundle.get("live") or {}).get("automations") or [])
    registry_automations = list(registry.get("automations") or [])
    report.automation_diff = diff_automations(registry_automations, live_automations)
    if report.automation_diff.get("invalid_delivery_claims"):
        report.findings.append(Finding(
            "NOTIFICATION_RECEIPT_UNPROVEN",
            "BLOCK" if request.get("require_notification_receipt") else "WARN",
            "scheduler last_run/configuration cannot prove user receipt",
            details={"automation_ids": report.automation_diff["invalid_delivery_claims"]},
        ))
    if request.get("require_automation_sync") and report.automation_diff.get("has_drift"):
        report.findings.append(Finding(
            "AUTOMATION_RUNTIME_DRIFT", "BLOCK",
            "live scheduler differs from JACS automation projection",
            details=report.automation_diff,
        ))
    if report.primary_wip_count > 1:
        report.findings.append(Finding(
            "PRIMARY_WIP_VIOLATION", "BLOCK",
            f"primary execution WIP is {report.primary_wip_count}, expected at most 1",
        ))

    if consequential:
        report.allowed = not any(item.severity == "BLOCK" for item in report.findings)
    if report.allowed and consequential and replay_journal_path is not None:
        try:
            append_snapshot_receipt(
                replay_journal_path,
                snapshot_id=envelope_info["snapshot_id"],
                content_digest=envelope_info["content_digest"],
                workflow_id=workflow_id,
                now=now,
            )
            report.snapshot_receipt_persisted = True
        except PreflightError as exc:
            report.findings.append(Finding("SNAPSHOT_REPLAY_PERSISTENCE_FAILED", "BLOCK", str(exc)))
            report.allowed = False
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
    replay_journal_path: Path | None = None,
    now: datetime | None = None,
) -> PreflightReport:
    if replay_journal_path is None and stale_journal_path is not None:
        replay_journal_path = stale_journal_path.with_name("jacs_snapshot_receipts.jsonl")
    return run_preflight(
        load_bundle(path),
        now=now,
        stale_journal_path=stale_journal_path,
        replay_journal_path=replay_journal_path,
    )
