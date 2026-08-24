from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = REPO_ROOT / "ops"
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import jacs_control_preflight as preflight  # noqa: E402
import control_hub_safe_entry as safe_entry  # noqa: E402

NOW = datetime(2026, 8, 24, 5, 27, tzinfo=timezone.utc)


def state(
    state_id: str,
    key: str,
    value,
    *,
    status: str = "VERIFIED",
    observed_at: str = "2026-08-24T00:20:00-05:00",
    stale_after: str = "2026-08-24T01:20:00-05:00",
    data_type: str = "generic",
    terminal: bool = False,
):
    return {
        "state_id": state_id,
        "canonical_key": key,
        "value": value,
        "status": status,
        "observed_at": observed_at,
        "stale_after": stale_after,
        "authority_data_type": data_type,
        "terminal": terminal,
    }


def bundle(
    *,
    states=None,
    facts=None,
    automations=None,
    live_automations=None,
    request=None,
    primary="WF.BUFFER.2000.v1",
):
    return {
        "default_timezone": "America/Chicago",
        "request": request
        or {
            "workflow_id": "WF.TEST",
            "consequential": True,
            "required_state_keys": [],
            "required_fact_keys": [],
            "require_automation_sync": False,
        },
        "registry": {
            "states": states or [],
            "automations": automations or [],
            "portfolio": {"primary_workflow_id": primary} if primary else {},
        },
        "facts": facts or [],
        "live": {"automations": live_automations or []},
    }


