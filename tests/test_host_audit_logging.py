"""A host audit must not finish while its report writer is still pending."""

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "ops/fleet_host_remediation.sh"


class HostAuditLoggingTests(unittest.TestCase):
    def run_audit(self, *, wrong_user=False, writer_status=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fleet = root / "workspace/fleet"
            (fleet / "ops").mkdir(parents=True)
            (fleet / "ops/control_hub_agent.py").write_text('print("fixture")\n')
            subprocess.run(["git", "init", "-q", str(fleet)], check=True)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            writer = bin_dir / "tee"
            writer.write_text(
                f"#!{sys.executable}\n"
                "import os, pathlib, socket, sys\n"
                "data = sys.stdin.buffer.read()\n"
                "gate = socket.socket(fileno=int(os.environ['AUDIT_TEST_GATE_FD']))\n"
                "gate.sendall(b'ready')\n"
                "assert gate.recv(1) == b'g'\n"
                "with pathlib.Path(sys.argv[-1]).open('ab') as report:\n"
                "    report.write(data)\n"
                "sys.stdout.buffer.write(data)\n"
                "sys.exit(int(os.environ['AUDIT_TEST_WRITER_STATUS']))\n"
            )
            writer.chmod(0o755)
            with tempfile.TemporaryFile() as output:
                parent, child = socket.socketpair()
                with parent, child:
                    parent.settimeout(10)
                    environment = {
                        **os.environ,
                        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                        "FLEET_USER_HOME": str(root),
                        "FLEET_EXPECTED_USER": "unmatched-test-user" if wrong_user else subprocess.check_output(["id", "-un"], text=True).strip(),
                        "FLEET_ALLOW_OTHER_USER": "0",
                        "AUDIT_TEST_GATE_FD": str(child.fileno()),
                        "AUDIT_TEST_WRITER_STATUS": str(writer_status),
                    }
                    process = subprocess.Popen(
                        ["bash", str(SCRIPT), "--quick", "--no-fetch",
                         "--output-root", str(root / "output"),
                         "--outer-workspace", str(fleet.parent), "--fleet-dir", str(fleet)],
                        env=environment, pass_fds=(child.fileno(),),
                        stdout=output, stderr=output,
                    )
                    try:
                        self.assertEqual(parent.recv(5), b"ready")
                        # The writer has received EOF but cannot finish until released.
                        with self.assertRaises(subprocess.TimeoutExpired):
                            process.wait(timeout=0.2)
                    finally:
                        parent.sendall(b"g")
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                            raise
                expected_status = 2 if wrong_user else writer_status
                self.assertEqual(process.returncode, expected_status)
                report = next((root / "output").glob("*/report.txt")).read_text()
                if wrong_user:
                    self.assertIn("ERROR: run as unmatched-test-user", report)
                else:
                    self.assertIn("REQUIRED_FAILURE_COUNT=0", report)
                    self.assertIn("NO_SYSTEMD_UNIT_CHANGED=YES", report)

    def test_waits_for_complete_report_before_success(self):
        self.run_audit()

    def test_flushes_report_and_preserves_early_failure(self):
        self.run_audit(wrong_user=True, writer_status=7)

    def test_report_writer_failure_prevents_success(self):
        self.run_audit(writer_status=7)


if __name__ == "__main__":
    unittest.main()
