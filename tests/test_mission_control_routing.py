from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = REPO_ROOT / "ops"
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import mission_control_agent  # noqa: E402


class MissionControlCommandTests(unittest.TestCase):
    def test_scan_only_forwards_control_hub_options_after_subcommand(self) -> None:
        hub_agent = Path("/tmp/control_hub_safe_entry.py")
        subcommand, command = mission_control_agent.build_hub_command(
            "/usr/bin/python3",
            hub_agent,
            scan_only=True,
            raw_hub_args=[
                "--projects-root",
                "/tmp/projects",
                "--db=/tmp/control-hub.db",
            ],
        )

        self.assertEqual(subcommand, "scan")
        self.assertEqual(
            command,
            [
                "/usr/bin/python3",
                str(hub_agent),
                "scan",
                "--projects-root",
                "/tmp/projects",
                "--db=/tmp/control-hub.db",
            ],
        )

    def test_scan_serve_forwards_separator_without_forwarding_separator_itself(self) -> None:
        hub_agent = Path("/tmp/control_hub_safe_entry.py")
        subcommand, command = mission_control_agent.build_hub_command(
            "/usr/bin/python3",
            hub_agent,
            scan_only=False,
            raw_hub_args=["--", "--projects-root", "/tmp/projects", "--port", "9000"],
        )

        self.assertEqual(subcommand, "scan-serve")
        self.assertNotIn("--", command)
        self.assertEqual(command[-4:], ["--projects-root", "/tmp/projects", "--port", "9000"])


class MissionControlIntegrationTests(unittest.TestCase):
    def test_scan_only_missing_root_uses_guard_and_preserves_database_absence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_root = Path(tmp) / "missing"
            db_path = Path(tmp) / "control-hub.db"
            result = subprocess.run(
                [
                    sys.executable,
                    str(OPS_DIR / "mission_control_agent.py"),
                    "--scan-only",
                    "--skip-chat",
                    "--skip-venture",
                    "--projects-root",
                    str(missing_root),
                    "--db",
                    str(db_path),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(db_path.exists())
            self.assertIn("control_hub_safe_entry.py scan", result.stdout)
            self.assertIn("scan refused", result.stderr)


if __name__ == "__main__":
    unittest.main()
