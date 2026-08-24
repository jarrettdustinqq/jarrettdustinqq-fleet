from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = REPO_ROOT / "ops"
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import jacs_snapshot_boundary as boundary  # noqa: E402
import jacs_snapshot_exporter as exporter  # noqa: E402


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def make_spec(now: datetime, snapshot_id: str = "jacs-export-test-0001") -> dict:
    observed = now - timedelta(seconds=5)
    fetched = now - timedelta(seconds=2)
    return {
        "default_timezone": "America/Chicago",
        "workflow_id": "WF.JACS.EXPORT.TEST",
        "consequential": True,
        "snapshot_id": snapshot_id,
        "max_age_seconds": 300,
        "required_state_keys": ["closure:primary"],
        "required_fact_keys": ["finance:checking:available"],
        "require_automation_sync": True,
        "require_notification_receipt": False,
        "registry_read": {
            "source_ref": "google-sheets:jacs:test",
            "observed_at": iso(observed),
            "fetched_at": iso(fetched),
        },
        "registry": {
            "states": [
                {
                    "state_id": "ST.PRIMARY",
                    "canonical_key": "closure:primary",
                    "value": "WF.BUFFER.2000.v1",
                    "status": "VERIFIED",
                    "observed_at": iso(observed),
                    "stale_after": iso(now + timedelta(minutes=10)),
                },
                {
                    "state_id": "ST.BALANCE",
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
                    "timing": "BEGIN:VEVENT\nRRULE:FREQ=DAILY;BYHOUR=8\nEND:VEVENT",
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
                "source_ref": "finances:get_linked_accounts:test",
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
                "source_ref": "automations:peek:test",
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
                    "schedule": "BEGIN:VEVENT\r\nRRULE:BYHOUR=8;FREQ=DAILY\r\nEND:VEVENT\r\n",
                    "notifications_enabled": False,
                    "email_enabled": False,
                }
            ]
        },
    }


class SnapshotExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)

    def test_build_snapshot_binds_source_and_validates_boundary(self) -> None:
        bundle = exporter.build_snapshot(make_spec(self.now), now=self.now)
        self.assertEqual(bundle["envelope"]["schema_version"], 2)
        self.assertEqual(bundle["facts"][0]["source_kind"], "financial_institution")
        self.assertEqual(
            bundle["envelope"]["content_digest"],
            boundary.compute_content_digest(bundle),
        )
        boundary.validate_snapshot_envelope(
            bundle,
            now=self.now,
            default_timezone="America/Chicago",
        )

    def test_stale_connector_fetch_is_rejected(self) -> None:
        spec = make_spec(self.now)
        spec["source_reads"][0]["fetched_at"] = iso(self.now - timedelta(minutes=10))
        with self.assertRaisesRegex(exporter.SnapshotExportError, "fresh connector read"):
            exporter.build_snapshot(spec, now=self.now)

    def test_fact_cannot_spoof_source_kind(self) -> None:
        spec = make_spec(self.now)
        spec["source_reads"][0]["facts"][0]["source_kind"] = "provider"
        with self.assertRaisesRegex(exporter.SnapshotExportError, "keys mismatch"):
            exporter.build_snapshot(spec, now=self.now)

    def test_required_fact_needs_fresh_non_predictive_read(self) -> None:
        spec = make_spec(self.now)
        spec["source_reads"][0]["source_kind"] = "recurrence_prediction"
        with self.assertRaisesRegex(exporter.SnapshotExportError, "non-predictive"):
            exporter.build_snapshot(spec, now=self.now)

    def test_live_automation_requires_scheduler_receipt(self) -> None:
        spec = make_spec(self.now)
        spec["source_reads"] = spec["source_reads"][:1]
        with self.assertRaisesRegex(exporter.SnapshotExportError, "scheduler_runtime"):
            exporter.build_snapshot(spec, now=self.now)

    def test_atomic_write_is_private_and_not_canonical(self) -> None:
        bundle = exporter.build_snapshot(make_spec(self.now), now=self.now)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "jacs_preflight.json"
            exporter.write_snapshot_atomic(target, bundle)
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
            loaded = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(loaded["envelope"]["snapshot_id"], "jacs-export-test-0001")
            self.assertNotIn("canonical_writeback", loaded)

    def test_schema_and_reference_examples_are_machine_readable(self) -> None:
        schema = json.loads(
            (REPO_ROOT / "schemas" / "jacs-preflight-snapshot-v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["properties"]["envelope"]["properties"]["schema_version"]["const"],
            2,
        )
        self.assertFalse(schema["additionalProperties"])

        example_dir = REPO_ROOT / "examples" / "jacs-preflight"
        valid = json.loads((example_dir / "valid-reference.json").read_text(encoding="utf-8"))
        boundary.validate_snapshot_envelope(
            valid,
            now=self.now,
            default_timezone="America/Chicago",
        )

        refused = {
            "refused-stale.json": "expired",
            "refused-unknown-source.json": "unregistered source_kind",
            "refused-undeclared-dependency.json": "declared_fact_reads",
        }
        for filename, fragment in refused.items():
            payload = json.loads((example_dir / filename).read_text(encoding="utf-8"))
            with self.subTest(filename=filename):
                with self.assertRaisesRegex(boundary.PreflightError, fragment):
                    boundary.validate_snapshot_envelope(
                        payload,
                        now=self.now,
                        default_timezone="America/Chicago",
                    )


if __name__ == "__main__":
    unittest.main()
