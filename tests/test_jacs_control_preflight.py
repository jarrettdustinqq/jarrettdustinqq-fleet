from __future__ import annotations

import argparse
import copy
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = REPO_ROOT / "ops"
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import control_hub_safe_entry as safe_entry  # noqa: E402
import jacs_control_preflight as preflight  # noqa: E402
import mission_control_agent as mission_control  # noqa: E402

NOW = datetime(2026, 8, 24, 5, 30, tzinfo=timezone.utc)


def manifest(*entries):
    if not entries:
        entries = (("registry_projection", ["generic"], "jacs:fixture"),)
    return [
        {
            "source_kind": kind,
            "data_types": data_types,
            "observed_at": "2026-08-24T05:28:00Z",
            "source_ref": ref,
        }
        for kind, data_types, ref in entries
    ]


def state(
    key="state:key",
    value="ok",
    *,
    status="VERIFIED",
    stale_after="2026-08-24T05:40:00Z",
    **extra,
):
    row = {
        "state_id": "ST." + key.replace(":", ".").upper(),
        "canonical_key": key,
        "value": value,
        "status": status,
        "stale_after": stale_after,
        "observed_at": "2026-08-24T05:28:00Z",
        "data_type": "generic",
    }
    row.update(extra)
    return row


def bundle(
    *,
    states=None,
    facts=None,
    automations=None,
    live=None,
    required_states=None,
    required_facts=None,
    source_manifest=None,
    workflow="WF.TEST",
    require_sync=False,
    require_receipt=False,
    generated_at="2026-08-24T05:29:00Z",
    expires_at="2026-08-24T05:39:00Z",
    max_age=900,
    snapshot_id="snap-test-0001",
):
    required_states = list(required_states or [])
    required_facts = list(required_facts or [])
    payload = {
        "default_timezone": "America/Chicago",
        "envelope": {
            "schema_version": preflight.SNAPSHOT_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "generated_at": generated_at,
            "expires_at": expires_at,
            "max_age_seconds": max_age,
            "authoritative_source_manifest": source_manifest or manifest(),
            "required_state_keys": required_states,
            "required_fact_keys": required_facts,
            "content_digest": "sha256:" + "0" * 64,
        },
        "request": {
            "workflow_id": workflow,
            "consequential": True,
            "declared_state_reads": list(required_states),
            "declared_fact_reads": list(required_facts),
            "require_automation_sync": require_sync,
            "require_notification_receipt": require_receipt,
        },
        "registry": {
            "states": list(states or []),
            "automations": list(automations or []),
            "portfolio": {"primary_workflow_id": "WF.PRIMARY"},
        },
        "facts": list(facts or []),
        "live": {"automations": list(live or [])},
    }
    payload["envelope"]["content_digest"] = preflight.compute_content_digest(payload)
    return payload


def dynamic_bundle(*, snapshot_id: str, states=None, required_states=None):
    now = datetime.now(timezone.utc)
    payload = bundle(
        snapshot_id=snapshot_id,
        states=states,
        required_states=required_states,
        generated_at=(now - timedelta(seconds=5)).isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
        max_age=600,
    )
    if states:
        for row in payload["registry"]["states"]:
            row["observed_at"] = (now - timedelta(seconds=10)).isoformat()
            row["stale_after"] = (now + timedelta(minutes=5)).isoformat()
    payload["envelope"]["authoritative_source_manifest"][0]["observed_at"] = (
        now - timedelta(seconds=10)
    ).isoformat()
    payload["envelope"]["content_digest"] = preflight.compute_content_digest(payload)
    return payload


def run(payload, *, stale=True, replay=True):
    stack = tempfile.TemporaryDirectory()
    root = Path(stack.name)
    report = preflight.run_preflight(
        payload,
        now=NOW,
        stale_journal_path=root / "stale.jsonl" if stale else None,
        replay_journal_path=root / "replay.jsonl" if replay else None,
    )
    return stack, root, report


