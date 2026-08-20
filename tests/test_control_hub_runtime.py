from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pwd
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = REPO_ROOT / "ops"
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import control_hub_runtime as runtime  # noqa: E402


class ControlHubRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.root.chmod(0o700)
        self.identity = runtime.current_identity()
        self.projects_root = self.root / "projects"
        self.projects_root.mkdir(mode=0o700)
        self.registry = self.root / "repo-registry.json"
        self.registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "updated_at": "2026-08-20T23:00:00Z",
                    "repositories": [],
                }
            ),
            encoding="utf-8",
        )
        self.registry.chmod(0o600)
        self.state_dir = self.root / "state"
        self.config_path = self.root / "config" / "runtime.json"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def configure(
        self,
        *,
        additional_roots: tuple[Path, ...] = (),
        replace: bool = False,
    ) -> runtime.RuntimeConfig:
        for root in additional_roots:
            root.mkdir(mode=0o700)
        args = argparse.Namespace(
            state_dir=self.state_dir,
            config=self.config_path,
            projects_root=self.projects_root,
            additional_projects_root=list(additional_roots),
            repo_registry=self.registry,
            host="127.0.0.1",
            port=8765,
            enable_window_tracking=False,
            replace_config=replace,
        )
        runtime.configure_runtime(args, self.identity)
        return runtime.load_runtime_config(self.config_path, self.identity)

    def create_database(self, path: Path | None = None) -> Path:
        db_path = path or (self.state_dir / "control_hub.db")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE operator_state (id INTEGER PRIMARY KEY, note TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO operator_state(note) VALUES (?)",
            ("preserve operator context",),
        )
        conn.commit()
        conn.close()
        db_path.chmod(0o600)
        return db_path

    def test_configure_binds_identity_paths_and_private_modes(self) -> None:
        additional = self.root / "second-root"
        config = self.configure(additional_roots=(additional,))

        self.assertEqual(config.identity, self.identity)
        self.assertEqual(config.state_dir, self.state_dir)
        self.assertEqual(config.db_path, self.state_dir / "control_hub.db")
        self.assertEqual(config.backups_dir, self.state_dir / "backups")
        self.assertEqual(
            config.projects_roots,
            (self.projects_root, additional),
        )
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.state_dir / "backups").stat().st_mode),
            0o700,
        )

    def test_config_replace_requires_explicit_flag(self) -> None:
        self.configure()
        with self.assertRaisesRegex(
            runtime.RuntimeContractError,
            "runtime config already exists",
        ):
            self.configure()

        replaced = self.configure(replace=True)
        self.assertEqual(replaced.identity, self.identity)

    def test_config_identity_mismatch_is_refused(self) -> None:
        self.configure()
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["identity"]["user"] = "another-user"
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        self.config_path.chmod(0o600)

        with self.assertRaisesRegex(
            runtime.RuntimeContractError,
            "runtime identity mismatch",
        ):
            runtime.load_runtime_config(self.config_path, self.identity)

    def test_group_or_world_readable_config_is_refused(self) -> None:
        self.configure()
        self.config_path.chmod(0o644)

        with self.assertRaisesRegex(
            runtime.RuntimeContractError,
            "permissions are too broad",
        ):
            runtime.load_runtime_config(self.config_path, self.identity)

    def test_root_identity_is_refused(self) -> None:
        root_identity = runtime.RuntimeIdentity(
            uid=0,
            gid=0,
            user="root",
            home=Path("/root"),
        )
        with self.assertRaisesRegex(
            runtime.RuntimeContractError,
            "refuses uid 0",
        ):
            runtime.require_non_root(root_identity)

    def test_cli_reports_root_identity_refusal(self) -> None:
        root_identity = runtime.RuntimeIdentity(
            uid=0,
            gid=0,
            user="root",
            home=Path("/root"),
        )
        stderr = io.StringIO()
        with mock.patch.object(runtime, "current_identity", return_value=root_identity):
            with contextlib.redirect_stderr(stderr):
                rc = runtime.main(["check", "--config", str(self.config_path)])

        self.assertEqual(rc, 2)
        self.assertIn("refuses uid 0", stderr.getvalue())

    def test_backup_verify_and_restore_preserve_database(self) -> None:
        self.configure()
        db_path = self.create_database()

        created = runtime.create_backup(self.config_path, self.identity)
        verified = runtime.verify_backup(
            self.config_path,
            Path(created["manifest"]),
            self.identity,
        )
        self.assertEqual(verified["integrity"], "ok")
        self.assertEqual(created["sha256"], verified["sha256"])
        self.assertEqual(
            stat.S_IMODE(Path(created["backup"]).stat().st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(Path(created["manifest"]).stat().st_mode),
            0o600,
        )

        original = db_path.with_suffix(".pre-restore.db")
        db_path.rename(original)
        restored = runtime.restore_backup(
            self.config_path,
            Path(created["manifest"]),
            self.identity,
        )
        self.assertEqual(restored["integrity"], "ok")
        conn = sqlite3.connect(db_path)
        note = conn.execute("SELECT note FROM operator_state").fetchone()[0]
        conn.close()
        self.assertEqual(note, "preserve operator context")
        self.assertEqual(stat.S_IMODE(db_path.stat().st_mode), 0o600)

    def test_restore_refuses_orphaned_sqlite_sidecar(self) -> None:
        self.configure()
        db_path = self.create_database()
        created = runtime.create_backup(self.config_path, self.identity)
        db_path.rename(db_path.with_suffix(".pre-restore.db"))
        wal_path = Path(f"{db_path}-wal")
        wal_path.write_bytes(b"orphaned sidecar")
        wal_path.chmod(0o600)

        with self.assertRaisesRegex(
            runtime.RuntimeContractError,
            "SQLite sidecars already exist",
        ):
            runtime.restore_backup(
                self.config_path,
                Path(created["manifest"]),
                self.identity,
            )

    def test_generated_inputs_are_tightened_before_runtime_use(self) -> None:
        config = self.configure()
        config.chat_work_json.write_text("{}", encoding="utf-8")
        config.chat_work_json.chmod(0o644)

        loaded = runtime.load_runtime_config(self.config_path, self.identity)
        runtime.prepare_runtime_paths(loaded, self.identity)

        self.assertEqual(
            stat.S_IMODE(config.chat_work_json.stat().st_mode),
            0o600,
        )

    def test_restore_refuses_while_runtime_serve_lock_is_held(self) -> None:
        config = self.configure()
        db_path = self.create_database()
        created = runtime.create_backup(self.config_path, self.identity)
        db_path.rename(db_path.with_suffix(".pre-restore.db"))

        with runtime.serve_boundary_lock(config, self.identity):
            with self.assertRaisesRegex(
                runtime.RuntimeContractError,
                "serve runtime is active",
            ):
                runtime.restore_backup(
                    self.config_path,
                    Path(created["manifest"]),
                    self.identity,
                )

    def test_backup_hash_tamper_is_refused(self) -> None:
        self.configure()
        self.create_database()
        created = runtime.create_backup(self.config_path, self.identity)
        backup_path = Path(created["backup"])
        with backup_path.open("r+b") as handle:
            original = handle.read(1)
            self.assertEqual(len(original), 1)
            handle.seek(0)
            handle.write(bytes([original[0] ^ 0xFF]))

        with self.assertRaisesRegex(
            runtime.RuntimeContractError,
            "SHA-256 does not match",
        ):
            runtime.verify_backup(
                self.config_path,
                Path(created["manifest"]),
                self.identity,
            )

    def test_migration_requires_absent_destination_and_preserves_rows(self) -> None:
        self.configure()
        source = self.create_database(self.root / "legacy.db")

        migrated = runtime.migrate_database(
            self.config_path,
            source,
            self.identity,
        )
        self.assertEqual(migrated["integrity"], "ok")
        destination = self.state_dir / "control_hub.db"
        conn = sqlite3.connect(destination)
        note = conn.execute("SELECT note FROM operator_state").fetchone()[0]
        conn.close()
        self.assertEqual(note, "preserve operator context")

        with self.assertRaisesRegex(
            runtime.RuntimeContractError,
            "authoritative database or SQLite sidecars already exist",
        ):
            runtime.migrate_database(
                self.config_path,
                source,
                self.identity,
            )

    def test_user_units_bind_runtime_config_without_user_override(self) -> None:
        self.configure()
        unit_dir = self.root / "units"
        installed = runtime.install_user_service(
            self.config_path,
            unit_dir,
            self.identity,
            no_start=True,
        )

        self.assertFalse(installed["started"])
        service = (unit_dir / runtime.SERVICE_NAME).read_text(encoding="utf-8")
        backup_service = (unit_dir / runtime.BACKUP_SERVICE_NAME).read_text(
            encoding="utf-8"
        )
        timer = (unit_dir / runtime.BACKUP_TIMER_NAME).read_text(encoding="utf-8")
        self.assertIn("hub-runtime serve --config", service)
        self.assertIn("UMask=0077", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertNotIn("\nUser=", service)
        self.assertIn("hub-runtime backup --config", backup_service)
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("Persistent=true", timer)
        for unit_name in runtime.UNIT_NAMES:
            self.assertEqual(
                stat.S_IMODE((unit_dir / unit_name).stat().st_mode),
                0o644,
            )

    def test_systemd_quoting_escapes_specifiers_and_environment_tokens(self) -> None:
        quoted = runtime.systemd_quote('/tmp/with % and $HOME/"quote"')
        self.assertEqual(
            quoted,
            '"/tmp/with %% and $$HOME/\\"quote\\""',
        )

    def test_safe_entry_arguments_are_explicit_and_disable_tracking(self) -> None:
        additional = self.root / "second-root"
        config = self.configure(additional_roots=(additional,))

        args = runtime.safe_entry_arguments(config, "scan-serve")

        self.assertEqual(args[0], "scan-serve")
        self.assertIn("--db", args)
        self.assertIn(str(config.db_path), args)
        self.assertIn("--repo-registry", args)
        self.assertIn(str(self.registry), args)
        self.assertIn("--additional-projects-root", args)
        self.assertIn(str(additional), args)
        self.assertIn("--no-window-tracking", args)


if __name__ == "__main__":
    unittest.main()
