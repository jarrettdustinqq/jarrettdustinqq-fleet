from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "ops" / "control_hub_agent.py"
SPEC = importlib.util.spec_from_file_location("control_hub_agent_reconciliation", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load module spec from {MODULE_PATH}")
HUB = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HUB
SPEC.loader.exec_module(HUB)


class RepoReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.db_path = self.root / "control-hub.db"
        self.chat_report = self.root / "missing-chat-report.json"
        self.venture_report = self.root / "missing-venture-report.json"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _seed_repo(
        self,
        path: str = "/srv/preserved-repo",
        *,
        inventory_status: str = "active",
    ) -> None:
        conn = HUB.db_connect(self.db_path)
        HUB.init_db(conn)
        conn.execute(
            """
            INSERT INTO repos (
                path, name, branch, dirty, ahead, behind,
                focus_level, next_action, inventory_status, updated_at
            ) VALUES (?, 'preserved-repo', 'main', 0, 0, 0, 3, ?, ?, ?)
            """,
            (
                path,
                "keep this operator state",
                inventory_status,
                "2026-08-20T12:00:00+00:00",
            ),
        )
        conn.commit()
        conn.close()

    def _write_registry(
        self,
        repositories: list[dict[str, str]],
        *,
        schema_version: int = 1,
    ) -> Path:
        path = self.root / "repo-registry.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": schema_version,
                    "updated_at": "2026-08-20T22:00:00Z",
                    "repositories": repositories,
                }
            ),
            encoding="utf-8",
        )
        return path

    def _registry_repo(self, name: str) -> dict[str, str]:
        return {
            "name": name,
            "class": "support",
            "role": "test repository",
            "default_branch": "main",
            "visibility": "private",
            "status": "active",
            "boundary": "Test boundary.",
            "next_action": "Test next action.",
        }

    def _init_repo(self, path: Path, remote_url: str | None = None) -> None:
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(
            ["git", "-C", str(path), "config", "user.name", "Fleet Test"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "config",
                "user.email",
                "fleet-test@example.invalid",
            ],
            check=True,
        )
        (path / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(path), "commit", "-qm", "fixture"],
            check=True,
        )
        if remote_url:
            subprocess.run(
                ["git", "-C", str(path), "remote", "add", "origin", remote_url],
                check=True,
            )

    def _run_scan(
        self,
        projects_root: Path,
        *,
        repo_registry_path: Path | None = None,
    ) -> dict[str, int | str]:
        return HUB.run_scan(
            self.db_path,
            projects_root,
            None,
            chat_work_json=self.chat_report,
            venture_report_json=self.venture_report,
            repo_registry_path=(
                repo_registry_path
                if repo_registry_path is not None
                else self.root / "missing-repo-registry.json"
            ),
        )

    def test_failed_root_refuses_before_database_open(self) -> None:
        with self.assertRaises(HUB.RepoDiscoveryError):
            self._run_scan(self.root / "missing")

        self.assertFalse(self.db_path.exists())

    def test_verified_empty_root_stale_marks_without_deleting_operator_state(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()
        self._seed_repo()

        summary = self._run_scan(projects_root)

        self.assertEqual(summary["repo_scan_status"], HUB.REPO_SCAN_COMPLETE)
        self.assertEqual(summary["repos"], 0)
        self.assertEqual(summary["stale_repos"], 1)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            """
            SELECT focus_level, next_action, inventory_status, missing_since
            FROM repos WHERE path = '/srv/preserved-repo'
            """
        ).fetchone()
        scan_run = conn.execute(
            """
            SELECT status, observed_repo_count, stale_repo_count
            FROM repo_scan_runs ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        conn.close()

        self.assertEqual(row[:3], (3, "keep this operator state", "stale"))
        self.assertIsNotNone(row[3])
        self.assertEqual(scan_run, (HUB.REPO_SCAN_COMPLETE, 0, 1))

    def test_repeated_empty_scan_preserves_original_missing_timestamp(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()
        self._seed_repo()

        self._run_scan(projects_root)
        conn = sqlite3.connect(self.db_path)
        first_missing_since = conn.execute(
            "SELECT missing_since FROM repos WHERE path = '/srv/preserved-repo'"
        ).fetchone()[0]
        conn.close()

        summary = self._run_scan(projects_root)
        conn = sqlite3.connect(self.db_path)
        second_missing_since = conn.execute(
            "SELECT missing_since FROM repos WHERE path = '/srv/preserved-repo'"
        ).fetchone()[0]
        conn.close()

        self.assertEqual(summary["stale_repos"], 0)
        self.assertEqual(second_missing_since, first_missing_since)

    def test_legacy_prune_entrypoint_stale_marks_instead_of_deleting(self) -> None:
        self._seed_repo()
        conn = sqlite3.connect(self.db_path)
        HUB.prune_missing_repos(conn, [])
        conn.commit()
        row = conn.execute(
            "SELECT inventory_status, focus_level, next_action FROM repos"
        ).fetchone()
        conn.close()

        self.assertEqual(row, ("stale", 3, "keep this operator state"))

    def test_partial_scan_preserves_rows_and_open_recommendations(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()
        self._seed_repo()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO recommendations (
                fingerprint, category, title, details, priority, status, updated_at
            ) VALUES ('preserve-me', 'inventory', 'Existing', 'Keep', 1, 'open', ?)
            """,
            ("2026-08-20T12:00:00+00:00",),
        )
        conn.commit()
        conn.close()

        partial = HUB.RepoDiscoveryResult(
            HUB.REPO_SCAN_PARTIAL,
            (),
            ("subdirectory unreadable",),
            projects_root,
        )
        with mock.patch.object(HUB, "discover_git_repos", return_value=partial):
            summary = self._run_scan(projects_root)

        conn = sqlite3.connect(self.db_path)
        repo_row = conn.execute(
            "SELECT inventory_status, focus_level, next_action FROM repos"
        ).fetchone()
        recommendation = conn.execute(
            "SELECT status FROM recommendations WHERE fingerprint = 'preserve-me'"
        ).fetchone()
        recommendation_titles = {
            row[0] for row in conn.execute("SELECT title FROM recommendations")
        }
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        scan_run = conn.execute(
            "SELECT status, stale_repo_count FROM repo_scan_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()

        self.assertEqual(summary["repo_scan_status"], HUB.REPO_SCAN_PARTIAL)
        self.assertEqual(summary["stale_repos"], 0)
        self.assertEqual(repo_row, ("active", 3, "keep this operator state"))
        self.assertEqual(recommendation, ("open",))
        self.assertIn("Repository observation incomplete", recommendation_titles)
        self.assertNotIn("No git repositories discovered", recommendation_titles)
        self.assertEqual(meta["last_repo_scan_status"], HUB.REPO_SCAN_PARTIAL)
        self.assertEqual(scan_run, (HUB.REPO_SCAN_PARTIAL, 0))

    def test_walk_error_makes_observation_partial(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()

        def walk_with_error(_root: Path, *, onerror=None):
            if onerror is not None:
                onerror(PermissionError("blocked subtree"))
            return iter(())

        with mock.patch.object(HUB.os, "walk", side_effect=walk_with_error):
            result = HUB.discover_git_repos(projects_root)

        self.assertEqual(result.status, HUB.REPO_SCAN_PARTIAL)
        self.assertEqual(result.repositories, ())
        self.assertIn("blocked subtree", result.errors[0])

    def test_malformed_git_file_is_partial_not_authoritative_empty(self) -> None:
        projects_root = self.root / "projects"
        candidate = projects_root / "broken-worktree"
        candidate.mkdir(parents=True)
        (candidate / ".git").write_text("not a valid gitdir\n", encoding="utf-8")

        result = HUB.discover_git_repos(projects_root)

        self.assertEqual(result.status, HUB.REPO_SCAN_PARTIAL)
        self.assertEqual(result.repositories, ())
        self.assertTrue(any("invalid Git candidate" in error for error in result.errors))

    def test_git_file_worktree_is_discovered(self) -> None:
        projects_root = self.root / "projects"
        primary = projects_root / "primary"
        linked = projects_root / "linked"
        self._init_repo(primary)
        subprocess.run(
            ["git", "-C", str(primary), "worktree", "add", "-qb", "linked-test", str(linked)],
            check=True,
        )

        result = HUB.discover_git_repos(projects_root)

        self.assertEqual(result.status, HUB.REPO_SCAN_COMPLETE)
        self.assertEqual(set(result.repositories), {primary.resolve(), linked.resolve()})

    def test_registry_only_repository_is_visible_without_local_checkout(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()
        registry_path = self._write_registry(
            [self._registry_repo("jarrettdustinqq/remote-only")]
        )

        summary = self._run_scan(
            projects_root,
            repo_registry_path=registry_path,
        )

        conn = HUB.db_connect(self.db_path)
        state = HUB.query_dashboard_state(conn)
        dashboard = HUB.render_dashboard(state)
        registered = conn.execute(
            """
            SELECT registry_name, registry_present, focus_level,
                   operator_next_action
            FROM registered_repos
            """
        ).fetchone()
        conn.close()

        self.assertEqual(summary["repo_registry_status"], HUB.REPO_REGISTRY_COMPLETE)
        self.assertEqual(summary["registered_repos"], 1)
        self.assertEqual(summary["repos"], 0)
        self.assertEqual(
            tuple(registered),
            ("jarrettdustinqq/remote-only", 1, 0, ""),
        )
        self.assertEqual(len(state["registered_repos"]), 1)
        self.assertEqual(state["registered_repos"][0]["checkout_count"], 0)
        self.assertIn("Canonical Repository Registry", dashboard)
        self.assertIn("jarrettdustinqq/remote-only", dashboard)

    def test_two_linked_worktrees_share_one_canonical_registry_identity(self) -> None:
        projects_root = self.root / "projects"
        primary = projects_root / "primary"
        linked = projects_root / "linked"
        remote_url = (
            "https://inventory-user:do-not-store@github.com/"
            "JarrettDustinQQ/example.git?access_token=do-not-store"
        )
        self._init_repo(primary, remote_url)
        subprocess.run(
            [
                "git",
                "-C",
                str(primary),
                "worktree",
                "add",
                "-qb",
                "linked-registry-test",
                str(linked),
            ],
            check=True,
        )
        registry_path = self._write_registry(
            [self._registry_repo("jarrettdustinqq/example")]
        )

        summary = self._run_scan(
            projects_root,
            repo_registry_path=registry_path,
        )

        conn = HUB.db_connect(self.db_path)
        observations = conn.execute(
            """
            SELECT path, registry_name, remote_url
            FROM repos ORDER BY path
            """
        ).fetchall()
        state = HUB.query_dashboard_state(conn)
        conn.close()

        self.assertEqual(summary["registered_repos"], 1)
        self.assertEqual(summary["repos"], 2)
        self.assertEqual(len(observations), 2)
        self.assertEqual(
            {row["registry_name"] for row in observations},
            {"jarrettdustinqq/example"},
        )
        self.assertEqual(
            {row["remote_url"] for row in observations},
            {"https://github.com/JarrettDustinQQ/example.git"},
        )
        self.assertNotIn("do-not-store", json.dumps([dict(row) for row in observations]))
        self.assertEqual(state["registered_repos"][0]["checkout_count"], 2)
        self.assertEqual(state["registered_repos"][0]["active_checkout_count"], 2)

    def test_invalid_registry_preserves_prior_canonical_and_operator_state(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()
        registry_path = self._write_registry(
            [self._registry_repo("jarrettdustinqq/preserved")]
        )
        self._run_scan(projects_root, repo_registry_path=registry_path)

        conn = HUB.db_connect(self.db_path)
        conn.execute(
            """
            UPDATE registered_repos
            SET focus_level = 3, operator_next_action = 'preserve canonical focus'
            WHERE registry_name = 'jarrettdustinqq/preserved'
            """
        )
        conn.commit()
        conn.close()

        self._write_registry(
            [
                self._registry_repo("jarrettdustinqq/preserved"),
                self._registry_repo("JARRETTDUSTINQQ/PRESERVED"),
            ]
        )
        summary = self._run_scan(projects_root, repo_registry_path=registry_path)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            """
            SELECT registry_name, registry_present, focus_level,
                   operator_next_action
            FROM registered_repos
            """
        ).fetchone()
        scan_run = conn.execute(
            """
            SELECT repo_registry_status, registered_repo_count
            FROM repo_scan_runs ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        conn.close()

        self.assertEqual(summary["repo_registry_status"], HUB.REPO_REGISTRY_INVALID)
        self.assertEqual(summary["registered_repos"], 1)
        self.assertEqual(
            row,
            (
                "jarrettdustinqq/preserved",
                1,
                3,
                "preserve canonical focus",
            ),
        )
        self.assertEqual(scan_run, (HUB.REPO_REGISTRY_INVALID, 1))

    def test_registry_import_migrates_legacy_path_management_state_once(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()
        self._seed_repo()
        conn = HUB.db_connect(self.db_path)
        conn.execute(
            """
            UPDATE repos
            SET remote_url = 'git@github.com:jarrettdustinqq/preserved-repo.git'
            WHERE path = '/srv/preserved-repo'
            """
        )
        conn.commit()
        conn.close()
        registry_path = self._write_registry(
            [self._registry_repo("jarrettdustinqq/preserved-repo")]
        )

        self._run_scan(projects_root, repo_registry_path=registry_path)

        conn = HUB.db_connect(self.db_path)
        canonical = conn.execute(
            """
            SELECT focus_level, operator_next_action, legacy_state_migrated
            FROM registered_repos
            """
        ).fetchone()
        conn.execute(
            """
            UPDATE registered_repos
            SET focus_level = 1, operator_next_action = 'canonical override'
            """
        )
        conn.execute(
            """
            UPDATE repos
            SET focus_level = 3, next_action = 'changed legacy value'
            """
        )
        conn.commit()
        conn.close()

        self._run_scan(projects_root, repo_registry_path=registry_path)
        conn = sqlite3.connect(self.db_path)
        after_second_import = conn.execute(
            """
            SELECT focus_level, operator_next_action, legacy_state_migrated
            FROM registered_repos
            """
        ).fetchone()
        conn.close()

        self.assertEqual(
            tuple(canonical),
            (3, "keep this operator state", 1),
        )
        self.assertEqual(after_second_import, (1, "canonical override", 1))

    def test_legacy_state_migrates_after_same_scan_remote_discovery(self) -> None:
        projects_root = self.root / "projects"
        checkout = projects_root / "legacy-checkout"
        self._init_repo(
            checkout,
            "https://github.com/jarrettdustinqq/newly-linked.git",
        )
        self._seed_repo(path=str(checkout))
        registry_path = self._write_registry(
            [self._registry_repo("jarrettdustinqq/newly-linked")]
        )

        self._run_scan(projects_root, repo_registry_path=registry_path)

        conn = sqlite3.connect(self.db_path)
        canonical = conn.execute(
            """
            SELECT focus_level, operator_next_action, legacy_state_migrated
            FROM registered_repos
            """
        ).fetchone()
        observation = conn.execute(
            "SELECT registry_name FROM repos WHERE path = ?",
            (str(checkout.resolve()),),
        ).fetchone()
        conn.close()

        self.assertEqual(canonical, (3, "keep this operator state", 1))
        self.assertEqual(observation, ("jarrettdustinqq/newly-linked",))

    def test_complete_registry_removal_marks_canonical_row_without_deleting_it(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()
        registry_path = self._write_registry(
            [self._registry_repo("jarrettdustinqq/retired")]
        )
        self._run_scan(projects_root, repo_registry_path=registry_path)

        conn = HUB.db_connect(self.db_path)
        conn.execute(
            """
            UPDATE registered_repos
            SET focus_level = 2, operator_next_action = 'review retirement'
            """
        )
        conn.commit()
        conn.close()

        self._write_registry([])
        summary = self._run_scan(projects_root, repo_registry_path=registry_path)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            """
            SELECT registry_present, focus_level, operator_next_action
            FROM registered_repos
            """
        ).fetchone()
        conn.close()

        self.assertEqual(summary["repo_registry_status"], HUB.REPO_REGISTRY_COMPLETE)
        self.assertEqual(summary["registered_repos"], 0)
        self.assertEqual(row, (0, 2, "review retirement"))

    def test_unsupported_registry_schema_is_rejected_without_database_loss(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()
        self._seed_repo()
        registry_path = self._write_registry([], schema_version=999)

        summary = self._run_scan(
            projects_root,
            repo_registry_path=registry_path,
        )

        conn = sqlite3.connect(self.db_path)
        preserved = conn.execute(
            "SELECT focus_level, next_action FROM repos WHERE path = ?",
            ("/srv/preserved-repo",),
        ).fetchone()
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        conn.close()

        self.assertEqual(summary["repo_registry_status"], HUB.REPO_REGISTRY_INVALID)
        self.assertEqual(preserved, (3, "keep this operator state"))
        self.assertEqual(meta["last_repo_registry_status"], HUB.REPO_REGISTRY_INVALID)

    def test_registry_validation_errors_are_bounded_with_omission_marker(self) -> None:
        registry_path = self._write_registry([{} for _ in range(10)])

        result = HUB.load_repo_registry(registry_path)

        self.assertEqual(result.status, HUB.REPO_REGISTRY_INVALID)
        self.assertEqual(len(result.errors), HUB.MAX_REPO_REGISTRY_ERRORS)
        self.assertEqual(
            result.errors[-1],
            "additional repository registry errors omitted",
        )
        self.assertTrue(
            all(
                len(error) <= HUB.MAX_REPO_REGISTRY_ERROR_CHARS
                for error in result.errors
            )
        )

    def test_schema_migration_preserves_inventory_context_and_scan_history(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE repos (
                path TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                branch TEXT,
                dirty INTEGER NOT NULL DEFAULT 0,
                ahead INTEGER NOT NULL DEFAULT 0,
                behind INTEGER NOT NULL DEFAULT 0,
                last_commit_at TEXT,
                last_commit_age_days INTEGER,
                remote_url TEXT,
                focus_level INTEGER NOT NULL DEFAULT 0,
                next_action TEXT NOT NULL DEFAULT '',
                inventory_status TEXT NOT NULL DEFAULT 'active',
                last_seen_at TEXT,
                missing_since TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE repo_scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                projects_root TEXT NOT NULL,
                status TEXT NOT NULL,
                observed_repo_count INTEGER NOT NULL DEFAULT 0,
                stale_repo_count INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE recommendations (
                fingerprint TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                details TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 3,
                status TEXT NOT NULL DEFAULT 'open',
                updated_at TEXT NOT NULL
            );
            INSERT INTO repos (
                path, name, branch, focus_level, next_action,
                inventory_status, last_seen_at, missing_since, updated_at
            ) VALUES (
                '/srv/preserved', 'preserved', 'main', 3, 'retain operator context',
                'stale', '2026-08-01T00:00:00+00:00',
                '2026-08-02T00:00:00+00:00', '2026-08-02T00:00:00+00:00'
            );
            INSERT INTO repo_scan_runs (
                started_at, completed_at, projects_root, status,
                observed_repo_count, stale_repo_count, errors_json
            ) VALUES (
                '2026-08-01T00:00:00+00:00', '2026-08-01T00:01:00+00:00',
                '/srv', 'complete', 1, 0, '[]'
            );
            INSERT INTO recommendations (
                fingerprint, category, title, details, priority, status, updated_at
            ) VALUES (
                'preserve-recommendation', 'inventory', 'Preserve', 'Keep this row',
                1, 'open', '2026-08-01T00:00:00+00:00'
            );
            """
        )
        conn.commit()
        conn.close()

        conn = HUB.db_connect(self.db_path)
        HUB.init_db(conn)
        repo = conn.execute(
            """
            SELECT focus_level, next_action, inventory_status, missing_since
            FROM repos
            """
        ).fetchone()
        recommendation = conn.execute(
            "SELECT status FROM recommendations WHERE fingerprint = ?",
            ("preserve-recommendation",),
        ).fetchone()
        scan_run = conn.execute(
            """
            SELECT status, observed_repo_count, stale_repo_count
            FROM repo_scan_runs WHERE id = 1
            """
        ).fetchone()
        repo_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(repos)")
        }
        scan_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(repo_scan_runs)")
        }
        conn.close()

        self.assertEqual(
            tuple(repo),
            (3, "retain operator context", "stale", "2026-08-02T00:00:00+00:00"),
        )
        self.assertEqual(tuple(recommendation), ("open",))
        self.assertEqual(tuple(scan_run), ("complete", 1, 0))
        self.assertIn("registry_name", repo_columns)
        self.assertIn("repo_registry_status", scan_columns)

    def test_init_migration_sanitizes_credentialed_remote_urls(self) -> None:
        self._seed_repo()
        conn = HUB.db_connect(self.db_path)
        conn.execute(
            """
            UPDATE repos
            SET remote_url = 'https://user:secret@github.com/org/repo.git?token=secret'
            """
        )
        conn.commit()
        HUB.init_db(conn)
        stored = conn.execute("SELECT remote_url FROM repos").fetchone()[0]
        conn.close()

        self.assertEqual(stored, "https://github.com/org/repo.git")


if __name__ == "__main__":
    unittest.main()