class SnapshotEnvelopeTests(unittest.TestCase):
    def test_valid_fresh_snapshot(self):
        payload = bundle(states=[state()], required_states=["state:key"])
        tmp, root, report = run(payload)
        self.addCleanup(tmp.cleanup)
        self.assertTrue(report.allowed, report.summary())
        self.assertTrue(report.snapshot_receipt_persisted)
        self.assertTrue((root / "replay.jsonl").exists())

    def test_stale_whole_snapshot_replay_is_rejected(self):
        payload = bundle(
            states=[state()],
            required_states=["state:key"],
            generated_at="2026-08-24T05:00:00Z",
            expires_at="2026-08-24T05:10:00Z",
            max_age=900,
        )
        with self.assertRaisesRegex(preflight.PreflightError, "expired"):
            preflight.run_preflight(payload, now=NOW)

    def test_future_dated_snapshot_is_rejected(self):
        payload = bundle(
            generated_at="2026-08-24T05:40:00Z",
            expires_at="2026-08-24T05:45:00Z",
            max_age=900,
        )
        with self.assertRaisesRegex(preflight.PreflightError, "future-dated"):
            preflight.run_preflight(payload, now=NOW)

    def test_duplicate_snapshot_id_is_rejected(self):
        payload = bundle(
            states=[state()],
            required_states=["state:key"],
            snapshot_id="snap-duplicate-01",
        )
        with tempfile.TemporaryDirectory() as tmp:
            replay = Path(tmp) / "replay.jsonl"
            stale = Path(tmp) / "stale.jsonl"
            first = preflight.run_preflight(
                payload,
                now=NOW,
                stale_journal_path=stale,
                replay_journal_path=replay,
            )
            self.assertTrue(first.allowed)
            second = preflight.run_preflight(
                payload,
                now=NOW,
                stale_journal_path=stale,
                replay_journal_path=replay,
            )
            self.assertFalse(second.allowed)
            self.assertIn("SNAPSHOT_REPLAY", {f.code for f in second.findings})

    def test_content_digest_tamper_is_rejected(self):
        payload = bundle(states=[state()], required_states=["state:key"])
        payload["registry"]["states"][0]["value"] = "tampered"
        with self.assertRaisesRegex(preflight.PreflightError, "content_digest"):
            preflight.run_preflight(payload, now=NOW)

    def test_malformed_authority_manifest_is_rejected(self):
        payload = bundle()
        payload["envelope"]["authoritative_source_manifest"] = [
            {"source_kind": "provider", "data_types": ["contractual"]}
        ]
        payload["envelope"]["content_digest"] = preflight.compute_content_digest(payload)
        with self.assertRaisesRegex(preflight.PreflightError, "manifest"):
            preflight.run_preflight(payload, now=NOW)

    def test_unknown_source_kind_is_rejected(self):
        facts = [
            {
                "canonical_key": "bill:due",
                "data_type": "contractual",
                "source_kind": "mystery_source",
                "value": 10,
                "observed_at": "2026-08-24T05:28:00Z",
            }
        ]
        payload = bundle(
            facts=facts,
            required_facts=["bill:due"],
            source_manifest=[
                {
                    "source_kind": "mystery_source",
                    "data_types": ["contractual"],
                    "observed_at": "2026-08-24T05:28:00Z",
                    "source_ref": "mystery:x",
                }
            ],
        )
        with self.assertRaisesRegex(preflight.PreflightError, "unregistered source_kind"):
            preflight.run_preflight(payload, now=NOW)

    def test_missing_required_dependency_declaration_is_rejected(self):
        payload = bundle(states=[state()], required_states=["state:key"])
        payload["request"]["declared_state_reads"] = []
        payload["envelope"]["content_digest"] = preflight.compute_content_digest(payload)
        with self.assertRaisesRegex(preflight.PreflightError, "exactly match"):
            preflight.run_preflight(payload, now=NOW)

    def test_runtime_dependency_guard_refuses_undeclared_read(self):
        payload = bundle(required_states=[])
        with self.assertRaisesRegex(preflight.PreflightError, "undeclared state dependency"):
            preflight.assert_dependency_declared(payload, "state", "state:hidden")


