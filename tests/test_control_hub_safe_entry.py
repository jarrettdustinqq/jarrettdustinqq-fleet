from __future__ import annotations

import contextlib
import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = REPO_ROOT / "ops"
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import control_hub_safe_entry as safe_entry  # noqa: E402


class SafeEntryParserTests(unittest.TestCase):
    def test_default_registry_follows_explicit_projects_root(self) -> None:
        with mock.patch.object(safe_entry.hub, "CONFIGURED_REPO_REGISTRY", ""):
            resolved = safe_entry.hub.resolve_repo_registry_path(
                Path("/tmp/projects"),
                None,
            )

        self.assertEqual(
            resolved,
            Path("/tmp/projects/continuity/repo-registry.json"),
        )

    def test_scan_accepts_inventory_options_after_subcommand(self) -> None:
        args = safe_entry.build_parser().parse_args(
            [
                "scan",
                "--projects-root",
                "/tmp/projects",
                "--db",
                "/tmp/control-hub.db",
                "--repo-registry",
                "/tmp/continuity/repo-registry.json",
            ]
        )

        self.assertEqual(args.cmd, "scan")
        self.assertEqual(args.projects_root, Path("/tmp/projects"))
        self.assertEqual(args.db, Path("/tmp/control-hub.db"))
        self.assertEqual(
            args.repo_registry,
            Path("/tmp/continuity/repo-registry.json"),
        )
        self.assertTrue(args.scan_first)

    def test_scan_serve_accepts_inventory_and_serve_options(self) -> None:
        args = safe_entry.build_parser().parse_args(
            [
                "scan-serve",
                "--projects-root",
                "/tmp/projects",
                "--db",
                "/tmp/control-hub.db",
                "--port",
                "9876",
                "--no-window-tracking",
            ]
        )

        self.assertEqual(args.projects_root, Path("/tmp/projects"))
        self.assertEqual(args.db, Path("/tmp/control-hub.db"))
        self.assertEqual(args.port, 9876)
        self.assertTrue(args.no_window_tracking)
        self.assertTrue(args.scan_first)


class SafeEntryGuardTests(unittest.TestCase):
    def test_missing_root_refuses_before_scan_function_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_root = Path(tmp) / "missing"
            db_path = Path(tmp) / "control-hub.db"
            stderr = io.StringIO()

            with mock.patch.object(safe_entry.hub, "cmd_scan") as cmd_scan:
                with contextlib.redirect_stderr(stderr):
                    rc = safe_entry.main(
                        [
                            "scan",
                            "--projects-root",
                            str(missing_root),
                            "--db",
                            str(db_path),
                        ]
                    )

            self.assertEqual(rc, 2)
            cmd_scan.assert_not_called()
            self.assertFalse(db_path.exists())
            self.assertIn("existing database state was not opened or pruned", stderr.getvalue())

    def test_filesystem_root_is_rejected(self) -> None:
        self.assertIn(
            "refusing to recursively scan filesystem root",
            safe_entry.projects_root_error(Path("/")) or "",
        )

    def test_existing_directory_reaches_scan_function(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects_root = Path(tmp) / "projects"
            projects_root.mkdir()

            with mock.patch.object(safe_entry.hub, "cmd_scan", return_value=0) as cmd_scan:
                rc = safe_entry.main(
                    [
                        "scan",
                        "--projects-root",
                        str(projects_root),
                        "--db",
                        str(Path(tmp) / "control-hub.db"),
                    ]
                )

            self.assertEqual(rc, 0)
            cmd_scan.assert_called_once()
            called_args = cmd_scan.call_args.args[0]
            self.assertEqual(called_args.projects_root, projects_root)

    def test_guarded_run_scan_preserves_repo_state_when_root_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_root = Path(tmp) / "missing"
            db_path = Path(tmp) / "control-hub.db"
            conn = safe_entry.hub.db_connect(db_path)
            safe_entry.hub.init_db(conn)
            conn.execute(
                """
                INSERT INTO repos (
                    path, name, branch, dirty, ahead, behind,
                    focus_level, next_action, updated_at
                ) VALUES (?, ?, ?, 0, 0, 0, 3, ?, ?)
                """,
                (
                    "/srv/preserved-repo",
                    "preserved-repo",
                    "main",
                    "keep this operator state",
                    "2026-08-14T12:00:00+00:00",
                ),
            )
            conn.commit()
            conn.close()

            with self.assertRaises(safe_entry.ScanRefusedError):
                safe_entry.guarded_run_scan(db_path, missing_root, None)

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT path, focus_level, next_action FROM repos WHERE path = ?",
                ("/srv/preserved-repo",),
            ).fetchone()
            conn.close()
            self.assertEqual(
                row,
                ("/srv/preserved-repo", 3, "keep this operator state"),
            )

    def test_http_rescan_refuses_if_root_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_root = Path(tmp) / "missing"
            handler = object.__new__(safe_entry.SafeHubHandler)
            handler.path = "/scan"
            handler.projects_root = missing_root
            handler.wfile = io.BytesIO()

            with mock.patch.object(handler, "send_response") as send_response:
                with mock.patch.object(handler, "send_header"):
                    with mock.patch.object(handler, "end_headers"):
                        handler.do_POST()

            send_response.assert_called_once_with(HTTPStatus.SERVICE_UNAVAILABLE)
            payload = handler.wfile.getvalue().decode("utf-8")
            self.assertIn("scan refused", payload)
            self.assertIn("existing database state was not opened or pruned", payload)


class FleetctlIntegrationTests(unittest.TestCase):
    def test_hub_scan_missing_root_does_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_root = Path(tmp) / "missing"
            db_path = Path(tmp) / "control-hub.db"
            result = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "fleetctl"),
                    "hub-scan",
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
            self.assertIn("scan refused", result.stderr)

    def test_existing_database_rows_survive_missing_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_root = Path(tmp) / "missing"
            db_path = Path(tmp) / "control-hub.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
            conn.execute("INSERT INTO sentinel(value) VALUES ('preserve-me')")
            conn.commit()
            conn.close()

            result = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "fleetctl"),
                    "hub-scan",
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
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT value FROM sentinel").fetchone()
            conn.close()
            self.assertEqual(row, ("preserve-me",))


if __name__ == "__main__":
    unittest.main()
