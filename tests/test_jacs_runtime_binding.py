from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = REPO_ROOT / "ops"
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import control_hub_safe_entry as safe_entry  # noqa: E402


class JACSRuntimeBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous = (
            safe_entry._JACS_PREFLIGHT_JSON,
            safe_entry._JACS_STALE_JOURNAL,
            safe_entry._JACS_PREFLIGHT_REQUIRED,
        )

    def tearDown(self) -> None:
        (
            safe_entry._JACS_PREFLIGHT_JSON,
            safe_entry._JACS_STALE_JOURNAL,
            safe_entry._JACS_PREFLIGHT_REQUIRED,
        ) = self.previous

    def test_runtime_caller_uses_actual_db_parent_not_environment_defaults(self) -> None:
        args = argparse.Namespace(
            db=Path("/custom/runtime/state/control_hub.db"),
            jacs_preflight_json=Path("/wrong/default/jacs_preflight.json"),
            jacs_stale_journal=Path("/wrong/default/jacs_stale_events.jsonl"),
            require_jacs_preflight=True,
        )
        with mock.patch.object(sys, "argv", ["/repo/ops/control_hub_runtime.py"]):
            safe_entry.configure_jacs_preflight(args)

        self.assertTrue(safe_entry._JACS_PREFLIGHT_REQUIRED)
        self.assertEqual(
            safe_entry._JACS_PREFLIGHT_JSON,
            Path("/custom/runtime/state/jacs_preflight.json"),
        )
        self.assertEqual(
            safe_entry._JACS_STALE_JOURNAL,
            Path("/custom/runtime/state/jacs_stale_events.jsonl"),
        )

    def test_non_runtime_explicit_snapshot_is_preserved(self) -> None:
        args = argparse.Namespace(
            db=Path("/custom/runtime/state/control_hub.db"),
            jacs_preflight_json=Path("/reviewed/snapshot.json"),
            jacs_stale_journal=Path("/reviewed/stale.jsonl"),
            require_jacs_preflight=True,
        )
        with mock.patch.object(sys, "argv", ["/repo/ops/control_hub_safe_entry.py"]):
            safe_entry.configure_jacs_preflight(args)

        self.assertEqual(
            safe_entry._JACS_PREFLIGHT_JSON,
            Path("/reviewed/snapshot.json"),
        )
        self.assertEqual(
            safe_entry._JACS_STALE_JOURNAL,
            Path("/reviewed/stale.jsonl"),
        )


if __name__ == "__main__":
    unittest.main()
