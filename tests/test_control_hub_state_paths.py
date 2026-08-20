from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = REPO_ROOT / "ops"


class ControlHubStatePathTests(unittest.TestCase):
    def import_defaults(self, environment: dict[str, str]) -> dict[str, str]:
        code = """
import json
import chat_work_agent as chat
import control_hub_agent as hub
import venture_autonomy_agent as venture
print(json.dumps({
    "hub_state": str(hub.DEFAULT_STATE_DIR),
    "hub_db": str(hub.DEFAULT_DB),
    "hub_chat": str(hub.DEFAULT_CHAT_WORK_JSON),
    "hub_venture": str(hub.DEFAULT_VENTURE_REPORT_JSON),
    "chat_json": str(chat.DEFAULT_JSON_OUT),
    "venture_json": str(venture.DEFAULT_JSON_OUT),
}))
"""
        env = os.environ.copy()
        env.pop("FLEET_CONTROL_HUB_STATE_DIR", None)
        env.pop("XDG_DATA_HOME", None)
        env.update(environment)
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env={**env, "PYTHONPATH": str(OPS_DIR)},
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.fail(f"module import failed: {result.stderr}")
        return json.loads(result.stdout)

    def assert_shared_state_dir(
        self,
        observed: dict[str, str],
        state_dir: Path,
    ) -> None:
        self.assertEqual(observed["hub_state"], str(state_dir))
        self.assertEqual(observed["hub_db"], str(state_dir / "control_hub.db"))
        self.assertEqual(observed["hub_chat"], str(state_dir / "chat_work_brief.json"))
        self.assertEqual(
            observed["hub_venture"],
            str(state_dir / "venture_autonomy_report.json"),
        )
        self.assertEqual(observed["chat_json"], observed["hub_chat"])
        self.assertEqual(observed["venture_json"], observed["hub_venture"])

    def test_explicit_runtime_state_dir_aligns_all_collectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "authoritative-state"
            observed = self.import_defaults(
                {"FLEET_CONTROL_HUB_STATE_DIR": str(state_dir)}
            )

        self.assert_shared_state_dir(observed, state_dir)

    def test_xdg_data_home_aligns_default_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_home = Path(tmp) / "xdg-data"
            state_dir = data_home / "fleet-control-hub"
            observed = self.import_defaults({"XDG_DATA_HOME": str(data_home)})

        self.assert_shared_state_dir(observed, state_dir)


if __name__ == "__main__":
    unittest.main()