class FreshnessAuthorityTests(unittest.TestCase):
    def test_expired_verified_row_is_effectively_stale_and_refused(self):
        payload = bundle(
            states=[state(stale_after="2026-08-24T05:20:00Z")],
            required_states=["state:key"],
        )
        tmp, root, report = run(payload)
        self.addCleanup(tmp.cleanup)
        self.assertFalse(report.allowed)
        self.assertEqual(report.effective_state_status["ST.STATE.KEY"], "STALE")
        self.assertEqual(report.stale_events_persisted, 1)
        self.assertIn("CANONICAL_WRITEBACK_PENDING", (root / "stale.jsonl").read_text())

    def test_persistence_failure_during_stale_transition_fails_closed(self):
        payload = bundle(
            states=[state(stale_after="2026-08-24T05:20:00Z")],
            required_states=["state:key"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = root / "as-directory"
            bad.mkdir()
            report = preflight.run_preflight(
                payload,
                now=NOW,
                stale_journal_path=bad,
                replay_journal_path=root / "replay.jsonl",
            )
            self.assertFalse(report.allowed)
            self.assertIn("STALE_PERSISTENCE_FAILED", {f.code for f in report.findings})

    def test_provider_fact_beats_recurrence_prediction(self):
        states = [state("bill:due", 238.42, data_type="contractual")]
        facts = [
            {
                "canonical_key": "bill:due",
                "data_type": "contractual",
                "source_kind": "provider",
                "value": 238.42,
                "observed_at": "2026-08-24T05:28:00Z",
            },
            {
                "canonical_key": "bill:due",
                "data_type": "contractual",
                "source_kind": "recurrence_prediction",
                "value": 227.16,
                "observed_at": "2026-08-24T05:27:00Z",
            },
        ]
        payload = bundle(
            states=states,
            facts=facts,
            required_facts=["bill:due"],
            source_manifest=manifest(
                ("provider", ["contractual"], "gmail:provider"),
                ("recurrence_prediction", ["contractual"], "finances:recurrence"),
            ),
        )
        tmp, _, report = run(payload)
        self.addCleanup(tmp.cleanup)
        self.assertTrue(report.allowed, report.summary())
        self.assertEqual(report.resolved_facts["bill:due"]["value"], 238.42)
        self.assertIn("PREDICTION_DEMOTED", {f.code for f in report.findings})

    def test_fresh_bank_balance_beats_cached_registry_and_blocks_until_reconciled(self):
        states = [state("bank:available", 650.08, data_type="financial_balance")]
        facts = [
            {
                "canonical_key": "bank:available",
                "data_type": "financial_balance",
                "source_kind": "financial_institution",
                "value": 141.76,
                "observed_at": "2026-08-24T05:28:00Z",
            }
        ]
        payload = bundle(
            states=states,
            facts=facts,
            required_facts=["bank:available"],
            source_manifest=manifest(
                ("financial_institution", ["financial_balance"], "finances:bank")
            ),
        )
        tmp, _, report = run(payload)
        self.addCleanup(tmp.cleanup)
        self.assertFalse(report.allowed)
        self.assertEqual(report.resolved_facts["bank:available"]["value"], 141.76)
        self.assertIn("REGISTRY_AUTHORITY_DRIFT", {f.code for f in report.findings})

    def test_late_higher_authority_evidence_reopens_terminal_result(self):
        states = [
            state(
                "subscription:status",
                "open",
                data_type="contractual",
                terminal=True,
                observed_at="2026-08-24T05:20:00Z",
            )
        ]
        facts = [
            {
                "canonical_key": "subscription:status",
                "data_type": "contractual",
                "source_kind": "provider",
                "value": "closed",
                "observed_at": "2026-08-24T05:28:00Z",
            }
        ]
        payload = bundle(
            states=states,
            facts=facts,
            required_facts=["subscription:status"],
            source_manifest=manifest(("provider", ["contractual"], "gmail:support")),
        )
        tmp, _, report = run(payload)
        self.addCleanup(tmp.cleanup)
        self.assertFalse(report.allowed)
        self.assertIn("LATE_HIGHER_AUTHORITY_REOPEN", {f.code for f in report.findings})

    def test_unknown_and_conflicted_are_not_inferred_away(self):
        payload = bundle(
            states=[state("a", status="UNKNOWN"), state("b", status="CONFLICTED")],
            required_states=["a", "b"],
        )
        tmp, _, report = run(payload)
        self.addCleanup(tmp.cleanup)
        self.assertFalse(report.allowed)
        codes = {f.code for f in report.findings}
        self.assertIn("REQUIRED_STATE_UNKNOWN", codes)
        self.assertIn("REQUIRED_STATE_CONFLICTED", codes)


class AutomationTests(unittest.TestCase):
    def test_normalized_equivalent_automation_schedules_do_not_drift(self):
        registry = [
            {
                "automation_id": "auto-1",
                "enabled": True,
                "timing": "BEGIN:VEVENT\nRRULE:BYMINUTE=0;FREQ=DAILY;BYHOUR=8\nEND:VEVENT",
                "notifications_enabled": True,
                "email_enabled": False,
            }
        ]
        live = [
            {
                "id": "auto-1",
                "is_enabled": True,
                "schedule": "BEGIN:VEVENT\r\nRRULE:FREQ=DAILY;BYHOUR=8;BYMINUTE=0\r\nEND:VEVENT\r\n",
                "notifications_enabled": True,
                "email_enabled": False,
            }
        ]
        diff = preflight.diff_automations(registry, live)
        self.assertFalse(diff["has_drift"], diff)

    def test_live_disabled_stored_enabled_is_detected(self):
        diff = preflight.diff_automations(
            [{"automation_id": "auto-1", "enabled": True}],
            [{"id": "auto-1", "is_enabled": False}],
        )
        self.assertTrue(diff["has_drift"])
        self.assertEqual(diff["field_drift"][0]["field"], "enabled")

    def test_missing_and_extra_automations_are_detected(self):
        diff = preflight.diff_automations(
            [{"automation_id": "stored", "enabled": False}],
            [{"id": "live", "is_enabled": True}],
        )
        self.assertEqual(diff["missing_in_registry"], ["live"])
        self.assertEqual(diff["extra_in_registry"], ["stored"])

    def test_enabled_background_monitor_does_not_consume_primary_wip(self):
        payload = bundle(
            automations=[
                {
                    "automation_id": "watch",
                    "enabled": True,
                    "disposition": "BACKGROUND_MONITORING",
                }
            ],
            live=[{"id": "watch", "is_enabled": True}],
            source_manifest=manifest(
                ("scheduler_runtime", ["automation_runtime"], "scheduler:peek")
            ),
        )
        self.assertEqual(preflight.primary_wip_count(payload), 1)

    def test_last_run_is_not_delivery_proof(self):
        registry = [
            {
                "automation_id": "notify",
                "enabled": True,
                "delivery_verified_from_last_run": True,
            }
        ]
        live = [
            {
                "id": "notify",
                "is_enabled": True,
                "last_run": "2026-08-24T05:20:00Z",
            }
        ]
        payload = bundle(
            automations=registry,
            live=live,
            require_receipt=True,
            source_manifest=manifest(
                ("scheduler_runtime", ["automation_runtime"], "scheduler:peek")
            ),
        )
        tmp, _, report = run(payload)
        self.addCleanup(tmp.cleanup)
        self.assertFalse(report.allowed)
        self.assertIn("NOTIFICATION_RECEIPT_UNPROVEN", {f.code for f in report.findings})


class AdversarialFixtureTests(unittest.TestCase):
    def test_fixture_refuses_multiple_bypass_conditions(self):
        fixture = preflight.load_bundle(REPO_ROOT / "tests" / "fixtures" / "jacs_adversarial.json")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = preflight.run_preflight(
                fixture,
                now=NOW,
                stale_journal_path=root / "stale.jsonl",
                replay_journal_path=root / "replay.jsonl",
            )
        self.assertFalse(report.allowed)
        codes = {f.code for f in report.findings}
        self.assertIn("REQUIRED_STATE_STALE", codes)
        self.assertIn("REGISTRY_AUTHORITY_DRIFT", codes)
        self.assertIn("AUTOMATION_RUNTIME_DRIFT", codes)
        self.assertIn("NOTIFICATION_RECEIPT_UNPROVEN", codes)


class SafeEntryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.previous = (
            safe_entry._JACS_PREFLIGHT_JSON,
            safe_entry._JACS_STALE_JOURNAL,
            safe_entry._JACS_PREFLIGHT_REQUIRED,
        )

    def tearDown(self):
        (
            safe_entry._JACS_PREFLIGHT_JSON,
            safe_entry._JACS_STALE_JOURNAL,
            safe_entry._JACS_PREFLIGHT_REQUIRED,
        ) = self.previous

    def test_shared_guard_refuses_missing_snapshot_before_core_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            safe_entry.configure_jacs_preflight(
                argparse.Namespace(
                    jacs_preflight_json=None,
                    jacs_stale_journal=None,
                    require_jacs_preflight=True,
                )
            )
            with mock.patch.object(safe_entry, "_CORE_RUN_SCAN") as core_scan:
                with self.assertRaisesRegex(safe_entry.ScanRefusedError, "required"):
                    safe_entry.guarded_run_scan(Path(tmp) / "db.sqlite", root, None)
            core_scan.assert_not_called()

    def test_shared_guard_accepts_one_fresh_snapshot_then_replay_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "projects"
            root.mkdir()
            snapshot = base / "jacs_preflight.json"
            stale = base / "jacs_stale_events.jsonl"
            payload = dynamic_bundle(
                snapshot_id="snap-safe-entry-0001",
                states=[state()],
                required_states=["state:key"],
            )
            snapshot.write_text(__import__("json").dumps(payload), encoding="utf-8")
            safe_entry.configure_jacs_preflight(
                argparse.Namespace(
                    jacs_preflight_json=snapshot,
                    jacs_stale_journal=stale,
                    require_jacs_preflight=True,
                )
            )
            with mock.patch.object(
                safe_entry,
                "_CORE_RUN_SCAN",
                return_value={"repo_scan_status": "complete"},
            ) as core_scan:
                result = safe_entry.guarded_run_scan(base / "db.sqlite", root, None)
                self.assertEqual(result["repo_scan_status"], "complete")
                with self.assertRaisesRegex(safe_entry.ScanRefusedError, "SNAPSHOT_REPLAY"):
                    safe_entry.guarded_run_scan(base / "db.sqlite", root, None)
            core_scan.assert_called_once()


class CommandSurfaceAuditTests(unittest.TestCase):
    def test_public_control_plane_commands_bind_jacs_preflight(self):
        text = (REPO_ROOT / "fleetctl").read_text(encoding="utf-8")
        self.assertIn("enable_jacs_preflight()", text)
        for command in ("mission-control)", "hub-scan)", "hub-serve)", "hub-runtime)"):
            section = text.split(command, 1)[1].split(";;", 1)[0]
            self.assertIn("enable_jacs_preflight", section, command)

    def test_direct_mission_control_binds_private_state_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HOME": tmp}, clear=True):
                mission_control.bind_jacs_preflight_environment()
                self.assertEqual(os.environ["JACS_PREFLIGHT_REQUIRED"], "1")
                self.assertTrue(os.environ["JACS_PREFLIGHT_JSON"].endswith("jacs_preflight.json"))
                self.assertTrue(os.environ["JACS_STALE_JOURNAL"].endswith("jacs_stale_events.jsonl"))


if __name__ == "__main__":
    unittest.main()