class FreshnessTests(unittest.TestCase):
    def test_expired_verified_row_is_effectively_stale_and_refused(self) -> None:
        expired = state(
            "ST.EXPIRED",
            "finance:cbt:available",
            643.65,
            stale_after="2026-08-23T23:00:00-05:00",
            data_type="financial_balance",
        )
        request = {
            "workflow_id": "WF.CONSEQUENTIAL",
            "consequential": True,
            "required_state_keys": ["finance:cbt:available"],
            "required_fact_keys": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "stale.jsonl"
            report = preflight.run_preflight(
                bundle(states=[expired], request=request),
                now=NOW,
                stale_journal_path=journal,
            )
            self.assertFalse(report.allowed)
            self.assertEqual(report.effective_state_status["ST.EXPIRED"], "STALE")
            self.assertEqual(report.stale_events_persisted, 1)
            records = [json.loads(line) for line in journal.read_text().splitlines()]
            self.assertEqual([row["record_type"] for row in records], ["Evidence", "Audit"])
            self.assertEqual(records[1]["new_state"], "STALE")

    def test_persistence_failure_during_stale_transition_fails_closed(self) -> None:
        expired = state("ST.EXPIRED", "x", 1, stale_after="2026-08-23T20:00:00-05:00")
        request = {
            "workflow_id": "WF.CONSEQUENTIAL",
            "consequential": True,
            "required_state_keys": ["x"],
            "required_fact_keys": [],
        }
        with mock.patch.object(
            preflight,
            "append_stale_transition_journal",
            side_effect=preflight.PreflightError("disk unavailable"),
        ):
            report = preflight.run_preflight(
                bundle(states=[expired], request=request),
                now=NOW,
                stale_journal_path=Path("/tmp/unused.jsonl"),
            )
        self.assertFalse(report.allowed)
        self.assertTrue(any(f.code == "STALE_PERSISTENCE_FAILED" for f in report.findings))

    def test_unknown_and_conflicted_are_not_inferred_away(self) -> None:
        states = [
            state("ST.UNKNOWN", "unknown:key", None, status="UNKNOWN"),
            state("ST.CONFLICT", "conflict:key", "a", status="CONFLICTED"),
        ]
        request = {
            "workflow_id": "WF.CONSEQUENTIAL",
            "consequential": True,
            "required_state_keys": ["unknown:key", "conflict:key"],
            "required_fact_keys": [],
        }
        report = preflight.run_preflight(bundle(states=states, request=request), now=NOW)
        self.assertFalse(report.allowed)
        codes = {finding.code for finding in report.findings}
        self.assertIn("REQUIRED_STATE_UNKNOWN", codes)
        self.assertIn("REQUIRED_STATE_CONFLICTED", codes)


class AuthorityTests(unittest.TestCase):
    def test_provider_fact_beats_recurrence_prediction(self) -> None:
        facts = [
            {
                "canonical_key": "finance:insurance:next_due",
                "data_type": "contractual",
                "source_kind": "recurrence_prediction",
                "value": {"amount": 572.22, "due": "2026-09-14"},
                "observed_at": "2026-08-23T12:00:00-05:00",
            },
            {
                "canonical_key": "finance:insurance:next_due",
                "data_type": "contractual",
                "source_kind": "provider",
                "value": {"amount": 562.22, "due": "2026-09-04"},
                "observed_at": "2026-08-23T13:00:00-05:00",
            },
        ]
        request = {
            "workflow_id": "WF.CONSEQUENTIAL",
            "consequential": True,
            "required_state_keys": [],
            "required_fact_keys": ["finance:insurance:next_due"],
        }
        report = preflight.run_preflight(bundle(facts=facts, request=request), now=NOW)
        self.assertTrue(report.allowed)
        self.assertEqual(
            report.resolved_facts["finance:insurance:next_due"]["source_kind"],
            "provider",
        )
        self.assertTrue(any(f.code == "PREDICTION_DEMOTED" for f in report.findings))

    def test_fresh_bank_balance_beats_cached_registry_and_blocks_until_reconciled(self) -> None:
        cached = state(
            "ST.FIN.CBT.AVAILABLE",
            "finance:cbt:available",
            643.65,
            data_type="financial_balance",
        )
        facts = [
            {
                "canonical_key": "finance:cbt:available",
                "data_type": "financial_balance",
                "source_kind": "financial_institution",
                "value": 141.76,
                "observed_at": "2026-08-24T00:22:00-05:00",
            }
        ]
        request = {
            "workflow_id": "WF.CASH",
            "consequential": True,
            "required_state_keys": ["finance:cbt:available"],
            "required_fact_keys": ["finance:cbt:available"],
        }
        report = preflight.run_preflight(bundle(states=[cached], facts=facts, request=request), now=NOW)
        self.assertFalse(report.allowed)
        self.assertEqual(report.resolved_facts["finance:cbt:available"]["value"], 141.76)
        self.assertTrue(any(f.code == "REGISTRY_AUTHORITY_DRIFT" for f in report.findings))

    def test_late_higher_authority_evidence_reopens_terminal_result(self) -> None:
        terminal = state(
            "ST.TERMINAL",
            "finance:openai:duplicate",
            "UNRESOLVED",
            data_type="contractual",
            observed_at="2026-08-18T12:00:00-05:00",
            terminal=True,
        )
        facts = [
            {
                "canonical_key": "finance:openai:duplicate",
                "data_type": "contractual",
                "source_kind": "provider",
                "value": "CANCELED_AND_REFUNDED",
                "observed_at": "2026-08-19T12:41:17-05:00",
            }
        ]
        request = {
            "workflow_id": "WF.AUDIT",
            "consequential": True,
            "required_state_keys": [],
            "required_fact_keys": ["finance:openai:duplicate"],
        }
        report = preflight.run_preflight(bundle(states=[terminal], facts=facts, request=request), now=NOW)
        self.assertFalse(report.allowed)
        codes = {finding.code for finding in report.findings}
        self.assertIn("REGISTRY_AUTHORITY_DRIFT", codes)
        self.assertIn("LATE_HIGHER_AUTHORITY_REOPEN", codes)


class AutomationTests(unittest.TestCase):
    def test_live_disabled_automation_stored_enabled_is_detected(self) -> None:
        registry = [
            {
                "automation_id": "auto-1",
                "enabled": True,
                "timing": "daily",
                "notifications_enabled": True,
                "email_enabled": True,
            }
        ]
        live = [
            {
                "id": "auto-1",
                "is_enabled": False,
                "schedule": "daily",
                "notifications_enabled": True,
                "email_enabled": True,
                "last_run_time": "2026-08-23T12:00:00Z",
            }
        ]
        request = {
            "workflow_id": "WF.AUDIT",
            "consequential": True,
            "required_state_keys": [],
            "required_fact_keys": [],
            "require_automation_sync": True,
        }
        report = preflight.run_preflight(
            bundle(automations=registry, live_automations=live, request=request),
            now=NOW,
        )
        self.assertFalse(report.allowed)
        self.assertTrue(report.automation_diff["has_drift"])
        self.assertEqual(report.automation_diff["field_drift"][0]["field"], "enabled")

    def test_enabled_background_monitor_does_not_consume_primary_wip(self) -> None:
        registry = [
            {"automation_id": "monitor", "enabled": True, "disposition": "BACKGROUND_MONITORING"},
            {"automation_id": "reminder", "enabled": True, "disposition": "PARKED"},
        ]
        live = [
            {"id": "monitor", "is_enabled": True},
            {"id": "reminder", "is_enabled": True},
        ]
        report = preflight.run_preflight(
            bundle(
                automations=registry,
                live_automations=live,
                primary="WF.BUFFER.2000.v1",
            ),
            now=NOW,
        )
        self.assertEqual(report.primary_wip_count, 1)

    def test_last_run_is_not_delivery_proof(self) -> None:
        registry = [
            {
                "automation_id": "auto-1",
                "enabled": True,
                "delivery_verified_from_last_run": True,
            }
        ]
        live = [
            {
                "id": "auto-1",
                "is_enabled": True,
                "last_run_time": "2026-08-23T13:11:31Z",
            }
        ]
        report = preflight.run_preflight(bundle(automations=registry, live_automations=live), now=NOW)
        self.assertTrue(any(f.code == "NOTIFICATION_RECEIPT_UNPROVEN" for f in report.findings))


class SafeEntryIntegrationTests(unittest.TestCase):
    def _write_snapshot(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_safe_entry_refuses_expired_required_state_before_core_scan(self) -> None:
        expired = state(
            "ST.EXPIRED",
            "finance:cbt:available",
            141.76,
            stale_after="2020-01-01T00:00:00Z",
            data_type="financial_balance",
        )
        request = {
            "workflow_id": "WF.CONSEQUENTIAL",
            "consequential": True,
            "required_state_keys": ["finance:cbt:available"],
            "required_fact_keys": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            snapshot = Path(tmp) / "preflight.json"
            journal = Path(tmp) / "stale.jsonl"
            self._write_snapshot(snapshot, bundle(states=[expired], request=request))
            stderr = io.StringIO()
            with mock.patch.object(safe_entry.hub, "cmd_scan", return_value=0) as cmd_scan:
                with contextlib.redirect_stderr(stderr):
                    rc = safe_entry.main(
                        [
                            "scan",
                            "--projects-root",
                            str(root),
                            "--db",
                            str(Path(tmp) / "hub.db"),
                            "--jacs-preflight-json",
                            str(snapshot),
                            "--jacs-stale-journal",
                            str(journal),
                            "--require-jacs-preflight",
                        ]
                    )
            self.assertEqual(rc, 2)
            cmd_scan.assert_not_called()
            self.assertTrue(journal.exists())
            self.assertIn("JACS preflight refused", stderr.getvalue())

    def test_safe_entry_strict_mode_refuses_missing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            stderr = io.StringIO()
            with mock.patch.object(safe_entry.hub, "cmd_scan", return_value=0) as cmd_scan:
                with contextlib.redirect_stderr(stderr):
                    rc = safe_entry.main(
                        [
                            "scan",
                            "--projects-root",
                            str(root),
                            "--db",
                            str(Path(tmp) / "hub.db"),
                            "--require-jacs-preflight",
                        ]
                    )
            self.assertEqual(rc, 2)
            cmd_scan.assert_not_called()
            self.assertIn("snapshot required", stderr.getvalue())

    def test_safe_entry_allows_fresh_reconciled_snapshot(self) -> None:
        fresh = state(
            "ST.FRESH",
            "finance:cbt:available",
            141.76,
            stale_after="2099-01-01T00:00:00Z",
            data_type="financial_balance",
        )
        request = {
            "workflow_id": "WF.CONSEQUENTIAL",
            "consequential": True,
            "required_state_keys": ["finance:cbt:available"],
            "required_fact_keys": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            snapshot = Path(tmp) / "preflight.json"
            self._write_snapshot(snapshot, bundle(states=[fresh], request=request))
            with mock.patch.object(safe_entry.hub, "cmd_scan", return_value=0) as cmd_scan:
                rc = safe_entry.main(
                    [
                        "scan",
                        "--projects-root",
                        str(root),
                        "--db",
                        str(Path(tmp) / "hub.db"),
                        "--jacs-preflight-json",
                        str(snapshot),
                        "--require-jacs-preflight",
                    ]
                )
            self.assertEqual(rc, 0)
            cmd_scan.assert_called_once()


class AdversarialFixtureTests(unittest.TestCase):
    def test_fixture_refuses_multiple_bypass_conditions(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "jacs_adversarial.json"
        with tempfile.TemporaryDirectory() as tmp:
            report = preflight.run_preflight_from_file(
                fixture,
                now=NOW,
                stale_journal_path=Path(tmp) / "stale.jsonl",
            )
        self.assertFalse(report.allowed)
        codes = {finding.code for finding in report.findings}
        self.assertIn("REQUIRED_STATE_STALE", codes)
        self.assertIn("REGISTRY_AUTHORITY_DRIFT", codes)
        self.assertIn("AUTOMATION_RUNTIME_DRIFT", codes)
        self.assertIn("NOTIFICATION_RECEIPT_UNPROVEN", codes)


if __name__ == "__main__":
    unittest.main()
