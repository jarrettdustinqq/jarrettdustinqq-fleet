#!/usr/bin/env python3
"""Intended-host JACS activation proof.

This harness never deploys or contacts external systems. It uses temporary files,
a temporary projects root, and a fake core scanner. Every negative case asserts
that the fake `_CORE_RUN_SCAN` call count does not increase.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = REPO_ROOT / "ops"
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import control_hub_runtime as runtime  # noqa: E402
import control_hub_safe_entry as safe_entry  # noqa: E402
import jacs_snapshot_boundary as boundary  # noqa: E402
import jacs_snapshot_exporter as exporter  # noqa: E402


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def make_spec(now: datetime, snapshot_id: str, *, max_age_seconds: int = 300) -> dict:
    observed = now - timedelta(seconds=5)
    fetched = now - timedelta(seconds=2)
    schedule_registry = (
        "BEGIN:VEVENT\n"
        "RRULE:FREQ=DAILY;BYHOUR=8;BYMINUTE=0\n"
        "END:VEVENT"
    )
    schedule_live = (
        "BEGIN:VEVENT\r\n"
        "RRULE:BYMINUTE=0;FREQ=DAILY;BYHOUR=8\r\n"
        "END:VEVENT\r\n"
    )
    return {
        "default_timezone": "America/Chicago",
        "workflow_id": "WF.JACS.HOST.ACTIVATION.HARNESS",
        "consequential": True,
        "snapshot_id": snapshot_id,
        "max_age_seconds": max_age_seconds,
        "required_state_keys": ["closure:primary"],
        "required_fact_keys": ["finance:checking:available"],
        "require_automation_sync": True,
        "require_notification_receipt": False,
        "registry_read": {
            "source_ref": "google-sheets:jacs-registry:harness",
            "observed_at": iso(observed),
            "fetched_at": iso(fetched),
        },
        "registry": {
            "states": [
                {
                    "state_id": "ST.CLOSURE.PRIMARY",
                    "canonical_key": "closure:primary",
                    "value": "WF.BUFFER.2000.v1",
                    "status": "VERIFIED",
                    "observed_at": iso(observed),
                    "stale_after": iso(now + timedelta(minutes=10)),
                },
                {
                    "state_id": "ST.FIN.CHECKING.AVAILABLE",
                    "canonical_key": "finance:checking:available",
                    "value": 100.0,
                    "status": "VERIFIED",
                    "authority_data_type": "financial_balance",
                    "observed_at": iso(observed),
                    "stale_after": iso(now + timedelta(minutes=5)),
                },
            ],
            "automations": [
                {
                    "automation_id": "auto-1",
                    "enabled": True,
                    "timing": schedule_registry,
                    "notifications_enabled": False,
                    "email_enabled": False,
                }
            ],
            "portfolio": {"primary_workflow_id": "WF.BUFFER.2000.v1"},
        },
        "source_reads": [
            {
                "source_kind": "financial_institution",
                "data_types": ["financial_balance"],
                "source_ref": "finances:get_linked_accounts:harness",
                "observed_at": iso(observed),
                "fetched_at": iso(fetched),
                "facts": [
                    {
                        "canonical_key": "finance:checking:available",
                        "data_type": "financial_balance",
                        "value": 100.0,
                        "observed_at": iso(observed),
                    }
                ],
            },
            {
                "source_kind": "scheduler_runtime",
                "data_types": ["automation_runtime"],
                "source_ref": "automations:peek:harness",
                "observed_at": iso(observed),
                "fetched_at": iso(fetched),
                "facts": [],
            },
        ],
        "live": {
            "automations": [
                {
                    "id": "auto-1",
                    "is_enabled": True,
                    "schedule": schedule_live,
                    "notifications_enabled": False,
                    "email_enabled": False,
                }
            ]
        },
    }


class ActivationHarness:
    def __init__(self, root: Path):
        self.root = root
        self.projects_root = root / "projects"
        self.projects_root.mkdir()
        self.snapshot_path = root / "jacs_preflight.json"
        self.stale_journal = root / "jacs_stale_events.jsonl"
        self.core_calls = 0
        self._saved_core = safe_entry._CORE_RUN_SCAN
        self._saved_snapshot = safe_entry._JACS_PREFLIGHT_JSON
        self._saved_stale = safe_entry._JACS_STALE_JOURNAL
        self._saved_required = safe_entry._JACS_PREFLIGHT_REQUIRED

        def fake_core(*args, **kwargs):
            self.core_calls += 1
            return {"status": "fake-core-called", "core_calls": self.core_calls}

        safe_entry._CORE_RUN_SCAN = fake_core
        safe_entry._JACS_PREFLIGHT_JSON = self.snapshot_path
        safe_entry._JACS_STALE_JOURNAL = self.stale_journal
        safe_entry._JACS_PREFLIGHT_REQUIRED = True

    def close(self) -> None:
        safe_entry._CORE_RUN_SCAN = self._saved_core
        safe_entry._JACS_PREFLIGHT_JSON = self._saved_snapshot
        safe_entry._JACS_STALE_JOURNAL = self._saved_stale
        safe_entry._JACS_PREFLIGHT_REQUIRED = self._saved_required

    def write(self, bundle: dict) -> None:
        exporter.write_snapshot_atomic(self.snapshot_path, bundle)

    def fresh(self, snapshot_id: str, *, now: datetime | None = None) -> dict:
        current = now or datetime.now(timezone.utc)
        return exporter.build_snapshot(make_spec(current, snapshot_id), now=current)

    def run(self) -> dict:
        return safe_entry.guarded_run_scan(
            self.root / "control_hub.db",
            self.projects_root,
            None,
        )

    def expect_refusal(self, name: str, expected_fragment: str | None = None) -> str:
        before = self.core_calls
        try:
            self.run()
        except safe_entry.ScanRefusedError as exc:
            message = str(exc)
        else:
            raise RuntimeError(f"{name}: expected refusal but scan was allowed")
        if self.core_calls != before:
            raise RuntimeError(
                f"{name}: _CORE_RUN_SCAN was called despite refusal "
                f"(before={before} after={self.core_calls})"
            )
        if expected_fragment and expected_fragment not in message:
            raise RuntimeError(
                f"{name}: refusal did not contain {expected_fragment!r}: {message}"
            )
        return message


def recompute(bundle: dict) -> dict:
    bundle["envelope"]["content_digest"] = boundary.compute_content_digest(bundle)
    return bundle


def verify_operational_entry_policy() -> None:
    policy_path = REPO_ROOT / "config" / "jacs-host-activation-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if policy.get("schema_version") != 1:
        raise RuntimeError("activation policy schema_version must be 1")

    direct_agent = REPO_ROOT / "ops" / "control_hub_agent.py"
    if direct_agent.stat().st_mode & 0o111:
        raise RuntimeError("control_hub_agent.py must remain non-executable")

    forbidden = policy["forbidden_operational_commands"]
    for relative in policy["audited_wrapper_files"]:
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        for command in forbidden:
            if command in text:
                raise RuntimeError(
                    f"supported wrapper {relative} contains forbidden direct agent command {command}"
                )

    service = runtime.render_user_units(Path("/tmp/jacs-activation-runtime.json"))[
        runtime.SERVICE_NAME
    ]
    for fragment in policy["required_service_substrings"]:
        if fragment not in service:
            raise RuntimeError(
                f"generated service is missing activation-policy fragment {fragment!r}"
            )

    fleetctl = (REPO_ROOT / "fleetctl").read_text(encoding="utf-8")
    strict_export = 'export JACS_PREFLIGHT_REQUIRED="${JACS_PREFLIGHT_REQUIRED:-1}"'
    if strict_export not in fleetctl:
        raise RuntimeError("fleetctl no longer defaults JACS preflight to required")
    for command_case in ("mission-control)", "hub-scan)", "hub-serve)", "hub-runtime)"):
        marker = command_case + "\n    enable_jacs_preflight"
        if marker not in fleetctl:
            raise RuntimeError(f"fleetctl {command_case} no longer invokes enable_jacs_preflight")


def main() -> int:
    verify_operational_entry_policy()
    negative_results: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="jacs-host-activation-") as tmp:
        harness = ActivationHarness(Path(tmp))
        try:
            if harness.snapshot_path.exists():
                harness.snapshot_path.unlink()
            negative_results["missing"] = harness.expect_refusal("missing", "required")

            old = datetime.now(timezone.utc) - timedelta(minutes=10)
            stale = exporter.build_snapshot(
                make_spec(old, "jacs-harness-stale-0001", max_age_seconds=60),
                now=old,
            )
            harness.write(stale)
            negative_results["stale"] = harness.expect_refusal("stale", "expired")

            future_time = datetime.now(timezone.utc) + timedelta(minutes=5)
            future = exporter.build_snapshot(
                make_spec(future_time, "jacs-harness-future-0001"),
                now=future_time,
            )
            harness.write(future)
            negative_results["future"] = harness.expect_refusal("future", "future-dated")

            tampered = harness.fresh("jacs-harness-tampered-0001")
            tampered["facts"][0]["value"] = 999.0
            harness.write(tampered)
            negative_results["tampered"] = harness.expect_refusal(
                "tampered", "content_digest"
            )

            undeclared = harness.fresh("jacs-harness-undeclared-0001")
            undeclared["request"]["declared_fact_reads"] = []
            recompute(undeclared)
            harness.write(undeclared)
            negative_results["undeclared"] = harness.expect_refusal(
                "undeclared", "declared_fact_reads"
            )

            unknown = harness.fresh("jacs-harness-unknown-source-0001")
            unknown["envelope"]["authoritative_source_manifest"][1][
                "source_kind"
            ] = "mystery_source"
            unknown["facts"][0]["source_kind"] = "mystery_source"
            recompute(unknown)
            harness.write(unknown)
            negative_results["unknown_source"] = harness.expect_refusal(
                "unknown_source", "unregistered source_kind"
            )

            authority = harness.fresh("jacs-harness-authority-drift-0001")
            authority["registry"]["states"][1]["value"] = 90.0
            recompute(authority)
            harness.write(authority)
            negative_results["authority_drift"] = harness.expect_refusal(
                "authority_drift", "REGISTRY_AUTHORITY_DRIFT"
            )

            automation = harness.fresh("jacs-harness-automation-drift-0001")
            automation["live"]["automations"][0]["is_enabled"] = False
            recompute(automation)
            harness.write(automation)
            negative_results["automation_drift"] = harness.expect_refusal(
                "automation_drift", "AUTOMATION_RUNTIME_DRIFT"
            )

            replay = harness.fresh("jacs-harness-replay-0001")
            harness.write(replay)
            before_replay_accept = harness.core_calls
            harness.run()
            if harness.core_calls != before_replay_accept + 1:
                raise RuntimeError("replay precondition: first use did not reach fake core")
            negative_results["replayed"] = harness.expect_refusal(
                "replayed", "SNAPSHOT_REPLAY"
            )

            snapshot_a = harness.fresh("jacs-harness-positive-a-0001")
            harness.write(snapshot_a)
            before_a = harness.core_calls
            harness.run()
            if harness.core_calls != before_a + 1:
                raise RuntimeError("positive A did not reach fake core exactly once")
            negative_results["positive_a_replay"] = harness.expect_refusal(
                "positive_a_replay", "SNAPSHOT_REPLAY"
            )

            snapshot_b = harness.fresh("jacs-harness-positive-b-0001")
            harness.write(snapshot_b)
            before_b = harness.core_calls
            harness.run()
            if harness.core_calls != before_b + 1:
                raise RuntimeError("positive B did not reach fake core exactly once")

            print(
                "JACS_HOST_ACTIVATION_HARNESS=PASS "
                f"negative_cases={len(negative_results)} "
                "positive_sequence=A-success,A-replay-refused,B-success "
                f"core_calls={harness.core_calls} "
                "canonical_jacs_writeback=NOT_PERFORMED"
            )
        finally:
            harness.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
