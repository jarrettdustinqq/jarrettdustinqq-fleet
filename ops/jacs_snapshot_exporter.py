#!/usr/bin/env python3
"""Trusted connector-side constructor for JACS schema-v2 preflight snapshots.

This module does not call providers, schedulers, Gmail, GitHub, Calendar, Finances,
or Google Sheets itself. A trusted connector orchestrator must perform those fresh
reads first and provide provenance-tagged read receipts. The exporter validates
fresh-read recency and structure, binds facts to their read source, constructs the
schema-v2 envelope, computes the deterministic digest, and atomically writes the
host snapshot.

It never writes canonical JACS/Google-Sheets State, Evidence, or Audit rows.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import jacs_snapshot_boundary as boundary


class SnapshotExportError(RuntimeError):
    """Raised when connector evidence cannot safely produce a snapshot."""


SOURCE_READ_KEYS = {
    "source_kind",
    "data_types",
    "source_ref",
    "observed_at",
    "fetched_at",
    "facts",
}
REGISTRY_READ_KEYS = {"source_ref", "observed_at", "fetched_at"}
FACT_KEYS = {"canonical_key", "data_type", "value", "observed_at"}
PREDICTIVE_SOURCES = {
    "recurrence_prediction",
    "model_prediction",
    "inference",
    "registry_projection",
    "cached_projection",
}


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SnapshotExportError(f"{field} must be an object")
    return dict(value)


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotExportError(f"{field} must be a non-empty string")
    return value.strip()


def _require_string_list(value: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list):
        raise SnapshotExportError(f"{field} must be a list")
    result = [_require_string(item, field) for item in value]
    if not allow_empty and not result:
        raise SnapshotExportError(f"{field} must not be empty")
    if len(set(result)) != len(result):
        raise SnapshotExportError(f"{field} contains duplicates")
    return result


def _parse(value: Any, field: str, timezone_name: str) -> datetime:
    try:
        return boundary.parse_timestamp(value, default_timezone=timezone_name)
    except boundary.PreflightError as exc:
        raise SnapshotExportError(f"{field}: {exc}") from exc


def _check_fresh_fetch(
    read: Mapping[str, Any],
    *,
    field: str,
    generated_at: datetime,
    max_age_seconds: int,
    timezone_name: str,
) -> tuple[datetime, datetime]:
    fetched_at = _parse(read.get("fetched_at"), f"{field}.fetched_at", timezone_name)
    observed_at = _parse(read.get("observed_at"), f"{field}.observed_at", timezone_name)
    skew = timedelta(seconds=boundary.MAX_FUTURE_SKEW_SECONDS)
    if fetched_at > generated_at + skew:
        raise SnapshotExportError(f"{field}.fetched_at is future-dated")
    if generated_at - fetched_at > timedelta(seconds=max_age_seconds):
        raise SnapshotExportError(
            f"{field}.fetched_at is older than max_age_seconds; fresh connector read required"
        )
    if observed_at > generated_at + skew:
        raise SnapshotExportError(f"{field}.observed_at is later than snapshot generation")
    return observed_at, fetched_at


def _validate_registry(
    registry: Mapping[str, Any],
    required_state_keys: Sequence[str],
) -> None:
    states = registry.get("states")
    if not isinstance(states, list):
        raise SnapshotExportError("registry.states must be a list")
    available = {
        str(state.get("canonical_key"))
        for state in states
        if isinstance(state, Mapping) and state.get("canonical_key")
    }
    missing = sorted(set(required_state_keys) - available)
    if missing:
        raise SnapshotExportError(f"required State keys absent from fresh registry read: {missing}")
    automations = registry.get("automations")
    if automations is not None and not isinstance(automations, list):
        raise SnapshotExportError("registry.automations must be a list when present")
    portfolio = registry.get("portfolio")
    if portfolio is not None and not isinstance(portfolio, Mapping):
        raise SnapshotExportError("registry.portfolio must be an object when present")


def _normalize_source_read(
    raw: Any,
    *,
    index: int,
    generated_at: datetime,
    max_age_seconds: int,
    timezone_name: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    read = _require_mapping(raw, f"source_reads[{index}]")
    if set(read) != SOURCE_READ_KEYS:
        missing = sorted(SOURCE_READ_KEYS - set(read))
        extra = sorted(set(read) - SOURCE_READ_KEYS)
        raise SnapshotExportError(
            f"source_reads[{index}] keys mismatch: missing={missing} extra={extra}"
        )
    source_kind = _require_string(read.get("source_kind"), f"source_reads[{index}].source_kind")
    if source_kind not in boundary.REGISTERED_SOURCE_KINDS:
        raise SnapshotExportError(f"unregistered source_kind {source_kind!r}")
    if source_kind == "registry_projection":
        raise SnapshotExportError("registry_projection must use registry_read, not source_reads")
    data_types = _require_string_list(
        read.get("data_types"),
        f"source_reads[{index}].data_types",
        allow_empty=False,
    )
    source_ref = _require_string(read.get("source_ref"), f"source_reads[{index}].source_ref")
    observed_at, _ = _check_fresh_fetch(
        read,
        field=f"source_reads[{index}]",
        generated_at=generated_at,
        max_age_seconds=max_age_seconds,
        timezone_name=timezone_name,
    )
    facts_raw = read.get("facts")
    if not isinstance(facts_raw, list):
        raise SnapshotExportError(f"source_reads[{index}].facts must be a list")
    facts: list[dict[str, Any]] = []
    for fact_index, raw_fact in enumerate(facts_raw):
        fact = _require_mapping(raw_fact, f"source_reads[{index}].facts[{fact_index}]")
        if set(fact) != FACT_KEYS:
            missing = sorted(FACT_KEYS - set(fact))
            extra = sorted(set(fact) - FACT_KEYS)
            raise SnapshotExportError(
                f"source_reads[{index}].facts[{fact_index}] keys mismatch: "
                f"missing={missing} extra={extra}"
            )
        canonical_key = _require_string(
            fact.get("canonical_key"),
            f"source_reads[{index}].facts[{fact_index}].canonical_key",
        )
        data_type = _require_string(
            fact.get("data_type"),
            f"source_reads[{index}].facts[{fact_index}].data_type",
        )
        if data_type not in data_types:
            raise SnapshotExportError(
                f"fact {canonical_key} data_type {data_type!r} is absent from its source read declaration"
            )
        fact_observed_at = _parse(
            fact.get("observed_at"),
            f"source_reads[{index}].facts[{fact_index}].observed_at",
            timezone_name,
        )
        if fact_observed_at > generated_at + timedelta(seconds=boundary.MAX_FUTURE_SKEW_SECONDS):
            raise SnapshotExportError(f"fact {canonical_key} is future-dated")
        facts.append(
            {
                "canonical_key": canonical_key,
                "data_type": data_type,
                "source_kind": source_kind,
                "value": fact.get("value"),
                "observed_at": _iso(fact_observed_at),
            }
        )
    manifest_entry = {
        "source_kind": source_kind,
        "data_types": data_types,
        "observed_at": _iso(observed_at),
        "source_ref": source_ref,
    }
    return manifest_entry, facts


def build_snapshot(spec: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Build a schema-v2 snapshot from already-fetched connector observations."""

    raw = _require_mapping(spec, "export spec")
    generated_at = _utc_now(now)
    timezone_name = str(raw.get("default_timezone") or "America/Chicago")
    workflow_id = _require_string(raw.get("workflow_id"), "workflow_id")
    consequential = bool(raw.get("consequential", True))
    max_age_seconds = raw.get("max_age_seconds", 300)
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, int)
        or not 1 <= max_age_seconds <= boundary.MAX_SNAPSHOT_AGE_SECONDS
    ):
        raise SnapshotExportError(
            f"max_age_seconds must be 1..{boundary.MAX_SNAPSHOT_AGE_SECONDS}"
        )
    required_state_keys = _require_string_list(
        raw.get("required_state_keys", []), "required_state_keys"
    )
    required_fact_keys = _require_string_list(
        raw.get("required_fact_keys", []), "required_fact_keys"
    )

    registry = _require_mapping(raw.get("registry"), "registry")
    _validate_registry(registry, required_state_keys)

    registry_read = _require_mapping(raw.get("registry_read"), "registry_read")
    if set(registry_read) != REGISTRY_READ_KEYS:
        missing = sorted(REGISTRY_READ_KEYS - set(registry_read))
        extra = sorted(set(registry_read) - REGISTRY_READ_KEYS)
        raise SnapshotExportError(
            f"registry_read keys mismatch: missing={missing} extra={extra}"
        )
    registry_ref = _require_string(registry_read.get("source_ref"), "registry_read.source_ref")
    registry_observed_at, _ = _check_fresh_fetch(
        registry_read,
        field="registry_read",
        generated_at=generated_at,
        max_age_seconds=max_age_seconds,
        timezone_name=timezone_name,
    )
    registry_data_types = sorted(
        {
            str(state.get("authority_data_type") or state.get("data_type") or "generic")
            for state in registry.get("states", [])
            if isinstance(state, Mapping)
        }
        or {"generic"}
    )
    manifest: list[dict[str, Any]] = [
        {
            "source_kind": "registry_projection",
            "data_types": registry_data_types,
            "observed_at": _iso(registry_observed_at),
            "source_ref": registry_ref,
        }
    ]

    source_reads = raw.get("source_reads")
    if not isinstance(source_reads, list):
        raise SnapshotExportError("source_reads must be a list")
    seen_sources = {"registry_projection"}
    facts: list[dict[str, Any]] = []
    for index, item in enumerate(source_reads):
        entry, source_facts = _normalize_source_read(
            item,
            index=index,
            generated_at=generated_at,
            max_age_seconds=max_age_seconds,
            timezone_name=timezone_name,
        )
        source_kind = entry["source_kind"]
        if source_kind in seen_sources:
            raise SnapshotExportError(
                f"source_reads must aggregate to one read receipt per source_kind; duplicate {source_kind}"
            )
        seen_sources.add(source_kind)
        manifest.append(entry)
        facts.extend(source_facts)

    external_fact_keys = {
        fact["canonical_key"]
        for fact in facts
        if fact["source_kind"] not in PREDICTIVE_SOURCES
    }
    missing_authoritative = sorted(set(required_fact_keys) - external_fact_keys)
    if consequential and missing_authoritative:
        raise SnapshotExportError(
            "consequential required facts require at least one fresh non-predictive "
            f"source read: {missing_authoritative}"
        )

    live = _require_mapping(raw.get("live", {"automations": []}), "live")
    live_automations = live.get("automations", [])
    if not isinstance(live_automations, list):
        raise SnapshotExportError("live.automations must be a list")
    require_automation_sync = bool(raw.get("require_automation_sync", False))
    if live_automations or require_automation_sync:
        scheduler_entries = [
            item
            for item in manifest
            if item["source_kind"] == "scheduler_runtime"
            and "automation_runtime" in item["data_types"]
        ]
        if not scheduler_entries:
            raise SnapshotExportError(
                "live automation use requires a fresh scheduler_runtime/automation_runtime read"
            )

    snapshot_id = raw.get("snapshot_id")
    if snapshot_id is None:
        snapshot_id = (
            "jacs-"
            + generated_at.strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + secrets.token_hex(8)
        )
    snapshot_id = _require_string(snapshot_id, "snapshot_id")

    envelope = {
        "schema_version": boundary.SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "generated_at": _iso(generated_at),
        "expires_at": _iso(generated_at + timedelta(seconds=max_age_seconds)),
        "max_age_seconds": max_age_seconds,
        "authoritative_source_manifest": manifest,
        "required_state_keys": required_state_keys,
        "required_fact_keys": required_fact_keys,
        "content_digest": "sha256:" + ("0" * 64),
    }
    bundle = {
        "default_timezone": timezone_name,
        "envelope": envelope,
        "request": {
            "workflow_id": workflow_id,
            "consequential": consequential,
            "declared_state_reads": list(required_state_keys),
            "declared_fact_reads": list(required_fact_keys),
            "require_automation_sync": require_automation_sync,
            "require_notification_receipt": bool(
                raw.get("require_notification_receipt", False)
            ),
        },
        "registry": registry,
        "facts": facts,
        "live": live,
    }
    envelope["content_digest"] = boundary.compute_content_digest(bundle)
    try:
        boundary.validate_snapshot_envelope(
            bundle,
            now=generated_at,
            default_timezone=timezone_name,
        )
    except boundary.PreflightError as exc:
        raise SnapshotExportError(f"constructed snapshot failed boundary validation: {exc}") from exc
    return bundle


def write_snapshot_atomic(path: Path, bundle: Mapping[str, Any]) -> None:
    """Atomically write a private snapshot without touching canonical JACS."""

    target = path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        temp_path = Path(temp_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(bundle, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        temp_path = None
        target.chmod(0o600)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise SnapshotExportError(f"unable to atomically write snapshot {target}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a JACS schema-v2 preflight snapshot from an already-fetched "
            "trusted connector export spec. This command performs no connector reads."
        )
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        raw = json.loads(args.input.read_text(encoding="utf-8"))
        bundle = build_snapshot(raw)
        write_snapshot_atomic(args.output, bundle)
    except (OSError, json.JSONDecodeError, SnapshotExportError) as exc:
        print(f"[jacs-snapshot-exporter] refused: {exc}", file=os.sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "written",
                "snapshot_id": bundle["envelope"]["snapshot_id"],
                "content_digest": bundle["envelope"]["content_digest"],
                "output": str(args.output),
                "canonical_jacs_writeback": "NOT_PERFORMED",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
